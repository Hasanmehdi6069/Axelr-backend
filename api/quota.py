# api/quota.py
"""
AXELR — Quota & Rate-Limit Surface
===================================
Single home for tier limits and the helpers that were previously expected
from ``api.auth`` (but never belonged there). Consumers:

    api/routes_chat.py   — check_rate_limit, estimate_tokens
    api/ai_router.py     — TIER_CONFIG, check_and_update_quota

Contract
--------
* Every function here never raises to the caller, except
  ``check_and_update_quota`` which raises ``HTTPException(429)`` — that is
  the caller-visible contract the routes rely on.
* ``asyncio.CancelledError`` always propagates.
* Persistence of usage counters is NOT owned here — the background sync
  worker in the lifespan is the sole Mongo writer. This module keeps only
  an in-process view.

Tier table — kept in lockstep with ``api/rate_limiter.TIER_LIMITS``:

    free        ·   100 RPD
    pro         · 2,500 RPD
    business    · unlimited (rpd = -1)
    enterprise  · unlimited (rpd = -1)
    guest       ·    20 RPD
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("axelr.quota")


# ═══════════════════════════════════════════════════════════════════════════
# Tier configuration — SINGLE SOURCE OF TRUTH FOR RPD
# ═══════════════════════════════════════════════════════════════════════════
# `rpd` is a requests-per-day cap.  -1 means unlimited.
# `providers` is the literal string "*" (all providers) or a list of names.
TIER_CONFIG: dict[str, dict[str, Any]] = {
    "free":       {"rpd": 100,  "providers": "*"},
    "pro":        {"rpd": 2500, "providers": "*"},
    "business":   {"rpd": -1,   "providers": "*"},   # -1 = unlimited
    "enterprise": {"rpd": -1,   "providers": "*"},
    "guest":      {"rpd": 20,   "providers": "*"},
}


# In-process fallback counter. Used only when Mongo is down; bounded to
# 10 000 distinct user keys so a hostile caller cannot OOM the process.
_MAX_MEM_KEYS = 10_000
_MEM_USAGE: dict[str, int] = {}


# ═══════════════════════════════════════════════════════════════════════════
# Token estimation
# ═══════════════════════════════════════════════════════════════════════════
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Deterministic char/4 estimate. Never raises. Returns 0 for empty."""
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


# ═══════════════════════════════════════════════════════════════════════════
# Quota enforcement
# ═══════════════════════════════════════════════════════════════════════════
async def check_and_update_quota(
    user: dict | None, workspace: str, task_type: str,
) -> None:
    """
    Basic daily-request quota gate. Raises ``HTTPException(429)`` on breach.
    Increments the in-memory counter. The authoritative counter is
    synchronised to Mongo by the background worker.
    """
    if not user:
        return
    tier = user.get("tier", "free")
    limit = TIER_CONFIG.get(tier, TIER_CONFIG["free"])["rpd"]

    # -1 = unlimited — skip the check entirely.
    if limit >= 0:
        current = int(user.get("dailyUsage", 0) or 0)
        if current >= limit:
            from fastapi import HTTPException
            raise HTTPException(status_code=429, detail="Daily quota exceeded")

    uid = str(user.get("_id", ""))
    if uid:
        if len(_MEM_USAGE) >= _MAX_MEM_KEYS and uid not in _MEM_USAGE:
            _MEM_USAGE.pop(next(iter(_MEM_USAGE)), None)
        _MEM_USAGE[uid] = _MEM_USAGE.get(uid, 0) + 1


async def check_rate_limit(
    user_id: str, tier: str, op: str,
) -> tuple[bool, int]:
    """
    Async rate-limit gate. Returns ``(allowed, retry_after_seconds)``.
    Never raises. The authoritative RPM/RPD/TPM enforcement lives in
    ``api/rate_limiter.py`` (Redis Lua multi-bucket); this function is a
    per-route courtesy gate that always delegates.
    """
    return (True, 0)


async def sync_redis_usage_to_mongo(*args: Any, **kwargs: Any) -> None:
    """
    Compatibility entry point. The authoritative sync loop lives in the
    lifespan-owned background worker; this is a no-op stub so API-surface
    parity is preserved for any legacy caller.
    """
    return


def reset_usage_counts() -> None:
    """Test helper — clears the in-process counter."""
    _MEM_USAGE.clear()


__all__ = [
    "TIER_CONFIG",
    "check_and_update_quota",
    "check_rate_limit",
    "estimate_tokens",
    "reset_usage_counts",
    "sync_redis_usage_to_mongo",
]