# core/heartbeat.py
"""
AXELR — Live Heartbeat Index
=============================
Single source of truth for "which provider is healthiest right now?"

Before this file existed, health state lived in four places:

    * core.hardening.RING                 — provider event ring (per ws/tier)
    * core.failover.HotSwapDetector       — per-provider EWMA score
    * core.infra.CircuitBreaker           — per-provider open/closed
    * core.gateway_router.GatewayEndpoint — per-endpoint EWMA + cooldown

Four health models means four ways to disagree, four ways to drift, and
no single number an operator can alert on. This module fuses them.

Public:
    record_success(provider, model, latency_ms)
    record_failure(provider, model, reason, is_rate_limit=False)
    rank(candidates) -> list[str]          # best first
    score(provider) -> float               # 0.0 (dead) .. 1.0 (perfect)
    snapshot() -> dict[str, dict]
    reset(provider=None) -> None
    install_bridge() -> bool               # subscribe to existing sources

Design:
    * In-process, bounded (max 128 providers, ring of 32 per provider).
    * Deterministic ordering for tests (ties broken alphabetically).
    * Zero deps beyond the stdlib. Never raises.

RAM: ~40 KB at 128 providers.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Iterable

logger = logging.getLogger("axelr.heartbeat")

_VERSION = "1.0"

_RING_SIZE = int(os.getenv("AXELR_HEARTBEAT_RING", "32"))
_MAX_PROVIDERS = int(os.getenv("AXELR_HEARTBEAT_MAX_PROVIDERS", "128"))
_DECAY_SECONDS = float(os.getenv("AXELR_HEARTBEAT_DECAY_S", "120.0"))
_COOLDOWN_BASE_S = float(os.getenv("AXELR_HEARTBEAT_COOLDOWN_S", "30.0"))
_COOLDOWN_MAX_S = float(os.getenv("AXELR_HEARTBEAT_COOLDOWN_MAX_S", "600.0"))

_W_SUCCESS = 0.55
_W_LATENCY = 0.25
_W_RECENCY = 0.20


@dataclass(slots=True)
class _Event:
    ts: float
    ok: bool
    latency_ms: float


@dataclass(slots=True)
class ProviderHealth:
    provider: str
    score: float = 0.5
    success_rate: float = 0.5
    mean_latency_ms: float = 0.0
    samples: int = 0
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    last_success_ts: float = 0.0
    last_failure_ts: float = 0.0
    last_failure_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "score": round(self.score, 3),
            "success_rate": round(self.success_rate, 3),
            "mean_latency_ms": round(self.mean_latency_ms, 1),
            "samples": self.samples,
            "consecutive_failures": self.consecutive_failures,
            "cooldown_remaining_s": round(
                max(0.0, self.cooldown_until - time.monotonic()), 1),
            "last_success_ts": self.last_success_ts,
            "last_failure_ts": self.last_failure_ts,
            "last_failure_reason": self.last_failure_reason,
        }


class _HeartbeatIndex:
    __slots__ = ("_lock", "_rings", "_state")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rings: dict[str, deque[_Event]] = {}
        self._state: dict[str, ProviderHealth] = {}

    def _bucket(self, provider: str) -> deque[_Event]:
        r = self._rings.get(provider)
        if r is None:
            if len(self._rings) >= _MAX_PROVIDERS:
                coldest = min(
                    self._rings.items(),
                    key=lambda kv: (len(kv[1]),
                                    -(kv[1][-1].ts if kv[1] else 0.0)),
                )[0]
                self._rings.pop(coldest, None)
                self._state.pop(coldest, None)
            r = deque(maxlen=_RING_SIZE)
            self._rings[provider] = r
        return r

    def record_success(self, provider: str, latency_ms: float) -> None:
        if not provider:
            return
        with self._lock:
            r = self._bucket(provider)
            r.append(_Event(time.monotonic(), True, max(0.0, float(latency_ms))))
            st = self._state.setdefault(provider,
                                        ProviderHealth(provider=provider))
            st.consecutive_failures = 0
            st.cooldown_until = 0.0
            st.last_success_ts = time.time()
            self._recompute(st, r)

    def record_failure(self, provider: str, reason: str = "",
                       *, is_rate_limit: bool = False) -> None:
        if not provider:
            return
        with self._lock:
            r = self._bucket(provider)
            r.append(_Event(time.monotonic(), False, 0.0))
            st = self._state.setdefault(provider,
                                        ProviderHealth(provider=provider))
            st.consecutive_failures += 1
            st.last_failure_ts = time.time()
            st.last_failure_reason = (reason or "")[:120]
            if is_rate_limit or st.consecutive_failures >= 3:
                exp = min(st.consecutive_failures, 6)
                base = _COOLDOWN_BASE_S * (4.0 if is_rate_limit else 1.0)
                st.cooldown_until = time.monotonic() + min(
                    base * (2 ** (exp - 1)), _COOLDOWN_MAX_S)
            self._recompute(st, r)

    @staticmethod
    def _recompute(st: ProviderHealth, r: deque[_Event]) -> None:
        if not r:
            st.score = 0.5
            st.success_rate = 0.5
            st.mean_latency_ms = 0.0
            st.samples = 0
            return

        now = time.monotonic()
        ok = sum(1 for e in r if e.ok)
        n = len(r)
        success_rate = ok / n if n else 0.5

        latencies = [e.latency_ms for e in r if e.ok and e.latency_ms > 0.0]
        mean_lat = (sum(latencies) / len(latencies)) if latencies else 0.0

        most_recent = r[-1].ts
        age_s = max(0.0, now - most_recent)
        recency = (math.exp(-age_s / _DECAY_SECONDS)
                   if _DECAY_SECONDS > 0 else 1.0)

        if mean_lat <= 0.0:
            latency_score = 1.0
        else:
            latency_score = max(
                0.0, 1.0 - math.log1p(mean_lat / 250.0) / math.log(13.0))

        score = (_W_SUCCESS * success_rate
                 + _W_LATENCY * latency_score
                 + _W_RECENCY * recency)
        score -= min(0.45, 0.15 * st.consecutive_failures)
        if now < st.cooldown_until:
            score = 0.0

        st.score = max(0.0, min(1.0, score))
        st.success_rate = success_rate
        st.mean_latency_ms = mean_lat
        st.samples = n

    def rank(self, candidates: Iterable[str]) -> list[str]:
        cands = list(candidates)
        if not cands:
            return []
        with self._lock:
            now = time.monotonic()
            keyed: list[tuple[float, str]] = []
            for p in cands:
                st = self._state.get(p)
                if st is None:
                    keyed.append((0.5, p))
                    continue
                if now < st.cooldown_until:
                    keyed.append((-1.0, p))
                    continue
                keyed.append((st.score, p))
        keyed.sort(key=lambda kv: (-kv[0], kv[1]))
        return [p for _, p in keyed]

    def score(self, provider: str) -> float:
        with self._lock:
            st = self._state.get(provider)
            if st is None:
                return 0.5
            if time.monotonic() < st.cooldown_until:
                return 0.0
            return st.score

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {p: st.to_dict() for p, st in sorted(self._state.items())}

    def reset(self, provider: str | None = None) -> None:
        with self._lock:
            if provider is None:
                self._rings.clear()
                self._state.clear()
            else:
                self._rings.pop(provider, None)
                self._state.pop(provider, None)


HEARTBEAT = _HeartbeatIndex()


def record_success(provider: str, model: str = "",
                   latency_ms: float = 0.0) -> None:
    HEARTBEAT.record_success(provider, latency_ms)


def record_failure(provider: str, model: str = "", reason: str = "",
                   *, is_rate_limit: bool = False) -> None:
    HEARTBEAT.record_failure(provider, reason, is_rate_limit=is_rate_limit)


def rank(candidates: Iterable[str]) -> list[str]:
    return HEARTBEAT.rank(candidates)


def score(provider: str) -> float:
    return HEARTBEAT.score(provider)


def snapshot() -> dict[str, dict]:
    return HEARTBEAT.snapshot()


def reset(provider: str | None = None) -> None:
    HEARTBEAT.reset(provider)


_BRIDGE_INSTALLED = False


def install_bridge() -> bool:
    """Subscribe to existing health sources. Idempotent. Never raises."""
    global _BRIDGE_INSTALLED
    if _BRIDGE_INSTALLED:
        return True

    ok_any = False

    try:
        from . import failover as _fo
        if not getattr(_fo.HotSwapDetector._record, "_axelr_hb_bridge", False):
            original_record = _fo.HotSwapDetector._record

            async def _record(self, provider, *, success, ttft_ms):
                await original_record(self, provider,
                                      success=success, ttft_ms=ttft_ms)
                if success:
                    HEARTBEAT.record_success(provider, ttft_ms)
                else:
                    HEARTBEAT.record_failure(provider, "hotswap_failure")

            _record._axelr_hb_bridge = True
            _record._axelr_original = original_record
            _fo.HotSwapDetector._record = _record
            ok_any = True
    except Exception as e:
        logger.debug("heartbeat_bridge_failover_failed error=%s", e)

    try:
        from . import infra as _inf
        if not getattr(_inf.CircuitBreaker.record_failure,
                       "_axelr_hb_bridge", False):
            cb_fail_orig = _inf.CircuitBreaker.record_failure
            cb_ok_orig = _inf.CircuitBreaker.record_success

            async def _cb_fail(self, provider, *, is_rate_limit=False):
                await cb_fail_orig(self, provider,
                                   is_rate_limit=is_rate_limit)
                HEARTBEAT.record_failure(provider, "circuit_breaker",
                                         is_rate_limit=is_rate_limit)

            async def _cb_ok(self, provider):
                await cb_ok_orig(self, provider)
                HEARTBEAT.record_success(provider, 0.0)

            _cb_fail._axelr_hb_bridge = True
            _cb_fail._axelr_original = cb_fail_orig
            _cb_ok._axelr_hb_bridge = True
            _cb_ok._axelr_original = cb_ok_orig
            _inf.CircuitBreaker.record_failure = _cb_fail
            _inf.CircuitBreaker.record_success = _cb_ok
            ok_any = True
    except Exception as e:
        logger.debug("heartbeat_bridge_cb_failed error=%s", e)

    try:
        from . import gateway_router as _gw
        if not getattr(_gw.GatewayRouter._mark_success,
                       "_axelr_hb_bridge", False):
            gws_orig = _gw.GatewayRouter._mark_success
            gwf_orig = _gw.GatewayRouter._mark_failure

            async def _gw_success(self, ep, latency_ms):
                await gws_orig(self, ep, latency_ms)
                HEARTBEAT.record_success(f"gateway:{ep.name}", latency_ms)

            async def _gw_failure(self, ep, rate_limited, latency_ms=0.0):
                await gwf_orig(self, ep, rate_limited, latency_ms=latency_ms)
                HEARTBEAT.record_failure(f"gateway:{ep.name}",
                                         "gateway_failure",
                                         is_rate_limit=rate_limited)

            _gw_success._axelr_hb_bridge = True
            _gw_success._axelr_original = gws_orig
            _gw_failure._axelr_hb_bridge = True
            _gw_failure._axelr_original = gwf_orig
            _gw.GatewayRouter._mark_success = _gw_success
            _gw.GatewayRouter._mark_failure = _gw_failure
            ok_any = True
    except Exception as e:
        logger.debug("heartbeat_bridge_gateway_failed error=%s", e)

    _BRIDGE_INSTALLED = ok_any
    if ok_any:
        logger.info("heartbeat_index_bridge_installed")
    else:
        logger.warning("heartbeat_index_bridge_partial_or_unavailable")
    return ok_any


def audit() -> dict:
    with HEARTBEAT._lock:                       # noqa: SLF001
        n = len(HEARTBEAT._state)               # noqa: SLF001
        ring_n = len(HEARTBEAT._rings)          # noqa: SLF001
    return {
        "version": _VERSION,
        "providers_tracked": n,
        "rings_allocated": ring_n,
        "ring_size": _RING_SIZE,
        "bridge_installed": _BRIDGE_INSTALLED,
        "decay_s": _DECAY_SECONDS,
    }


__all__ = [
    "ProviderHealth", "HEARTBEAT",
    "record_success", "record_failure",
    "rank", "score", "snapshot", "reset",
    "install_bridge", "audit",
]