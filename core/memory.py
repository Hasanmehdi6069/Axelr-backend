# core/memory.py
"""
AXELR — Tiered Session Memory Engine (canonical)
=================================================
Single entry point for conversation memory. Three storage layers, each
independent and optional:

    ┌──────────────┬──────────────────────────────────────────────┐
    │ Redis        │ recent exchanges (list, TTL 7 d, sub-ms)     │
    │ Vectorize    │ semantic index + Workers AI embeddings       │
    │ Supabase     │ cold audit copy (SQL, queryable)             │
    └──────────────┴──────────────────────────────────────────────┘

Never raises. Every method has a degradation path.

Breaking change vs. the pre-consolidation file
----------------------------------------------
``get_memory()`` is **async only**. The old module defined an ``async``
factory and then redefined a synchronous one just below it, which shadowed
the async version and made every ``await get_memory()`` call crash with
``TypeError: object Memory can't be used in 'await' expression``. The
sync override has been removed. Any caller doing ``mem = get_memory()``
without ``await`` must be updated. The pipeline already awaits.

What was removed (documented, not hidden)
-----------------------------------------
* ``FreeTierChatMemory``  — dead: nobody imported it.
* ``ProTierCrossSessionMemory`` — had a runtime hazard: its
  ``SessionMemoryEngine`` did ``self._pro_memory.__class__ = type(...)``
  on a ``Memory`` instance whose ``__slots__`` differ from the target
  class. That raises ``TypeError: __class__ assignment: 'ProTierEnhancedMemory'
  object layout differs from 'Memory'`` on every construction.
* ``SessionMemoryEngine`` — same crash as above; nobody imported it.
* Second ``MemoryItem`` dataclass definition — dead shadow.

Companion modules:
    * ``core/repo_indexer`` — imports ``_VectorizeClient`` and ``_sha``
      from here. Their canonical home is this module.
    * ``core.infra`` — supplies ``cf_embed_batch`` and ``get_redis_client``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("axelr.memory")


# Add placeholder for RepoIndexer if it's not implemented yet
class RepoIndexer:
    """Placeholder for RepoIndexer to prevent import errors"""

    pass


_CF_BASE = "https://api.cloudflare.com/client/v4/accounts/{acct}"
_VECTOR_URL = _CF_BASE + "/vectorize/v2/indexes/{index}{suffix}"

_EMBED_MODEL = os.getenv("MEMORY_EMBED_MODEL", "@cf/baai/bge-small-en-v1.5")
_CHAT_INDEX = (os.getenv("CONV_MEMORY_INDEX") or "axelr-conversation-memory").strip()
_FACT_INDEX = (os.getenv("MEMORY_BUS_INDEX") or "axelr-memory-bus").strip()

_MAX_TEXT_CHARS = 4000
_DEFAULT_TOP_K = 6
_DEFAULT_MIN_SCORE = 0.0
_DEFAULT_RECENT_LIMIT = 20
_EMBED_CACHE_TTL = 86_400
_RECENT_TTL = 86_400 * 7
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


def _sha(text: str) -> str:
    """SHA-256 of ``text`` as lowercase hex. Used as stable memory id seed."""
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _make_id(user_id: str, session_id: str, text: str) -> str:
    """Build a 32-char vector id from user, session, time, and text."""
    return _sha(f"{user_id}|{session_id}|{time.time_ns()}|{text}")[:32]


# ═══════════════════════════════════════════════════════════════════════════
# Shared Vectorize client — canonical home
# ═══════════════════════════════════════════════════════════════════════════
class _VectorizeClient:
    """Minimal Cloudflare Vectorize REST wrapper.

    Used by this module and by ``core/repo_indexer``. Shared surface for
    upsert / query / delete-by-ids. Never raises — every method returns
    an empty value or ``False`` on error.
    """

    __slots__ = ("_acct", "_key", "_http", "_index")

    def __init__(self, acct: str, key: str, index: str, http: httpx.AsyncClient) -> None:
        self._acct = acct
        self._key = key
        self._index = index
        self._http = http

    @property
    def enabled(self) -> bool:
        """True when account, key, and index are all set."""
        return bool(self._acct and self._key and self._index)

    def _url(self, suffix: str) -> str:
        return _VECTOR_URL.format(acct=self._acct, index=self._index, suffix=suffix)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}

    async def upsert(self, vectors: list[dict[str, Any]], timeout: float = 30.0) -> bool:
        """Insert or update vectors. Returns ``False`` on any failure."""
        if not self.enabled or not vectors:
            return False
        try:
            r = await self._http.post(
                self._url("/upsert"), headers=self._headers(), json={"vectors": vectors}, timeout=timeout
            )
            r.raise_for_status()
            return True
        except Exception as e:
            logger.warning("vectorize_upsert_failed error=%s", e)
            return False

    async def query(
        self,
        vector: Sequence[float],
        *,
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
        timeout: float = 15.0,
    ) -> list[dict[str, Any]]:
        """Vector search. Returns empty list on failure."""
        if not self.enabled:
            return []
        body: dict[str, Any] = {
            "vector": list(vector),
            "topK": int(top_k),
            "returnValues": False,
            "returnMetadata": "all",
        }
        if filters:
            body["filter"] = filters
        try:
            r = await self._http.post(self._url("/query"), headers=self._headers(), json=body, timeout=timeout)
            r.raise_for_status()
            return list(((r.json().get("result") or {}).get("matches")) or [])
        except Exception as e:
            logger.warning("vectorize_query_failed error=%s", e)
            return []

    async def delete_by_ids(self, ids: list[str], timeout: float = 30.0) -> bool:
        """Delete vectors by id list. Returns ``False`` on failure."""
        if not self.enabled or not ids:
            return False
        try:
            r = await self._http.post(
                self._url("/delete_by_ids"), headers=self._headers(), json={"ids": ids}, timeout=timeout
            )
            r.raise_for_status()
            return True
        except Exception as e:
            logger.warning("vectorize_delete_failed error=%s", e)
            return False


# ═══════════════════════════════════════════════════════════════════════════
# MemoryItem — single canonical dataclass
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class MemoryItem:
    """One recalled memory fragment.

    Returned by ``Memory.recall`` / ``Memory.recent``. ``render()`` gives
    the ``[role] text`` form used inside a prompt.
    """

    id: str
    text: str
    role: str
    session_id: str
    score: float
    created_at: float

    def render(self) -> str:
        return f"[{self.role}] {self.text}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "role": self.role,
            "session_id": self.session_id,
            "score": round(self.score, 4),
            "created_at": self.created_at,
        }


# ═══════════════════════════════════════════════════════════════════════════
# Memory — chat + fact memory engine
# ═══════════════════════════════════════════════════════════════════════════
class Memory:
    """Multi-layer chat memory.

    Contract:
        * Every method catches its own exceptions and returns a safe
          default. ``remember`` returns ``str | None`` (id or None).
        * ``vector_enabled`` is a cheap read of the Vectorize config.
        * ``close()`` releases the HTTP client only if this instance
          created it.
    """

    __slots__ = (
        "_chat_vec",
        "_fact_vec",
        "_acct",
        "_key",
        "_http",
        "_owns_http",
        "_redis",
        "_supabase",
        "_recent_limit",
        "_embed_prefix",
        "_recent_prefix",
        "_ids_prefix",
        "_chat_index",
        "_fact_index",
    )

    def __init__(
        self,
        *,
        account_id: str | None = None,
        api_key: str | None = None,
        chat_index: str | None = None,
        fact_index: str | None = None,
        redis_client: Any | None = None,
        http_client: httpx.AsyncClient | None = None,
        recent_limit: int = _DEFAULT_RECENT_LIMIT,
    ) -> None:
        self._acct = (account_id or os.getenv("CLOUDFLARE_ACCOUNT_ID") or "").strip()
        self._key = (api_key or os.getenv("CLOUDFLARE_API_KEY") or "").strip()
        self._chat_index = (chat_index or _CHAT_INDEX).strip()
        self._fact_index = (fact_index or _FACT_INDEX).strip()
        if http_client is None:
            self._http = httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT, limits=httpx.Limits(max_connections=10, max_keepalive_connections=5)
            )
            self._owns_http = True
        else:
            self._http = http_client
            self._owns_http = False
        self._chat_vec = _VectorizeClient(self._acct, self._key, self._chat_index, self._http)
        self._fact_vec = _VectorizeClient(self._acct, self._key, self._fact_index, self._http)
        self._redis = redis_client
        self._supabase: Any = None
        self._recent_limit = max(1, int(recent_limit))
        self._embed_prefix = "axelr:convmem:embed"
        self._recent_prefix = "axelr:convmem:recent"
        self._ids_prefix = "axelr:convmem:ids"

    def attach_supabase(self, client: Any) -> None:
        """Register a Supabase client for cold-copy writes. Never raises."""
        self._supabase = client

    @property
    def vector_enabled(self) -> bool:
        return self._chat_vec.enabled

    # ── remember ────────────────────────────────────────────────────
    async def remember(
        self,
        *,
        user_id: str,
        session_id: str,
        role: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """Persist one message across all enabled layers.

        Returns the vector id (or ``None`` if text was empty / every
        layer failed). Never raises.
        """
        try:
            if not text or not text.strip():
                return None
            text = text.strip()[:_MAX_TEXT_CHARS]
            vid = _make_id(user_id, session_id, text)
            now = time.time()

            await self._recent_push(
                user_id,
                session_id,
                MemoryItem(id=vid, text=text, role=role, session_id=session_id, score=1.0, created_at=now),
            )

            if self._chat_vec.enabled:
                vec = await self._embed(text)
                if vec:
                    md: dict[str, Any] = {
                        "user_id": user_id[:64],
                        "session_id": session_id[:64],
                        "role": role[:16],
                        "text": text,
                        "created_at": float(now),
                    }
                    if metadata:
                        for k, v in metadata.items():
                            if isinstance(v, (str, int, float, bool)) and len(str(v)) < 256:
                                md[str(k)[:32]] = v
                    if await self._chat_vec.upsert([{"id": vid, "values": vec, "metadata": md}]):
                        await self._track_id(user_id, session_id, vid)

            if self._supabase is not None:
                with contextlib.suppress(Exception):
                    await self._supabase.execute(
                        "insert into memory "
                        "(user_id, session_id, role, text, created_at) "
                        "values ($1,$2,$3,$4,to_timestamp($5))",
                        user_id,
                        session_id,
                        role,
                        text,
                        now,
                    )
            return vid
        except Exception as e:
            logger.warning("memory_remember_failed error=%s", e)
            return None

    # ── recall ──────────────────────────────────────────────────────
    async def recall(
        self,
        *,
        user_id: str,
        query: str,
        top_k: int = _DEFAULT_TOP_K,
        session_id: str | None = None,
        min_score: float = _DEFAULT_MIN_SCORE,
    ) -> list[MemoryItem]:
        """Semantic search over past messages. Empty list on failure."""
        if not query or not query.strip():
            return []
        if not self._chat_vec.enabled:
            return await self.recent(user_id, session_id or "", limit=top_k)
        try:
            vec = await self._embed(query)
            if not vec:
                return await self.recent(user_id, session_id or "", limit=top_k)
            filters: dict[str, Any]
            if session_id:
                filters = {
                    "$and": [
                        {"user_id": {"$eq": user_id}},
                        {"session_id": {"$eq": session_id}},
                    ]
                }
            else:
                filters = {"user_id": {"$eq": user_id}}
            matches = await self._chat_vec.query(vec, top_k=top_k, filters=filters)
            out: list[MemoryItem] = []
            for m in matches:
                score = float(m.get("score", 0.0))
                if score < min_score:
                    continue
                md = m.get("metadata") or {}
                out.append(
                    MemoryItem(
                        id=str(m.get("id", "")),
                        text=str(md.get("text", "")),
                        role=str(md.get("role", "user")),
                        session_id=str(md.get("session_id", "")),
                        score=score,
                        created_at=float(md.get("created_at", 0.0)),
                    )
                )
            return out
        except Exception as e:
            logger.warning("memory_recall_failed error=%s", e)
            return []

    # ── recent ──────────────────────────────────────────────────────
    async def recent(
        self,
        user_id: str,
        session_id: str,
        *,
        limit: int = 10,
    ) -> list[MemoryItem]:
        """Most-recent messages from Redis. Empty list if Redis is absent."""
        if self._redis is None:
            return []
        key = f"{self._recent_prefix}:{user_id}:{session_id}"
        try:
            raw = await self._redis.lrange(key, 0, max(0, limit - 1))
        except Exception as e:
            logger.warning("memory_recent_failed error=%s", e)
            return []
        out: list[MemoryItem] = []
        for entry in raw:
            try:
                d = json.loads(entry)
                out.append(
                    MemoryItem(
                        id=str(d.get("id", "")),
                        text=str(d.get("text", "")),
                        role=str(d.get("role", "user")),
                        session_id=str(d.get("session_id", session_id)),
                        score=float(d.get("score", 1.0)),
                        created_at=float(d.get("created_at", 0.0)),
                    )
                )
            except Exception:
                continue
        return out

    # ── forget ──────────────────────────────────────────────────────
    async def forget_session(self, user_id: str, session_id: str) -> int:
        """Purge a session's vectors and Redis entries. Returns delete count."""
        if self._redis is None:
            return 0
        removed = 0
        recent_key = f"{self._recent_prefix}:{user_id}:{session_id}"
        ids_key = f"{self._ids_prefix}:{user_id}:{session_id}"
        try:
            ids: list[str] = []
            with contextlib.suppress(Exception):
                ids = list(await self._redis.smembers(ids_key))
            if ids and self._chat_vec.enabled:
                await self._chat_vec.delete_by_ids(ids)
            for key in (recent_key, ids_key):
                with contextlib.suppress(Exception):
                    deleted = await self._redis.delete(key)
                    removed += int(deleted or 0)
        except Exception as e:
            logger.warning("memory_forget_failed error=%s", e)
        return removed

    async def close(self) -> None:
        """Close the HTTP client if this instance owns it."""
        if self._owns_http:
            with contextlib.suppress(Exception):
                await self._http.aclose()

    # ── backward-compat aliases ────────────────────────────────────
    async def add_message(
        self,
        user_id: str,
        session_id: str,
        role: str,
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """Alias for ``remember()``."""
        return await self.remember(user_id=user_id, session_id=session_id, role=role, text=text, metadata=metadata)

    async def retrieve(
        self,
        user_id: str,
        query: str,
        *,
        top_k: int = _DEFAULT_TOP_K,
        session_id: str | None = None,
        min_score: float = _DEFAULT_MIN_SCORE,
    ) -> list[MemoryItem]:
        """Alias for ``recall()``."""
        return await self.recall(user_id=user_id, query=query, top_k=top_k, session_id=session_id, min_score=min_score)

    async def retrieve_recent(
        self,
        user_id: str,
        session_id: str,
        *,
        limit: int = 10,
    ) -> list[MemoryItem]:
        """Alias for ``recent()``."""
        return await self.recent(user_id, session_id, limit=limit)

    # ── internals ──────────────────────────────────────────────────
    async def _embed(self, text: str) -> list[float] | None:
        """Embed text, using Redis as a short-term cache. Never raises."""
        if not text or not text.strip():
            return None
        text = text.strip()[:_MAX_TEXT_CHARS]
        cache_key = f"{self._embed_prefix}:{_sha(text)}"
        if self._redis is not None:
            with contextlib.suppress(Exception):
                raw = await self._redis.get(cache_key)
                if raw:
                    return json.loads(raw)
        vec = await self._embed_remote(text)
        if vec and self._redis is not None:
            with contextlib.suppress(Exception):
                await self._redis.setex(cache_key, _EMBED_CACHE_TTL, json.dumps(vec))
        return vec

    async def _embed_remote(self, text: str) -> list[float] | None:
        """Delegate to the canonical ``cf_embed_batch``. Never raises."""
        from .infra import cf_embed_batch

        vecs = await cf_embed_batch([text])
        if not vecs:
            return None
        return vecs[0]

    async def _recent_push(
        self,
        user_id: str,
        session_id: str,
        item: MemoryItem,
    ) -> None:
        if self._redis is None:
            return
        key = f"{self._recent_prefix}:{user_id}:{session_id}"
        with contextlib.suppress(Exception):
            await self._redis.lpush(key, json.dumps(item.to_dict()))
            await self._redis.ltrim(key, 0, self._recent_limit - 1)
            await self._redis.expire(key, _RECENT_TTL)

    async def _track_id(
        self,
        user_id: str,
        session_id: str,
        vid: str,
    ) -> None:
        if self._redis is None:
            return
        with contextlib.suppress(Exception):
            key = f"{self._ids_prefix}:{user_id}:{session_id}"
            await self._redis.sadd(key, vid)
            await self._redis.expire(key, _RECENT_TTL)


# ═══════════════════════════════════════════════════════════════════════════
# Lazy singleton
# ═══════════════════════════════════════════════════════════════════════════
_MEMORY_LOCK = threading.Lock()
_MEMORY: Memory | None = None


async def get_memory() -> Memory:
    """Return the process-wide ``Memory`` instance.

    Contract:
        * **Async only** — every caller must ``await`` this. The
          pre-consolidation sync override was removed because it shadowed
          the async factory and made ``await get_memory()`` crash.
        * Thread-safe via ``_MEMORY_LOCK``.
        * Never raises; if Redis is absent the memory still constructs
          with ``redis_client=None``.
    """
    global _MEMORY
    if _MEMORY is not None:
        return _MEMORY
    with _MEMORY_LOCK:
        if _MEMORY is None:
            try:
                from .infra import get_redis_client

                redis = get_redis_client()
            except Exception:
                redis = None
            _MEMORY = Memory(redis_client=redis)
        return _MEMORY


# ── Backward compatibility ────────────────────────────────────────────
ConversationMemory = Memory
MemoryBus = Memory
MemoryHit = MemoryItem
ConversationMemoryItem = MemoryItem


__all__ = [
    "Memory",
    "MemoryItem",
    "get_memory",
    "ConversationMemory",
    "MemoryBus",
    "MemoryHit",
    "ConversationMemoryItem",
    "_VectorizeClient",
    "_sha",
]
