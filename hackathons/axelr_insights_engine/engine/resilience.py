"""axelr-insights-engine — Resilience primitives.

Every primitive is async-by-default, single-threaded-safe (Python's GIL
makes async code cooperative), and independently testable.

Classes:
    TokenBucket          — rate limiting
    CircuitBreaker       — failure isolation (closed/open/half_open)
    Semaphore            — concurrency cap
    LocalCache           — LRU + TTL
    RequestDeduplicator  — coalesce in-flight calls
    MetricsCollector     — counters + histograms + gauges
    StructuredLogger     — JSON logs with correlation IDs
    RetryPolicy          — frozen retry recipe

Utilities:
    backoff_delay()      — exponential backoff with cap
    jittered_sleep()     — sleep with ±jitter
    generate_corr_id()   — correlation id
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import sys
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

# ═══════════════════════════════════════════════════════════════════════
# Token bucket
# ═══════════════════════════════════════════════════════════════════════


class TokenBucket:
    """Async token bucket. Thread-safe under asyncio.

    Capacity = burst limit. Refill rate = sustained rate.
    `acquire()` blocks until enough tokens are available.
    """

    def __init__(self, rate_per_sec: float, capacity: int) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        self._rate = rate_per_sec
        self._cap = max(1, capacity)
        self._tokens = float(self._cap)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: float = 1.0) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(self._cap, self._tokens + elapsed * self._rate)
            self._last = now

            if self._tokens >= cost:
                self._tokens -= cost
                return

            needed = cost - self._tokens
            wait_s = needed / self._rate
            self._tokens = 0.0

        # Sleep outside the lock so other callers can refill
        await asyncio.sleep(wait_s)

        async with self._lock:
            self._last = time.monotonic()

    @property
    def available(self) -> float:
        return self._tokens


# ═══════════════════════════════════════════════════════════════════════
# Circuit breaker
# ═══════════════════════════════════════════════════════════════════════


class CBState:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Nygard-style circuit breaker.

    closed  → calls flow, `failures` counts consecutive failures
    open    → calls fail fast, `opened_at` marks trip time
    half_open → after cooldown, next `successes_needed` calls are probes
    """

    def __init__(
        self,
        *,
        threshold: int = 10,
        cooldown_s: float = 60.0,
        successes_needed: int = 2,
    ) -> None:
        self._threshold = max(1, threshold)
        self._cooldown_s = max(0.0, cooldown_s)
        self._successes_needed = max(1, successes_needed)
        self._failures = 0
        self._opened_at: float | None = None
        self._half_open_successes = 0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return CBState.CLOSED
        if time.monotonic() - self._opened_at >= self._cooldown_s:
            return CBState.HALF_OPEN
        return CBState.OPEN

    async def allow(self) -> bool:
        async with self._lock:
            return self._state_locked() != CBState.OPEN

    async def record_success(self) -> None:
        async with self._lock:
            if self._state_locked() == CBState.HALF_OPEN:
                self._half_open_successes += 1
                if self._half_open_successes >= self._successes_needed:
                    self._opened_at = None
                    self._failures = 0
                    self._half_open_successes = 0
            else:
                self._failures = 0

    async def record_failure(self) -> None:
        async with self._lock:
            if self._state_locked() == CBState.HALF_OPEN:
                self._opened_at = time.monotonic()
                self._half_open_successes = 0
                return
            self._failures += 1
            if self._failures >= self._threshold and self._opened_at is None:
                self._opened_at = time.monotonic()

    async def reset(self) -> None:
        async with self._lock:
            self._failures = 0
            self._opened_at = None
            self._half_open_successes = 0

    def _state_locked(self) -> str:
        if self._opened_at is None:
            return CBState.CLOSED
        if time.monotonic() - self._opened_at >= self._cooldown_s:
            return CBState.HALF_OPEN
        return CBState.OPEN

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "failures": self._failures,
            "opened_at": self._opened_at,
            "cooldown_s": self._cooldown_s,
            "threshold": self._threshold,
        }


# ═══════════════════════════════════════════════════════════════════════
# Semaphore
# ═══════════════════════════════════════════════════════════════════════


class Semaphore:
    """Async semaphore — caps in-flight SDK calls."""

    def __init__(self, max_concurrent: int) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self._sem = asyncio.Semaphore(max_concurrent)
        self._max = max_concurrent
        self._active = 0

    async def acquire(self) -> None:
        await self._sem.acquire()
        self._active += 1

    def release(self) -> None:
        self._active = max(0, self._active - 1)
        self._sem.release()

    @property
    def active(self) -> int:
        return self._active

    @property
    def capacity(self) -> int:
        return self._max


# ═══════════════════════════════════════════════════════════════════════
# Local cache (LRU + TTL)
# ═══════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class _CacheEntry:
    value: Any
    cached_at: float


class LocalCache:
    """Thread-safe (via asyncio) LRU + TTL cache."""

    def __init__(self, *, max_entries: int = 1000, ttl_s: float = 300.0) -> None:
        self._max = max(1, max_entries)
        self._ttl = max(0.0, ttl_s)
        self._store: OrderedDict[str, _CacheEntry] = OrderedDict()

    def _key(self, parts: tuple[Any, ...]) -> str:
        return ":".join(str(p) for p in parts)

    def get(self, parts: tuple[Any, ...]) -> Any | None:
        k = self._key(parts)
        entry = self._store.get(k)
        if entry is None:
            return None
        if self._ttl > 0 and time.monotonic() - entry.cached_at > self._ttl:
            self._store.pop(k, None)
            return None
        self._store.move_to_end(k)
        return entry.value

    def set(self, parts: tuple[Any, ...], value: Any) -> None:
        k = self._key(parts)
        if k in self._store:
            self._store.move_to_end(k)
        elif len(self._store) >= self._max:
            self._store.popitem(last=False)
        self._store[k] = _CacheEntry(value=value, cached_at=time.monotonic())

    def delete(self, parts: tuple[Any, ...]) -> None:
        self._store.pop(self._key(parts), None)

    def clear(self) -> None:
        self._store.clear()

    @property
    def size(self) -> int:
        return len(self._store)


# ═══════════════════════════════════════════════════════════════════════
# Request deduplicator
# ═══════════════════════════════════════════════════════════════════════


class RequestDeduplicator:
    """Coalesce identical in-flight async calls."""

    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Task[Any]] = {}

    async def run(self, key: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        existing = self._inflight.get(key)
        if existing is not None:
            return await existing
        task = asyncio.ensure_future(fn())
        self._inflight[key] = task
        try:
            return await task
        finally:
            self._inflight.pop(key, None)

    @property
    def size(self) -> int:
        return len(self._inflight)

    def clear(self) -> None:
        self._inflight.clear()


# ═══════════════════════════════════════════════════════════════════════
# Metrics collector
# ═══════════════════════════════════════════════════════════════════════


class MetricsCollector:
    """Counters, histograms, gauges — thread-safe within asyncio."""

    MAX_HIST = 10_000

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._histograms: dict[str, list[float]] = {}
        self._gauges: dict[str, float] = {}

    def inc(self, name: str, value: int = 1) -> None:
        self._counters[name] = self._counters.get(name, 0) + value

    def observe(self, name: str, value: float) -> None:
        if not math.isfinite(value):
            return
        bucket = self._histograms.setdefault(name, [])
        bucket.append(float(value))
        if len(bucket) > self.MAX_HIST:
            del bucket[: len(bucket) - self.MAX_HIST]

    def gauge(self, name: str, value: float) -> None:
        self._gauges[name] = float(value)

    def snapshot(self) -> dict[str, Any]:
        hist_out: dict[str, dict[str, Any]] = {}
        for name, values in self._histograms.items():
            if not values:
                hist_out[name] = {"count": 0}
                continue
            sorted_v = sorted(values)
            n = len(sorted_v)

            def pct(p: float) -> float:
                idx = min(n - 1, int(round(p * (n - 1))))
                return sorted_v[idx]

            hist_out[name] = {
                "count": n,
                "min": sorted_v[0],
                "max": sorted_v[-1],
                "mean": round(sum(sorted_v) / n, 4),
                "p50": round(pct(0.50), 4),
                "p95": round(pct(0.95), 4),
                "p99": round(pct(0.99), 4),
            }
        return {
            "counters": dict(self._counters),
            "gauges": dict(self._gauges),
            "histograms": hist_out,
            "timestamp": time.time(),
        }

    def reset(self) -> None:
        self._counters.clear()
        self._histograms.clear()
        self._gauges.clear()


# ═══════════════════════════════════════════════════════════════════════
# Structured logger
# ═══════════════════════════════════════════════════════════════════════

_LOG_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}


class StructuredLogger:
    """JSON-line logger with correlation IDs. Never leaks secrets."""

    def __init__(self, *, level: str = "info", context: dict[str, Any] | None = None) -> None:
        self._level = _LOG_LEVELS.get(level.lower(), 20)
        self._context = dict(context or {})

    def with_(self, **fields: Any) -> "StructuredLogger":
        child = StructuredLogger.__new__(StructuredLogger)
        child._level = self._level
        child._context = {**self._context, **fields}
        return child

    def _emit(self, level: str, msg: str, fields: dict[str, Any]) -> None:
        if _LOG_LEVELS[level] < self._level:
            return
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "level": level,
            "msg": msg,
            **self._context,
            **fields,
        }
        try:
            line = json.dumps(payload, default=str)
        except Exception:
            line = json.dumps({"ts": payload["ts"], "level": level, "msg": msg})
        stream = sys.stderr if level in ("warn", "error") else sys.stdout
        print(line, file=stream, flush=True)

    def debug(self, msg: str, **fields: Any) -> None:
        self._emit("debug", msg, fields)

    def info(self, msg: str, **fields: Any) -> None:
        self._emit("info", msg, fields)

    def warn(self, msg: str, **fields: Any) -> None:
        self._emit("warn", msg, fields)

    def error(self, msg: str, **fields: Any) -> None:
        self._emit("error", msg, fields)


# ═══════════════════════════════════════════════════════════════════════
# Retry policy
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Frozen retry recipe."""

    max_attempts: int = 3
    base_delay_s: float = 0.15
    max_delay_s: float = 10.0
    jitter: float = 0.15

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay_s < 0:
            raise ValueError("base_delay_s must be >= 0")
        if self.max_delay_s < self.base_delay_s:
            raise ValueError("max_delay_s must be >= base_delay_s")

    def delay_for(self, attempt: int) -> float:
        """Delay for the Nth retry (1-indexed attempt is the second try)."""
        exp = min(self.max_delay_s, self.base_delay_s * (2 ** (attempt - 1)))
        if self.jitter > 0:
            exp *= 1.0 + random.uniform(-self.jitter, self.jitter)
        return max(0.0, exp)


# ═══════════════════════════════════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════════════════════════════════


def backoff_delay(attempt: int, base_ms: float, max_ms: float) -> float:
    """Milliseconds of backoff for `attempt` (0-indexed)."""
    return min(max_ms, base_ms * (2**attempt))


async def jittered_sleep(base_s: float, jitter: float = 0.15) -> None:
    """Sleep with ±jitter to avoid thundering herd."""
    factor = 1.0 + random.uniform(-jitter, jitter)
    await asyncio.sleep(max(0.0, base_s * factor))


def generate_corr_id() -> str:
    """Short correlation ID — 16 chars, collision-resistant."""
    return uuid.uuid4().hex[:16]


# ═══════════════════════════════════════════════════════════════════════
# Idempotency store (merged from idempotency.py)
# ═══════════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class _IdemRecord:
    payload_hash: str
    result: dict[str, Any]
    stored_at: float


class IdempotencyStore:
    """LRU store with hard TTL. Never evicts live records under pressure."""

    def __init__(self, *, max_entries: int = 10_000, ttl_s: float = 86_400.0) -> None:
        self._max = max(1, max_entries)
        self._ttl = max(0.0, ttl_s)
        self._store: OrderedDict[str, _IdemRecord] = OrderedDict()

    def _key(self, table: str, key: str) -> str:
        return f"{table}::{key}"

    @staticmethod
    def hash_payload(payload: Any) -> str:
        import hashlib

        raw = json.dumps(payload, default=str, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()

    def _expire(self) -> None:
        if self._ttl <= 0:
            return
        now = time.monotonic()
        for k in list(self._store.keys()):
            if now - self._store[k].stored_at > self._ttl:
                del self._store[k]
            else:
                break

    def lookup(self, table: str, key: str, payload: Any) -> dict[str, Any] | None:
        self._expire()
        k = self._key(table, key)
        entry = self._store.get(k)
        if entry is None:
            return None
        if entry.payload_hash != self.hash_payload(payload):
            raise IdempotencyConflictError(
                f"idempotency key {key!r} reused for table {table!r} with a different payload"
            )
        self._store.move_to_end(k)
        return entry.result

    def store(self, table: str, key: str, payload: Any, result: dict[str, Any]) -> None:
        k = self._key(table, key)
        if k in self._store:
            self._store.move_to_end(k)
        elif len(self._store) >= self._max:
            oldest = next(iter(self._store))
            del self._store[oldest]
        self._store[k] = _IdemRecord(payload_hash=self.hash_payload(payload), result=result, stored_at=time.monotonic())

    def clear(self) -> None:
        self._store.clear()

    @property
    def size(self) -> int:
        return len(self._store)


# ═══════════════════════════════════════════════════════════════════════
# Observability hooks (merged from hooks.py)
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class CallInfo:
    operation: str
    corr_id: str
    attempt: int
    duration_ms: float
    success: bool
    error: str | None = None
    retryable: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


Hook = Callable[[CallInfo], Any | Awaitable[Any]]


class HookRegistry:
    """Registry of named hook callbacks. Failures in hooks are swallowed."""

    def __init__(self) -> None:
        self._hooks: dict[str, list[Hook]] = {}

    def register(self, name: str, fn: Hook) -> None:
        self._hooks.setdefault(name, []).append(fn)

    def unregister(self, name: str, fn: Hook) -> None:
        lst = self._hooks.get(name)
        if lst and fn in lst:
            lst.remove(fn)

    def clear(self) -> None:
        self._hooks.clear()

    async def emit(self, name: str, info: CallInfo) -> None:
        import inspect

        for fn in list(self._hooks.get(name, [])):
            try:
                r = fn(info)
                if inspect.isawaitable(r):
                    await r
            except Exception as e:
                import logging

                logging.getLogger("axelr_insights_engine.hooks").warning(
                    "hook_error name=%s err=%s", name, str(e)[:160]
                )


# ═══════════════════════════════════════════════════════════════════════
# Utility: stable hash for dedup keys
# ═══════════════════════════════════════════════════════════════════════


def stable_hash(obj: Any) -> int:
    """Stable cross-process hash for dedup keys. Uses sha256, NOT hash()."""
    import hashlib

    raw = json.dumps(obj, default=str, sort_keys=True, separators=(",", ":")).encode()
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")
