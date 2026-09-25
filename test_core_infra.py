"""
Unit tests for core/infra.py — cache + circuit breaker + context registry.
=================================================================
All tests run against the in-memory fallback path (Redis=None) unless
explicitly testing the Redis path with a mocked client.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from core.infra import CircuitBreaker, ContextRegistry, ContextItem


# ── CircuitBreaker: in-memory path ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_breaker_allows_when_closed():
    b = CircuitBreaker(redis_client=None, threshold=3, cooldown_s=60)
    assert await b.is_available("groq") is True


@pytest.mark.asyncio
async def test_breaker_blocks_after_threshold():
    b = CircuitBreaker(redis_client=None, threshold=3, cooldown_s=60)
    for _ in range(3):
        await b.record_failure("groq")
    assert await b.is_available("groq") is False


@pytest.mark.asyncio
async def test_breaker_success_resets_failures():
    b = CircuitBreaker(redis_client=None, threshold=3, cooldown_s=60)
    await b.record_failure("groq")
    await b.record_failure("groq")
    await b.record_success("groq")
    assert await b.is_available("groq") is True
    assert b._mem_failures["groq"] == 0


@pytest.mark.asyncio
async def test_breaker_cooldown_expires():
    b = CircuitBreaker(redis_client=None, threshold=2, cooldown_s=0.1)
    await b.record_failure("groq")
    await b.record_failure("groq")
    assert await b.is_available("groq") is False
    await asyncio.sleep(0.15)
    assert await b.is_available("groq") is True


@pytest.mark.asyncio
async def test_breaker_rate_limit_trips_immediately():
    """A single rate-limit failure should trip with the long cooldown."""
    b = CircuitBreaker(
        redis_client=None, threshold=2,
        cooldown_s=0.1, rate_limit_cooldown_s=30,
    )
    await b.record_failure("gemini", is_rate_limit=True)
    assert await b.is_available("gemini") is False
    until = b._mem_cooldowns["gemini"]
    assert until - time.time() > 25


@pytest.mark.asyncio
async def test_breaker_snapshot_in_memory():
    b = CircuitBreaker(redis_client=None, threshold=1, cooldown_s=60)
    await b.record_failure("x")
    snap = await b.snapshot()
    assert "x" in snap
    assert snap["x"]["open"] is True
    assert snap["x"]["cooldown_remaining_s"] > 0


# ── CircuitBreaker: Redis path (mocked) ──────────────────────────────────────

@pytest.mark.asyncio
async def test_breaker_redis_path_consulted():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)   # not tripped
    redis.incr = AsyncMock(return_value=1)     # first failure
    redis.expire = AsyncMock(return_value=True)
    redis.setex = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=1)

    b = CircuitBreaker(redis_client=redis, threshold=3, cooldown_s=60,
                       namespace="test:cb")
    await b.record_failure("groq")
    redis.incr.assert_awaited()
    redis.expire.assert_awaited()


@pytest.mark.asyncio
async def test_breaker_redis_open_key_blocks():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value="1")    # open
    b = CircuitBreaker(redis_client=redis, threshold=3, cooldown_s=60)
    assert await b.is_available("groq") is False


@pytest.mark.asyncio
async def test_breaker_degrades_to_memory_on_redis_exception():
    redis = AsyncMock()
    redis.get = AsyncMock(side_effect=RuntimeError("redis down"))
    b = CircuitBreaker(redis_client=redis, threshold=2, cooldown_s=60)
    # Should not raise; falls through to memory
    assert await b.is_available("groq") is True
    await b.record_failure("groq")
    await b.record_failure("groq")
    assert await b.is_available("groq") is False


# ── ContextRegistry: degraded mode ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_context_registry_no_sources_returns_none(monkeypatch):
    for var in ("JIRA_URL", "JIRA_TOKEN", "LINEAR_API_KEY",
                "OPENAPI_CONTEXT_URL"):
        monkeypatch.delenv(var, raising=False)
    reg = ContextRegistry()
    assert await reg.get_context("u1", "core") is None


@pytest.mark.asyncio
async def test_context_registry_set_then_get(monkeypatch):
    for var in ("JIRA_URL", "JIRA_TOKEN", "LINEAR_API_KEY",
                "OPENAPI_CONTEXT_URL"):
        monkeypatch.delenv(var, raising=False)

    redis = AsyncMock()
    store: dict[str, str] = {}

    async def _get(k): return store.get(k)
    async def _setex(k, ttl, v): store[k] = v; return True
    redis.get = _get
    redis.setex = _setex

    reg = ContextRegistry(redis_client=redis)
    # Manually prime the sources dict so get_context short-circuits early
    reg._sources = {"jira": {"url": "http://x", "token": "t", "project": "P"}}

    await reg.set_context("u1", "core", "cached-context")
    assert await reg.get_context("u1", "core") == "cached-context"


@pytest.mark.asyncio
async def test_context_registry_fetch_failure_isolated(monkeypatch):
    """A broken Jira must not propagate an exception."""
    reg = ContextRegistry()
    reg._sources = {"jira": {"url": "http://127.0.0.1:1", "token": "x",
                             "project": "P"}}
    # _fetch_jira swallows the connection error
    items = await reg._fetch_jira("u1")
    assert items == []


# ── ContextItem ──────────────────────────────────────────────────────────────

def test_context_item_render_includes_metadata():
    item = ContextItem(
        source="jira", type="issue", title="Fix login",
        description="Users cannot log in",
        status="Open", priority="High",
    )
    rendered = item.render()
    assert "[jira]" in rendered
    assert "Fix login" in rendered
    assert "Open" in rendered
    assert "High" in rendered


def test_context_item_render_truncates_description():
    item = ContextItem(
        source="linear", type="issue", title="x",
        description="a" * 1000,
    )
    rendered = item.render()
    # _PER_ITEM_CHAR_LIMIT == 200
    assert len(rendered) < 500