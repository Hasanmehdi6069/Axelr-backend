"""Semantic search over the Axelr knowledge base.

The matrix lives in-process; documents live in the backend. Every
mutation marks the matrix dirty and triggers a rebuild on next read.

Pluggable embedding backend:
  * `tfidf` — default, no deps, best for keyword-heavy corpora.
  * `sentence-transformer` — requires `pip install sentence-transformers`,
    semantic quality, ~80MB model download at boot.

Honest limitations
------------------
* TF-IDF rebuilds the whole matrix on every mutation — O(n) per write.
  Fine up to a few thousand docs. At 100k+ docs, swap for a real vector
  store (Qdrant, pgvector, Milvus). Do not "optimise" the in-memory
  rebuild; it is a modelling choice, not a bug.
* Sentence-transformer inference is single-process. For high QPS,
  precompute embeddings and store only the vectors, not the model.
* Query strings are read-only; index mutations are admin-only. Every
  mutation rebuilds under the same lock, so readers never see a torn
  matrix.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from typing import Any

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from .config import settings
from .storage_base import get_backend

logger = logging.getLogger("insights.search")

try:
    from api.state import state  # type: ignore
except Exception:  # pragma: no cover — Redis is optional
    state = None


# ── Embedding backends ────────────────────────────────────────────────

class _Tfidf:
    name = "tfidf"

    def __init__(self) -> None:
        self._v = TfidfVectorizer(
            stop_words="english", ngram_range=(1, 2), sublinear_tf=True,
            min_df=1, max_features=50_000,
        )

    def fit(self, corpus: list[str]) -> Any:
        return self._v.fit_transform(corpus)

    def transform(self, texts: list[str]) -> Any:
        return self._v.transform(texts)


class _SentenceTransformer:
    name = "sentence-transformer"

    def __init__(self) -> None:
        from sentence_transformers import SentenceTransformer  # type: ignore
        self._m = SentenceTransformer("all-MiniLM-L6-v2")

    def fit(self, corpus: list[str]) -> Any:
        return np.asarray(self._m.encode(corpus, normalize_embeddings=True))

    def transform(self, texts: list[str]) -> Any:
        return np.asarray(self._m.encode(texts, normalize_embeddings=True))


def _build_embedding() -> Any:
    if settings.search_backend == "sentence-transformer":
        try:
            return _SentenceTransformer()
        except ImportError:
            logger.warning(
                "sentence-transformers not installed; falling back to tfidf"
            )
    return _Tfidf()


# ── Index ─────────────────────────────────────────────────────────────

@dataclass
class DocEntry:
    doc_id: str
    content: str
    meta: dict[str, Any]


class SemanticIndex:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._embedding = _build_embedding()
        self._docs: dict[str, DocEntry] = {}
        self._order: list[str] = []
        self._matrix: Any = None
        self._dirty = False
        self._bootstrapped = False

    async def bootstrap(self) -> None:
        if self._bootstrapped:
            return
        try:
            rows = await get_backend().list_documents(
                limit=settings.search_max_docs,
            )
        except Exception as e:
            logger.warning("search bootstrap failed: %s", e)
            return
        with self._lock:
            for row in rows:
                doc_id, content = row.get("doc_id"), row.get("content")
                if doc_id and content:
                    self._docs[doc_id] = DocEntry(
                        doc_id, content, row.get("meta") or {},
                    )
            self._rebuild_locked()
            self._bootstrapped = True
        logger.info("search bootstrapped n=%d", len(self._docs))

    def _rebuild_locked(self) -> None:
        if not self._docs:
            self._matrix = None
            self._order = []
            self._dirty = False
            return
        self._order = list(self._docs.keys())
        corpus = [self._docs[k].content for k in self._order]
        try:
            self._matrix = self._embedding.fit(corpus)
        except ValueError as e:
            logger.warning("search rebuild empty vocab: %s", e)
            self._matrix = None
        self._dirty = False

    def force_rebuild(self) -> None:
        with self._lock:
            self._dirty = True
            self._rebuild_locked()

    def is_bootstrapped(self) -> bool:
        return self._bootstrapped

    def __len__(self) -> int:
        return len(self._docs)

    # ── Mutations ──

    async def upsert(
        self, doc_id: str, content: str,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        meta = meta or {}
        stored = await get_backend().upsert_document(doc_id, content, meta)
        with self._lock:
            self._docs[doc_id] = DocEntry(doc_id, content, meta)
            self._dirty = True
        return stored

    async def remove(self, doc_id: str) -> bool:
        ok = await get_backend().delete_document(doc_id)
        with self._lock:
            existed = doc_id in self._docs
            self._docs.pop(doc_id, None)
            if existed:
                self._dirty = True
        return ok or existed

    async def list_documents(self, limit: int = 200) -> list[dict[str, Any]]:
        return await get_backend().list_documents(limit=limit)

    # ── Query ──

    def search(
        self, query: str, top_k: int = 5,
        min_score: float | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            if self._dirty:
                self._rebuild_locked()
            if self._matrix is None or not self._docs:
                return []
            try:
                qv = self._embedding.transform([query])
            except ValueError:
                return []

            # Backend shape guard — config swapped at runtime.
            if (hasattr(qv, "shape") and hasattr(self._matrix, "shape")
                    and qv.shape[1] != self._matrix.shape[1]):
                self._rebuild_locked()
                if self._matrix is None:
                    return []
                try:
                    qv = self._embedding.transform([query])
                except ValueError:
                    return []

            sims = cosine_similarity(qv, self._matrix).flatten()
            if sims.size == 0:
                return []

            floor = settings.search_min_score if min_score is None else min_score
            k = max(1, min(top_k, sims.size))
            hits: list[dict[str, Any]] = []
            for idx in np.argsort(sims)[::-1][:k]:
                score = float(sims[int(idx)])
                if score <= 0 or score < floor:
                    continue
                entry = self._docs[self._order[int(idx)]]
                hits.append({
                    "doc_id": entry.doc_id,
                    "content": entry.content,
                    "score": round(score, 4),
                    "metadata": entry.meta,
                })
            return hits


# ── Singleton ─────────────────────────────────────────────────────────

_INDEX: SemanticIndex | None = None
_BOOT_LOCK = asyncio.Lock()


def get_index() -> SemanticIndex:
    global _INDEX
    if _INDEX is None:
        _INDEX = SemanticIndex()
    return _INDEX


async def ensure_bootstrapped() -> SemanticIndex:
    idx = get_index()
    if idx.is_bootstrapped():
        return idx
    async with _BOOT_LOCK:
        if not idx.is_bootstrapped():
            await idx.bootstrap()
    return idx


# ── Cached search ─────────────────────────────────────────────────────

def _cache_key(query: str, top_k: int, min_score: float | None) -> str:
    payload = f"{query}|{top_k}|{min_score}".encode()
    return f"insights:search:{hashlib.sha256(payload).hexdigest()[:32]}"


async def search_cached(
    query: str, top_k: int = 5, min_score: float | None = None,
) -> list[dict[str, Any]]:
    """Redis-cached wrapper around `SemanticIndex.search`.

    Falls back to the in-process matrix when Redis is absent or
    unreachable. The cache is intentionally not warmed on write — a
    stale hit costs more than a cold miss here.
    """
    await ensure_bootstrapped()
    idx = get_index()
    key = _cache_key(query, top_k, min_score)
    redis = getattr(state, "redis_client", None) if state else None
    if redis is not None and settings.search_cache_ttl > 0:
        try:
            cached = await redis.get(key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass
    hits = idx.search(query, top_k=top_k, min_score=min_score)
    if redis is not None and hits and settings.search_cache_ttl > 0:
        try:
            await redis.setex(key, settings.search_cache_ttl, json.dumps(hits))
        except Exception:
            pass
    return hits