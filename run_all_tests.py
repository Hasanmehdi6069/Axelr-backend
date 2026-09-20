import asyncio
import time

from app import (
    PROVIDER_FUNC_MAP,
    PROVIDER_KEY_CHECK,
    PROVIDER_MODELS,
    build_local_fallback_response,
    provider_failures,
    route_ai_request,
    route_ai_request_parallel,
)


async def test_provider(name, func, model):
    test_prompt = "Say OK"
    try:
        start = time.time()
        resp = await asyncio.wait_for(func(test_prompt, 5, 0.0, model), timeout=8.0)
        latency = (time.time() - start) * 1000
        if resp and len(resp.strip()) > 0:
            return {"status": "OK", "latency_ms": round(latency, 2)}
        else:
            return {"status": "EMPTY", "response": resp[:50] if resp else ""}
    except Exception as e:
        return {"status": "FAIL", "error": str(e)[:100]}

async def test_all_providers():
    results = {}
    for name, func in PROVIDER_FUNC_MAP.items():
        if not PROVIDER_KEY_CHECK.get(name, False):
            results[name] = {"status": "SKIPPED", "reason": "Not configured"}
            continue
        models = PROVIDER_MODELS.get(name, [])
        if not models:
            results[name] = {"status": "SKIPPED", "reason": "No models"}
            continue
        results[name] = await test_provider(name, func, models[0])
    return results

async def test_circuit_breaker():
    # Force gemini to fail
    original = PROVIDER_FUNC_MAP["gemini"]
    PROVIDER_FUNC_MAP["gemini"] = lambda *args, **kwargs: raise_exception("Quota exceeded")
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
    PROVIDER_FUNC_MAP["gemini"] = original
    # Check that provider is in cooldown
    if provider_failures.get("gemini", 0) >= 3:
        return {"status": "PASS", "detail": "Circuit breaker triggered"}
    else:
        return {"status": "FAIL", "detail": "Circuit breaker not triggered"}

async def test_caching():
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
    if result2.get("cached") and result1["text"] == result2["text"]:
        return {"status": "PASS"}
    else:
        return {"status": "FAIL", "detail": "Caching not working"}

async def test_parallel():
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
    if result["success"] and "4" in result["text"]:
        return {"status": "PASS", "provider": result.get("provider")}
    else:
        return {"status": "FAIL", "text": result.get("text", "")}

def test_local_fallback():
    resp = build_local_fallback_response("data", "extraction", "Give me data")
    if "data analysis" in resp.lower() or "provide" in resp.lower():
        return {"status": "PASS"}
    return {"status": "FAIL"}

async def main():
    print("=== AXELR AI TEST SUITE ===")
    print("Testing providers...")
    provider_results = await test_all_providers()
    ok = sum(1 for v in provider_results.values() if v.get("status") == "OK")
    total = sum(1 for v in provider_results.values() if v.get("status") != "SKIPPED")
    print(f"Providers: {ok}/{total} OK")
    for name, res in provider_results.items():
        print(f"  {name}: {res.get('status')} {res.get('latency_ms', '')}ms {res.get('error', '')}")

    print("\nTesting circuit breaker...")
    cb = await test_circuit_breaker()
    print(f"  {cb['status']}: {cb.get('detail', '')}")

    print("\nTesting caching...")
    cache = await test_caching()
    print(f"  {cache['status']}")

    print("\nTesting parallel routing...")
    par = await test_parallel()
    print(f"  {par['status']} (provider: {par.get('provider', 'N/A')})")

    print("\nTesting local fallback...")
    loc = test_local_fallback()
    print(f"  {loc['status']}")

    # Overall summary
    all_ok = all([
        ok >= total * 0.7,
        cb['status'] == 'PASS',
        cache['status'] == 'PASS',
        par['status'] == 'PASS',
        loc['status'] == 'PASS'
    ])
    print("\n=== OVERALL RESULT ===")
    print("✅ PASS" if all_ok else "❌ FAIL")
    if not all_ok:
        print("Please check the individual failures above.")

if __name__ == "__main__":
    asyncio.run(main())