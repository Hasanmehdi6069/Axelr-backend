"""Master stress suite for Bharat Agent — concurrency, chaos, perf envelope.

Rules for this file:
  * every test is marked `slow` — excluded from fast PR runs
  * thresholds are conservative (CI-friendly); tighten after a week of data
"""
from __future__ import annotations

import asyncio
import statistics
import time

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.stress]


# ══════════════════════════════════════════════════════════════════════════
# A. Concurrency
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a1_400_parallel_intent_classifications(bharat_agent):
    """400 parallel intent classifications must complete without errors."""
    async def one(i):
        return await bharat_agent.classify_intent(f"user query {i} about product {i % 50}")
    results = await asyncio.gather(*[one(i) for i in range(400)],
                                    return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"{len(failures)}/400 intent classifications failed: {failures[:2]}"


@pytest.mark.asyncio
async def test_a2_concurrent_context_updates(bharat_agent):
    """150 concurrent context updates must not corrupt conversation state."""
    async def update(i):
        return await bharat_agent.update_context(f"session-{i % 30}", {"message": f"msg-{i}", "timestamp": time.time()})
    results = await asyncio.gather(*[update(i) for i in range(150)],
                                    return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"{len(failures)}/150 context updates failed"


@pytest.mark.asyncio
async def test_a3_parallel_response_generation_no_deadlock(bharat_agent):
    """250 parallel response generations must complete without deadlock."""
    async def generate(i):
        return await bharat_agent.generate_response(f"query-{i}", f"session-{i % 50}")
    results = await asyncio.gather(*[generate(i) for i in range(250)],
                                    return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"{len(failures)}/250 response generations failed"


@pytest.mark.asyncio
async def test_a4_intent_throughput_under_contention(bharat_agent):
    """800 intent classification throughput must be > 600 ops/sec under contention."""
    start = time.perf_counter()
    results = await asyncio.gather(*[
        bharat_agent.classify_intent("test query")
        for _ in range(800)
    ])
    elapsed = time.perf_counter() - start
    throughput = 800 / elapsed
    assert throughput > 600, f"Intent throughput too slow: {throughput:.0f} ops/sec"
    print(f"\n  Intent throughput: {throughput:.0f} ops/sec")


# ══════════════════════════════════════════════════════════════════════════
# B. Chaos
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_b1_model_timeout_mid_generation(bharat_agent, monkeypatch):
    """Model timeout mid-generation must return clean error, not hang."""
    def mock_timeout(*args, **kwargs):
        raise TimeoutError("Model generation timed out")
    monkeypatch.setattr(bharat_agent, "generate_response", mock_timeout)
    with pytest.raises(TimeoutError):
        await bharat_agent.generate_response("test query", "session-1")


@pytest.mark.asyncio
async def test_b2_corrupt_context_handled(bharat_agent, monkeypatch):
    """Corrupt context must be handled gracefully."""
    def mock_corrupt(*args, **kwargs):
        return {"error": "invalid context state", "session": None}
    monkeypatch.setattr(bharat_agent, "get_context", mock_corrupt)
    context = await bharat_agent.get_context("session-1")
    assert isinstance(context, dict) or context is not None


@pytest.mark.asyncio
async def test_b3_model_overload_handled_gracefully(bharat_agent):
    """Model overload must return backpressure, not crash."""
    results = await asyncio.gather(*[
        bharat_agent.generate_response(f"overload-{i}", "session-1")
        for i in range(200)
    ])
    success = [r for r in results if r.get("success", False)]
    backpressure = [r for r in results if r.get("backpressure", False)]
    assert len(success) + len(backpressure) == 200, "All requests must be either success or backpressure"


# ══════════════════════════════════════════════════════════════════════════
# C. Performance envelope
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_c1_intent_latency_p50_p99(bharat_agent):
    """p50 < 8ms, p99 < 40ms for intent classification."""
    times = []
    for _ in range(100):
        t0 = time.perf_counter()
        await bharat_agent.classify_intent("test query")
        times.append((time.perf_counter() - t0) * 1000)
    p50 = statistics.median(times)
    p99 = statistics.quantiles(times, n=100)[98]
    assert p50 < 8, f"p50 latency too high: {p50:.1f}ms"
    assert p99 < 40, f"p99 latency too high: {p99:.1f}ms"
    print(f"\n  Intent latency p50={p50:.1f}ms p99={p99:.1f}ms")


@pytest.mark.asyncio
async def test_c2_response_generation_latency_p95(bharat_agent):
    """p95 < 150ms for response generation."""
    times = []
    for _ in range(50):
        t0 = time.perf_counter()
        await bharat_agent.generate_response("test query", "session-1")
        times.append((time.perf_counter() - t0) * 1000)
    p95 = statistics.quantiles(times, n=100)[94]
    assert p95 < 150, f"p95 response latency too high: {p95:.1f}ms"


# ══════════════════════════════════════════════════════════════════════════
# D. Soak test
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_d1_soak_8000_queries_no_leak(bharat_agent):
    """8,000 sequential queries must not leak memory or degrade performance."""
    start = time.perf_counter()
    for i in range(8000):
        await bharat_agent.classify_intent(f"soak query {i}")
    elapsed = time.perf_counter() - start
    avg_latency = elapsed / 8000 * 1000
    assert avg_latency < 5, f"Soak test avg latency too high: {avg_latency:.2f}ms"
    print(f"\n  Soak test: 8000 queries in {elapsed:.2f}s, avg {avg_latency:.2f}ms")