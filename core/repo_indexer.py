# core/repo_indexer.py
"""
AXELR Repository Indexer
========================
Turn a GitHub repository into a semantically searchable Vectorize index.

Flow
----
1. Resolve the default branch (one API call).
2. Fetch the recursive tree (paginated / truncated-aware).
3. Filter to source files under a size cap.
4. Fetch each blob and chunk it (Python AST / JS regex / line window).
5. Embed chunks in batches and upsert into Cloudflare Vectorize.

Caching
-------
* An index ``fingerprint`` (repo, branch, head SHA) is cached in Redis.
* ``index_repo`` is idempotent: if the fingerprint matches and ``force``
  is not set, indexing is skipped.
* ``is_stale`` lets a scheduler decide when to re-index.

RAM footprint: < 25 MB (streaming, per-file processing, no global cache).
"""

from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("axelr.repo_indexer")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CF_ACCT = "https://api.cloudflare.com/client/v4/accounts/{acct}"
_EMBED_URL = _CF_ACCT + "/ai/run/{model}"
_VECTOR_URL = _CF_ACCT + "/vectorize/v2/indexes/{index}{suffix}"
_EMBED_MODEL = "@cf/baai/bge-small-en-v1.5"

_GITHUB_API = "https://api.github.com"
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

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
    """
)

_DEFAULT_BRANCH_FALLBACK = "main"
_MAX_DEFAULT_FILE_BYTES = 200 * 1024
_MAX_DEFAULT_FILES = 500
_MAX_DEFAULT_CHUNKS = 5000
_DEFAULT_EMBED_BATCH = 32
_FINGERPRINT_TTL = 86_400          # 1 day


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class IndexStats:
    """Outcome of a full repo indexing run."""

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
        """Return a JSON-serialisable representation."""
        return {
            "repo": self.repo,
            "branch": self.branch,
            "head_sha": self.head_sha,
            "files_seen": self.files_seen,
            "files_indexed": self.files_indexed,
            "files_skipped": self.files_skipped,
            "chunks": self.chunks,
            "duration_ms": round(self.duration_ms, 2),
            "errors": self.errors[:20],
            "truncated": self.truncated,
            "skipped_reason": self.skipped_reason,
        }


@dataclass(slots=True)
class CodeChunk:
    """A single indexed code chunk."""

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
        """Format as a fenced code block for prompt injection."""
        lang = self.language or "text"
        return f"`{self.path}` (L{self.start_line}-{self.end_line}):\n```{lang}\n{self.text}\n```"

    def to_dict(self) -> dict:
        """Return a JSON-serialisable representation."""
        return {
            "id": self.id,
            "path": self.path,
            "language": self.language,
            "text": self.text,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "score": round(self.score, 4),
            "repo": self.repo,
            "branch": self.branch,
        }


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _parse_repo_url(url: str) -> tuple[str, str]:
    """Return ``(owner, repo)`` from a GitHub URL; raises ``ValueError``."""
    u = (url or "").strip()
    if not u:
        raise ValueError("empty repo URL")
    u = u.removesuffix(".git")
    u = u.rstrip("/")
    m = re.match(r"^(?:https?://github\.com/)?([^/]+)/([^/]+)$", u)
    if not m:
        raise ValueError(f"unsupported repo URL: {url!r}")
    return m.group(1), m.group(2)


# ---------------------------------------------------------------------------
# Inline Vectorize client (self-contained)
# ---------------------------------------------------------------------------

class _VectorizeClient:
    """Minimal Cloudflare Vectorize REST wrapper."""

    __slots__ = ("_acct", "_http", "_index", "_key")

    def __init__(self, acct: str, key: str, index: str, http: httpx.AsyncClient) -> None:
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
        return {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}

    async def upsert(self, vectors: list[dict], timeout: float = 60.0) -> bool:
        if not self.enabled or not vectors:
            return False
        try:
            r = await self._http.post(
                self._url("/upsert"), headers=self._headers(),
                json={"vectors": vectors}, timeout=timeout,
            )
            r.raise_for_status()
            return True
        except Exception as e:
            logger.warning("vectorize_upsert_failed error=%s", e)
            return False

    async def query(
        self, vector: Sequence[float], *, top_k: int = 10,
        filters: dict[str, Any] | None = None, timeout: float = 20.0,
    ) -> list[dict]:
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
                self._url("/query"), headers=self._headers(), json=body, timeout=timeout,
            )
            r.raise_for_status()
            return list(((r.json().get("result") or {}).get("matches")) or [])
        except Exception as e:
            logger.warning("vectorize_query_failed error=%s", e)
            return []

    async def delete_by_ids(self, ids: list[str], timeout: float = 30.0) -> bool:
        if not self.enabled or not ids:
            return False
        try:
            r = await self._http.post(
                self._url("/delete_by_ids"), headers=self._headers(),
                json={"ids": ids}, timeout=timeout,
            )
            r.raise_for_status()
            return True
        except Exception as e:
            logger.warning("vectorize_delete_failed error=%s", e)
            return False


# ---------------------------------------------------------------------------
# Chunkers
# ---------------------------------------------------------------------------

def _chunk_lines(
    content: str, max_chars: int = 2400, overlap: int = 6
) -> list[tuple[int, int, str]]:
    """Fallback line-window chunker with a small overlap."""
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
    """AST-aware chunker: split at top-level defs/classes; keep a header chunk."""
    try:
        tree = ast.parse(content)
    except Exception:
        return _chunk_lines(content, max_chars)

    lines = content.splitlines(keepends=True)
    if not lines:
        return []

    boundaries: list[tuple[int, int]] = []  # (start_idx, end_idx) inclusive
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

    # Header (imports + module docstring)
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
            # Sub-chunk oversized defs by line window
            for s, e, t in _chunk_lines(text, max_chars):
                chunks.append((start + s, start + e - 1, t))
    return chunks


_JS_BOUNDARY_RE = re.compile(
    r"""(?m)
    ^(?:export\s+)?(?:default\s+)?(?:async\s+)?
    (?:function|class|const|let|var)\b
    """,
    re.VERBOSE,
)


def _chunk_js(content: str, max_chars: int = 2400) -> list[tuple[int, int, str]]:
    """Regex-based JS/TS chunker; falls back to line windows."""
    lines = content.splitlines(keepends=True)
    if not lines:
        return []

    boundaries: list[int] = [0]
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped:
            continue
        # Heuristic: top-level declaration (no leading whitespace)
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


def _chunk_file(path: str, content: str, max_chars: int = 2400) -> list[tuple[int, int, str]]:
    """Dispatch to the appropriate chunker for the file extension."""
    ext = Path(path).suffix.lower()
    if ext in (".py", ".pyi"):
        return _chunk_python(content, max_chars)
    if ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte"):
        return _chunk_js(content, max_chars)
    return _chunk_lines(content, max_chars)


# ---------------------------------------------------------------------------
# RepoIndexer
# ---------------------------------------------------------------------------

class RepoIndexer:
    """
    Index a GitHub repository into Cloudflare Vectorize.

    Parameters
    ----------
    account_id, api_key : str | None
        Cloudflare credentials (Vectorize + Workers AI embeddings).
    index_name : str | None
        Vectorize index; defaults to ``REPO_INDEX`` env or
        ``"axelr-repo-index"``.
    github_token : str | None
        GitHub PAT for private repos and higher rate limits. Falls back to
        ``GITHUB_INDEX_TOKEN`` / ``GITHUB_TOKEN``.
    redis_client : Any | None
        Async Redis client for the fingerprint cache.
    http_client : httpx.AsyncClient | None
        Shared HTTP client.
    max_file_bytes : int
        Skip blobs larger than this. Default 200 KB.
    max_files : int
        Hard cap on the number of source files indexed per run.
    max_chunks : int
        Hard cap on total chunks per run.
    embed_batch : int
        Number of texts per Workers AI embedding request.
    """

    __slots__ = (
        "_embed_batch",
        "_fp_prefix",
        "_gh_token",
        "_http",
        "_max_chunks",
        "_max_file_bytes",
        "_max_files",
        "_owns_http",
        "_redis",
        "_vec",
    )

    def __init__(
        self,
        *,
        account_id: str | None = None,
        api_key: str | None = None,
        index_name: str | None = None,
        github_token: str | None = None,
        redis_client: Any | None = None,
        http_client: httpx.AsyncClient | None = None,
        max_file_bytes: int = _MAX_DEFAULT_FILE_BYTES,
        max_files: int = _MAX_DEFAULT_FILES,
        max_chunks: int = _MAX_DEFAULT_CHUNKS,
        embed_batch: int = _DEFAULT_EMBED_BATCH,
    ) -> None:
        acct = (account_id or os.getenv("CLOUDFLARE_ACCOUNT_ID") or "").strip()
        key = (api_key or os.getenv("CLOUDFLARE_API_KEY") or "").strip()
        idx = (index_name or os.getenv("REPO_INDEX") or "axelr-repo-index").strip()
        tok = (
            github_token
            or os.getenv("GITHUB_INDEX_TOKEN")
            or os.getenv("GITHUB_TOKEN")
            or ""
        ).strip()

        if http_client is None:
            self._http = httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                headers={"User-Agent": "axelr-repo-indexer/1.0"},
            )
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

    # -- public ------------------------------------------------------------

    async def index_repo(
        self,
        repo_url: str,
        branch: str | None = None,
        *,
        force: bool = False,
    ) -> IndexStats:
        """
        Index (or re-index) a repository.

        If the cached head SHA matches and ``force=False``, indexing is
        skipped and ``skipped_reason`` is populated. Never raises — a
        failure is reported in ``IndexStats.errors``.
        """
        t0 = time.time()
        try:
            owner, repo = _parse_repo_url(repo_url)
        except ValueError as e:
            return IndexStats(
                repo=repo_url, branch=branch or _DEFAULT_BRANCH_FALLBACK,
                errors=[str(e)],
            )

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

    async def query(
        self,
        repo_url: str,
        query: str,
        *,
        top_k: int = 10,
        branch: str | None = None,
        min_score: float = 0.0,
    ) -> list[CodeChunk]:
        """
        Semantic search inside an indexed repository. Never raises.
        """
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
            filters = {
                "$and": [
                    {"repo": {"$eq": f"{owner}/{repo}"}},
                    {"branch": {"$eq": branch}},
                ]
            }

        matches = await self._vec.query(vec, top_k=top_k, filters=filters)
        out: list[CodeChunk] = []
        for m in matches:
            score = float(m.get("score", 0.0))
            if score < min_score:
                continue
            md = m.get("metadata") or {}
            out.append(
                CodeChunk(
                    id=str(m.get("id", "")),
                    path=str(md.get("path", "")),
                    language=str(md.get("language", "")),
                    text=str(md.get("text", "")),
                    start_line=int(md.get("start_line", 0) or 0),
                    end_line=int(md.get("end_line", 0) or 0),
                    score=score,
                    repo=str(md.get("repo", "")),
                    branch=str(md.get("branch", "")),
                )
            )
        return out

    async def invalidate(self, repo_url: str, branch: str | None = None) -> bool:
        """Delete the cached fingerprint (forces a fresh index next run)."""
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

    async def is_stale(
        self, repo_url: str, branch: str | None = None, *, max_age: int = 86_400
    ) -> bool:
        """Return True if the repo has no fresh fingerprint."""
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
        """Release the internal HTTP client, if owned."""
        if self._owns_http:
            try:
                await self._http.aclose()
            except Exception:
                pass

    # -- internal: GitHub -------------------------------------------------

    def _gh_headers(self) -> dict[str, str]:
        h = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._gh_token:
            h["Authorization"] = f"Bearer {self._gh_token}"
        return h

    async def _gh_get_json(
        self, url: str, *, params: dict[str, Any] | None = None
    ) -> Any | None:
        """GET with rate-limit awareness + one retry on 5xx."""
        for attempt in range(2):
            try:
                r = await self._http.get(
                    url, headers=self._gh_headers(), params=params, timeout=20.0
                )
                # Rate-limit handling
                remaining = r.headers.get("x-ratelimit-remaining")
                reset = r.headers.get("x-ratelimit-reset")
                if r.status_code in (403, 429) and remaining is not None:
                    try:
                        if int(remaining) <= 1 and reset:
                            wait = max(1, min(60, int(reset) - int(time.time())))
                            logger.warning("github_rate_limited sleeping_s=%s", wait)
                            await asyncio.sleep(wait)
                            continue
                    except Exception:
                        pass
                r.raise_for_status()
                return r.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500 and attempt == 0:
                    await asyncio.sleep(1.5)
                    continue
                logger.warning("github_get_failed url=%s status=%s", url, e.response.status_code)
                return None
            except Exception as e:
                if attempt == 0:
                    await asyncio.sleep(1.0)
                    continue
                logger.warning("github_get_error url=%s error=%s", url, e)
                return None
        return None

    async def _resolve_head(
        self, owner: str, repo: str, branch: str | None
    ) -> tuple[str, str]:
        """Return ``(head_sha, branch_name)`` for the repo."""
        meta = await self._gh_get_json(f"{_GITHUB_API}/repos/{owner}/{repo}")
        default_branch = (
            (meta or {}).get("default_branch") or _DEFAULT_BRANCH_FALLBACK
        )
        resolved = branch or default_branch
        ref = await self._gh_get_json(
            f"{_GITHUB_API}/repos/{owner}/{repo}/git/ref/heads/{resolved}"
        )
        sha = ""
        if ref and isinstance(ref, dict):
            sha = str(((ref.get("object") or {}).get("sha")) or "")
        if not sha:
            commits = await self._gh_get_json(
                f"{_GITHUB_API}/repos/{owner}/{repo}/commits/{resolved}",
                params={"per_page": 1},
            )
            if commits and isinstance(commits, list) and commits:
                sha = str(commits[0].get("sha") or "")
        return sha, resolved

    async def _list_blobs(
        self, owner: str, repo: str, head_sha: str, stats: IndexStats
    ) -> list[dict[str, Any]]:
        """Return filtered blob entries (``path``, ``sha``, ``size``)."""
        if not head_sha:
            return []
        url = f"{_GITHUB_API}/repos/{owner}/{repo}/git/trees/{head_sha}"
        data = await self._gh_get_json(url, params={"recursive": 1})
        if not data or not isinstance(data, dict):
            stats.errors.append("tree_fetch_failed")
            return []

        stats.truncated = bool(data.get("truncated"))
        entries = list(data.get("tree") or [])
        # If truncated, we still use what we have, but warn.
        if stats.truncated:
            stats.errors.append("tree_truncated")

        return self._filter_entries(entries)

    def _filter_entries(self, entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """Filter tree entries to indexable source blobs."""
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
                ext = Path(path).suffix.lower()
                if ext not in _INDEXABLE_EXTS:
                    continue
                if size > self._max_file_bytes:
                    continue
                out.append({"path": path, "sha": sha, "size": size})
                if len(out) >= self._max_files * 2:  # headroom for skip-failures
                    break
            except Exception:
                continue
        return out

    # -- internal: indexing ------------------------------------------------

    async def _index_blobs(
        self,
        owner: str,
        repo: str,
        branch: str,
        blobs: list[dict[str, Any]],
        stats: IndexStats,
    ) -> None:
        """Fetch, chunk, embed and upsert blobs in bounded batches."""
        stats.files_seen = len(blobs)
        if not blobs:
            return

        repo_key = f"{owner}/{repo}"
        pending_texts: list[str] = []
        pending_meta: list[dict[str, Any]] = []
        chunk_count = 0

        for entry in blobs:
            if stats.files_indexed >= self._max_files:
                break
            if chunk_count >= self._max_chunks:
                stats.errors.append("chunk_budget_exhausted")
                break

            try:
                content = await self._fetch_blob(owner, repo, entry["sha"])
            except Exception:
                stats.files_skipped += 1
                continue
            if content is None:
                stats.files_skipped += 1
                continue

            path = entry["path"]
            ext = Path(path).suffix.lower()
            language = _INDEXABLE_EXTS.get(ext, "")

            pieces = _chunk_file(path, content)
            if not pieces:
                stats.files_skipped += 1
                continue

            for start, end, text in pieces:
                if chunk_count >= self._max_chunks:
                    break
                cid = _sha(f"{repo_key}|{branch}|{path}|{start}|{end}")[:32]
                pending_texts.append(text)
                pending_meta.append(
                    {
                        "id": cid,
                        "path": path,
                        "language": language,
                        "text": text,
                        "start_line": int(start),
                        "end_line": int(end),
                        "repo": repo_key,
                        "branch": branch,
                    }
                )
                chunk_count += 1

                if len(pending_texts) >= self._embed_batch:
                    await self._flush(pending_texts, pending_meta, stats)
                    pending_texts.clear()
                    pending_meta.clear()

            stats.files_indexed += 1

        if pending_texts:
            await self._flush(pending_texts, pending_meta, stats)
        stats.chunks = chunk_count

    async def _flush(
        self,
        texts: list[str],
        metas: list[dict[str, Any]],
        stats: IndexStats,
    ) -> None:
        """Embed a batch and upsert."""
        vectors = await self._embed_many(texts)
        if not vectors or len(vectors) != len(metas):
            stats.errors.append("embed_batch_failed")
            return
        payload = []
        for vec, meta in zip(vectors, metas):
            md = {k: v for k, v in meta.items() if k != "id"}
            payload.append({"id": meta["id"], "values": vec, "metadata": md})
        ok = await self._vec.upsert(payload)
        if not ok:
            stats.errors.append("vectorize_upsert_failed")

    async def _fetch_blob(self, owner: str, repo: str, sha: str) -> str | None:
        """Fetch a blob's text content (base64-decoded)."""
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
                raw = base64.b64decode(content)
                return raw.decode("utf-8", errors="ignore")
            except Exception:
                return None
        # Some mirrors return plain utf-8
        return str(content)

    # -- internal: fingerprints -------------------------------------------

    async def _is_fresh(
        self, owner: str, repo: str, branch: str, head_sha: str
    ) -> bool:
        if self._redis is None or not head_sha:
            return False
        try:
            raw = await self._redis.get(f"{self._fp_prefix}:{owner}/{repo}:{branch}")
            if not raw:
                return False
            data = json.loads(raw)
            return str(data.get("sha")) == head_sha
        except Exception:
            return False

    async def _store_fingerprint(
        self, owner: str, repo: str, branch: str, head_sha: str
    ) -> None:
        if self._redis is None or not head_sha:
            return
        try:
            key = f"{self._fp_prefix}:{owner}/{repo}:{branch}"
            await self._redis.setex(
                key, _FINGERPRINT_TTL,
                json.dumps({"sha": head_sha, "ts": time.time()}),
            )
        except Exception:
            pass

    # -- internal: embeddings ---------------------------------------------

    async def _embed_one(self, text: str) -> list[float] | None:
        vecs = await self._embed_many([text])
        return vecs[0] if vecs else None

    async def _embed_many(self, texts: list[str]) -> list[list[float]] | None:
        """Call Cloudflare Workers AI batch embedding."""
        if not texts:
            return []
        if not (self._vec._acct and self._vec._key):
            return None
        url = _EMBED_URL.format(acct=self._vec._acct, model=_EMBED_MODEL)
        try:
            r = await self._http.post(
                url,
                headers={
                    "Authorization": f"Bearer {self._vec._key}",
                    "Content-Type": "application/json",
                },
                json={"text": texts},
                timeout=30.0,
            )
            r.raise_for_status()
            data = r.json()
            vectors = (data.get("result") or {}).get("data") or []
            if not isinstance(vectors, list) or len(vectors) != len(texts):
                return None
            return [[float(x) for x in v] for v in vectors]
        except Exception as e:
            logger.warning("embed_many_failed error=%s", e)
            return None