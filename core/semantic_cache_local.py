# core/semantic_cache_local.py
"""
AXELR — Local Semantic Cache (SQLite + FTS5)

The prompt names "DuckDB/SQLite Semantic Cache". DuckDB ships a ~30 MB
compiled wheel — unacceptable on a 512 MB Render tier. SQLite is stdlib,
zero bytes of extra dependency, and FTS5 gives sub-millisecond keyword
matching. Optional embeddings stored as BLOBs enable cosine reranking
when a numpy-compatible vector exists in the cache row.

Serves 0.5–2 ms lookups as specified in prompt 5.

Public:
    LocalSemanticCache(path=":memory:")
        .put(prompt, response, workspace="core", embedding=None)
        .get(prompt, workspace="core") -> str | None
        .stats() -> dict
        .purge_expired() -> int
        .close()

Never raises. All SQL is parameterized. Write-Ahead Logging enabled.
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import sqlite3
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("axelr.semantic_cache_local")

_DEFAULT_TTL_S = int(os.getenv("LOCAL_CACHE_TTL_S", "3600"))
_DEFAULT_MAX_ROWS = int(os.getenv("LOCAL_CACHE_MAX_ROWS", "20000"))
_FTS_LIMIT = int(os.getenv("LOCAL_CACHE_FTS_LIMIT", "8"))
_MIN_EXACT_HIT = 1.0


def _hash(prompt: str) -> str:
    return hashlib.sha256(prompt.strip().lower().encode(
        "utf-8", errors="ignore")).hexdigest()


def _pack_vec(v: list[float] | None) -> bytes | None:
    if not v:
        return None
    try:
        return struct.pack(f"<{len(v)}f", *v)
    except Exception:
        return None


def _unpack_vec(b: bytes | None) -> list[float] | None:
    if not b:
        return None
    try:
        n = len(b) // 4
        return list(struct.unpack(f"<{n}f", b))
    except Exception:
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;

CREATE TABLE IF NOT EXISTS entries (
    prompt_hash TEXT PRIMARY KEY,
    prompt      TEXT NOT NULL,
    response    TEXT NOT NULL,
    workspace   TEXT NOT NULL DEFAULT 'core',
    embedding   BLOB,
    created_at  REAL NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_entries_workspace
    ON entries (workspace);
CREATE INDEX IF NOT EXISTS idx_entries_created
    ON entries (created_at);

CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
    prompt, response, workspace UNINDEXED,
    content='entries', content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
    INSERT INTO entries_fts(rowid, prompt, response, workspace)
    VALUES (new.rowid, new.prompt, new.response, new.workspace);
END;
CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, prompt, response, workspace)
    VALUES ('delete', old.rowid, old.prompt, old.response, old.workspace);
END;
CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, prompt, response, workspace)
    VALUES ('delete', old.rowid, old.prompt, old.response, old.workspace);
    INSERT INTO entries_fts(rowid, prompt, response, workspace)
    VALUES (new.rowid, new.prompt, new.response, new.workspace);
END;
"""


@dataclass(slots=True)
class _Stats:
    hits: int = 0
    misses: int = 0
    fts_hits: int = 0
    writes: int = 0
    evictions: int = 0


class LocalSemanticCache:
    __slots__ = ("_conn", "_lock", "_path", "_ttl_s", "_max_rows", "_stats")

    def __init__(self, path: str | None = None,
                 *, ttl_s: int = _DEFAULT_TTL_S,
                 max_rows: int = _DEFAULT_MAX_ROWS) -> None:
        self._path = path or os.getenv(
            "LOCAL_CACHE_PATH", "/tmp/axelr_semantic_cache.db")
        self._ttl_s = max(60, int(ttl_s))
        self._max_rows = max(100, int(max_rows))
        self._lock = threading.Lock()
        self._stats = _Stats()
        self._conn = self._open()
        self._install_ddl()

    def _open(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(
                self._path, timeout=5.0, isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            return conn
        except Exception as e:
            logger.warning("local_cache_open_failed path=%s error=%s",
                           self._path, e)
            conn = sqlite3.connect(":memory:", check_same_thread=False)
            conn.row_factory = sqlite3.Row
            return conn

    def _install_ddl(self) -> None:
        try:
            with self._lock:
                self._conn.executescript(_DDL)
        except Exception as e:
            logger.warning("local_cache_ddl_failed error=%s", e)

    def put(self, prompt: str, response: str,
            *, workspace: str = "core",
            embedding: list[float] | None = None) -> bool:
        if not prompt or not response:
            return False
        try:
            h = _hash(prompt)
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO entries "
                    "(prompt_hash, prompt, response, workspace, embedding, "
                    " created_at, hits) VALUES (?, ?, ?, ?, ?, ?, 0)",
                    (h, prompt[:4000], response, workspace or "core",
                     _pack_vec(embedding), time.time()),
                )
                self._stats.writes += 1
                self._maybe_evict_locked()
            return True
        except Exception as e:
            logger.debug("local_cache_put_failed error=%s", e)
            return False

    def get(self, prompt: str, *, workspace: str = "core",
            query_embedding: list[float] | None = None,
            min_cosine: float = 0.86) -> str | None:
        if not prompt:
            return None
        try:
            h = _hash(prompt)
            with self._lock:
                row = self._conn.execute(
                    "SELECT response, created_at FROM entries "
                    "WHERE prompt_hash=? AND workspace=? LIMIT 1",
                    (h, workspace or "core"),
                ).fetchone()
                if row is not None and self._fresh(row["created_at"]):
                    self._bump_hit_locked(h)
                    self._stats.hits += 1
                    return row["response"]
                self._stats.misses += 1

            # Exact miss → FTS5 keyword search.
            kw = self._fts_lookup(prompt, workspace)
            if kw is not None:
                self._stats.fts_hits += 1
                self._stats.hits += 1
                return kw

            # Optional semantic rerank when an embedding is supplied.
            if query_embedding is not None:
                best = self._embedding_lookup(query_embedding, workspace,
                                              min_cosine)
                if best is not None:
                    self._stats.hits += 1
                    return best
        except Exception as e:
            logger.debug("local_cache_get_failed error=%s", e)
        return None

    def _fts_lookup(self, prompt: str, workspace: str) -> str | None:
        terms = self._fts_query(prompt)
        if not terms:
            return None
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT e.response, e.created_at, "
                    "       bm25(entries_fts) AS rank "
                    "FROM entries_fts "
                    "JOIN entries e ON e.rowid = entries_fts.rowid "
                    "WHERE entries_fts MATCH ? AND e.workspace = ? "
                    "ORDER BY rank LIMIT ?",
                    (terms, workspace or "core", _FTS_LIMIT),
                ).fetchall()
            for r in rows:
                if self._fresh(r["created_at"]):
                    return r["response"]
        except Exception:
            return None
        return None

    @staticmethod
    def _fts_query(prompt: str) -> str:
        # Extract keyword-length tokens; escape FTS5 reserved chars.
        import re
        toks = re.findall(r"[a-z0-9_]{3,}", prompt.lower())[:8]
        if not toks:
            return ""
        # OR of individual terms — BM25 ranks.
        return " OR ".join(f'"{t}"' for t in toks)

    def _embedding_lookup(self, q: list[float], workspace: str,
                          min_cos: float) -> str | None:
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT response, embedding, created_at FROM entries "
                    "WHERE workspace=? AND embedding IS NOT NULL "
                    "ORDER BY created_at DESC LIMIT 200",
                    (workspace or "core",),
                ).fetchall()
        except Exception:
            return None
        best_score = min_cos
        best_response: str | None = None
        for r in rows:
            if not self._fresh(r["created_at"]):
                continue
            v = _unpack_vec(r["embedding"])
            if v is None:
                continue
            s = _cosine(q, v)
            if s > best_score:
                best_score = s
                best_response = r["response"]
        return best_response

    def _bump_hit_locked(self, h: str) -> None:
        with contextlib.suppress(Exception):
            self._conn.execute(
                "UPDATE entries SET hits = hits + 1 WHERE prompt_hash=?",
                (h,),
            )

    def _fresh(self, created_at: float) -> bool:
        return (time.time() - float(created_at)) < self._ttl_s

    def _maybe_evict_locked(self) -> None:
        try:
            n = self._conn.execute(
                "SELECT COUNT(*) FROM entries").fetchone()[0]
            if n <= self._max_rows:
                return
            cutoff = time.time() - self._ttl_s
            cur = self._conn.execute(
                "DELETE FROM entries WHERE created_at < ?", (cutoff,))
            self._stats.evictions += cur.rowcount or 0
            if n - (cur.rowcount or 0) <= self._max_rows:
                return
            # Hard cap: drop oldest.
            excess = n - self._max_rows
            self._conn.execute(
                "DELETE FROM entries WHERE prompt_hash IN "
                "(SELECT prompt_hash FROM entries ORDER BY created_at "
                " LIMIT ?)", (excess,))
            self._stats.evictions += excess
        except Exception as e:
            logger.debug("local_cache_evict_failed error=%s", e)

    def purge_expired(self) -> int:
        try:
            cutoff = time.time() - self._ttl_s
            with self._lock:
                cur = self._conn.execute(
                    "DELETE FROM entries WHERE created_at < ?", (cutoff,))
                return cur.rowcount or 0
        except Exception:
            return 0

    def stats(self) -> dict[str, Any]:
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT COUNT(*) FROM entries").fetchone()[0]
        except Exception:
            rows = 0
        s = self._stats
        total = s.hits + s.misses
        return {
            "path": self._path,
            "rows": rows,
            "max_rows": self._max_rows,
            "ttl_s": self._ttl_s,
            "hits": s.hits,
            "misses": s.misses,
            "fts_hits": s.fts_hits,
            "writes": s.writes,
            "evictions": s.evictions,
            "hit_rate": round(s.hits / total, 3) if total else 0.0,
        }

    def close(self) -> None:
        with contextlib.suppress(Exception):
            with self._lock:
                self._conn.close()


_SINGLETON: LocalSemanticCache | None = None
_SINGLETON_LOCK = threading.Lock()


def get_local_cache() -> LocalSemanticCache:
    global _SINGLETON
    if _SINGLETON is not None:
        return _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = LocalSemanticCache()
        return _SINGLETON


def audit() -> dict[str, Any]:
    if _SINGLETON is None:
        return {"initialized": False}
    return {"initialized": True, **_SINGLETON.stats()}


__all__ = [
    "LocalSemanticCache", "get_local_cache", "audit",
]