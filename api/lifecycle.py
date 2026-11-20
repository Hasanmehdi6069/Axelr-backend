# api/lifecycle.py
"""AXELR — process lifecycle: boot · background loops · shutdown."""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI

from .config import ENABLE_PR_DEFENSE, ORIGIN, WORKSPACE_ROOT, get_http_client
from .state import (
    init_db,
    init_qstash,
    init_redis,
    state,
)
from .state import (
    init_redis as init_shared_redis_cache,
)

logger = structlog.get_logger("axelr.lifecycle")

_INSTALLED: bool = False
_ATTACHED: set[int] = set()

import sys
from types import ModuleType

_boot_module = ModuleType("api._boot")
_boot_module.__all__ = ["audit", "_SSE_CONTRACT", "classify_intent"]
_boot_module.audit = _audit_snapshot
_boot_module._SSE_CONTRACT = _SSE_CONTRACT
_boot_module.classify_intent = classify_intent
sys.modules["api._boot"] = _boot_module

_SSE_CONTRACT: dict[str, Any] = {
    "version": 2,
    "endpoints": {
        "/api/extract_stream": {
            "media_type": "text/event-stream",
            "frames": [
                {"event": "route", "data": {"route": "CORE|DATA|DESIGN", "intent": "..."}},
                {"event": "thinking", "data": {"stage": "...", "status": "..."}},
                {"event": "thinking", "data": {"content": "<reasoning>", "source": "llm"}},
                {"event": "message", "data": {"text": "<chunk>"}},
                {"event": "message", "data": {"feature_metadata": {}}},
                {"event": "message", "data": {"watermark": "..."}},
                {"event": "close", "data": {}},
            ],
        },
    },
}


def classify_intent(prompt: str) -> str:
    try:
        from core.context import classify_flat
        return classify_flat(prompt)
    except Exception:
        return "conversational" if len((prompt or "").split()) <= 7 else "strategy"


# ═══════════════════════════════════════════════════════════════════════
# Boot installation
# ═══════════════════════════════════════════════════════════════════════
def _install_hedge_wrap() -> None:
    try:
        from core.elite_orchestrator import hedge_wrap

        from . import ai_router as rc
    except Exception as e:
        logger.info("hedge_wrap_deferred reason=%s", e)
        return
    current = getattr(rc, "route_ai_request_parallel", None)
    if current is None or getattr(current, "_axelr_hedged", False):
        return
    with contextlib.suppress(Exception):
        wrapped = hedge_wrap(current)
        wrapped._axelr_hedged = True
        rc.route_ai_request_parallel = wrapped
        for mod_name in ("routes_tools", "ai_router"):
            with contextlib.suppress(Exception):
                mod = __import__(f"{__package__}.{mod_name}", fromlist=[mod_name])
                if hasattr(mod, "route_ai_request_parallel"):
                    mod.route_ai_request_parallel = wrapped
        logger.info("hedge_wrap_installed")


def _install_cognition() -> None:
    try:
        from core.resilience import AXELR_COGNITION_LEVEL, get_pipeline
        get_pipeline()
        logger.info("cognition_pipeline_installed level=%d", AXELR_COGNITION_LEVEL)
    except Exception as e:
        logger.warning("cognition_pipeline_install_failed error=%s", e)


def install_boot() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _install_hedge_wrap()
    _install_cognition()
    _INSTALLED = True
    logger.info("axelr_boot_install_complete")


def attach_boot(app: FastAPI) -> None:
    if id(app) in _ATTACHED:
        return
    _ATTACHED.add(id(app))

    with contextlib.suppress(Exception):
        if not any(getattr(r, "path", None) == "/api/contract" for r in app.routes):
            @app.get("/api/contract", include_in_schema=False)
            async def _contract():
                return _SSE_CONTRACT

    with contextlib.suppress(Exception):
        if not any(getattr(r, "path", None) == "/api/audit" for r in app.routes):
            @app.get("/api/audit", include_in_schema=False)
            async def _audit_endpoint():
                return _audit_snapshot()


def _audit_snapshot() -> dict[str, Any]:
    out: dict[str, Any] = {"boot_installed": _INSTALLED, "version": "1.0.0", "cognition_pipeline": {}}
    with contextlib.suppress(Exception):
        from core.resilience import audit as _res_audit
        out["resilience"] = _res_audit()
    return out


# ═══════════════════════════════════════════════════════════════════════
# Background loops
# ═══════════════════════════════════════════════════════════════════════
async def background_health_check() -> None:
    from .providers import (
        ACTIVE_PROVIDER_ENTRIES,
        PROVIDER_KEY_CHECK,
        PROVIDER_MODELS,
        record_provider_result,
    )
    while True:
        try:
            for name, func, _ in ACTIVE_PROVIDER_ENTRIES:
                if name == "local" or not PROVIDER_KEY_CHECK.get(name, False):
                    continue
                models = PROVIDER_MODELS.get(name, [])
                if not models:
                    continue
                try:
                    t0 = time.time()
                    resp = await asyncio.wait_for(
                        func("Say OK", 5, 0.0, models[0]), timeout=3.0,
                    )
                    if resp and resp.strip():
                        await record_provider_result(name, time.time() - t0, success=True)
                except Exception as e:
                    await record_provider_result(
                        name, 3.0, success=False, is_rate_limit=("429" in str(e)))
        except Exception as e:
            logger.warning("health_check_error error=%s", e)
        await asyncio.sleep(300)


async def validate_all_providers(force: bool = False) -> dict:
    _KEY = state.PROVIDER_VALIDATION_KEY
    _TTL = state.PROVIDER_VALIDATION_TTL
    now = time.time()

    if not force:
        with contextlib.suppress(Exception):
            from .state import get_redis_cache
            cached = await get_redis_cache(_KEY)
            if cached and isinstance(cached, dict) and now - cached.get("ts", 0) < _TTL:
                return cached.get("result", {})

    from .providers import ACTIVE_PROVIDER_ENTRIES, PROVIDER_KEY_CHECK, PROVIDER_MODELS
    from .state import set_redis_cache

    results: dict = {}
    probes = []
    for name, func, _ in ACTIVE_PROVIDER_ENTRIES:
        if name == "local" or not PROVIDER_KEY_CHECK.get(name, False):
            results[name] = "skipped (no key)"
            continue
        models = PROVIDER_MODELS.get(name) or []
        if not models:
            results[name] = "skipped (no models)"
            continue
        probes.append((name, func, models[0]))

    async def _probe(name, func, model):
        t0 = time.time()
        try:
            resp = await asyncio.wait_for(func("Say OK", 5, 0.0, model), timeout=5.0)
            return name, (f"healthy ({(time.time() - t0) * 1000:.0f}ms)"
                          if resp and resp.strip() else "unhealthy (empty)")
        except Exception as e:
            return name, f"error: {str(e)[:80]}"

    for name, status in await asyncio.gather(*(_probe(*p) for p in probes)):
        results[name] = status

    with contextlib.suppress(Exception):
        await set_redis_cache(_KEY, {"result": results, "ts": now}, ttl=_TTL)
    logger.info("provider_validation_done count=%d", len(results))
    return results


async def pr_defense_cleanup() -> None:
    from datetime import datetime, timedelta, timezone
    if not state.db_available or state.pr_reports_col is None:
        return
    while True:
        with contextlib.suppress(Exception):
            cutoff = datetime.now(timezone.utc) - timedelta(days=30)
            await state.pr_reports_col.delete_many({"createdAt": {"$lt": cutoff}})
        await asyncio.sleep(86400)


async def _keepalive_loop() -> None:
    base = (os.getenv("SELF_KEEPALIVE_URL") or "").strip().rstrip("/")
    if not base:
        return
    while True:
        with contextlib.suppress(Exception):
            client = get_http_client()
            for path in ("/", "/api/health"):
                with contextlib.suppress(Exception):
                    await client.get(f"{base}{path}", timeout=5.0)
            await asyncio.sleep(180)
        await asyncio.sleep(60)


# ═══════════════════════════════════════════════════════════════════════
# Founder-tier seeding
# ═══════════════════════════════════════════════════════════════════════
async def _seed_founder_tiers() -> None:
    if not state.db_available or state.users_col is None:
        return
    from datetime import datetime, timezone

    from .config import ADMIN_EMAIL
    if not ADMIN_EMAIL:
        return
    doc = await state.users_col.find_one({"email": ADMIN_EMAIL})
    if doc and not doc.get("manually_provisioned"):
        await state.users_col.update_one(
            {"_id": doc["_id"]},
            {"$set": {
                "tier": "free", "subTier": "founder_full",
                "manually_provisioned": True,
                "provisioned_by": "system",
                "provisioned_at": datetime.now(timezone.utc).isoformat(),
                "provision_reason": "Founder/Building permanent access",
                "is_admin": True,
            }},
        )
        logger.info("founder_tier_seeded email=%s", ADMIN_EMAIL)


# ═══════════════════════════════════════════════════════════════════════
# Lazy elite initialization
# ═══════════════════════════════════════════════════════════════════════
_lazy_initialization_complete = False


async def _initialize_elite_services() -> None:
    global _lazy_initialization_complete
    try:
        logger.info("starting_lazy_initialization_of_elite_services")
        from .ai_router import _sandbox_execute, route_ai_request_parallel

        with contextlib.suppress(Exception):
            from core.thermal_guard import start as _thermal_start
            if _thermal_start():
                logger.info("thermal_guard_started")

        try:
            from core.code_intel import TestLoop
            from core.memory import Memory
            from core.repo_indexer import RepoIndexer
            state.conversation_memory = Memory(
                redis_client=state.redis_client, http_client=get_http_client())
            state.repo_indexer = RepoIndexer(
                redis_client=state.redis_client, http_client=get_http_client())
            state.test_loop = TestLoop(
                route_func=route_ai_request_parallel, execute_func=_sandbox_execute)
        except Exception as e:
            logger.warning("elite_memory_init_failed error=%s", e)

        try:
            from core.routing import Orchestrator
            state.orchestrator = Orchestrator(route_ai_request_parallel, max_parallel=4)
        except Exception as e:
            logger.warning("orchestrator_init_failed error=%s", e)

        try:
            from core.twin_engine import get_twin_engine
            _engine = await get_twin_engine()
            if _engine is not None:
                await _engine.start()
        except Exception as e:
            logger.info("twin_engine_unavailable reason=%s", e)

        with contextlib.suppress(Exception):
            from core.heartbeat import install_bridge as _hb_install
            logger.info("heartbeat_bridge_installed=%s", _hb_install())

        if (os.getenv("ENABLE_CONTEXT_REGISTRY", "true").lower() == "true"
                and state.redis_client is not None and state.users_col is not None):
            with contextlib.suppress(Exception):
                from core.infra import ContextRegistry
                state.context_registry = ContextRegistry(state.redis_client, state.users_col)

        if (os.getenv("ENABLE_BLAST_RADIUS", "false").lower() == "true"
                and WORKSPACE_ROOT):
            with contextlib.suppress(Exception):
                from core.code_intel import DependencyTracker
                state.dependency_tracker = DependencyTracker(WORKSPACE_ROOT)
                await asyncio.to_thread(state.dependency_tracker.build, max_files=3000)

        if os.getenv("ENABLE_SELF_HEAL", "true").lower() == "true":
            with contextlib.suppress(Exception):
                from core.code_intel import SelfHealer
                state.self_healer = SelfHealer(route_ai_request_parallel, max_retries=2)

        with contextlib.suppress(Exception):
            from core.infra import CircuitBreaker
            state.circuit_breaker = CircuitBreaker(
                redis_client=state.redis_client,
                threshold=3, cooldown_s=60, rate_limit_cooldown_s=1800,
                namespace="axelr:cb")

        if os.getenv("ENABLE_INTENT_CLASSIFIER", "true").lower() == "true":
            state.intent_classifier = state.intent_router
        if os.getenv("ENABLE_CRITIC", "true").lower() == "true":
            state.critic_agent = state.code_guard

        with contextlib.suppress(Exception):
            from core.code_intel import TouchFixEngine
            state.touch_fix_engine = TouchFixEngine(route_func=route_ai_request_parallel)

        with contextlib.suppress(Exception):
            from core.memory import Memory
            from core.resilience import ResilientPipeline
            from core.twin_engine import get_twin_engine
            state.memory_bus = Memory(
                redis_client=state.redis_client, http_client=get_http_client())
            _twin = await get_twin_engine()
            if _twin is not None:
                await _twin.start()
                state.twin_engine = _twin
            state.provider_cascade = ResilientPipeline()

        try:
            from .config import SUPABASE_BUCKET, SUPABASE_KEY, SUPABASE_URL
            if SUPABASE_URL and SUPABASE_KEY:
                from supabase import create_async_client
                state.supabase = await create_async_client(SUPABASE_URL, SUPABASE_KEY)
                state.supabase_bucket = SUPABASE_BUCKET
            else:
                state.supabase = None
                state.supabase_bucket = None
        except Exception as e:
            state.supabase = None
            state.supabase_bucket = None
            logger.info("supabase_audit_tier_skipped error=%s", e)

        with contextlib.suppress(Exception):
            await asyncio.wait_for(state.semantic_cache._ensure_model(), timeout=30)

        with contextlib.suppress(Exception):
            from .routes_admin import _seed_founder_tiers
            await _seed_founder_tiers()

        _lazy_initialization_complete = True
        logger.info("axelr_full_startup_complete origin=%s mongo=%s",
                    ORIGIN, "SET" if os.getenv("MONGO_URI") else "MISSING")
    except Exception:
        logger.error("lazy_initialization_failed", exc_info=True)


# ═══════════════════════════════════════════════════════════════════════
# Lifespan — ONE canonical definition
# ═══════════════════════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("axelr_startup_begin")
    install_boot()

    memory_tuner_available = False
    try:
        from core.memory_tuner import arm as memory_tuner_arm
        from core.memory_tuner import disarm as memory_tuner_disarm
        memory_tuner_arm()
        memory_tuner_available = True
    except Exception as e:
        logger.warning("memory_tuner_unavailable error=%s", e)
    app.state.bg_tasks = set()

    def _spawn(coro, *, name: str):
        t = asyncio.create_task(coro, name=name)
        app.state.bg_tasks.add(t)
        t.add_done_callback(app.state.bg_tasks.discard)
        return t

    try:
        await init_redis()
        await init_shared_redis_cache()
        await init_qstash()
        await init_db()
        state.start_time = time.time()
        app.state.start_time = state.start_time

        # Seed founder tiers
        await _seed_founder_tiers()

        _spawn(validate_all_providers(), name="validate_providers")
        _spawn(background_health_check(), name="health_check")
        if ENABLE_PR_DEFENSE:
            _spawn(pr_defense_cleanup(), name="pr_cleanup")
        if os.getenv("ENABLE_SELF_KEEPALIVE", "false").lower() in ("1", "true", "render"):
            _spawn(_keepalive_loop(), name="keepalive")
        _spawn(_initialize_elite_services(), name="initialize_elite_services")

        logger.info("core_infrastructure_online_degraded_until_elite_ready")
        yield
    except Exception as e:
        logger.error("application_startup_failed error=%s", e, exc_info=True)
        raise
    finally:
        logger.info("axelr_shutdown_begin")
        tasks = list(app.state.bg_tasks)
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        with contextlib.suppress(Exception):
            from core.thermal_guard import stop as _thermal_stop
            _thermal_stop()
        with contextlib.suppress(Exception):
            from core.burst_scheduler import shutdown as _burst_stop
            _burst_stop()

        import core.twin_engine as _twin_mod
        _eng = getattr(_twin_mod, "_ENGINE", None)
        if _eng is not None:
            with contextlib.suppress(Exception):
                await _eng.shutdown()

        if getattr(state, "supabase", None) is not None:
            with contextlib.suppress(Exception):
                await state.supabase.aclose()
        state.supabase = None
        state.supabase_bucket = None

        if state.client is not None:
            with contextlib.suppress(Exception):
                await state.client.close()
        if state.redis_client is not None:
            with contextlib.suppress(Exception):
                await state.redis_client.aclose()
        with contextlib.suppress(Exception):
            await get_http_client().aclose()

        for name in (
            "conversation_memory", "repo_indexer", "test_loop", "circuit_breaker",
            "orchestrator", "critic_agent", "context_registry", "dependency_tracker",
            "self_healer", "pr_defense", "touch_fix_engine", "provider_cascade",
            "twin_engine", "memory_bus",
        ):
            setattr(state, name, None)

        if memory_tuner_available:
            with contextlib.suppress(Exception):
                memory_tuner_disarm()
        logger.info("axelr_shutdown_complete")


__all__ = [
    "attach_boot", "background_health_check", "classify_intent",
    "install_boot", "lifespan", "pr_defense_cleanup",
    "validate_all_providers",
]