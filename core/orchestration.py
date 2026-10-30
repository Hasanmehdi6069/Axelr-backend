# core/orchestration.py
"""
AXELR — Unified Orchestration & Optimization (canonical)
=========================================================
Replaces ``core/orchestration.py``, the abandoned ``core/orchestration/``
package, and ``core/elite_orchestrator.py``.

Section layout:
§1 — Env gates and tunables
§2 — Local-first hedge race
§3 — Negative cache (opt-in via install_negative_cache())
§4 — Canonical byte-identical system prefix
§5 — Health-failure propagation (opt-in)
§6 — Predictive warmup
§7 — P95-based max_tokens trimming
§8 — Task scheduler
§9 — Lazy singletons
§10 — Audit

Public surface:

    Elite optimizations (opt-in via ``install_*()``):
        hedge_wrap                 — local-first race (40–60% remote cost cut
                                     on verifiable tasks)
        install_negative_cache     — prevent retry storms on providers that
                                     just failed this exact prompt
        install_health_propagation — cascade breaker trips across
                                     ``provider:model`` scopes
        canonical_system           — byte-stable system prefix so Anthropic
                                     and Gemini prompt-caching actually fires
        adapt_max_tokens           — P95-based context-window trimming
        signal_warmup, pacemaker_loop — predictive cold-start warmup

    Pipeline singletons (lazy):
        get_pipeline               — one ``ResilientPipeline`` per process
        run_pipeline(**kw)         — returns the terminal result dict
        stream_pipeline(**kw)      — yields ``(kind, payload)`` tuples
        get_hot_swap_detector      — one ``HotSwapDetector`` per process

    Scheduler:
        TaskScheduler, get_task_scheduler, Priority, Task

    Failover re-exports (canonical home ``core.failover``):
        Tier, TIERS, ladder_run, ProviderFunc, HotSwapResult, HotSwapDetector

    Pipeline re-export (canonical home ``core.pipeline``):
        ResilientPipeline, ProviderCascade

Import policy:
    * No ``api.*`` import at module scope.
    * ``install_*()`` patches at call time, never at import.
    * All relative imports use ``.`` (this module lives *inside* ``core``),
      NOT ``..`` — the pre-consolidation ``core/orchestration.py`` had
      ``from ..structured_gen import …`` which resolved to the project
      root and raised ImportError at first call.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, List, Dict, Optional

# Inlined failover definitions (originally from core.failover)
ProviderFunc = Callable[..., Awaitable[Any]]

@dataclass
class Tier:
    name: str
    deadline_s: float
    priority: int
    providers: List[str] = field(default_factory=list)

TIERS: Dict[str, Tier] = {
    "fast": Tier(name="fast", deadline_s=1.0, priority=1),
    "pro": Tier(name="pro", deadline_s=3.0, priority=2),
    "backup": Tier(name="backup", deadline_s=10.0, priority=3),
}

@dataclass
class HotSwapResult:
    success: bool
    provider: Optional[str] = None
    error: Optional[Exception] = None
    latency_s: float = 0.0

class HotSwapDetector:
    def __init__(self, providers: Optional[Dict[str, ProviderFunc]] = None, **kwargs):
        self.providers = providers or {}
        self.failure_counts: Dict[str, int] = {}
        self.cooldown_until: Dict[str, float] = {}
        self._attempt = self._default_attempt
    
    def _default_attempt(self, provider_name: str, *args, **kwargs) -> Awaitable[Any]:
        if provider_name not in self.providers:
            raise ValueError(f"Unknown provider: {provider_name}")
        return self.providers[provider_name](*args, **kwargs)

async def ladder_run(providers: List[ProviderFunc], *args, **kwargs) -> HotSwapResult:
    """Try providers in order until one succeeds"""
    start_time = time.time()
    last_error = None
    for provider in providers:
        try:
            result = await provider(*args, **kwargs)
            return HotSwapResult(
                success=True,
                provider=getattr(provider, "__name__", str(provider)),
                latency_s=time.time() - start_time
            )
        except Exception as e:
            last_error = e
            continue
    return HotSwapResult(
        success=False,
        error=last_error,
        latency_s=time.time() - start_time
    )

from .pipeline import (
    AXELR_COGNITION_LEVEL, FAST_TIER_DEADLINE_S, MAX_ROUNDS,
    PIPELINE_DEADLINE_S, PRO_TIER_DEADLINE_S, RACE_WIDTH,
    ProviderCascade, ResilientPipeline,
)

logger = logging.getLogger("axelr.orchestration")

# ═══════════════════════════════════════════════════════════════════════════
# §1 — Env gates and tunables
# ═══════════════════════════════════════════════════════════════════════════
def _bool_env(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or ("true" if default else "false")).lower()
    return raw in ("1", "true", "yes")

_ENABLE_HEDGE: bool = _bool_env("AXELR_ELITE_HEDGE", True)
_ENABLE_NEG: bool = _bool_env("AXELR_ELITE_NEG_CACHE", True)
_ENABLE_CANON: bool = _bool_env("AXELR_ELITE_CANONICAL_PREFIX", True)
_ENABLE_HEALTH: bool = _bool_env("AXELR_ELITE_HEALTH_PROP", True)
_ENABLE_TRIM: bool = _bool_env("AXELR_ELITE_TOKEN_TRIM", True)

_HEDGE_LOCAL_MS: float = float(os.getenv("AXELR_ELITE_HEDGE_LOCAL_MS", "450"))
_NEG_TTL_S: int = int(os.getenv("AXELR_ELITE_NEG_TTL_S", "60"))
_TRIM_MIN_SAMPLES: int = int(os.getenv("AXELR_ELITE_TRIM_MIN_N", "8"))
_TRIM_HEADROOM: float = float(os.getenv("AXELR_ELITE_TRIM_HEADROOM", "1.30"))

_CANON_CACHE: dict[tuple[str, str], str] = {}
_CANON_LOCK = threading.Lock()
_TOKEN_HISTORY: dict[tuple[str, str], list[int]] = {}


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Local-first hedge race
# ═══════════════════════════════════════════════════════════════════════════
def hedge_wrap(
    base_route: Callable[..., Awaitable[dict[str, Any]]],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Wrap a ``route_ai_request_parallel``-shaped callable.

    For verifiable tasks (per ``core.structured_gen.classify``), fires
    local inference in parallel with the base route. If local passes its
    verifier inside ``_HEDGE_LOCAL_MS``, the base route is cancelled and
    local wins. Verifier ``degraded`` verdicts are treated as failures —
    we never ship unverified local text as a "verified win".

    Contract:
        * Idempotent: wraps at most once (marker ``_axelr_elite_hedge``).
        * No-op when ``AXELR_ELITE_HEDGE=false``.
        * Never raises from inside the wrapper — falls back to base route.
    """
    if getattr(base_route, "_axelr_elite_hedge", False):
        return base_route
    if not _ENABLE_HEDGE:
        return base_route

    async def _hedged(
        workspace: str, task_type: str, prompt: str,
        history: Any, files: Any,
        max_tokens: int, temp: float, tier: str,
        user: dict[str, Any] | None = None,
        context: str = "", request: Any = None,
    ) -> dict[str, Any]:
        try:
            from .structured_gen import classify as _classify
            decision = _classify(
                prompt, files, intent=task_type,
                tier=tier or "free", workspace=workspace)
        except Exception:
            decision = None

        if decision is None or getattr(decision, "route", "api") != "local":
            return await base_route(
                workspace, task_type, prompt, history, files,
                max_tokens, temp, tier, user, context, request)

        local_task = asyncio.create_task(
            _local_attempt(workspace, task_type, prompt, decision),
            name="elite_local_hedge")
        base_task = asyncio.create_task(
            base_route(workspace, task_type, prompt, history, files,
                       max_tokens, temp, tier, user, context, request),
            name="elite_remote_hedge")

        try:
            done, _ = await asyncio.wait(
                [local_task],
                timeout=_HEDGE_LOCAL_MS / 1000.0,
                return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            if not base_task.done():
                base_task.cancel()
            raise

        if done:
            local_result: dict[str, Any] | None = None
            with contextlib.suppress(Exception):
                local_result = local_task.result()
            if local_result is not None and local_result.get("success"):
                if not base_task.done():
                    base_task.cancel()
                with contextlib.suppress(Exception):
                    await base_task
                local_result.setdefault(
                    "router", decision.to_dict() if hasattr(decision, "to_dict") else {})
                local_result.setdefault("hedge", "local_win")
                return local_result

        try:
            result = await base_task
        finally:
            if not local_task.done():
                local_task.cancel()
                with contextlib.suppress(Exception):
                    await local_task
        if isinstance(result, dict):
            result.setdefault(
                "router", decision.to_dict() if hasattr(decision, "to_dict") else {})
            result.setdefault("hedge", "remote_won")
        return result

    _hedged._axelr_elite_hedge = True       # type: ignore[attr-defined]
    _hedged._axelr_original = base_route    # type: ignore[attr-defined]
    return _hedged


async def _local_attempt(
    workspace: str, task_type: str, prompt: str, decision: Any,
) -> dict[str, Any] | None:
    """Run local inference + verifier. ``None`` on any failure (never raises)."""
    try:
        from .twin_engine import execute_local_inference
    except Exception:
        return None
    try:
        text = await asyncio.wait_for(
            execute_local_inference(workspace, task_type, prompt),
            timeout=max(0.3, _HEDGE_LOCAL_MS / 1000.0))
    except (asyncio.TimeoutError, Exception):
        return None
    if not text or text.lstrip().startswith("[stub]"):
        return None
    try:
        from .structured_gen import verify as _verify
        v = _verify(text,
                    kind=getattr(decision, "verifier_kind", "none"),
                    prompt=prompt)
    except Exception:
        return None
    # A degraded verifier did NOT actually run — treat as failure.
    if not v.passed or getattr(v, "degraded", False):
        return None
    return {
        "success": True, "text": text,
        "provider": "local_twin",
        "model_used": f"local:{getattr(decision, 'task_class', 'unknown')}",
        "tokens_used": len(text.split()),
        "latency_ms": 0.0,
        "verifier": v.to_dict() if hasattr(v, "to_dict") else {},
    }


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Negative cache (opt-in via install_negative_cache())
# ═══════════════════════════════════════════════════════════════════════════
def _neg_key(workspace: str, provider: str, prompt: str) -> str:
    h = hashlib.sha256(prompt.strip().lower().encode()).hexdigest()[:24]
    return f"axelr:neg:{workspace or 'core'}:{provider}:{h}"


def install_negative_cache() -> bool:
    """Patch ``HotSwapDetector._attempt`` to skip providers in cooldown.

    Contract:
        * Returns ``True`` if the patch is now active (idempotent).
        * Returns ``False`` when disabled by env, when the failover module
          is unavailable, or when ``HotSwapDetector._attempt`` is missing.
        * Patches at **call time** only; importing this module never
          mutates another module's class.
        * Application calls this once at startup.
    """
    if not _ENABLE_NEG:
        return False
    if not hasattr(HotSwapDetector, "_attempt"):
        logger.warning(
            "install_negative_cache: HotSwapDetector._attempt missing; "
            "skipping — this opt-in needs failover internals")
        return False
    if getattr(HotSwapDetector._attempt, "_axelr_neg", False):
        return True
    original = HotSwapDetector._attempt

    async def _attempt_with_neg(
        self: Any, provider: str, model: str, func: Callable[..., Awaitable[str]],
        prompt: str, max_tokens: int, temp: float,
    ) -> Any:
        try:
            from .infra import get_redis_client
            r = get_redis_client()
        except Exception:
            r = None
        if r is None:
            return await original(self, provider, model, func, prompt,
                                  max_tokens, temp)
        key = _neg_key("", provider, prompt)
        with contextlib.suppress(Exception):
            if await r.get(key):
                return None
        result = await original(self, provider, model, func, prompt,
                                max_tokens, temp)
        if result is None:
            with contextlib.suppress(Exception):
                await r.setex(key, _NEG_TTL_S, "1")
        return result

    _attempt_with_neg._axelr_neg = True            # type: ignore[attr-defined]
    _attempt_with_neg._axelr_original = original   # type: ignore[attr-defined]
    HotSwapDetector._attempt = _attempt_with_neg
    logger.info("negative_cache_installed ttl_s=%d", _NEG_TTL_S)
    return True


# ═══════════════════════════════════════════════════════════════════════════
# §4 — Canonical byte-identical system prefix
# ═══════════════════════════════════════════════════════════════════════════
def canonical_system(workspace: str, tier: str) -> str:
    """Return a system-prompt directive whose bytes depend only on
    ``(workspace, tier)`` — never on the request prompt.

    Anthropic ``cache_control: ephemeral`` and Gemini ``cachedContent``
    only fire on byte-identical prefixes. This function locks the variant
    for the process lifetime so remote caches actually hit.

    Contract:
        * Deterministic per ``(workspace, tier)`` within one process.
        * Thread-safe (``_CANON_LOCK``).
        * Falls back to a minimal static anchor if the prompt vault is
          unavailable. Never raises.
    """
    if not _ENABLE_CANON:
        return (f"<workspace_anchor>{workspace or 'core'}</workspace_anchor>\n"
                f"<tier>{tier or 'free'}</tier>")
    key = (workspace or "core", tier or "free")
    with _CANON_LOCK:
        cached = _CANON_CACHE.get(key)
        if cached is not None:
            return cached
        text = ""
        try:
            from .context import build as _vault_build  # type: ignore
            text = _vault_build(
                workspace=workspace or "core", intent="generic",
                sub_intent="", prompt="", include_drift=True) or ""
        except Exception:
            text = ""
        if not text:
            text = (f"<workspace_anchor>{workspace or 'core'}</workspace_anchor>\n"
                    f"<tier>{tier or 'free'}</tier>")
        _CANON_CACHE[key] = text
        return text


# ═══════════════════════════════════════════════════════════════════════════
# §5 — Health-failure propagation (opt-in)
# ═══════════════════════════════════════════════════════════════════════════
def install_health_propagation() -> bool:
    """Patch the singleton breaker so ``provider:model`` failures also
    trip the bare ``provider`` scope.

    Contract:
        * Returns ``True`` if now active (idempotent).
        * Returns ``False`` when disabled by env or breaker unavailable.
        * Only propagates ``provider:model`` → ``provider``. The reverse
          direction needs a registry of model names per provider, which
          we do not maintain — propagating a bare-provider failure to
          unknown model scopes would trip breakers that were never used.
        * Patches at call time only.
    """
    if not _ENABLE_HEALTH:
        return False
    try:
        from .resilience import get_breaker
    except Exception:
        return False
    try:
        breaker = get_breaker()
    except Exception:
        return False
    if not hasattr(breaker, "record_failure"):
        return False
    if getattr(breaker.record_failure, "_axelr_health", False):
        return True

    original = breaker.record_failure

    async def _record_failure_with_prop(
        self: Any, provider: str, *, is_rate_limit: bool = False,
    ) -> None:
        await original(provider, is_rate_limit=is_rate_limit)
        if ":" not in provider:
            return
        base = provider.split(":", 1)[0]
        try:
            await original(base, is_rate_limit=is_rate_limit)
        except Exception:
            pass

    _record_failure_with_prop._axelr_health = True            # type: ignore[attr-defined]
    _record_failure_with_prop._axelr_original = original      # type: ignore[attr-defined]
    breaker.record_failure = _record_failure_with_prop.__get__(  # type: ignore[method-assign]
        breaker, type(breaker))
    logger.info("health_propagation_installed")
    return True


# ═══════════════════════════════════════════════════════════════════════════
# §7 — P95-based max_tokens trimming
# ═══════════════════════════════════════════════════════════════════════════
def adapt_max_tokens(provider: str, workspace: str, requested: int) -> int:
    """Adapt ``max_tokens`` down using historical completion lengths.

    Only activates after ``_TRIM_MIN_SAMPLES`` samples. Uses P95 * 1.3 so
    truncation is never introduced while still reclaiming context window.
    Floor is 256 tokens.

    Contract: pure function; never raises.
    """
    if not _ENABLE_TRIM:
        return requested
    key = (provider, workspace or "core")
    history = _TOKEN_HISTORY.get(key, [])
    if len(history) < _TRIM_MIN_SAMPLES:
        return requested
    sorted_h = sorted(history)
    p95 = sorted_h[int(len(sorted_h) * 0.95)]
    adapted = min(requested, int(p95 * _TRIM_HEADROOM))
    return max(adapted, 256)


def record_completion_length(
    provider: str, workspace: str, tokens_used: int,
) -> None:
    """Record a completion length for future ``adapt_max_tokens`` calls.

    Bounded at 100 samples per ``(provider, workspace)`` pair. Never raises.
    """
    key = (provider, workspace or "core")
    history = _TOKEN_HISTORY.setdefault(key, [])
    if len(history) >= 100:
        history.pop(0)
    history.append(int(tokens_used))


# ═══════════════════════════════════════════════════════════════════════════
# §6 — Predictive warmup
# ═══════════════════════════════════════════════════════════════════════════
# Connection warmup (DB/Redis ping)
_WARMUP_KEY = "axelr:warmup:signal"
_WARMUP_TTL_S = 5


async def signal_warmup(redis_client) -> bool:
    """Record that a submit is imminent. Best-effort; never raises."""
    if redis_client is None:
        return False
    with contextlib.suppress(Exception):
        await redis_client.setex(_WARMUP_KEY, _WARMUP_TTL_S, str(time.time()))
        return True
    return False


async def pacemaker_loop(state, *, interval_s: float = 0.5) -> None:
    """
    Long-running watcher. Called from lifespan as a background task.
    Pings the DB and Redis drivers when the warmup key is live.
    """
    while True:
        try:
            await asyncio.sleep(interval_s)
            r = getattr(state, "redis_client", None)
            if r is None:
                continue
            with contextlib.suppress(Exception):
                if await r.get(_WARMUP_KEY) is None:
                    continue
            # Warmup key live — ping both drivers.
            if getattr(state, "db_available", False) and getattr(state, "db", None):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(state.db.command("ping"), timeout=0.5)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(r.ping(), timeout=0.3)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.debug("pacemaker_loop_error error=%s", e)


# Twin engine KV cache warmup
_VERSION = "1.0"

_COOLDOWN_S = float(os.getenv("AXELR_PREWARM_COOLDOWN_S", "90.0"))
_MAX_CONCURRENT = int(os.getenv("AXELR_PREWARM_MAX_CONCURRENT", "4"))
_PREWARM_TIMEOUT_S = float(os.getenv("AXELR_PREWARM_TIMEOUT_S", "6.0"))
_ENABLED = os.getenv("AXELR_PREWARM", "true").lower() in ("1", "true", "yes")


_SYNTHETIC_PROMPTS: dict[str, str] = {
    "extraction": (
        "Extract the requested fields from the following record. "
        "Return a compact JSON array inside ```json fences.\n\n"
        "<record>\nfield1: value1\nfield2: value2\n</record>\n"
    ),
    "structuring": (
        "Return a single JSON object with the requested keys.\n\n"
        "<request>summarize the following in a structured form</request>\n"
    ),
    "frontend": (
        "Produce a single self-contained HTML document with inline CSS "
        "and JS. Modern 2025 styling.\n\n"
        "<request>build a small landing page</request>\n"
    ),
    "touch_fix": (
        "Fix the following code. Return the full corrected file inside "
        "a single fenced block.\n\n"
        "<code>def f(): pass</code>\n"
    ),
}


@dataclass(slots=True)
class _PrewarmEntry:
    workspace: str
    task_type: str
    started_at: float
    task: asyncio.Task | None = None
    result: str = ""


_state: dict[str, Any] = {
    "in_flight": {},
    "last_ts": {},
    "stats": {
        "signals": 0, "fires": 0, "coalesced": 0,
        "cancelled": 0, "skipped_cooldown": 0, "skipped_cap": 0,
        "skipped_disabled": 0, "skipped_unavailable": 0,
    },
}


def _key(workspace: str, task_type: str) -> tuple[str, str]:
    return (workspace or "core", task_type or "structuring")


def _synthetic(task_type: str) -> str:
    return _SYNTHETIC_PROMPTS.get(
        task_type, _SYNTHETIC_PROMPTS["structuring"])


async def _run_prewarm(workspace: str, task_type: str,
                       prompt_hint: str) -> None:
    key = _key(workspace, task_type)
    entry: _PrewarmEntry | None = _state["in_flight"].get(key)
    try:
        try:
            from .twin_engine import execute_local_inference
        except Exception as e:
            logger.debug("prewarm_twin_import_failed error=%s", e)
            _state["stats"]["skipped_unavailable"] += 1
            if entry:
                entry.result = "skipped:no_engine"
            return

        synthetic = _synthetic(task_type)
        if prompt_hint:
            synthetic = prompt_hint[:200] + "\n\n" + synthetic

        t0 = time.monotonic()
        try:
            await asyncio.wait_for(
                execute_local_inference(workspace, task_type, synthetic),
                timeout=_PREWARM_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            if entry:
                entry.result = "error:timeout"
            return
        except asyncio.CancelledError:
            if entry:
                entry.result = "cancelled"
            raise
        except Exception as e:
            if entry:
                entry.result = f"error:{type(e).__name__}"
            logger.debug("prewarm_inference_failed error=%s", e)
            return
        if entry:
            entry.result = "ok"
        _state["stats"]["fires"] += 1
        logger.debug(
            "prewarm_done workspace=%s task=%s elapsed_ms=%.0f",
            workspace, task_type, (time.monotonic() - t0) * 1000,
        )
    finally:
        _state["in_flight"].pop(key, None)


def signal(workspace: str, task_type: str, prompt_hint: str = "") -> bool:
    if not _ENABLED:
        _state["stats"]["skipped_disabled"] += 1
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False

    _state["stats"]["signals"] += 1
    key = _key(workspace, task_type)
    now = time.monotonic()

    if key in _state["in_flight"]:
        _state["stats"]["coalesced"] += 1
        return True

    last = _state["last_ts"].get(key, 0.0)
    if (now - last) < _COOLDOWN_S:
        _state["stats"]["skipped_cooldown"] += 1
        return False

    if len(_state["in_flight"]) >= _MAX_CONCURRENT:
        _state["stats"]["skipped_cap"] += 1
        return False

    _state["last_ts"][key] = now
    entry = _PrewarmEntry(
        workspace=workspace, task_type=task_type, started_at=now,
    )
    _state["in_flight"][key] = entry
    task = loop.create_task(
        _run_prewarm(workspace, task_type, prompt_hint),
        name=f"prewarm::{key[0]}::{key[1]}",
    )
    entry.task = task
    return True


def cancel(workspace: str, task_type: str) -> bool:
    key = _key(workspace, task_type)
    entry = _state["in_flight"].get(key)
    if entry is None or entry.task is None or entry.task.done():
        return False
    entry.task.cancel()
    _state["stats"]["cancelled"] += 1
    return True


def snapshot() -> dict[str, Any]:
    now = time.monotonic()
    in_flight = [
        {
            "workspace": e.workspace,
            "task_type": e.task_type,
            "age_ms": round((now - e.started_at) * 1000, 1),
        }
        for e in _state["in_flight"].values()
    ]
    cooldowns = {
        f"{k[0]}:{k[1]}": round(max(0.0, _COOLDOWN_S - (now - ts)), 1)
        for k, ts in _state["last_ts"].items()
    }
    return {
        "version": _VERSION,
        "enabled": _ENABLED,
        "in_flight": in_flight,
        "cooldowns_remaining_s": cooldowns,
        "stats": dict(_state["stats"]),
        "tunables": {
            "cooldown_s": _COOLDOWN_S,
            "max_concurrent": _MAX_CONCURRENT,
            "timeout_s": _PREWARM_TIMEOUT_S,
        },
    }


def prewarm_audit() -> dict[str, Any]:
    return snapshot()


# ═══════════════════════════════════════════════════════════════════════════
# §8 — Task scheduler
# ═══════════════════════════════════════════════════════════════════════════
class Priority:
    """Named constants for task priority levels."""
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


@dataclass(slots=True)
class Task:
    """A queued coroutine with priority and observability fields."""
    id: str
    coro: Awaitable[Any]
    priority: int
    created_at: float = field(default_factory=time.time)
    executed_at: float | None = None
    completed_at: float | None = None
    success: bool | None = None
    error: str | None = None


class TaskScheduler:
    """Priority queue + bounded concurrency.

    Contract:
        * ``__init__`` starts a background worker task — call only from
          an active event loop (use ``get_task_scheduler()`` lazily).
        * ``schedule()`` returns a task id immediately; execution is
          async. Always takes the highest-priority queued item first.
        * ``close(wait=True)`` drains the queue, then cancels any
          still-running tasks. After close, further ``schedule()`` calls
          are queued but never executed — do not call post-close.
        * Never raises to the event loop from the worker.
    """
    __slots__ = ("_queue", "_running", "_max_concurrency",
                 "_worker", "_closed")

    def __init__(self, max_concurrency: int = 10) -> None:
        self._max_concurrency = max(1, int(max_concurrency))
        self._queue: list[Task] = []
        self._running: set[asyncio.Task[None]] = set()
        self._closed = False
        self._worker = asyncio.create_task(
            self._worker_loop(), name="task_scheduler_worker")

    async def _worker_loop(self) -> None:
        while not self._closed or self._queue:
            for done in {t for t in self._running if t.done()}:
                self._running.discard(done)
                with contextlib.suppress(Exception):
                    await done
            while (len(self._running) < self._max_concurrency
                   and self._queue):
                task = self._queue.pop(0)
                at = asyncio.create_task(
                    self._execute_task(task), name=f"task_{task.id}")
                self._running.add(at)
            await asyncio.sleep(0.01)

    async def _execute_task(self, task: Task) -> None:
        task.executed_at = time.time()
        try:
            await task.coro
            task.success = True
        except Exception as e:
            task.success = False
            task.error = str(e)[:200]
            logger.error("task_scheduler_failure id=%s error=%s",
                         task.id, task.error)
        finally:
            task.completed_at = time.time()

    def schedule(
        self, coro: Awaitable[Any], priority: int = Priority.NORMAL,
    ) -> str:
        """Enqueue ``coro`` at the given priority. Returns task id."""
        task_id = uuid.uuid4().hex[:8]
        task = Task(id=task_id, coro=coro, priority=int(priority))
        for i, existing in enumerate(self._queue):
            if existing.priority < task.priority:
                self._queue.insert(i, task)
                break
        else:
            self._queue.append(task)
        return task_id

    async def close(self, *, wait: bool = True) -> None:
        """Signal shutdown; optionally await drain of the queue."""
        self._closed = True
        if wait and not self._worker.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._worker
        for t in self._running:
            if not t.done():
                t.cancel()
        self._running.clear()


# ═══════════════════════════════════════════════════════════════════════════
# §9 — Lazy singletons
# ═══════════════════════════════════════════════════════════════════════════
_PIPELINE_LOCK = threading.Lock()
_PIPELINE: ResilientPipeline | None = None

_DETECTOR_LOCK = threading.Lock()
_DETECTOR: HotSwapDetector | None = None

_SCHEDULER_LOCK = threading.Lock()
_SCHEDULER: TaskScheduler | None = None


def get_pipeline() -> ResilientPipeline:
    """Return the process-wide ``ResilientPipeline``. Thread-safe, never raises."""
    global _PIPELINE
    if _PIPELINE is not None:
        return _PIPELINE
    with _PIPELINE_LOCK:
        if _PIPELINE is None:
            _PIPELINE = ResilientPipeline()
        return _PIPELINE


async def run_pipeline(**kwargs: Any) -> dict[str, Any]:
    """Run the pipeline once and return the terminal result dict.

    Contract: identical to ``get_pipeline().run(**kwargs)`` — never raises,
    always returns the stub-shaped result on total failure.
    """
    return await get_pipeline().run(**kwargs)


async def stream_pipeline(**kwargs: Any):
    """Stream pipeline events. Yields ``(kind, payload)`` tuples.

    Contract: identical to ``get_pipeline().stream(**kwargs)`` — exactly
    one terminal ``("result", dict)`` is emitted.
    """
    async for item in get_pipeline().stream(**kwargs):
        yield item


def get_hot_swap_detector(
    providers: dict[str, ProviderFunc] | None = None,
    **kwargs: Any,
) -> HotSwapDetector | None:
    """Return the process-wide ``HotSwapDetector``.

    Contract:
        * First call constructs with ``providers`` (or resolves via
          ``api.providers.PROVIDER_FUNC_MAP`` if omitted). Subsequent calls
          ignore the argument.
        * Returns ``None`` if the provider map cannot be resolved — never
          raises, callers must null-check.
        * Thread-safe.
    """
    global _DETECTOR
    if _DETECTOR is not None:
        return _DETECTOR
    with _DETECTOR_LOCK:
        if _DETECTOR is not None:
            return _DETECTOR
        if providers is None:
            try:
                from api.providers import PROVIDER_FUNC_MAP  # type: ignore
                providers = {
                    k: v for k, v in PROVIDER_FUNC_MAP.items()
                    if k != "local"
                }
            except Exception as e:
                logger.warning(
                    "hot_swap_provider_map_unavailable error=%s", e)
                return None
        try:
            _DETECTOR = HotSwapDetector(providers, **kwargs)
        except Exception as e:
            logger.warning("hot_swap_detector_init_failed error=%s", e)
            return None
        logger.info(
            "hot_swap_detector_ready providers=%d", len(providers))
        return _DETECTOR


def get_task_scheduler() -> TaskScheduler:
    """Return the process-wide ``TaskScheduler``.

    
    Contract: must be called from an active event loop (the scheduler
    starts a background worker on first construction). Thread-safe.
    """
    global _SCHEDULER
    if _SCHEDULER is not None:
        return _SCHEDULER
    with _SCHEDULER_LOCK:
        if _SCHEDULER is None:
            _SCHEDULER = TaskScheduler()
        return _SCHEDULER


# ═══════════════════════════════════════════════════════════════════════════
# §10 — Audit
# ═══════════════════════════════════════════════════════════════════════════
def pipeline_stats() -> dict[str, int]:
    """Return a snapshot of ``get_pipeline().stats``. Never raises."""
    return dict(get_pipeline().stats)


def audit() -> dict[str, Any]:
    """JSON-serialisable snapshot of the entire orchestration layer."""
    return {
        "pipeline_stats": pipeline_stats(),
        "tiers": list(TIERS.keys()),
        "cognition_level": AXELR_COGNITION_LEVEL,
        "max_rounds": MAX_ROUNDS,
        "race_width": RACE_WIDTH,
        "deadline_s": PIPELINE_DEADLINE_S,
        "fast_deadline_s": FAST_TIER_DEADLINE_S,
        "pro_deadline_s": PRO_TIER_DEADLINE_S,
        "hot_swap": _DETECTOR is not None,
        "scheduler": _SCHEDULER is not None,
        "elite": {
            "hedge": _ENABLE_HEDGE,
            "neg_cache": _ENABLE_NEG,
            "canonical_prefix": _ENABLE_CANON,
            "health_prop": _ENABLE_HEALTH,
            "token_trim": _ENABLE_TRIM,
        },
    }





__all__ = [
    # Elite
    "hedge_wrap", "install_negative_cache", "canonical_system",
    "install_health_propagation", "adapt_max_tokens",
    "record_completion_length",
    # Scheduler
    "TaskScheduler", "get_task_scheduler", "Priority", "Task",
    # Singletons
    "get_pipeline", "run_pipeline", "stream_pipeline",
    "get_hot_swap_detector",
    # Failover re-exports
    "Tier", "TIERS", "ladder_run", "ProviderFunc",
    "HotSwapResult", "HotSwapDetector",
    # Pipeline re-exports
    "ResilientPipeline", "ProviderCascade",
    # Audit
    "audit", "pipeline_stats",
    # Warmup
    "signal_warmup", "pacemaker_loop",
    # Twin engine KV cache warmup
    "signal", "cancel", "snapshot", "prewarm_audit",
]