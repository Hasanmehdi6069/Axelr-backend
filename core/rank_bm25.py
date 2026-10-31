<<<<<<< HEAD
# core/rank_bm25.py
"""
AXELR — Pure-Stdlib BM25 Local Retrieval
==========================================
Fills the "Rank-BM25 / Minsearch" gap in the pillar spec. "Minsearch"
is not a real product; Okapi BM25 is the canonical implementation.

Design:
    * Inverted index: term -> {doc_id -> term_frequency}
    * Metadata per doc, filterable at query time
    * LRU eviction when max_docs exceeded
    * Thread-safe
    * Optional SQLite persistence (save/load)
    * Zero third-party deps

RAM: ~45 KB at 5000 short docs. Bounded by max_docs and max_chars_per_doc.

Never raises.
"""
from __future__ import annotations

import contextlib
import logging
import math
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

logger = logging.getLogger("axelr.rank_bm25")

_TOKEN_RE = re.compile(r"[a-z0-9_]{2,}")

_K1 = 1.5
_B = 0.75
_MAX_DOCS = 5000
_MAX_CHARS_PER_DOC = 8000
_MAX_TOKENS_PER_QUERY = 32


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    return _TOKEN_RE.findall(text.lower())[:4096]


@dataclass(frozen=True, slots=True)
class BM25Hit:
    doc_id: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"doc_id": self.doc_id, "score": round(self.score, 4),
                "metadata": self.metadata}


class BM25Index:
    __slots__ = (
        "_lock", "_postings", "_doc_lengths", "_doc_meta",
        "_doc_order", "_total_tokens", "_max_docs",
        "_max_chars_per_doc", "_k1", "_b",
    )

    def __init__(
        self, *, k1: float = _K1, b: float = _B,
        max_docs: int = _MAX_DOCS,
        max_chars_per_doc: int = _MAX_CHARS_PER_DOC,
    ) -> None:
        self._lock = threading.RLock()
        # term -> {doc_id -> tf}
        self._postings: dict[str, dict[str, int]] = {}
        self._doc_lengths: dict[str, int] = {}
        self._doc_meta: dict[str, dict[str, Any]] = {}
        self._doc_order: OrderedDict[str, None] = OrderedDict()
        self._total_tokens = 0
        self._max_docs = max(64, int(max_docs))
        self._max_chars_per_doc = max(256, int(max_chars_per_doc))
        self._k1 = float(k1)
        self._b = float(b)

    # ── mutation ────────────────────────────────────────────────────────
    def add(self, doc_id: str, text: str,
            metadata: dict[str, Any] | None = None) -> bool:
        if not doc_id or not text:
            return False
        try:
            with self._lock:
                self._remove_locked(doc_id)
                text = text[: self._max_chars_per_doc]
                toks = _tokenize(text)
                if not toks:
                    return False
                tf: dict[str, int] = {}
                for t in toks:
                    tf[t] = tf.get(t, 0) + 1
                for term, count in tf.items():
                    self._postings.setdefault(term, {})[doc_id] = count
                self._doc_lengths[doc_id] = len(toks)
                self._doc_meta[doc_id] = dict(metadata or {})
                self._doc_order[doc_id] = None
                self._total_tokens += len(toks)
                self._evict_locked()
            return True
        except Exception as e:
            logger.debug("bm25_add_failed doc=%s error=%s", doc_id, e)
            return False

    def add_many(self, docs: Iterable[dict[str, Any]]) -> int:
        added = 0
        for d in docs:
            if not isinstance(d, dict):
                continue
            if self.add(str(d.get("id") or d.get("doc_id") or ""),
                        str(d.get("text") or ""),
                        d.get("metadata")):
                added += 1
        return added

    def remove(self, doc_id: str) -> bool:
        with self._lock:
            return self._remove_locked(doc_id)

    def _remove_locked(self, doc_id: str) -> bool:
        if doc_id not in self._doc_lengths:
            return False
        dl = self._doc_lengths.pop(doc_id, 0)
        self._doc_meta.pop(doc_id, None)
        self._doc_order.pop(doc_id, None)
        self._total_tokens -= dl
        # Purge from every posting list.
        empty_terms: list[str] = []
        for term, pl in self._postings.items():
            if doc_id in pl:
                del pl[doc_id]
                if not pl:
                    empty_terms.append(term)
        for t in empty_terms:
            self._postings.pop(t, None)
        return True

    def _evict_locked(self) -> None:
        while len(self._doc_order) > self._max_docs:
            oldest, _ = self._doc_order.popitem(last=False)
            self._remove_locked(oldest)

    def clear(self) -> None:
        with self._lock:
            self._postings.clear()
            self._doc_lengths.clear()
            self._doc_meta.clear()
            self._doc_order.clear()
            self._total_tokens = 0

    # ── query ───────────────────────────────────────────────────────────
    def query(
        self, text: str, *, top_k: int = 10,
        filter_fn: Callable[[dict[str, Any]], bool] | None = None,
    ) -> list[BM25Hit]:
        if not text:
            return []
        try:
            qtoks = list(dict.fromkeys(_tokenize(text)))[:_MAX_TOKENS_PER_QUERY]
            if not qtoks:
                return []
            with self._lock:
                n = len(self._doc_lengths)
                if n == 0:
                    return []
                avgdl = (self._total_tokens / n) if n else 0.0
                if avgdl <= 0:
                    return []
                # Gather candidate docs from query terms.
                scores: dict[str, float] = {}
                for t in qtoks:
                    pl = self._postings.get(t)
                    if not pl:
                        continue
                    df = len(pl)
                    idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
                    for doc_id, tf in pl.items():
                        dl = self._doc_lengths.get(doc_id, 0)
                        if dl <= 0:
                            continue
                        denom = tf + self._k1 * (
                            1.0 - self._b + self._b * dl / avgdl)
                        score = idf * (tf * (self._k1 + 1.0)) / denom
                        scores[doc_id] = scores.get(doc_id, 0.0) + score
                if not scores:
                    return []
                # Filter + top-k.
                hits: list[BM25Hit] = []
                for doc_id, score in scores.items():
                    meta = self._doc_meta.get(doc_id, {})
                    if filter_fn is not None and not filter_fn(meta):
                        continue
                    hits.append(BM25Hit(doc_id, score, meta))
            hits.sort(key=lambda h: (-h.score, h.doc_id))
            return hits[: max(1, int(top_k))]
        except Exception as e:
            logger.debug("bm25_query_failed error=%s", e)
            return []

    # ── introspection ───────────────────────────────────────────────────
    def stats(self) -> dict[str, Any]:
        with self._lock:
            n = len(self._doc_lengths)
            terms = len(self._postings)
            avgdl = (self._total_tokens / n) if n else 0.0
        return {
            "documents": n,
            "unique_terms": terms,
            "avg_doc_length": round(avgdl, 2),
            "max_docs": self._max_docs,
            "k1": self._k1, "b": self._b,
        }

    # ── persistence (optional) ─────────────────────────────────────────
    def save(self, path: str) -> bool:
        try:
            with self._lock, sqlite3.connect(path, timeout=5.0) as conn:
                conn.executescript(
                    "CREATE TABLE IF NOT EXISTS bm25_docs "
                    "(id TEXT PRIMARY KEY, text TEXT, meta TEXT);")
                conn.execute("DELETE FROM bm25_docs;")
                for doc_id, meta in self._doc_meta.items():
                    conn.execute(
                        "INSERT INTO bm25_docs(id, text, meta) VALUES (?,?,?)",
                        (doc_id, "", _json_dumps(meta)))
                conn.commit()
            return True
        except Exception as e:
            logger.debug("bm25_save_failed error=%s", e)
            return False

    def load(self, path: str) -> int:
        try:
            with sqlite3.connect(path, timeout=5.0) as conn:
                cur = conn.execute("SELECT id, meta FROM bm25_docs")
                rows = cur.fetchall()
            for doc_id, meta_json in rows:
                self.add(doc_id, doc_id, _json_loads(meta_json))
            return len(rows)
        except Exception as e:
            logger.debug("bm25_load_failed error=%s", e)
            return 0


def _json_dumps(obj: Any) -> str:
    import json
    return json.dumps(obj, separators=(",", ":"))


def _json_loads(s: str) -> dict[str, Any]:
    import json
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


# ── module singleton ─────────────────────────────────────────────────────
_INDEX: BM25Index | None = None
_INDEX_LOCK = threading.Lock()


def get_bm25_index() -> BM25Index:
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    with _INDEX_LOCK:
        if _INDEX is None:
            _INDEX = BM25Index()
        return _INDEX


def audit() -> dict[str, Any]:
    if _INDEX is None:
        return {"initialized": False}
    return {"initialized": True, **_INDEX.stats()}


=======
# core/rank_bm25.py
"""
AXELR — Pure-Stdlib BM25 Local Retrieval
==========================================
Fills the "Rank-BM25 / Minsearch" gap in the pillar spec. "Minsearch"
is not a real product; Okapi BM25 is the canonical implementation.

Design:
    * Inverted index: term -> {doc_id -> term_frequency}
    * Metadata per doc, filterable at query time
    * LRU eviction when max_docs exceeded
    * Thread-safe
    * Optional SQLite persistence (save/load)
    * Zero third-party deps

RAM: ~45 KB at 5000 short docs. Bounded by max_docs and max_chars_per_doc.

Never raises.
"""
from __future__ import annotations

import contextlib
import logging
import math
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

logger = logging.getLogger("axelr.rank_bm25")

_TOKEN_RE = re.compile(r"[a-z0-9_]{2,}")

_K1 = 1.5
_B = 0.75
_MAX_DOCS = 5000
_MAX_CHARS_PER_DOC = 8000
_MAX_TOKENS_PER_QUERY = 32


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    return _TOKEN_RE.findall(text.lower())[:4096]


@dataclass(frozen=True, slots=True)
class BM25Hit:
    doc_id: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"doc_id": self.doc_id, "score": round(self.score, 4),
                "metadata": self.metadata}


class BM25Index:
    __slots__ = (
        "_lock", "_postings", "_doc_lengths", "_doc_meta",
        "_doc_order", "_total_tokens", "_max_docs",
        "_max_chars_per_doc", "_k1", "_b",
    )

    def __init__(
        self, *, k1: float = _K1, b: float = _B,
        max_docs: int = _MAX_DOCS,
        max_chars_per_doc: int = _MAX_CHARS_PER_DOC,
    ) -> None:
        self._lock = threading.RLock()
        # term -> {doc_id -> tf}
        self._postings: dict[str, dict[str, int]] = {}
        self._doc_lengths: dict[str, int] = {}
        self._doc_meta: dict[str, dict[str, Any]] = {}
        self._doc_order: OrderedDict[str, None] = OrderedDict()
        self._total_tokens = 0
        self._max_docs = max(64, int(max_docs))
        self._max_chars_per_doc = max(256, int(max_chars_per_doc))
        self._k1 = float(k1)
        self._b = float(b)

    # ── mutation ────────────────────────────────────────────────────────
    def add(self, doc_id: str, text: str,
            metadata: dict[str, Any] | None = None) -> bool:
        if not doc_id or not text:
            return False
        try:
            with self._lock:
                self._remove_locked(doc_id)
                text = text[: self._max_chars_per_doc]
                toks = _tokenize(text)
                if not toks:
                    return False
                tf: dict[str, int] = {}
                for t in toks:
                    tf[t] = tf.get(t, 0) + 1
                for term, count in tf.items():
                    self._postings.setdefault(term, {})[doc_id] = count
                self._doc_lengths[doc_id] = len(toks)
                self._doc_meta[doc_id] = dict(metadata or {})
                self._doc_order[doc_id] = None
                self._total_tokens += len(toks)
                self._evict_locked()
            return True
        except Exception as e:
            logger.debug("bm25_add_failed doc=%s error=%s", doc_id, e)
            return False

    def add_many(self, docs: Iterable[dict[str, Any]]) -> int:
        added = 0
        for d in docs:
            if not isinstance(d, dict):
                continue
            if self.add(str(d.get("id") or d.get("doc_id") or ""),
                        str(d.get("text") or ""),
                        d.get("metadata")):
                added += 1
        return added

    def remove(self, doc_id: str) -> bool:
        with self._lock:
            return self._remove_locked(doc_id)

    def _remove_locked(self, doc_id: str) -> bool:
        if doc_id not in self._doc_lengths:
            return False
        dl = self._doc_lengths.pop(doc_id, 0)
        self._doc_meta.pop(doc_id, None)
        self._doc_order.pop(doc_id, None)
        self._total_tokens -= dl
        # Purge from every posting list.
        empty_terms: list[str] = []
        for term, pl in self._postings.items():
            if doc_id in pl:
                del pl[doc_id]
                if not pl:
                    empty_terms.append(term)
        for t in empty_terms:
            self._postings.pop(t, None)
        return True

    def _evict_locked(self) -> None:
        while len(self._doc_order) > self._max_docs:
            oldest, _ = self._doc_order.popitem(last=False)
            self._remove_locked(oldest)

    def clear(self) -> None:
        with self._lock:
            self._postings.clear()
            self._doc_lengths.clear()
            self._doc_meta.clear()
            self._doc_order.clear()
            self._total_tokens = 0

    # ── query ───────────────────────────────────────────────────────────
    def query(
        self, text: str, *, top_k: int = 10,
        filter_fn: Callable[[dict[str, Any]], bool] | None = None,
    ) -> list[BM25Hit]:
        if not text:
            return []
        try:
            qtoks = list(dict.fromkeys(_tokenize(text)))[:_MAX_TOKENS_PER_QUERY]
            if not qtoks:
                return []
            with self._lock:
                n = len(self._doc_lengths)
                if n == 0:
                    return []
                avgdl = (self._total_tokens / n) if n else 0.0
                if avgdl <= 0:
                    return []
                # Gather candidate docs from query terms.
                scores: dict[str, float] = {}
                for t in qtoks:
                    pl = self._postings.get(t)
                    if not pl:
                        continue
                    df = len(pl)
                    idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
                    for doc_id, tf in pl.items():
                        dl = self._doc_lengths.get(doc_id, 0)
                        if dl <= 0:
                            continue
                        denom = tf + self._k1 * (
                            1.0 - self._b + self._b * dl / avgdl)
                        score = idf * (tf * (self._k1 + 1.0)) / denom
                        scores[doc_id] = scores.get(doc_id, 0.0) + score
                if not scores:
                    return []
                # Filter + top-k.
                hits: list[BM25Hit] = []
                for doc_id, score in scores.items():
                    meta = self._doc_meta.get(doc_id, {})
                    if filter_fn is not None and not filter_fn(meta):
                        continue
                    hits.append(BM25Hit(doc_id, score, meta))
            hits.sort(key=lambda h: (-h.score, h.doc_id))
            return hits[: max(1, int(top_k))]
        except Exception as e:
            logger.debug("bm25_query_failed error=%s", e)
            return []

    # ── introspection ───────────────────────────────────────────────────
    def stats(self) -> dict[str, Any]:
        with self._lock:
            n = len(self._doc_lengths)
            terms = len(self._postings)
            avgdl = (self._total_tokens / n) if n else 0.0
        return {
            "documents": n,
            "unique_terms": terms,
            "avg_doc_length": round(avgdl, 2),
            "max_docs": self._max_docs,
            "k1": self._k1, "b": self._b,
        }

    # ── persistence (optional) ─────────────────────────────────────────
    def save(self, path: str) -> bool:
        try:
            with self._lock, sqlite3.connect(path, timeout=5.0) as conn:
                conn.executescript(
                    "CREATE TABLE IF NOT EXISTS bm25_docs "
                    "(id TEXT PRIMARY KEY, text TEXT, meta TEXT);")
                conn.execute("DELETE FROM bm25_docs;")
                for doc_id, meta in self._doc_meta.items():
                    conn.execute(
                        "INSERT INTO bm25_docs(id, text, meta) VALUES (?,?,?)",
                        (doc_id, "", _json_dumps(meta)))
                conn.commit()
            return True
        except Exception as e:
            logger.debug("bm25_save_failed error=%s", e)
            return False

    def load(self, path: str) -> int:
        try:
            with sqlite3.connect(path, timeout=5.0) as conn:
                cur = conn.execute("SELECT id, meta FROM bm25_docs")
                rows = cur.fetchall()
            for doc_id, meta_json in rows:
                self.add(doc_id, doc_id, _json_loads(meta_json))
            return len(rows)
        except Exception as e:
            logger.debug("bm25_load_failed error=%s", e)
            return 0


def _json_dumps(obj: Any) -> str:
    import json
    return json.dumps(obj, separators=(",", ":"))


def _json_loads(s: str) -> dict[str, Any]:
    import json
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


# ── module singleton ─────────────────────────────────────────────────────
_INDEX: BM25Index | None = None
_INDEX_LOCK = threading.Lock()


def get_bm25_index() -> BM25Index:
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    with _INDEX_LOCK:
        if _INDEX is None:
            _INDEX = BM25Index()
        return _INDEX


def audit() -> dict[str, Any]:
    if _INDEX is None:
        return {"initialized": False}
    return {"initialized": True, **_INDEX.stats()}


>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
__all__ = ["BM25Index", "BM25Hit", "get_bm25_index", "audit"]