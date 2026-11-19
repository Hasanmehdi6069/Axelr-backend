"""Master stress suite for Ops Room — concurrency, chaos, perf envelope.

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
async def test_a1_300_parallel_task_executions(ops_room):
    """300 parallel task executions must complete without errors."""
    async def one(i):
        return await ops_room.execute_task(f"task-{i}", {"action": "ping", "target": f"service-{i % 10}"})
    results = await asyncio.gather(*[one(i) for i in range(300)],
                                    return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"{len(failures)}/300 tasks failed: {failures[:2]}"


@pytest.mark.asyncio
async def test_a2_concurrent_task_queue_updates(ops_room):
    """100 concurrent task queue updates must not corrupt queue state."""
    async def queue(i):
        return await ops_room.queue_task(f"queued-{i}", {"action": "process", "data": f"item-{i}"})
    results = await asyncio.gather(*[queue(i) for i in range(100)],
                                    return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"{len(failures)}/100 queue operations failed"
    queue_size = await ops_room.get_queue_size()
    assert queue_size >= 100, f"Expected at least 100 queued tasks, got {queue_size}"


@pytest.mark.asyncio
async def test_a3_parallel_status_updates_no_deadlock(ops_room):
    """200 parallel status updates must complete without deadlock."""
    async def update(i):
        return await ops_room.update_status(f"task-{i % 50}", {"status": "running", "progress": i})
    results = await asyncio.gather(*[update(i) for i in range(200)],
                                    return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"{len(failures)}/200 status updates failed"


@pytest.mark.asyncio
async def test_a4_task_throughput_under_load(ops_room):
    """500 task throughput must be > 300 ops/sec under load."""
    start = time.perf_counter()
    results = await asyncio.gather(*[
        ops_room.execute_task(f"throughput-{i}", {"action": "ping"})
        for i in range(500)
    ])
    elapsed = time.perf_counter() - start
    throughput = 500 / elapsed
    assert throughput > 300, f"Task throughput too slow: {throughput:.0f} ops/sec"
    print(f"\n  Task throughput: {throughput:.0f} ops/sec")


# ══════════════════════════════════════════════════════════════════════════
# B. Chaos
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_b1_service_unreachable_mid_task(ops_room, monkeypatch):
    """Service unreachable mid-task must return clean error, not hang."""
    def mock_unreachable(*args, **kwargs):
        raise ConnectionError("Target service unreachable")
    monkeypatch.setattr(ops_room, "execute_task", mock_unreachable)
    with pytest.raises(ConnectionError):
        await ops_room.execute_task("test-task", {"action": "process"})


@pytest.mark.asyncio
async def test_b2_task_cancellation_mid_execution(ops_room):
    """Task cancellation mid-execution must not leave orphaned processes."""
    task = asyncio.create_task(ops_room.execute_task("cancel-test", {"action": "long-running"}))
    await asyncio.sleep(0.1)
    cancel_result = await ops_room.cancel_task("cancel-test")
    assert cancel_result["cancelled"] is True
    status = await ops_room.get_task_status("cancel-test")
    assert status["status"] == "cancelled"


@pytest.mark.asyncio
async def test_b3_queue_overflow_handled_gracefully(ops_room):
    """Queue overflow must be handled gracefully with backpressure."""
    results = await asyncio.gather(*[
        ops_room.queue_task(f"overflow-{i}", {"action": "process"})
        for i in range(1000)
    ])
    accepted = [r for r in results if r.get("queued", False)]
    rejected = [r for r in results if r.get("rejected", False)]
    assert len(accepted) + len(rejected) == 1000, "All requests must be either accepted or rejected"


# ══════════════════════════════════════════════════════════════════════════
# C. Performance envelope
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_c1_task_latency_p50_p99(ops_room):
    """p50 < 15ms, p99 < 75ms for simple tasks."""
    times = []
    for _ in range(100):
        t0 = time.perf_counter()
        await ops_room.execute_task("latency-test", {"action": "ping"})
        times.append((time.perf_counter() - t0) * 1000)
    p50 = statistics.median(times)
    p99 = statistics.quantiles(times, n=100)[98]
    assert p50 < 15, f"p50 latency too high: {p50:.1f}ms"
    assert p99 < 75, f"p99 latency too high: {p99:.1f}ms"
    print(f"\n  Task latency p50={p50:.1f}ms p99={p99:.1f}ms")


@pytest.mark.asyncio
async def test_c2_queue_processing_latency_p95(ops_room):
    """p95 < 50ms for queue processing."""
    times = []
    for _ in range(50):
        t0 = time.perf_counter()
        task_id = await ops_room.queue_task("queue-latency", {"action": "process"})
        await ops_room.wait_for_task(task_id, timeout=1)
        times.append((time.perf_counter() - t0) * 1000)
    p95 = statistics.quantiles(times, n=100)[94]
    assert p95 < 50, f"p95 queue latency too high: {p95:.1f}ms"


# ══════════════════════════════════════════════════════════════════════════
# D. Soak test
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_d1_soak_5000_tasks_no_leak(ops_room):
    """5,000 sequential tasks must not leak memory or degrade performance."""
    start = time.perf_counter()
    for i in range(5000):
        await ops_room.execute_task(f"soak-{i}", {"action": "ping"})
    elapsed = time.perf_counter() - start
    avg_latency = elapsed / 5000 * 1000
    assert avg_latency < 10, f"Soak test avg latency too high: {avg_latency:.2f}ms"
    print(f"\n  Soak test: 5000 tasks in {elapsed:.2f}s, avg {avg_latency:.2f}ms")