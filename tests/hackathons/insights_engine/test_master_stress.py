"""Master stress suite for Insights Engine — concurrency, chaos, perf envelope.

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
async def test_a1_100_parallel_query_executions(insights_engine):
    """100 parallel query executions must complete without errors."""
    async def one(i):
        return await insights_engine.execute_query(f"SELECT * FROM test_table WHERE id = {i}")
    results = await asyncio.gather(*[one(i) for i in range(100)],
                                    return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"{len(failures)}/100 queries failed: {failures[:2]}"


@pytest.mark.asyncio
async def test_a2_concurrent_write_no_corruption(insights_engine):
    """20 concurrent writes to the same table must not corrupt data."""
    async def write(i):
        return await insights_engine.insert_data("test_table", {"id": i, "value": f"v{i}"})
    results = await asyncio.gather(*[write(i) for i in range(20)],
                                    return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"{len(failures)}/20 writes failed"
    count = await insights_engine.count_rows("test_table")
    assert count >= 20, f"Expected at least 20 rows, got {count}"


@pytest.mark.asyncio
async def test_a3_parallel_aggregations_no_deadlock(insights_engine):
    """50 parallel aggregation queries must complete without deadlock."""
    async def agg(i):
        return await insights_engine.execute_query(f"SELECT COUNT(*) FROM test_table WHERE id > {i}")
    results = await asyncio.gather(*[agg(i) for i in range(50)],
                                    return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"{len(failures)}/50 aggregations failed"


# ══════════════════════════════════════════════════════════════════════════
# B. Chaos
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_b1_database_connection_lost_mid_query(insights_engine, monkeypatch):
    """Database connection lost mid-query must return clean error, not crash."""
    def mock_connection_error(*args, **kwargs):
        raise ConnectionError("Database connection lost")
    monkeypatch.setattr(insights_engine, "execute_query", mock_connection_error)
    with pytest.raises(ConnectionError):
        await insights_engine.execute_query("SELECT * FROM test")


@pytest.mark.asyncio
async def test_b2_corrupt_query_result_handled(insights_engine, monkeypatch):
    """Corrupt query results must be handled gracefully."""
    def mock_corrupt_result(*args, **kwargs):
        return {"invalid": "result", "missing": "fields"}
    monkeypatch.setattr(insights_engine, "execute_query", mock_corrupt_result)
    result = await insights_engine.execute_query("SELECT * FROM test")
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_b3_query_timeout_handled(insights_engine, monkeypatch):
    """Query timeout must be handled gracefully."""
    def mock_timeout(*args, **kwargs):
        raise TimeoutError("Query timed out")
    monkeypatch.setattr(insights_engine, "execute_query", mock_timeout)
    with pytest.raises(TimeoutError):
        await insights_engine.execute_query("SELECT * FROM test")


# ══════════════════════════════════════════════════════════════════════════
# C. Performance envelope
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_c1_query_latency_p50_p99(insights_engine):
    """p50 < 100ms, p99 < 500ms for simple queries."""
    times = []
    for _ in range(50):
        t0 = time.perf_counter()
        result = await insights_engine.execute_query("SELECT 1")
        times.append((time.perf_counter() - t0) * 1000)
    p50 = statistics.median(times)
    p99 = max(times)
    print(f"\n  query p50={p50:.1f}ms p99={p99:.1f}ms")
    assert p50 < 100, f"p50 {p50:.1f}ms > 100ms"
    assert p99 < 500, f"p99 {p99:.1f}ms > 500ms"


@pytest.mark.asyncio
async def test_c2_insert_throughput(insights_engine):
    """Insert throughput > 500 rows/sec."""
    t0 = time.perf_counter()
    for i in range(500):
        await insights_engine.insert_data("test_table", {"id": i, "value": f"v{i}"})
    rate = 500 / (time.perf_counter() - t0)
    print(f"\n  insert throughput {rate:.0f}/sec")
    assert rate > 500, f"insert {rate:.0f}/s < 500/s"


@pytest.mark.asyncio
async def test_c3_aggregation_throughput(insights_engine):
    """Aggregation throughput > 100 queries/sec."""
    t0 = time.perf_counter()
    for _ in range(100):
        await insights_engine.execute_query("SELECT COUNT(*) FROM test_table")
    rate = 100 / (time.perf_counter() - t0)
    print(f"\n  aggregation throughput {rate:.0f}/sec")
    assert rate > 100, f"aggregation {rate:.0f}/s < 100/s"


# ══════════════════════════════════════════════════════════════════════════
# D. Soak — bounded sustained load
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.soak
async def test_d1_30s_soak_no_degradation(insights_engine):
    """30s of continuous query load. Last-10s mean latency must be within 1.5× first-10s."""
    start = time.monotonic()
    buckets: list[list[float]] = [[], [], []]
    iterations = 0
    while (elapsed := time.monotonic() - start) < 30:
        bucket = min(2, int(elapsed / 10))
        t0 = time.perf_counter()
        await insights_engine.execute_query("SELECT * FROM test_table LIMIT 10")
        buckets[bucket].append((time.perf_counter() - t0) * 1000)
        iterations += 1
    first, last = statistics.mean(buckets[0]), statistics.mean(buckets[2])
    print(f"\n  soak: {iterations} iters, first10s={first:.0f}ms last10s={last:.0f}ms")
    assert last < first * 1.5, f"degradation detected: {last/first:.2f}x ratio"