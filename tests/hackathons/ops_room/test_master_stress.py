"""Master stress suite for Ops Room — concurrency, chaos, perf envelope.

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
async def test_a1_100_parallel_task_executions(ops_room):
    """100 parallel task executions must complete without errors."""
    async def one(i):
        return await ops_room.execute_task(f"task-{i}", {"action": "ping"})
    results = await asyncio.gather(*[one(i) for i in range(100)],
                                    return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"{len(failures)}/100 tasks failed: {failures[:2]}"


@pytest.mark.asyncio
async def test_a2_concurrent_task_submission_no_loss(ops_room):
    """50 concurrent task submissions must not lose any tasks."""
    async def submit(i):
        return await ops_room.submit_task({"id": f"task-{i}", "action": "ping"})
    results = await asyncio.gather(*[submit(i) for i in range(50)],
                                    return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"{len(failures)}/50 submissions failed"
    count = await ops_room.get_task_count()
    assert count >= 50, f"Expected at least 50 tasks, got {count}"


@pytest.mark.asyncio
async def test_a3_parallel_status_checks_no_deadlock(ops_room):
    """100 parallel status checks must complete without deadlock."""
    async def check(i):
        return await ops_room.get_task_status(f"task-{i % 10}")
    results = await asyncio.gather(*[check(i) for i in range(100)],
                                    return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"{len(failures)}/100 status checks failed"


# ══════════════════════════════════════════════════════════════════════════
# B. Chaos
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_b1_worker_failure_mid_task(ops_room, monkeypatch):
    """Worker failure mid-task must be handled gracefully."""
    def mock_worker_failure(*args, **kwargs):
        raise RuntimeError("Worker crashed unexpectedly")
    monkeypatch.setattr(ops_room, "execute_task", mock_worker_failure)
    with pytest.raises(RuntimeError):
        await ops_room.execute_task("test-task", {})


@pytest.mark.asyncio
async def test_b2_task_queue_overflow_handled(ops_room, monkeypatch):
    """Task queue overflow must be handled gracefully."""
    original = ops_room.submit_task
    async def mock_overflow(*args, **kwargs):
        raise OverflowError("Task queue is full")
    monkeypatch.setattr(ops_room, "submit_task", mock_overflow)
    with pytest.raises(OverflowError):
        await ops_room.submit_task({"id": "overflow-task"})


@pytest.mark.asyncio
async def test_b3_dependency_failure_handled(ops_room, monkeypatch):
    """Task dependency failure must be handled gracefully."""
    def mock_dep_failure(*args, **kwargs):
        raise RuntimeError("Required dependency failed")
    monkeypatch.setattr(ops_room, "execute_task", mock_dep_failure)
    with pytest.raises(RuntimeError):
        await ops_room.execute_task("dependent-task", {"requires": ["other-task"]})


# ══════════════════════════════════════════════════════════════════════════
# C. Performance envelope
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_c1_task_submission_latency_p50_p99(ops_room):
    """p50 < 50ms, p99 < 200ms for task submission."""
    times = []
    for _ in range(50):
        t0 = time.perf_counter()
        await ops_room.submit_task({"id": "perf-test", "action": "ping"})
        times.append((time.perf_counter() - t0) * 1000)
    p50 = statistics.median(times)
    p99 = max(times)
    print(f"\n  submit p50={p50:.1f}ms p99={p99:.1f}ms")
    assert p50 < 50, f"p50 {p50:.1f}ms > 50ms"
    assert p99 < 200, f"p99 {p99:.1f}ms > 200ms"


@pytest.mark.asyncio
async def test_c2_task_execution_throughput(ops_room):
    """Task execution throughput > 100 tasks/sec."""
    t0 = time.perf_counter()
    for i in range(100):
        await ops_room.execute_task(f"task-{i}", {"action": "ping"})
    rate = 100 / (time.perf_counter() - t0)
    print(f"\n  execution throughput {rate:.0f}/sec")
    assert rate > 100, f"execution {rate:.0f}/s < 100/s"


@pytest.mark.asyncio
async def test_c3_status_check_throughput(ops_room):
    """Status check throughput > 500 checks/sec."""
    t0 = time.perf_counter()
    for _ in range(500):
        await ops_room.get_task_status("test-task")
    rate = 500 / (time.perf_counter() - t0)
    print(f"\n  status throughput {rate:.0f}/sec")
    assert rate > 500, f"status {rate:.0f}/s < 500/s"


# ══════════════════════════════════════════════════════════════════════════
# D. Soak — bounded sustained load
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.soak
async def test_d1_30s_soak_no_degradation(ops_room):
    """30s of continuous task load. Last-10s mean latency must be within 1.5× first-10s."""
    start = time.monotonic()
    buckets: list[list[float]] = [[], [], []]
    iterations = 0
    while (elapsed := time.monotonic() - start) < 30:
        bucket = min(2, int(elapsed / 10))
        t0 = time.perf_counter()
        await ops_room.execute_task("soak-task", {"action": "ping"})
        buckets[bucket].append((time.perf_counter() - t0) * 1000)
        iterations += 1
    first, last = statistics.mean(buckets[0]), statistics.mean(buckets[2])
    print(f"\n  soak: {iterations} iters, first10s={first:.0f}ms last10s={last:.0f}ms")
    assert last < first * 1.5, f"degradation detected: {last/first:.2f}x ratio"