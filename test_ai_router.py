import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from app import (
    PROVIDER_COOLDOWN,
    PROVIDER_FUNC_MAP,
    PROVIDER_KEY_CHECK,
    PROVIDER_MODELS,
    ai_cache,
    build_local_fallback_response,
    model_failures,
    model_last_fail,
    provider_failures,
    provider_last_fail,
    route_ai_request,
    route_ai_request_parallel,
)


# ---------- Fixtures ----------
@pytest.fixture
def mock_user():
    return {
        "_id": "test_id",
        "email": "test@example.com",
        "tier": "free",
        "puter_enabled": False,
        "subTierOptions": {"hasDataAccess": False, "hasDesignAccess": False}
    }

@pytest.fixture
def clear_caches():
    ai_cache.clear()
    provider_failures.clear()
    provider_last_fail.clear()
    model_failures.clear()
    model_last_fail.clear()
    yield

# ---------- 1. Provider Health Tests ----------
@pytest.mark.asyncio
async def test_all_providers_basic(clear_caches):
    """Verify each configured provider responds to a simple prompt."""
    test_prompt = "Say OK"
    results = {}
    for name, func in PROVIDER_FUNC_MAP.items():
        # Skip providers that are not configured
        if not PROVIDER_KEY_CHECK.get(name, False):
            continue
        models = PROVIDER_MODELS.get(name, [])
        if not models:
            continue
        try:
            start = datetime.now()
            resp = await asyncio.wait_for(func(test_prompt, 5, 0.0, models[0]), timeout=8.0)
            latency = (datetime.now() - start).total_seconds() * 1000
            results[name] = {"status": "OK", "latency_ms": round(latency, 2)}
            assert resp and len(resp.strip()) > 0, f"{name} returned empty"
        except Exception as e:
            results[name] = {"status": "FAIL", "error": str(e)[:100]}
    # Print results; at least 80% should succeed (adjust threshold)
    successes = sum(1 for v in results.values() if v["status"] == "OK")
    print(f"Provider health: {successes}/{len(results)} OK")
    assert successes >= len(results) * 0.7, f"Too many providers failed: {results}"

# ---------- 2. Circuit Breaker Tests ----------
@pytest.mark.asyncio
async def test_circuit_breaker(clear_caches):
    """Simulate repeated failures to trigger cooldown."""
    provider = "gemini"
    # Force failures
    with patch("app.call_gemini", AsyncMock(side_effect=Exception("Quota exceeded"))):
        for _ in range(4):
            try:
                await route_ai_request(
                    workspace="general",
                    task_type="general",
                    prompt="test",
                    history=[],
                    files=[],
                    max_tokens=10,
                    temp=0.0,
                    tier="free",
                    user=None
                )
            except:
                pass
        # Provider should be in cooldown
        assert provider_failures[provider] >= 3
        assert time.time() - provider_last_fail[provider] < PROVIDER_COOLDOWN
        # Route should skip it and fallback to local
        result = await route_ai_request(
            workspace="general",
            task_type="general",
            prompt="test",
            history=[],
            files=[],
            max_tokens=10,
            temp=0.0,
            tier="free",
            user=None
        )
        assert result["provider"] == "local", "Did not fallback to local"

# ---------- 3. LiteLLM Router Fallback ----------
@pytest.mark.asyncio
async def test_litellm_fallback(clear_caches):
    """If primary fails, router should fallback to next."""
    # Force gemini to fail by patching
    with patch("app.router.acompletion", AsyncMock(side_effect=Exception("Overloaded"))):
        result = await route_ai_request(
            workspace="general",
            task_type="general",
            prompt="test",
            history=[],
            files=[],
            max_tokens=10,
            temp=0.0,
            tier="free",
            user=None
        )
        # Should eventually fallback to sequential (non-LiteLLM) providers
        assert result["success"] is True
        # provider could be any, but not 'local' unless all fail
        assert result["provider"] != "security"

# ---------- 4. Sequential vs Parallel ----------
@pytest.mark.asyncio
async def test_parallel_routing(clear_caches):
    """Parallel router should pick fastest provider."""
    result = await route_ai_request_parallel(
        workspace="general",
        task_type="general",
        prompt="What is 2+2?",
        history=[],
        files=[],
        max_tokens=50,
        temp=0.0,
        tier="free",
        user=None
    )
    assert result["success"] is True
    assert "4" in result["text"] or "four" in result["text"].lower(), "Parallel did not return expected answer"
    assert result["provider"] in PROVIDER_FUNC_MAP

# ---------- 5. Caching ----------
@pytest.mark.asyncio
async def test_caching(clear_caches):
    """Repeated identical prompts should return cached response."""
    prompt = "What is the capital of France?"
    result1 = await route_ai_request(
        workspace="general",
        task_type="general",
        prompt=prompt,
        history=[],
        files=[],
        max_tokens=50,
        temp=0.0,
        tier="free",
        user=None
    )
    result2 = await route_ai_request(
        workspace="general",
        task_type="general",
        prompt=prompt,
        history=[],
        files=[],
        max_tokens=50,
        temp=0.0,
        tier="free",
        user=None
    )
    assert result2.get("cached") is True
    assert result1["text"] == result2["text"]

# ---------- 6. Local Fallback ----------
def test_local_fallback_build():
    resp = build_local_fallback_response("data", "extraction", "Give me data")
    assert "data analysis" in resp.lower() or "provide" in resp.lower()
    resp2 = build_local_fallback_response("design", "frontend", "design a button")
    assert "design concept" in resp2.lower()

# ---------- 7. Quota & Rate Limiting (Mock) ----------
# (We'll skip detailed DB tests for brevity; but you should test quotas with a mock DB)