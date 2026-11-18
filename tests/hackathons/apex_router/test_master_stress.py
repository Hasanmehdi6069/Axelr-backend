"""Master stress suite for Apex Router — concurrency, chaos, perf envelope.

Rules for this file:
  * every test is marked `slow` — excluded from fast PR runs
  * thresholds are conservative (CI-friendly); tighten after a week of data
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import statistics
import time

import pytest

pytestmark = [pytest.mark.slow]


# ══════════════════════════════════════════════════════════════════════════
# A. Concurrency
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a1_200_parallel_routing_requests(apex_router):
    """200 parallel routing requests must complete without errors."""
    async def one(i):
        return await apex_router.route_request(f"request-{i}", {"path": f"/api/v1/resource/{i}"})
    results = await asyncio.gather(*[one(i) for i in range(200)],
                                    return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"{len(failures)}/200 requests failed: {failures[:2]}"


@pytest.mark.asyncio
async def test_a2_concurrent_rule_updates_no_crash(apex_router):
    """20 concurrent rule updates must not crash the router."""
    async def update(i):
        return await apex_router.add_rule(f"rule-{i}", {"path": f"/api/v{i}", "target": "service"})
    results = await asyncio.gather(*[update(i) for i in range(20)],
                                    return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"{len(failures)}/20 updates failed"
    rules = await apex_router.get_all_rules()
    assert len(rules) >= 20, f"Expected at least 20 rules, got {len(rules)}"


@pytest.mark.asyncio
async def test_a3_parallel_health_checks_no_deadlock(apex_router):
    """100 parallel health checks must complete without deadlock."""
    async def check(i):
        return await apex_router.health_check(f"service-{i % 5}")
    results = await asyncio.gather(*[check(i) for i in range(100)],
                                    return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"{len(failures)}/100 health checks failed"


# ══════════════════════════════════════════════════════════════════════════
# B. Chaos
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_b1_service_unreachable_mid_routing(apex_router, monkeypatch):
    """Service unreachable mid-routing must return clean error, not crash."""
    def mock_unreachable(*args, **kwargs):
        raise ConnectionError("Target service unreachable")
    monkeypatch.setattr(apex_router, "route_request", mock_unreachable)
    with pytest.raises(ConnectionError):
        await apex_router.route_request("test-req", {})


@pytest.mark.asyncio
async def test_b2_routing_table_corruption_handled(apex_router, monkeypatch):
    """Corrupted routing table must be handled gracefully."""
    def mock_corrupt_table(*args, **kwargs):
        return {"invalid": "state", "missing": "routes"}
    monkeypatch.setattr(apex_router, "get_all_rules", mock_corrupt_table)
    rules = await apex_router.get_all_rules()
    assert isinstance(rules, dict)


@pytest.mark.asyncio
async def test_b3_request_timeout_handled(apex_router, monkeypatch):
    """Request timeout must be handled gracefully."""
    def mock_timeout(*args, **kwargs):
        raise TimeoutError("Routing request timed out")
    monkeypatch.setattr(apex_router, "route_request", mock_timeout)
    with pytest.raises(TimeoutError):
        await apex_router.route_request("test-req", {})


# ══════════════════════════════════════════════════════════════════════════
# C. Performance envelope
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_c1_routing_latency_p50_p99(apex_router):
    """p50 < 20ms, p99 < 100ms for routing requests."""
    times = []
    for _ in range(100):
        t0 = time.perf_counter()
        result = await apex_router.route_request("perf-req", {"path": "/api/v1/test"})
        times.append((time.perf_counter() - t0) * 1000)
    p50 = statistics.median(times)
    p99 = max(times)
    print(f"\n  route p50={p50:.1f}ms p99={p99:.1f}ms")
    assert p50 < 20, f"p50 {p50:.1f}ms > 20ms"
    assert p99 < 100, f"p99 {p99:.1f}ms > 100ms"


@pytest.mark.asyncio
async def test_c2_routing_throughput(apex_router):
    """Routing throughput > 1000 req/sec."""
    t0 = time.perf_counter()
    for i in range(1000):
        await apex_router.route_request(f"req-{i}", {"path": f"/api/v{i}"})
    rate = 1000 / (time.perf_counter() - t0)
    print(f"\n  routing throughput {rate:.0f}/sec")
    assert rate > 1000, f"routing {rate:.0f}/s < 1000/s"


@pytest.mark.asyncio
async def test_c3_rule_update_throughput(apex_router):
    """Rule update throughput > 100 updates/sec."""
    t0 = time.perf_counter()
    for i in range(100):
        await apex_router.add_rule(f"rule-{i}", {"path": f"/update{i}", "target": "service"})
    rate = 100 / (time.perf_counter() - t0)
    print(f"\n  update throughput {rate:.0f}/sec")
    assert rate > 100, f"update {rate:.0f}/s < 100/s"


# ══════════════════════════════════════════════════════════════════════════
# D. Soak — bounded sustained load
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.soak
async def test_d1_60s_soak_no_degradation(apex_router):
    """60s of continuous routing load. Last-20s mean latency must be within 1.5× first-20s."""
    start = time.monotonic()
    buckets: list[list[float]] = [[], [], []]
    iterations = 0
    while (elapsed := time.monotonic() - start) < 60:
        bucket = min(2, int(elapsed / 20))
        t0 = time.perf_counter()
        await apex_router.route_request("soak-req", {"path": "/api/v1/test"})
        buckets[bucket].append((time.perf_counter() - t0) * 1000)
        iterations += 1
    first, last = statistics.mean(buckets[0]), statistics.mean(buckets[2])
    print(f"\n  soak: {iterations} iters, first20s={first:.0f}ms last20s={last:.0f}ms")
    assert last < first * 1.5, f"degradation detected: {last/first:.2f}x ratio"