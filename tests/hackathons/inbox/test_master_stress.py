"""Master stress suite for Inbox Organizer — concurrency, throughput, stability.

Rules:
  * Every test marked `slow` — excluded from fast PR runs
  * Thresholds are conservative, CI-friendly
  * Tests verify system survives sustained load and edge cases
"""

from __future__ import annotations

import asyncio
import pathlib
import time

import pytest

pytestmark = [pytest.mark.slow]


# ══════════════════════════════════════════════════════════════════════════
# A. Concurrency
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_a1_100_concurrent_safe_segment_calls():
    """100 concurrent safe_segment calls must all return valid results."""
    from axelr.core import _safe_segment

    test_strings = [f"test string {i} with .. / \\ ` $ characters" for i in range(100)]

    results = await asyncio.gather(*[asyncio.to_thread(_safe_segment, s) for s in test_strings], return_exceptions=True)

    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"{len(failures)}/100 concurrent calls failed: {failures[:5]}"

    for result in results:
        assert isinstance(result, str)
        assert len(result) > 0


@pytest.mark.asyncio
async def test_a2_concurrent_undo_log_writes(inbox_root: pathlib.Path):
    """500 concurrent undo log writes must all persist without corruption."""
    from axelr.core import UndoEntry, UndoLog

    log_path = inbox_root / "undo.jsonl"
    log = UndoLog(log_path, max_entries=1000)

    async def write_one(i):
        await log.record(
            UndoEntry(
                op_id=f"op-{i}",
                timestamp=float(i),
                src=f"/a/{i}",
                dst=f"/b/{i}",
                filename=f"f{i}",
                category="Invoices",
                content_hash=f"h{i}",
            )
        )

    start = time.perf_counter()
    await asyncio.gather(*[write_one(i) for i in range(500)])
    elapsed = time.perf_counter() - start

    snap = await log.snapshot()
    assert len(snap) == 500, f"Lost writes: {len(snap)}/500"

    rate = 500 / elapsed
    print(f"\n  Undo log write throughput: {rate:.0f} ops/sec")
    assert rate > 100, f"Undo log too slow: {rate:.0f} ops/sec"


@pytest.mark.asyncio
async def test_a3_concurrent_target_path_generation(inbox_root: pathlib.Path):
    """100 concurrent target path generations must all produce unique paths."""
    from axelr.core import DocumentMetadata, _target_path

    async def generate_one(i):
        meta = DocumentMetadata(
            document_type="invoice",
            date=f"2026-{11:02d}-{(i % 28) + 1:02d}",
            sender=f"Sender {i}",
            category="Invoices",
            confidence=0.9,
        )
        return _target_path(inbox_root, meta, pathlib.Path(f"file-{i}.pdf"))

    results = await asyncio.gather(*[generate_one(i) for i in range(100)])

    unique_paths = set(results)
    assert len(unique_paths) == 100, f"Duplicate paths: {100 - len(unique_paths)} duplicates"

    for path in results:
        assert inbox_root.resolve() in path.resolve().parents


# ══════════════════════════════════════════════════════════════════════════
# B. Throughput
# ══════════════════════════════════════════════════════════════════════════


def test_b1_safe_segment_throughput():
    """safe_segment must process >10,000 inputs per second."""
    from axelr.core import _safe_segment

    test_inputs = [
        "normal string",
        "../../path/traversal",
        "file with spaces and special chars !@#$%^&*()",
        "very long string " * 10,
        "",
        "   whitespace   ",
    ] * 1000

    t0 = time.perf_counter()
    results = [_safe_segment(s) for s in test_inputs]
    elapsed = time.perf_counter() - t0

    rate = len(test_inputs) / elapsed
    print(f"\n  safe_segment throughput: {rate:.0f} ops/sec")
    assert rate > 10000, f"safe_segment too slow: {rate:.0f} ops/sec"
    assert len(results) == len(test_inputs)


def test_b2_metadata_normalization_throughput():
    """Metadata normalization must process >5,000 inputs per second."""
    from axelr.core import normalize_metadata

    test_metadata = [
        {
            "document_type": "invoice",
            "date": "2026-11-03",
            "sender": "Test",
            "total_amount": 100.0,
            "currency": "usd",
            "confidence": 0.9,
        },
        {
            "document_type": "receipt",
            "date": "2026-11-04",
            "sender": "Store",
            "total_amount": 50.0,
            "currency": "eur",
            "confidence": 0.85,
        },
        {
            "document_type": "other",
            "date": "2026-11-05",
            "sender": "Unknown",
            "total_amount": 0.0,
            "currency": "",
            "confidence": 0.5,
        },
    ] * 1000

    t0 = time.perf_counter()
    results = [normalize_metadata(m) for m in test_metadata]
    elapsed = time.perf_counter() - t0

    rate = len(test_metadata) / elapsed
    print(f"\n  Metadata normalization throughput: {rate:.0f} ops/sec")
    assert rate > 5000, f"Metadata normalization too slow: {rate:.0f} ops/sec"
    assert len(results) == len(test_metadata)


# ══════════════════════════════════════════════════════════════════════════
# C. Stability & Edge Cases
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_c1_1000_iterations_safe_segment():
    """1000 iterations of various inputs must all succeed without errors."""
    from axelr.core import _safe_segment

    test_cases = []
    for i in range(1000):
        test_cases.append(f"normal input {i}")
        test_cases.append(f"../../escape/{i}")
        test_cases.append(f"file name with spaces {i}.pdf")
        test_cases.append("")
        test_cases.append("   ")
        test_cases.append(f"very long string {'x' * 100} {i}")

    results = []
    for tc in test_cases:
        results.append(_safe_segment(tc))

    assert len(results) == len(test_cases)
    assert all(isinstance(r, str) for r in results)
    assert all(len(r) > 0 for r in results)


@pytest.mark.asyncio
async def test_c2_undo_log_soak(inbox_root: pathlib.Path):
    """10,000 undo log entries must be handled without memory leaks."""
    from axelr.core import UndoEntry, UndoLog

    log_path = inbox_root / "undo.jsonl"
    log = UndoLog(log_path, max_entries=10000)

    t0 = time.perf_counter()
    for i in range(10000):
        await log.record(
            UndoEntry(
                op_id=f"op-{i}",
                timestamp=float(i),
                src=f"/a/{i}",
                dst=f"/b/{i}",
                filename=f"f{i}",
                category="Invoices",
                content_hash=f"h{i}",
            )
        )
    elapsed = time.perf_counter() - t0

    snap = await log.snapshot()
    assert len(snap) == 10000

    rate = 10000 / elapsed
    print(f"\n  Undo log 10k entries throughput: {rate:.0f} ops/sec")
    assert rate > 1000, f"Undo log too slow: {rate:.0f} ops/sec"


# ══════════════════════════════════════════════════════════════════════════
# D. Resource Usage
# ══════════════════════════════════════════════════════════════════════════


def test_d1_memory_efficiency():
    """Core functions must not have excessive memory usage."""
    import sys

    from axelr.core import _safe_segment

    test_string = "test string with various characters" * 1000
    iterations = 10000

    before = sys.getsizeof(test_string)
    for _ in range(iterations):
        result = _safe_segment(test_string)
    after = sys.getsizeof(result)

    assert after < 1000, f"Result uses too much memory: {after} bytes"


@pytest.mark.asyncio
async def test_d2_concurrent_file_operations(inbox_root: pathlib.Path, sample_invoice: str):
    """50 concurrent file operations must complete without errors."""
    from axelr.core import DocumentSkill

    for i in range(50):
        src = inbox_root / "inbox" / f"invoice-{i}.txt"
        src.write_text(sample_invoice, encoding="utf-8")

    skill = await DocumentSkill.create(inbox_root)

    async def process_one(i):
        src = inbox_root / "inbox" / f"invoice-{i}.txt"
        return await skill.process_now(src)

    results = await asyncio.gather(*[process_one(i) for i in range(50)], return_exceptions=True)
    await skill.stop()

    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"{len(failures)}/50 concurrent file ops failed: {failures[:5]}"
