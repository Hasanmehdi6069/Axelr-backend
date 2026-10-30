<<<<<<< HEAD
# core/failover.py
"""
AXELR — Failover & Hotswap Infrastructure (canonical)
====================================================
Provides tiered provider failover logic, hotswap detection, and provider ladder
execution. This is the canonical home for all failover-related functionality.
"""
from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence, TypeVar

logger = logging.getLogger("axelr.failover")

T = TypeVar("T")

# ──────────────────────────────────────────────────────────────────────────────
# Tier Definitions
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Tier:
    """Represents a provider tier with priority, cost, and capability flags."""
    name: str
    priority: int  # lower = higher priority
    cost_per_1k_tokens: float
    supports_streaming: bool = True
    supports_function_calling: bool = True

# Predefined tiers
TIERS = {
    "local": Tier(
        name="local",
        priority=0,
        cost_per_1k_tokens=0.0,
        supports_streaming=True,
        supports_function_calling=True
    ),
    "first_party": Tier(
        name="first_party",
        priority=1,
        cost_per_1k_tokens=0.05,
        supports_streaming=True,
        supports_function_calling=True
    ),
    "premium": Tier(
        name="premium",
        priority=2,
        cost_per_1k_tokens=0.15,
        supports_streaming=True,
        supports_function_calling=True
    ),
    "fallback": Tier(
        name="fallback",
        priority=3,
        cost_per_1k_tokens=0.30,
        supports_streaming=True,
        supports_function_calling=False
    )
}

# ──────────────────────────────────────────────────────────────────────────────
# Core Failover Types
# ──────────────────────────────────────────────────────────────────────────────
ProviderFunc = Callable[..., Awaitable[Any]]

@dataclass(frozen=True, slots=True)
class HotSwapResult:
    """Result of a hotswap operation between providers."""
    success: bool
    original_provider: str
    new_provider: str
    latency_saved: float
    error: str | None = None

@dataclass
class HotSwapDetector:
    """Detects when a provider is underperforming and triggers hotswap."""
    threshold_latency_p95: float = 2.0  # seconds
    min_samples: int = 5
    latency_history: dict[str, list[float]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __init__(self, threshold_latency_p95: float = 2.0):
        self.threshold_latency_p95 = threshold_latency_p95
        self.latency_history = {}
        self._lock = asyncio.Lock()

    async def record_latency(self, provider: str, latency: float) -> None:
        """Record a latency sample for a provider."""
        async with self._lock:
            if provider not in self.latency_history:
                self.latency_history[provider] = []
            self.latency_history[provider].append(latency)
            # Keep only last 100 samples
            if len(self.latency_history[provider]) > 100:
                self.latency_history[provider] = self.latency_history[provider][-100:]

    async def should_hotswap(self, provider: str) -> bool:
        """Check if a provider's p95 latency exceeds threshold."""
        async with self._lock:
            if provider not in self.latency_history:
                return False
            samples = self.latency_history[provider]
            if len(samples) < self.min_samples:
                return False
            # Calculate p95
            sorted_samples = sorted(samples)
            p95_index = int(len(sorted_samples) * 0.95)
            p95 = sorted_samples[p95_index]
            return p95 > self.threshold_latency_p95

    async def audit(self) -> dict[str, Any]:
        """Return audit stats for all providers."""
        async with self._lock:
            return {
                "threshold": self.threshold_latency_p95,
                "providers": {
                    provider: {
                        "samples": len(latencies),
                        "p95": sorted(latencies)[int(len(latencies)*0.95)] if len(latencies)>=5 else None
                    }
                    for provider, latencies in self.latency_history.items()
                }
            }

# Lazy singleton for hotswap detector
_detector_instance: HotSwapDetector | None = None

def get_hotswap_detector() -> HotSwapDetector:
    """Get the global HotSwapDetector instance (lazy singleton)."""
    global _detector_instance
    if _detector_instance is None:
        _detector_instance = HotSwapDetector()
    return _detector_instance

# ──────────────────────────────────────────────────────────────────────────────
# Ladder Execution
# ──────────────────────────────────────────────────────────────────────────────
async def ladder_run(
    providers: Sequence[ProviderFunc],
    *args: Any,
    stop_on_success: bool = True,
    **kwargs: Any
) -> list[Any]:
    """Run providers in order (ladder), stopping on first success if requested."""
    results = []
    for provider in providers:
        try:
            result = await provider(*args, **kwargs)
            results.append(result)
            if stop_on_success and result.get("success", False):
                return results
        except Exception as e:
            logger.warning(f"Provider failed in ladder run: {str(e)}")
            results.append({"success": False, "error": str(e)})
            if stop_on_success:
                continue
    return results

# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────
__all__ = [
    "Tier",
    "TIERS",
    "ladder_run",
    "ProviderFunc",
    "HotSwapResult",
    "HotSwapDetector",
    "get_hotswap_detector"
=======
# core/failover.py
"""
AXELR — Failover Primitives (ladder · hot-swap detector)
==========================================================
Two interchangeable failover strategies used by `resilience.ResilientPipeline`:

    §1 TIERS + ladder_run     named-tier degradation (best→worst)
    §2 HotSwapDetector        mid-flight provider failover with health EWMA

Public:
    from core.failover import TIERS, Tier, ladder_run
    from core.failover import HotSwapDetector, HotSwapResult, get_hot_swap_detector
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable

logger = logging.getLogger("axelr.failover")

_MAX_WAVE_DEPTH = 6  # hard cap on multi-wave recursion


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Degradation ladder
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class Tier:
    level: int
    name: str
    degraded: bool


TIERS: dict[str, Tier] = {
    "full":           Tier(0, "full", False),
    "reduced_ctx":    Tier(1, "reduced_ctx", True),
    "reduced_tokens": Tier(2, "reduced_tokens", True),
    "cached_similar": Tier(3, "cached_similar", True),
    "local":          Tier(4, "local", True),
    "stub":           Tier(5, "stub", True),
}


async def ladder_run(
    *,
    full: Callable[[], Awaitable[dict[str, Any]]],
    reduced_ctx: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    reduced_tokens: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    cached_similar: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    local: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    stub: Callable[[], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Walk the ladder. First success wins. Never raises."""
    steps = [(n, f) for n, f in (
        ("full", full), ("reduced_ctx", reduced_ctx),
        ("reduced_tokens", reduced_tokens), ("cached_similar", cached_similar),
        ("local", local),
    ) if f is not None]

    for name, fn in steps:
        tier = TIERS[name]
        t0 = time.monotonic()
        try:
            result = await fn()
        except Exception as e:
            logger.warning("ladder_tier_failed tier=%s error=%s", name, e)
            continue
        if isinstance(result, dict) and result.get("success") and result.get("text"):
            result["tier"] = tier.name
            result["degraded"] = tier.degraded
            result["ladder_ms"] = round((time.monotonic() - t0) * 1000, 2)
            return result

    if stub is not None:
        try:
            result = await stub()
            if isinstance(result, dict):
                result.setdefault("success", True)
                result.setdefault("text", "⚠️ All AI services are temporarily busy.")
                result["tier"] = TIERS["stub"].name
                result["degraded"] = True
                return result
        except Exception as e:
            logger.warning("ladder_stub_failed error=%s", e)

    return {"success": False, "text": "⚠️ All AI services are temporarily busy.",
            "provider": "none", "model_used": "none",
            "tokens_used": 0, "latency_ms": 0.0,
            "tier": TIERS["stub"].name, "degraded": True}


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Hot-swap detector
# ═══════════════════════════════════════════════════════════════════════════
ProviderFunc = Callable[[str, int, float, str], Awaitable[str]]


@dataclass(slots=True)
class _Health:
    provider: str
    successes: int = 0
    failures: int = 0
    ttft_ms: float = 0.0
    last_ts: float = 0.0

    @property
    def score(self) -> float:
        total = self.successes + self.failures
        if total == 0:
            return 0.5
        return 0.7 * (self.successes / total) + \
               0.3 * (1.0 / (1.0 + max(0.0, self.ttft_ms) / 800.0))


@dataclass(slots=True)
class HotSwapResult:
    provider: str
    model: str
    text: str
    first_token_ms: float
    total_ms: float
    attempts: int
    swapped_from: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model,
                "first_token_ms": round(self.first_token_ms, 2),
                "total_ms": round(self.total_ms, 2),
                "attempts": self.attempts, "swapped_from": self.swapped_from}


class HotSwapDetector:
    """Race top-K providers; first valid response wins. Mid-flight failover."""
    __slots__ = ("_alpha", "_health", "_lock", "_providers", "_race_width",
                 "_recent", "_stall_s", "_timeout_s")

    def __init__(self, providers: dict[str, ProviderFunc], *,
                 race_width: int = 3, stall_ms: int = 2500,
                 attempt_timeout_s: float = 25.0, ewma_alpha: float = 0.3) -> None:
        self._providers = dict(providers)
        self._race_width = max(1, int(race_width))
        self._stall_s = max(0.3, stall_ms / 1000.0)
        self._timeout_s = max(1.0, float(attempt_timeout_s))
        self._alpha = max(0.05, min(0.9, float(ewma_alpha)))
        self._health = {p: _Health(provider=p) for p in self._providers}
        self._recent: deque[dict[str, Any]] = deque(maxlen=64)
        self._lock = asyncio.Lock()

    def snapshot(self) -> dict[str, Any]:
        return {
            "providers": [
                {"provider": h.provider, "score": round(h.score, 3),
                 "ttft_ms": round(h.ttft_ms, 1),
                 "successes": h.successes, "failures": h.failures}
                for h in sorted(self._health.values(), key=lambda h: -h.score)],
            "recent": list(self._recent),
            "race_width": self._race_width,
            "stall_ms": int(self._stall_s * 1000),
        }

    async def stream(self, *, workspace: str, task_type: str, prompt: str,
                     max_tokens: int = 2048, temp: float = 0.4,
                     candidate_providers: list[str] | None = None,
                     model_map: dict[str, list[str]] | None = None,
                     exclude: set[str] | None = None
                     ) -> AsyncIterator[tuple[str, Any]]:
        exclude = exclude or set()
        candidates = candidate_providers or list(self._providers.keys())
        ranked = await self._rank(candidates, exclude)
        if not ranked:
            yield ("failed", {"reason": "no_candidates"})
            return
        result = await self._race_waves(
            ranked, model_map or {}, prompt, max_tokens, temp,
        )
        if result is None:
            yield ("failed", {"reason": "all_providers_failed",
                              "wave": ranked[:self._race_width]})
            return
        yield ("winner", result)

    async def _rank(self, candidates: list[str], exclude: set[str]) -> list[str]:
        async with self._lock:
            scored: list[tuple[float, str]] = []
            for p in candidates:
                if p in exclude or p not in self._providers:
                    continue
                scored.append((self._health.setdefault(p, _Health(provider=p)).score, p))
            scored.sort(key=lambda t: (-t[0], t[1]))
            return [p for _, p in scored]

    async def _race_waves(
        self, ranked: list[str], model_map: dict[str, list[str]],
        prompt: str, max_tokens: int, temp: float,
    ) -> HotSwapResult | None:
        """
        Bounded iteration — not recursion. Fixes B1: the previous
        implementation called `_race_wave(..., wave=wave+1)` on a method
        that has no `wave` kwarg, causing `TypeError` on the second wave.
        """
        width = self._race_width
        for wave_index in range(_MAX_WAVE_DEPTH):
            start = wave_index * width
            if start >= len(ranked):
                return None
            wave = ranked[start:start + width]
            if not wave:
                return None
            result = await self._race_wave(wave, model_map, prompt, max_tokens, temp)
            if result is not None:
                return result
            logger.info(
                "wave_failed_trying_next wave_idx=%d wave=%s remaining=%d",
                wave_index, wave, len(ranked) - (start + width),
            )
        return None

    async def _race_wave(self, wave: list[str], model_map: dict[str, list[str]],
                         prompt: str, max_tokens: int,
                         temp: float) -> HotSwapResult | None:
        if not wave:
            return None
        # Create tasks with their metadata embedded directly in coroutine returns
        async def wrapped_attempt(p: str, model: str, func: ProviderFunc) -> tuple[str, str, float, dict[str, Any] | None]:
            t0 = time.monotonic()
            outcome = await self._attempt(p, model, func, prompt, max_tokens, temp)
            return (p, model, t0, outcome)
            
        tasks: list[asyncio.Task] = []
        for p in wave:
            func = self._providers.get(p)
            if not func:
                continue
            models = model_map.get(p)
            if not models:
                model = "default-model"  # Fallback if no models specified
            else:
                model = models[0]
            # Always create a task even with default model - no more skipped providers
            task = asyncio.create_task(
                wrapped_attempt(p, model, func),
                name=f"hotswap::{p}")
            tasks.append(task)
        if not tasks:
            logger.warning(f"_race_wave_no_tasks wave={wave}")
            return None
        logger.info(f"_race_wave_created_tasks wave={wave} tasks_created={len(tasks)}")
        winner: HotSwapResult | None = None
        try:
            for fut in asyncio.as_completed(tasks, timeout=self._timeout_s + 5.0):
                try:
                    p, model, t0, outcome = await fut
                except Exception as e:
                    logger.debug("provider_task_failed error=%s", str(e)[:100])
                    continue
                if outcome is None:
                    logger.debug(f"provider_task_returned_none provider={p}")
                    continue
                # We got a valid outcome! Set as winner immediately
                logger.debug(f"provider_task_succeeded provider={p} model={model} resp_length={len(outcome['text'])}")
                await self._record(p, success=True, ttft_ms=outcome["ttft_ms"])
                winner = HotSwapResult(
                    provider=p, model=model, text=outcome["text"],
                    first_token_ms=outcome["ttft_ms"],
                    total_ms=(time.monotonic() - t0) * 1000.0,
                    attempts=len(tasks),
                )
                break
        except asyncio.TimeoutError:
            logger.debug("wave_timeout wave=%s timeout=%ss",
                         wave, self._timeout_s + 5.0)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if winner is None:
            logger.warning(f"_race_wave_no_winner wave={wave}")
            # Record failures for all tasks that didn't succeed
            for t in tasks:
                if t.done():
                    try:
                        p, _, _, _ = t.result()
                        await self._record(p, success=False, ttft_ms=0.0)
                    except:
                        pass
        return winner

    async def _attempt(self, provider: str, model: str, func: ProviderFunc,
                       prompt: str, max_tokens: int,
                       temp: float) -> dict[str, Any] | None:
        logger.debug(f"_attempt_start provider={provider} model={model}")
        t0 = time.monotonic()
        try:
            resp = await asyncio.wait_for(
                func(prompt, max_tokens, temp, model), timeout=self._timeout_s)
            logger.debug(f"_attempt_success provider={provider} model={model} resp_length={len(resp)}")
            if not resp or len(resp.strip()) <= 3:
                logger.debug(f"_attempt_empty_response provider={provider} model={model}")
                return None
            ttft = (time.monotonic() - t0) * 1000.0
            self._recent.append({"provider": provider, "model": model,
                                 "ttft_ms": round(ttft, 1), "ok": True,
                                 "t": time.time()})
            return {"text": resp, "ttft_ms": ttft}
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.debug(f"_attempt_timeout_cancelled provider={provider}")
            raise
        except Exception as e:
            logger.debug(f"_attempt_exception provider={provider} error={str(e)[:80]}")
            self._recent.append({"provider": provider, "model": model,
                                 "error": str(e)[:80], "ok": False,
                                 "t": time.time()})
            return None

    async def _record(self, provider: str, *, success: bool, ttft_ms: float) -> None:
        async with self._lock:
            h = self._health.setdefault(provider, _Health(provider=provider))
            h.last_ts = time.time()
            if success:
                h.successes += 1
                h.ttft_ms = ttft_ms if h.ttft_ms <= 0 else \
                    self._alpha * ttft_ms + (1 - self._alpha) * h.ttft_ms
            else:
                h.failures += 1


# ── Singleton ─────────────────────────────────────────────────────────────
_DETECTOR: HotSwapDetector | None = None


def get_hot_swap_detector(providers: dict[str, ProviderFunc] | None = None,
                          **kwargs: Any) -> HotSwapDetector | None:
    global _DETECTOR
    if _DETECTOR is not None:
        return _DETECTOR
    if providers is None:
        try:
            from api.providers import PROVIDER_FUNC_MAP  # type: ignore
            providers = {k: v for k, v in PROVIDER_FUNC_MAP.items() if k != "local"}
        except Exception as e:
            logger.warning("hot_swap_provider_map_unavailable error=%s", e)
            return None
    _DETECTOR = HotSwapDetector(providers, **kwargs)
    logger.info("hot_swap_detector_ready providers=%d", len(providers))
    return _DETECTOR


__all__ = [
    "Tier", "TIERS", "ladder_run",
    "ProviderFunc", "HotSwapResult", "HotSwapDetector", "get_hot_swap_detector",
>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
]