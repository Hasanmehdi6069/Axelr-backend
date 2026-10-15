# api/lifespan.py
"""
AXELR — Application Lifespan & Background Tasks
================================================
Canonical lifespan. Owns boot sequencing AND all long-running background
tasks (provider validation, health check, PR cleanup, keepalive), plus
post-readiness initialization of heavy elite services.

Split from `middleware.py` so HTTP middleware stays HTTP-only.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from core.infra import init_shared_redis_cache

from .config import ENABLE_PR_DEFENSE, ORIGIN, WORKSPACE_ROOT, get_http_client
from .state import init_db, init_qstash, init_redis, state

logger = structlog.get_logger("axelr")


def _http_client():
    return get_http_client()


# ═══════════════════════════════════════════════════════════════════════════
# Background tasks
# ═══════════════════════════════════════════════════════════════════════════
async def background_health_check() -> None:
    """Periodic liveness probe against every active provider."""
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
                        await record_provider_result(
                            name, time.time() - t0, success=True)
                except Exception as e:
                    await record_provider_result(
                        name, 3.0, success=False,
                        is_rate_limit=("429" in str(e)))
        except Exception as e:
            logger.warning("health_check_error error=%s", e)
        await asyncio.sleep(300)


async def validate_all_providers(force: bool = False) -> dict:
    """Redis-cached validation sweep. Runs once at boot."""
    _KEY = state.PROVIDER_VALIDATION_KEY
    _TTL = state.PROVIDER_VALIDATION_TTL
    now = time.time()

    if not force:
        try:
            from .state import get_redis_cache
            cached = await get_redis_cache(_KEY)
            if cached and isinstance(cached, dict):
                if now - cached.get("ts", 0.0) < _TTL:
                    return cached.get("result", {})
        except Exception:
            pass

    from .providers import (
        ACTIVE_PROVIDER_ENTRIES,
        PROVIDER_KEY_CHECK,
        PROVIDER_MODELS,
    )
    from .state import set_redis_cache

    results: dict = {}
    probes = []
    for name, func, _ in ACTIVE_PROVIDER_ENTRIES:
        if name == "local" or not PROVIDER_KEY_CHECK.get(name, False):
            results[name] = "skipped (no key or not configured)"
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
            if resp and resp.strip():
                return name, f"healthy ({(time.time() - t0) * 1000:.0f}ms)"
            return name, "unhealthy (empty response)"
        except Exception as e:
            return name, f"error: {str(e)[:80]}"

    for name, status in await asyncio.gather(*(_probe(*p) for p in probes)):
        results[name] = status

    try:
        await set_redis_cache(_KEY, {"result": results, "ts": now}, ttl=_TTL)
    except Exception:
        pass
    logger.info("provider_validation_done count=%d", len(results))
    return results


async def pr_defense_cleanup() -> None:
    """Delete PR reports older than 30 days, once per day."""
    from datetime import datetime, timedelta, timezone
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
    """Self-ping loop (Render idle spin-down mitigation)."""
    base = (os.getenv("SELF_KEEPALIVE_URL") or "").strip().rstrip("/")
    if not base:
        logger.info("keepalive_disabled reason=no_url")
        return
    while True:
        try:
            client = get_http_client()
            for path in ("/", "/api/health"):
                with contextlib.suppress(Exception):
                    await client.get(f"{base}{path}", timeout=5.0)
            await asyncio.sleep(180)
        except Exception:
            await asyncio.sleep(60)


# ═══════════════════════════════════════════════════════════════════════════
# Heavy background initialization (post-readiness)
# ═══════════════════════════════════════════════════════════════════════════
_lazy_initialization_complete = False


async def _initialize_elite_services() -> None:
    """Construct heavy services in background so API answers instantly."""
    global _lazy_initialization_complete
    try:
        logger.info("starting_lazy_initialization_of_elite_services")

        from .ai_router import _sandbox_execute, route_ai_request_parallel

        try:
            from core.thermal_guard import start as _thermal_start
            if _thermal_start():
                logger.info("thermal_guard_started")
        except Exception as e:
            logger.info("thermal_guard_unavailable reason=%s", e)
        # ── Memory (conversation) ─────────────────────────────────────
        try:
            from core.memory import Memory
            state.conversation_memory = Memory(
                redis_client=state.redis_client, http_client=_http_client())
            logger.info("conversation_memory_initialized")
        except Exception as e:
            logger.warning("conversation_memory_init_failed error=%s", e)
            state.conversation_memory = None

        # ── Repo indexer ──────────────────────────────────────────────
        try:
            from core.memory import RepoIndexer
            state.repo_indexer = RepoIndexer(
                redis_client=state.redis_client, http_client=_http_client())
            logger.info("repo_indexer_initialized")
        except Exception as e:
            logger.warning("repo_indexer_init_failed error=%s", e)
            state.repo_indexer = None

        # ── Test loop ─────────────────────────────────────────────────
        try:
            from core.code_intel import TestLoop
            state.test_loop = TestLoop(
                route_func=route_ai_request_parallel,
                execute_func=_sandbox_execute,
            )
            logger.info("test_loop_initialized")
        except Exception as e:
            logger.warning("test_loop_init_failed error=%s", e)
            state.test_loop = None

        # ── Orchestrator ──────────────────────────────────────────────
        try:
            from core.routing import Orchestrator
            state.orchestrator = Orchestrator(
                route_ai_request_parallel, max_parallel=4)
            logger.info("orchestrator_initialized")
        except Exception as e:
            logger.warning("orchestrator_init_failed error=%s", e)
            state.orchestrator = None

        # ── Local inference engine ────────────────────────────────────
        try:
            from core.twin_engine import get_twin_engine
            _engine = await get_twin_engine()
            if _engine is not None:
                await _engine.start()
                snap = _engine.snapshot()
                logger.info("twin_engine_started binary=%s roster=%s mode=%s",
                            snap.get("binary"), snap.get("roster_size"),
                            snap.get("mode"))
            else:
                logger.info("twin_engine_unavailable remote_only_mode")
        except Exception as e:
            logger.info("twin_engine_unavailable reason=%s", e)
                    # ── Live Heartbeat Index bridge ───────────────────────────────
        try:
            from core.heartbeat import install_bridge as _hb_install
            ok = _hb_install()
            logger.info("heartbeat_bridge_installed=%s", ok)
        except Exception as e:
            logger.info("heartbeat_bridge_unavailable reason=%s", e)
        # ── Context registry ──────────────────────────────────────────
        if (os.getenv("ENABLE_CONTEXT_REGISTRY", "true").lower() == "true"
                and state.redis_client is not None
                and state.users_col is not None):
            try:
                from core.infra import ContextRegistry
                state.context_registry = ContextRegistry(
                    state.redis_client, state.users_col)
                logger.info("context_registry_initialized")
            except Exception as e:
                logger.warning("context_registry_init_failed error=%s", e)

        # ── Dependency tracker (opt-in, threaded) ─────────────────────
        if (os.getenv("ENABLE_BLAST_RADIUS", "false").lower() == "true"
                and WORKSPACE_ROOT):
            try:
                from core.code_intel import DependencyTracker
                state.dependency_tracker = DependencyTracker(WORKSPACE_ROOT)
                await asyncio.to_thread(
                    state.dependency_tracker.build, max_files=3000)
                logger.info("dependency_tracker_initialized")
            except Exception as e:
                logger.warning("dependency_tracker_build_failed error=%s", e)

        # ── Self-healer ───────────────────────────────────────────────
        if os.getenv("ENABLE_SELF_HEAL", "true").lower() == "true":
            try:
                from core.code_intel import SelfHealer
                state.self_healer = SelfHealer(
                    route_ai_request_parallel, max_retries=2)
                logger.info("self_healer_initialized")
            except Exception as e:
                logger.warning("self_healer_init_failed error=%s", e)

        # ── Circuit breaker ───────────────────────────────────────────
        try:
            from core.infra import CircuitBreaker
            state.circuit_breaker = CircuitBreaker(
                redis_client=state.redis_client,
                threshold=3, cooldown_s=60, rate_limit_cooldown_s=1800,
                namespace="axelr:cb",
            )
            logger.info("circuit_breaker_initialized redis=%s",
                        bool(state.redis_client))
        except Exception as e:
            logger.warning("circuit_breaker_init_failed error=%s", e)
            state.circuit_breaker = None

        # ── Adjudicator (optional) ────────────────────────────────────
        try:
            from core.routing import get_orchestrator
            _adj = await get_orchestrator()
            logger.info("adjudicator_ready")
        except Exception as e:
            logger.warning("adjudicator_init_failed error=%s", e)

        # ── Intent classifier + critic ────────────────────────────────
        if os.getenv("ENABLE_INTENT_CLASSIFIER", "true").lower() == "true":
            state.intent_classifier = state.intent_router
            if state.intent_classifier is None:
                logger.warning("intent_classifier_unavailable_degraded")
        if os.getenv("ENABLE_CRITIC", "true").lower() == "true":
            state.critic_agent = state.code_guard

        # ── Touch-fix engine ──────────────────────────────────────────
        try:
            from core.code_intel import TouchFixEngine
            state.touch_fix_engine = TouchFixEngine(
                route_func=route_ai_request_parallel)
            logger.info("touch_fix_engine_initialized")
        except Exception as e:
            logger.warning("touch_fix_engine_init_failed error=%s", e)
            state.touch_fix_engine = None

        # ── Cascade: memory + twin + resilient pipeline ───────────────
        try:
            from core.memory import Memory
            from core.resilience import ResilientPipeline
            from core.twin_engine import get_twin_engine

            state.memory_bus = Memory(
                redis_client=state.redis_client, http_client=_http_client())
            _twin = await get_twin_engine()
            if _twin is not None:
                await _twin.start()
                state.twin_engine = _twin
            state.provider_cascade = ResilientPipeline()
            _mode = (_twin.snapshot() or {}).get("mode") if _twin else "unavailable"
            logger.info("cascade_initialised pipeline=resilient_pipeline "
                        "memory=core.memory twin_mode=%s", _mode)
        except Exception as e:
            logger.warning("cascade_init_failed error=%s", e)
            state.twin_engine = None
            state.provider_cascade = None
            state.memory_bus = None

        # ── Supabase compliance-audit tier (Stage 3.4) ──────────────────
        try:
            from .config import SUPABASE_BUCKET, SUPABASE_KEY, SUPABASE_URL
            if SUPABASE_URL and SUPABASE_KEY:
                from supabase import create_async_client
                state.supabase = await create_async_client(
                    SUPABASE_URL, SUPABASE_KEY)
                state.supabase_bucket = SUPABASE_BUCKET
                logger.info("supabase_audit_tier_initialized url=%s", SUPABASE_URL)
            else:
                state.supabase = None
                state.supabase_bucket = None
                logger.info("supabase_audit_tier_skipped reason=no_credentials")
        except Exception as e:
            state.supabase = None
            state.supabase_bucket = None
            logger.warning("supabase_audit_tier_failed error=%s", e)

        # ── Connectivity probes ───────────────────────────────────────
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

        # ── Semantic cache warm-up ────────────────────────────────────
        try:
            await asyncio.wait_for(
                state.semantic_cache._ensure_model(), timeout=30)
            logger.info("semantic_cache_warmed")
        except Exception as e:
            logger.warning("semantic_cache_warmup_failed error=%s", e)

        logger.info("axelr_full_startup_complete origin=%s mongo=%s",
                    ORIGIN, "SET" if os.getenv("MONGO_URI") else "MISSING")
        _lazy_initialization_complete = True
        logger.info("all_lazy_elite_services_initialization_complete")
    except Exception:
        logger.error("lazy_initialization_failed", exc_info=True)
        # NOTE: Supabase cleanup is owned by lifespan()::finally — never here.


# ═══════════════════════════════════════════════════════════════════════════
# Lifespan
# ═══════════════════════════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("axelr_startup_begin")

    # ── Memory tuner: disable cyclic GC + load libc handle ─────────────
    from core.memory_tuner import arm as memory_tuner_arm
    from core.memory_tuner import disarm as memory_tuner_disarm
    memory_tuner_arm()

    app.state.bg_tasks = set()

    try:
        await init_redis()
        await init_shared_redis_cache()
        await init_qstash()
        await init_db()

        state.start_time = time.time()
        app.state.start_time = state.start_time

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
        try:
            from core.thermal_guard import stop as _thermal_stop
            _thermal_stop()
        except Exception:
            pass
        try:
            from core.burst_scheduler import shutdown as _burst_stop
            _burst_stop()
        except Exception:
            pass
        try:
            from core.prewarm import cancel as _pw_cancel
            from core.prewarm import snapshot as _pw_snap
            for entry in _pw_snap().get("in_flight", []):
                _pw_cancel(entry["workspace"], entry["task_type"])
        except Exception:
            pass
        # ── Twin engine ─────────────────────────────────────────────────
        import core.twin_engine as _twin_mod
        _eng = getattr(_twin_mod, "_ENGINE", None)
        if _eng is not None:
            with contextlib.suppress(Exception):
                await _eng.shutdown()
                logger.info("twin_engine_stopped")

        # ── OTel span flush ─────────────────────────────────────────────
        try:
            from core.observe import flush_pending_spans
            n = flush_pending_spans()
            if n:
                logger.info("otel_spans_flushed_on_shutdown", extra={"count": n})
        except Exception:
            pass

        # ── Supabase audit tier (Stage 3.4) — close cleanly ─────────────
        if getattr(state, "supabase", None) is not None:
            with contextlib.suppress(Exception):
                await state.supabase.aclose()
                logger.info("supabase_audit_tier_closed")
        state.supabase = None
        state.supabase_bucket = None

        # ── Mongo / Redis / HTTP ────────────────────────────────────────
        if state.client is not None:
            with contextlib.suppress(Exception):
                state.client.close()
                logger.info("mongo_connection_closed")
        if state.redis_client is not None:
            with contextlib.suppress(Exception):
                await state.redis_client.aclose()
                logger.info("redis_connection_closed")
        with contextlib.suppress(Exception):
            await get_http_client().aclose()
            logger.info("http_client_closed")

        # ── Reference drops ─────────────────────────────────────────────
        for name in (
            "conversation_memory", "repo_indexer", "test_loop", "circuit_breaker",
            "orchestrator", "critic_agent", "context_registry", "dependency_tracker",
            "self_healer", "pr_defense", "touch_fix_engine", "provider_cascade",
            "twin_engine", "memory_bus", "supabase", "supabase_bucket",
        ):
            setattr(state, name, None)
        for name in ("twin_engine", "provider_cascade", "memory_bus"):
            setattr(app.state, name, None)
        # ── Memory tuner: re-enable cyclic GC ───────────────────────────
        with contextlib.suppress(Exception):
            memory_tuner_disarm()
        logger.info("axelr_shutdown_complete")


__all__ = [
    "background_health_check",
    "lifespan",
    "pr_defense_cleanup",
    "validate_all_providers",
]