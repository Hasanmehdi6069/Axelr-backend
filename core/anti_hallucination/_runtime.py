"""_runtime — runtime services + primitive re-exports.

Expanded from a shim into a substantive module: primitives remain
re-exported for backward compatibility, and runtime-specific services
(metrics, rate limit, memory budget, snapshot) live here.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .primitives import (  # noqa: F401 — re-exports
    build_audit, conformance_score, bitemporal_stamp,
    QuotaTracker, TokenCaps, intern_str, CircuitBreaker,
    CrossRequestCache, PositionWatermark, merkle_root, crc32_payload,
)


# ═══════════════════════════════════════════════════════════════════════════
# RuntimeMetrics — thread-safe counter/histogram
# ═══════════════════════════════════════════════════════════════════════════
class RuntimeMetrics:
    """Thread-safe counter + duration tracker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[str, int] = {}
        self._durations: Dict[str, List[float]] = {}
        self._gauges: Dict[str, float] = {}

    def incr(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + n

    def record_duration(self, key: str, seconds: float) -> None:
        with self._lock:
            self._durations.setdefault(key, []).append(seconds)
            if len(self._durations[key]) > 1000:
                self._durations[key] = self._durations[key][-500:]

    def set_gauge(self, key: str, value: float) -> None:
        with self._lock:
            self._gauges[key] = value

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            durations = {
                k: {
                    "count": len(v),
                    "mean": sum(v) / len(v) if v else 0.0,
                    "min": min(v) if v else 0.0,
                    "max": max(v) if v else 0.0,
                }
                for k, v in self._durations.items()
            }
            return {
                "counters": dict(self._counters),
                "durations": durations,
                "gauges": dict(self._gauges),
            }

    def prometheus(self) -> str:
        snap = self.snapshot()
        lines: List[str] = []
        for k, v in snap["counters"].items():
            safe = k.replace('"', '\\"')
            lines.append(f'ahe_counter{{key="{safe}"}} {v}')
        for k, d in snap["durations"].items():
            safe = k.replace('"', '\\"')
            lines.append(f'ahe_duration_seconds{{key="{safe}",agg="mean"}} {d["mean"]}')
        for k, v in snap["gauges"].items():
            safe = k.replace('"', '\\"')
            lines.append(f'ahe_gauge{{key="{safe}"}} {v}')
        return "\n".join(lines) + ("\n" if lines else "")


# ═══════════════════════════════════════════════════════════════════════════
# RateLimiter — token bucket
# ═══════════════════════════════════════════════════════════════════════════
class RateLimiter:
    """Token bucket. Thread-safe. Refills at `rate` tokens/sec up to `burst`."""

    def __init__(self, rate: float = 10.0, burst: int = 20) -> None:
        self.rate = rate
        self.burst = burst
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
        self._last = now

    def try_acquire(self, n: int = 1) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False

    def tokens(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens


# ═══════════════════════════════════════════════════════════════════════════
# MemoryBudget — soft budget tracker
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class MemoryBudget:
    """Track virtual memory consumption with soft cap + warning callback."""

    limit_bytes: int = 512 * 1024 * 1024
    warn_at: float = 0.8
    _used: int = field(default=0, repr=False)
    _warned: bool = field(default=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _on_warn: Optional[Callable[[int, int], None]] = field(default=None, repr=False)

    def set_callback(self, cb: Callable[[int, int], None]) -> None:
        self._on_warn = cb

    def reserve(self, n: int) -> bool:
        with self._lock:
            if self._used + n > self.limit_bytes:
                return False
            self._used += n
            if (not self._warned and self._on_warn is not None
                    and self._used / self.limit_bytes >= self.warn_at):
                self._warned = True
                try:
                    self._on_warn(self._used, self.limit_bytes)
                except Exception:  # noqa: BLE001
                    pass
            return True

    def release(self, n: int) -> None:
        with self._lock:
            self._used = max(0, self._used - n)

    def usage(self) -> float:
        with self._lock:
            return self._used / self.limit_bytes if self.limit_bytes else 0.0


# ═══════════════════════════════════════════════════════════════════════════
# RuntimeSnapshot — coherent point-in-time state
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class RuntimeSnapshot:
    engine_version: str
    tier_count: int
    active_requests: int
    queued_requests: int
    memory_used_bytes: int
    circuit_open: bool
    timestamp: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "engine_version": self.engine_version,
            "tier_count": self.tier_count,
            "active_requests": self.active_requests,
            "queued_requests": self.queued_requests,
            "memory_used_bytes": self.memory_used_bytes,
            "circuit_open": self.circuit_open,
            "timestamp": self.timestamp,
        }


# ═══════════════════════════════════════════════════════════════════════════
# RequestQueue — bounded FIFO
# ═══════════════════════════════════════════════════════════════════════════
class RequestQueue:
    """Bounded FIFO. Drops oldest on overflow (explicit, not silent)."""

    def __init__(self, max_size: int = 1000) -> None:
        from collections import deque
        self.max_size = max_size
        self._q: deque = deque(maxlen=max_size)
        self._lock = threading.Lock()
        self._dropped = 0

    def push(self, item: Any) -> None:
        with self._lock:
            if len(self._q) == self.max_size:
                self._dropped += 1
            self._q.append(item)

    def pop(self) -> Optional[Any]:
        with self._lock:
            return self._q.popleft() if self._q else None

    def size(self) -> int:
        with self._lock:
            return len(self._q)

    def dropped(self) -> int:
        with self._lock:
            return self._dropped


# ═══════════════════════════════════════════════════════════════════════════
# Health aggregation
# ═══════════════════════════════════════════════════════════════════════════
def aggregate_health(*components: Dict[str, Any]) -> Dict[str, Any]:
    """Merge multiple health reports; report overall healthy iff all healthy."""
    merged: Dict[str, Any] = {"healthy": True, "components": []}
    for comp in components:
        c_healthy = bool(comp.get("healthy", True))
        merged["components"].append({"healthy": c_healthy, **comp})
        merged["healthy"] = merged["healthy"] and c_healthy
    return merged


__all__ = [
    "build_audit", "conformance_score", "bitemporal_stamp",
    "QuotaTracker", "TokenCaps", "intern_str", "CircuitBreaker",
    "CrossRequestCache", "PositionWatermark", "merkle_root", "crc32_payload",
    # v1.7 additions
    "RuntimeMetrics", "RateLimiter", "MemoryBudget",
    "RuntimeSnapshot", "RequestQueue", "aggregate_health",
]