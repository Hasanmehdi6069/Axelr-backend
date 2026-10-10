# api/middleware.py
"""
AXELR API — HTTP middleware, exception handlers, monitor endpoints.
===================================================================
Single home for:
  * CORS + logging + metrics + security headers + body-size + monitor safety
  * All exception handlers (validation / HTTP / unhandled)
  * Monitor endpoints (liveness, readiness, root, echo, probe) — one route per path
  * PII sanitizer vault (Stage 3.2) + stream-recovery buffer (Stage 3.3)

Runtime singletons AND the lifespan live in ``api.lifespan`` — that is the
ONLY place they are constructed. This module re-exports the four background
task coroutines for callers that still ``from .middleware import ...``.
"""
from __future__ import annotations

import asyncio
import os
import re as _re_metric
import time
import uuid
from datetime import datetime, timezone

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import REGISTRY, Counter
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response as _StarletteResponse

from .state import (
    RateLimitExceeded,
    _rate_limit_exceeded_handler,
    limiter,
    state,
)
from .lifespan import (  # noqa: E402,F401 — backward-compat re-exports
    background_health_check,
    pr_defense_cleanup,
    validate_all_providers,
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
# PII vault (Stage 3.2) + Stream-Heal buffer (Stage 3.3)
# ═══════════════════════════════════════════════════════════════════════════
_pii_sanitizer_vault: dict[str, dict[str, str]] = {}
_stream_recovery_buffer: dict[str, str] = {}
PROVIDER_TIMEOUT_WINDOW_MS = 800
HARD_SERVER_TIMEOUT_S = 15  # Stage 4.1


def get_sanitizer_vault() -> dict[str, dict[str, str]]:
    return _pii_sanitizer_vault


def get_stream_recovery_buffer() -> dict[str, str]:
    return _stream_recovery_buffer


def mask_pii(
    session_id: str, tenant_id: str, text: str, sensitive_values: list[str],
) -> str:
    """Mask PII/keys in prompts; store mapping in ephemeral vault only."""
    session_key = f"{session_id}:{tenant_id}"
    vault = _pii_sanitizer_vault.setdefault(session_key, {})
    masked = text
    for i, val in enumerate(sensitive_values):
        if val and val in masked:
            masked_id = f"[REDACTED_{i}]"
            vault[masked_id] = val
            masked = masked.replace(val, masked_id)
    return masked


def restore_pii(session_id: str, tenant_id: str, text: str) -> str:
    """Restore PII/keys from ephemeral vault; drop vault after use."""
    session_key = f"{session_id}:{tenant_id}"
    vault = _pii_sanitizer_vault.pop(session_key, None)
    if not vault:
        return text
    restored = text
    for masked_id, original in vault.items():
        if masked_id in restored:
            restored = restored.replace(masked_id, original)
    return restored


# ═══════════════════════════════════════════════════════════════════════════
# Prometheus (idempotent)
# ═══════════════════════════════════════════════════════════════════════════
def _metric(name: str, factory):
    try:
        return factory()
    except ValueError:
        collectors = getattr(REGISTRY, "_names_to_collectors", {})
        existing = collectors.get(name)
        if existing is None:
            raise
        return existing


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
            "success": False, "code": "VALIDATION_ERROR",
            "message": "Request body failed validation.",
            "errors": exc.errors(),
        },
    )


async def _http_err(request: Request, exc: StarletteHTTPException):
    """Soften 404/405 on monitor paths → 200. Never mask real routing bugs."""
    path = request.url.path
    norm = path.rstrip("/") or "/"
    is_monitor = path in MONITOR_PATHS or norm in MONITOR_PATHS
    if is_monitor and exc.status_code in (404, 405):
        logger.info("monitor_exception_softened", path=path,
                    method=request.method, status=exc.status_code)
        if request.method in ("HEAD", "OPTIONS"):
            return Response(status_code=200)
        return JSONResponse(
            status_code=200,
            content={"status": "ok", "path": path, "method": request.method,
                     "note": "method_not_native_but_endpoint_alive"},
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "code": f"HTTP_{exc.status_code}",
                 "message": exc.detail},
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
        content={"success": False, "code": "INTERNAL_ERROR",
                 "message": "Internal server error.", "request_id": rid},
    )


# ═══════════════════════════════════════════════════════════════════════════
# HTTP middleware
# ═══════════════════════════════════════════════════════════════════════════
async def timeout_middleware(request: Request, call_next):
    """Enforce the 15 s hard timeout on all server channels (Stage 4.1)."""
    from fastapi import HTTPException
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())[:8]
    request.state.request_id = request_id
    start = time.time()
    try:
        response = await asyncio.wait_for(
            call_next(request), timeout=HARD_SERVER_TIMEOUT_S,
        )
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "http_request", request_id=request_id,
            method=request.method, path=request.url.path,
            status=response.status_code,
            duration_ms=round((time.time() - start) * 1000, 2),
        )
        return response
    except asyncio.TimeoutError:
        captured = _stream_recovery_buffer.pop(request_id, "")
        logger.error(
            "server_timeout_breach", request_id=request_id,
            path=request.url.path,
            duration_ms=round((time.time() - start) * 1000, 2),
            captured_tokens_len=len(captured),
        )
        sid = request.headers.get("x-session-id")
        tid = request.headers.get("x-tenant-id")
        if sid and tid:
            _pii_sanitizer_vault.pop(f"{sid}:{tid}", None)
        raise HTTPException(
            status_code=504,
            detail=f"Request exceeded {HARD_SERVER_TIMEOUT_S}s hard timeout limit",
        )


async def logging_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())[:8]
    request.state.request_id = request_id
    start = time.time()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "http_request", request_id=request_id,
        method=request.method, path=request.url.path,
        status=response.status_code,
        duration_ms=round((time.time() - start) * 1000, 2),
    )
    return response


_OBJECTID_RE = _re_metric.compile(r"^[0-9a-fA-F]{24}$")
_NUMERIC_RE = _re_metric.compile(r"^\d+$")


def _metric_path(path: str) -> str:
    parts = path.split("/")
    for i, seg in enumerate(parts):
        if not seg:
            continue
        if (_OBJECTID_RE.match(seg) or _NUMERIC_RE.match(seg)
                or (len(seg) == 36 and seg.count("-") == 4)):
            parts[i] = "{id}"
    return "/".join(parts) or "/"


async def metrics_middleware(request: Request, call_next):
    response = await call_next(request)
    REQUESTS.labels(
        method=request.method,
        endpoint=_metric_path(request.url.path),
        status=response.status_code,
    ).inc()
    return response


async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    if request.url.path in ("/", "/terms", "/privacy"):
        csp = (
            "default-src 'self'; "
            "script-src 'self' https://accounts.google.com https://cdn.jsdelivr.net "
            "https://cdnjs.cloudflare.com 'unsafe-inline' https://js.stripe.com; "
            "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "img-src 'self' data: blob:; "
            "connect-src 'self' https://axelr-backend.onrender.com "
            "https://api.puter.com https://api.stripe.com; "
            "frame-src 'self' https://accounts.google.com https://hooks.stripe.com; "
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
                        content={"success": False, "code": "PAYLOAD_TOO_LARGE",
                                 "message": f"Body exceeds {MAX_BODY_BYTES} bytes"},
                    )
            except ValueError:
                pass
    return await call_next(request)


async def monitor_safety_net(request: Request, call_next):
    """Belt-and-suspenders guard for monitor traffic."""
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
        headers={"Cache-Control": "no-store, no-cache, must-revalidate",
                 "X-Robots-Tag": "noindex"},
    )


async def _liveness(request: Request):
    if request.method in ("HEAD", "OPTIONS"):
        return _no_body_ok()
    return _StarletteResponse(
        status_code=200, content=b"ok",
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate",
                 "X-Robots-Tag": "noindex"},
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
            "status": overall, "service": "axelr-backend", "version": "24.4",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "uptime_seconds": round(time.time() - start_time, 2),
            "checks": {"database": db_status, "redis": redis_status},
        },
        headers={"Cache-Control": "no-store, no-cache, must-revalidate",
                 "X-Robots-Tag": "noindex"},
    )


async def _root_probe(request: Request):
    if request.method in ("HEAD", "OPTIONS"):
        return _no_body_ok()
    start_time = getattr(state, "start_time", None) or time.time()
    return JSONResponse(
        status_code=200,
        content={"service": "axelr-backend", "status": "ok", "version": "24.4",
                 "uptime_seconds": round(time.time() - start_time, 2)},
        headers={"Cache-Control": "no-store"},
    )


async def _debug_echo(request: Request):
    if request.method == "HEAD":
        return _StarletteResponse(status_code=200)
    return {
        "method": request.method, "path": request.url.path,
        "query": str(request.query_params), "headers": dict(request.headers),
        "client": request.client.host if request.client else None,
    }


async def _monitor_probe(request: Request):
    if request.method == "HEAD":
        return Response(status_code=200)
    return {
        "method": request.method, "path": request.url.path,
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
    app.middleware("http")(timeout_middleware)
    app.middleware("http")(logging_middleware)
    app.middleware("http")(metrics_middleware)
    app.middleware("http")(security_headers_middleware)
    app.middleware("http")(body_size_guard)
    app.middleware("http")(monitor_safety_net)


def register_exception_handlers(app: FastAPI) -> None:
    try:
        from core.anti_hallucination import AxelrError as AheAxelrError

        async def _ahe_axelr_err(request: Request, exc: AheAxelrError):
            doc_id = None
            try:
                body = await request.json()
                doc_id = body.get("document_id")
            except Exception:
                pass
            return JSONResponse(
                status_code=getattr(exc, "http_status", 500),
                content=exc.to_payload(doc_id),
            )

        app.add_exception_handler(AheAxelrError, _ahe_axelr_err)
    except Exception:
        pass
    app.add_exception_handler(RequestValidationError, _validation_err)
    app.add_exception_handler(StarletteHTTPException, _http_err)
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_exception_handler(Exception, _unhandled_err)
    app.state.limiter = limiter


def register_monitor_routes(app: FastAPI) -> None:
    for p in LIVENESS_PATHS:
        app.add_api_route(
            p, _liveness, methods=["GET", "HEAD", "OPTIONS"],
            include_in_schema=False,
            name=f"liveness_{p.strip('/').replace('/', '_') or 'root'}",
        )
    for p in READINESS_PATHS:
        app.add_api_route(
            p, _readiness, methods=["GET", "HEAD", "OPTIONS"],
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


__all__ = [
    "MONITOR_PATHS", "LIVENESS_PATHS", "READINESS_PATHS",
    "MAX_BODY_BYTES", "HARD_SERVER_TIMEOUT_S", "PROVIDER_TIMEOUT_WINDOW_MS",
    "get_sanitizer_vault", "get_stream_recovery_buffer",
    "mask_pii", "restore_pii",
    "register_middleware", "register_exception_handlers", "register_monitor_routes",
    "background_health_check", "validate_all_providers", "pr_defense_cleanup",
]