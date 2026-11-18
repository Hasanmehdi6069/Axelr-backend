# core/infra.py
"""
AXELR — Infrastructure Layer (Redis · ContextRegistry · embeddings)
=====================================================================
Single source of truth for all external-service communication.

    §1  Redis shim        api.state client, transparent dummy fallback
    §2  Shared local TTL  in-process cache for non-critical reads
    §3  ContextRegistry   Jira / Linear / OpenAPI fetch + summary cache
    §4  cf_embed_batch    single Cloudflare Workers AI embedding call

Contract (every section):
    * Redis primary, in-memory fallback.
    * Never raise to the caller.
    * Bounded state, bounded retries, deterministic behaviour.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from cachetools import TTLCache

logger = logging.getLogger("axelr.infra")


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Redis shim (bridge to api.state without an import cycle)
# ═══════════════════════════════════════════════════════════════════════════
def get_redis_client() -> Any:
    """Return ``api.state.state.redis_client`` or None. Never raises."""
    try:
        state_mod = importlib.import_module("api.state")
        return getattr(state_mod.state, "redis_client", None)
    except Exception:
        return None


class _DummyAsyncCache:
    """No-op stand-in with the same async surface as a Redis client."""
    def __init__(self):
        self._data: dict[str, Any] = {}
        self._ttl: dict[str, float] = {}

    async def get(self, key: str):
        import time
        if key in self._ttl and time.time() > self._ttl[key]:
            del self._data[key]
            del self._ttl[key]
            return None
        return self._data.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None):
        import time
        self._data[key] = value
        if ex is not None:
            self._ttl[key] = time.time() + ex

    async def delete(self, *keys):
        for key in keys:
            self._data.pop(key, None)
            self._ttl.pop(key, None)

    async def exists(self, *keys):
        return sum(1 for k in keys if k in self._data)

    async def incr(self, key: str, amount: int = 1):
        self._data[key] = self._data.get(key, 0) + amount
        return self._data[key]

    async def expire(self, key: str, ttl: int):
        import time
        if key in self._data:
            self._ttl[key] = time.time() + ttl

    async def setex(self, key: str, ttl: int, value: Any):
        await self.set(key, value, ex=ttl)

    async def keys(self, pattern: str = "*"):
        import fnmatch
        return [k for k in self._data.keys() if fnmatch.fnmatch(k, pattern)]

    async def ttl(self, key: str):
        import time
        if key not in self._data:
            return -2
        if key not in self._ttl:
            return -1
        return max(0, int(self._ttl[key] - time.time()))


shared_async_cache: Any | None = None


async def init_shared_redis_cache() -> Any:
    """Resolve the shared async Redis client. Never blocks import."""
    global shared_async_cache
    if shared_async_cache is not None:
        return shared_async_cache
    client = get_redis_client()
    if client is not None:
        shared_async_cache = client
        logger.info("shared_redis_connected_via_state_client")
        return shared_async_cache
    logger.warning("shared_redis_unavailable_falling_back_to_dummy_cache")
    shared_async_cache = _DummyAsyncCache()
    return shared_async_cache


async def _ensure_cache() -> Any:
    if shared_async_cache is None:
        await init_shared_redis_cache()
    return shared_async_cache


async def get_redis_cache(key: str) -> Any:
    try:
        return await (await _ensure_cache()).get(key)
    except Exception:
        return None


async def set_redis_cache(key: str, value: Any, ttl: int | None = None) -> bool:
    try:
        cache = await _ensure_cache()
        if ttl is not None:
            await cache.set(key, value, ex=ttl)
        else:
            await cache.set(key, value)
        return True
    except Exception:
        return False


async def delete_redis_cache(key: str) -> bool:
    try:
        await (await _ensure_cache()).delete(key)
        return True
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Shared local TTL cache (bounded, evicts on its own)
# ═══════════════════════════════════════════════════════════════════════════
local_cache: TTLCache = TTLCache(maxsize=1000, ttl=300)


# ═══════════════════════════════════════════════════════════════════════════
# §3 — ContextRegistry (Jira / Linear / OpenAPI)
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
    priority: int = 0
    updated_at: float = 0.0

    def render(self) -> str:
        line = f"[{self.source}] {self.title}"
        if self.status or self.priority:
            line += f" (Status: {self.status}, Priority: {self.priority})"
        if self.description:
            line += f"\n  {self.description[:_PER_ITEM_CHAR_LIMIT]}"
        return line


class ContextRegistryEntry:
    def __init__(self, context: str, priority: int = 1):
        self.context = context
        self.priority = priority


class ContextRegistry:
    """Jira / Linear / OpenAPI fetch + summary cache. Failure isolated per source."""

    __slots__ = ("_db", "_http", "_redis", "_sources", "_contexts")

    def __init__(self, redis_client: Any = None, db_collection: Any = None) -> None:
        self._redis = redis_client
        self._db = db_collection
        self._http: httpx.Client | None = None
        self._sources = {}
        self._contexts: dict[tuple[str, str], list[ContextRegistryEntry]] = {}

    def get_context(self, user_id: str, workspace: str) -> str | None:
        key = (user_id, workspace)
        if key in self._contexts and self._contexts[key]:
            return self._contexts[key][0].context
        return None

    def set_context(self, user_id: str, workspace: str, context: str, priority: int = 1) -> None:
        key = (user_id, workspace)
        if key not in self._contexts:
            self._contexts[key] = []
        self._contexts[key].append(ContextRegistryEntry(context, priority))

    def get_all(self, user_id: str, workspace: str) -> list[ContextRegistryEntry]:
        entries = []
        for (u, w), items in self._contexts.items():
            if u == user_id and w == workspace:
                entries.extend(items)
        entries.sort(key=lambda x: -x.priority)
        return entries

    def summarise(self, user_id: str, workspace: str, max_tokens: int = 1000) -> str:
        entries = self.get_all(user_id, workspace)
        total_tokens = 0
        summary_parts = []
        for entry in entries:
            entry_text = entry.context
            entry_tokens = _estimate_tokens(entry_text)
            if total_tokens + entry_tokens > max_tokens:
                remaining = max_tokens - total_tokens
                summary_parts.append(entry_text[:remaining * _CHARS_PER_TOKEN])
                break
            summary_parts.append(entry_text)
            total_tokens += entry_tokens
        return "\n".join(summary_parts)

    def audit(self) -> dict[str, Any]:
        total_tokens = 0
        total_entries = 0
        for items in self._contexts.values():
            for entry in items:
                total_entries += 1
                total_tokens += _estimate_tokens(entry.context)
        return {"entries": total_entries, "total_tokens": total_tokens, "sources": list(self._sources.keys())}

    def close(self) -> None:
        if self._http is not None:
            with contextlib.suppress(Exception):
                self._http.close()
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
        try:
            c = await self._get_http()
            resp = await c.get(
                f"{cfg['url']}/rest/api/3/search",
                headers={"Authorization": f"Bearer {cfg['token']}", "Accept": "application/json"},
                params={"jql": f'project={cfg["project"]} AND assignee="{user_id}"',
                        "fields": "summary,description,status,priority", "maxResults": 10},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("jira_fetch_failed error=%s", e)
            return []
        out: list[ContextItem] = []
        for issue in data.get("issues", [])[:10]:
            f = issue.get("fields", {})
            out.append(ContextItem(
                source="jira", type="issue",
                title=str(f.get("summary", ""))[:200],
                description=str(f.get("description", "") or ""),
                status=str((f.get("status") or {}).get("name", "")),
                priority=str((f.get("priority") or {}).get("name", "")),
            ))
        return out

    async def _fetch_linear(self, user_id: str) -> list[ContextItem]:
        cfg = self._sources["linear"]
        query = """query V{viewer{assignedIssues(first:10){nodes{title description
                   state{name} priorityLabel updatedAt}}}}"""
        try:
            c = await self._get_http()
            r = await c.post("https://api.linear.app/graphql",
                             headers={"Authorization": cfg["api_key"],
                                      "Content-Type": "application/json"},
                             json={"query": query})
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning("linear_fetch_failed error=%s", e)
            return []
        nodes = ((data.get("data") or {}).get("viewer") or {}).get(
            "assignedIssues", {}).get("nodes", [])
        return [
            ContextItem(source="linear", type="issue",
                        title=str(n.get("title", ""))[:200],
                        description=str(n.get("description", "") or ""),
                        status=str((n.get("state") or {}).get("name", "")),
                        priority=str(n.get("priorityLabel", "")))
            for n in nodes[:10]
        ]

    async def _fetch_openapi(self, user_id: str) -> list[ContextItem]:
        cfg = self._sources["openapi"]
        headers = {"Authorization": f"Bearer {cfg['token']}"} if cfg.get("token") else {}
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
    def _summarise(items: list[ContextItem]) -> str:
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
# §4 — Context Registry (thread-safe in-memory fallback)
# ═══════════════════════════════════════════════════════════════════════════
import threading

_CONTEXT_REGISTRY_LOCK = threading.Lock()
_CONTEXT_REGISTRY: dict[tuple[str, str], list[tuple[int, str]]] = {}


def context_get(user_id: str, workspace: str) -> str | None:
    """Thread-safe get context from in-memory registry."""
    key = (user_id, workspace)
    with _CONTEXT_REGISTRY_LOCK:
        entries = _CONTEXT_REGISTRY.get(key, [])
        if not entries:
            return None
        return "\n".join(text for _, text in sorted(entries, key=lambda x: x[0], reverse=True))


def context_set(user_id: str, workspace: str, text: str, priority: int = 0) -> None:
    """Thread-safe set context in in-memory registry."""
    key = (user_id, workspace)
    with _CONTEXT_REGISTRY_LOCK:
        if key not in _CONTEXT_REGISTRY:
            _CONTEXT_REGISTRY[key] = []
        _CONTEXT_REGISTRY[key].append((priority, text))


def context_summarise(user_id: str, workspace: str, max_tokens: int = 1000) -> str:
    """Summarise context to fit within token budget."""
    context = context_get(user_id, workspace) or ""
    words = context.split()
    if len(words) <= max_tokens:
        return context
    return " ".join(words[:max_tokens])


def context_get_all(user_id: str, workspace: str) -> list[str]:
    """Get all context entries sorted by priority."""
    key = (user_id, workspace)
    with _CONTEXT_REGISTRY_LOCK:
        entries = _CONTEXT_REGISTRY.get(key, [])
        return [text for _, text in sorted(entries, key=lambda x: x[0], reverse=True)]


def context_audit() -> dict[str, Any]:
    """Return context registry statistics."""
    with _CONTEXT_REGISTRY_LOCK:
        total_tokens = sum(len(text.split()) for entries in _CONTEXT_REGISTRY.values() for _, text in entries)
        total_entries = sum(len(entries) for entries in _CONTEXT_REGISTRY.values())
        return {"entries": total_entries, "total_tokens": total_tokens, "workspaces": len(_CONTEXT_REGISTRY)}


# ═══════════════════════════════════════════════════════════════════════════
# §5 — Cloudflare Workers AI embedding (shared primitive)
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
    Single Cloudflare Workers AI embedding call.
    Returns None on any failure (missing creds, network, malformed).
    Never raises.
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
# Audit
# ═══════════════════════════════════════════════════════════════════════════
def audit() -> dict[str, Any]:
    return {
        "redis_available": get_redis_client() is not None,
        "shared_cache_connected": shared_async_cache is not None,
    }