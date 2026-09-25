"""
Unit tests for core/vectorize.py + core/conversation.py + core/repo_indexer.py.
================================================================================
All tests run against degraded mode (no Cloudflare, no Redis) unless
explicitly passed a mocked client.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.vectorize import (
    CacheEntry,
    CloudflareVectorizeCache,
    get_vector_cache,
)
from core.conversation import (
    ConversationMemory,
    MemoryItem,
)
from core.repo_indexer import (
    CodeChunk,
    IndexStats,
    RepoIndexer,
    _chunk_file,
    _chunk_js,
    _chunk_lines,
    _chunk_python,
    _parse_repo_url,
)


# ═══════════════════════════════════════════════════════════════════════════
# Section: CacheEntry / MemoryItem / CodeChunk / IndexStats (dataclasses)
# ═══════════════════════════════════════════════════════════════════════════

def test_cache_entry_to_dict_roundtrip():
    e = CacheEntry(prompt="p", response="r", score=0.9)
    d = e.to_dict()
    assert d["prompt"] == "p"
    assert d["response"] == "r"
    assert d["score"] == 0.9


def test_memory_item_render_and_to_dict():
    m = MemoryItem(
        id="x", text="hello", role="user",
        session_id="s1", score=0.9, created_at=1.0,
    )
    assert m.render() == "[user] hello"
    d = m.to_dict()
    assert d["id"] == "x"
    assert d["score"] == 0.9


def test_code_chunk_render_fenced_block():
    c = CodeChunk(
        id="x", path="a.py", language="python", text="x=1",
        start_line=1, end_line=1, score=0.5,
    )
    out = c.render()
    assert "`a.py`" in out
    assert "```python" in out
    assert "x=1" in out


def test_index_stats_to_dict_truncates_errors():
    s = IndexStats(
        repo="o/r", branch="main",
        errors=[f"e{i}" for i in range(50)],
    )
    d = s.to_dict()
    assert len(d["errors"]) == 20


# ═══════════════════════════════════════════════════════════════════════════
# Section: CloudflareVectorizeCache
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def cache_without_cf(monkeypatch):
    """A cache instance forced into local-fallback mode."""
    monkeypatch.delenv("CLOUDFLARE_API_KEY", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    return CloudflareVectorizeCache(api_key="", account_id="")


@pytest.mark.asyncio
async def test_vectorize_get_empty_prompt_returns_none(cache_without_cf):
    assert await cache_without_cf.get("") is None


@pytest.mark.asyncio
async def test_vectorize_local_round_trip(cache_without_cf):
    """Without CF credentials, set/get still work via the local LRU."""
    await cache_without_cf.set("hello world", "response-A")
    got = await cache_without_cf.get("hello world")
    assert got == "response-A"


@pytest.mark.asyncio
async def test_vectorize_local_case_insensitive(cache_without_cf):
    await cache_without_cf.set("Hello World", "x")
    got = await cache_without_cf.get("hello world")
    assert got == "x"


@pytest.mark.asyncio
async def test_vectorize_clear_wipes_local(cache_without_cf):
    await cache_without_cf.set("a", "b")
    await cache_without_cf.clear()
    assert await cache_without_cf.get("a") is None


@pytest.mark.asyncio
async def test_vectorize_warmup_is_idempotent(cache_without_cf):
    await cache_without_cf._ensure_model()
    await cache_without_cf._ensure_model()  # no-op second call
    assert cache_without_cf._warmed is True


@pytest.mark.asyncio
async def test_vectorize_get_returns_none_when_cf_configured_but_embed_fails(
    monkeypatch,
):
    monkeypatch.setenv("CLOUDFLARE_API_KEY", "k")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")

    cache = CloudflareVectorizeCache()
    # Force the embed call to blow up
    cache._embed = AsyncMock(side_effect=RuntimeError("no network"))
    assert await cache.get("anything") is None


@pytest.mark.asyncio
async def test_vectorize_close_releases_client(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_KEY", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    cache = CloudflareVectorizeCache()
    # Force a client to exist
    _ = cache._client()
    assert cache._http is not None
    await cache.close()
    assert cache._http is None


def test_get_vector_cache_returns_singleton():
    # Reset singleton for determinism
    import core.vectorize as mem_mod
    mem_mod._SINGLETON = None
    c1 = get_vector_cache()
    c2 = get_vector_cache()
    assert c1 is c2


# ═══════════════════════════════════════════════════════════════════════════
# Section: ConversationMemory
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def conv_memory_no_redis(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_KEY", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    return ConversationMemory(redis_client=None)


@pytest.mark.asyncio
async def test_conv_memory_empty_text_returns_none(conv_memory_no_redis):
    assert await conv_memory_no_redis.add_message("u", "s", "user", "") is None
    assert await conv_memory_no_redis.add_message("u", "s", "user", "   ") is None


@pytest.mark.asyncio
async def test_conv_memory_retrieve_without_vector_returns_empty(
    conv_memory_no_redis,
):
    assert await conv_memory_no_redis.retrieve("u", "query") == []


@pytest.mark.asyncio
async def test_conv_memory_retrieve_recent_without_redis_returns_empty(
    conv_memory_no_redis,
):
    assert await conv_memory_no_redis.retrieve_recent("u", "s") == []


@pytest.mark.asyncio
async def test_conv_memory_forget_without_redis_returns_zero(
    conv_memory_no_redis,
):
    assert await conv_memory_no_redis.forget_session("u", "s") == 0


@pytest.mark.asyncio
async def test_conv_memory_add_message_pushes_to_redis(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_KEY", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)

    redis = AsyncMock()
    redis.lpush = AsyncMock(return_value=1)
    redis.ltrim = AsyncMock(return_value=True)
    redis.expire = AsyncMock(return_value=True)
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock(return_value=True)
    redis.sadd = AsyncMock(return_value=1)

    mem = ConversationMemory(redis_client=redis)
    vid = await mem.add_message("u1", "s1", "user", "hello")
    assert vid is not None
    redis.lpush.assert_awaited()


@pytest.mark.asyncio
async def test_conv_memory_vector_enabled_property(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_KEY", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    mem = ConversationMemory()
    assert mem.vector_enabled is False


# ═══════════════════════════════════════════════════════════════════════════
# Section: RepoIndexer
# ═══════════════════════════════════════════════════════════════════════════

def test_repo_indexer_url_parser_accepts_https():
    assert _parse_repo_url("https://github.com/foo/bar") == ("foo", "bar")


def test_repo_indexer_url_parser_strips_git_suffix():
    assert _parse_repo_url("https://github.com/foo/bar.git") == ("foo", "bar")


def test_repo_indexer_url_parser_accepts_short_form():
    assert _parse_repo_url("foo/bar") == ("foo", "bar")


def test_repo_indexer_url_parser_rejects_garbage():
    with pytest.raises(ValueError):
        _parse_repo_url("not a url")


@pytest.mark.asyncio
async def test_repo_indexer_query_returns_empty_without_cf(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_KEY", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    idx = RepoIndexer()
    assert await idx.query("foo/bar", "search") == []


@pytest.mark.asyncio
async def test_repo_indexer_index_repo_bad_url(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_KEY", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    idx = RepoIndexer()
    stats = await idx.index_repo("not-a-repo")
    assert isinstance(stats, IndexStats)
    assert stats.errors


@pytest.mark.asyncio
async def test_repo_indexer_invalidate_without_redis(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_KEY", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    idx = RepoIndexer(redis_client=None)
    assert await idx.invalidate("foo/bar") is False


@pytest.mark.asyncio
async def test_repo_indexer_is_stale_without_redis(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_KEY", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    idx = RepoIndexer(redis_client=None)
    # No fingerprint cache → always stale
    assert await idx.is_stale("foo/bar") is True


# ═══════════════════════════════════════════════════════════════════════════
# Section: chunker helpers
# ═══════════════════════════════════════════════════════════════════════════

def test_chunk_lines_produces_bounded_chunks():
    text = "\n".join(f"line {i}" for i in range(200))
    chunks = _chunk_lines(text, max_chars=200)
    assert all(len(t) <= 400 for _, _, t in chunks)


def test_chunk_python_splits_at_top_level_defs():
    code = (
        "import os\n"
        "def a():\n    pass\n"
        "def b():\n    pass\n"
    )
    chunks = _chunk_python(code)
    # Header + 2 function chunks
    assert len(chunks) >= 3


def test_chunk_js_splits_on_top_level_decls():
    code = (
        "const a = 1;\n"
        "function b() { return 2; }\n"
        "class C {}\n"
    )
    chunks = _chunk_js(code)
    assert len(chunks) >= 2


def test_chunk_file_dispatches_by_extension():
    py = _chunk_file("x.py", "def f():\n    pass\n")
    js = _chunk_file("x.js", "const a = 1;\n")
    assert py and js