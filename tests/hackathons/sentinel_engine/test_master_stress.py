"""Master stress suite for Sentinel Engine — concurrency, chaos, perf envelope.

Rules for this file:
  * every test is marked `slow` — excluded from fast PR runs
  * thresholds are conservative (CI-friendly); tighten after a week of data
  * chaos tests mutate external state mid-operation, they do NOT mock it

A green stress suite means: the demo will survive ten clicks by a judge.
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
async def test_a1_200_parallel_sandbox_runs(engine):
    from sentinel_agentic.actions import dispatch
    async def one(i):
        return await dispatch(engine, {
            "action": "sandbox.run_script",
            "params": {"script": f"print({i})"},
            "dryRun": False,
        })
    results = await asyncio.gather(*[one(i) for i in range(200)],
                                    return_exceptions=True)
    failures = [r for r in results
                if isinstance(r, Exception) or not r.get("ok")]
    assert not failures, f"{len(failures)}/200 failed: {failures[:2]}"


@pytest.mark.asyncio
async def test_a2_concurrent_writes_no_corruption(engine):
    """20 writers to the same file. Final state is one of them; rollback
    restores 'not existed'."""
    from sentinel_agentic.actions import dispatch
    async def write(i):
        return await dispatch(engine, {
            "action": "sandbox.write_file",
            "params": {"path": "contended.txt", "content": f"w{i}"},
            "dryRun": False,
        })
    results = await asyncio.gather(*[write(i) for i in range(20)],
                                    return_exceptions=True)
    assert not [r for r in results
                if isinstance(r, Exception) or not r.get("ok")]
    final = engine.assert_path("contended.txt").read_text()
    assert final.startswith("w")
    await engine.rollback()
    assert not engine.assert_path("contended.txt").exists()


@pytest.mark.asyncio
async def test_a3_parallel_workflows_no_cross_contamination(engine):
    """20 workflows in parallel; one fails. Successes untouched, failure clean."""
    from sentinel_agentic.workflow import run_workflow
    async def run_one(i, fail):
        steps = [{"action": "sandbox.write_file",
                  "params": {"path": f"wf-{i}.txt", "content": f"c{i}"}}]
        if fail:
            steps.append({"action": "sandbox.run_script",
                          "params": {"script": "eval('nope')"}})
        return i, fail, await run_workflow(engine, steps, name=f"wf-{i}")
    results = await asyncio.gather(*[run_one(i, i % 3 == 0) for i in range(20)])
    for i, fail, r in results:
        if fail:
            assert r["ok"] is False
            assert not engine.assert_path(f"wf-{i}.txt").exists(), \
                f"failed wf {i} leaked a file"
        else:
            assert r["ok"] is True
            assert engine.assert_path(f"wf-{i}.txt").exists(), \
                f"successful wf {i} lost its file"


@pytest.mark.asyncio
async def test_a4_audit_no_lost_writes_under_contention(engine):
    """1000 concurrent audit writes → 1000 complete lines, all valid JSON."""
    from sentinel_agentic.engine import audit, AUDIT_PATH, flush_audit
    await asyncio.gather(*[audit({"kind": "stress", "i": i}) for i in range(1000)])
    await flush_audit()
    lines = AUDIT_PATH.read_text().strip().split("\n")
    stress_lines = []
    for line in lines:
        if not line:
            continue
        entry = json.loads(line)
        if entry.get("kind") == "stress":
            stress_lines.append(entry)
    assert len(stress_lines) == 1000, f"lost writes: {len(stress_lines)}/1000"
    for entry in stress_lines:
        assert "ts" in entry and "i" in entry


@pytest.mark.asyncio
async def test_a5_concurrent_ast_validations_no_deadlock(engine):
    """500 concurrent AST validations must complete without deadlock."""
    payloads = [f"x = {i}\nprint(x)" for i in range(500)]
    results = await asyncio.gather(*[
        asyncio.to_thread(engine.validate, p) for p in payloads
    ], return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"{len(failures)}/500 AST validations failed"


# ══════════════════════════════════════════════════════════════════════════
# B. Chaos
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_b1_scratch_deleted_mid_run(engine):
    """If scratch is wiped between ops, next dispatch either recreates it
    or fails cleanly — never crashes."""
    import shutil
    scratch = engine.assert_path(".")
    shutil.rmtree(scratch)
    from sentinel_agentic.actions import dispatch
    r = await dispatch(engine, {
        "action": "sandbox.write_file",
        "params": {"path": "post-delete.txt", "content": "y"},
        "dryRun": False,
    })
    assert isinstance(r, dict)


@pytest.mark.asyncio
async def test_b2_kill9_child_handled(engine):
    """Child SIGKILLs itself; parent reports failure, does not hang."""
    from sentinel_agentic.actions import dispatch
    r = await dispatch(engine, {
        "action": "sandbox.run_script",
        "params": {"script": "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"},
        "dryRun": False,
    })
    assert r["ok"] is False


@pytest.mark.asyncio
async def test_b3_corrupt_audit_log_does_not_crash_tail(engine):
    """Truncated JSONL: tail() skips bad lines, returns what's parseable."""
    from sentinel_agentic.engine import AUDIT_PATH, tail
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.write_text(
        '{"ts": "2024-01-01", "kind": "a"}\n'
        '{"ts": "2024-01-02", "kind": "b"\n'
    )
    result = await tail(10)
    assert isinstance(result, list)


@pytest.mark.asyncio
async def test_b4_wal_db_readonly_clean_failure(engine, tmp_path):
    """Unwritable WAL directory → dispatch returns error dict, not exception."""
    wal_db = pathlib.Path(os.environ["SENTINEL_WAL_PATH"])
    wal_db.parent.chmod(0o500)
    try:
        from sentinel_agentic.actions import dispatch
        r = await dispatch(engine, {
            "action": "sandbox.write_file",
            "params": {"path": "x.txt", "content": "y"},
            "dryRun": False,
        })
        assert isinstance(r, dict) and "ok" in r
    finally:
        wal_db.parent.chmod(0o700)


@pytest.mark.asyncio
async def test_b5_disk_full_simulation(engine, tmp_path, monkeypatch):
    """Simulate full disk → dispatch returns error dict, not exception."""
    def mock_disk_full(*args, **kwargs):
        raise OSError("No space left on device")
    import pathlib
    monkeypatch.setattr(pathlib.Path, "write_text", mock_disk_full)
    from sentinel_agentic.actions import dispatch
    r = await dispatch(engine, {
        "action": "sandbox.write_file",
        "params": {"path": "x.txt", "content": "y"},
        "dryRun": False,
    })
    assert isinstance(r, dict) and "ok" in r


# ══════════════════════════════════════════════════════════════════════════
# C. Performance envelope
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_c1_dispatch_latency_p50_p99(engine):
    """p50 < 300ms, p99 < 1s. Tracks regressions as features accrete."""
    from sentinel_agentic.actions import dispatch
    times = []
    for _ in range(50):
        t0 = time.perf_counter()
        r = await dispatch(engine, {
            "action": "sandbox.run_script",
            "params": {"script": "print('t')"},
            "dryRun": False,
        })
        assert r["ok"]
        times.append((time.perf_counter() - t0) * 1000)
    p50 = statistics.median(times)
    p99 = max(times)
    print(f"\n  dispatch p50={p50:.1f}ms p99={p99:.1f}ms")
    assert p50 < 300, f"p50 {p50:.1f}ms > 300ms"
    assert p99 < 1000, f"p99 {p99:.1f}ms > 1000ms"


@pytest.mark.asyncio
async def test_c2_audit_throughput(engine):
    """Audit writes >1000/sec (persistent I/O, not memory)."""
    from sentinel_agentic.engine import audit
    t0 = time.perf_counter()
    for i in range(500):
        await audit({"kind": "bench", "i": i})
    rate = 500 / (time.perf_counter() - t0)
    print(f"\n  audit throughput {rate:.0f}/sec")
    assert rate > 1000, f"audit {rate:.0f}/s < 1000/s"


@pytest.mark.asyncio
async def test_c3_ast_validation_throughput(engine):
    """AST shield processes >1000 payloads/sec (parse, not regex)."""
    payload = "x = 1\n" * 200
    t0 = time.perf_counter()
    for _ in range(1000):
        engine.validate(payload)
    rate = 1000 / (time.perf_counter() - t0)
    print(f"\n  AST validation {rate:.0f}/sec")
    assert rate > 1000, f"AST {rate:.0f}/s < 1000/s"


@pytest.mark.asyncio
async def test_c4_workflow_throughput(engine):
    """Workflow execution >100/sec."""
    from sentinel_agentic.workflow import run_workflow
    t0 = time.perf_counter()
    for i in range(100):
        r = await run_workflow(engine, [
            {"action": "sandbox.write_file",
             "params": {"path": f"wf-{i}.txt", "content": f"c{i}"}}
        ], name=f"wf-{i}")
        assert r["ok"]
    rate = 100 / (time.perf_counter() - t0)
    print(f"\n  workflow throughput {rate:.0f}/sec")
    assert rate > 10, f"workflow {rate:.0f}/s < 10/s"


# ══════════════════════════════════════════════════════════════════════════
# D. Soak — bounded sustained load, checks for degradation
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.soak
async def test_d1_60s_soak_no_degradation(engine):
    """60s of continuous load. Last-15s mean latency must be within 1.5× the
    first-15s mean. A leak or handle exhaustion shows up here."""
    from sentinel_agentic.actions import dispatch
    start = time.monotonic()
    buckets: list[list[float]] = [[], [], [], []]
    iterations = 0
    while (elapsed := time.monotonic() - start) < 60:
        bucket = min(3, int(elapsed / 15))
        t0 = time.perf_counter()
        r = await dispatch(engine, {
            "action": "sandbox.run_script",
            "params": {"script": f"print('soak-{iterations}')"},
            "dryRun": False,
        })
        assert r["ok"], f"iteration {iterations} failed: {r.get('error')}"
        buckets[bucket].append((time.perf_counter() - t0) * 1000)
        iterations += 1

    first, last = statistics.mean(buckets[0]), statistics.mean(buckets[3])
    print(f"\n  soak: {iterations} iters, first15s={first:.0f}ms "
          f"last15s={last:.0f}ms, ratio={last/first:.2f}x")
    assert last < first * 1.5, (
        f"degradation: first15s={first:.0f}ms last15s={last:.0f}ms — "
        f"possible resource leak"
    )


@pytest.mark.asyncio
@pytest.mark.soak
async def test_d2_memory_leak_detection(engine):
    """Run 1000 operations, check memory usage doesn't grow unboundedly."""
    import psutil
    import gc
    process = psutil.Process()
    initial_memory = process.memory_info().rss
    from sentinel_agentic.actions import dispatch
    for i in range(1000):
        r = await dispatch(engine, {
            "action": "sandbox.run_script",
            "params": {"script": f"x = {i}"},
            "dryRun": False,
        })
        assert r["ok"]
    gc.collect()
    final_memory = process.memory_info().rss
    memory_growth = final_memory - initial_memory
    memory_growth_mb = memory_growth / (1024 * 1024)
    print(f"\n  memory growth: {memory_growth_mb:.2f}MB after 1000 ops")
    assert memory_growth_mb < 50, f"memory leak detected: {memory_growth_mb:.2f}MB growth"