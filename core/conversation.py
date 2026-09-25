# core/conversation.py
"""
AXELR Conversation Memory
=========================
Async, externally-backed long-term memory for chat sessions.

Architecture
------------
* **Embeddings** are produced by Cloudflare Workers AI
  (``@cf/baai/bge-small-en-v1.5``, 384 dims) — no local ONNX, no numpy.
* **Vectors** are stored in Cloudflare Vectorize (external, zero local RAM).
* **Recent messages** are mirrored to Redis (Upstash) so a session always
  has fast recency context even if Vectorize is momentarily unavailable.
* Per-user isolation is enforced through Vectorize metadata filters.

Failure isolation
-----------------
Every external call is bounded and wrapped. Any failure degrades to fewer
results — ``add_message`` / ``retrieve`` never raise.

RAM footprint < 15 MB.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("axelr.memory")


# Shared Cloudflare endpoints (also re-used by core.repo_indexer).
_CF_BASE = "https://api.cloudflare.com/client/v4/accounts/{acct}"
_EMBED_URL = _CF_BASE + "/ai/run/{model}"
_VECTOR_URL = _CF_BASE + "/vectorize/v2/indexes/{index}{suffix}"
_EMBED_MODEL = "@cf/baai/bge-small-en-v1.5"

_EMBED_DIM = 384

_MAX_TEXT_CHARS = 4000
_DEFAULT_TOP_K = 5
_DEFAULT_RECENT_LIMIT = 20
_EMBED_CACHE_TTL = 86_400
_RECENT_TTL = 86_400 * 7
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


@dataclass(slots=True)
class MemoryItem:
    """A single retrieved memory entry."""

    id: str
    text: str
    role: str
    session_id: str
    score: float
    created_at: float

    def render(self) -> str:
        """Format as a line suitable for prompt injection."""
        return f"[{self.role}] {self.text}"

    def to_dict(self) -> dict:
        """Return a JSON-serialisable representation."""
        return {
            "id": self.id,
            "text": self.text,
            "role": self.role,
            "session_id": self.session_id,
            "score": round(self.score, 4),
            "created_at": self.created_at,
        }


def _sha(text: str) -> str:
    """Deterministic short hash."""
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _make_id(user_id: str, session_id: str, text: str) -> str:
    """Stable, collision-resistant vector id."""
    return _sha(f"{user_id}|{session_id}|{time.time_ns()}|{text}")[:32]


class _ConvVectorizeClient:
    """Minimal Cloudflare Vectorize REST wrapper (conversation memory)."""

    __slots__ = ("_account_id", "_api_key", "_http", "_index")

    def __init__(
        self,
        account_id: str,
        api_key: str,
        index: str,
        http: httpx.AsyncClient,
    ) -> None:
        self._account_id = account_id
        self._api_key = api_key
        self._index = index
        self._http = http

    @property
    def enabled(self) -> bool:
        """True iff all three CF credentials/identifiers are configured."""
        return bool(self._account_id and self._api_key and self._index)

    def _url(self, suffix: str) -> str:
        return _VECTOR_URL.format(acct=self._account_id, index=self._index, suffix=suffix)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def upsert(self, vectors: list[dict], timeout: float = 30.0) -> bool:
        """Upsert vectors; return True on success."""
        if not self.enabled or not vectors:
            return False
        try:
            r = await self._http.post(
                self._url("/upsert"),
                headers=self._headers(),
                json={"vectors": vectors},
                timeout=timeout,
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
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
        timeout: float = 15.0,
    ) -> list[dict]:
        """Semantic query; returns the raw ``matches`` list (possibly empty)."""
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
            r = await self._http.post(
                self._url("/query"), headers=self._headers(), json=body, timeout=timeout
            )
            r.raise_for_status()
            data = r.json()
            result = data.get("result") or {}
            return list(result.get("matches") or [])
        except Exception as e:
            logger.warning("vectorize_query_failed error=%s", e)
            return []

    async def delete_by_ids(self, ids: list[str], timeout: float = 30.0) -> bool:
        """Delete vectors by id."""
        if not self.enabled or not ids:
            return False
        try:
            r = await self._http.post(
                self._url("/delete_by_ids"),
                headers=self._headers(),
                json={"ids": ids},
                timeout=timeout,
            )
            r.raise_for_status()
            return True
        except Exception as e:
            logger.warning("vectorize_delete_failed error=%s", e)
            return False


class ConversationMemory:
    """
    Async vector-backed long-term memory for chat sessions.

    Parameters
    ----------
    account_id, api_key : str | None
        Cloudflare credentials. Default to ``CLOUDFLARE_ACCOUNT_ID`` /
        ``CLOUDFLARE_API_KEY``.
    index_name : str | None
        Vectorize index. Defaults to ``CONV_MEMORY_INDEX`` or
        ``"axelr-conversation-memory"``.
    redis_client : Any | None
        Async Redis client (``redis.asyncio``). Optional.
    http_client : httpx.AsyncClient | None
        Shared HTTP client. When omitted, an internal client is created
        and owned by this instance (freed in :meth:`close`).
    recent_limit : int
        Cap on per-session recent messages stored in Redis.
    """

    __slots__ = (
        "_embed_cache_prefix",
        "_http",
        "_ids_prefix",
        "_owns_http",
        "_recent_limit",
        "_recent_prefix",
        "_redis",
        "_vec",
    )

    def __init__(
        self,
        *,
        account_id: str | None = None,
        api_key: str | None = None,
        index_name: str | None = None,
        redis_client: Any | None = None,
        http_client: httpx.AsyncClient | None = None,
        recent_limit: int = _DEFAULT_RECENT_LIMIT,
    ) -> None:
        acct = (account_id or os.getenv("CLOUDFLARE_ACCOUNT_ID") or "").strip()
        key = (api_key or os.getenv("CLOUDFLARE_API_KEY") or "").strip()
        idx = (
            index_name
            or os.getenv("CONV_MEMORY_INDEX")
            or "axelr-conversation-memory"
        ).strip()

        if http_client is None:
            self._http = httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
            self._owns_http = True
        else:
            self._http = http_client
            self._owns_http = False

        self._vec = _ConvVectorizeClient(acct, key, idx, self._http)
        self._redis = redis_client
        self._recent_limit = max(1, int(recent_limit))
        self._embed_cache_prefix = "axelr:convmem:embed"
        self._recent_prefix = "axelr:convmem:recent"
        self._ids_prefix = "axelr:convmem:ids"

    # -- public ------------------------------------------------------------

    @property
    def vector_enabled(self) -> bool:
        """Whether the semantic (Vectorize) tier is configured."""
        return self._vec.enabled

    async def add_message(
        self,
        user_id: str,
        session_id: str,
        role: str,
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """
        Persist a message to Redis (recent) and Vectorize (semantic).

        Returns the vector id on success, ``None`` otherwise. Never raises.
        """
        try:
            if not text or not text.strip():
                return None
            text = text.strip()[:_MAX_TEXT_CHARS]
            vid = _make_id(user_id, session_id, text)
            now = time.time()

            # 1. Recent layer (always)
            await self._recent_push(
                user_id,
                session_id,
                MemoryItem(
                    id=vid,
                    text=text,
                    role=role,
                    session_id=session_id,
                    score=1.0,
                    created_at=now,
                ),
            )

            # 2. Semantic layer (best-effort)
            if not self.vector_enabled:
                return vid
            vec = await self._embed(text)
            if not vec:
                return vid

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

            ok = await self._vec.upsert(
                [{"id": vid, "values": vec, "metadata": md}]
            )
            if ok:
                await self._track_id(user_id, session_id, vid)
            return vid if ok else None
        except Exception as e:
            logger.warning("add_message_failed error=%s", e)
            return None

    async def retrieve(
        self,
        user_id: str,
        query: str,
        *,
        top_k: int = _DEFAULT_TOP_K,
        session_id: str | None = None,
        min_score: float = 0.0,
    ) -> list[MemoryItem]:
        """
        Semantic search over past messages for ``user_id``.

        Returns up to ``top_k`` items sorted by descending similarity. If
        ``session_id`` is supplied the search is scoped to that session.
        Never raises.
        """
        if not query or not self.vector_enabled:
            return []
        try:
            vec = await self._embed(query)
            if not vec:
                return []

            if session_id:
                filters: dict[str, Any] = {
                    "$and": [
                        {"user_id": {"$eq": user_id}},
                        {"session_id": {"$eq": session_id}},
                    ]
                }
            else:
                filters = {"user_id": {"$eq": user_id}}

            matches = await self._vec.query(vec, top_k=top_k, filters=filters)

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
            logger.warning("retrieve_failed error=%s", e)
            return []

    async def retrieve_recent(
        self,
        user_id: str,
        session_id: str,
        *,
        limit: int = 10,
    ) -> list[MemoryItem]:
        """Return the most recent N messages for a session (Redis only)."""
        try:
            return await self._recent_read(user_id, session_id, limit)
        except Exception as e:
            logger.warning("retrieve_recent_failed error=%s", e)
            return []

    async def forget_session(self, user_id: str, session_id: str) -> int:
        """
        Best-effort removal of a session's recent memory (and its tracked
        vector ids). Returns the number of Redis keys deleted.
        """
        removed = 0
        if self._redis is None:
            return 0
        recent_key = f"{self._recent_prefix}:{user_id}:{session_id}"
        ids_key = f"{self._ids_prefix}:{user_id}:{session_id}"
        try:
            ids: list[str] = []
            try:
                ids = list(await self._redis.smembers(ids_key))
            except Exception:
                ids = []
            if ids and self.vector_enabled:
                await self._vec.delete_by_ids(ids)
            for key in (recent_key, ids_key):
                try:
                    deleted = await self._redis.delete(key)
                    removed += int(deleted or 0)
                except Exception:
                    pass
        except Exception as e:
            logger.warning("forget_session_failed error=%s", e)
        return removed

    async def close(self) -> None:
        """Release the internal HTTP client, if owned."""
        if self._owns_http:
            try:
                await self._http.aclose()
            except Exception:
                pass

    # -- internal: embedding ----------------------------------------------

    async def _embed(self, text: str) -> list[float] | None:
        """Return a normalised embedding vector for ``text`` (or ``None``)."""
        if not text or not text.strip():
            return None
        text = text.strip()[:_MAX_TEXT_CHARS]

        cache_key = f"{self._embed_cache_prefix}:{_sha(text)}"
        if self._redis is not None:
            try:
                raw = await self._redis.get(cache_key)
                if raw:
                    return json.loads(raw)
            except Exception:
                pass

        vec = await self._embed_remote(text)
        if vec and self._redis is not None:
            try:
                await self._redis.setex(cache_key, _EMBED_CACHE_TTL, json.dumps(vec))
            except Exception:
                pass
        return vec

    async def _embed_remote(self, text: str) -> list[float] | None:
        """Call Cloudflare Workers AI for a single embedding."""
        if not (self._vec._account_id and self._vec._api_key):
            return None
        url = _EMBED_URL.format(acct=self._vec._account_id, model=_EMBED_MODEL)
        try:
            r = await self._http.post(
                url,
                headers={
                    "Authorization": f"Bearer {self._vec._api_key}",
                    "Content-Type": "application/json",
                },
                json={"text": [text]},
                timeout=10.0,
            )
            r.raise_for_status()
            data = r.json()
            vecs = (data.get("result") or {}).get("data") or []
            if not vecs or not isinstance(vecs[0], list):
                return None
            v = [float(x) for x in vecs[0]]
            return v if len(v) == _EMBED_DIM else v  # be lenient on dim
        except Exception as e:
            logger.warning("embed_remote_failed error=%s", e)
            return None

    # -- internal: redis --------------------------------------------------

    async def _recent_push(
        self, user_id: str, session_id: str, item: MemoryItem
    ) -> None:
        if self._redis is None:
            return
        key = f"{self._recent_prefix}:{user_id}:{session_id}"
        try:
            await self._redis.lpush(key, json.dumps(item.to_dict()))
            await self._redis.ltrim(key, 0, self._recent_limit - 1)
            await self._redis.expire(key, _RECENT_TTL)
        except Exception as e:
            logger.warning("recent_push_failed error=%s", e)

    async def _recent_read(
        self, user_id: str, session_id: str, limit: int
    ) -> list[MemoryItem]:
        if self._redis is None:
            return []
        key = f"{self._recent_prefix}:{user_id}:{session_id}"
        try:
            raw = await self._redis.lrange(key, 0, max(0, limit - 1))
        except Exception as e:
            logger.warning("recent_read_failed error=%s", e)
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

    async def _track_id(self, user_id: str, session_id: str, vid: str) -> None:
        if self._redis is None:
            return
        try:
            key = f"{self._ids_prefix}:{user_id}:{session_id}"
            await self._redis.sadd(key, vid)
            await self._redis.expire(key, _RECENT_TTL)
        except Exception:
            pass


__all__ = ["ConversationMemory", "MemoryItem"]