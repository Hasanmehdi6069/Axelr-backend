# semantic_cache.py
"""
AXELR Semantic Cache
====================
FastEmbed (ONNX) + NumPy cosine similarity semantic cache.

* Model: BAAI/bge-small-en-v1.5 (384 dims, ~50 MB on disk)
* No FAISS — brute-force NumPy dot product over a bounded matrix.
* LRU eviction via an OrderedDict of row indices.
* Optional Redis persistence (fire-and-forget, best-effort).

All heavy dependencies (numpy, fastembed) are imported lazily inside
methods — module import is I/O-free and near-instantaneous.

RAM footprint: < 100 MB steady state at 10k entries.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

__all__ = ["CacheEntry", "SemanticCache", "get_semantic_cache"]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_DIM = 384
DEFAULT_MAX_ENTRIES = 10_000
DEFAULT_SIMILARITY_THRESHOLD = 0.92


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class CacheEntry:
    """A single cached prompt/response pair."""

    prompt: str
    response: str
    created_at: float
    hits: int = 0

    def to_dict(self) -> dict:
        """Return a JSON-serialisable representation."""
        return {
            "prompt": self.prompt,
            "response": self.response,
            "created_at": self.created_at,
            "hits": self.hits,
        }


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class SemanticCache:
    """
    Semantic response cache.

    Parameters
    ----------
    model_name : str
        FastEmbed model identifier.
    max_entries : int
        Hard cap on in-memory entries (LRU eviction).
    similarity_threshold : float
        Minimum cosine similarity to count as a hit.
    redis_key_prefix : str
        Namespace prefix for Redis keys (``REDIS_URL`` read from env).
    """

    __slots__ = (
        "_dim",
        "_embedder",
        "_embeddings",
        "_entries",
        "_id_to_row",
        "_load_lock",
        "_lock",
        "_max_entries",
        "_model_loaded",
        "_model_name",
        "_redis",
        "_redis_prefix",
        "_threshold",
    )

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        redis_key_prefix: str = "axelr:semantic_cache",
    ) -> None:
        """Initialise the cache; the embedding model loads lazily on first use."""
        self._model_name = model_name
        self._max_entries = int(max_entries)
        self._threshold = float(similarity_threshold)
        self._dim = DEFAULT_DIM

        self._embedder = None
        self._embeddings = None                            # lazy numpy matrix
        self._entries: OrderedDict[int, CacheEntry] = OrderedDict()
        self._id_to_row: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._model_loaded = False
        self._load_lock = asyncio.Lock()

        self._redis_prefix = redis_key_prefix
        self._redis = None                                 # lazy redis client

    # -- public API ---------------------------------------------------------

    async def get(
        self,
        prompt: str,
        *,
        threshold: float | None = None,
    ) -> str | None:
        """
        Look up the closest cached response. Returns ``None`` on miss.

        Never raises — internal failures degrade to cache miss.
        """
        if not prompt or not prompt.strip():
            return None

        try:
            await self._ensure_model()
            if self._embeddings is None or self._embeddings.shape[0] == 0:
                return None

            query_vec = await asyncio.to_thread(self._encode_one, prompt)
            if query_vec is None:
                return None

            async with self._lock:
                sims = self._embeddings @ query_vec
                if sims.size == 0:
                    return None
                best_idx = int(sims.argmax())
                best_score = float(sims[best_idx])

            effective_threshold = (
                threshold if threshold is not None else self._threshold
            )
            if best_score < effective_threshold:
                return None

            async with self._lock:
                entry = self._entries.get(best_idx)
                if entry is None:
                    return None
                entry.hits += 1
                self._entries.move_to_end(best_idx)
                return entry.response
        except Exception:
            return None

    async def set(self, prompt: str, response: str) -> None:
        """Insert or update a cache entry. Never raises."""
        if not prompt or not response:
            return

        try:
            await self._ensure_model()
            vec = await asyncio.to_thread(self._encode_one, prompt)
            if vec is None:
                return

            key = _hash(prompt)

            async with self._lock:
                # Update in place if key already exists.
                existing_row = self._id_to_row.get(key)
                if existing_row is not None and self._embeddings is not None:
                    if existing_row < self._embeddings.shape[0]:
                        self._embeddings[existing_row] = vec
                        entry = self._entries.get(existing_row)
                        if entry is not None:
                            entry.response = response
                            entry.created_at = time.time()
                        self._entries.move_to_end(existing_row)
                        return

                # Evict if full
                if len(self._entries) >= self._max_entries:
                    self._evict_lru_locked()

                row_idx = self._append_embedding_locked(vec)
                self._entries[row_idx] = CacheEntry(
                    prompt=prompt,
                    response=response,
                    created_at=time.time(),
                )
                self._id_to_row[key] = row_idx

            # Fire-and-forget Redis persistence
            asyncio.create_task(self._persist_async(key, prompt, response))
        except Exception:
            return

    async def clear(self) -> None:
        """Drop all in-memory state. Never raises."""
        try:
            async with self._lock:
                self._entries.clear()
                self._id_to_row.clear()
                self._embeddings = None
        except Exception:
            pass

    def stats(self) -> dict[str, Any]:
        """Return a lightweight snapshot of cache metrics."""
        try:
            total_hits = sum(e.hits for e in self._entries.values())
            return {
                "size": len(self._entries),
                "max_entries": self._max_entries,
                "total_hits": total_hits,
                "model": self._model_name,
                "threshold": self._threshold,
                "embedder_loaded": self._model_loaded,
                "redis_configured": bool(
                    os.getenv("REDIS_URL") or self._redis is not None
                ),
            }
        except Exception:
            return {"size": 0, "error": "stats unavailable"}

    # -- model loading ------------------------------------------------------

    async def _ensure_model(self) -> None:
        """Ensure the embedder is loaded (single-flight)."""
        if self._model_loaded:
            return
        async with self._load_lock:
            if self._model_loaded:
                return
            try:
                await asyncio.to_thread(self._load_model)
                self._model_loaded = True
            except Exception:
                self._model_loaded = False

    def _load_model(self) -> None:
        """Synchronous model load — executed inside ``asyncio.to_thread``."""
        # Lazy heavy import
        from fastembed import TextEmbedding  # type: ignore

        # Restrict ONNX Runtime to a single thread — 0.1 CPU budget.
        os.environ.setdefault("OMP_NUM_THREADS", "1")
        os.environ.setdefault("ORT_NUM_THREADS", "1")

        self._embedder = TextEmbedding(
            model_name=self._model_name,
            threads=1,
        )

    def _encode_one(self, prompt: str):
        """Return a normalised float32 vector of shape (dim,), or None."""
        # Lazy heavy import
        import numpy as np

        if self._embedder is None:
            return None

        try:
            for vec in self._embedder.embed([prompt]):
                arr = np.asarray(vec, dtype=np.float32)
                norm = float(np.linalg.norm(arr))
                if norm > 0:
                    arr = arr / norm
                return arr
        except Exception:
            return None
        return None

    # -- matrix growth / eviction ------------------------------------------

    def _append_embedding_locked(self, vec) -> int:
        """Append a row to the embedding matrix. Caller must hold the lock."""
        import numpy as np

        vec = vec.reshape(1, -1).astype(np.float32, copy=False)

        if self._embeddings is None:
            # Initial allocation — start at 64 rows to reduce reallocation.
            initial = max(64, min(self._max_entries, 256))
            self._embeddings = np.zeros((initial, self._dim), dtype=np.float32)
            self._embeddings[0] = vec
            return 0

        # Count live rows via OrderedDict keys (max key + 1)
        current_n = (max(self._entries.keys()) + 1) if self._entries else 0

        if current_n >= self._embeddings.shape[0]:
            # Chunked doubling — amortised O(1) appends.
            new_capacity = max(64, self._embeddings.shape[0] * 2)
            new_capacity = min(new_capacity, self._max_entries)
            grown = np.zeros((new_capacity, self._dim), dtype=np.float32)
            grown[: self._embeddings.shape[0]] = self._embeddings
            self._embeddings = grown

        row_idx = current_n
        self._embeddings[row_idx] = vec
        return row_idx

    def _evict_lru_locked(self) -> None:
        """Evict the least-recently-used entry. Caller must hold the lock."""
        if not self._entries:
            return
        old_idx, old_entry = self._entries.popitem(last=False)
        key = _hash(old_entry.prompt)
        self._id_to_row.pop(key, None)

        # Zero out the row so it doesn't contribute to future lookups.
        if self._embeddings is not None and old_idx < self._embeddings.shape[0]:
            self._embeddings[old_idx] = 0.0

    # -- Redis persistence (best-effort, fire-and-forget) ------------------

    async def _get_redis(self):
        """Return a shared Redis client, or ``None`` if unavailable."""
        if self._redis is not None:
            return self._redis
        url = os.getenv("REDIS_URL")
        if not url:
            return None
        try:
            import redis.asyncio as aioredis  # type: ignore

            self._redis = await aioredis.from_url(
                url, decode_responses=True, max_connections=4
            )
            return self._redis
        except Exception:
            self._redis = None
            return None

    async def _persist_async(self, key: str, prompt: str, response: str) -> None:
        """Persist a cache entry to Redis (best-effort, non-blocking)."""
        try:
            client = await self._get_redis()
            if client is None:
                return
            payload = json.dumps({"prompt": prompt, "response": response})
            await client.setex(f"{self._redis_prefix}:{key}", 3600, payload)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash(prompt: str) -> str:
    """Deterministic short hash for a prompt (case- and whitespace-normalised)."""
    return hashlib.sha256(
        prompt.strip().lower().encode("utf-8", errors="ignore")
    ).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_default_cache: SemanticCache | None = None


def get_semantic_cache() -> SemanticCache:
    """Return the process-wide singleton cache."""
    global _default_cache
    if _default_cache is None:
        _default_cache = SemanticCache()
    return _default_cache

