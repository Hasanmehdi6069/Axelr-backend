<<<<<<< HEAD
﻿# core/infra.py
"""
AXELR - Infra Layer (Redis + CircuitBreaker + ContextRegistry + state shim + chaos)
====================================================================================
One module for everything that talks to Redis, backs off on failure, and
degrades gracefully. The circuit breaker, the shared cache, the external
context registry, and the chaos injector all share the same contract:
    * Redis primary, in-memory fallback.
    * Never raise to the caller.
    * Bounded state, bounded retries, deterministic behavior.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import os
import random
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from cachetools import TTLCache

logger = logging.getLogger("axelr.infra")


# ------------------------------------------------------------------------------
# 1 - State shim (was core/state_compat.py)
# ------------------------------------------------------------------------------
def get_redis_client() -> Any:
    """Bridges core/* to api.state.redis_client without an import cycle."""
    try:
        state_mod = importlib.import_module("api.state")
        return getattr(state_mod.state, "redis_client", None)
    except Exception:
        return None


# ------------------------------------------------------------------------------
# 2 - Shared Redis cache + process-local TTL cache (was core/infra.py)
# ------------------------------------------------------------------------------
local_cache: TTLCache = TTLCache(maxsize=1000, ttl=300)


class _DummyAsyncCache:
    """No-op fallback when Redis is unavailable. Same shape, zero risk."""
    async def get(self, *a, **k): return None
    async def set(self, *a, **k): pass
    async def delete(self, *a, **k): pass
    async def exists(self, *a, **k): return 0
    async def incr(self, *a, **k): return 1
    async def expire(self, *a, **k): pass
    async def setex(self, *a, **k): pass
    async def keys(self, *a, **k): return []
    async def ttl(self, *a, **k): return 0


shared_async_cache: Any | None = None


async def init_shared_redis_cache() -> None:
    """Pull the shared async Redis client from api.state. Never blocks import."""
    global shared_async_cache
    if shared_async_cache is not None:
        return
    client = get_redis_client()
    if client is not None:
        shared_async_cache = client
        logger.info("shared_redis_connected_via_state_client")
        return
    logger.warning("shared_redis_unavailable_falling_back_to_dummy_cache")
    shared_async_cache = _DummyAsyncCache()


async def get_redis_cache(key: str) -> Any:
    if not shared_async_cache:
        await init_shared_redis_cache()
    try:
        return await shared_async_cache.get(key)  # type: ignore[union-attr]
    except Exception:
        return None


async def set_redis_cache(key: str, value: Any, ttl: int | None = None) -> bool:
    if not shared_async_cache:
        await init_shared_redis_cache()
    try:
        if ttl is not None:
            await shared_async_cache.set(key, value, ex=ttl)  # type: ignore[union-attr]
        else:
            await shared_async_cache.set(key, value)  # type: ignore[union-attr]
        return True
    except Exception:
        return False


async def delete_redis_cache(key: str) -> bool:
    if not shared_async_cache:
        await init_shared_redis_cache()
    try:
        await shared_async_cache.delete(key)  # type: ignore[union-attr]
        return True
    except Exception:
        return False


# ------------------------------------------------------------------------------
# 3 - Circuit breaker (was core/infra.py)
# ------------------------------------------------------------------------------
class CircuitBreaker:
    """Distributed circuit breaker. Redis primary, memory fallback. Never raises."""

    def __init__(
        self,
        redis_client: Any | None,
        *,
        threshold: int = 3,
        cooldown_s: float = 60.0,
        rate_limit_cooldown_s: float = 1800.0,
        namespace: str = "axelr:cb",
    ) -> None:
        self.redis = redis_client
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self.rate_limit_cooldown_s = rate_limit_cooldown_s
        self.ns = namespace
        self._mem_failures: dict[str, int] = {}
        self._mem_cooldowns: dict[str, float] = {}
        self._lock = asyncio.Lock()

    def _fail_key(self, p: str) -> str: return f"{self.ns}:fail:{p}"
    def _open_key(self, p: str) -> str: return f"{self.ns}:open:{p}"

    async def is_available(self, provider: str) -> bool:
        if self.redis is not None:
            try:
                if await self.redis.get(self._open_key(provider)) is not None:
                    return False
                return True
            except Exception:
                pass
        return time.time() >= self._mem_cooldowns.get(provider, 0.0)

    async def record_success(self, provider: str) -> None:
        if self.redis is not None:
            try:
                await self.redis.delete(self._fail_key(provider))
                await self.redis.delete(self._open_key(provider))
            except Exception:
                pass
        async with self._lock:
            self._mem_failures[provider] = 0
            self._mem_cooldowns.pop(provider, None)

    async def record_failure(self, provider: str, *, is_rate_limit: bool = False) -> None:
        if self.redis is not None:
            try:
                return await self._record_redis(provider, is_rate_limit)
            except Exception:
                pass
        async with self._lock:
            self._record_mem(provider, is_rate_limit)

    async def _record_redis(self, provider: str, is_rate_limit: bool) -> None:
        fk, ok = self._fail_key(provider), self._open_key(provider)
        # Phase 1: unconditional INCR + EXPIRE (idempotent, safe).
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.incr(fk)
            pipe.expire(fk, int(self.rate_limit_cooldown_s * 2))
            result = await pipe.execute()
        n = int(result[0])
        # Phase 2: trip is atomic - either both writes land or neither.
        if is_rate_limit or n >= self.threshold:
            cooldown = self.rate_limit_cooldown_s if is_rate_limit else self.cooldown_s
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.setex(ok, int(cooldown), "1")
                pipe.delete(fk)
                await pipe.execute()

    def _record_mem(self, provider: str, is_rate_limit: bool) -> None:
        n = self._mem_failures.get(provider, 0) + 1
        self._mem_failures[provider] = n
        cooldown = self.rate_limit_cooldown_s if is_rate_limit else self.cooldown_s
        if is_rate_limit or n >= self.threshold:
            self._mem_cooldowns[provider] = time.time() + cooldown
            self._mem_failures[provider] = 0

    async def snapshot(self) -> dict[str, dict]:
        if self.redis is not None:
            try:
                keys = await self.redis.keys(f"{self.ns}:open:*")
                out: dict[str, dict] = {}
                for k in keys:
                    p = k.rsplit(":", 1)[-1]
                    out[p] = {"open": True,
                              "cooldown_remaining_s": max(0, await self.redis.ttl(k))}
                return out
            except Exception:
                pass
        now = time.time()
        return {
            p: {"open": True, "cooldown_remaining_s": max(0, u - now)}
            for p, u in self._mem_cooldowns.items() if u > now
        }


# ------------------------------------------------------------------------------
# 4 - External context registry (was core/infra.py)
# ------------------------------------------------------------------------------
_FETCH_TIMEOUT = 8.0
_CACHE_TTL = 3600
_SUMMARY_TOKEN_BUDGET = 1000
_PER_ITEM_CHAR_LIMIT = 200
_CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


@dataclass(slots=True)
class ContextItem:
    source: str
    type: str
    title: str = ""
    description: str = ""
    status: str = ""
    priority: str = ""
    updated_at: float = 0.0

    def render(self) -> str:
        line = f"[{self.source}] {self.title}"
        if self.status or self.priority:
            line += f" (Status: {self.status}, Priority: {self.priority})"
        if self.description:
            line += f"\n  {self.description[:_PER_ITEM_CHAR_LIMIT]}"
        return line


class ContextRegistry:
    """Jira / Linear / OpenAPI context fetch + cache. Failure isolated per source."""

    __slots__ = ("_db", "_http", "_redis", "_sources")

    def __init__(self, redis_client: Any = None, db_collection: Any = None) -> None:
        self._redis = redis_client
        self._db = db_collection
        self._http: httpx.AsyncClient | None = None
        self._sources = self._load_sources()

    async def get_context(self, user_id: str, workspace: str) -> str | None:
        if not self._sources:
            return None
        key = f"context:{user_id}:{workspace}"
        cached = await self._cache_get(key)
        if cached:
            return cached
        items = await self._fetch_all(user_id)
        if not items:
            return None
        summary = self._summarise(items, workspace)
        if not summary:
            return None
        await self._cache_set(key, summary)
        await self._audit(user_id, workspace, summary)
        return summary

    async def set_context(self, user_id: str, workspace: str, context: str) -> None:
        await self._cache_set(f"context:{user_id}:{workspace}", context)
        await self._audit(user_id, workspace, context)

    async def close(self) -> None:
        if self._http is not None:
            with contextlib.suppress(Exception):
                await self._http.aclose()
            self._http = None

    @staticmethod
    def _load_sources() -> dict[str, dict[str, str]]:
        s: dict[str, dict[str, str]] = {}
        if (u := os.getenv("JIRA_URL")) and (t := os.getenv("JIRA_TOKEN")):
            s["jira"] = {"url": u.rstrip("/"), "token": t,
                         "project": os.getenv("JIRA_PROJECT", "AXELR")}
        if (k := os.getenv("LINEAR_API_KEY")):
            s["linear"] = {"api_key": k}
        if (u := os.getenv("OPENAPI_CONTEXT_URL")):
            s["openapi"] = {"url": u, "token": os.getenv("OPENAPI_CONTEXT_TOKEN", "")}
        return s

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(_FETCH_TIMEOUT),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=4),
            )
        return self._http

    async def _fetch_all(self, user_id: str) -> list[ContextItem]:
        fetchers = []
        if "jira" in self._sources:    fetchers.append(self._fetch_jira(user_id))
        if "linear" in self._sources:  fetchers.append(self._fetch_linear(user_id))
        if "openapi" in self._sources: fetchers.append(self._fetch_openapi(user_id))
        if not fetchers:
            return []
        results = await asyncio.gather(*fetchers, return_exceptions=True)
        items: list[ContextItem] = []
        for r in results:
            if isinstance(r, Exception):
                logger.warning("context_fetch_error error=%s", r)
                continue
            items.extend(r)
        return items

    async def _fetch_jira(self, user_id: str) -> list[ContextItem]:
        cfg = self._sources["jira"]
        url = f"{cfg['url']}/rest/api/3/search"
        headers = {"Authorization": f"Bearer {cfg['token']}", "Accept": "application/json"}
        params = {"jql": f'project={cfg["project"]} AND assignee="{user_id}"',
                  "fields": "summary,description,status,priority", "maxResults": 10}
        try:
            c = await self._get_http()
            resp = await c.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("jira_fetch_failed error=%s", e)
            return []
        out: list[ContextItem] = []
        for issue in data.get("issues", [])[:10]:
            f = issue.get("fields", {})
            updated = f.get("updated", "")
            try:
                ua = datetime.fromisoformat(updated.replace("Z", "+00:00")).timestamp()
            except Exception:
                ua = 0.0
            out.append(ContextItem(
                source="jira", type="issue",
                title=str(f.get("summary", ""))[:200],
                description=str(f.get("description", "") or ""),
                status=str((f.get("status") or {}).get("name", "")),
                priority=str((f.get("priority") or {}).get("name", "")),
                updated_at=ua,
            ))
        return out

    async def _fetch_linear(self, user_id: str) -> list[ContextItem]:
        cfg = self._sources["linear"]
        headers = {"Authorization": cfg["api_key"], "Content-Type": "application/json"}
        query = """query V{viewer{assignedIssues(first:10){nodes{title description
                   state{name} priorityLabel updatedAt}}}}"""
        try:
            c = await self._get_http()
            r = await c.post("https://api.linear.app/graphql",
                             headers=headers, json={"query": query})
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning("linear_fetch_failed error=%s", e)
            return []
        nodes = ((data.get("data") or {}).get("viewer") or {}) \
                    .get("assignedIssues", {}).get("nodes", [])
        out: list[ContextItem] = []
        for n in nodes[:10]:
            updated = n.get("updatedAt", "")
            try:
                ua = datetime.fromisoformat(updated.replace("Z", "+00:00")).timestamp()
            except Exception:
                ua = 0.0
            out.append(ContextItem(
                source="linear", type="issue",
                title=str(n.get("title", ""))[:200],
                description=str(n.get("description", "") or ""),
                status=str((n.get("state") or {}).get("name", "")),
                priority=str(n.get("priorityLabel", "")),
                updated_at=ua,
            ))
        return out

    async def _fetch_openapi(self, user_id: str) -> list[ContextItem]:
        cfg = self._sources["openapi"]
        headers = {}
        if cfg.get("token"):
            headers["Authorization"] = f"Bearer {cfg['token']}"
        try:
            c = await self._get_http()
            r = await c.get(cfg["url"], headers=headers)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning("openapi_fetch_failed error=%s", e)
            return []
        if not isinstance(data, list):
            return []
        return [
            ContextItem(source="openapi", type="item",
                        title=str(row.get("title", ""))[:200],
                        description=str(row.get("description", "") or ""),
                        status=str(row.get("status", "")),
                        priority=str(row.get("priority", "")))
            for row in data[:10] if isinstance(row, dict)
        ]

    @staticmethod
    def _summarise(items: list[ContextItem], workspace: str) -> str:
        if not items:
            return ""
        prio = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        items.sort(key=lambda i: (-i.updated_at, prio.get(i.priority.strip().lower(), 4)))
        lines: list[str] = []
        used = 0
        for item in items:
            rendered = item.render()
            cost = _estimate_tokens(rendered)
            if used + cost > _SUMMARY_TOKEN_BUDGET:
                if used > 0:
                    lines.append("... (additional context truncated)")
                break
            lines.append(rendered)
            used += cost
        return "\n".join(lines).strip()

    async def _cache_get(self, key: str) -> str | None:
        if self._redis is None:
            return None
        try:
            return await self._redis.get(key)
        except Exception:
            return None

    async def _cache_set(self, key: str, value: str) -> None:
        if self._redis is None:
            return
        with contextlib.suppress(Exception):
            await self._redis.setex(key, _CACHE_TTL, value)

    async def _audit(self, user_id: str, workspace: str, summary: str) -> None:
        if self._db is None:
            return
        with contextlib.suppress(Exception):
            await self._db.update_one(
                {"userId": user_id, "workspace": workspace},
                {"$set": {"context": summary,
                          "updatedAt": datetime.now(timezone.utc)}},
                upsert=True,
            )


# ------------------------------------------------------------------------------
# 5 - Cloudflare AI embedding batch (vectorize integration)
# ------------------------------------------------------------------------------
async def cf_embed_batch(texts: list[str], model: str = "@cf/baai/bge-base-en-v1.5") -> list[list[float]] | None:
    """Batch generate embeddings via Cloudflare AI API."""
    api_token = os.getenv("CLOUDFLARE_API_TOKEN")
    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID")
    if not api_token or not account_id:
        logger.warning("cf_embed_missing_credentials")
        return None

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            tasks = []
            # Split into batches of 10 to avoid API limits
            for i in range(0, len(texts), 10):
                batch = texts[i:i+10]
                tasks.append(client.post(url, json={"text": batch}, headers=headers))
            
            results = await asyncio.gather(*tasks, return_exceptions=True)
            all_embeddings: list[list[float]] = []
            for r in results:
                if isinstance(r, Exception):
                    logger.error("cf_embed_batch_error error=%s", r)
                    continue
                if r.status_code != 200:
                    logger.error("cf_embed_batch_status status=%d", r.status_code)
                    continue
                data = r.json()
                if "result" in data and "data" in data["result"]:
                    for item in data["result"]["data"]:
                        if "embedding" in item:
                            all_embeddings.append(item["embedding"])
            return all_embeddings if len(all_embeddings) == len(texts) else None
    except Exception as e:
        logger.error("cf_embed_failed error=%s", e)
        return None
=======
# core/infra.py
"""
AXELR — Infra Layer (Redis · CircuitBreaker · ContextRegistry · state shim · chaos)
====================================================================================
One module for everything that talks to Redis, backs off on failure, and
degrades gracefully. The circuit breaker, the shared cache, the external
context registry, and the chaos injector all share the same contract:
    * Redis primary, in-memory fallback.
    * Never raise to the caller.
    * Bounded state, bounded retries, deterministic behavior.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import os
import random
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from cachetools import TTLCache

logger = logging.getLogger("axelr.infra")


# ═══════════════════════════════════════════════════════════════════════════
# §1 — State shim (was core/state_compat.py)
# ═══════════════════════════════════════════════════════════════════════════
def get_redis_client() -> Any:
    """Bridges core/* to api.state.redis_client without an import cycle."""
    try:
        state_mod = importlib.import_module("api.state")
        return getattr(state_mod.state, "redis_client", None)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Shared Redis cache + process-local TTL cache (was core/infra.py)
# ═══════════════════════════════════════════════════════════════════════════
local_cache: TTLCache = TTLCache(maxsize=1000, ttl=300)


class _DummyAsyncCache:
    """No-op fallback when Redis is unavailable. Same shape, zero risk."""
    async def get(self, *a, **k): return None
    async def set(self, *a, **k): pass
    async def delete(self, *a, **k): pass
    async def exists(self, *a, **k): return 0
    async def incr(self, *a, **k): return 1
    async def expire(self, *a, **k): pass
    async def setex(self, *a, **k): pass
    async def keys(self, *a, **k): return []
    async def ttl(self, *a, **k): return 0


shared_async_cache: Any | None = None


async def init_shared_redis_cache() -> None:
    """Pull the shared async Redis client from api.state. Never blocks import."""
    global shared_async_cache
    if shared_async_cache is not None:
        return
    client = get_redis_client()
    if client is not None:
        shared_async_cache = client
        logger.info("shared_redis_connected_via_state_client")
        return
    logger.warning("shared_redis_unavailable_falling_back_to_dummy_cache")
    shared_async_cache = _DummyAsyncCache()


async def get_redis_cache(key: str) -> Any:
    if not shared_async_cache:
        await init_shared_redis_cache()
    try:
        return await shared_async_cache.get(key)  # type: ignore[union-attr]
    except Exception:
        return None


async def set_redis_cache(key: str, value: Any, ttl: int | None = None) -> bool:
    if not shared_async_cache:
        await init_shared_redis_cache()
    try:
        if ttl is not None:
            await shared_async_cache.set(key, value, ex=ttl)  # type: ignore[union-attr]
        else:
            await shared_async_cache.set(key, value)  # type: ignore[union-attr]
        return True
    except Exception:
        return False


async def delete_redis_cache(key: str) -> bool:
    if not shared_async_cache:
        await init_shared_redis_cache()
    try:
        await shared_async_cache.delete(key)  # type: ignore[union-attr]
        return True
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Circuit breaker (was core/infra.py)
# ═══════════════════════════════════════════════════════════════════════════
class CircuitBreaker:
    """Distributed circuit breaker. Redis primary, memory fallback. Never raises."""

    def __init__(
        self,
        redis_client: Any | None,
        *,
        threshold: int = 3,
        cooldown_s: float = 60.0,
        rate_limit_cooldown_s: float = 1800.0,
        namespace: str = "axelr:cb",
    ) -> None:
        self.redis = redis_client
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self.rate_limit_cooldown_s = rate_limit_cooldown_s
        self.ns = namespace
        self._mem_failures: dict[str, int] = {}
        self._mem_cooldowns: dict[str, float] = {}
        self._lock = asyncio.Lock()

    def _fail_key(self, p: str) -> str: return f"{self.ns}:fail:{p}"
    def _open_key(self, p: str) -> str: return f"{self.ns}:open:{p}"

    async def is_available(self, provider: str) -> bool:
        if self.redis is not None:
            try:
                if await self.redis.get(self._open_key(provider)) is not None:
                    return False
                return True
            except Exception:
                pass
        return time.time() >= self._mem_cooldowns.get(provider, 0.0)

    async def record_success(self, provider: str) -> None:
        if self.redis is not None:
            try:
                await self.redis.delete(self._fail_key(provider))
                await self.redis.delete(self._open_key(provider))
            except Exception:
                pass
        async with self._lock:
            self._mem_failures[provider] = 0
            self._mem_cooldowns.pop(provider, None)

    async def record_failure(self, provider: str, *, is_rate_limit: bool = False) -> None:
        if self.redis is not None:
            try:
                return await self._record_redis(provider, is_rate_limit)
            except Exception:
                pass
        async with self._lock:
            self._record_mem(provider, is_rate_limit)

    async def _record_redis(self, provider: str, is_rate_limit: bool) -> None:
        fk, ok = self._fail_key(provider), self._open_key(provider)
        # Phase 1: unconditional INCR + EXPIRE (idempotent, safe).
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.incr(fk)
            pipe.expire(fk, int(self.rate_limit_cooldown_s * 2))
            result = await pipe.execute()
        n = int(result[0])
        # Phase 2: trip is atomic — either both writes land or neither.
        if is_rate_limit or n >= self.threshold:
            cooldown = self.rate_limit_cooldown_s if is_rate_limit else self.cooldown_s
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.setex(ok, int(cooldown), "1")
                pipe.delete(fk)
                await pipe.execute()

    def _record_mem(self, provider: str, is_rate_limit: bool) -> None:
        n = self._mem_failures.get(provider, 0) + 1
        self._mem_failures[provider] = n
        cooldown = self.rate_limit_cooldown_s if is_rate_limit else self.cooldown_s
        if is_rate_limit or n >= self.threshold:
            self._mem_cooldowns[provider] = time.time() + cooldown
            self._mem_failures[provider] = 0

    async def snapshot(self) -> dict[str, dict]:
        if self.redis is not None:
            try:
                keys = await self.redis.keys(f"{self.ns}:open:*")
                out: dict[str, dict] = {}
                for k in keys:
                    p = k.rsplit(":", 1)[-1]
                    out[p] = {"open": True,
                              "cooldown_remaining_s": max(0, await self.redis.ttl(k))}
                return out
            except Exception:
                pass
        now = time.time()
        return {
            p: {"open": True, "cooldown_remaining_s": max(0, u - now)}
            for p, u in self._mem_cooldowns.items() if u > now
        }


# ═══════════════════════════════════════════════════════════════════════════
# §4 — External context registry (was core/infra.py)
# ═══════════════════════════════════════════════════════════════════════════
_FETCH_TIMEOUT = 8.0
_CACHE_TTL = 3600
_SUMMARY_TOKEN_BUDGET = 1000
_PER_ITEM_CHAR_LIMIT = 200
_CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


@dataclass(slots=True)
class ContextItem:
    source: str
    type: str
    title: str = ""
    description: str = ""
    status: str = ""
    priority: str = ""
    updated_at: float = 0.0

    def render(self) -> str:
        line = f"[{self.source}] {self.title}"
        if self.status or self.priority:
            line += f" (Status: {self.status}, Priority: {self.priority})"
        if self.description:
            line += f"\n  {self.description[:_PER_ITEM_CHAR_LIMIT]}"
        return line


class ContextRegistry:
    """Jira / Linear / OpenAPI context fetch + cache. Failure isolated per source."""

    __slots__ = ("_db", "_http", "_redis", "_sources")

    def __init__(self, redis_client: Any = None, db_collection: Any = None) -> None:
        self._redis = redis_client
        self._db = db_collection
        self._http: httpx.AsyncClient | None = None
        self._sources = self._load_sources()

    async def get_context(self, user_id: str, workspace: str) -> str | None:
        if not self._sources:
            return None
        key = f"context:{user_id}:{workspace}"
        cached = await self._cache_get(key)
        if cached:
            return cached
        items = await self._fetch_all(user_id)
        if not items:
            return None
        summary = self._summarise(items, workspace)
        if not summary:
            return None
        await self._cache_set(key, summary)
        await self._audit(user_id, workspace, summary)
        return summary

    async def set_context(self, user_id: str, workspace: str, context: str) -> None:
        await self._cache_set(f"context:{user_id}:{workspace}", context)
        await self._audit(user_id, workspace, context)

    async def close(self) -> None:
        if self._http is not None:
            with contextlib.suppress(Exception):
                await self._http.aclose()
            self._http = None

    @staticmethod
    def _load_sources() -> dict[str, dict[str, str]]:
        s: dict[str, dict[str, str]] = {}
        if (u := os.getenv("JIRA_URL")) and (t := os.getenv("JIRA_TOKEN")):
            s["jira"] = {"url": u.rstrip("/"), "token": t,
                         "project": os.getenv("JIRA_PROJECT", "AXELR")}
        if (k := os.getenv("LINEAR_API_KEY")):
            s["linear"] = {"api_key": k}
        if (u := os.getenv("OPENAPI_CONTEXT_URL")):
            s["openapi"] = {"url": u, "token": os.getenv("OPENAPI_CONTEXT_TOKEN", "")}
        return s

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(_FETCH_TIMEOUT),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=4),
            )
        return self._http

    async def _fetch_all(self, user_id: str) -> list[ContextItem]:
        fetchers = []
        if "jira" in self._sources:    fetchers.append(self._fetch_jira(user_id))
        if "linear" in self._sources:  fetchers.append(self._fetch_linear(user_id))
        if "openapi" in self._sources: fetchers.append(self._fetch_openapi(user_id))
        if not fetchers:
            return []
        results = await asyncio.gather(*fetchers, return_exceptions=True)
        items: list[ContextItem] = []
        for r in results:
            if isinstance(r, Exception):
                logger.warning("context_fetch_error error=%s", r)
                continue
            items.extend(r)
        return items

    async def _fetch_jira(self, user_id: str) -> list[ContextItem]:
        cfg = self._sources["jira"]
        url = f"{cfg['url']}/rest/api/3/search"
        headers = {"Authorization": f"Bearer {cfg['token']}", "Accept": "application/json"}
        params = {"jql": f'project={cfg["project"]} AND assignee="{user_id}"',
                  "fields": "summary,description,status,priority", "maxResults": 10}
        try:
            c = await self._get_http()
            resp = await c.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("jira_fetch_failed error=%s", e)
            return []
        out: list[ContextItem] = []
        for issue in data.get("issues", [])[:10]:
            f = issue.get("fields", {})
            updated = f.get("updated", "")
            try:
                ua = datetime.fromisoformat(updated.replace("Z", "+00:00")).timestamp()
            except Exception:
                ua = 0.0
            out.append(ContextItem(
                source="jira", type="issue",
                title=str(f.get("summary", ""))[:200],
                description=str(f.get("description", "") or ""),
                status=str((f.get("status") or {}).get("name", "")),
                priority=str((f.get("priority") or {}).get("name", "")),
                updated_at=ua,
            ))
        return out

    async def _fetch_linear(self, user_id: str) -> list[ContextItem]:
        cfg = self._sources["linear"]
        headers = {"Authorization": cfg["api_key"], "Content-Type": "application/json"}
        query = """query V{viewer{assignedIssues(first:10){nodes{title description
                   state{name} priorityLabel updatedAt}}}}"""
        try:
            c = await self._get_http()
            r = await c.post("https://api.linear.app/graphql",
                             headers=headers, json={"query": query})
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning("linear_fetch_failed error=%s", e)
            return []
        nodes = ((data.get("data") or {}).get("viewer") or {}) \
                    .get("assignedIssues", {}).get("nodes", [])
        out: list[ContextItem] = []
        for n in nodes[:10]:
            updated = n.get("updatedAt", "")
            try:
                ua = datetime.fromisoformat(updated.replace("Z", "+00:00")).timestamp()
            except Exception:
                ua = 0.0
            out.append(ContextItem(
                source="linear", type="issue",
                title=str(n.get("title", ""))[:200],
                description=str(n.get("description", "") or ""),
                status=str((n.get("state") or {}).get("name", "")),
                priority=str(n.get("priorityLabel", "")),
                updated_at=ua,
            ))
        return out

    async def _fetch_openapi(self, user_id: str) -> list[ContextItem]:
        cfg = self._sources["openapi"]
        headers = {}
        if cfg.get("token"):
            headers["Authorization"] = f"Bearer {cfg['token']}"
        try:
            c = await self._get_http()
            r = await c.get(cfg["url"], headers=headers)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning("openapi_fetch_failed error=%s", e)
            return []
        if not isinstance(data, list):
            return []
        return [
            ContextItem(source="openapi", type="item",
                        title=str(row.get("title", ""))[:200],
                        description=str(row.get("description", "") or ""),
                        status=str(row.get("status", "")),
                        priority=str(row.get("priority", "")))
            for row in data[:10] if isinstance(row, dict)
        ]

    @staticmethod
    def _summarise(items: list[ContextItem], workspace: str) -> str:
        if not items:
            return ""
        prio = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        items.sort(key=lambda i: (-i.updated_at, prio.get(i.priority.strip().lower(), 4)))
        lines: list[str] = []
        used = 0
        for item in items:
            rendered = item.render()
            cost = _estimate_tokens(rendered)
            if used + cost > _SUMMARY_TOKEN_BUDGET:
                if used > 0:
                    lines.append("... (additional context truncated)")
                break
            lines.append(rendered)
            used += cost
        return "\n".join(lines).strip()

    async def _cache_get(self, key: str) -> str | None:
        if self._redis is None:
            return None
        try:
            return await self._redis.get(key)
        except Exception:
            return None

    async def _cache_set(self, key: str, value: str) -> None:
        if self._redis is None:
            return
        with contextlib.suppress(Exception):
            await self._redis.setex(key, _CACHE_TTL, value)

    async def _audit(self, user_id: str, workspace: str, summary: str) -> None:
        if self._db is None:
            return
        with contextlib.suppress(Exception):
            await self._db.update_one(
                {"userId": user_id, "workspace": workspace},
                {"$set": {"context": summary,
                          "updatedAt": datetime.now(timezone.utc)}},
                upsert=True,
            )




# ═══════════════════════════════════════════════════════════════════════════
# §6 — Cloudflare Workers AI embedding (shared by cache.py and memory.py)
# ═══════════════════════════════════════════════════════════════════════════
_CF_ACCT = (os.getenv("CLOUDFLARE_ACCOUNT_ID") or "").strip()
_CF_KEY = (os.getenv("CLOUDFLARE_API_KEY") or "").strip()
_CF_EMBED_MODEL = os.getenv("CF_EMBED_MODEL", "@cf/baai/bge-small-en-v1.5").strip()
_CF_EMBED_URL = ("https://api.cloudflare.com/client/v4/accounts/{acct}"
                 "/ai/run/{model}")
_CF_EMBED_TIMEOUT_S = float(os.getenv("CF_EMBED_TIMEOUT_S", "15.0"))


async def cf_embed_batch(
    texts: list[str], *, model: str | None = None,
    timeout_s: float | None = None,
) -> list[list[float]] | None:
    """
    Single Cloudflare Workers AI embedding call. Returns None on any failure
    (missing creds, network error, malformed response). Never raises.

    The three callers (`cache.CloudflareVectorizeCache._embed`,
    `memory.Memory._embed_remote`, `memory.RepoIndexer._embed_many`) all
    delegate here so the model, timeout, and response shape stay in lockstep.
    """
    if not texts or not (_CF_ACCT and _CF_KEY):
        return None
    url = _CF_EMBED_URL.format(acct=_CF_ACCT, model=model or _CF_EMBED_MODEL)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s or _CF_EMBED_TIMEOUT_S, connect=5.0),
        ) as client:
            r = await client.post(
                url,
                headers={"Authorization": f"Bearer {_CF_KEY}",
                         "Content-Type": "application/json"},
                json={"text": texts},
            )
            r.raise_for_status()
            data = r.json()
            vecs = (data.get("result") or {}).get("data") or []
            if not isinstance(vecs, list) or len(vecs) != len(texts):
                return None
            return [[float(x) for x in v] for v in vecs]
    except Exception as e:
        logger.warning("cf_embed_batch_failed error=%s", e)
        return None
# ═══════════════════════════════════════════════════════════════════════════
# Public surface
# ═══════════════════════════════════════════════════════════════════════════
def audit() -> dict[str, Any]:
    return {
        "cf_embed": {"configured": bool(_CF_ACCT and _CF_KEY),
                     "model": _CF_EMBED_MODEL},
        "redis": shared_async_cache is not None,
        "circuit_breaker_configured": True,
        "context_sources": list(ContextRegistry._load_sources().keys()),
    }


__all__ = [
    # state shim
    "get_redis_client",
    # cache
    "local_cache", "shared_async_cache", "init_shared_redis_cache",
    "get_redis_cache", "set_redis_cache", "delete_redis_cache",
    # circuit breaker
    "CircuitBreaker",
    # context registry
    "ContextItem", "ContextRegistry",
    # audit
    "audit",
]
>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
