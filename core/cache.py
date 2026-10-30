<<<<<<< HEAD
# core/cache.py
"""
AXELR — Cache Layer (prefix · semantic · counters)
====================================================
Two caches, one file — because they share the same lifecycle, the same
Redis handle, and the same measurement surface:

    §1 Prefix cache      dedup the system-prompt block across requests
    §2 Semantic cache    Cloudflare Vectorize + Workers AI embeddings
    §3 Counters          Prometheus hit/miss for both

Public:
    canonical_prefix(workspace, intent, tier, extra) -> (id, text)
    remember_prefix / recall_prefix / provider_cache_hint / measure_hit
    CloudflareVectorizeCache · get_vector_cache
    CacheEntry · prefix_hit · prefix_miss
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("axelr.cache")

# ── Shared Cloudflare endpoint templates ─────────────────────────────────
_CF_BASE = "https://api.cloudflare.com/client/v4/accounts/{acct}"
_EMBED_URL = _CF_BASE + "/ai/run/{model}"
_VECTOR_URL = _CF_BASE + "/vectorize/v2/indexes/{index}{suffix}"


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Prometheus counters (prefix + semantic hit/miss)
# ═══════════════════════════════════════════════════════════════════════════
try:
    from prometheus_client import REGISTRY, Counter

    def _metric(name: str, factory):
        try:
            return factory()
        except ValueError:
            return getattr(REGISTRY, "_names_to_collectors", {}).get(name)

    _PREFIX_HITS = _metric("axelr_prefix_cache_hits_total", lambda: Counter(
        "axelr_prefix_cache_hits_total", "Prefix-cache hits",
        ["workspace", "tier"]))
    _PREFIX_MISSES = _metric("axelr_prefix_cache_misses_total", lambda: Counter(
        "axelr_prefix_cache_misses_total", "Prefix-cache misses",
        ["workspace", "tier"]))
    _SEM_HITS = _metric("axelr_cache_hits_total", lambda: Counter(
        "axelr_cache_hits_total", "Semantic cache hits", ["workspace"]))
    _SEM_MISSES = _metric("axelr_cache_misses_total", lambda: Counter(
        "axelr_cache_misses_total", "Semantic cache misses", ["workspace"]))
    _PROM_OK = True
except Exception as e:
    logger.warning("prometheus_unavailable error=%s", e)
    _PREFIX_HITS = _PREFIX_MISSES = _SEM_HITS = _SEM_MISSES = None
    _PROM_OK = False


def prefix_hit(workspace: str = "core", tier: str = "free") -> None:
    if _PREFIX_HITS is None: return
    with contextlib.suppress(Exception):
        _PREFIX_HITS.labels(workspace=workspace or "core", tier=tier or "free").inc()


def prefix_miss(workspace: str = "core", tier: str = "free") -> None:
    if _PREFIX_MISSES is None: return
    with contextlib.suppress(Exception):
        _PREFIX_MISSES.labels(workspace=workspace or "core", tier=tier or "free").inc()


def semantic_hit(workspace: str = "core") -> None:
    if _SEM_HITS is None: return
    with contextlib.suppress(Exception):
        _SEM_HITS.labels(workspace=workspace or "core").inc()


def semantic_miss(workspace: str = "core") -> None:
    if _SEM_MISSES is None: return
    with contextlib.suppress(Exception):
        _SEM_MISSES.labels(workspace=workspace or "core").inc()


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Prefix cache (system-prompt block dedup)
# ═══════════════════════════════════════════════════════════════════════════
_PREFIX_TTL_S = int(os.getenv("PREFIX_CACHE_TTL_S", "3600"))


def _prefix_hash(*parts: str) -> str:
    h = hashlib.blake2b(digest_size=8)
    for p in parts:
        h.update((p or "").encode("utf-8", errors="ignore"))
        h.update(b"|")
    return h.hexdigest()


def canonical_prefix(
    workspace: str, intent: str, tier: str, extra_directives: str = "",
) -> tuple[str, str]:
    pid = _prefix_hash(workspace or "core", intent or "strategy",
                       tier or "free", extra_directives or "")
    return pid, extra_directives


async def remember_prefix(prefix_id: str, text: str) -> None:
    try:
        from .infra import get_redis_client
        r = get_redis_client()
        if r is not None:
            await r.setex(f"prefix:{prefix_id}", _PREFIX_TTL_S, text)
    except Exception as e:
        logger.debug("prefix_remember_failed error=%s", e)


async def recall_prefix(prefix_id: str) -> str | None:
    try:
        from .infra import get_redis_client
        r = get_redis_client()
        if r is None:
            return None
        v = await r.get(f"prefix:{prefix_id}")
        if v is None:
            return None
        return v if isinstance(v, str) else v.decode("utf-8", "ignore")
    except Exception:
        return None


def provider_cache_hint(provider: str, prefix_id: str, prefix_text: str) -> dict[str, Any]:
    p = (provider or "").lower()
    if p == "anthropic":
        return {"system": [{"type": "text", "text": prefix_text,
                            "cache_control": {"type": "ephemeral"}}]}
    if p == "gemini":
        return {"_axelr_prefix_id": prefix_id}
    return {}


async def measure_hit(prefix_id: str) -> None:
    try:
        from .infra import get_redis_client
        r = get_redis_client()
        if r is not None:
            await r.incr(f"prefix:hits:{prefix_id}")
            await r.expire(f"prefix:hits:{prefix_id}", _PREFIX_TTL_S)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Semantic cache (Cloudflare Vectorize + Workers AI)
# ═══════════════════════════════════════════════════════════════════════════
_CF_KEY = (os.getenv("CLOUDFLARE_API_KEY") or "").strip()
_CF_ACCT = (os.getenv("CLOUDFLARE_ACCOUNT_ID") or "").strip()
_CF_INDEX = os.getenv("CLOUDFLARE_VECTORIZE_INDEX", "axelr-semantic-cache").strip()
_CF_MODEL = os.getenv("CLOUDFLARE_EMBED_MODEL", "@cf/baai/bge-base-en-v1.5").strip()
_CF_ENABLED = bool(_CF_KEY and _CF_ACCT)

_LOCAL_MAX = int(os.getenv("SEMANTIC_CACHE_MAX_ENTRIES", "2000"))
_LOCAL_TTL = int(os.getenv("SEMANTIC_CACHE_TTL", "3600"))
_DEFAULT_TOP_K = 1
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
    """Bounded LRU + TTL fallback for exact-match hits."""
    __slots__ = ("_max", "_ttl", "_store", "_lock")

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
    """Semantic cache over Vectorize. Degrades to exact-match when CF is absent."""

    __slots__ = ("_api_key", "_account_id", "_index_name", "_embed_model",
                 "_enabled", "_http", "_owns_http", "_local", "_warmed")

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
        self._warmed = False
        if not self._enabled:
            logger.info("vectorize_disabled_using_local_only")

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0))
        return self._http

    async def _ensure_model(self) -> None:
        if self._warmed: return
        self._warmed = True
        if not self._enabled: return
        with contextlib.suppress(Exception):
            await self._embed("ok")

    async def _embed(self, text: str) -> list[float]:
        from .infra import cf_embed_batch
        vecs = await cf_embed_batch([text], model=self._embed_model)
        if not vecs:
            raise RuntimeError("cf_embed_failed")
        return vecs[0]
        url = _EMBED_URL.format(acct=self._account_id, model=self._embed_model)
        r = await self._client().post(
            url,
            headers={"Authorization": f"Bearer {self._api_key}",
                     "Content-Type": "application/json"},
            json={"text": [text]},
        )
        r.raise_for_status()
        vecs = (r.json().get("result") or {}).get("data") or []
        if not vecs or not isinstance(vecs[0], list):
            raise RuntimeError("unexpected_embed_response")
        return [float(x) for x in vecs[0]]

    async def get(self, prompt: str) -> str | None:
        if not prompt: return None
        with contextlib.suppress(Exception):
            exact = await self._local.get(prompt)
            if exact is not None:
                return exact
        if not self._enabled: return None
        try:
            vec = await self._embed(prompt)
        except Exception as e:
            logger.debug("vectorize_embed_failed error=%s", e); return None
        try:
            url = _VECTOR_URL.format(acct=self._account_id, index=self._index_name,
                                     suffix="/query")
            r = await self._client().post(
                url,
                headers={"Authorization": f"Bearer {self._api_key}",
                         "Content-Type": "application/json"},
                json={"vector": vec, "topK": _DEFAULT_TOP_K, "returnMetadata": "all"},
            )
            r.raise_for_status()
            matches = (r.json().get("result") or {}).get("matches") or []
        except Exception as e:
            logger.debug("vectorize_query_failed error=%s", e); return None
        if not matches: return None
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
        if not prompt or not response: return
        with contextlib.suppress(Exception):
            await self._local.set(prompt, response)
        if not self._enabled: return
        try:
            vec = await self._embed(prompt)
        except Exception:
            return
        vid = hashlib.sha256(prompt.strip().lower().encode()).hexdigest()[:32]
        try:
            url = _VECTOR_URL.format(acct=self._account_id, index=self._index_name,
                                     suffix="/upsert")
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
# Audit
# ═══════════════════════════════════════════════════════════════════════════
def audit() -> dict[str, Any]:
    return {
        "prefix_cache": {"ttl_s": _PREFIX_TTL_S},
        "semantic_cache": {"enabled": _CF_ENABLED, "index": _CF_INDEX,
                           "local_max": _LOCAL_MAX, "local_ttl": _LOCAL_TTL},
        "prometheus": {"enabled": _PROM_OK},
    }


__all__ = [
    # prefix
    "canonical_prefix", "remember_prefix", "recall_prefix",
    "provider_cache_hint", "measure_hit",
    # semantic
    "CacheEntry", "CloudflareVectorizeCache", "get_vector_cache",
    # counters
    "prefix_hit", "prefix_miss", "semantic_hit", "semantic_miss",
    # audit
    "audit",
=======
# core/cache.py
"""
AXELR — Cache Layer (prefix · semantic · counters)
====================================================
Two caches, one file — because they share the same lifecycle, the same
Redis handle, and the same measurement surface:

    §1 Prefix cache      dedup the system-prompt block across requests
    §2 Semantic cache    Cloudflare Vectorize + Workers AI embeddings
    §3 Counters          Prometheus hit/miss for both

Public:
    canonical_prefix(workspace, intent, tier, extra) -> (id, text)
    remember_prefix / recall_prefix / provider_cache_hint / measure_hit
    CloudflareVectorizeCache · get_vector_cache
    CacheEntry · prefix_hit · prefix_miss
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("axelr.cache")

# ── Shared Cloudflare endpoint templates ─────────────────────────────────
_CF_BASE = "https://api.cloudflare.com/client/v4/accounts/{acct}"
_EMBED_URL = _CF_BASE + "/ai/run/{model}"
_VECTOR_URL = _CF_BASE + "/vectorize/v2/indexes/{index}{suffix}"


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Prometheus counters (prefix + semantic hit/miss)
# ═══════════════════════════════════════════════════════════════════════════
try:
    from prometheus_client import REGISTRY, Counter

    def _metric(name: str, factory):
        try:
            return factory()
        except ValueError:
            return getattr(REGISTRY, "_names_to_collectors", {}).get(name)

    _PREFIX_HITS = _metric("axelr_prefix_cache_hits_total", lambda: Counter(
        "axelr_prefix_cache_hits_total", "Prefix-cache hits",
        ["workspace", "tier"]))
    _PREFIX_MISSES = _metric("axelr_prefix_cache_misses_total", lambda: Counter(
        "axelr_prefix_cache_misses_total", "Prefix-cache misses",
        ["workspace", "tier"]))
    _SEM_HITS = _metric("axelr_cache_hits_total", lambda: Counter(
        "axelr_cache_hits_total", "Semantic cache hits", ["workspace"]))
    _SEM_MISSES = _metric("axelr_cache_misses_total", lambda: Counter(
        "axelr_cache_misses_total", "Semantic cache misses", ["workspace"]))
    _PROM_OK = True
except Exception as e:
    logger.warning("prometheus_unavailable error=%s", e)
    _PREFIX_HITS = _PREFIX_MISSES = _SEM_HITS = _SEM_MISSES = None
    _PROM_OK = False


def prefix_hit(workspace: str = "core", tier: str = "free") -> None:
    if _PREFIX_HITS is None: return
    with contextlib.suppress(Exception):
        _PREFIX_HITS.labels(workspace=workspace or "core", tier=tier or "free").inc()


def prefix_miss(workspace: str = "core", tier: str = "free") -> None:
    if _PREFIX_MISSES is None: return
    with contextlib.suppress(Exception):
        _PREFIX_MISSES.labels(workspace=workspace or "core", tier=tier or "free").inc()


def semantic_hit(workspace: str = "core") -> None:
    if _SEM_HITS is None: return
    with contextlib.suppress(Exception):
        _SEM_HITS.labels(workspace=workspace or "core").inc()


def semantic_miss(workspace: str = "core") -> None:
    if _SEM_MISSES is None: return
    with contextlib.suppress(Exception):
        _SEM_MISSES.labels(workspace=workspace or "core").inc()


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Prefix cache (system-prompt block dedup)
# ═══════════════════════════════════════════════════════════════════════════
_PREFIX_TTL_S = int(os.getenv("PREFIX_CACHE_TTL_S", "3600"))


def _prefix_hash(*parts: str) -> str:
    h = hashlib.blake2b(digest_size=8)
    for p in parts:
        h.update((p or "").encode("utf-8", errors="ignore"))
        h.update(b"|")
    return h.hexdigest()


def canonical_prefix(
    workspace: str, intent: str, tier: str, extra_directives: str = "",
) -> tuple[str, str]:
    pid = _prefix_hash(workspace or "core", intent or "strategy",
                       tier or "free", extra_directives or "")
    return pid, extra_directives


async def remember_prefix(prefix_id: str, text: str) -> None:
    try:
        from .infra import get_redis_client
        r = get_redis_client()
        if r is not None:
            await r.setex(f"prefix:{prefix_id}", _PREFIX_TTL_S, text)
    except Exception as e:
        logger.debug("prefix_remember_failed error=%s", e)


async def recall_prefix(prefix_id: str) -> str | None:
    try:
        from .infra import get_redis_client
        r = get_redis_client()
        if r is None:
            return None
        v = await r.get(f"prefix:{prefix_id}")
        if v is None:
            return None
        return v if isinstance(v, str) else v.decode("utf-8", "ignore")
    except Exception:
        return None


def provider_cache_hint(provider: str, prefix_id: str, prefix_text: str) -> dict[str, Any]:
    p = (provider or "").lower()
    if p == "anthropic":
        return {"system": [{"type": "text", "text": prefix_text,
                            "cache_control": {"type": "ephemeral"}}]}
    if p == "gemini":
        return {"_axelr_prefix_id": prefix_id}
    return {}


async def measure_hit(prefix_id: str) -> None:
    try:
        from .infra import get_redis_client
        r = get_redis_client()
        if r is not None:
            await r.incr(f"prefix:hits:{prefix_id}")
            await r.expire(f"prefix:hits:{prefix_id}", _PREFIX_TTL_S)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Semantic cache (Cloudflare Vectorize + Workers AI)
# ═══════════════════════════════════════════════════════════════════════════
_CF_KEY = (os.getenv("CLOUDFLARE_API_KEY") or "").strip()
_CF_ACCT = (os.getenv("CLOUDFLARE_ACCOUNT_ID") or "").strip()
_CF_INDEX = os.getenv("CLOUDFLARE_VECTORIZE_INDEX", "axelr-semantic-cache").strip()
_CF_MODEL = os.getenv("CLOUDFLARE_EMBED_MODEL", "@cf/baai/bge-base-en-v1.5").strip()
_CF_ENABLED = bool(_CF_KEY and _CF_ACCT)

_LOCAL_MAX = int(os.getenv("SEMANTIC_CACHE_MAX_ENTRIES", "2000"))
_LOCAL_TTL = int(os.getenv("SEMANTIC_CACHE_TTL", "3600"))
_DEFAULT_TOP_K = 1
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
    """Bounded LRU + TTL fallback for exact-match hits."""
    __slots__ = ("_max", "_ttl", "_store", "_lock")

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
    """Semantic cache over Vectorize. Degrades to exact-match when CF is absent."""

    __slots__ = ("_api_key", "_account_id", "_index_name", "_embed_model",
                 "_enabled", "_http", "_owns_http", "_local", "_warmed")

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
        self._warmed = False
        if not self._enabled:
            logger.info("vectorize_disabled_using_local_only")

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0))
        return self._http

    async def _ensure_model(self) -> None:
        if self._warmed: return
        self._warmed = True
        if not self._enabled: return
        with contextlib.suppress(Exception):
            await self._embed("ok")

    async def _embed(self, text: str) -> list[float]:
        from .infra import cf_embed_batch
        vecs = await cf_embed_batch([text], model=self._embed_model)
        if not vecs:
            raise RuntimeError("cf_embed_failed")
        return vecs[0]
        url = _EMBED_URL.format(acct=self._account_id, model=self._embed_model)
        r = await self._client().post(
            url,
            headers={"Authorization": f"Bearer {self._api_key}",
                     "Content-Type": "application/json"},
            json={"text": [text]},
        )
        r.raise_for_status()
        vecs = (r.json().get("result") or {}).get("data") or []
        if not vecs or not isinstance(vecs[0], list):
            raise RuntimeError("unexpected_embed_response")
        return [float(x) for x in vecs[0]]

    async def get(self, prompt: str) -> str | None:
        if not prompt: return None
        with contextlib.suppress(Exception):
            exact = await self._local.get(prompt)
            if exact is not None:
                return exact
        if not self._enabled: return None
        try:
            vec = await self._embed(prompt)
        except Exception as e:
            logger.debug("vectorize_embed_failed error=%s", e); return None
        try:
            url = _VECTOR_URL.format(acct=self._account_id, index=self._index_name,
                                     suffix="/query")
            r = await self._client().post(
                url,
                headers={"Authorization": f"Bearer {self._api_key}",
                         "Content-Type": "application/json"},
                json={"vector": vec, "topK": _DEFAULT_TOP_K, "returnMetadata": "all"},
            )
            r.raise_for_status()
            matches = (r.json().get("result") or {}).get("matches") or []
        except Exception as e:
            logger.debug("vectorize_query_failed error=%s", e); return None
        if not matches: return None
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
        if not prompt or not response: return
        with contextlib.suppress(Exception):
            await self._local.set(prompt, response)
        if not self._enabled: return
        try:
            vec = await self._embed(prompt)
        except Exception:
            return
        vid = hashlib.sha256(prompt.strip().lower().encode()).hexdigest()[:32]
        try:
            url = _VECTOR_URL.format(acct=self._account_id, index=self._index_name,
                                     suffix="/upsert")
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
# Audit
# ═══════════════════════════════════════════════════════════════════════════
def audit() -> dict[str, Any]:
    return {
        "prefix_cache": {"ttl_s": _PREFIX_TTL_S},
        "semantic_cache": {"enabled": _CF_ENABLED, "index": _CF_INDEX,
                           "local_max": _LOCAL_MAX, "local_ttl": _LOCAL_TTL},
        "prometheus": {"enabled": _PROM_OK},
    }


__all__ = [
    # prefix
    "canonical_prefix", "remember_prefix", "recall_prefix",
    "provider_cache_hint", "measure_hit",
    # semantic
    "CacheEntry", "CloudflareVectorizeCache", "get_vector_cache",
    # counters
    "prefix_hit", "prefix_miss", "semantic_hit", "semantic_miss",
    # audit
    "audit",
>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
]