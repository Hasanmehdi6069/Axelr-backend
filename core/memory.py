# core/memory.py
"""
AXELR — Conversation & Fact Memory
====================================
Cross-chat semantic memory. Three layers (each independently optional):
    1. Redis      — recent exchanges (list, TTL 7 days, sub-ms)
    2. Vectorize  — semantic index + Workers AI embeddings
    3. Supabase   — cold audit copy (SQL, queryable)

Never raises. Every method has a degradation path.

Companion module:
    core/repo_indexer.RepoIndexer — GitHub repo → Vectorize index

Public:
    MemoryItem · Memory · get_memory
    ConversationMemory · MemoryBus (backward-compat aliases)
"""
from __future__ import annotations

import ast          # noqa: F401 — used by RepoIndexer._chunk_python
import asyncio      # noqa: F401 — used by RepoIndexer, _chunk_file
import base64       # noqa: F401 — used by RepoIndexer._fetch_blob
import contextlib
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path  # noqa: F401 — used by RepoIndexer helpers
from typing import Any

import httpx
logger = logging.getLogger("axelr.memory")

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
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _make_id(user_id: str, session_id: str, text: str) -> str:
    return _sha(f"{user_id}|{session_id}|{time.time_ns()}|{text}")[:32]

# ═══════════════════════════════════════════════════════════════════════════
# §0 — Shared Vectorize client + embed helper
# ═══════════════════════════════════════════════════════════════════════════
class _VectorizeClient:
    """Minimal Cloudflare Vectorize REST wrapper. Shared by §1 and §2."""
    __slots__ = ("_acct", "_key", "_http", "_index")

    def __init__(self, acct: str, key: str, index: str, http: httpx.AsyncClient):
        self._acct = acct
        self._key = key
        self._index = index
        self._http = http

    @property
    def enabled(self) -> bool:
        return bool(self._acct and self._key and self._index)

    def _url(self, suffix: str) -> str:
        return _VECTOR_URL.format(acct=self._acct, index=self._index, suffix=suffix)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json"}

    async def upsert(self, vectors: list[dict], timeout: float = 30.0) -> bool:
        if not self.enabled or not vectors:
            return False
        try:
            r = await self._http.post(self._url("/upsert"), headers=self._headers(),
                                      json={"vectors": vectors}, timeout=timeout)
            r.raise_for_status()
            return True
        except Exception as e:
            logger.warning("vectorize_upsert_failed error=%s", e)
            return False

    async def query(self, vector: Sequence[float], *, top_k: int = 10,
                    filters: dict[str, Any] | None = None,
                    timeout: float = 15.0) -> list[dict]:
        if not self.enabled:
            return []
        body: dict[str, Any] = {"vector": list(vector), "topK": int(top_k),
                                "returnValues": False, "returnMetadata": "all"}
        if filters:
            body["filter"] = filters
        try:
            r = await self._http.post(self._url("/query"), headers=self._headers(),
                                      json=body, timeout=timeout)
            r.raise_for_status()
            return list(((r.json().get("result") or {}).get("matches")) or [])
        except Exception as e:
            logger.warning("vectorize_query_failed error=%s", e)
            return []

    async def delete_by_ids(self, ids: list[str], timeout: float = 30.0) -> bool:
        if not self.enabled or not ids:
            return False
        try:
            r = await self._http.post(self._url("/delete_by_ids"),
                                      headers=self._headers(),
                                      json={"ids": ids}, timeout=timeout)
            r.raise_for_status()
            return True
        except Exception as e:
            logger.warning("vectorize_delete_failed error=%s", e)
            return False


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Memory (was core/memory.py)
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class MemoryItem:
    id: str
    text: str
    role: str
    session_id: str
    score: float
    created_at: float

    def render(self) -> str:
        return f"[{self.role}] {self.text}"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "role": self.role,
                "session_id": self.session_id, "score": round(self.score, 4),
                "created_at": self.created_at}


class Memory:
    """Chat + fact memory. Never raises. See module docstring."""
    __slots__ = ("_chat_vec", "_fact_vec", "_acct", "_key", "_http",
                 "_owns_http", "_redis", "_supabase", "_recent_limit",
                 "_embed_prefix", "_recent_prefix", "_ids_prefix",
                 "_chat_index", "_fact_index")

    def __init__(self, *, account_id: str | None = None,
                 api_key: str | None = None,
                 chat_index: str | None = None,
                 fact_index: str | None = None,
                 redis_client: Any | None = None,
                 http_client: httpx.AsyncClient | None = None,
                 recent_limit: int = _DEFAULT_RECENT_LIMIT) -> None:
        self._acct = (account_id or os.getenv("CLOUDFLARE_ACCOUNT_ID") or "").strip()
        self._key = (api_key or os.getenv("CLOUDFLARE_API_KEY") or "").strip()
        self._chat_index = (chat_index or _CHAT_INDEX).strip()
        self._fact_index = (fact_index or _FACT_INDEX).strip()
        if http_client is None:
            self._http = httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5))
            self._owns_http = True
        else:
            self._http = http_client
            self._owns_http = False
        self._chat_vec = _VectorizeClient(self._acct, self._key,
                                          self._chat_index, self._http)
        self._fact_vec = _VectorizeClient(self._acct, self._key,
                                          self._fact_index, self._http)
        self._redis = redis_client
        self._supabase: Any = None
        self._recent_limit = max(1, int(recent_limit))
        self._embed_prefix = "axelr:convmem:embed"
        self._recent_prefix = "axelr:convmem:recent"
        self._ids_prefix = "axelr:convmem:ids"

    def attach_supabase(self, client: Any) -> None:
        self._supabase = client

    @property
    def vector_enabled(self) -> bool:
        return self._chat_vec.enabled

    async def remember(self, *, user_id: str, session_id: str, role: str,
                       text: str, metadata: dict[str, Any] | None = None
                       ) -> str | None:
        try:
            if not text or not text.strip():
                return None
            text = text.strip()[:_MAX_TEXT_CHARS]
            vid = _make_id(user_id, session_id, text)
            now = time.time()
            await self._recent_push(user_id, session_id, MemoryItem(
                id=vid, text=text, role=role, session_id=session_id,
                score=1.0, created_at=now))
            if self._chat_vec.enabled:
                vec = await self._embed(text)
                if vec:
                    md: dict[str, Any] = {
                        "user_id": user_id[:64], "session_id": session_id[:64],
                        "role": role[:16], "text": text, "created_at": float(now)}
                    if metadata:
                        for k, v in metadata.items():
                            if isinstance(v, (str, int, float, bool)) and len(str(v)) < 256:
                                md[str(k)[:32]] = v
                    if await self._chat_vec.upsert([{"id": vid, "values": vec,
                                                     "metadata": md}]):
                        await self._track_id(user_id, session_id, vid)
            if self._supabase is not None:
                with contextlib.suppress(Exception):
                    await self._supabase.execute(
                        "insert into memory (user_id, session_id, role, text, "
                        "created_at) values ($1,$2,$3,$4,to_timestamp($5))",
                        user_id, session_id, role, text, now)
            return vid
        except Exception as e:
            logger.warning("memory_remember_failed error=%s", e)
            return None

    async def recall(self, *, user_id: str, query: str,
                     top_k: int = _DEFAULT_TOP_K, session_id: str | None = None,
                     min_score: float = _DEFAULT_MIN_SCORE) -> list[MemoryItem]:
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
                filters = {"$and": [{"user_id": {"$eq": user_id}},
                                    {"session_id": {"$eq": session_id}}]}
            else:
                filters = {"user_id": {"$eq": user_id}}
            matches = await self._chat_vec.query(vec, top_k=top_k, filters=filters)
            out: list[MemoryItem] = []
            for m in matches:
                score = float(m.get("score", 0.0))
                if score < min_score:
                    continue
                md = m.get("metadata") or {}
                out.append(MemoryItem(
                    id=str(m.get("id", "")), text=str(md.get("text", "")),
                    role=str(md.get("role", "user")),
                    session_id=str(md.get("session_id", "")),
                    score=score, created_at=float(md.get("created_at", 0.0))))
            return out
        except Exception as e:
            logger.warning("memory_recall_failed error=%s", e)
            return []

    async def recent(self, user_id: str, session_id: str, *,
                     limit: int = 10) -> list[MemoryItem]:
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
                out.append(MemoryItem(
                    id=str(d.get("id", "")), text=str(d.get("text", "")),
                    role=str(d.get("role", "user")),
                    session_id=str(d.get("session_id", session_id)),
                    score=float(d.get("score", 1.0)),
                    created_at=float(d.get("created_at", 0.0))))
            except Exception:
                continue
        return out

    async def forget_session(self, user_id: str, session_id: str) -> int:
        removed = 0
        if self._redis is None:
            return 0
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
        if self._owns_http:
            with contextlib.suppress(Exception):
                await self._http.aclose()

    # Backward-compat aliases
    async def add_message(self, user_id: str, session_id: str, role: str,
                          text: str, *,
                          metadata: dict[str, Any] | None = None) -> str | None:
        return await self.remember(user_id=user_id, session_id=session_id,
                                   role=role, text=text, metadata=metadata)

    async def retrieve(self, user_id: str, query: str, *,
                       top_k: int = _DEFAULT_TOP_K, session_id: str | None = None,
                       min_score: float = _DEFAULT_MIN_SCORE) -> list[MemoryItem]:
        return await self.recall(user_id=user_id, query=query, top_k=top_k,
                                 session_id=session_id, min_score=min_score)

    async def retrieve_recent(self, user_id: str, session_id: str, *,
                              limit: int = 10) -> list[MemoryItem]:
        return await self.recent(user_id, session_id, limit=limit)

    # ── internals ─────────────────────────────────────────────────────
    async def _embed(self, text: str) -> list[float] | None:
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
        if not (self._acct and self._key):
            return None
        url = _CF_BASE.format(acct=self._acct) + f"/ai/run/{_EMBED_MODEL}"
        try:
            r = await self._http.post(
                url,
                headers={"Authorization": f"Bearer {self._key}",
                         "Content-Type": "application/json"},
                json={"text": [text]}, timeout=10.0)
            r.raise_for_status()
            vecs = (r.json().get("result") or {}).get("data") or []
            if not vecs or not isinstance(vecs[0], list):
                return None
            return [float(x) for x in vecs[0]]
        except Exception as e:
            logger.warning("embed_remote_failed error=%s", e)
            return None

    async def _recent_push(self, user_id: str, session_id: str,
                           item: MemoryItem) -> None:
        if self._redis is None:
            return
        key = f"{self._recent_prefix}:{user_id}:{session_id}"
        with contextlib.suppress(Exception):
            await self._redis.lpush(key, json.dumps(item.to_dict()))
            await self._redis.ltrim(key, 0, self._recent_limit - 1)
            await self._redis.expire(key, _RECENT_TTL)

    async def _track_id(self, user_id: str, session_id: str, vid: str) -> None:
        if self._redis is None:
            return
        with contextlib.suppress(Exception):
            key = f"{self._ids_prefix}:{user_id}:{session_id}"
            await self._redis.sadd(key, vid)
            await self._redis.expire(key, _RECENT_TTL)


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Repo indexer (was core/repo_indexer.py)
# ═══════════════════════════════════════════════════════════════════════════
_GITHUB_API = "https://api.github.com"
_REPO_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

_INDEXABLE_EXTS: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust", ".rb": "ruby",
    ".java": "java", ".kt": "kotlin", ".swift": "swift",
    ".cs": "csharp", ".php": "php", ".scala": "scala",
    ".html": "html", ".css": "css", ".scss": "css",
    ".vue": "vue", ".svelte": "svelte",
    ".md": "markdown", ".sh": "shell",
}
_SKIP_PATH_PATTERNS = re.compile(
    r"""(?xi)
    (?:^|/)
    (?:
        node_modules | vendor | dist | build | coverage | \.next | \.nuxt
      | \.venv | venv | env | __pycache__ | \.git | \.idea | \.vscode
      | target | bin | obj | out
    )
    (?:/|$)
    """)
_DEFAULT_BRANCH_FALLBACK = "main"
_MAX_DEFAULT_FILE_BYTES = 200 * 1024
_MAX_DEFAULT_FILES = 500
_MAX_DEFAULT_CHUNKS = 5000
_DEFAULT_EMBED_BATCH = 32
_FINGERPRINT_TTL = 86_400


@dataclass(slots=True)
class IndexStats:
    repo: str
    branch: str
    head_sha: str = ""
    files_seen: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    chunks: int = 0
    duration_ms: float = 0.0
    errors: list[str] = field(default_factory=list)
    truncated: bool = False
    skipped_reason: str | None = None

    def to_dict(self) -> dict:
        return {"repo": self.repo, "branch": self.branch,
                "head_sha": self.head_sha, "files_seen": self.files_seen,
                "files_indexed": self.files_indexed,
                "files_skipped": self.files_skipped, "chunks": self.chunks,
                "duration_ms": round(self.duration_ms, 2),
                "errors": self.errors[:20], "truncated": self.truncated,
                "skipped_reason": self.skipped_reason}


@dataclass(slots=True)
class CodeChunk:
    id: str
    path: str
    language: str
    text: str
    start_line: int
    end_line: int
    score: float = 0.0
    repo: str = ""
    branch: str = ""

    def render(self) -> str:
        lang = self.language or "text"
        return f"`{self.path}` (L{self.start_line}-{self.end_line}):\n```{lang}\n{self.text}\n```"

    def to_dict(self) -> dict:
        return {"id": self.id, "path": self.path, "language": self.language,
                "text": self.text, "start_line": self.start_line,
                "end_line": self.end_line, "score": round(self.score, 4),
                "repo": self.repo, "branch": self.branch}


def _parse_repo_url(url: str) -> tuple[str, str]:
    u = (url or "").strip()
    if not u:
        raise ValueError("empty repo URL")
    u = u.removesuffix(".git").rstrip("/")
    m = re.match(r"^(?:https?://github\.com/)?([^/]+)/([^/]+)$", u)
    if not m:
        raise ValueError(f"unsupported repo URL: {url!r}")
    return m.group(1), m.group(2)


def _chunk_lines(content: str, max_chars: int = 2400,
                 overlap: int = 6) -> list[tuple[int, int, str]]:
    lines = content.splitlines(keepends=True)
    if not lines:
        return []
    chunks: list[tuple[int, int, str]] = []
    i = 0
    n = len(lines)
    while i < n:
        start = i
        budget = 0
        while i < n and budget < max_chars:
            budget += len(lines[i])
            i += 1
        end = i
        text = "".join(lines[start:end]).rstrip()
        if text:
            chunks.append((start + 1, end, text))
        if i < n:
            i = max(start + 1, i - overlap)
    return chunks


def _chunk_python(content: str, max_chars: int = 2400) -> list[tuple[int, int, str]]:
    try:
        tree = ast.parse(content)
    except Exception:
        return _chunk_lines(content, max_chars)
    lines = content.splitlines(keepends=True)
    if not lines:
        return []
    boundaries: list[tuple[int, int]] = []
    first_def_line = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = max(0, node.lineno - 1)
            end = getattr(node, "end_lineno", None) or (start + 1)
            boundaries.append((start, end))
            if first_def_line is None:
                first_def_line = start
    chunks: list[tuple[int, int, str]] = []
    if first_def_line is None:
        return _chunk_lines(content, max_chars)
    header = "".join(lines[:first_def_line]).rstrip()
    if header.strip():
        chunks.append((1, first_def_line, header))
    for start, end in boundaries:
        text = "".join(lines[start:end]).rstrip()
        if not text:
            continue
        if len(text) <= max_chars:
            chunks.append((start + 1, end, text))
        else:
            for s, e, t in _chunk_lines(text, max_chars):
                chunks.append((start + s, start + e - 1, t))
    return chunks


_JS_BOUNDARY_RE = re.compile(
    r"""(?m)
    ^(?:export\s+)?(?:default\s+)?(?:async\s+)?
    (?:function|class|const|let|var)\b
    """, re.VERBOSE)


def _chunk_js(content: str, max_chars: int = 2400) -> list[tuple[int, int, str]]:
    lines = content.splitlines(keepends=True)
    if not lines:
        return []
    boundaries: list[int] = [0]
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped:
            continue
        if line[0] not in (" ", "\t") and _JS_BOUNDARY_RE.match(line):
            boundaries.append(i)
    boundaries = sorted(set(boundaries))
    chunks: list[tuple[int, int, str]] = []
    for i, start in enumerate(boundaries):
        end = boundaries[i + 1] if i + 1 < len(boundaries) else len(lines)
        text = "".join(lines[start:end]).rstrip()
        if not text:
            continue
        if len(text) <= max_chars:
            chunks.append((start + 1, end, text))
        else:
            for s, e, t in _chunk_lines(text, max_chars):
                chunks.append((start + s, start + e - 1, t))
    return chunks


async def _chunk_file(path: str, content: str,
                      max_chars: int = 2400) -> list[tuple[int, int, str]]:
    ext = Path(path).suffix.lower()

    def _sync():
        if ext in (".py", ".pyi"):
            return _chunk_python(content, max_chars)
        if ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte"):
            return _chunk_js(content, max_chars)
        return _chunk_lines(content, max_chars)

    return await asyncio.to_thread(_sync)


class RepoIndexer:
    """GitHub repo → Cloudflare Vectorize. Never raises on index_repo()."""
    __slots__ = ("_embed_batch", "_fp_prefix", "_gh_token", "_http",
                 "_max_chunks", "_max_file_bytes", "_max_files",
                 "_owns_http", "_redis", "_vec")

    def __init__(self, *, account_id: str | None = None,
                 api_key: str | None = None, index_name: str | None = None,
                 github_token: str | None = None, redis_client: Any | None = None,
                 http_client: httpx.AsyncClient | None = None,
                 max_file_bytes: int = _MAX_DEFAULT_FILE_BYTES,
                 max_files: int = _MAX_DEFAULT_FILES,
                 max_chunks: int = _MAX_DEFAULT_CHUNKS,
                 embed_batch: int = _DEFAULT_EMBED_BATCH) -> None:
        acct = (account_id or os.getenv("CLOUDFLARE_ACCOUNT_ID") or "").strip()
        key = (api_key or os.getenv("CLOUDFLARE_API_KEY") or "").strip()
        idx = (index_name or os.getenv("REPO_INDEX") or "axelr-repo-index").strip()
        tok = (github_token or os.getenv("GITHUB_INDEX_TOKEN")
               or os.getenv("GITHUB_TOKEN") or "").strip()
        if http_client is None:
            self._http = httpx.AsyncClient(
                timeout=_REPO_HTTP_TIMEOUT,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                headers={"User-Agent": "axelr-repo-indexer/1.0"})
            self._owns_http = True
        else:
            self._http = http_client
            self._owns_http = False
        self._vec = _VectorizeClient(acct, key, idx, self._http)
        self._redis = redis_client
        self._gh_token = tok
        self._max_file_bytes = max(1024, int(max_file_bytes))
        self._max_files = max(1, int(max_files))
        self._max_chunks = max(1, int(max_chunks))
        self._embed_batch = max(1, min(128, int(embed_batch)))
        self._fp_prefix = "axelr:repo:fp"

    async def index_repo(self, repo_url: str, branch: str | None = None, *,
                         force: bool = False) -> IndexStats:
        t0 = time.time()
        try:
            owner, repo = _parse_repo_url(repo_url)
        except ValueError as e:
            return IndexStats(repo=repo_url,
                              branch=branch or _DEFAULT_BRANCH_FALLBACK,
                              errors=[str(e)])
        stats = IndexStats(repo=f"{owner}/{repo}", branch="")
        try:
            head_sha, resolved_branch = await self._resolve_head(owner, repo, branch)
            stats.branch = resolved_branch
            stats.head_sha = head_sha
            if not force and await self._is_fresh(owner, repo, resolved_branch, head_sha):
                stats.skipped_reason = "fingerprint_match"
                stats.duration_ms = (time.time() - t0) * 1000
                return stats
            if not self._vec.enabled:
                stats.errors.append("vectorize_not_configured")
                stats.duration_ms = (time.time() - t0) * 1000
                return stats
            blobs = await self._list_blobs(owner, repo, head_sha, stats)
            await self._index_blobs(owner, repo, resolved_branch, blobs, stats)
            await self._store_fingerprint(owner, repo, resolved_branch, head_sha)
        except Exception as e:
            logger.exception("index_repo_failed error=%s", e)
            stats.errors.append(f"{type(e).__name__}: {e}")
        stats.duration_ms = (time.time() - t0) * 1000
        return stats

    async def query(self, repo_url: str, query: str, *, top_k: int = 10,
                    branch: str | None = None,
                    min_score: float = 0.0) -> list[CodeChunk]:
        try:
            owner, repo = _parse_repo_url(repo_url)
        except ValueError:
            return []
        if not query or not self._vec.enabled:
            return []
        vec = await self._embed_one(query)
        if not vec:
            return []
        filters: dict[str, Any] = {"repo": {"$eq": f"{owner}/{repo}"}}
        if branch:
            filters = {"$and": [{"repo": {"$eq": f"{owner}/{repo}"}},
                                {"branch": {"$eq": branch}}]}
        matches = await self._vec.query(vec, top_k=top_k, filters=filters)
        out: list[CodeChunk] = []
        for m in matches:
            score = float(m.get("score", 0.0))
            if score < min_score:
                continue
            md = m.get("metadata") or {}
            out.append(CodeChunk(
                id=str(m.get("id", "")), path=str(md.get("path", "")),
                language=str(md.get("language", "")), text=str(md.get("text", "")),
                start_line=int(md.get("start_line", 0) or 0),
                end_line=int(md.get("end_line", 0) or 0),
                score=score, repo=str(md.get("repo", "")),
                branch=str(md.get("branch", ""))))
        return out

    async def invalidate(self, repo_url: str, branch: str | None = None) -> bool:
        try:
            owner, repo = _parse_repo_url(repo_url)
        except ValueError:
            return False
        if self._redis is None:
            return False
        key = f"{self._fp_prefix}:{owner}/{repo}:{branch or '*'}"
        try:
            await self._redis.delete(key)
            return True
        except Exception:
            return False

    async def is_stale(self, repo_url: str, branch: str | None = None, *,
                       max_age: int = 86_400) -> bool:
        try:
            owner, repo = _parse_repo_url(repo_url)
        except ValueError:
            return True
        if self._redis is None:
            return True
        key = f"{self._fp_prefix}:{owner}/{repo}:{branch or '*'}"
        try:
            raw = await self._redis.get(key)
            if not raw:
                return True
            data = json.loads(raw)
            return (time.time() - float(data.get("ts", 0))) > max_age
        except Exception:
            return True

    async def close(self) -> None:
        if self._owns_http:
            with contextlib.suppress(Exception):
                await self._http.aclose()

    # ── GitHub ─────────────────────────────────────────────────────────
    def _gh_headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28"}
        if self._gh_token:
            h["Authorization"] = f"Bearer {self._gh_token}"
        return h

    async def _gh_get_json(self, url: str,
                           *, params: dict[str, Any] | None = None) -> Any | None:
        for attempt in range(2):
            try:
                r = await self._http.get(url, headers=self._gh_headers(),
                                         params=params, timeout=20.0)
                remaining = r.headers.get("x-ratelimit-remaining")
                reset = r.headers.get("x-ratelimit-reset")
                if r.status_code in (403, 429) and remaining is not None:
                    try:
                        if int(remaining) <= 1 and reset:
                            wait = max(1, min(60, int(reset) - int(time.time())))
                            logger.warning("github_rate_limited sleeping_s=%s", wait)
                            await asyncio.sleep(wait); continue
                    except Exception:
                        pass
                r.raise_for_status()
                return r.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500 and attempt == 0:
                    await asyncio.sleep(1.5); continue
                logger.warning("github_get_failed url=%s status=%s",
                               url, e.response.status_code)
                return None
            except Exception as e:
                if attempt == 0:
                    await asyncio.sleep(1.0); continue
                logger.warning("github_get_error url=%s error=%s", url, e)
                return None
        return None

    async def _resolve_head(self, owner: str, repo: str,
                            branch: str | None) -> tuple[str, str]:
        meta = await self._gh_get_json(f"{_GITHUB_API}/repos/{owner}/{repo}")
        default_branch = (meta or {}).get("default_branch") or _DEFAULT_BRANCH_FALLBACK
        resolved = branch or default_branch
        ref = await self._gh_get_json(
            f"{_GITHUB_API}/repos/{owner}/{repo}/git/ref/heads/{resolved}")
        sha = ""
        if ref and isinstance(ref, dict):
            sha = str(((ref.get("object") or {}).get("sha")) or "")
        if not sha:
            commits = await self._gh_get_json(
                f"{_GITHUB_API}/repos/{owner}/{repo}/commits/{resolved}",
                params={"per_page": 1})
            if commits and isinstance(commits, list) and commits:
                sha = str(commits[0].get("sha") or "")
        return sha, resolved

    async def _list_blobs(self, owner: str, repo: str, head_sha: str,
                          stats: IndexStats) -> list[dict[str, Any]]:
        if not head_sha:
            return []
        url = f"{_GITHUB_API}/repos/{owner}/{repo}/git/trees/{head_sha}"
        data = await self._gh_get_json(url, params={"recursive": 1})
        if not data or not isinstance(data, dict):
            stats.errors.append("tree_fetch_failed")
            return []
        stats.truncated = bool(data.get("truncated"))
        entries = list(data.get("tree") or [])
        if stats.truncated:
            stats.errors.append("tree_truncated")
        return self._filter_entries(entries)

    def _filter_entries(self, entries: Sequence[dict[str, Any]]
                        ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for e in entries:
            try:
                if e.get("type") != "blob":
                    continue
                path = str(e.get("path") or "")
                sha = str(e.get("sha") or "")
                size = int(e.get("size") or 0)
                if not path or not sha:
                    continue
                if _SKIP_PATH_PATTERNS.search(path):
                    continue
                if Path(path).suffix.lower() not in _INDEXABLE_EXTS:
                    continue
                if size > self._max_file_bytes:
                    continue
                out.append({"path": path, "sha": sha, "size": size})
                if len(out) >= self._max_files * 2:
                    break
            except Exception:
                continue
        return out

    async def _index_blobs(self, owner: str, repo: str, branch: str,
                           blobs: list[dict[str, Any]], stats: IndexStats) -> None:
        stats.files_seen = len(blobs)
        if not blobs:
            return
        repo_key = f"{owner}/{repo}"
        pending_texts: list[str] = []
        pending_meta: list[dict[str, Any]] = []
        chunk_count = 0
        for entry in blobs:
            if stats.files_indexed >= self._max_files or chunk_count >= self._max_chunks:
                if chunk_count >= self._max_chunks:
                    stats.errors.append("chunk_budget_exhausted")
                break
            try:
                content = await self._fetch_blob(owner, repo, entry["sha"])
            except Exception:
                stats.files_skipped += 1; continue
            if content is None:
                stats.files_skipped += 1; continue
            path = entry["path"]
            language = _INDEXABLE_EXTS.get(Path(path).suffix.lower(), "")
            pieces = await _chunk_file(path, content)
            if not pieces:
                stats.files_skipped += 1; continue
            for start, end, text in pieces:
                if chunk_count >= self._max_chunks:
                    break
                cid = _sha(f"{repo_key}|{branch}|{path}|{start}|{end}")[:32]
                pending_texts.append(text)
                pending_meta.append({"id": cid, "path": path, "language": language,
                                     "text": text, "start_line": int(start),
                                     "end_line": int(end), "repo": repo_key,
                                     "branch": branch})
                chunk_count += 1
                if len(pending_texts) >= self._embed_batch:
                    await self._flush(pending_texts, pending_meta, stats)
                    pending_texts.clear(); pending_meta.clear()
            stats.files_indexed += 1
        if pending_texts:
            await self._flush(pending_texts, pending_meta, stats)
        stats.chunks = chunk_count

    async def _flush(self, texts: list[str], metas: list[dict[str, Any]],
                     stats: IndexStats) -> None:
        vectors = await self._embed_many(texts)
        if not vectors or len(vectors) != len(metas):
            stats.errors.append("embed_batch_failed"); return
        payload = [{"id": meta["id"], "values": vec,
                    "metadata": {k: v for k, v in meta.items() if k != "id"}}
                   for vec, meta in zip(vectors, metas)]
        if not await self._vec.upsert(payload):
            stats.errors.append("vectorize_upsert_failed")

    async def _fetch_blob(self, owner: str, repo: str, sha: str) -> str | None:
        url = f"{_GITHUB_API}/repos/{owner}/{repo}/git/blobs/{sha}"
        data = await self._gh_get_json(url)
        if not data or not isinstance(data, dict):
            return None
        encoding = data.get("encoding")
        content = data.get("content")
        if not content:
            return None
        if encoding == "base64":
            try:
                return base64.b64decode(content).decode("utf-8", errors="ignore")
            except Exception:
                return None
        return str(content)

    async def _is_fresh(self, owner: str, repo: str, branch: str,
                        head_sha: str) -> bool:
        if self._redis is None or not head_sha:
            return False
        try:
            raw = await self._redis.get(f"{self._fp_prefix}:{owner}/{repo}:{branch}")
            if not raw:
                return False
            return str(json.loads(raw).get("sha")) == head_sha
        except Exception:
            return False

    async def _store_fingerprint(self, owner: str, repo: str, branch: str,
                                 head_sha: str) -> None:
        if self._redis is None or not head_sha:
            return
        with contextlib.suppress(Exception):
            key = f"{self._fp_prefix}:{owner}/{repo}:{branch}"
            await self._redis.setex(key, _FINGERPRINT_TTL,
                                    json.dumps({"sha": head_sha, "ts": time.time()}))

    async def _embed_one(self, text: str) -> list[float] | None:
        vecs = await self._embed_many([text])
        return vecs[0] if vecs else None

    async def _embed_many(self, texts: list[str]) -> list[list[float]] | None:
        if not texts:
            return []
        if not (self._vec._acct and self._vec._key):
            return None
        url = _CF_BASE.format(acct=self._vec._acct) + f"/ai/run/{_EMBED_MODEL}"
        try:
            r = await self._http.post(
                url,
                headers={"Authorization": f"Bearer {self._vec._key}",
                         "Content-Type": "application/json"},
                json={"text": texts}, timeout=30.0)
            r.raise_for_status()
            data = r.json()
            vecs = (data.get("result") or {}).get("data") or []
            if not isinstance(vecs, list) or len(vecs) != len(texts):
                return None
            return [[float(x) for x in v] for v in vecs]
        except Exception as e:
            logger.warning("embed_many_failed error=%s", e)
            return None


# ═══════════════════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════════════════
_MEMORY: Memory | None = None
_MEMORY_LOCK = None


async def get_memory() -> Memory:
    global _MEMORY, _MEMORY_LOCK
    if _MEMORY is not None:
        return _MEMORY
    import asyncio as _a
    if _MEMORY_LOCK is None:
        _MEMORY_LOCK = _a.Lock()
    async with _MEMORY_LOCK:
        if _MEMORY is None:
            from .infra import get_redis_client   # ← was .state_compat
            _MEMORY = Memory(redis_client=get_redis_client())
        return _MEMORY


# Backward-compat aliases
ConversationMemory = Memory
MemoryBus = Memory
MemoryHit = MemoryItem
ConversationMemoryItem = MemoryItem


__all__ = [
    "MemoryItem", "Memory", "get_memory",
    "RepoIndexer", "IndexStats", "CodeChunk",
    "ConversationMemory", "MemoryBus",
    "MemoryHit", "ConversationMemoryItem",
]