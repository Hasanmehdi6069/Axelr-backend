# api/state.py
"""
AXELR API — shared mutable runtime state.
==========================================
Every long-lived, boot-time-populated object lives here once. Other modules
consume via ``from .state import state`` and access attributes at *call*
time (never at import time), so the values populated during ``lifespan``
are always visible.

Also hosts:
  * ``get_object_id()``                — Mongo ObjectId when DB is up
  * ``init_db`` / ``init_redis`` / ``init_qstash``
  * Sync-friendly Redis wrappers (async): ``get_redis_cache``, etc.
  * ``limiter``                        — slowapi rate limiter
"""
from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import time
from typing import Any

import httpx
import redis.asyncio as aioredis
from bson import ObjectId
from cachetools import TTLCache
from fastapi import Request
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorClient

from .config import (
    MONGO_URI,
    REDIS_URL,
    HTTP_CLIENT,
)

logger = logging.getLogger("axelr.state")


# ── Slowapi (guarded) ────────────────────────────────────────────────────────

try:
    _slowapi = importlib.import_module("slowapi")
    Limiter = _slowapi.Limiter
    _rate_limit_exceeded_handler = _slowapi._rate_limit_exceeded_handler
    RateLimitExceeded = importlib.import_module("slowapi.errors").RateLimitExceeded
except ImportError:
    class Limiter:                                   # type: ignore
        def __init__(self, *a, **kw): pass
        def limit(self, *a, **kw): return lambda f: f

    class RateLimitExceeded(Exception): pass

    def _rate_limit_exceeded_handler(request, exc):  # type: ignore
        return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})


def _ratelimit_key(request: Request) -> str:
    """Stable rate-limit key: hashed bearer token if present, else client IP."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return hashlib.sha256(auth.encode()).hexdigest()[:24]
    return request.client.host if request.client else "unknown"


limiter = Limiter(key_func=_ratelimit_key, default_limits=["100/minute"])


# ── Mutable state container ──────────────────────────────────────────────────

class _AppState:
    """
    Every long-lived runtime object. Populated at boot by ``init_*()``
    functions and consumed by route handlers *at call time*.
    """
    # MongoDB
    client: AsyncIOMotorClient | None = None
    db: Any = None
    users_col: Any = None
    sessions_col: Any = None
    reports_col: Any = None
    pr_reports_col: Any = None
    projects_col: Any = None
    db_available: bool = False

    # Redis
    redis_client: aioredis.Redis | None = None

    # Hot-process cache — single TTLCache shared by AI responses + enhancer
    ai_cache: TTLCache = TTLCache(maxsize=3000, ttl=3600)

    # Provider metrics / cooldowns
    provider_latency: dict[str, float] = {}
    provider_health: dict[str, dict] = {}

    # Distributed circuit breaker (Redis-backed)
    circuit_breaker: Any = None

    # Elite service holders (set in lifespan)
    intent_classifier: Any = None
    context_registry: Any = None
    dependency_tracker: Any = None
    critic_agent: Any = None
    self_healer: Any = None
    pr_defense: Any = None
    touch_fix_engine: Any = None
    orchestrator: Any = None
    global_ai_router: Any = None

    # Sub-module instances (conversation memory, repo indexer, test loop)
    conversation_memory: Any = None
    repo_indexer: Any = None
    test_loop: Any = None

    # Stateless services (constructed once at import of app.py)
    code_guard: Any = None
    intent_router: Any = None
    semantic_cache: Any = None

    # Provider validation cache (Redis key constants)
    PROVIDER_VALIDATION_KEY: str = "axelr:provider:validation"
    PROVIDER_VALIDATION_TTL: int = 300


state = _AppState()


# ── ObjectId helper ──────────────────────────────────────────────────────────

def get_object_id():
    """Return the ObjectId class when Mongo is up, else ``None``."""
    return ObjectId if state.db_available else None


# ── Redis wrappers (async, JSON) ─────────────────────────────────────────────

async def get_redis_cache(key: str) -> Any:
    if not state.redis_client:
        return None
    try:
        data = await state.redis_client.get(key)
        return json.loads(data) if data else None
    except Exception as e:
        logger.warning("redis_get_failed key=%s error=%s", key, e)
        return None


async def set_redis_cache(key: str, value: Any, ttl: int) -> None:
    if not state.redis_client:
        return
    try:
        await state.redis_client.setex(key, ttl, json.dumps(value))
    except Exception as e:
        logger.warning("redis_set_failed key=%s error=%s", key, e)


async def delete_redis_cache(key: str) -> None:
    if not state.redis_client:
        return
    try:
        await state.redis_client.delete(key)
    except Exception as e:
        logger.warning("redis_delete_failed key=%s error=%s", key, e)


# ── Boot helpers ─────────────────────────────────────────────────────────────

async def init_redis() -> None:
    """Connect to Redis if a URL is configured. Never raises."""
    if not REDIS_URL or not REDIS_URL.startswith(("redis://", "rediss://", "unix://")):
        logger.info("redis_not_configured")
        return
    try:
        state.redis_client = await aioredis.from_url(
            REDIS_URL, decode_responses=True, max_connections=10,
        )
        if await state.redis_client.ping():
            logger.info("redis_connected")
            return
    except Exception as e:
        logger.warning("redis_connect_failed error=%s", e)
        state.redis_client = None


async def init_db() -> None:
    """Connect to MongoDB. Never raises; flips ``db_available`` accordingly."""
    if not MONGO_URI:
        logger.critical("mongo_unavailable_degraded_mode")
        state.db_available = False
        return
    try:
        state.client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        await state.client.admin.command("ping")
        state.db = state.client.NexusDB
        state.users_col      = state.db.users
        state.sessions_col   = state.db.conversations
        state.reports_col    = state.db.reports
        state.pr_reports_col = state.db.pr_reports
        state.projects_col   = state.db.projects
        state.db_available = True
        logger.info("mongo_connected")
    except Exception as e:
        logger.critical("mongo_connect_failed error=%s", e)
        state.db_available = False


async def init_qstash() -> None:
    """Best-effort validation of QStash token. Never raises."""
    token = (os.getenv("QSTASH_TOKEN") or "").strip()
    base = (os.getenv("QSTASH_URL") or "https://qstash.upstash.io").strip().rstrip("/")
    if not token:
        logger.info("qstash_not_configured")
        return
    if len(token) < 20:
        logger.warning("qstash_token_too_short")
        return
    try:
        headers = {"Authorization": f"Bearer {token}"}
        r = await HTTP_CLIENT.get(f"{base}/v2/events?limit=1", headers=headers, timeout=10.0)
        if r.status_code == 401:
            logger.error("qstash_unauthorized")
            return
        r.raise_for_status()
        logger.info("qstash_configured")
    except httpx.ConnectError as e:
        logger.error("qstash_connect_failed error=%s", e)
    except httpx.TimeoutException:
        logger.error("qstash_timeout")
    except httpx.HTTPStatusError as e:
        logger.error("qstash_http_error status=%s body=%s",
                     e.response.status_code, e.response.text[:200])
    except Exception as e:
        logger.error("qstash_unexpected_error error=%s", e)


# ── Public surface ───────────────────────────────────────────────────────────
__all__ = [
    "state",
    "limiter",
    "RateLimitExceeded",
    "_rate_limit_exceeded_handler",
    "get_object_id",
    "get_redis_cache",
    "set_redis_cache",
    "delete_redis_cache",
    "init_redis",
    "init_db",
    "init_qstash",
]