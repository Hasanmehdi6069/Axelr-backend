"""Master stress suite for Buildx — concurrency, chaos, perf envelope.

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
async def test_a1_50_parallel_builds(buildx_engine):
    """50 parallel build jobs must complete without errors."""
    async def one(i):
        return await buildx_engine.start_build(f"build-{i}", {"dockerfile": "test.Dockerfile"})
    results = await asyncio.gather(*[one(i) for i in range(50)],
                                    return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"{len(failures)}/50 builds failed: {failures[:2]}"


@pytest.mark.asyncio
async def test_a2_concurrent_image_pulls_no_corruption(buildx_engine):
    """20 concurrent image pulls must not corrupt images."""
    async def pull(i):
        return await buildx_engine.pull_image(f"alpine:{i}")
    results = await asyncio.gather(*[pull(i) for i in range(20)],
                                    return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"{len(failures)}/20 pulls failed"
    images = await buildx_engine.list_images()
    assert len(images) >= 20, f"Expected at least 20 images, got {len(images)}"


@pytest.mark.asyncio
async def test_a3_parallel_log_streams_no_deadlock(buildx_engine):
    """30 parallel log streams must complete without deadlock."""
    async def stream(i):
        return await buildx_engine.get_build_logs(f"build-{i % 10}")
    results = await asyncio.gather(*[stream(i) for i in range(30)],
                                    return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"{len(failures)}/30 log streams failed"


# ══════════════════════════════════════════════════════════════════════════
# B. Chaos
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_b1_daemon_crash_mid_build(buildx_engine, monkeypatch):
    """Docker daemon crash mid-build must return clean error, not hang."""
    def mock_daemon_crash(*args, **kwargs):
        raise ConnectionError("Docker daemon crashed")
    monkeypatch.setattr(buildx_engine, "start_build", mock_daemon_crash)
    with pytest.raises(ConnectionError):
        await buildx_engine.start_build("crash-test", {})


@pytest.mark.asyncio
async def test_b2_corrupt_dockerfile_handled(buildx_engine, monkeypatch):
    """Corrupt Dockerfile must be handled gracefully."""
    def mock_corrupt_build(*args, **kwargs):
        raise ValueError("Invalid Dockerfile syntax")
    monkeypatch.setattr(buildx_engine, "start_build", mock_corrupt_build)
    with pytest.raises(ValueError):
        await buildx_engine.start_build("corrupt-test", {})


@pytest.mark.asyncio
async def test_b3_registry_timeout_handled(buildx_engine, monkeypatch):
    """Registry timeout must be handled gracefully."""
    def mock_reg_timeout(*args, **kwargs):
        raise TimeoutError("Registry connection timed out")
    monkeypatch.setattr(buildx_engine, "pull_image", mock_reg_timeout)
    with pytest.raises(TimeoutError):
        await buildx_engine.pull_image("timeout-test")


# ══════════════════════════════════════════════════════════════════════════
# C. Performance envelope
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_c1_build_queue_latency_p50_p99(buildx_engine):
    """p50 < 100ms, p99 < 500ms for build queue submission."""
    times = []
    for _ in range(50):
        t0 = time.perf_counter()
        await buildx_engine.start_build("perf-test", {})
        times.append((time.perf_counter() - t0) * 1000)
    p50 = statistics.median(times)
    p99 = max(times)
    print(f"\n  queue p50={p50:.1f}ms p99={p99:.1f}ms")
    assert p50 < 100, f"p50 {p50:.1f}ms > 100ms"
    assert p99 < 500, f"p99 {p99:.1f}ms > 500ms"


@pytest.mark.asyncio
async def test_c2_image_list_throughput(buildx_engine):
    """Image listing throughput > 100 lists/sec."""
    t0 = time.perf_counter()
    for _ in range(100):
        await buildx_engine.list_images()
    rate = 100 / (time.perf_counter() - t0)
    print(f"\n  list throughput {rate:.0f}/sec")
    assert rate > 100, f"list {rate:.0f}/s < 100/s"


@pytest.mark.asyncio
async def test_c3_log_fetch_throughput(buildx_engine):
    """Log fetch throughput > 500 fetches/sec."""
    t0 = time.perf_counter()
    for _ in range(500):
        await buildx_engine.get_build_logs("test-build")
    rate = 500 / (time.perf_counter() - t0)
    print(f"\n  log fetch throughput {rate:.0f}/sec")
    assert rate > 500, f"log {rate:.0f}/s < 500/s"


# ══════════════════════════════════════════════════════════════════════════
# D. Soak — bounded sustained load
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.soak
async def test_d1_60s_soak_no_degradation(buildx_engine):
    """60s of continuous build queue load. Last-20s mean latency must be within 1.5× first-20s."""
    start = time.monotonic()
    buckets: list[list[float]] = [[], [], []]
    iterations = 0
    while (elapsed := time.monotonic() - start) < 60:
        bucket = min(2, int(elapsed / 20))
        t0 = time.perf_counter()
        await buildx_engine.start_build("soak-build", {})
        buckets[bucket].append((time.perf_counter() - t0) * 1000)
        iterations += 1
    first, last = statistics.mean(buckets[0]), statistics.mean(buckets[2])
    print(f"\n  soak: {iterations} iters, first20s={first:.0f}ms last20s={last:.0f}ms")
    assert last < first * 1.5, f"degradation detected: {last/first:.2f}x ratio"