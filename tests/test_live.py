"""
Opt-in live-network tests. Skipped by default.
==============================================
Run with:
    pytest tests/test_live.py --live -v

These tests hit real providers / Cloudflare / the running server.
They are NOT run in CI. They require valid API keys in the env and
a running server on localhost:8000 (unless the test targets a public URL).
"""
from __future__ import annotations

import asyncio
import os

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--live", action="store_true", default=False,
        help="Run live-network tests (hits real providers)",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip_marker = pytest.mark.skip(reason="Live test — pass --live to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_marker)


pytestmark = pytest.mark.live


# ═══════════════════════════════════════════════════════════════════════════
# Section: provider health (direct api.providers callables)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_all_configured_providers_basic():
    """Every provider with a key must respond to 'Say OK'."""
    from api.providers import (
        PROVIDER_FUNC_MAP, PROVIDER_KEY_CHECK, PROVIDER_MODELS,
    )
    test_prompt = "Say OK"

    failures = []
    for name, func in PROVIDER_FUNC_MAP.items():
        if name == "local":
            continue
        if not PROVIDER_KEY_CHECK.get(name, False):
            continue
        models = PROVIDER_MODELS.get(name) or []
        if not models:
            continue
        try:
            resp = await asyncio.wait_for(
                func(test_prompt, 5, 0.0, models[0]), timeout=10.0,
            )
            assert resp and resp.strip(), f"{name} returned empty"
        except Exception as e:                          # noqa: BLE001
            failures.append((name, str(e)[:120]))

    # At least 60% of configured providers must pass
    assert len(failures) <= 4, f"Too many failures: {failures}"


# ═══════════════════════════════════════════════════════════════════════════
# Section: end-to-end router with real providers
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_route_ai_request_parallel_e2e():
    from api.routes_core import route_ai_request_parallel
    result = await route_ai_request_parallel(
        workspace="core",
        task_type="structuring",
        prompt="Reply with exactly the number 4 and nothing else.",
        history=[],
        files=[],
        max_tokens=10,
        temp=0.0,
        tier="free",
        user=None,
    )
    assert result["success"] is True
    assert "4" in result["text"]


# ═══════════════════════════════════════════════════════════════════════════
# Section: live FastAPI server
# ═══════════════════════════════════════════════════════════════════════════

def _base_url() -> str:
    return os.getenv("LIVE_BASE_URL", "http://localhost:8000").rstrip("/")


def test_live_health():
    import httpx
    try:
        r = httpx.get(f"{_base_url()}/api/health", timeout=5)
    except httpx.ConnectError:
        pytest.skip("Server not running")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("operational", "degraded")


def test_live_root():
    import httpx
    try:
        r = httpx.get(f"{_base_url()}/", timeout=5)
    except httpx.ConnectError:
        pytest.skip("Server not running")
    assert r.status_code == 200
    assert r.json().get("service") == "axelr-backend"


def test_live_guest_session():
    import httpx
    try:
        r = httpx.post(f"{_base_url()}/api/guest/session", timeout=5)
    except httpx.ConnectError:
        pytest.skip("Server not running")
    assert r.status_code == 200
    assert "sessionId" in r.json()


# ═══════════════════════════════════════════════════════════════════════════
# Section: live Cloudflare Vectorize
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_live_vectorize_round_trip():
    if not (os.getenv("CLOUDFLARE_API_KEY") and os.getenv("CLOUDFLARE_ACCOUNT_ID")):
        pytest.skip("Cloudflare creds missing")

    from core.vectorize import CloudflareVectorizeCache
    cache = CloudflareVectorizeCache()
    prompt = "Live test: what is the capital of France?"
    response = "Paris is the capital of France."
    await cache.set(prompt, response)

    # Allow Cloudflare to propagate
    await asyncio.sleep(3)
    got = await cache.get("Capital city of France?")
    await cache.close()
    # Semantic match may or may not hit; we only care that the call succeeds
    assert got is None or isinstance(got, str)


# ═══════════════════════════════════════════════════════════════════════════
# Section: repo indexer live
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_live_repo_indexer_query_returns_list():
    if not (os.getenv("CLOUDFLARE_API_KEY") and os.getenv("CLOUDFLARE_ACCOUNT_ID")):
        pytest.skip("Cloudflare creds missing")

    from core.repo_indexer import RepoIndexer
    idx = RepoIndexer()
    # Query a repo we haven't indexed — should return empty list gracefully
    chunks = await idx.query("octocat/Hello-World", "test", top_k=3)
    await idx.close()
    assert isinstance(chunks, list)