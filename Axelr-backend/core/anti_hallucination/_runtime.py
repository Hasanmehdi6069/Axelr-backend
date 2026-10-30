"""Merged runtime services: audit, quota, streaming caps (v1.5).

Adds: CrossRequestCache, CircuitBreaker.
Preserves: merkle_root, crc32, conformance_score, bitemporal, QuotaTracker,
TokenCaps, PositionWatermark.
"""
from __future__ import annotations

import gc
import hashlib
import sys
import threading
import time
import zlib
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from .schemas import QuotaExceededError, SchemaViolationError, VerifiedPayload


def intern_str(s: str) -> str:
    return sys.intern(s) if isinstance(s, str) else s


def freeze_gc_after_warmup() -> int:
    gc.collect()
    return gc.freeze()


def _sha(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def merkle_root(leaves: Sequence[bytes]) -> str:
    if not leaves:
        return _sha(b"").hex()
    level = [_sha(l) for l in leaves]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [_sha(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
    return level[0].hex()


def crc32_payload(leaves: Sequence[bytes]) -> int:
    c = 0
    for l in leaves:
        c = zlib.crc32(l, c)
    return c & 0xFFFFFFFF


def conformance_score(*, verified: int, total: int, evidence_mean: float,
                      negation_clean_ratio: float, canon_ratio: float,
                      multi_doc_ratio: float) -> int:
    if total <= 0:
        return 0
    score = (
        (verified / total) * 40.0 +
        min(evidence_mean, 3) / 3 * 20.0 +
        negation_clean_ratio * 20.0 +
        canon_ratio * 10.0 +
        multi_doc_ratio * 10.0
    )
    return max(0, min(100, int(round(score))))


def bitemporal_stamp() -> tuple:
    now = datetime.now(timezone.utc)
    return now.isoformat(), str(int(now.timestamp() * 1_000_000))


def build_audit(payload: VerifiedPayload) -> tuple:
    leaves = []
    for c in sorted(
        payload.claims,
        key=lambda x: (x.field_name, x.coordinate.char_start,
                       x.coordinate.char_end, x.value),
    ):
        leaves.append(
            f"{c.field_name}\x00{c.value}\x00"
            f"{c.coordinate.document_id}\x00{c.coordinate.chunk_id}\x00"
            f"{c.coordinate.char_start}-{c.coordinate.char_end}".encode()
        )
    return merkle_root(leaves), crc32_payload(leaves)


class QuotaTracker:
    __slots__ = ("_lock", "_state")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict = {}

    def _current_month(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def check_and_consume(self, *, workspace: str, tier: str,
                          mb_used: float, max_queries: int, max_mb: int) -> None:
        with self._lock:
            k = (workspace, tier)
            st = self._state.setdefault(k, {"month": None, "q": 0, "mb_x100": 0})
            m = self._current_month()
            if st["month"] != m:
                st["month"] = m; st["q"] = 0; st["mb_x100"] = 0
            if max_queries >= 0 and st["q"] + 1 > max_queries:
                raise QuotaExceededError(
                    f"monthly_queries exceeded for {workspace}/{tier}",
                    {"limit": max_queries, "used": st["q"]},
                )
            mb_x100 = int(mb_used * 100)
            if max_mb >= 0 and st["mb_x100"] + mb_x100 > max_mb * 100:
                raise QuotaExceededError(
                    f"monthly_mb exceeded for {workspace}/{tier}",
                    {"limit": max_mb, "used_mb": st["mb_x100"] / 100},
                )
            st["q"] += 1
            st["mb_x100"] += mb_x100


class TokenCaps:
    __slots__ = ("_per_field", "_global")
    def __init__(self, per_field: int = 1024, global_: int = 8192) -> None:
        self._per_field = per_field
        self._global = global_
    def check_field(self, field: str, value: str) -> None:
        if len(value) > self._per_field:
            raise SchemaViolationError(
                f"field '{field}' value exceeds per_field cap",
                {"len": len(value), "cap": self._per_field},
            )
    def check_total(self, total: int) -> None:
        if total > self._global:
            raise SchemaViolationError(
                "total claim value exceeds global cap",
                {"total": total, "cap": self._global},
            )


class PositionWatermark:
    __slots__ = ("_last",)
    def __init__(self) -> None:
        self._last: dict = {}
    def advance(self, doc_id: str, chunk_id: str, start: int, end: int) -> None:
        k = (doc_id, chunk_id)
        prev = self._last.get(k, -1)
        if start < prev:
            raise SchemaViolationError(
                "position watermark regressed",
                {"doc": doc_id, "chunk": chunk_id, "prev": prev, "start": start},
            )
        self._last[k] = max(prev, end)


class CrossRequestCache:
    """Bounded thread-safe LRU. Shares canonicalize results across requests.
    Safe because canonicalize is pure. Caller supplies the compute function."""
    __slots__ = ("_lock", "_cache", "_size", "_hits", "_misses")

    def __init__(self, size: int = 4096) -> None:
        self._lock = threading.Lock()
        self._cache: "OrderedDict[str, object]" = OrderedDict()
        self._size = size
        self._hits = 0
        self._misses = 0

    def get_or_compute(self, key: str, compute: Callable[[], object]) -> object:
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                self._hits += 1
                return hit
        value = compute()
        with self._lock:
            self._cache[key] = value
            if len(self._cache) > self._size:
                self._cache.popitem(last=False)
            self._misses += 1
        return value

    def stats(self) -> dict:
        with self._lock:
            return {
                "size": len(self._cache),
                "capacity": self._size,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": (
                    self._hits / max(1, self._hits + self._misses)
                ),
            }


class CircuitBreaker:
    """Classic three-state breaker. Closed → open after N consecutive fails;
    half-open after cooldown; closed on success. Thread-safe."""
    __slots__ = ("_lock", "_threshold", "_cooldown_s",
                 "_consecutive_fails", "_opened_at")

    def __init__(self, threshold: int = 5, cooldown_s: float = 30.0) -> None:
        self._lock = threading.Lock()
        self._threshold = threshold
        self._cooldown_s = cooldown_s
        self._consecutive_fails = 0
        self._opened_at: Optional[float] = None

    def is_open(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return False
            if time.monotonic() - self._opened_at > self._cooldown_s:
                self._opened_at = None
                self._consecutive_fails = 0
                return False
            return True

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_fails = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_fails += 1
            if self._consecutive_fails >= self._threshold:
                self._opened_at = time.monotonic()

    def state(self) -> dict:
        with self._lock:
            return {
                "open": self._opened_at is not None,
                "consecutive_fails": self._consecutive_fails,
                "threshold": self._threshold,
                "cooldown_s": self._cooldown_s,
            }


__all__ = [
    "intern_str", "freeze_gc_after_warmup",
    "merkle_root", "crc32_payload", "conformance_score", "bitemporal_stamp",
    "build_audit", "QuotaTracker", "TokenCaps", "PositionWatermark",
    "CrossRequestCache", "CircuitBreaker",
]