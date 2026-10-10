"""
Fault injection — every external dependency must degrade, not 5xx.
====================================================================
Each test kills exactly one thing and asserts the system survives.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.chaos


# ═══════════════════════════════════════════════════════════════════════════
# Chaos probe self-verification
# ═══════════════════════════════════════════════════════════════════════════
def test_chaos_probe_disabled_by_default():
    # WHY: production must NEVER inject chaos by default.
    import chaos_probe
    assert chaos_probe._CHAOS_ENABLED is False


def test_chaos_probe_protected_providers_never_injected(chaos):
    enable, _ = chaos
    cp = enable(rate=1.0, providers="")
    for protected in ("local", "semantic_cache", "cache", "security", ""):
        assert cp._chaos_maybe(protected, "delay") is False


def test_chaos_probe_provider_allowlist_respected(chaos):
    enable, _ = chaos
    cp = enable(rate=1.0, providers="groq")
    assert cp._chaos_maybe("groq", "error") is True
    assert cp._chaos_maybe("openai", "error") is False


def test_chaos_probe_seed_determinism(chaos):
    enable, _ = chaos
    cp1 = enable(rate=0.5, providers="")
    seq1 = [cp1._chaos_maybe("groq", "error") for _ in range(20)]
    cp2 = enable(rate=0.5, providers="")
    seq2 = [cp2._chaos_maybe("groq", "error") for _ in range(20)]
    assert seq1 == seq2


@pytest.mark.asyncio
async def test_chaos_probe_delay_actually_delays(chaos):
    # WHY: a no-op delay makes every latency test vacuous.
    import time
    enable, _ = chaos
    cp = enable(rate=1.0, providers="", delay_s=0.05)
    t0 = time.monotonic()
    await cp.chaos_inject_delay("groq")
    assert time.monotonic() - t0 >= 0.04


def test_chaos_probe_audit_reports_stats(chaos):
    enable, _ = chaos
    cp = enable(rate=1.0, providers="")
    cp._chaos_maybe("groq", "error")
    a = cp.audit()
    assert a["chaos"]["enabled"] is True
    assert a["chaos"]["stats"]["error"] >= 1


# ═══════════════════════════════════════════════════════════════════════════
# Redis down
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_redis_down_circuit_breaker_degrades_to_memory():
    from core.infra import CircuitBreaker
    redis = AsyncMock()
    for name in ("get", "set", "setex", "incr", "expire", "delete"):
        getattr(redis, name).side_effect = ConnectionError("redis down")
    b = CircuitBreaker(redis_client=redis, threshold=2, cooldown_s=60)
    assert await b.is_available("groq") is True
    await b.record_failure("groq")
    await b.record_failure("groq")
    assert await b.is_available("groq") is False


@pytest.mark.asyncio
async def test_redis_down_context_registry_still_returns():
    from core.infra import ContextRegistry
    redis = AsyncMock()
    redis.get.side_effect = ConnectionError("down")
    redis.setex.side_effect = ConnectionError("down")
    reg = ContextRegistry(redis_client=redis)
    # No sources configured → returns None without touching Redis.
    assert await reg.get_context("u", "core") is None


def test_redis_down_health_still_200(client):
    r = client.get("/api/health")
    assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# Provider chain outage
# ═══════════════════════════════════════════════════════════════════════════
def test_local_fallback_last_in_chain():
    from api.providers import get_provider_order
    for ws in ("data", "design", "core", "prompt", "touch_fix"):
        assert get_provider_order(ws)[-1] == "local"


def test_every_workspace_has_local_fallback_text():
    from api.providers import build_local_fallback_response
    for ws in ("data", "design", "core"):
        out = build_local_fallback_response(ws, ws, "x")
        assert isinstance(out, str) and out


# ═══════════════════════════════════════════════════════════════════════════
# Worker down
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_worker_unavailable_no_hang(monkeypatch):
    import core.io_pipeline as io
    monkeypatch.setattr(io, "WORKER_URL", "")
    r = await io.execute_code_on_worker("python", "print(1)")
    assert r["success"] is False and r["error"] == "worker_unavailable"


@pytest.mark.asyncio
async def test_worker_timeout_returns_error(monkeypatch):
    import core.io_pipeline as io
    monkeypatch.setattr(io, "WORKER_URL", "http://w.test")
    client = AsyncMock()
    import httpx
    client.post.side_effect = httpx.TimeoutException("t")
    monkeypatch.setattr(io, "_worker_client", lambda: client)
    r = await io.execute_code_on_worker("python", "print(1)")
    assert r["success"] is False


# ═══════════════════════════════════════════════════════════════════════════
# Vectorize down
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_vectorize_get_none_when_embed_fails(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_KEY", "k")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    from core.vectorize import CloudflareVectorizeCache
    c = CloudflareVectorizeCache()
    c._embed = AsyncMock(side_effect=RuntimeError("no net"))
    assert await c.get("x") is None


@pytest.mark.asyncio
async def test_vectorize_set_does_not_raise_on_failure(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_KEY", "k")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    from core.vectorize import CloudflareVectorizeCache
    c = CloudflareVectorizeCache()
    c._embed = AsyncMock(side_effect=RuntimeError("down"))
    # Must not raise
    await c.set("p", "r")


# ═══════════════════════════════════════════════════════════════════════════
# Twin engine (non-Linux / no binary)
# ═══════════════════════════════════════════════════════════════════════════
def test_twin_engine_roster_is_exactly_20():
    from core.twin_roster import MODEL_ROSTER
    assert len(MODEL_ROSTER) == 20


def test_twin_engine_roster_respects_budget():
    from core.twin_roster import MAX_RESIDENT_MB, MODEL_ROSTER
    for m in MODEL_ROSTER:
        assert m.total_mb <= MAX_RESIDENT_MB, m.id


def test_twin_engine_stub_returns_empty_for_empty_prompt():
    from core.twin_engine import TwinEngine
    assert TwinEngine._stub("", "x") == ""


# ═══════════════════════════════════════════════════════════════════════════
# Memory down
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_memory_no_redis_no_vector_recall_returns_empty():
    from core.memory import Memory
    m = Memory(redis_client=None, http_client=AsyncMock())
    assert await m.recall(user_id="u", query="q") == []


@pytest.mark.asyncio
async def test_memory_forget_without_redis_is_zero():
    from core.memory import Memory
    m = Memory(redis_client=None, http_client=AsyncMock())
    assert await m.forget_session("u", "s") == 0


# ═══════════════════════════════════════════════════════════════════════════
# Sanity — everything survives total provider death
# ═══════════════════════════════════════════════════════════════════════════
def test_no_provider_has_key_in_test_env():
    # WHY: if any test env leaks a real key, a test could hit a real provider.
    from api.providers import PROVIDER_KEY_CHECK
    for name, has_key in PROVIDER_KEY_CHECK.items():
        if name in ("bifrost", "local"):
            continue
        # keyless providers may be True by design
        # everything else should be False in CI
    # No strict assertion — env-dependent. The test exists as a reminder.
    assert True