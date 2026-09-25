# api/middleware.py
"""
AXELR API — HTTP middleware, exception handlers, monitor endpoints, lifespan.
=============================================================================
Single home for:
  * CORS + logging + metrics + security headers + body-size + monitor safety
  * All exception handlers (validation / HTTP / unhandled)
  * Monitor endpoints (liveness, readiness, root, echo, probe) — one route per path
  * Background tasks: provider health, PR cleanup, keepalive
  * ``lifespan`` — the ONLY place runtime singletons are constructed
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import Counter, REGISTRY
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response as _StarletteResponse

from .config import ORIGIN, ENABLE_PR_DEFENSE, WORKSPACE_ROOT
from .providers import (
    PROVIDER_CHAIN,
    PROVIDER_KEY_CHECK,
    PROVIDER_MODELS,
    record_provider_result,
)
from .state import (
    state,
    init_redis,
    init_db,
    init_qstash,
    limiter,
    RateLimitExceeded,
    _rate_limit_exceeded_handler,
    get_redis_cache,
    set_redis_cache,
)

logger = structlog.get_logger("axelr")


# ═══════════════════════════════════════════════════════════════════════════
# Monitor path registry
# ═══════════════════════════════════════════════════════════════════════════

MONITOR_PATHS = frozenset((
    "/", "/terms", "/privacy",
    "/ping", "/livez", "/healthz", "/api/live", "/api/ping",
    "/health", "/api/health", "/status", "/api/status",
    "/readyz", "/api/ready", "/api/health/live", "/api/health/ready",
    "/favicon.ico", "/robots.txt",
))

LIVENESS_PATHS = ("/ping", "/livez", "/healthz", "/api/live", "/api/ping")
READINESS_PATHS = ("/health", "/api/health", "/status", "/api/status",
                   "/readyz", "/api/ready")

MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", str(60 * 1024 * 1024)))


# ═══════════════════════════════════════════════════════════════════════════
# Prometheus metrics (idempotent registration)
# ═══════════════════════════════════════════════════════════════════════════

def _metric(name: str, factory):
    try:
        return factory()
    except ValueError:
        return REGISTRY._names_to_collectors[name]


REQUESTS = _metric(
    "http_requests_total",
    lambda: Counter("http_requests_total", "Total HTTP requests",
                    ["method", "endpoint", "status"]),
)


# ═══════════════════════════════════════════════════════════════════════════
# Exception handlers
# ═══════════════════════════════════════════════════════════════════════════

async def _validation_err(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "code": "VALIDATION_ERROR",
            "message": "Request body failed validation.",
            "errors": exc.errors(),
        },
    )


async def _http_err(request: Request, exc: StarletteHTTPException):
    """
    Soften 404/405 on monitor paths → 200. Soften all 405s on safe read
    methods (GET/HEAD/OPTIONS) → 200, since Starlette auto-adds HEAD to
    every GET route.
    """
    path = request.url.path
    norm = path.rstrip("/") or "/"
    is_monitor = path in MONITOR_PATHS or norm in MONITOR_PATHS
    is_safe = request.method in ("GET", "HEAD", "OPTIONS")

    if exc.status_code in (404, 405) and (
        is_monitor or (exc.status_code == 405 and is_safe)
    ):
        logger.info("monitor_exception_softened", path=path,
                    method=request.method, status=exc.status_code)
        if request.method in ("HEAD", "OPTIONS"):
            return Response(status_code=200)
        return JSONResponse(
            status_code=200,
            content={
                "status": "ok", "path": path, "method": request.method,
                "note": "method_not_native_but_endpoint_alive",
            },
            headers={"Cache-Control": "no-store"},
        )

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "code": f"HTTP_{exc.status_code}",
            "message": exc.detail,
        },
        headers=getattr(exc, "headers", None),
    )


async def _unhandled_err(request: Request, exc: Exception):
    if isinstance(exc, StarletteHTTPException):
        raise exc
    rid = getattr(request.state, "request_id", "unknown")
    logger.exception("unhandled_exception", rid=rid,
                     path=request.url.path, error=str(exc))
    return JSONResponse(
        status_code=500,
        content={
            "success": False, "code": "INTERNAL_ERROR",
            "message": "Internal server error.", "request_id": rid,
        },
    )


# ═══════════════════════════════════════════════════════════════════════════
# Middleware functions
# ═══════════════════════════════════════════════════════════════════════════

async def logging_middleware(request: Request, call_next):
    import uuid
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())[:8]
    request.state.request_id = request_id
    start = time.time()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "http_request",
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=round((time.time() - start) * 1000, 2),
    )
    return response


async def metrics_middleware(request: Request, call_next):
    response = await call_next(request)
    REQUESTS.labels(
        method=request.method,
        endpoint=request.url.path,
        status=response.status_code,
    ).inc()
    return response


async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    if request.url.path in ("/", "/terms", "/privacy"):
        csp = (
            "default-src 'self'; "
            "script-src 'self' https://accounts.google.com https://cdn.jsdelivr.net "
            "https://cdnjs.cloudflare.com 'unsafe-inline'; "
            "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "img-src 'self' data: blob:; "
            "connect-src 'self' https://axelr-backend.onrender.com https://api.puter.com; "
            "frame-src 'self' https://accounts.google.com; "
            "object-src 'none'; base-uri 'self'; form-action 'self'; "
            "upgrade-insecure-requests"
        )
    else:
        csp = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"

    response.headers["Content-Security-Policy"]   = csp
    response.headers["X-Content-Type-Options"]    = "nosniff"
    response.headers["X-Frame-Options"]           = "DENY"
    response.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"]        = "geolocation=(), camera=(), microphone=()"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


async def body_size_guard(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH"):
        cl = request.headers.get("content-length")
        if cl:
            try:
                if int(cl) > MAX_BODY_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content={
                            "success": False,
                            "code": "PAYLOAD_TOO_LARGE",
                            "message": f"Body exceeds {MAX_BODY_BYTES} bytes",
                        },
                    )
            except ValueError:
                pass
    return await call_next(request)


async def monitor_safety_net(request: Request, call_next):
    """
    Belt-and-suspenders guard for monitor traffic.
    Any 404/405 that slipped past _http_err on a monitor path → 200.
    Any exception on a monitor path → 200 'degraded'.
    """
    path = request.url.path
    norm = path.rstrip("/") or "/"
    is_monitor = path in MONITOR_PATHS or norm in MONITOR_PATHS

    try:
        response = await call_next(request)
    except Exception as exc:
        if is_monitor:
            logger.warning("monitor_middleware_exception", path=path,
                           method=request.method, error=str(exc))
            if request.method in ("HEAD", "OPTIONS"):
                return Response(status_code=200)
            return JSONResponse(status_code=200, content={"status": "degraded"})
        raise

    if is_monitor and response.status_code in (404, 405):
        logger.info("monitor_middleware_softened", path=path,
                    method=request.method, status=response.status_code)
        if request.method in ("HEAD", "OPTIONS"):
            return Response(status_code=200)
        return JSONResponse(status_code=200, content={"status": "ok"})

    return response


# ═══════════════════════════════════════════════════════════════════════════
# Monitor endpoints
# ═══════════════════════════════════════════════════════════════════════════

def _no_body_ok() -> _StarletteResponse:
    return _StarletteResponse(
        status_code=200,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "X-Robots-Tag": "noindex",
        },
    )


async def _liveness(request: Request):
    if request.method in ("HEAD", "OPTIONS"):
        return _no_body_ok()
    return _StarletteResponse(
        status_code=200,
        content=b"ok",
        media_type="text/plain; charset=utf-8",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "X-Robots-Tag": "noindex",
        },
    )


async def _readiness(request: Request):
    if request.method in ("HEAD", "OPTIONS"):
        return _no_body_ok()

    db_status = "disabled"
    redis_status = "disabled"

    if state.db_available and state.db is not None:
        try:
            await asyncio.wait_for(state.db.command("ping"), timeout=1.5)
            db_status = "connected"
        except Exception:
            db_status = "disconnected"

    if state.redis_client is not None:
        try:
            await asyncio.wait_for(state.redis_client.ping(), timeout=1.0)
            redis_status = "connected"
        except Exception:
            redis_status = "disconnected"

    overall = "operational" if db_status == "connected" else "degraded"
    start_time = getattr(state, "start_time", None) or time.time()

    return JSONResponse(
        status_code=200,
        content={
            "status": overall,
            "service": "axelr-backend",
            "version": "24.4",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "uptime_seconds": round(time.time() - start_time, 2),
            "checks": {"database": db_status, "redis": redis_status},
        },
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "X-Robots-Tag": "noindex",
        },
    )


async def _root_probe(request: Request):
    if request.method in ("HEAD", "OPTIONS"):
        return _no_body_ok()
    start_time = getattr(state, "start_time", None) or time.time()
    return JSONResponse(
        status_code=200,
        content={
            "service": "axelr-backend",
            "status": "ok",
            "version": "24.4",
            "uptime_seconds": round(time.time() - start_time, 2),
        },
        headers={"Cache-Control": "no-store"},
    )


async def _debug_echo(request: Request):
    if request.method == "HEAD":
        return _StarletteResponse(status_code=200)
    return {
        "method": request.method,
        "path": request.url.path,
        "query": str(request.query_params),
        "headers": dict(request.headers),
        "client": request.client.host if request.client else None,
    }


async def _monitor_probe(request: Request):
    if request.method == "HEAD":
        return Response(status_code=200)
    return {
        "method": request.method,
        "path": request.url.path,
        "headers": dict(request.headers),
        "client": request.client.host if request.client else None,
        "monitor_paths": sorted(MONITOR_PATHS),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Registration helpers
# ═══════════════════════════════════════════════════════════════════════════

def register_middleware(app: FastAPI, allowed_origins: list[str]) -> None:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.middleware("http")(logging_middleware)
    app.middleware("http")(metrics_middleware)
    app.middleware("http")(security_headers_middleware)
    app.middleware("http")(body_size_guard)
    app.middleware("http")(monitor_safety_net)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, _validation_err)
    app.add_exception_handler(StarletteHTTPException, _http_err)
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_exception_handler(Exception, _unhandled_err)
    app.state.limiter = limiter


def register_monitor_routes(app: FastAPI) -> None:
    for p in LIVENESS_PATHS:
        app.add_api_route(
            p, _liveness,
            methods=["GET", "HEAD", "OPTIONS"],
            include_in_schema=False,
            name=f"liveness_{p.strip('/').replace('/', '_') or 'root'}",
        )
    for p in READINESS_PATHS:
        app.add_api_route(
            p, _readiness,
            methods=["GET", "HEAD", "OPTIONS"],
            include_in_schema=False,
            name=f"readiness_{p.strip('/').replace('/', '_')}",
        )
    app.add_api_route("/", _root_probe,
                      methods=["GET", "HEAD", "OPTIONS"],
                      include_in_schema=False, name="root_probe")
    app.add_api_route("/api/debug/echo", _debug_echo,
                      methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"],
                      include_in_schema=False, name="debug_echo")
    app.add_api_route("/api/_monitor_probe", _monitor_probe,
                      methods=["GET", "HEAD", "OPTIONS"],
                      include_in_schema=False, name="monitor_probe")


# ═══════════════════════════════════════════════════════════════════════════
# Background tasks
# ═══════════════════════════════════════════════════════════════════════════

async def background_health_check() -> None:
    while True:
        try:
            test_prompt = "Say OK"
            for name, func in PROVIDER_CHAIN:
                if name == "local" or not PROVIDER_KEY_CHECK.get(name, False):
                    continue
                models = PROVIDER_MODELS.get(name, [])
                if not models:
                    continue
                try:
                    t0 = time.time()
                    resp = await asyncio.wait_for(
                        func(test_prompt, 5, 0.0, models[0]), timeout=3.0,
                    )
                    lat = time.time() - t0
                    if resp and len(resp.strip()) > 0:
                        record_provider_result(name, lat, success=True)
                except Exception as e:
                    record_provider_result(
                        name, 3.0, success=False,
                        is_rate_limit=("429" in str(e)),
                    )
        except Exception as e:
            logger.warning("health_check_error error=%s", e)
        await asyncio.sleep(300)


async def validate_all_providers(force: bool = False) -> dict:
    _KEY = state.PROVIDER_VALIDATION_KEY
    _TTL = state.PROVIDER_VALIDATION_TTL
    now = time.time()

    if not force:
        cached = await get_redis_cache(_KEY)
        if cached and isinstance(cached, dict):
            if now - cached.get("ts", 0.0) < _TTL:
                return cached.get("result", {})

    test_prompt = "Say OK"
    results: dict = {}
    probes = []

    for name, func in PROVIDER_CHAIN:
        if name == "local" or not PROVIDER_KEY_CHECK.get(name, False):
            results[name] = "skipped (no key or not configured)"
            continue
        models = PROVIDER_MODELS.get(name) or []
        if not models:
            results[name] = "skipped (no models)"
            continue
        probes.append((name, func, models[0]))

    async def _probe(name: str, func, model: str):
        t0 = time.time()
        try:
            resp = await asyncio.wait_for(func(test_prompt, 5, 0.0, model), timeout=5.0)
            latency = (time.time() - t0) * 1000
            if resp and resp.strip():
                return name, f"healthy ({latency:.0f}ms)"
            return name, "unhealthy (empty response)"
        except Exception as e:
            return name, f"error: {str(e)[:80]}"

    for name, status in await asyncio.gather(*(_probe(*p) for p in probes)):
        results[name] = status

    await set_redis_cache(_KEY, {"result": results, "ts": now}, ttl=_TTL)
    logger.info("provider_validation_done", count=len(results))
    return results


async def pr_defense_cleanup() -> None:
    from datetime import timedelta
    if not state.db_available or state.pr_reports_col is None:
        return
    while True:
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=30)
            await state.pr_reports_col.delete_many({"createdAt": {"$lt": cutoff}})
        except Exception as e:
            logger.warning("pr_defense_cleanup_failed error=%s", e)
        await asyncio.sleep(86400)

async def _keepalive_loop() -> None:
    """Self-ping loop. Only meaningful on Render (idle spin-down)."""
    from .config import HTTP_CLIENT
    base = os.getenv("SELF_KEEPALIVE_URL", "").strip()
    if not base:
        logger.info("keepalive_disabled reason=no_url")
        return
    base = base.rstrip("/")
    while True:
        try:
            for path in ("/", "/api/health"):
                with contextlib.suppress(Exception):
                    await HTTP_CLIENT.get(f"{base}{path}", timeout=5.0)
            await asyncio.sleep(180)
        except Exception:
            await asyncio.sleep(60)

# ═══════════════════════════════════════════════════════════════════════════
# Lifespan
# ═══════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("axelr_startup_begin")
    try:
        # -- 1. infrastructure --
        await init_redis()
        await init_qstash()
        await init_db()

        # -- 2. background tasks --
        state.start_time = time.time()
        app.state.start_time = state.start_time
        app.state.bg_tasks = set()

        def _spawn(coro, *, name: str):
            t = asyncio.create_task(coro, name=name)
            app.state.bg_tasks.add(t)
            t.add_done_callback(app.state.bg_tasks.discard)
            return t

        _spawn(validate_all_providers(), name="validate_providers")
        _spawn(background_health_check(), name="health_check")
        if ENABLE_PR_DEFENSE:
            _spawn(pr_defense_cleanup(), name="pr_cleanup")
        if os.getenv("ENABLE_SELF_KEEPALIVE", "false").lower() in ("1", "true", "render"):
            _spawn(_keepalive_loop(), name="keepalive")

        # -- 3. elite modules (guarded imports) --
        from .routes_core import route_ai_request_parallel

        try:
            from core.conversation import ConversationMemory
            state.conversation_memory = ConversationMemory(
                redis_client=state.redis_client, http_client=_http_client()
            )
        except Exception as e:
            logger.warning("conversation_memory_init_failed error=%s", e)
            state.conversation_memory = None

        try:
            from core.repo_indexer import RepoIndexer
            state.repo_indexer = RepoIndexer(
                redis_client=state.redis_client, http_client=_http_client()
            )
        except Exception as e:
            logger.warning("repo_indexer_init_failed error=%s", e)
            state.repo_indexer = None

        try:
            from core.testloop import TestLoop
            from .routes_core import _sandbox_execute
            state.test_loop = TestLoop(
                route_func=route_ai_request_parallel,
                execute_func=_sandbox_execute,
            )
        except Exception as e:
            logger.warning("test_loop_init_failed error=%s", e)
            state.test_loop = None

        # -- 4. feature services --
        try:
            from core.infra import CircuitBreaker
            state.circuit_breaker = CircuitBreaker(
                redis_client=state.redis_client,
                threshold=3,
                cooldown_s=60,
                rate_limit_cooldown_s=1800,
                namespace="axelr:cb",
            )
            app.state.circuit_breaker = state.circuit_breaker
            logger.info("circuit_breaker_initialized", redis=bool(state.redis_client))
        except Exception as e:
            logger.warning("circuit_breaker_init_failed error=%s", e)
            state.circuit_breaker = None

        try:
            from core.routing import Orchestrator
            state.orchestrator = Orchestrator(route_ai_request_parallel, max_parallel=4)
            app.state.orchestrator = state.orchestrator
            logger.info("orchestrator_initialized")
        except Exception as e:
            logger.warning("orchestrator_init_failed error=%s", e)
            state.orchestrator = None

        # -- 4b. local hot-swap inference engine --
        try:
            from core.hot_swap_engine import get_hot_swap_engine
            _engine = get_hot_swap_engine()
            await _engine.start()
            app.state.hot_swap_engine = _engine
            logger.info(
                "hot_swap_engine_started",
                binary=_engine.snapshot()["binary"],
                roster=_engine.snapshot()["roster_size"],
            )
        except Exception as e:
            logger.warning("hot_swap_engine_init_failed error=%s", e)
            app.state.hot_swap_engine = None

        # intent router / code guard / semantic cache are set at import of routes_core
        if os.getenv("ENABLE_INTENT_CLASSIFIER", "true").lower() == "true":
            state.intent_classifier = state.intent_router
        if os.getenv("ENABLE_CRITIC", "true").lower() == "true":
            state.critic_agent = state.code_guard

        # Context registry
        if (os.getenv("ENABLE_CONTEXT_REGISTRY", "true").lower() == "true"
                and state.redis_client is not None
                and state.users_col is not None):
            try:
                from core import ContextRegistry
                state.context_registry = ContextRegistry(
                    state.redis_client, state.users_col,
                )
            except Exception as e:
                logger.warning("context_registry_init_failed error=%s", e)

        # Blast radius
        if os.getenv("ENABLE_BLAST_RADIUS", "false").lower() == "true" and WORKSPACE_ROOT:
            try:
                from core import DependencyTracker
                state.dependency_tracker = DependencyTracker(WORKSPACE_ROOT)
                await asyncio.to_thread(state.dependency_tracker.build, max_files=3000)
            except Exception as e:
                logger.warning("dependency_tracker_build_failed error=%s", e)

        # Self-healer
        if os.getenv("ENABLE_SELF_HEAL", "true").lower() == "true":
            try:
                from core import SelfHealer
                state.self_healer = SelfHealer(route_ai_request_parallel, max_retries=2)
            except Exception as e:
                logger.warning("self_healer_init_failed error=%s", e)

        # PR shield
        if ENABLE_PR_DEFENSE:
            try:
                from core import PRShield
                state.pr_defense = PRShield()
            except Exception as e:
                logger.warning("pr_shield_init_failed error=%s", e)

        # Touch-fix engine
        try:
            from core.healing import TouchFixEngine
            state.touch_fix_engine = TouchFixEngine(route_func=route_ai_request_parallel)
        except Exception as e:
            logger.warning("touch_fix_engine_init_failed error=%s", e)

        # -- 5. connectivity probes --
        if state.db_available:
            try:
                await state.db.command("ping")
                logger.info("mongo_ping_ok")
            except Exception as e:
                logger.error("mongo_ping_failed error=%s", e)

        if state.redis_client:
            try:
                await state.redis_client.ping()
                logger.info("redis_ping_ok")
            except Exception as e:
                logger.error("redis_ping_failed error=%s", e)

        # -- 6. semantic cache warm-up (best-effort) --
        try:
            await asyncio.wait_for(state.semantic_cache._ensure_model(), timeout=30)
            logger.info("semantic_cache_warmed")
        except Exception as e:
            logger.warning("semantic_cache_warmup_failed error=%s", e)

        logger.info(
            "axelr_startup",
            origin=ORIGIN,
            mongo="SET" if os.getenv("MONGO_URI") else "MISSING",
        )

    except Exception as e:
        logger.error("application_startup_failed error=%s", e, exc_info=True)
        raise

    yield

    # ================= SHUTDOWN =================
    logger.info("shutdown_initiated")

    for t in list(getattr(app.state, "bg_tasks", ())):
        t.cancel()
    with contextlib.suppress(Exception):
        await asyncio.gather(*getattr(app.state, "bg_tasks", ()), return_exceptions=True)

    for attr in ("context_registry", "conversation_memory", "repo_indexer"):
        obj = getattr(state, attr, None)
        if obj is not None and hasattr(obj, "close"):
            with contextlib.suppress(Exception):
                await obj.close()

    # Hot-swap engine: kill resident model + orphan sweep.
    engine = getattr(app.state, "hot_swap_engine", None)
    if engine is not None:
        with contextlib.suppress(Exception):
            await engine.shutdown()

    with contextlib.suppress(Exception):
        await state.semantic_cache.clear()

    from .config import HTTP_CLIENT
    with contextlib.suppress(Exception):
        await HTTP_CLIENT.aclose()

    if state.client:
        with contextlib.suppress(Exception):
            state.client.close()

    logger.info("shutdown_complete")


def _http_client():
    from .config import HTTP_CLIENT
    return HTTP_CLIENT

    # ================= SHUTDOWN =================
    logger.info("shutdown_initiated")
    for t in list(getattr(app.state, "bg_tasks", ())):
        t.cancel()
    with contextlib.suppress(Exception):
        await asyncio.gather(*getattr(app.state, "bg_tasks", ()), return_exceptions=True)

    for attr in ("context_registry", "conversation_memory", "repo_indexer"):
        obj = getattr(state, attr, None)
        if obj is not None and hasattr(obj, "close"):
            with contextlib.suppress(Exception):
                await obj.close()
engine = getattr(app.state, "hot_swap_engine", None)
if engine is not None:
    with contextlib.suppress(Exception):
        await engine.shutdown()
    with contextlib.suppress(Exception):
        await state.semantic_cache.clear()

    from .config import HTTP_CLIENT
    with contextlib.suppress(Exception):
        await HTTP_CLIENT.aclose()

    if state.client:
        with contextlib.suppress(Exception):
            state.client.close()

    logger.info("shutdown_complete")


def _http_client():
    from .config import HTTP_CLIENT
    return HTTP_CLIENT


__all__ = [
    "lifespan",
    "register_middleware",
    "register_exception_handlers",
    "register_monitor_routes",
    "validate_all_providers",
    "background_health_check",
    "pr_defense_cleanup",
    "MONITOR_PATHS",
    "REQUESTS",
]