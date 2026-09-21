# core/context_registry.py
"""
AXELR Context Registry
======================
External context retrieval (Jira, Linear, OpenAPI) with Redis caching.

Design goals:
    * Zero blocking I/O — every fetch is bounded by a hard timeout.
    * Failure isolation — a broken Jira never blocks a design request.
    * Aggressive caching — 1h TTL in Redis, best-effort Mongo audit.
    * Token budget — summaries hard-capped at ~1000 tokens.

RAM footprint: < 5 MB.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger("axelr-unified")

__all__ = ["ContextItem", "ContextRegistry"]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_FETCH_TIMEOUT = 8.0
_CACHE_TTL = 3600
_SUMMARY_CHAR_BUDGET = 4000      # ≈1000 tokens at ~4 chars/token
_PER_ITEM_CHAR_LIMIT = 200


# ---------------------------------------------------------------------------
# Data structure
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ContextItem:
    """Normalised context item across all sources."""
    source: str
    type: str
    title: str = ""
    description: str = ""
    status: str = ""
    priority: str = ""

    def render(self) -> str:
        line = f"[{self.source}] {self.title}"
        if self.status or self.priority:
            line += f" (Status: {self.status}, Priority: {self.priority})"
        if self.description:
            line += f"\n  {self.description[:_PER_ITEM_CHAR_LIMIT]}"
        return line


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ContextRegistry:
    """
    Fetches, summarises, and caches external context per (user, workspace).

    Parameters
    ----------
    redis_client : Any
        An async Redis client (``redis.asyncio``). May be ``None``.
    db_collection : Any
        A Motor collection for audit persistence. May be ``None``.
    """

    __slots__ = ("_db", "_http", "_redis", "_sources")

    def __init__(self, redis_client: Any = None, db_collection: Any = None) -> None:
        self._redis = redis_client
        self._db = db_collection
        self._http: httpx.AsyncClient | None = None
        self._sources = self._load_sources()

    # -- public API ---------------------------------------------------------

    async def get_context(self, user_id: str, workspace: str) -> str | None:
        """
        Return a token-bounded context summary for the given user/workspace.
        Returns ``None`` if no sources are configured or all fetches fail.
        """
        if not self._sources:
            return None

        cache_key = f"context:{user_id}:{workspace}"

        # 1. Cache lookup
        cached = await self._cache_get(cache_key)
        if cached:
            return cached

        # 2. Fetch from sources in parallel
        items = await self._fetch_all(user_id)
        if not items:
            return None

        # 3. Summarise
        summary = self._summarise(items, workspace)
        if not summary:
            return None

        # 4. Persist (cache + audit)
        await self._cache_set(cache_key, summary)
        await self._audit(user_id, workspace, summary)
        return summary

    async def set_context(self, user_id: str, workspace: str, context: str) -> None:
        """Manually prime the context for a user (used by settings UI)."""
        await self._cache_set(f"context:{user_id}:{workspace}", context)
        await self._audit(user_id, workspace, context)

    async def close(self) -> None:
        """Release the internal HTTP client. Call on app shutdown."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # -- internal: sources --------------------------------------------------

    @staticmethod
    def _load_sources() -> dict[str, dict[str, str]]:
        """Read source configuration from environment."""
        sources: dict[str, dict[str, str]] = {}

        jira_url = os.getenv("JIRA_URL")
        jira_token = os.getenv("JIRA_TOKEN")
        if jira_url and jira_token:
            sources["jira"] = {
                "url": jira_url.rstrip("/"),
                "token": jira_token,
                "project": os.getenv("JIRA_PROJECT", "AXELR"),
            }

        linear_key = os.getenv("LINEAR_API_KEY")
        if linear_key:
            sources["linear"] = {"api_key": linear_key}

        openapi_url = os.getenv("OPENAPI_CONTEXT_URL")
        if openapi_url:
            sources["openapi"] = {
                "url": openapi_url,
                "token": os.getenv("OPENAPI_CONTEXT_TOKEN", ""),
            }

        return sources

    async def _get_http(self) -> httpx.AsyncClient:
        """Lazy-initialise a shared HTTP client."""
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(_FETCH_TIMEOUT),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=4),
            )
        return self._http

    async def _fetch_all(self, user_id: str) -> list[ContextItem]:
        """Run all source fetches concurrently with failure isolation."""
        fetchers = []
        if "jira" in self._sources:
            fetchers.append(self._fetch_jira(user_id))
        if "linear" in self._sources:
            fetchers.append(self._fetch_linear(user_id))
        if "openapi" in self._sources:
            fetchers.append(self._fetch_openapi(user_id))

        if not fetchers:
            return []

        results = await asyncio.gather(*fetchers, return_exceptions=True)
        items: list[ContextItem] = []
        for res in results:
            if isinstance(res, Exception):
                logger.warning("context fetch error: %s", res)
                continue
            items.extend(res)
        return items

    # -- internal: individual sources --------------------------------------

    async def _fetch_jira(self, user_id: str) -> list[ContextItem]:
        cfg = self._sources.get("jira")
        if not cfg:
            return []
        url = f"{cfg['url']}/rest/api/3/search"
        headers = {"Authorization": f"Bearer {cfg['token']}", "Accept": "application/json"}
        params = {
            "jql": f'project={cfg["project"]} AND assignee="{user_id}"',
            "fields": "summary,description,status,priority",
            "maxResults": 10,
        }
        try:
            client = await self._get_http()
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("jira fetch failed: %s", e)
            return []

        items: list[ContextItem] = []
        for issue in data.get("issues", [])[:10]:
            fields = issue.get("fields", {})
            status = (fields.get("status") or {}).get("name", "")
            priority = (fields.get("priority") or {}).get("name", "")
            items.append(
                ContextItem(
                    source="jira",
                    type="issue",
                    title=str(fields.get("summary", ""))[:200],
                    description=str(fields.get("description", "") or ""),
                    status=str(status),
                    priority=str(priority),
                )
            )
        return items

    async def _fetch_linear(self, user_id: str) -> list[ContextItem]:
        cfg = self._sources.get("linear")
        if not cfg:
            return []
        url = "https://api.linear.app/graphql"
        headers = {
            "Authorization": cfg["api_key"],   # Linear uses raw API key, not Bearer
            "Content-Type": "application/json",
        }
        # NOTE: Linear GraphQL does NOT support string-formatted user_id — use viewerId
        query = """
        query Viewer {
          viewer {
            assignedIssues(first: 10) {
              nodes {
                title
                description
                state { name }
                priorityLabel
              }
            }
          }
        }
        """
        try:
            client = await self._get_http()
            resp = await client.post(url, headers=headers, json={"query": query})
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("linear fetch failed: %s", e)
            return []

        nodes = (data.get("data") or {}).get("viewer", {}).get("assignedIssues", {}).get("nodes", [])
        items: list[ContextItem] = []
        for n in nodes[:10]:
            items.append(
                ContextItem(
                    source="linear",
                    type="issue",
                    title=str(n.get("title", ""))[:200],
                    description=str(n.get("description", "") or ""),
                    status=str((n.get("state") or {}).get("name", "")),
                    priority=str(n.get("priorityLabel", "")),
                )
            )
        return items

    async def _fetch_openapi(self, user_id: str) -> list[ContextItem]:
        cfg = self._sources.get("openapi")
        if not cfg:
            return []
        headers = {}
        if cfg.get("token"):
            headers["Authorization"] = f"Bearer {cfg['token']}"
        try:
            client = await self._get_http()
            resp = await client.get(cfg["url"], headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("openapi fetch failed: %s", e)
            return []

        if not isinstance(data, list):
            return []
        items: list[ContextItem] = []
        for row in data[:10]:
            if not isinstance(row, dict):
                continue
            items.append(
                ContextItem(
                    source="openapi",
                    type="item",
                    title=str(row.get("title", ""))[:200],
                    description=str(row.get("description", "") or ""),
                    status=str(row.get("status", "")),
                    priority=str(row.get("priority", "")),
                )
            )
        return items

    # -- internal: summary --------------------------------------------------

    @staticmethod
    def _summarise(items: list[ContextItem], workspace: str) -> str:
        """Render items into a token-bounded text block."""
        if not items:
            return ""

        # Design/data workspaces favour different priorities
        if workspace == "data":
            items.sort(key=lambda i: (i.priority != "High", i.source))
        else:
            items.sort(key=lambda i: i.source)

        lines: list[str] = []
        running = 0
        for item in items:
            rendered = item.render()
            if running + len(rendered) > _SUMMARY_CHAR_BUDGET:
                lines.append("... (truncated)")
                break
            lines.append(rendered)
            running += len(rendered)

        return "\n".join(lines).strip()

    # -- internal: persistence ---------------------------------------------

    async def _cache_get(self, key: str) -> str | None:
        if self._redis is None:
            return None
        try:
            return await self._redis.get(key)
        except Exception as e:
            logger.warning("redis get failed: %s", e)
            return None

    async def _cache_set(self, key: str, value: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.setex(key, _CACHE_TTL, value)
        except Exception as e:
            logger.warning("redis set failed: %s", e)

    async def _audit(self, user_id: str, workspace: str, summary: str) -> None:
        if self._db is None:
            return
        try:
            await self._db.update_one(
                {"userId": user_id, "workspace": workspace},
                {
                    "$set": {
                        "context": summary,
                        "updatedAt": datetime.now(timezone.utc),
                    }
                },
                upsert=True,
            )
        except Exception as e:
            logger.warning("audit write failed: %s", e)