#!/usr/bin/env python3
"""
AXELR - Full Provider/Model Test Suite
Run: python test_all_providers.py
"""

import asyncio
import json
import sys
import time
from datetime import datetime

# Load .env (same as app)
from dotenv import load_dotenv

load_dotenv(override=True)

# Import all provider functions and their model lists from app
# We need to import from app, but app also does a lot of initialization (DB, etc.)
# We'll selectively import only the functions and constants we need.

# To avoid running the whole app, we'll import from app but skip the startup.
# We'll manually load the functions and model dicts.

# However, app.py has many side effects (e.g., creating Router, HTTP client).
# We'll create a minimal environment by mocking the Router and HTTP client.
# Alternatively, we can just call the provider functions directly – they are async.

# We'll copy the provider functions into this script or import them.
# For simplicity, we'll import app and patch anything that might fail.
import app

# Re-use the HTTP_CLIENT from app (already initialized)
http_client = app.HTTP_CLIENT

# We'll test provider functions with their own models.
PROVIDER_MODELS = app.PROVIDER_MODELS
PROVIDER_FUNC_MAP = app.PROVIDER_FUNC_MAP
PROVIDER_KEY_CHECK = app.PROVIDER_KEY_CHECK

# Override any missing keys with environment variables from .env
# (already loaded)

# Test parameters
TEST_PROMPT = "Say 'Hello' in exactly one word."
MAX_TOKENS = 5
TEMPERATURE = 0.0
TIMEOUT = 10  # seconds per call

async def test_provider(provider_name: str, func, models: list[str]) -> dict:
    """Test a single provider with all its models."""
    results = {
        "provider": provider_name,
        "models": {},
        "overall": "unknown"
    }
    if not PROVIDER_KEY_CHECK.get(provider_name, False):
        results["overall"] = "skipped (no key)"
        return results

    if not models:
        results["overall"] = "skipped (no models)"
        return results

    successes = 0
    for model in models:
        start = time.time()
        try:
            # Use asyncio.wait_for to enforce timeout
            resp = await asyncio.wait_for(
                func(TEST_PROMPT, MAX_TOKENS, TEMPERATURE, model),
                timeout=TIMEOUT
            )
            elapsed = (time.time() - start) * 1000
            if resp and len(resp.strip()) > 0:
                results["models"][model] = {
                    "status": "OK",
                    "latency_ms": round(elapsed, 2),
                    "preview": resp[:80]
                }
                successes += 1
            else:
                results["models"][model] = {
                    "status": "EMPTY",
                    "latency_ms": round(elapsed, 2)
                }
        except asyncio.TimeoutError:
            results["models"][model] = {"status": "TIMEOUT"}
        except Exception as e:
            results["models"][model] = {"status": "ERROR", "error": str(e)[:200]}

    total = len(models)
    results["overall"] = f"{successes}/{total} OK" if successes > 0 else "FAIL"
    return results

async def test_litellm_router():
    """Test LiteLLM router with each configured model."""
    from app import router
    results = {}
    # We'll test each model in the router's model_list
    for model_entry in app._router_models:
        model_name = model_entry["model_name"]
        full_model = model_entry["litellm_params"]["model"]
        try:
            start = time.time()
            response = await asyncio.wait_for(
                router.acompletion(
                    model=full_model,
                    messages=[{"role": "user", "content": TEST_PROMPT}],
                    max_tokens=MAX_TOKENS,
                    temperature=TEMPERATURE,
                ),
                timeout=10
            )
            elapsed = (time.time() - start) * 1000
            if response and response.choices:
                text = response.choices[0].message.content
                results[model_name] = {
                    "status": "OK",
                    "latency_ms": round(elapsed, 2),
                    "preview": text[:80]
                }
            else:
                results[model_name] = {"status": "EMPTY"}
        except Exception as e:
            results[model_name] = {"status": "ERROR", "error": str(e)[:200]}
    return results

async def main():
    print("=" * 80)
    print("AXELR AI - FULL PROVIDER & MODEL TEST")
    print(f"Started at {datetime.utcnow().isoformat()}")
    print("=" * 80)

    # 1. Test all custom providers
    all_results = {}
    for name, func in PROVIDER_FUNC_MAP.items():
        models = PROVIDER_MODELS.get(name, [])
        print(f"\nTesting provider: {name} (models: {len(models)})")
        result = await test_provider(name, func, models)
        all_results[name] = result
        # Print quick summary for this provider
        print(f"  -> {result['overall']}")

    # 2. Test LiteLLM router
    print("\n\nTesting LiteLLM router...")
    litellm_results = await test_litellm_router()
    all_results["__litellm__"] = litellm_results

    # 3. Generate report
    print("\n\n" + "=" * 80)
    print("FINAL REPORT")
    print("=" * 80)

    # Summary table
    print(f"{'Provider':<20} {'Status':<20} {'Models OK/Total':<15} {'Notes'}")
    print("-" * 80)
    for name, res in all_results.items():
        if name == "__litellm__":
            # LiteLLM is a special case
            ok = sum(1 for v in res.values() if v.get("status") == "OK")
            total = len(res)
            print(f"{'LiteLLM (router)':<20} {'OK' if ok>0 else 'FAIL':<20} {ok}/{total:<15} ")
            # Print each model detail
            for mname, mres in res.items():
                status = mres.get("status", "UNKNOWN")
                preview = mres.get("preview", "")
                print(f"  - {mname:<30} {status:<10} {preview[:50]}")
        else:
            status = res.get("overall", "unknown")
            models = res.get("models", {})
            ok = sum(1 for m in models.values() if m.get("status") == "OK")
            total = len(models)
            print(f"{name:<20} {status:<20} {ok}/{total:<15} ")
            # Print any failing models
            if ok < total:
                for mname, mres in models.items():
                    if mres.get("status") != "OK":
                        print(f"  - {mname:<30} {mres.get('status')} - {mres.get('error', '')[:60]}")

    # 4. Save full JSON report
    filename = f"test_report_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
    with open(filename, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nFull report saved to {filename}")

    # 5. Exit with appropriate code
    # Check if any provider that should work failed completely
    critical_fail = False
    for name, res in all_results.items():
        if name == "__litellm__":
            continue
        if res.get("overall") == "FAIL" and PROVIDER_KEY_CHECK.get(name, False):
            critical_fail = True
            break
    sys.exit(1 if critical_fail else 0)

if __name__ == "__main__":
    asyncio.run(main())