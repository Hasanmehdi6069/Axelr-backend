# core/vectorize.py
"""
AXELR AI — Cloudflare Vectorize semantic cache.
================================================
Public surface:
    CacheEntry                  — one stored (prompt -> response) pair
    CloudflareVectorizeCache    — semantic cache (Cloudflare Vectorize + Workers AI)
    get_vector_cache()          — process-wide singleton

Design:
    * When CLOUDFLARE_API_KEY + CLOUDFLARE_ACCOUNT_ID are set, embeddings are
      produced by Workers AI (`@cf/baai/bge-base-en-v1.5`) and vectors are
      stored in a Cloudflare Vectorize index.
    * When credentials are missing, the cache transparently degrades to a
      bounded, exact-match in-memory dict — the app still boots and behaves
      correctly; only the *semantic* aspect is lost.
    * All network calls are wrapped in try/except; a cache miss/failure never
      propagates to the caller.

RAM footprint: < 10 MB.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("axelr.memory")


CF_API_KEY    = (os.getenv("CLOUDFLARE_API_KEY") or "").strip()
CF_ACCOUNT_ID = (os.getenv("CLOUDFLARE_ACCOUNT_ID") or "").strip()
CF_VECTORIZE_INDEX = os.getenv("CLOUDFLARE_VECTORIZE_INDEX", "axelr-semantic-cache").strip()
CF_EMBED_MODEL = os.getenv("CLOUDFLARE_EMBED_MODEL", "@cf/baai/bge-base-en-v1.5").strip()

CF_ENABLED = bool(CF_API_KEY and CF_ACCOUNT_ID)

# Local fallback bounds
_LOCAL_MAX_ENTRIES = int(os.getenv("SEMANTIC_CACHE_MAX_ENTRIES", "2000"))
_LOCAL_TTL_SECONDS = int(os.getenv("SEMANTIC_CACHE_TTL", "3600"))

# Vectorize parameters
DEFAULT_TOP_K     = 1
DEFAULT_MIN_SCORE = float(os.getenv("SEMANTIC_CACHE_MIN_SCORE", "0.85"))


@dataclass(slots=True)
class CacheEntry:
    """One cached (prompt -> response) pair with provenance metadata."""
    prompt: str
    response: str
    score: float = 1.0
    created_at: float = field(default_factory=time.time)
    provider: str = "vectorize"

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "response": self.response,
            "score": self.score,
            "created_at": self.created_at,
            "provider": self.provider,
        }


class _LocalCache:
    """Bounded LRU + TTL local fallback cache."""
    def __init__(self, max_entries: int, ttl: int) -> None:
        self._max = max_entries
        self._ttl = ttl
        self._store: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(prompt: str) -> str:
        return hashlib.sha256(prompt.strip().lower().encode()).hexdigest()

    async def get(self, prompt: str) -> str | None:
        k = self._key(prompt)
        async with self._lock:
            item = self._store.get(k)
            if not item:
                return None
            ts, value = item
            if self._ttl > 0 and (time.time() - ts) > self._ttl:
                self._store.pop(k, None)
                return None
            self._store.move_to_end(k)
            return value

    async def set(self, prompt: str, response: str) -> None:
        k = self._key(prompt)
        async with self._lock:
            self._store[k] = (time.time(), response)
            self._store.move_to_end(k)
            while len(self._store) > self._max:
                self._store.popitem(last=False)

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()


class CloudflareVectorizeCache:
    """
    Semantic cache backed by Cloudflare Vectorize + Workers AI embeddings.

    Public API (matches app.py expectations):
        await cache._ensure_model()      # warm-up hook
        await cache.get(prompt)          # -> str | None
        await cache.set(prompt, resp)    # -> None
        await cache.clear()              # -> None
    """

    def __init__(
        self,
        api_key: str | None = None,
        account_id: str | None = None,
        index_name: str | None = None,
        embed_model: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key     = (api_key     or CF_API_KEY).strip()
        self._account_id  = (account_id  or CF_ACCOUNT_ID).strip()
        self._index_name  = (index_name  or CF_VECTORIZE_INDEX).strip()
        self._embed_model = (embed_model or CF_EMBED_MODEL).strip()

        self._enabled = bool(self._api_key and self._account_id)

        # Lazy HTTP client; owned if not injected
        self._http: httpx.AsyncClient | None = http_client
        self._owns_http = http_client is None

        self._local = _LocalCache(_LOCAL_MAX_ENTRIES, _LOCAL_TTL_SECONDS)
        self._warmed = False

        if not self._enabled:
            logger.info(
                "cloudflare_vectorize_disabled_fallback_local reason=missing_credentials"
            )
        else:
            logger.info(
                "cloudflare_vectorize_enabled index=%s model=%s",
                self._index_name, self._embed_model,
            )

    # ------------------------------------------------------------------ http
    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0))
        return self._http

    async def _ensure_model(self) -> None:
        """Warm-up hook called from FastAPI lifespan(). Idempotent."""
        if self._warmed:
            return
        self._warmed = True
        if not self._enabled:
            return
        # Cheap probe: encode a trivial string. Failure is non-fatal.
        try:
            await self._embed("ok")
            logger.info("cloudflare_vectorize_warmup_ok")
        except Exception as e:
            logger.warning("cloudflare_vectorize_warmup_failed error=%s", e)

    # --------------------------------------------------------------- embedding
    async def _embed(self, text: str) -> list[float]:
        """Return an embedding via Cloudflare Workers AI."""
        if not self._enabled:
            raise RuntimeError("vectorize_disabled")
        url = (
            f"https://api.cloudflare.com/client/v4/accounts/"
            f"{self._account_id}/ai/run/{self._embed_model}"
        )
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {"text": [text]}
        r = await self._client().post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
        # Workers AI returns {"result": {"data": [[...]]}} for bge models
        vectors = (data.get("result") or {}).get("data") or []
        if not vectors or not isinstance(vectors[0], list):
            raise RuntimeError(f"unexpected_embed_response:{str(data)[:200]}")
        return list(vectors[0])

    # ------------------------------------------------------------------- get
    async def get(self, prompt: str) -> str | None:
        if not prompt:
            return None

        # Fast path: exact match in the local cache first (free + fast)
        try:
            exact = await self._local.get(prompt)
            if exact is not None:
                return exact
        except Exception:
            pass

        if not self._enabled:
            return None

        try:
            vector = await self._embed(prompt)
        except Exception as e:
            logger.debug("vectorize_embed_failed_for_get error=%s", e)
            return None

        try:
            url = (
                f"https://api.cloudflare.com/client/v4/accounts/"
                f"{self._account_id}/vectorize/v2/indexes/"
                f"{self._index_name}/query"
            )
            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "vector": vector,
                "topK": DEFAULT_TOP_K,
                "returnMetadata": "all",
            }
            r = await self._client().post(url, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.debug("vectorize_query_failed error=%s", e)
            return None

        matches = ((data.get("result") or {}).get("matches")) or []
        if not matches:
            return None
        best = matches[0]
        score = float(best.get("score") or 0.0)
        if score < DEFAULT_MIN_SCORE:
            return None
        meta = best.get("metadata") or {}
        response = meta.get("response")
        if isinstance(response, str) and response:
            # Backfill local exact-match cache so identical repeats are free
            try:
                await self._local.set(prompt, response)
            except Exception:
                pass
            return response
        return None

    # ------------------------------------------------------------------- set
    async def set(self, prompt: str, response: str) -> None:
        if not prompt or not response:
            return

        # Always populate local exact-match cache first (works offline).
        try:
            await self._local.set(prompt, response)
        except Exception:
            pass

        if not self._enabled:
            return

        try:
            vector = await self._embed(prompt)
        except Exception as e:
            logger.debug("vectorize_embed_failed_for_set error=%s", e)
            return

        vector_id = hashlib.sha256(
            prompt.strip().lower().encode()
        ).hexdigest()[:32]

        try:
            url = (
                f"https://api.cloudflare.com/client/v4/accounts/"
                f"{self._account_id}/vectorize/v2/indexes/"
                f"{self._index_name}/upsert"
            )
            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "vectors": [{
                    "id": vector_id,
                    "values": vector,
                    "metadata": {
                        "prompt": prompt[:1000],
                        "response": response,
                        "ts": int(time.time()),
                    },
                }],
            }
            r = await self._client().post(url, headers=headers, json=payload)
            r.raise_for_status()
        except Exception as e:
            logger.debug("vectorize_upsert_failed error=%s", e)

    # ----------------------------------------------------------------- clear
    async def clear(self) -> None:
        try:
            await self._local.clear()
        except Exception:
            pass
        # Remote index is NOT deleted — that would be destructive and shared
        # across workers. `clear()` only affects the local process.

    # ---------------------------------------------------------------- close
    async def close(self) -> None:
        """Release the owned HTTP client, if any."""
        if self._owns_http and self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None


_SINGLETON: CloudflareVectorizeCache | None = None


def get_vector_cache() -> CloudflareVectorizeCache:
    """Return the process-wide CloudflareVectorizeCache instance."""
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = CloudflareVectorizeCache()
    return _SINGLETON


__all__ = [
    "CacheEntry", "CloudflareVectorizeCache", "get_vector_cache",
]