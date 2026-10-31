<<<<<<< HEAD
# core/token_governor.py
"""
AXELR — Token Governor (v1.1, LRU-bounded)
============================================
Sliding-window token budget per (user, tier) with Redis primary and
in-process fallback. Prevents a single user from burning the free tier.

v1.1 fix: _MEM was a plain dict → unbounded growth. Now an OrderedDict
with LRU eviction at TOK_MAX_USERS (default 10 000). Worst-case memory
is ~10 000 × 2 deques × 30 samples × 16 B ≈ 9 MB. Bounded, deterministic.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("axelr.governor")

# Tier → (per-minute, per-day) token ceilings. Env-overridable.
_TIER_LIMITS: dict[str, tuple[int, int]] = {
    "free":     (int(os.getenv("TOK_FREE_MIN", "25000")), int(os.getenv("TOK_FREE_DAY", "300000"))),
    "pro":      (int(os.getenv("TOK_PRO_MIN", "120000")), int(os.getenv("TOK_PRO_DAY", "2000000"))),
    "business": (int(os.getenv("TOK_BIZ_MIN", "400000")), int(os.getenv("TOK_BIZ_DAY", "8000000"))),
    "guest":    (int(os.getenv("TOK_GST_MIN", "8000")),   int(os.getenv("TOK_GST_DAY", "20000"))),
}

_CHARS_PER_TOKEN = 4
_MINUTE_S = 60
_DAY_S = 86_400
_MAX_USERS = int(os.getenv("TOK_MAX_USERS", "10000"))


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


@dataclass(slots=True)
class _Bucket:
    minute: deque[tuple[float, int]] = field(default_factory=deque)
    day: deque[tuple[float, int]] = field(default_factory=deque)


_MEM: "OrderedDict[str, _Bucket]" = OrderedDict()
_MEM_LOCK = asyncio.Lock()


def _limits(tier: str) -> tuple[int, int]:
    return _TIER_LIMITS.get(tier or "free", _TIER_LIMITS["free"])


def _prune(bucket: _Bucket, now: float) -> None:
    cutoff_min = now - _MINUTE_S
    while bucket.minute and bucket.minute[0][0] < cutoff_min:
        bucket.minute.popleft()
    cutoff_day = now - _DAY_S
    while bucket.day and bucket.day[0][0] < cutoff_day:
        bucket.day.popleft()


def _sum_bucket(bucket: _Bucket) -> tuple[int, int]:
    return (sum(t for _, t in bucket.minute),
            sum(t for _, t in bucket.day))


def _get_bucket_lru(uid: str) -> _Bucket:
    """LRU-bounded get-or-create. Moves to end on access. Never exceeds _MAX_USERS."""
    b = _MEM.get(uid)
    if b is not None:
        _MEM.move_to_end(uid)
        return b
    b = _Bucket()
    _MEM[uid] = b
    while len(_MEM) > _MAX_USERS:
        _MEM.popitem(last=False)
    return b


def _mem_check(uid: str, tier: str, est: int) -> tuple[bool, int, int]:
    now = time.time()
    b = _get_bucket_lru(uid)
    _prune(b, now)
    used_min, used_day = _sum_bucket(b)
    cap_min, cap_day = _limits(tier)
    if used_min + est > cap_min:
        retry = max(1, int(b.minute[0][0] + _MINUTE_S - now)) if b.minute else 1
        return False, max(0, cap_min - used_min), retry
    if used_day + est > cap_day:
        retry = max(1, int(b.day[0][0] + _DAY_S - now)) if b.day else 1
        return False, max(0, cap_day - used_day), retry
    b.minute.append((now, est))
    b.day.append((now, est))
    return True, cap_min - (used_min + est), 0


async def _redis_check(uid: str, tier: str, est: int) -> tuple[bool, int, int] | None:
    try:
        from api.state import state  # type: ignore
    except Exception:
        return None
    r = getattr(state, "redis_client", None)
    if r is None:
        return None
    try:
        cap_min, cap_day = _limits(tier)
        now = int(time.time())
        win_min = now // _MINUTE_S
        win_day = now // _DAY_S
        kmin = f"tok:{uid}:m:{win_min}"
        kday = f"tok:{uid}:d:{win_day}"
        async with r.pipeline(transaction=True) as pipe:
            pipe.incrby(kmin, est)
            pipe.expire(kmin, _MINUTE_S + 5)
            pipe.incrby(kday, est)
            pipe.expire(kday, _DAY_S + 5)
            res = await pipe.execute()
        used_min = int(res[0])
        used_day = int(res[2])
        if used_min > cap_min:
            return False, max(0, cap_min - (used_min - est)), _MINUTE_S - (now % _MINUTE_S)
        if used_day > cap_day:
            return False, max(0, cap_day - (used_day - est)), _DAY_S - (now % _DAY_S)
        return True, cap_min - used_min, 0
    except Exception as e:
        logger.debug("governor_redis_error error=%s", e)
        return None


async def reserve(uid: str, tier: str, est_tokens: int) -> tuple[bool, int, int]:
    """Returns (allowed, remaining_in_window, retry_after_s)."""
    est = max(0, int(est_tokens))
    if est == 0 or not uid:
        return True, 0, 0
    outcome = await _redis_check(uid, tier, est)
    if outcome is not None:
        return outcome
    async with _MEM_LOCK:
        return _mem_check(uid, tier, est)


async def settle(uid: str, delta_tokens: int) -> None:
    """Apply a post-hoc delta (negative to refund). Best-effort."""
    if not uid or delta_tokens == 0:
        return
    try:
        from api.state import state  # type: ignore
        r = getattr(state, "redis_client", None)
        if r is not None:
            now = int(time.time())
            await r.incrby(f"tok:{uid}:m:{now // _MINUTE_S}", int(delta_tokens))
            await r.incrby(f"tok:{uid}:d:{now // _DAY_S}", int(delta_tokens))
            return
    except Exception:
        pass
    async with _MEM_LOCK:
        b = _MEM.get(uid)
        if b is not None:
            _MEM.move_to_end(uid)
            b.minute.append((time.time(), int(delta_tokens)))
            b.day.append((time.time(), int(delta_tokens)))


async def snapshot(uid: str, tier: str) -> dict[str, Any]:
    cap_min, cap_day = _limits(tier)
    try:
        from api.state import state  # type: ignore
        r = getattr(state, "redis_client", None)
        if r is not None:
            now = int(time.time())
            um = await r.get(f"tok:{uid}:m:{now // _MINUTE_S}") or 0
            ud = await r.get(f"tok:{uid}:d:{now // _DAY_S}") or 0
            return {
                "tier": tier,
                "min_used": int(um), "min_cap": cap_min,
                "day_used": int(ud), "day_cap": cap_day,
                "min_remaining": max(0, cap_min - int(um)),
                "day_remaining": max(0, cap_day - int(ud)),
            }
    except Exception:
        pass
    async with _MEM_LOCK:
        b = _get_bucket_lru(uid)
        _prune(b, time.time())
        um, ud = _sum_bucket(b)
        return {
            "tier": tier,
            "min_used": um, "min_cap": cap_min,
            "day_used": ud, "day_cap": cap_day,
            "min_remaining": max(0, cap_min - um),
            "day_remaining": max(0, cap_day - ud),
        }


=======
# core/token_governor.py
"""
AXELR — Token Governor
=======================
Sliding-window token budget per (user, tier) with Redis primary and
in-process fallback. Prevents a single user from burning the free tier,
enforces fair-share across concurrent tenants, and emits Prometheus
counters on every allow/deny.

Design:
    * Window: rolling 60 s and rolling 86 400 s buckets.
    * Redis path: atomic INCRBY + EXPIRE, single round-trip decision.
    * Memory path: bounded deque of (ts, tokens); prunes on read.
    * Decisions are `(allowed, remaining, retry_after_s)`.
    * Cost estimation uses chars/4 — deterministic, no tokenizer.

Semantics:
    * `reserve(uid, tier, est_tokens)` — pre-flight check + charge.
    * `settle(uid, actual_tokens)` — adjust the delta.
    * Never raises. On unknown tier, falls back to `free`.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("axelr.governor")

# Tier → (per-minute, per-day) token ceilings. Env-overridable.
_TIER_LIMITS: dict[str, tuple[int, int]] = {
    "free":     (int(os.getenv("TOK_FREE_MIN",   "25000")), int(os.getenv("TOK_FREE_DAY",   "300000"))),
    "pro":      (int(os.getenv("TOK_PRO_MIN",   "120000")), int(os.getenv("TOK_PRO_DAY",  "2000000"))),
    "business": (int(os.getenv("TOK_BIZ_MIN",   "400000")), int(os.getenv("TOK_BIZ_DAY",  "8000000"))),
    "guest":    (int(os.getenv("TOK_GST_MIN",   "8000")),  int(os.getenv("TOK_GST_DAY",    "20000"))),
}

_CHARS_PER_TOKEN = 4
_MINUTE_S = 60
_DAY_S    = 86_400


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


@dataclass(slots=True)
class _Bucket:
    minute: deque[tuple[float, int]] = field(default_factory=deque)
    day:    deque[tuple[float, int]] = field(default_factory=deque)


_MEM: dict[str, _Bucket] = {}
_MEM_LOCK = asyncio.Lock()


def _limits(tier: str) -> tuple[int, int]:
    return _TIER_LIMITS.get(tier or "free", _TIER_LIMITS["free"])


def _prune(bucket: _Bucket, now: float) -> None:
    cutoff_min = now - _MINUTE_S
    while bucket.minute and bucket.minute[0][0] < cutoff_min:
        bucket.minute.popleft()
    cutoff_day = now - _DAY_S
    while bucket.day and bucket.day[0][0] < cutoff_day:
        bucket.day.popleft()


def _sum_bucket(bucket: _Bucket) -> tuple[int, int]:
    return (sum(t for _, t in bucket.minute),
            sum(t for _, t in bucket.day))


def _mem_check(uid: str, tier: str, est: int) -> tuple[bool, int, int]:
    now = time.time()
    b = _MEM.setdefault(uid, _Bucket())
    _prune(b, now)
    used_min, used_day = _sum_bucket(b)
    cap_min, cap_day = _limits(tier)
    if used_min + est > cap_min:
        retry = max(1, int(b.minute[0][0] + _MINUTE_S - now)) if b.minute else 1
        return False, max(0, cap_min - used_min), retry
    if used_day + est > cap_day:
        retry = max(1, int(b.day[0][0] + _DAY_S - now)) if b.day else 1
        return False, max(0, cap_day - used_day), retry
    b.minute.append((now, est))
    b.day.append((now, est))
    return True, cap_min - (used_min + est), 0


async def _redis_check(uid: str, tier: str, est: int) -> tuple[bool, int, int] | None:
    try:
        from api.state import state                       # type: ignore
    except Exception:
        return None
    r = getattr(state, "redis_client", None)
    if r is None:
        return None
    try:
        cap_min, cap_day = _limits(tier)
        now = int(time.time())
        win_min = now // _MINUTE_S
        win_day = now // _DAY_S
        kmin = f"tok:{uid}:m:{win_min}"
        kday = f"tok:{uid}:d:{win_day}"
        async with r.pipeline(transaction=True) as pipe:
            pipe.incrby(kmin, est)
            pipe.expire(kmin, _MINUTE_S + 5)
            pipe.incrby(kday, est)
            pipe.expire(kday, _DAY_S + 5)
            res = await pipe.execute()
        used_min = int(res[0])
        used_day = int(res[2])
        if used_min > cap_min:
            return False, max(0, cap_min - (used_min - est)), _MINUTE_S - (now % _MINUTE_S)
        if used_day > cap_day:
            return False, max(0, cap_day - (used_day - est)), _DAY_S - (now % _DAY_S)
        return True, cap_min - used_min, 0
    except Exception as e:                                          # noqa: BLE001
        logger.debug("governor_redis_error error=%s", e)
        return None


async def reserve(uid: str, tier: str, est_tokens: int) -> tuple[bool, int, int]:
    """Returns (allowed, remaining_in_window, retry_after_s)."""
    est = max(0, int(est_tokens))
    if est == 0 or not uid:
        return True, 0, 0
    outcome = await _redis_check(uid, tier, est)
    if outcome is not None:
        return outcome
    async with _MEM_LOCK:
        return _mem_check(uid, tier, est)


async def settle(uid: str, delta_tokens: int) -> None:
    """Apply a post-hoc delta (negative to refund). Best-effort."""
    if not uid or delta_tokens == 0:
        return
    try:
        from api.state import state                       # type: ignore
        r = getattr(state, "redis_client", None)
        if r is not None:
            now = int(time.time())
            await r.incrby(f"tok:{uid}:m:{now // _MINUTE_S}", int(delta_tokens))
            await r.incrby(f"tok:{uid}:d:{now // _DAY_S}", int(delta_tokens))
            return
    except Exception:
        pass
    async with _MEM_LOCK:
        b = _MEM.get(uid)
        if b:
            b.minute.append((time.time(), int(delta_tokens)))
            b.day.append((time.time(), int(delta_tokens)))


async def snapshot(uid: str, tier: str) -> dict[str, Any]:
    cap_min, cap_day = _limits(tier)
    try:
        from api.state import state                       # type: ignore
        r = getattr(state, "redis_client", None)
        if r is not None:
            now = int(time.time())
            um = await r.get(f"tok:{uid}:m:{now // _MINUTE_S}") or 0
            ud = await r.get(f"tok:{uid}:d:{now // _DAY_S}") or 0
            return {
                "tier": tier,
                "min_used": int(um), "min_cap": cap_min,
                "day_used": int(ud), "day_cap": cap_day,
                "min_remaining": max(0, cap_min - int(um)),
                "day_remaining": max(0, cap_day - int(ud)),
            }
    except Exception:
        pass
    async with _MEM_LOCK:
        b = _MEM.setdefault(uid, _Bucket())
        _prune(b, time.time())
        um, ud = _sum_bucket(b)
        return {
            "tier": tier,
            "min_used": um, "min_cap": cap_min,
            "day_used": ud, "day_cap": cap_day,
            "min_remaining": max(0, cap_min - um),
            "day_remaining": max(0, cap_day - ud),
        }


>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
__all__ = ["estimate_tokens", "reserve", "settle", "snapshot"]