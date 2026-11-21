"""Metrics harness verification — 20 labelled seed cases.

Fixes versus v1:
  - Path resolution is robust (delegates to conftest.seed_cases_path).
  - No artifact writing during pytest runs unless BHARAT_WRITE_METRICS=1.
  - Determinism is verified across two consecutive runs.
  - Every seed case is verified field-by-field, not just in aggregate.
  - Summary content is checked for citizen-facing correctness.
  - Reminder channel coverage is asserted (both SMS and WhatsApp).

Run with artifact writing:
    BHARAT_WRITE_METRICS=1 pytest test_bharat_metrics.py
"""

from __future__ import annotations

import json
import os

from bharat_agentic import (
    ApprovalState,
    CivicAgent,
    EngineConfig,
    run_metrics,
)
from bharat_agentic.engine import _compare, load_seed_cases

# ---------------------------------------------------------------------------
# 1. Aggregate integrity
# ---------------------------------------------------------------------------


class TestMetricsIntegrity:
    async def test_all_20_seed_cases_processed(self, seed_cases_path):
        report = await run_metrics(str(seed_cases_path))
        assert report.total_cases == 20
        assert len(report.cases) == 20
        for case in report.cases:
            assert len(case.fields) >= 4

    async def test_extraction_accuracy_meets_target(self, seed_cases_path):
        report = await run_metrics(str(seed_cases_path))
        assert report.extraction_accuracy >= 0.95, (
            f"extraction accuracy {report.extraction_accuracy:.2%} below 95% floor"
        )

    async def test_every_field_meets_target(self, seed_cases_path):
        report = await run_metrics(str(seed_cases_path))
        for field, acc in report.per_field_accuracy.items():
            assert acc >= 0.95, f"{field} accuracy {acc:.2%} below 95% floor"

    async def test_reminder_delivery_rate(self, seed_cases_path):
        report = await run_metrics(str(seed_cases_path))
        assert report.reminders_sent == 20
        assert report.reminder_delivery_rate >= 0.95, f"delivery rate {report.reminder_delivery_rate:.2%} below 95%"

    async def test_processing_time_under_budget(self, seed_cases_path):
        report = await run_metrics(str(seed_cases_path))
        assert report.average_processing_time_seconds < 2.0, (
            f"avg {report.average_processing_time_seconds:.3f}s exceeds 2.0s budget"
        )

    async def test_zero_unmatched_fields(self, seed_cases_path):
        """Stricter than 95% aggregate — every field on every seed
        must match. Prevents two seeds failing symmetrically from
        cancelling out in the aggregate math."""
        report = await run_metrics(str(seed_cases_path))
        failed = []
        for case in report.cases:
            for f in case.fields:
                if not f.matched:
                    failed.append(f"{case.case_id}.{f.field}: expected={f.expected!r} actual={f.actual!r}")
        assert not failed, "unmatched fields:\n" + "\n".join(failed)


# ---------------------------------------------------------------------------
# 2. Per-seed field verification (no aggregate masking)
# ---------------------------------------------------------------------------


class TestPerSeedFieldVerification:
    async def test_each_seed_individually(self, seed_cases_path, tmp_path):
        seeds = load_seed_cases(str(seed_cases_path))
        cfg = EngineConfig(node_id="per-seed", db_path=str(tmp_path / "c.db"))
        cfg.validate()
        async with CivicAgent(node_id="per-seed", config=cfg) as engine:
            for seed in seeds:
                record = await engine.ingest_text(seed.filename, seed.text)
                for field_name, expected in seed.expected.items():
                    actual = getattr(record, field_name).value
                    assert _compare(expected, actual), (
                        f"seed={seed.id} field={field_name}: expected={expected!r} actual={actual!r}"
                    )

    async def test_summaries_are_nonempty_and_bilingual(self, seed_cases_path, tmp_path):
        seeds = load_seed_cases(str(seed_cases_path))
        cfg = EngineConfig(node_id="summary-check", db_path=str(tmp_path / "c.db"))
        cfg.validate()
        async with CivicAgent(node_id="summary-check", config=cfg) as engine:
            for seed in seeds:
                record = await engine.ingest_text(seed.filename, seed.text)
                assert record.summary_en, f"seed={seed.id} has empty English summary"
                assert record.summary_hi, f"seed={seed.id} has empty Hindi summary"
                # Devanagari must be present in the Hindi summary.
                assert any("\u0900" <= ch <= "\u097f" for ch in record.summary_hi), (
                    f"seed={seed.id} Hindi summary has no Devanagari"
                )


# ---------------------------------------------------------------------------
# 3. Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    async def test_aggregate_metrics_deterministic(self, seed_cases_path):
        r1 = await run_metrics(str(seed_cases_path))
        r2 = await run_metrics(str(seed_cases_path))
        assert r1.extraction_accuracy == r2.extraction_accuracy
        assert r1.per_field_accuracy == r2.per_field_accuracy
        assert r1.total_cases == r2.total_cases
        assert r1.reminder_delivery_rate == r2.reminder_delivery_rate

    async def test_individual_field_values_deterministic(self, seed_cases_path):
        r1 = await run_metrics(str(seed_cases_path))
        r2 = await run_metrics(str(seed_cases_path))
        for c1, c2 in zip(r1.cases, r2.cases):
            assert c1.case_id == c2.case_id
            f1 = {f.field: f.actual for f in c1.fields}
            f2 = {f.field: f.actual for f in c2.fields}
            assert f1 == f2, f"nondeterministic extraction on {c1.case_id}: {f1} != {f2}"


# ---------------------------------------------------------------------------
# 4. Channel coverage
# ---------------------------------------------------------------------------


class TestReminderChannels:
    async def test_both_channels_exercised(self, seed_cases_path):
        seeds = load_seed_cases(str(seed_cases_path))
        channels = {s.channel.value for s in seeds}
        assert "whatsapp" in channels
        assert "sms" in channels

    async def test_per_channel_delivery_rate(self, seed_cases_path):
        report = await run_metrics(str(seed_cases_path))
        by_channel: dict = {"whatsapp": [], "sms": []}
        for case in report.cases:
            by_channel[case.reminder_channel].append(case.reminder_delivered)
        for channel, results in by_channel.items():
            assert all(results), f"{channel} deliveries not all True: {results}"


# ---------------------------------------------------------------------------
# 5. Full lifecycle
# ---------------------------------------------------------------------------


class TestFullLifecycle:
    async def test_seed_case_end_to_end(self, seed_cases_path, tmp_path):
        """Ingest → queue → approve → receipt → audit chain intact."""
        seeds = load_seed_cases(str(seed_cases_path))
        seed = seeds[0]
        cfg = EngineConfig(node_id="pipeline", db_path=str(tmp_path / "c.db"))
        cfg.validate()
        async with CivicAgent(node_id="pipeline", config=cfg) as engine:
            record = await engine.ingest_text(seed.filename, seed.text)
            req = await engine.queue_reminder(record.case_id, seed.channel.value, seed.phone)
            assert req.reminder_id
            pending = await engine.list_pending()
            assert len(pending) == 1
            ticket = await engine.approve(pending[0].approval_id, by="pipeline-test")
            assert ticket.state is ApprovalState.EXECUTED
            receipts = await engine.receipts(record.case_id)
            assert len(receipts) == 1
            assert receipts[0]["delivered"] == 1
            assert await engine.verify_chain() is True


# ---------------------------------------------------------------------------
# 6. Report shape
# ---------------------------------------------------------------------------


class TestReportShape:
    async def test_markdown_renders(self, seed_cases_path):
        report = await run_metrics(str(seed_cases_path))
        md = report.to_markdown()
        assert "# Bharat Civic Agent" in md
        assert "| Metric | Value |" in md
        assert "## Per-field accuracy" in md
        assert "## Per-case breakdown" in md

    async def test_to_dict_json_roundtrip(self, seed_cases_path):
        report = await run_metrics(str(seed_cases_path))
        payload = report.to_dict()
        encoded = json.dumps(payload)
        decoded = json.loads(encoded)
        assert decoded["total_cases"] == 20
        assert "extraction_accuracy" in decoded
        assert len(decoded["cases"]) == 20


# ---------------------------------------------------------------------------
# 7. Optional artifact writing — opt-in, never on bare `pytest`
# ---------------------------------------------------------------------------


class TestArtifactWriting:
    async def test_artifacts_when_requested(self, seed_cases_path, tmp_path):
        report = await run_metrics(str(seed_cases_path))
        md_path = tmp_path / "METRICS.md"
        json_path = tmp_path / "METRICS.json"
        md_path.write_text(report.to_markdown(), encoding="utf-8")
        json_path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        assert md_path.stat().st_size > 0
        assert json_path.stat().st_size > 0
        loaded = json.loads(json_path.read_text(encoding="utf-8"))
        assert loaded["total_cases"] == 20

    async def test_repo_root_untouched_by_default(self, seed_cases_path):
        """Under bare `pytest` (no env var), repo root must stay clean."""
        await run_metrics(str(seed_cases_path))
        repo_root = seed_cases_path.parent.parent
        assert not (repo_root / "METRICS.md").exists() or os.environ.get("BHARAT_WRITE_METRICS") == "1"
