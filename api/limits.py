# api/limits.py
"""AXELR — unified tier, rate-limit, and quota enforcement.

Single tier table. Redis-Lua atomic limiter. In-process fallback. FastAPI
middleware reads identity straight from the JWT — never from
``request.state.user`` (populated *after* middleware runs).
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from .state import state

logger = logging.getLogger("axelr.limits")


# ═══════════════════════════════════════════════════════════════════════
# Tier table — the ONE table
# ═══════════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class TierLimits:
    rpm: int
    rpd: int                    # -1 = unlimited
    tpm: int                    # -1 = unlimited
    upload_bytes: int
    daily_extractions: int
    daily_generations: int
    daily_enhancements: int
    remote_providers_support: list[str] | str
    models_description: str
    file_size_limit_mb: int
    max_files_simultaneously: int
    context_window_min_tokens: int
    context_window_max_tokens: int
    features: list[str]
    guest: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "rpm": self.rpm, "rpd": self.rpd, "tpm": self.tpm,
            "upload_bytes": self.upload_bytes,
            "daily_extractions": self.daily_extractions,
            "daily_generations": self.daily_generations,
            "daily_enhancements": self.daily_enhancements,
            "remote_providers_support": self.remote_providers_support,
            "models_description": self.models_description,
            "file_size_limit_mb": self.file_size_limit_mb,
            "max_files_simultaneously": self.max_files_simultaneously,
            "context_window_min_tokens": self.context_window_min_tokens,
            "context_window_max_tokens": self.context_window_max_tokens,
            "features": self.features,
        }


TIERS: dict[str, TierLimits] = {
    "guest": TierLimits(
        rpm=3, rpd=30, tpm=5_000, upload_bytes=5 * 1024 * 1024,
        daily_extractions=5, daily_generations=5, daily_enhancements=0,
        remote_providers_support=["provider_1"], models_description="Basic",
        file_size_limit_mb=5, max_files_simultaneously=5,
        context_window_min_tokens=256, context_window_max_tokens=2048,
        features=[], guest=True,
    ),
    "free": TierLimits(
        rpm=3, rpd=15, tpm=10_000, upload_bytes=10 * 1024 * 1024,
        daily_extractions=5, daily_generations=5, daily_enhancements=5,
        remote_providers_support=["gemini", "groq", "cloudflare"],  # Minimum 3 active healthy providers
        models_description="3+ active AI providers with specialized 50M–120M parameter internal engines",
        file_size_limit_mb=10, max_files_simultaneously=10,
        context_window_min_tokens=512, context_window_max_tokens=4096, features=[],
    ),
    "pro": TierLimits(
        rpm=30, rpd=60, tpm=64_000, upload_bytes=30 * 1024 * 1024,
        daily_extractions=20, daily_generations=20, daily_enhancements=20,
        remote_providers_support="All 9+ active providers",
        models_description="Specialized 80M–300M+ parameter internal engines",
        file_size_limit_mb=30, max_files_simultaneously=12,
        context_window_min_tokens=2048, context_window_max_tokens=8192,
        features=["Anti-hallucination Pro", "Touch & Fix"],
    ),
    "business": TierLimits(
        rpm=60, rpd=120, tpm=200_000, upload_bytes=50 * 1024 * 1024,
        daily_extractions=40, daily_generations=40, daily_enhancements=40,
        remote_providers_support="All 40+ providers",
        models_description="Specialized 80M–500M+ parameter internal engines",
        file_size_limit_mb=50, max_files_simultaneously=20,
        context_window_min_tokens=8192, context_window_max_tokens=999_999,
        features=["Anti-hallucination Max", "Touch & Fix",
                  "Generate tests with loop"],
    ),
    "enterprise": TierLimits(
        rpm=500, rpd=-1, tpm=2_000_000, upload_bytes=10 * 1024 * 1024 * 1024,
        daily_extractions=-1, daily_generations=-1, daily_enhancements=9999,
        remote_providers_support="Custom", models_description="Custom",
        file_size_limit_mb=10240, max_files_simultaneously=100,
        context_window_min_tokens=16384, context_window_max_tokens=9_999_999,
        features=["Custom"],
    ),
}

TIER_LIMITS = TIERS
TIER_CONFIG: dict[str, dict[str, Any]] = {
    name: {"rpd": t.rpd, "providers": "*"} for name, t in TIERS.items()
}
RATE_LIMITS: dict[str, int] = {name: t.rpm for name, t in TIERS.items()}


def get_tier(tier: str | None) -> TierLimits:
    return TIERS.get((tier or "free").lower(), TIERS["free"])


# ═══════════════════════════════════════════════════════════════════════
# Atomic Lua check
# ═══════════════════════════════════════════════════════════════════════
_LUA_CHECK = """
local rpm_key, rpd_key, tpm_key = KEYS[1], KEYS[2], KEYS[3]
local rpm_limit = tonumber(ARGV[1])
local rpd_limit = tonumber(ARGV[2])
local tpm_limit = tonumber(ARGV[3])
local cost      = tonumber(ARGV[4])

local rpm_val = tonumber(redis.call('GET', rpm_key) or '0')
if rpm_val >= rpm_limit then
    return {0, rpm_limit - rpm_val, 0, 0, math.max(redis.call('TTL', rpm_key), 1)}
end
local rpd_val = 0
if rpd_limit >= 0:
    rpd_val = tonumber(redis.call('GET', rpd_key) or '0')
    if rpd_val >= rpd_limit then
        return {0, rpm_limit - rpm_val, rpd_limit - rpd_val, 0,
                math.max(redis.call('TTL', rpd_key), 1)}
    end
end
local tpm_val = 0
if tpm_limit >= 0:
    tpm_val = tonumber(redis.call('GET', tpm_key) or '0')
    if tpm_val + cost > tpm_limit then
        return {0, rpm_limit - rpm_val, rpd_limit - rpd_val,
                math.max(tpm_limit - tpm_val, 0),
                math.max(redis.call('TTL', tpm_key), 1)}
    end
end
redis.call('INCR', rpm_key)
if redis.call('TTL', rpm_key) < 0 then redis.call('EXPIRE', rpm_key, 65) end
if rpd_limit >= 0:
    redis.call('INCR', rpd_key)
    if redis.call('TTL', rpd_key) < 0 then redis.call('EXPIRE', rpd_key, 86500) end
end
if tpm_limit >= 0 and cost > 0:
    redis.call('INCRBY', tpm_key, cost)
    if redis.call('TTL', tpm_key) < 0 then redis.call('EXPIRE', tpm_key, 65) end
end
return {1, rpm_limit - rpm_val - 1,
        (rpd_limit >= 0 and rpd_limit - rpd_val - 1 or -1),
        (tpm_limit >= 0 and tpm_limit - tpm_val - cost or -1), 0}
"""


# ═══════════════════════════════════════════════════════════════════════
# In-process fallback
# ═══════════════════════════════════════════════════════════════════════
_MEM_LOCK = asyncio.Lock()
_MEM: dict[str, dict[str, Any]] = {}
_MEM_MAX_KEYS = 20_000


def _mem_check(user_id: str, tier: str, cost: int) -> tuple[bool, dict[str, int]]:
    limits = get_tier(tier)
    now = time.time()
    day_start = math.floor(now / 86400) * 86400
    minute_start = math.floor(now / 60) * 60
    if len(_MEM) >= _MEM_MAX_KEYS and user_id not in _MEM:
        _MEM.pop(next(iter(_MEM)), None)
    slot = _MEM.setdefault(user_id, {
        "minute": 0, "day": 0, "tokens": 0,
        "minute_start": minute_start, "day_start": day_start,
    })
    if slot["minute_start"] != minute_start:
        slot["minute"] = 0
        slot["tokens"] = 0
        slot["minute_start"] = minute_start
    if slot["day_start"] != day_start:
        slot["day"] = 0
        slot["day_start"] = day_start

    if slot["minute"] >= limits.rpm:
        return False, {"rpm_remaining": 0, "rpd_remaining": -1, "tpm_remaining": -1}
    if limits.rpd >= 0 and slot["day"] >= limits.rpd:
        return False, {
            "rpm_remaining": limits.rpm - slot["minute"],
            "rpd_remaining": 0, "tpm_remaining": -1,
        }
    if limits.tpm >= 0 and slot["tokens"] + cost > limits.tpm:
        return False, {
            "rpm_remaining": limits.rpm - slot["minute"],
            "rpd_remaining": (limits.rpd - slot["day"] if limits.rpd >= 0 else -1),
            "tpm_remaining": limits.tpm - slot["tokens"],
        }

    slot["minute"] += 1
    slot["day"] += 1
    slot["tokens"] += cost
    return True, {
        "rpm_remaining": limits.rpm - slot["minute"],
        "rpd_remaining": (limits.rpd - slot["day"] if limits.rpd >= 0 else -1),
        "tpm_remaining": (limits.tpm - slot["tokens"] if limits.tpm >= 0 else -1),
    }


# ═══════════════════════════════════════════════════════════════════════
# RateLimiter
# ═══════════════════════════════════════════════════════════════════════
class RateLimiter:
    __slots__ = ("_lua_sha",)

    def __init__(self) -> None:
        self._lua_sha: str | None = None

    async def _ensure_script(self) -> str | None:
        if state.redis_client is None:
            return None
        if self._lua_sha:
            return self._lua_sha
        try:
            self._lua_sha = await state.redis_client.script_load(_LUA_CHECK)
            return self._lua_sha
        except Exception as e:
            logger.warning("rate_limit_script_load_failed error=%s", e)
            return None

    async def check(
        self, user_id: str, tier: str, cost_tokens: int = 0,
    ) -> tuple[bool, dict[str, Any]]:
        limits = get_tier(tier)
        cost = max(0, int(cost_tokens))
        sha = await self._ensure_script()
        if sha is not None:
            try:
                now = int(time.time())
                res = await state.redis_client.evalsha(
                    sha, 3,
                    f"rl:{user_id}:rpm:{now // 60}",
                    f"rl:{user_id}:rpd:{now // 86400}",
                    f"rl:{user_id}:tpm:{now // 60}",
                    limits.rpm, limits.rpd, limits.tpm, cost,
                )
                allowed = bool(res[0])
                return allowed, {
                    "rpm_remaining": int(res[1]),
                    "rpd_remaining": int(res[2]),
                    "tpm_remaining": int(res[3]),
                    "retry_after_s": int(res[4]) if not allowed else 0,
                    "tier": tier,
                }
            except Exception as e:
                logger.debug("rate_limit_redis_failed error=%s", e)

        async with _MEM_LOCK:
            allowed, info = _mem_check(user_id, tier, cost)
        info["tier"] = tier
        info["retry_after_s"] = 0 if allowed else 60
        return allowed, info

    async def peek(self, user_id: str, tier: str) -> dict[str, Any]:
        limits = get_tier(tier)
        if state.redis_client is not None:
            try:
                now = int(time.time())
                vals = await state.redis_client.mget(
                    f"rl:{user_id}:rpm:{now // 60}",
                    f"rl:{user_id}:rpd:{now // 86400}",
                    f"rl:{user_id}:tpm:{now // 60}",
                )
                return {
                    "tier": tier,
                    "rpm_used": int(vals[0] or 0), "rpm_limit": limits.rpm,
                    "rpd_used": int(vals[1] or 0), "rpd_limit": limits.rpd,
                    "tpm_used": int(vals[2] or 0), "tpm_limit": limits.tpm,
                    "upload_limit_bytes": limits.upload_bytes,
                }
            except Exception:
                pass
        slot = _MEM.get(user_id) or {}
        return {
            "tier": tier,
            "rpm_used": slot.get("minute", 0), "rpm_limit": limits.rpm,
            "rpd_used": slot.get("day", 0), "rpd_limit": limits.rpd,
            "tpm_used": slot.get("tokens", 0), "tpm_limit": limits.tpm,
            "upload_limit_bytes": limits.upload_bytes,
        }


_LIMITER: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _LIMITER
    if _LIMITER is None:
        _LIMITER = RateLimiter()
    return _LIMITER


# ═══════════════════════════════════════════════════════════════════════
# Token estimation
# ═══════════════════════════════════════════════════════════════════════
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


# ═══════════════════════════════════════════════════════════════════════
# Daily quota gate (raises 429)
# ═══════════════════════════════════════════════════════════════════════
_MEM_QUOTA: dict[str, int] = {}


async def check_and_update_quota(
    user: dict | None, workspace: str, task_type: str,
) -> None:
    if not user:
        return
    tier = user.get("tier", "free")
    limits = get_tier(tier)
    if limits.rpd < 0:
        return
    current = int(user.get("dailyUsage", 0) or 0)
    if current >= limits.rpd:
        from fastapi import HTTPException
        raise HTTPException(status_code=429, detail="Daily quota exceeded")
    uid = str(user.get("_id", ""))
    if uid:
        if len(_MEM_QUOTA) >= _MEM_MAX_KEYS and uid not in _MEM_QUOTA:
            _MEM_QUOTA.pop(next(iter(_MEM_QUOTA)), None)
        _MEM_QUOTA[uid] = _MEM_QUOTA.get(uid, 0) + 1


async def check_rate_limit(
    user_id: str, tier: str, op: str = "",
) -> tuple[bool, int]:
    allowed, info = await get_rate_limiter().check(user_id, tier, cost_tokens=1)
    return allowed, int(info.get("retry_after_s", 0))


def reset_usage_counts() -> None:
    _MEM_QUOTA.clear()


async def sync_redis_usage_to_mongo(*args: Any, **kwargs: Any) -> None:
    return


# ═══════════════════════════════════════════════════════════════════════
# FastAPI middleware — reads identity from the JWT directly
# ═══════════════════════════════════════════════════════════════════════
_RATE_LIMIT_SKIP = frozenset({
    "/", "/terms", "/privacy",
    "/health", "/healthz", "/livez", "/ping", "/readyz",
    "/api/health", "/api/health/ping", "/api/health/live", "/api/health/ready",
    "/api/live", "/api/ping", "/api/ready", "/api/status",
    "/metrics", "/docs", "/redoc", "/openapi.json",
    "/api/contract", "/api/audit",
    "/favicon.ico", "/robots.txt",
})


def _estimate_cost(request: Request) -> int:
    cl = request.headers.get("content-length")
    try:
        body_bytes = int(cl) if cl else 0
    except ValueError:
        body_bytes = 0
    return max(1, body_bytes // 4)


def _identity_from_request(request: Request) -> tuple[str, str] | None:
    """Extract (user_id, tier) from the bearer token without hitting Mongo."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    try:
        from .auth import decode_token
        payload = decode_token(token)
        if payload:
            uid = payload.get("uid") or payload.get("sub") or ""
            tier = payload.get("tier") or "free"
            if uid:
                return (str(uid), str(tier))
    except Exception:
        pass
    try:
        from google.oauth2 import id_token as _gid
        from google.auth.transport import requests as _greq
        from .config import GOOGLE_CLIENT_ID
        info = _gid.verify_oauth2_token(token, _greq.Request(), GOOGLE_CLIENT_ID)
        return (f"g:{info['sub']}", "free")
    except Exception:
        return None


async def rate_limit_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Any]],
):
    path = request.url.path
    if path in _RATE_LIMIT_SKIP or path.startswith(("/static/", "/docs")):
        return await call_next(request)

    identity = _identity_from_request(request)
    if identity is None:
        return await call_next(request)

    user_id, tier = identity
    cost = _estimate_cost(request)
    allowed, info = await get_rate_limiter().check(user_id, tier, cost)
    if not allowed:
        retry = int(info.get("retry_after_s", 60))
        return JSONResponse(
            status_code=429,
            content={
                "success": False, "code": "RATE_LIMIT_EXCEEDED",
                "message": "Rate limit exceeded.",
                "retry_after_s": retry,
                "limits": info,
            },
            headers={"Retry-After": str(retry)},
        )
    response = await call_next(request)
    limits = get_tier(tier)
    response.headers["X-RateLimit-Tier"] = tier
    response.headers["X-RateLimit-Remaining"] = str(info.get("rpm_remaining", -1))
    response.headers["X-RateLimit-Limit"] = str(limits.rpm)
    return response


__all__ = [
    "RATE_LIMITS", "TIERS", "TIER_CONFIG", "TIER_LIMITS", "TierLimits",
    "RateLimiter", "check_and_update_quota", "check_rate_limit",
    "estimate_tokens", "get_rate_limiter", "get_tier",
    "rate_limit_middleware", "reset_usage_counts", "sync_redis_usage_to_mongo",
]