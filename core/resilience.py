# core/resilience.py
"""
AXELR — Resilience Layer (circuit breaker · failover · RTT tracker)
=====================================================================
Three coupling points for provider-failure isolation. All state bounded,
all fallbacks degraded, all public methods catch their own exceptions.

    §1  CircuitBreaker      distributed closed/half/open breaker
    §2  Failover ladder     named-tier degradation (full → stub)
    §3  HotSwapDetector     mid-flight provider race with health EWMA
    §4  WaveRacer           live p90 RTT tracker for hedge timing

Design:
    * Redis-backed when available, memory-only otherwise.
    * Every path has a bounded retry.
    * ``BREAKER`` is safe to access at import (created eagerly).
"""
from __future__ import annotations

import asyncio
import logging
import os
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .foundation import load as _cfg

logger = logging.getLogger("axelr.resilience")

# Add missing constant that was being imported
AXELR_COGNITION_LEVEL = os.getenv("AXELR_COGNITION_LEVEL", "standard")

__all__ = [
    "CircuitBreaker", "BREAKER", "get_breaker",
    "FailoverTier", "TIERS", "ladder_run",
    "HotSwapResult", "HotSwapDetector", "get_hot_swap_detector",
    "WaveRacer", "RACER",
    "AXELR_COGNITION_LEVEL",
]


# ═══════════════════════════════════════════════════════════════════════════
# §1 — CircuitBreaker (canonical)
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class _State:
    failures: list[float] = field(default_factory=list)
    opened_until: float = 0.0
    last_success: float = 0.0


class CircuitBreaker:
    """
    Distributed circuit breaker. Redis primary, memory fallback.
    Never raises. States: closed (normal), open (blocking), half-open
    (testing recovery after cooldown).
    """
    __slots__ = ("_redis", "_lock", "_states", "_threshold",
                 "_cooldown_s", "_rl_cooldown_s", "_window_s", "_ns")

    def __init__(self, *, redis: Any = None, namespace: str = "axelr:cb") -> None:
        cfg = _cfg()
        self._redis = redis
        self._lock = asyncio.Lock()
        self._states: dict[str, _State] = {}
        self._threshold = max(1, cfg.cb_threshold)
        self._cooldown_s = max(0.0, cfg.cb_cooldown_s)
        self._rl_cooldown_s = max(0.0, cfg.cb_rate_limit_cooldown_s)
        self._window_s = max(1.0, self._cooldown_s)
        self._ns = namespace

    def _key_fail(self, scope: str) -> str:
        return f"{self._ns}:fail:{scope}"

    def _key_open(self, scope: str) -> str:
        return f"{self._ns}:open:{scope}"

    async def is_available(self, scope: str) -> bool:
        if self._redis is not None:
            try:
                if await self._redis.get(self._key_open(scope)) is not None:
                    return False
                return True
            except Exception:
                pass
        async with self._lock:
            st = self._states.get(scope)
            if st is None:
                return True
            if st.opened_until and time.monotonic() < st.opened_until:
                return False
            if st.opened_until:
                st.opened_until = 0.0
                st.failures.clear()
            return True

    async def record_success(self, scope: str) -> None:
        if self._redis is not None:
            try:
                await self._redis.delete(self._key_fail(scope))
                await self._redis.delete(self._key_open(scope))
            except Exception:
                pass
        async with self._lock:
            st = self._states.setdefault(scope, _State())
            st.failures.clear()
            st.opened_until = 0.0
            st.last_success = time.monotonic()

    async def record_failure(self, scope: str, *, is_rate_limit: bool = False) -> None:
        cooldown = self._rl_cooldown_s if is_rate_limit else self._cooldown_s
        if self._redis is not None:
            try:
                return await self._record_redis(scope, cooldown, is_rate_limit)
            except Exception:
                pass
        async with self._lock:
            self._record_mem(scope, cooldown, is_rate_limit)

    async def _record_redis(self, scope: str, cooldown: float,
                            is_rate_limit: bool) -> None:
        fk, ok = self._key_fail(scope), self._key_open(scope)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(fk)
            pipe.expire(fk, max(int(cooldown * 2), 60))
            res = await pipe.execute()
        n = int(res[0])
        if is_rate_limit or n >= self._threshold:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.setex(ok, max(1, int(cooldown)), "1")
                pipe.delete(fk)
                await pipe.execute()

    def _record_mem(self, scope: str, cooldown: float, is_rate_limit: bool) -> None:
        now = time.monotonic()
        st = self._states.setdefault(scope, _State())
        st.failures = [t for t in st.failures if now - t < self._window_s]
        st.failures.append(now)
        if is_rate_limit or len(st.failures) >= self._threshold:
            st.opened_until = now + cooldown
            st.failures.clear()
            logger.warning("circuit_open scope=%s cooldown_s=%.1f rate_limited=%s",
                           scope, cooldown, is_rate_limit)

    async def snapshot(self) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        async with self._lock:
            return {
                scope: {"open": st.opened_until > now,
                        "cooldown_remaining_s": max(0.0, st.opened_until - now),
                        "recent_failures": len([t for t in st.failures
                                                if now - t < self._window_s])}
                for scope, st in self._states.items()
            }


BREAKER = CircuitBreaker()


async def get_breaker() -> CircuitBreaker:
    return BREAKER


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Degradation ladder
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
    """Walk the ladder, first success wins. Never raises."""
    steps = [(n, f) for n, f in (
        ("full", full), ("reduced_ctx", reduced_ctx),
        ("reduced_tokens", reduced_tokens), ("cached_similar", cached_similar),
        ("local", local),
    ) if f is not None]

    for name, fn in steps:
        t0 = time.monotonic()
        try:
            result = await fn()
        except Exception as e:
            logger.warning("ladder_tier_failed tier=%s error=%s", name, e)
            continue
        if isinstance(result, dict) and result.get("success") and result.get("text"):
            tier = TIERS[name]
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
            "provider": "none", "model_used": "none", "tokens_used": 0,
            "latency_ms": 0.0, "tier": TIERS["stub"].name, "degraded": True}


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Hot-swap detector
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


_MAX_WAVE_DEPTH = 6


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

    async def race(self, prompt: str, *, max_tokens: int = 2048,
                   temp: float = 0.4,
                   candidate_providers: list[str] | None = None,
                   model_map: dict[str, list[str]] | None = None,
                   exclude: set[str] | None = None) -> HotSwapResult | None:
        exclude = exclude or set()
        candidates = candidate_providers or list(self._providers.keys())
        ranked = await self._rank(candidates, exclude)
        if not ranked:
            return None
        return await self._race_waves(ranked, model_map or {}, prompt, max_tokens, temp)

    async def _rank(self, candidates: list[str], exclude: set[str]) -> list[str]:
        async with self._lock:
            scored: list[tuple[float, str]] = []
            for p in candidates:
                if p in exclude or p not in self._providers:
                    continue
                scored.append((self._health.setdefault(p, _Health(provider=p)).score, p))
            scored.sort(key=lambda t: (-t[0], t[1]))
            return [p for _, p in scored]

    async def _race_waves(self, ranked: list[str], model_map: dict[str, list[str]],
                          prompt: str, max_tokens: int,
                          temp: float) -> HotSwapResult | None:
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
            logger.info("wave_failed_trying_next wave_idx=%d wave=%s remaining=%d",
                        wave_index, wave, len(ranked) - (start + width))
        return None

    async def _race_wave(self, wave: list[str], model_map: dict[str, list[str]],
                         prompt: str, max_tokens: int,
                         temp: float) -> HotSwapResult | None:
        if not wave:
            return None

        async def wrapped_attempt(p: str, model: str, func: ProviderFunc):
            t0 = time.monotonic()
            outcome = await self._attempt(p, model, func, prompt, max_tokens, temp)
            return (p, model, t0, outcome)

        tasks: list[asyncio.Task] = []
        for p in wave:
            func = self._providers.get(p)
            if not func:
                continue
            models = model_map.get(p) or []
            model = models[0] if models else "default-model"
            tasks.append(asyncio.create_task(
                wrapped_attempt(p, model, func), name=f"hotswap::{p}"))

        if not tasks:
            return None

        winner: HotSwapResult | None = None
        try:
            for fut in asyncio.as_completed(tasks, timeout=self._timeout_s + 5.0):
                try:
                    p, model, t0, outcome = await fut
                except Exception as e:
                    logger.debug("provider_task_failed error=%s", str(e)[:100])
                    continue
                if outcome is None:
                    continue
                await self._record(p, success=True, ttft_ms=outcome["ttft_ms"])
                winner = HotSwapResult(
                    provider=p, model=model, text=outcome["text"],
                    first_token_ms=outcome["ttft_ms"],
                    total_ms=(time.monotonic() - t0) * 1000.0,
                    attempts=len(tasks),
                )
                break
        except asyncio.TimeoutError:
            logger.debug("wave_timeout wave=%s timeout=%ss", wave, self._timeout_s + 5.0)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if winner is None:
            for t in tasks:
                if t.done() and not t.cancelled():
                    try:
                        p, _, _, _ = t.result()
                        await self._record(p, success=False, ttft_ms=0.0)
                    except Exception:
                        pass
        return winner

    async def _attempt(self, provider: str, model: str, func: ProviderFunc,
                       prompt: str, max_tokens: int,
                       temp: float) -> dict[str, Any] | None:
        t0 = time.monotonic()
        try:
            resp = await asyncio.wait_for(
                func(prompt, max_tokens, temp, model), timeout=self._timeout_s)
            if not resp or len(resp.strip()) <= 3:
                return None
            ttft = (time.monotonic() - t0) * 1000.0
            self._recent.append({"provider": provider, "model": model,
                                 "ttft_ms": round(ttft, 1), "ok": True,
                                 "t": time.time()})
            return {"text": resp, "ttft_ms": ttft}
        except (asyncio.TimeoutError, asyncio.CancelledError):
            raise
        except Exception as e:
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


# ═══════════════════════════════════════════════════════════════════════════
# §4 — WaveRacer (live p90 RTT tracker)
# ═══════════════════════════════════════════════════════════════════════════
_MAX_RTT_SAMPLES = 32
_MAX_PROVIDERS = 64
_SAFETY_MARGIN_MS = 15.0


class WaveRacer:
    """Tracks live p90 RTT per provider. Used for hedge timing."""
    __slots__ = ("_lock", "_rtt", "_last_fire_ts")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rtt: dict[str, deque[float]] = {}
        self._last_fire_ts: dict[str, float] = {}

    def record_rtt(self, provider: str, rtt_ms: float) -> None:
        if not provider or rtt_ms < 0 or rtt_ms > 60_000:
            return
        with self._lock:
            if len(self._rtt) >= _MAX_PROVIDERS and provider not in self._rtt:
                self._rtt.pop(next(iter(self._rtt)), None)
            d = self._rtt.setdefault(provider, deque(maxlen=_MAX_RTT_SAMPLES))
            d.append(float(rtt_ms))

    def record_ttft(self, provider: str) -> None:
        now = time.monotonic()
        with self._lock:
            prev = self._last_fire_ts.get(provider)
            self._last_fire_ts[provider] = now
        if prev is not None:
            self.record_rtt(provider, (now - prev) * 1000.0)

    def rtt_p90(self, provider: str) -> float:
        with self._lock:
            d = self._rtt.get(provider)
            samples = list(d) if d else []
        if len(samples) < 3:
            return 800.0
        samples.sort()
        idx = min(len(samples) - 1, int(0.9 * (len(samples) - 1)))
        return samples[idx]

    def rtt_mean(self, provider: str) -> float:
        """Mean RTT for a provider. Returns 800 ms default on no samples."""
        with self._lock:
            d = self._rtt.get(provider)
            samples = list(d) if d else []
        return statistics.fmean(samples) if samples else 800.0
        with self._lock:
            d = self._rtt.get(provider)
            samples = list(d) if d else []
        return statistics.fmean(samples) if samples else 800.0

    def next_wave_offset(self, timeout_ms: float,
                         backup_candidates: list[str]) -> float:
        if not backup_candidates:
            return max(0.0, timeout_ms - _SAFETY_MARGIN_MS)
        rtts = [self.rtt_p90(p) for p in backup_candidates]
        worst = max(rtts) if rtts else 800.0
        offset = timeout_ms - _SAFETY_MARGIN_MS - worst
        return max(0.0, min(timeout_ms - _SAFETY_MARGIN_MS, offset))

    def snapshot(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return {p: {"p90_ms": self.rtt_p90(p), "mean_ms": self.rtt_mean(p),
                        "samples": len(self._rtt.get(p, ()))}
                    for p in self._rtt}


RACER = WaveRacer()


def audit() -> dict[str, Any]:
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return {
                "breaker": {},
                "hotswap": get_hot_swap_detector().snapshot() if get_hot_swap_detector() else None,
                "racer": RACER.snapshot(),
            }
        else:
            return {
                "breaker": asyncio.get_event_loop().run_until_complete(BREAKER.snapshot()),
                "hotswap": get_hot_swap_detector().snapshot() if get_hot_swap_detector() else None,
                "racer": RACER.snapshot(),
            }
    except Exception:
        return {
            "breaker": {},
            "hotswap": get_hot_swap_detector().snapshot() if get_hot_swap_detector() else None,
            "racer": RACER.snapshot(),
        }


__all__ = [
    # Circuit breaker
    "CircuitBreaker", "BREAKER", "get_breaker",
    # Failover
    "Tier", "TIERS", "ladder_run",
    # Hot-swap
    "ProviderFunc", "HotSwapResult", "HotSwapDetector", "get_hot_swap_detector",
    # Wave racer
    "WaveRacer", "RACER",
    # Audit
    "audit",
]