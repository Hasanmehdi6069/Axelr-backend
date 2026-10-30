# core/meter.py
"""
AXELR — Unified Latency & Health Meter
========================================
Replaces five partially-overlapping trackers:
    wave_racer.WaveRacer        (RTT p90 per provider)
    failover._Health            (success rate + EWMA ttft)
    orchestration._TOKEN_RATIOS      (output/input ratio)
    twin_engine._LatencyTracker (per-model p95)
    heartbeat                   (aggregated provider health)

Bounded: <= 64 scopes, <= 128 providers per scope, 32 samples per (scope, provider).
Thread-safe. Never raises.
"""
from __future__ import annotations
import math
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Meter", "METER"]

_SAMPLE_RING = 32
_MAX_PROVIDERS_PER_SCOPE = 128
_MAX_SCOPES = 64
_EWMA_ALPHA = 0.3


@dataclass(slots=True)
class _Stat:
    successes: int = 0
    failures: int = 0
    total_latency_ms: float = 0.0
    ewma_latency_ms: float = 0.0
    last_ts: float = 0.0
    samples: deque[float] = field(default_factory=lambda: deque(maxlen=_SAMPLE_RING))

    def record(self, success: bool, latency_ms: float) -> None:
        self.last_ts = time.monotonic()
        if success:
            self.successes += 1
        else:
            self.failures += 1
        latency_ms = max(0.0, float(latency_ms))
        self.total_latency_ms += latency_ms
        self.samples.append(latency_ms)
        self.ewma_latency_ms = (
            latency_ms if self.ewma_latency_ms <= 0
            else _EWMA_ALPHA * latency_ms + (1 - _EWMA_ALPHA) * self.ewma_latency_ms
        )

    def success_rate(self) -> float:
        n = self.successes + self.failures
        return self.successes / n if n else 0.5

    def p(self, quantile: float) -> float:
        if not self.samples:
            return 0.0
        ordered = sorted(self.samples)
        idx = min(len(ordered) - 1, int(quantile * (len(ordered) - 1)))
        return ordered[idx]

    def health(self) -> float:
        """0.0–1.0. Weighted: 70% success rate, 30% inverse-EWMA-latency."""
        rate = self.success_rate()
        lat_penalty = 1.0 / (1.0 + self.ewma_latency_ms / 800.0)
        return round(0.7 * rate + 0.3 * lat_penalty, 4)


class Meter:
    __slots__ = ("_lock", "_scopes")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._scopes: OrderedDict[str, dict[str, _Stat]] = OrderedDict()

    def _scope(self, scope: str) -> dict[str, _Stat]:
        bucket = self._scopes.get(scope)
        if bucket is None:
            bucket = {}
            self._scopes[scope] = bucket
            if len(self._scopes) > _MAX_SCOPES:
                self._scopes.popitem(last=False)
        else:
            self._scopes.move_to_end(scope)
        return bucket

    def record(self, scope: str, provider: str, *, success: bool,
               latency_ms: float, ttft_ms: float | None = None) -> None:
        if not scope or not provider:
            return
        with self._lock:
            bucket = self._scope(scope)
            stat = bucket.get(provider)
            if stat is None:
                stat = _Stat()
                bucket[provider] = stat
                if len(bucket) > _MAX_PROVIDERS_PER_SCOPE:
                    bucket.pop(next(iter(bucket)))
            stat.record(success, latency_ms)
            if ttft_ms is not None and ttft_ms > 0:
                stat.record(success, ttft_ms)  # counts toward the same ring

    def p50(self, scope: str, provider: str) -> float:
        with self._lock:
            stat = self._scopes.get(scope, {}).get(provider)
            return stat.p(0.50) if stat else 0.0

    def p95(self, scope: str, provider: str) -> float:
        with self._lock:
            stat = self._scopes.get(scope, {}).get(provider)
            return stat.p(0.95) if stat else 0.0

    def health(self, scope: str, provider: str) -> float:
        with self._lock:
            stat = self._scopes.get(scope, {}).get(provider)
            return stat.health() if stat else 0.5

    def rank(self, scope: str, providers: list[str]) -> list[str]:
        """Stable sort by descending health, then ascending p95."""
        if not providers:
            return []
        with self._lock:
            bucket = self._scopes.get(scope, {})
            scored: list[tuple[float, float, str]] = []
            for p in providers:
                stat = bucket.get(p)
                h = stat.health() if stat else 0.5
                p95 = stat.p(0.95) if stat else 800.0
                scored.append((-h, p95, p))
        scored.sort()
        return [p for _, _, p in scored]

    def snapshot(self, scope: str | None = None) -> dict[str, Any]:
        with self._lock:
            if scope is not None:
                bucket = self._scopes.get(scope, {})
                return {"scope": scope,
                        "providers": {p: self._stat_dict(s) for p, s in bucket.items()}}
            out: dict[str, Any] = {}
            for scope_name, bucket in self._scopes.items():
                out[scope_name] = {p: self._stat_dict(s) for p, s in bucket.items()}
            return out

    @staticmethod
    def _stat_dict(s: _Stat) -> dict[str, float | int]:
        return {
            "successes": s.successes, "failures": s.failures,
            "success_rate": round(s.success_rate(), 3),
            "ewma_ms": round(s.ewma_latency_ms, 1),
            "p50_ms": round(s.p(0.50), 1),
            "p95_ms": round(s.p(0.95), 1),
            "health": s.health(),
            "samples": len(s.samples),
        }


METER = Meter()