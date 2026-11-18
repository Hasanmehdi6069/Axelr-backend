# core/cache.py
"""
AXELR — Cache Layer (prefix · Vectorize · SQLite-FTS5 · BM25)
===============================================================
Four caches, one file — they share the Redis handle, the Prometheus
surface, and the embedding primitive.

    §1  Prefix cache       dedup system-prompt block across requests
    §2  Semantic cache     Cloudflare Vectorize + Workers AI embeddings
    §3  Local SQLite cache SQLite + FTS5 for sub-ms cold-start lookups
    §4  BM25 index         pure-stdlib inverted index for local retrieval

Contract: every method catches its own exceptions, returns a safe default.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import os
import re
import sqlite3
import struct
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import httpx

from .foundation import load as _cfg
from .infra import cf_embed_batch, get_redis_client

logger = logging.getLogger("axelr.cache")

# ── Cloudflare endpoints ──────────────────────────────────────────────────
_CF_BASE = "https://api.cloudflare.com/client/v4/accounts/{acct}"
_VECTOR_URL = _CF_BASE + "/vectorize/v2/indexes/{index}{suffix}"
_CF_KEY = (os.getenv("CLOUDFLARE_API_KEY") or "").strip()
_CF_ACCT = (os.getenv("CLOUDFLARE_ACCOUNT_ID") or "").strip()
_CF_INDEX = os.getenv("CLOUDFLARE_VECTORIZE_INDEX", "axelr-semantic-cache").strip()
_CF_MODEL = os.getenv("CLOUDFLARE_EMBED_MODEL", "@cf/baai/bge-base-en-v1.5").strip()
_CF_ENABLED = bool(_CF_KEY and _CF_ACCT)


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Prometheus counters (defensive; falls back to no-op)
# ═══════════════════════════════════════════════════════════════════════════
try:
    from prometheus_client import REGISTRY, Counter

    def _metric(name: str, factory):
        try:
            return factory()
        except ValueError:
            return getattr(REGISTRY, "_names_to_collectors", {}).get(name)

    _PREFIX_HITS = _metric("axelr_prefix_cache_hits_total", lambda: Counter(
        "axelr_prefix_cache_hits_total", "Prefix-cache hits", ["workspace", "tier"]))
    _PREFIX_MISSES = _metric("axelr_prefix_cache_misses_total", lambda: Counter(
        "axelr_prefix_cache_misses_total", "Prefix-cache misses", ["workspace", "tier"]))
    _PROM_OK = True
except Exception as e:
    logger.warning("prometheus_unavailable error=%s", e)
    _PREFIX_HITS = _PREFIX_MISSES = None
    _PROM_OK = False


def prefix_hit(workspace: str = "core", tier: str = "free") -> None:
    if _PREFIX_HITS is None:
        return
    with contextlib.suppress(Exception):
        _PREFIX_HITS.labels(workspace=workspace or "core", tier=tier or "free").inc()


def prefix_miss(workspace: str = "core", tier: str = "free") -> None:
    if _PREFIX_MISSES is None:
        return
    with contextlib.suppress(Exception):
        _PREFIX_MISSES.labels(workspace=workspace or "core", tier=tier or "free").inc()


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Prefix cache
# ═══════════════════════════════════════════════════════════════════════════
_PREFIX_TTL_S = int(os.getenv("PREFIX_CACHE_TTL_S", "3600"))


def _prefix_hash(*parts: str) -> str:
    h = hashlib.blake2b(digest_size=8)
    for p in parts:
        h.update((p or "").encode("utf-8", errors="ignore"))
        h.update(b"|")
    return h.hexdigest()


def canonical_prefix(workspace: str, intent: str, tier: str,
                     extra_directives: str = "") -> tuple[str, str]:
    pid = _prefix_hash(workspace or "core", intent or "strategy",
                       tier or "free", extra_directives or "")
    return pid, extra_directives


_PREFIX_LOCAL_CACHE: dict[str, str] = {}


def remember_prefix(workspace: str, intent: str, tier: str, result: str) -> None:
    pid, _ = canonical_prefix(workspace, intent, tier)
    try:
        r = get_redis_client()
        if r is not None:
            r.setex(f"prefix:{pid}", _PREFIX_TTL_S, result)
        else:
            _PREFIX_LOCAL_CACHE[pid] = result
    except Exception as e:
        logger.debug("prefix_remember_failed error=%s", e)
        _PREFIX_LOCAL_CACHE[pid] = result


def recall_prefix(workspace: str, intent: str, tier: str) -> str | None:
    pid, _ = canonical_prefix(workspace, intent, tier)
    try:
        r = get_redis_client()
        if r is not None:
            v = r.get(f"prefix:{pid}")
            if v is None:
                return None
            return v if isinstance(v, str) else v.decode("utf-8", "ignore")
        else:
            return _PREFIX_LOCAL_CACHE.get(pid)
    except Exception:
        return _PREFIX_LOCAL_CACHE.get(pid)


def provider_cache_hint(prefix_text: str, provider: str) -> dict[str, Any]:
    p = (provider or "").lower()
    result = {"prefix": prefix_text, "provider": provider}
    if p == "anthropic":
        result["system"] = [{"type": "text", "text": prefix_text, "cache_control": {"type": "ephemeral"}}]
    if p == "gemini":
        result["_axelr_prefix_id"] = prefix_text
    return result


def measure_hit(prefix_id: str) -> None:
    try:
        r = get_redis_client()
        if r is not None:
            r.incr(f"prefix:hits:{prefix_id}")
            r.expire(f"prefix:hits:{prefix_id}", _PREFIX_TTL_S)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Semantic cache (Cloudflare Vectorize, exact-match LRU fallback)
# ═══════════════════════════════════════════════════════════════════════════
_LOCAL_MAX = int(os.getenv("SEMANTIC_CACHE_MAX_ENTRIES", "2000"))
_LOCAL_TTL = int(os.getenv("SEMANTIC_CACHE_TTL", "3600"))
_DEFAULT_MIN_SCORE = float(os.getenv("SEMANTIC_CACHE_MIN_SCORE", "0.85"))


@dataclass(slots=True)
class CacheEntry:
    prompt: str
    response: str
    score: float = 1.0
    created_at: float = field(default_factory=time.time)
    provider: str = "vectorize"

    def to_dict(self) -> dict[str, Any]:
        return {"prompt": self.prompt, "response": self.response,
                "score": self.score, "created_at": self.created_at,
                "provider": self.provider}


class _LocalCache:
    __slots__ = ("_max", "_ttl", "_store", "_lock")

    def __init__(self, max_entries: int, ttl: int) -> None:
        self._max = max_entries
        self._ttl = ttl
        self._store: OrderedDict[str, tuple[float, str]] = OrderedDict()
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
    """Semantic cache over Vectorize. Degrades to exact-match when CF is absent."""

    __slots__ = ("_api_key", "_account_id", "_index_name", "_embed_model",
                 "_enabled", "_http", "_owns_http", "_local")

    def __init__(self, api_key: str | None = None, account_id: str | None = None,
                 index_name: str | None = None, embed_model: str | None = None,
                 http_client: httpx.AsyncClient | None = None) -> None:
        self._api_key = (api_key or _CF_KEY).strip()
        self._account_id = (account_id or _CF_ACCT).strip()
        self._index_name = (index_name or _CF_INDEX).strip()
        self._embed_model = (embed_model or _CF_MODEL).strip()
        self._enabled = bool(self._api_key and self._account_id)
        self._http = http_client
        self._owns_http = http_client is None
        self._local = _LocalCache(_LOCAL_MAX, _LOCAL_TTL)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0))
        return self._http

    async def _embed(self, text: str) -> list[float]:
        vecs = await cf_embed_batch([text], model=self._embed_model)
        if not vecs:
            raise RuntimeError("cf_embed_failed")
        return vecs[0]

    async def get(self, prompt: str) -> str | None:
        if not prompt:
            return None
        with contextlib.suppress(Exception):
            exact = await self._local.get(prompt)
            if exact is not None:
                return exact
        if not self._enabled:
            return None
        try:
            vec = await self._embed(prompt)
        except Exception as e:
            logger.debug("vectorize_embed_failed error=%s", e)
            return None
        try:
            url = _VECTOR_URL.format(acct=self._account_id, index=self._index_name, suffix="/query")
            r = await self._client().post(
                url,
                headers={"Authorization": f"Bearer {self._api_key}",
                         "Content-Type": "application/json"},
                json={"vector": vec, "topK": 1, "returnMetadata": "all"},
            )
            r.raise_for_status()
            matches = (r.json().get("result") or {}).get("matches") or []
        except Exception as e:
            logger.debug("vectorize_query_failed error=%s", e)
            return None
        if not matches:
            return None
        best = matches[0]
        if float(best.get("score") or 0.0) < _DEFAULT_MIN_SCORE:
            return None
        response = (best.get("metadata") or {}).get("response")
        if isinstance(response, str) and response:
            with contextlib.suppress(Exception):
                await self._local.set(prompt, response)
            return response
        return None

    async def set(self, prompt: str, response: str) -> None:
        if not prompt or not response:
            return
        with contextlib.suppress(Exception):
            await self._local.set(prompt, response)
        if not self._enabled:
            return
        try:
            vec = await self._embed(prompt)
        except Exception:
            return
        vid = hashlib.sha256(prompt.strip().lower().encode()).hexdigest()[:32]
        try:
            url = _VECTOR_URL.format(acct=self._account_id, index=self._index_name, suffix="/upsert")
            await self._client().post(
                url,
                headers={"Authorization": f"Bearer {self._api_key}",
                         "Content-Type": "application/json"},
                json={"vectors": [{"id": vid, "values": vec,
                                   "metadata": {"prompt": prompt[:1000],
                                                "response": response,
                                                "ts": int(time.time())}}]},
            )
        except Exception as e:
            logger.debug("vectorize_upsert_failed error=%s", e)

    async def clear(self) -> None:
        with contextlib.suppress(Exception):
            await self._local.clear()

    async def close(self) -> None:
        if self._owns_http and self._http is not None:
            with contextlib.suppress(Exception):
                await self._http.aclose()
            self._http = None


_SEMANTIC: CloudflareVectorizeCache | None = None


def get_vector_cache() -> CloudflareVectorizeCache:
    global _SEMANTIC
    if _SEMANTIC is None:
        _SEMANTIC = CloudflareVectorizeCache()
    return _SEMANTIC


# ═══════════════════════════════════════════════════════════════════════════
# §4 — Local SQLite cache (FTS5, sub-ms cold-start)
# ═══════════════════════════════════════════════════════════════════════════
def _hash_prompt(prompt: str) -> str:
    return hashlib.sha256(prompt.strip().lower().encode("utf-8", errors="ignore")).hexdigest()


_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS entries (
    prompt_hash TEXT PRIMARY KEY, prompt TEXT NOT NULL, response TEXT NOT NULL,
    workspace TEXT NOT NULL DEFAULT 'core', created_at REAL NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_entries_ws ON entries (workspace);
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
    prompt, response, workspace UNINDEXED,
    content='entries', content_rowid='rowid', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
    INSERT INTO entries_fts(rowid, prompt, response, workspace)
    VALUES (new.rowid, new.prompt, new.response, new.workspace);
END;
"""


class LocalSemanticCache:
    __slots__ = ("_conn", "_lock", "_path", "_ttl_s", "_max_rows", "_stats")

    def __init__(self, path: str | None = None, *, ttl_s: int = 3600,
                 max_rows: int = 20_000) -> None:
        self._path = path or os.getenv("LOCAL_CACHE_PATH", "/tmp/axelr_cache.db")
        self._ttl_s = max(60, int(ttl_s))
        self._max_rows = max(100, int(max_rows))
        self._lock = threading.Lock()
        self._stats = {"hits": 0, "misses": 0, "writes": 0}
        self._conn = self._open()
        with contextlib.suppress(Exception):
            with self._lock:
                self._conn.executescript(_DDL)

    def _open(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(self._path, timeout=5.0,
                                   isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            return conn
        except Exception as e:
            logger.warning("local_cache_open_failed path=%s error=%s", self._path, e)
            conn = sqlite3.connect(":memory:", check_same_thread=False)
            conn.row_factory = sqlite3.Row
            return conn

    def put(self, prompt: str, response: str, *, workspace: str = "core") -> bool:
        if not prompt or not response:
            return False
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO entries "
                    "(prompt_hash, prompt, response, workspace, created_at, hits) "
                    "VALUES (?, ?, ?, ?, ?, 0)",
                    (_hash_prompt(prompt), prompt[:4000], response,
                     workspace or "core", time.time()))
                self._stats["writes"] += 1
                # Evict oldest entries if we exceed max_rows
                rows = self._conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
                if rows > self._max_rows:
                    self._conn.execute(
                        "DELETE FROM entries WHERE rowid IN "
                        "(SELECT rowid FROM entries ORDER BY created_at ASC LIMIT ?)",
                        (rows - self._max_rows,)
                    )
                    # Also evict from FTS5 table to maintain consistency
                    self._conn.execute(
                        "DELETE FROM entries_fts WHERE rowid NOT IN (SELECT rowid FROM entries)"
                    )
            return True
        except Exception as e:
            logger.debug("local_cache_put_failed error=%s", e)
            return False

    def get(self, prompt: str, *, workspace: str = "core") -> str | None:
        if not prompt:
            return None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT response, created_at FROM entries WHERE prompt_hash=? AND workspace=? LIMIT 1",
                    (_hash_prompt(prompt), workspace or "core")).fetchone()
                if row is not None and self._fresh(row["created_at"]):
                    self._stats["hits"] += 1
                    return row["response"]
                self._stats["misses"] += 1
        except Exception as e:
            logger.debug("local_cache_get_failed error=%s", e)
        return None

    def _fresh(self, ts: float) -> bool:
        return (time.time() - float(ts)) < self._ttl_s

    def stats(self) -> dict[str, Any]:
        with self._lock:
            try:
                rows = self._conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            except Exception:
                rows = 0
        total = self._stats["hits"] + self._stats["misses"]
        return {"path": self._path, "rows": rows, **self._stats,
                "hit_rate": round(self._stats["hits"] / total, 3) if total else 0.0}

    def close(self) -> None:
        with contextlib.suppress(Exception):
            with self._lock:
                self._conn.close()


_LOCAL_SINGLETON: LocalSemanticCache | None = None


def get_local_cache() -> LocalSemanticCache:
    global _LOCAL_SINGLETON
    if _LOCAL_SINGLETON is None:
        _LOCAL_SINGLETON = LocalSemanticCache()
    return _LOCAL_SINGLETON


# ═══════════════════════════════════════════════════════════════════════════
# §5 — BM25 local index
# ═══════════════════════════════════════════════════════════════════════════
_TOKEN_RE = re.compile(r"[a-z0-9_]{2,}")
_K1 = 1.5
_B = 0.75
_MAX_DOCS = 5000
_MAX_CHARS_PER_DOC = 8000
_MAX_QUERY_TOKENS = 32


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
    __slots__ = ("_lock", "_postings", "_doc_lengths", "_doc_meta",
                 "_doc_order", "_total_tokens", "_max_docs", "_max_chars",
                 "_k1", "_b")

    def __init__(self, *, k1: float = _K1, b: float = _B,
                 max_docs: int = _MAX_DOCS,
                 max_chars_per_doc: int = _MAX_CHARS_PER_DOC) -> None:
        self._lock = threading.RLock()
        self._postings: dict[str, dict[str, int]] = {}
        self._doc_lengths: dict[str, int] = {}
        self._doc_meta: dict[str, dict[str, Any]] = {}
        self._doc_order: OrderedDict[str, None] = OrderedDict()
        self._total_tokens = 0
        self._max_docs = int(max_docs)
        self._max_chars = max(256, int(max_chars_per_doc))
        self._k1 = float(k1)
        self._b = float(b)

    def add(self, doc_id: str, text: str,
            metadata: dict[str, Any] | None = None) -> bool:
        if not doc_id or not text:
            return False
        try:
            with self._lock:
                self._remove_locked(doc_id)
                text = text[: self._max_chars]
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
                # Evict oldest entries if we exceed max_docs
                while len(self._doc_order) > self._max_docs:
                    oldest, _ = self._doc_order.popitem(last=False)
                    self._remove_locked(oldest)
            return True
        except Exception as e:
            logger.debug("bm25_add_failed doc=%s error=%s", doc_id, e)
            return False

    def _remove_locked(self, doc_id: str) -> bool:
        if doc_id not in self._doc_lengths:
            return False
        dl = self._doc_lengths.pop(doc_id, 0)
        self._doc_meta.pop(doc_id, None)
        self._doc_order.pop(doc_id, None)
        self._total_tokens -= dl
        empty: list[str] = []
        for term, pl in self._postings.items():
            if doc_id in pl:
                del pl[doc_id]
                if not pl:
                    empty.append(term)
        for t in empty:
            self._postings.pop(t, None)
        return True

    def _evict_locked(self) -> None:
        while len(self._doc_order) > self._max_docs:
            oldest, _ = self._doc_order.popitem(last=False)
            self._remove_locked(oldest)

    def query(self, text: str, *, top_k: int = 10,
              filter_fn: Callable[[dict[str, Any]], bool] | None = None
              ) -> list[BM25Hit]:
        if not text:
            return []
        try:
            qtoks = list(dict.fromkeys(_tokenize(text)))[:_MAX_QUERY_TOKENS]
            if not qtoks:
                return []
            with self._lock:
                n = len(self._doc_lengths)
                if n == 0:
                    return []
                avgdl = (self._total_tokens / n) if n else 0.0
                if avgdl <= 0:
                    return []
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
                        denom = tf + self._k1 * (1.0 - self._b + self._b * dl / avgdl)
                        scores[doc_id] = scores.get(doc_id, 0.0) + \
                            idf * (tf * (self._k1 + 1.0)) / denom
                if not scores:
                    return []
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

    def stats(self) -> dict[str, Any]:
        with self._lock:
            n = len(self._doc_lengths)
            return {"documents": n, "unique_terms": len(self._postings),
                    "avg_doc_length": round(self._total_tokens / n, 2) if n else 0.0,
                    "max_docs": self._max_docs, "k1": self._k1, "b": self._b}


_BM25_SINGLETON: BM25Index | None = None


def get_bm25_index() -> BM25Index:
    global _BM25_SINGLETON
    if _BM25_SINGLETON is None:
        _BM25_SINGLETON = BM25Index()
    return _BM25_SINGLETON


# ═══════════════════════════════════════════════════════════════════════════
# Audit
# ═══════════════════════════════════════════════════════════════════════════
def audit() -> dict[str, Any]:
    return {
        "prefix_cache": {"ttl_s": _PREFIX_TTL_S},
        "semantic_cache": {"enabled": _CF_ENABLED, "index": _CF_INDEX},
        "local_cache": _LOCAL_SINGLETON.stats() if _LOCAL_SINGLETON else {"initialised": False},
        "bm25": _BM25_SINGLETON.stats() if _BM25_SINGLETON else {"initialised": False},
        "prometheus": {"enabled": _PROM_OK},
    }


# Compatibility aliases for test_rag.py
SemanticCache = LocalSemanticCache
BM25Cache = BM25Index
get_bm25_singleton = get_bm25_index

__all__ = [
    # Prefix
    "canonical_prefix", "remember_prefix", "recall_prefix",
    "provider_cache_hint", "measure_hit", "prefix_hit", "prefix_miss",
    # Semantic
    "CacheEntry", "CloudflareVectorizeCache", "get_vector_cache",
    # Local SQLite
    "LocalSemanticCache", "get_local_cache", "SemanticCache",
    # BM25
    "BM25Index", "BM25Hit", "get_bm25_index", "BM25Cache", "get_bm25_singleton",
    # Audit
    "audit",
]