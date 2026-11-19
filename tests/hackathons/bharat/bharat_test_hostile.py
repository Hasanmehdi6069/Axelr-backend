"""Hostile stress-test suite for the Bharat Civic Agent.

Attacks:
  - Adversarial input: SQL/command injection text, null bytes, unicode,
    emoji, code-mixed Hinglish/Devanagari/Tamil, oversized payloads.
  - Concurrency: parallel approves on one ticket, parallel audit appends,
    parallel reminder enqueues on one case.
  - Network chaos: backend_sync timeouts, backend_health timeouts,
    provider send failures.
  - Logic: TOCTOU, date ambiguity, idempotency, chain tamper-detection.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from typing import Any, List

import pytest
from bharat_agentic import (
    ApprovalConflictError,
    ApprovalState,
    BharatAgentError,
    CivicAgent,
    EngineConfig,
    InputTooLargeError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "civic.db")
    yield path
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


@pytest.fixture()
async def engine(db_path):
    cfg = EngineConfig(
        node_id="test-node",
        db_path=db_path,
        max_text_bytes=64 * 1024,
        max_file_bytes=128 * 1024,
        max_text_chars=10_000,
        tool_timeout_seconds=2.0,
        health_timeout_seconds=1.0,
        sync_timeout_seconds=1.0,
        sync_max_retries=3,
        sync_backoff_base=0.01,
        rate_limit_per_second=10_000.0,
        audit_verify_cache_seconds=0.0,  # always re-walk during tests
    )
    cfg.validate()
    async with CivicAgent(node_id="test-node", config=cfg) as e:
        yield e


NOTICE = (
    "IN THE COURT OF THE CHIEF JUDICIAL MAGISTRATE, PATNA\n"
    "Case No. 412/2024\n"
    "State of Bihar vs. Ramesh Kumar\n"
    "You are hereby directed to appear in person on 15/11/2026 at 10:30 AM."
)


# ---------------------------------------------------------------------------
# Adversarial inputs
# ---------------------------------------------------------------------------

class TestAdversarialInputs:
    async def test_sql_injection_in_text_is_stored_verbatim_not_executed(
            self, engine):
        evil = (
            "IN THE COURT OF THE CHIEF JUDICIAL MAGISTRATE, PATNA\n"
            "Case No. 412/2024\n"
            "State vs. Ramesh\n"
            "'; DROP TABLE cases; -- appear in person on 15/11/2026."
        )
        record = await engine.ingest_text("evil.txt", evil)
        # Table must still exist.
        again = await engine.get_case(record.case_id)
        assert again is not None
        assert again.case_id == record.case_id
        # Injection payload preserved as data, not interpreted.
        with sqlite3.connect(engine.db_path) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "cases" in tables

    async def test_empty_text_rejected(self, engine):
        with pytest.raises(ValueError, match="empty"):
            await engine.ingest_text("empty.txt", "")
        with pytest.raises(ValueError, match="empty"):
            await engine.ingest_text("empty.txt", "   \n\t  ")

    async def test_oversized_text_rejected(self, engine):
        payload = "X" * (engine.config.max_text_chars + 1)
        with pytest.raises(InputTooLargeError):
            await engine.ingest_text("huge.txt", payload)

    async def test_oversized_file_rejected(self, engine):
        payload = b"X" * (engine.config.max_file_bytes + 1)
        with pytest.raises(InputTooLargeError):
            await engine.ingest("huge.txt", payload)

    async def test_unicode_and_emoji_roundtrip(self, engine):
        text = (
            "IN THE DISTRICT COURT, DELHI\n"
            "Case No. 45/2023\n"
            "State vs. Sunita Sharma\n"
            "You are directed to appear in person on 22/12/2026. "
            "🙏🏽 नमस्ते வணக்கம்"
        )
        record = await engine.ingest_text("unicode.txt", text)
        assert record.case_number.value == "45/2023"
        # Summary must be generated for both languages.
        assert record.summary_en and record.summary_hi

    async def test_null_bytes_do_not_crash(self, engine):
        text = NOTICE.replace("\n", "\x00")
        record = await engine.ingest_text("nulls.txt", text)
        assert record.case_id.startswith("case_")


# ---------------------------------------------------------------------------
# Multi-language detection
# ---------------------------------------------------------------------------

class TestMultilingualDetection:
    async def test_hindi_devanagari_action(self, engine):
        text = (
            "माननीय न्यायालय, पटना\n"
            "वाद संख्या 412/2024\n"
            "राज्य बनाम रमेश कुमार\n"
            "कृपया 15/11/2026 को व्यक्तिगत रूप से उपस्थित हों।"
        )
        record = await engine.ingest_text("hi.txt", text)
        assert record.required_action.value == "appear in person"
        assert record.next_hearing.value == "2026-11-15"

    async def test_hinglish_action(self, engine):
        text = (
            "District Court, Delhi\n"
            "Case No. 45/2023\n"
            "State vs. Sunita\n"
            "Aapko 22/12/2026 ko document leke aana hai."
        )
        record = await engine.ingest_text("hinglish.txt", text)
        assert record.required_action.value == "produce documents"

    async def test_unknown_language_flags_review(self, engine):
        # Tamil notice — no action keyword in our dictionary yet.
        text = (
            "சென்னை உயர் நீதிமன்றம்\n"
            "வழக்கு எண் 890/2024\n"
            "22/12/2026 அன்று ஆஜராக வேண்டும்."
        )
        record = await engine.ingest_text("ta.txt", text)
        # No action detected → warning emitted, review triggered.
        assert any("required_action" in w
                   for w in record.extraction_warnings)
        assert record.status.value == "needs_review"


# ---------------------------------------------------------------------------
# Date disambiguation
# ---------------------------------------------------------------------------

class TestDateDisambiguation:
    async def test_fir_date_does_not_beat_hearing_date(self, engine):
        text = (
            "IN THE COURT OF THE CHIEF JUDICIAL MAGISTRATE, PATNA\n"
            "FIR dated 01/01/2024. Case No. 412/2024.\n"
            "State vs. Ramesh\n"
            "Next date of hearing: 15/11/2026."
        )
        record = await engine.ingest_text("ambiguous.txt", text)
        assert record.next_hearing.value == "2026-11-15"

    async def test_only_fir_date_low_confidence(self, engine):
        text = (
            "IN THE DISTRICT COURT, DELHI\n"
            "Case No. 45/2023\n"
            "State vs. Sunita\n"
            "FIR dated 01/01/2024. Awaiting further orders."
        )
        record = await engine.ingest_text("fir_only.txt", text)
        # Best-effort candidate exists but confidence is low.
        assert record.next_hearing.confidence < 0.9


# ---------------------------------------------------------------------------
# Approval concurrency — the killer test
# ---------------------------------------------------------------------------

class TestApprovalConcurrency:
    async def test_parallel_approves_on_one_ticket_exactly_one_wins(
            self, engine):
        record = await engine.ingest_text("n.txt", NOTICE)
        req = await engine.queue_reminder(
            record.case_id, "whatsapp", "+919876543210")
        fresh = await engine.get_case(record.case_id)
        ticket_id = fresh.reminder_ids[-1]

        async def approve(i: int) -> Any:
            try:
                return await engine.approve(
                    ticket_id, by=f"op-{i}", reason="race")
            except ApprovalConflictError as exc:
                return exc

        results = await asyncio.gather(
            *[approve(i) for i in range(20)])
        wins = [r for r in results if not isinstance(r, ApprovalConflictError)]
        conflicts = [r for r in results
                     if isinstance(r, ApprovalConflictError)]
        assert len(wins) == 1, f"expected exactly 1 winner, got {len(wins)}"
        assert len(conflicts) == 19
        # Exactly one receipt recorded.
        receipts = await engine.receipts(record.case_id)
        assert len(receipts) == 1

    async def test_reject_and_approve_race(self, engine):
        record = await engine.ingest_text("n.txt", NOTICE)
        await engine.queue_reminder(
            record.case_id, "sms", "+919876543210")
        fresh = await engine.get_case(record.case_id)
        ticket_id = fresh.reminder_ids[-1]

        async def do_approve():
            try:
                return ("approve", await engine.approve(ticket_id))
            except ApprovalConflictError:
                return ("approve", None)

        async def do_reject():
            try:
                return ("reject", await engine.reject(ticket_id))
            except ApprovalConflictError:
                return ("reject", None)

        a, r = await asyncio.gather(do_approve(), do_reject())
        won = [x for x in (a, r) if x[1] is not None]
        assert len(won) == 1


# ---------------------------------------------------------------------------
# Audit chain integrity
# ---------------------------------------------------------------------------

class TestAuditChainIntegrity:
    async def test_concurrent_append_does_not_fork_chain(self, engine):
        async def append_many(n: int):
            await engine.sentinel.audit.append(
                "test_event", {"n": n})
        await asyncio.gather(*[append_many(i) for i in range(50)])
        ok = await engine.verify_chain()
        assert ok is True

    async def test_tampering_detected(self, engine):
        await engine.ingest_text("n.txt", NOTICE)
        with sqlite3.connect(engine.db_path) as conn:
            row = conn.execute(
                "SELECT sequence, entry_hash FROM audit_log LIMIT 1"
            ).fetchone()
            assert row is not None
            conn.execute(
                "UPDATE audit_log SET entry_hash='deadbeef' "
                "WHERE sequence=?", (row[0],))
            conn.commit()
        # Cache was set during ingest; force fresh verify.
        engine.sentinel.audit._last_verify = None
        ok = await engine.verify_chain()
        assert ok is False


# ---------------------------------------------------------------------------
# Backend sync resilience
# ---------------------------------------------------------------------------

class TestBackendSyncResilience:
    async def test_sync_timeout_marks_failed_and_does_not_hang(
            self, engine):
        async def slow_sync(record):
            await asyncio.sleep(30)  # 30s — engine timeout is 1s
            return True

        engine.backend_sync = slow_sync
        t0 = time.monotonic()
        record = await engine.ingest_text("n.txt", NOTICE)
        # Let the sync task complete its retry cycle.
        await asyncio.gather(*engine._sync_tasks, return_exceptions=True)
        elapsed = time.monotonic() - t0
        assert elapsed < 15.0, (
            f"sync task hung: elapsed={elapsed:.1f}s")
        # Case should be marked unsynced.
        with sqlite3.connect(engine.db_path) as conn:
            synced = conn.execute(
                "SELECT synced FROM cases WHERE case_id=?",
                (record.case_id,)).fetchone()[0]
        assert synced == 0

    async def test_sync_retries_and_succeeds(self, engine):
        attempts: List[int] = []

        async def flaky_sync(record):
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("transient")
            return True

        engine.backend_sync = flaky_sync
        record = await engine.ingest_text("n.txt", NOTICE)
        while engine._sync_tasks:
            await asyncio.gather(*engine._sync_tasks, return_exceptions=True)
            await asyncio.sleep(0.01)
        assert len(attempts) == 3
        with sqlite3.connect(engine.db_path) as conn:
            synced = conn.execute(
                "SELECT synced FROM cases WHERE case_id=?",
                (record.case_id,)).fetchone()[0]
        assert synced == 1

    async def test_health_backend_timeout_returns_not_ok(self, engine):
        async def slow_health():
            await asyncio.sleep(30)
            return True

        engine.backend_health = slow_health
        t0 = time.monotonic()
        result = await engine.health()
        elapsed = time.monotonic() - t0
        assert result["ok"] is False
        assert elapsed < 3.0
        assert result["details"]["backend_error"] is not None


# ---------------------------------------------------------------------------
# Provider failure
# ---------------------------------------------------------------------------

class TestProviderFailure:
    async def test_invalid_phone_rejected_before_send(self, engine):
        record = await engine.ingest_text("n.txt", NOTICE)
        await engine.queue_reminder(
            record.case_id, "sms", "not-a-phone")
        fresh = await engine.get_case(record.case_id)
        ticket_id = fresh.reminder_ids[-1]
        # Approval should transition to FAILED, not EXECUTED.
        ticket = await engine.approve(ticket_id, by="test")
        assert ticket.state is ApprovalState.FAILED
        receipts = await engine.receipts(record.case_id)
        assert receipts == []


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    async def test_same_key_returns_same_reminder(self, engine):
        record = await engine.ingest_text("n.txt", NOTICE)
        r1 = await engine.queue_reminder(
            record.case_id, "sms", "+919876543210",
            idempotency_key="op-123")
        r2 = await engine.queue_reminder(
            record.case_id, "sms", "+919876543210",
            idempotency_key="op-123")
        assert r1.reminder_id == r2.reminder_id

    async def test_different_payload_same_key_raises(self, engine):
        record = await engine.ingest_text("n.txt", NOTICE)
        await engine.queue_reminder(
            record.case_id, "sms", "+919876543210",
            idempotency_key="op-123")
        with pytest.raises(BharatAgentError):
            await engine.queue_reminder(
                record.case_id, "whatsapp", "+919876543210",
                idempotency_key="op-123")

    async def test_concurrent_same_key_produces_one_ticket(self, engine):
        record = await engine.ingest_text("n.txt", NOTICE)

        async def queue():
            return await engine.queue_reminder(
                record.case_id, "sms", "+919876543210",
                idempotency_key="op-race")

        results = await asyncio.gather(*[queue() for _ in range(10)])
        reminder_ids = {r.reminder_id for r in results}
        assert len(reminder_ids) == 1


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------

class TestRateLimit:
    async def test_burst_bounded_by_configured_rate(self, tmp_path):
        cfg = EngineConfig(
            node_id="rl",
            db_path=str(tmp_path / "rl.db"),
            rate_limit_per_second=50.0,
            rate_limit_enabled=True,
        )
        cfg.validate()
        async with CivicAgent(node_id="rl", config=cfg) as eng:
            t0 = time.monotonic()
            for _ in range(120):
                await eng._rate_limit()
            elapsed = time.monotonic() - t0
            # 120 tokens at 50/s with burst=50: ≥ 70 tokens must wait.
            # 70/50 = 1.4s minimum.
            assert elapsed >= 1.2
            assert elapsed < 5.0


# ---------------------------------------------------------------------------
# RPC handler
# ---------------------------------------------------------------------------

class TestRpcHandler:
    async def test_missing_field_clear_error(self, engine):
        with pytest.raises(ValueError, match="case_id"):
            await engine.handle("get_case", {})

    async def test_unknown_action(self, engine):
        with pytest.raises(ValueError, match="unknown action"):
            await engine.handle("nuke_from_orbit", {})

    async def test_full_rpc_cycle(self, engine):
        r = await engine.handle(
            "ingest", {"filename": "x.txt", "text": NOTICE})
        case_id = r["case"]["case_id"]
        rem = await engine.handle(
            "queue_reminder",
            {"case_id": case_id, "phone": "+919876543210",
             "channel": "sms"})
        assert "reminder" in rem
        pending = await engine.handle("list_pending", {})
        assert len(pending["pending"]) == 1
        aid = pending["pending"][0]["approval_id"]
        res = await engine.handle(
            "approve", {"approval_id": aid, "by": "op"})
        assert res["approval"]["state"] == "executed"


# ---------------------------------------------------------------------------
# Metrics harness smoke test
# ---------------------------------------------------------------------------

class TestMetricsHarness:
    async def test_seed_cases_all_succeed(self, tmp_path):
        from bharat_agentic import run_metrics
        report = await run_metrics("cases.json")
        assert report.total_cases == 20
        assert report.extraction_accuracy >= 0.95
        assert report.reminder_delivery_rate >= 0.95
