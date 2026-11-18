"""Master stress suite — concurrency, chaos, perf envelope.

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
async def test_a1_100_parallel_sandbox_runs(engine):
    from sentinel_agentic.actions import dispatch
    async def one(i):
        return await dispatch(engine, {
            "action": "sandbox.run_script",
            "params": {"script": f"print({i})"},
            "dryRun": False,
        })
    results = await asyncio.gather(*[one(i) for i in range(100)],
                                    return_exceptions=True)
    failures = [r for r in results
                if isinstance(r, Exception) or not r.get("ok")]
    assert not failures, f"{len(failures)}/100 failed: {failures[:2]}"


@pytest.mark.asyncio
async def test_a2_concurrent_writes_no_corruption(engine):
    """10 writers to the same file. Final state is one of them; rollback
    restores 'not existed'."""
    from sentinel_agentic.actions import dispatch
    async def write(i):
        return await dispatch(engine, {
            "action": "sandbox.write_file",
            "params": {"path": "contended.txt", "content": f"w{i}"},
            "dryRun": False,
        })
    results = await asyncio.gather(*[write(i) for i in range(10)],
                                    return_exceptions=True)
    assert not [r for r in results
                if isinstance(r, Exception) or not r.get("ok")]
    final = engine.assert_path("contended.txt").read_text()
    assert final.startswith("w")
    await engine.rollback()
    assert not engine.assert_path("contended.txt").exists()


@pytest.mark.asyncio
async def test_a3_parallel_workflows_no_cross_contamination(engine):
    """10 workflows in parallel; one fails. Successes untouched, failure clean."""
    from sentinel_agentic.workflow import run_workflow
    async def run_one(i, fail):
        steps = [{"action": "sandbox.write_file",
                  "params": {"path": f"wf-{i}.txt", "content": f"c{i}"}}]
        if fail:
            steps.append({"action": "sandbox.run_script",
                          "params": {"script": "eval('nope')"}})
        return i, fail, await run_workflow(engine, steps, name=f"wf-{i}")
    results = await asyncio.gather(*[run_one(i, i % 3 == 0) for i in range(10)])
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
    """500 concurrent audit writes → 500 complete lines, all valid JSON."""
    from sentinel_agentic.engine import audit, AUDIT_PATH, flush_audit
    await asyncio.gather(*[audit({"kind": "stress", "i": i}) for i in range(500)])
    await flush_audit()
    lines = AUDIT_PATH.read_text().strip().split("\n")
    stress_lines = []
    for line in lines:
        if not line:
            continue
        entry = json.loads(line)
        if entry.get("kind") == "stress":
            stress_lines.append(entry)
    assert len(stress_lines) == 500, f"lost writes: {len(stress_lines)}/500"
    for entry in stress_lines:
        assert "ts" in entry and "i" in entry


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
        '{"ts": "2024-01-02", "kind": "b"\n'   # truncated
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


# ══════════════════════════════════════════════════════════════════════════
# C. Performance envelope
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_c1_dispatch_latency_p50_p99(engine):
    """p50 < 500ms, p99 < 2s. Tracks regressions as features accrete."""
    from sentinel_agentic.actions import dispatch
    times = []
    for _ in range(20):
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
    assert p50 < 500, f"p50 {p50:.1f}ms > 500ms"
    assert p99 < 2000, f"p99 {p99:.1f}ms > 2000ms"


@pytest.mark.asyncio
async def test_c2_audit_throughput(engine):
    """Audit writes >500/sec (persistent I/O, not memory)."""
    from sentinel_agentic.engine import audit
    t0 = time.perf_counter()
    for i in range(200):
        await audit({"kind": "bench", "i": i})
    rate = 200 / (time.perf_counter() - t0)
    print(f"\n  audit throughput {rate:.0f}/sec")
    assert rate > 500, f"audit {rate:.0f}/s < 500/s"


@pytest.mark.asyncio
async def test_c3_ast_validation_throughput(engine):
    """AST shield processes >500 payloads/sec (parse, not regex)."""
    payload = "x = 1\n" * 200  # ~1.2KB
    t0 = time.perf_counter()
    for _ in range(500):
        engine.validate(payload)
    rate = 500 / (time.perf_counter() - t0)
    print(f"\n  AST validation {rate:.0f}/sec")
    assert rate > 500, f"AST {rate:.0f}/s < 500/s"


# ══════════════════════════════════════════════════════════════════════════
# D. Soak — bounded sustained load, checks for degradation
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.soak
async def test_d1_20s_soak_no_degradation(engine):
    """20s of continuous load. Last-5s mean latency must be within 2× the
    first-5s mean. A leak or handle exhaustion shows up here."""
    from sentinel_agentic.actions import dispatch
    start = time.monotonic()
    buckets: list[list[float]] = [[], [], [], []]  # 4 x 5s windows
    iterations = 0
    while (elapsed := time.monotonic() - start) < 20:
        bucket = min(3, int(elapsed / 5))
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
    print(f"\n  soak: {iterations} iters, first5s={first:.0f}ms "
          f"last5s={last:.0f}ms, ratio={last/first:.2f}x")
    assert last < first * 2.0, (
        f"degradation: first5s={first:.0f}ms last5s={last:.0f}ms — "
        f"possible resource leak"
    )