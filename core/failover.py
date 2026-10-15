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
]