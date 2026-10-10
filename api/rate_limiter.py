# api/rate_limiter.py
"""
AXELR — Multi-Tier Token Rate Limiter
========================================
Enforces RPM, RPD, and TPM per user via Redis Lua scripts (atomic).
Gracefully degrades to in-process fallback when Redis is unavailable.

Tier table (matches the production spec):

    FREE       · 3 RPM   · 100 RPD   · 10,000 TPM   · 5 MB upload
    PRO        · 60 RPM  · 2,500 RPD · 100,000 TPM  · 100 MB upload
    ENTERPRISE · 200 RPM · ∞ RPD     · 500,000 TPM  · 1 GB upload

Public API
----------
    limiter = get_rate_limiter()
    ok, info = await limiter.check(user_id, tier, cost_tokens)
    bucket = TIER_LIMITS[tier]

FastAPI middleware:
    app.middleware("http")(rate_limit_middleware)
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse

from .state import state

logger = logging.getLogger("axelr.rate_limiter")


# ═══════════════════════════════════════════════════════════════════════════
# Tier configuration
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class TierLimits:
    rpm: int
    rpd: int                    # -1 = unlimited
    tpm: int
    upload_bytes: int
    models: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rpm": self.rpm, "rpd": self.rpd, "tpm": self.tpm,
            "upload_bytes": self.upload_bytes, "models": list(self.models),
        }


TIER_LIMITS: dict[str, TierLimits] = {
    "free": TierLimits(
        rpm=3, rpd=100, tpm=10_000,
        upload_bytes=5 * 1024 * 1024,
        models=("gemini", "groq", "cloudflare", "openrouter", "local"),
    ),
    "pro": TierLimits(
        rpm=60, rpd=2_500, tpm=100_000,
        upload_bytes=100 * 1024 * 1024,
        models=("*",),
    ),
    "business": TierLimits(
        rpm=200, rpd=-1, tpm=500_000,
        upload_bytes=1024 * 1024 * 1024,
        models=("*",),
    ),
    "enterprise": TierLimits(
        rpm=200, rpd=-1, tpm=500_000,
        upload_bytes=1024 * 1024 * 1024,
        models=("*",),
    ),
}


# ═══════════════════════════════════════════════════════════════════════════
# Lua: atomic multi-bucket check
# ═══════════════════════════════════════════════════════════════════════════
# Keys:   1=rpm key, 2=rpd key, 3=tpm key
# Args:   1=rpm_limit, 2=rpd_limit (-1 = unlimited), 3=tpm_limit,
#         4=cost_tokens, 5=now_epoch
# Returns: {allowed, rpm_remaining, rpd_remaining, tpm_remaining,
#           retry_after_s}
_LUA_CHECK = """
local rpm_key, rpd_key, tpm_key = KEYS[1], KEYS[2], KEYS[3]
local rpm_limit = tonumber(ARGV[1])
local rpd_limit = tonumber(ARGV[2])
local tpm_limit = tonumber(ARGV[3])
local cost      = tonumber(ARGV[4])
local now       = tonumber(ARGV[5])

-- RPM bucket (60s window)
local rpm_val = tonumber(redis.call('GET', rpm_key) or '0')
if rpm_val >= rpm_limit then
    local ttl = redis.call('TTL', rpm_key)
    return {0, rpm_limit - rpm_val, 0, 0, math.max(ttl, 1)}
end

-- RPD bucket (daily window)
local rpd_val = 0
if rpd_limit >= 0 then
    rpd_val = tonumber(redis.call('GET', rpd_key) or '0')
    if rpd_val >= rpd_limit then
        local ttl = redis.call('TTL', rpd_key)
        return {0, rpm_limit - rpm_val, rpd_limit - rpd_val, 0, math.max(ttl, 1)}
    end
end

-- TPM bucket (60s window)
local tpm_val = tonumber(redis.call('GET', tpm_key) or '0')
if tpm_val + cost > tpm_limit then
    local ttl = redis.call('TTL', tpm_key)
    return {0, rpm_limit - rpm_val, rpd_limit - rpd_val,
            math.max(tpm_limit - tpm_val, 0), math.max(ttl, 1)}
end

-- Commit
redis.call('INCR', rpm_key)
if redis.call('TTL', rpm_key) < 0 then redis.call('EXPIRE', rpm_key, 65) end
if rpd_limit >= 0 then
    redis.call('INCR', rpd_key)
    if redis.call('TTL', rpd_key) < 0 then redis.call('EXPIRE', rpd_key, 86500) end
end
if cost > 0 then
    redis.call('INCRBY', tpm_key, cost)
    if redis.call('TTL', tpm_key) < 0 then redis.call('EXPIRE', tpm_key, 65) end
end

return {1, rpm_limit - rpm_val - 1,
        (rpd_limit >= 0 and rpd_limit - rpd_val - 1 or -1),
        tpm_limit - tpm_val - cost, 0}
"""


# ═══════════════════════════════════════════════════════════════════════════
# In-process fallback
# ═══════════════════════════════════════════════════════════════════════════
_MEM_LOCK = asyncio.Lock()
_MEM: dict[str, dict[str, Any]] = {}


def _mem_check(user_id: str, tier: str, cost: int) -> tuple[bool, dict[str, int]]:
    limits = TIER_LIMITS.get(tier, TIER_LIMITS["free"])
    now = time.time()
    day_start = math.floor(now / 86400) * 86400
    minute_start = math.floor(now / 60) * 60
    slot = _MEM.setdefault(user_id, {"minute": 0, "day": 0, "tokens": 0,
                                     "minute_start": minute_start,
                                     "day_start": day_start})
    if slot["minute_start"] != minute_start:
        slot["minute"] = 0
        slot["tokens"] = 0
        slot["minute_start"] = minute_start
    if slot["day_start"] != day_start:
        slot["day"] = 0
        slot["day_start"] = day_start

    if slot["minute"] >= limits.rpm:
        return False, {"rpm_remaining": 0, "rpd_remaining": -1, "tpm_remaining": 0}
    if limits.rpd >= 0 and slot["day"] >= limits.rpd:
        return False, {"rpm_remaining": limits.rpm - slot["minute"],
                       "rpd_remaining": 0, "tpm_remaining": -1}
    if slot["tokens"] + cost > limits.tpm:
        return False, {"rpm_remaining": limits.rpm - slot["minute"],
                       "rpd_remaining": (limits.rpd - slot["day"]
                                          if limits.rpd >= 0 else -1),
                       "tpm_remaining": limits.tpm - slot["tokens"]}

    slot["minute"] += 1
    slot["day"] += 1
    slot["tokens"] += cost
    return True, {"rpm_remaining": limits.rpm - slot["minute"],
                  "rpd_remaining": (limits.rpd - slot["day"]
                                     if limits.rpd >= 0 else -1),
                  "tpm_remaining": limits.tpm - slot["tokens"]}


# ═══════════════════════════════════════════════════════════════════════════
# Limiter
# ═══════════════════════════════════════════════════════════════════════════
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
            sha = await state.redis_client.script_load(_LUA_CHECK)
            self._lua_sha = sha
            return sha
        except Exception as e:
            logger.warning("rate_limit_script_load_failed error=%s", e)
            return None

    async def check(
        self, user_id: str, tier: str, cost_tokens: int = 0,
    ) -> tuple[bool, dict[str, Any]]:
        """
        Atomic check-and-commit across RPM/RPD/TPM. Never raises.
        Returns (allowed, info) where info contains remaining counters
        and, on deny, `retry_after_s`.
        """
        limits = TIER_LIMITS.get(tier, TIER_LIMITS["free"])
        cost = max(0, int(cost_tokens))

        sha = await self._ensure_script()
        if sha is not None:
            try:
                now = int(time.time())
                minute_key = f"rl:{user_id}:rpm:{now // 60}"
                day_key = f"rl:{user_id}:rpd:{now // 86400}"
                tpm_key = f"rl:{user_id}:tpm:{now // 60}"
                res = await state.redis_client.evalsha(
                    sha, 3, minute_key, day_key, tpm_key,
                    limits.rpm, limits.rpd, limits.tpm, cost, now,
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
                # fall through to memory

        async with _MEM_LOCK:
            allowed, info = _mem_check(user_id, tier, cost)
        info["tier"] = tier
        info["retry_after_s"] = 0 if allowed else 60
        return allowed, info

    async def peek(
        self, user_id: str, tier: str,
    ) -> dict[str, Any]:
        """Read-only — does not consume. Used by the /usage endpoint."""
        limits = TIER_LIMITS.get(tier, TIER_LIMITS["free"])
        if state.redis_client is not None:
            try:
                now = int(time.time())
                k1 = f"rl:{user_id}:rpm:{now // 60}"
                k2 = f"rl:{user_id}:rpd:{now // 86400}"
                k3 = f"rl:{user_id}:tpm:{now // 60}"
                vals = await state.redis_client.mget(k1, k2, k3)
                rpm = int(vals[0] or 0)
                rpd = int(vals[1] or 0)
                tpm = int(vals[2] or 0)
                return {
                    "tier": tier,
                    "rpm_used": rpm, "rpm_limit": limits.rpm,
                    "rpd_used": rpd, "rpd_limit": limits.rpd,
                    "tpm_used": tpm, "tpm_limit": limits.tpm,
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


# ═══════════════════════════════════════════════════════════════════════════
# FastAPI middleware
# ═══════════════════════════════════════════════════════════════════════════
# Paths that bypass rate limiting (liveness, docs, static).
_RATE_LIMIT_SKIP = frozenset({
    "/", "/health", "/api/health", "/api/health/ping", "/readyz",
    "/livez", "/ping", "/metrics", "/docs", "/openapi.json",
    "/redoc", "/api/contract", "/api/audit",
})


def _estimate_cost(request: Request) -> int:
    """Cheap header-based token estimate. Real cost adjusted by pipeline."""
    cl = request.headers.get("content-length")
    try:
        body_bytes = int(cl) if cl else 0
    except ValueError:
        body_bytes = 0
    return max(1, body_bytes // 4)


async def rate_limit_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Any]],
):
    path = request.url.path
    if path in _RATE_LIMIT_SKIP or path.startswith(("/static/", "/docs")):
        return await call_next(request)

    # Auth comes from the pipeline's user dict on request.state if present;
    # otherwise we skip and let route-level checks fire.
    user = getattr(request.state, "user", None)
    if not user or not isinstance(user, dict):
        return await call_next(request)

    tier = user.get("tier", "free")
    user_id = str(user.get("_id", ""))
    if not user_id:
        return await call_next(request)

    cost = _estimate_cost(request)
    allowed, info = await get_rate_limiter().check(user_id, tier, cost)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={
                "success": False, "code": "RATE_LIMIT_EXCEEDED",
                "message": "Rate limit exceeded.",
                "retry_after_s": info.get("retry_after_s", 60),
                "limits": info,
            },
            headers={"Retry-After": str(info.get("retry_after_s", 60))},
        )
    response = await call_next(request)
    response.headers["X-RateLimit-Tier"] = tier
    response.headers["X-RateLimit-Remaining"] = str(
        info.get("rpm_remaining", -1))
    response.headers["X-RateLimit-Limit"] = str(
        TIER_LIMITS.get(tier, TIER_LIMITS["free"]).rpm)
    return response


# ═══════════════════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════════════════
_LIMITER: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _LIMITER
    if _LIMITER is None:
        _LIMITER = RateLimiter()
    return _LIMITER


__all__ = [
    "TierLimits", "TIER_LIMITS",
    "RateLimiter", "get_rate_limiter",
    "rate_limit_middleware",
]