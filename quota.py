# api/quota.py
"""
AXELR — Quota + rate limiting.
==============================
Extracted from routes_core.py.

Public surface
--------------
  * TIER_CONFIG
  * check_and_update_quota
  * check_rate_limit
  * estimate_tokens
"""
from __future__ import annotations

import time

from fastapi import HTTPException

import structlog
logger = structlog.get_logger("axelr.quota")

from .state import state


# ═══════════════════════════════════════════════════════════════════════════
# Tier configuration
# ═══════════════════════════════════════════════════════════════════════════

TIER_CONFIG = {
    "free": {
        "rpm": 5, "tpm": 10000, "rpd": 7,
        "enhancements_per_month": 3, "max_models_per_ws": 1,
        "token_limit_per_day": 100000,
        "providers": ["groq", "cloudflare", "gemini"],
    },
    "pro": {
        "rpm": 20, "tpm": 60000, "rpd": 50,
        "enhancements_per_month": 7, "max_models_per_ws": 2,
        "token_limit_per_day": 500000,
        "providers": ["groq", "gemini", "cloudflare", "openrouter"],
    },
    "business": {
        "rpm": 45, "tpm": 180000, "rpd": 150,
        "enhancements_per_month": 25, "max_models_per_ws": 3,
        "token_limit_per_day": 2000000,
        "providers": ["groq", "gemini", "cloudflare", "openrouter", "mistral"],
    },
    "enterprise": {
        "rpm": 120, "tpm": 500000, "rpd": 1000,
        "enhancements_per_month": 9999, "max_models_per_ws": 5,
        "token_limit_per_day": 10000000, "providers": "*",
    },
}


# ═══════════════════════════════════════════════════════════════════════════
# Quota
# ═══════════════════════════════════════════════════════════════════════════

async def check_and_update_quota(user: dict | None, workspace: str, task_type: str) -> None:
    if not user:
        return
    tier = user.get("tier", "free")
    tier_config = TIER_CONFIG.get(tier, TIER_CONFIG["free"])
    limit = tier_config.get("rpd", 0)

    if workspace == "data":
        usage_field = "quotas.dailyExtractionsUsed"
    elif workspace == "design":
        usage_field = "quotas.dailyGenerationsUsed"
    elif workspace == "prompt":
        usage_field = "quotas.dailyEnhancementsUsed"
    else:
        usage_field = "dailyUsage"

    current = user.get("quotas", {}).get(usage_field.split(".")[-1], 0)
    if usage_field == "dailyUsage":
        current = user.get("dailyUsage", 0)

    if current >= limit:
        raise HTTPException(status_code=429, detail={
            "code": "QUOTA_EXCEEDED",
            "message": f"Daily limit of {limit} requests exceeded for this workspace.",
            "usage": current, "limit": limit,
        })

    if state.db_available and state.users_col is not None:
        await state.users_col.update_one(
            {"_id": user["_id"]},
            {"$inc": {usage_field: 1, "dailyUsage": 1}},
        )


async def check_rate_limit(user_id: str, tier: str, endpoint: str) -> tuple[bool, int]:
    if not state.redis_client:
        return True, 0
    try:
        now = int(time.time())
        limits = {
            "free":     {"rpm": 5,  "tpm": 10000,  "rpd": 5,  "rpm_hard": 8,  "tpm_hard": 15000,  "rpd_hard": 8},
            "pro":      {"rpm": 15, "tpm": 50000,  "rpd": 15, "rpm_hard": 20, "tpm_hard": 75000,  "rpd_hard": 20},
            "business": {"rpm": 30, "tpm": 150000, "rpd": 30, "rpm_hard": 45, "tpm_hard": 225000, "rpd_hard": 45},
        }
        lim = limits.get(tier, limits["free"])
        key_rpm = f"rate:{user_id}:rpm:{endpoint}"
        key_tpm = f"rate:{user_id}:tpm:{endpoint}"
        key_rpd = f"rate:{user_id}:rpd:{endpoint}"

        minute_ago = now - 60
        day_ago = now - 86400
        if not hasattr(state.redis_client, "pipeline"):
            return True, 0
        pipe = state.redis_client.pipeline()
        pipe.zremrangebyscore(key_rpm, 0, minute_ago)
        pipe.zremrangebyscore(key_tpm, 0, minute_ago)
        pipe.zremrangebyscore(key_rpd, 0, day_ago)
        await pipe.execute()

        rpm_count = await state.redis_client.zcard(key_rpm)
        tpm_count = await state.redis_client.zcard(key_tpm)
        rpd_count = await state.redis_client.zcard(key_rpd)

        if rpm_count >= lim["rpm_hard"] or tpm_count >= lim["tpm_hard"] or rpd_count >= lim["rpd_hard"]:
            return False, 60

        pipe = state.redis_client.pipeline()
        pipe.zadd(key_rpm, {str(now): now})
        pipe.zadd(key_tpm, {str(now): now})
        pipe.zadd(key_rpd, {str(now): now})
        pipe.expire(key_rpm, 120)
        pipe.expire(key_tpm, 120)
        pipe.expire(key_rpd, 172800)
        await pipe.execute()
        return True, 0
    except Exception as e:
        logger.warning("rate_limit_check_failed error=%s", e)
        return True, 0


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


__all__ = [
    "TIER_CONFIG",
    "check_and_update_quota",
    "check_rate_limit",
    "estimate_tokens",
]