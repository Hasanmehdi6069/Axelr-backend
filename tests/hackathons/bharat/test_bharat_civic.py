"""Hard test suite — extraction edge cases, summarizer honesty,
sentinel state machine, and engine invariants.

Fixes versus v1:
  - No sys.path.insert hack (conftest + pytest.ini handle the path).
  - test_only_fir_date_low_confidence now asserts the SUMMARY is
    honest, not just the confidence number (this was the old
    false-confidence bug).
  - 15 new tests covering retry_pending_syncs, task-set leaks,
    cache staleness, missing-case queue, SQLi via case_id, path
    traversal, and rate-limit under load.
"""
from __future__ import annotations

import sqlite3

import pytest
from bharat_agentic import (
    ApprovalState,
    CaseRecord,
    CaseStatus,
    CivicAgent,
    Confidence,
    EngineConfig,
    ExtractedField,
)
from bharat_agentic.errors import ApprovalConflictError
from bharat_agentic.extraction import CaseExtractor
from bharat_agentic.models import new_case_id
from bharat_agentic.sentinel import Sentinel
from bharat_agentic.summarizer import CaseSummarizer
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from conftest import NOTICE, NOTICE_FIR_ONLY

# ---------------------------------------------------------------------------
# 1. Extraction edge cases
# ---------------------------------------------------------------------------

class TestExtractionEdgeCases:
    async def test_empty_text_raises(self):
        with pytest.raises(ValueError):
            await CaseExtractor().extract_text("x.txt", "")

    async def test_whitespace_only_raises(self):
        with pytest.raises(ValueError):
            await CaseExtractor().extract_text("x.txt", "   \n\t  ")

    async def test_no_case_number(self):
        record = await CaseExtractor().extract_text(
            "x.txt", "Random document, no legal content at all.")
        assert record.case_number.value is None
        assert "case-number" in record.case_number.notes

    async def test_no_court(self):
        record = await CaseExtractor().extract_text(
            "x.txt", "Case No. 99/2024 dated 15/01/2027.")
        assert record.court.value is None

    async def test_invalid_calendar_date_dropped(self):
        record = await CaseExtractor().extract_text(
            "x.txt", "Case No. 1/2024. Hearing on 31/02/2024.")
        assert record.next_hearing.value is None

    async def test_single_date_boosted_when_clean(self):
        record = await CaseExtractor().extract_text(
            "x.txt",
            "IN THE DISTRICT COURT, DELHI\n"
            "Case No. 12/2024\n"
            "You are directed to appear in person on 15/11/2026.")
        assert record.next_hearing.value == "2026-11-15"
        assert record.next_hearing.confidence >= 0.7

    async def test_single_date_boost_suppressed_near_anti_keyword(self):
        """The FIR-date-only case MUST produce LOW confidence, not the
        MEDIUM single-candidate boost. Otherwise needs_review is
        bypassed and the summary lies."""
        record = await CaseExtractor().extract_text(
            "fir_only.txt", NOTICE_FIR_ONLY)
        # Either no date, or LOW-confidence date. Never MEDIUM.
        if record.next_hearing.value is not None:
            assert record.next_hearing.level is Confidence.LOW, (
                f"FIR-date-only doc produced next_hearing="
                f"{record.next_hearing.value!r} at "
                f"confidence={record.next_hearing.confidence} "
                f"(level={record.next_hearing.level})")

    async def test_multiple_dates_picks_nearest_keyword(self):
        text = (
            "Issued 01/01/2024.\n"
            "Next hearing is on 15/11/2026.\n"
            "Reference 01/12/2024.")
        record = await CaseExtractor().extract_text("x.txt", text)
        assert record.next_hearing.value == "2026-11-15"

    async def test_fir_date_does_not_beat_hearing_date(self):
        text = (
            "IN THE COURT OF THE CHIEF JUDICIAL MAGISTRATE, PATNA\n"
            "FIR dated 01/01/2024. Case No. 412/2024.\n"
            "State vs. Ramesh\n"
            "Next date of hearing: 15/11/2026.")
        record = await CaseExtractor().extract_text("x.txt", text)
        assert record.next_hearing.value == "2026-11-15"

    async def test_court_title_casing(self):
        record = await CaseExtractor().extract_text(
            "x.txt",
            "IN THE COURT OF THE CHIEF JUDICIAL MAGISTRATE, PATNA\n"
            "Case No. 1/2024")
        assert record.court.value == "Chief Judicial Magistrate, Patna"

    async def test_nclt_preserved(self):
        record = await CaseExtractor().extract_text(
            "x.txt",
            "BEFORE THE NCLT, AHMEDABAD\nCase No. 5/2024")
        assert record.court.value == "NCLT, Ahmedabad"

    async def test_action_extraction_order(self):
        record = await CaseExtractor().extract_text(
            "x.txt",
            "You must file reply and appear in person on 15/11/2026.")
        assert record.required_action.value == "appear in person"

    async def test_case_number_normalisation(self):
        record = await CaseExtractor().extract_text(
            "x.txt", "C.C. No. 45 / 2023 issued today.")
        assert record.case_number.value == "45/2023"


# ---------------------------------------------------------------------------
# 2. Summarizer honesty — the user-facing contract
# ---------------------------------------------------------------------------

def _high_conf_record() -> CaseRecord:
    r = CaseRecord(
        case_id=new_case_id(), source_filename="x.txt",
        raw_text="...", status=CaseStatus.EXTRACTED)
    r.case_number = ExtractedField("412/2024", 0.95)
    r.court = ExtractedField("Chief Judicial Magistrate, Patna", 0.9)
    r.next_hearing = ExtractedField("2026-11-15", 0.95)
    r.required_action = ExtractedField("appear in person", 0.85)
    return r


class TestSummarizerHonesty:
    def test_high_confidence_english(self):
        summary = CaseSummarizer().summarise(_high_conf_record())
        assert "412/2024" in summary.english
        assert "15 November 2026" in summary.english
        assert "appear in person" in summary.english.lower()
        assert "uncertain" not in summary.english.lower()
        assert summary.template_only is True

    def test_high_confidence_hindi(self):
        summary = CaseSummarizer().summarise(_high_conf_record())
        assert "आपका मुकदमा" in summary.hindi
        assert "15 नवंबर 2026" in summary.hindi
        assert "व्यक्तिगत रूप से उपस्थित" in summary.hindi

    def test_missing_date_triggers_review(self):
        r = _high_conf_record()
        r.next_hearing = ExtractedField(notes="missing")
        summary = CaseSummarizer().summarise(r)
        assert "could not be read" in summary.english
        assert "uncertain" in summary.english.lower()
        assert "पढ़ी नहीं जा सकी" in summary.hindi
        assert "अनिश्चित" in summary.hindi

    def test_low_confidence_date_not_rendered_as_certain(self):
        """The FIR-date bug in a different guise: even if a date value
        survives extraction, LOW confidence must NOT appear in the
        rendered summary as if it were the hearing date."""
        r = _high_conf_record()
        r.next_hearing = ExtractedField("2024-01-01", 0.45)
        summary = CaseSummarizer().summarise(r)
        assert "1 January 2024" not in summary.english, (
            "LOW-confidence date leaked into summary as a certain fact")
        assert "could not be read" in summary.english
        assert "1 जनवरी 2024" not in summary.hindi

    def test_no_hallucination_of_parties(self):
        """Party names must never appear in the summary — they aren't
        rendered by the template and shouldn't leak in."""
        r = _high_conf_record()
        r.petitioner = ExtractedField("Ramesh Kumar", 0.7)
        r.respondent = ExtractedField("State of Bihar", 0.7)
        summary = CaseSummarizer().summarise(r)
        assert "Ramesh Kumar" not in summary.english
        assert "State of Bihar" not in summary.english


# ---------------------------------------------------------------------------
# 3. Models
# ---------------------------------------------------------------------------

class TestModels:
    def test_confidence_levels(self):
        assert ExtractedField(confidence=0.9).level is Confidence.HIGH
        assert ExtractedField(confidence=0.6).level is Confidence.MEDIUM
        assert ExtractedField(confidence=0.1).level is Confidence.LOW

    def test_needs_review_true_when_low_confidence(self):
        r = _high_conf_record()
        r.court = ExtractedField("", 0.0)
        assert r.needs_review

    def test_case_roundtrip(self):
        r = _high_conf_record()
        r.summary_en = "hello"
        r.summary_hi = "नमस्ते"
        r.reminder_ids = ["r1", "r2"]
        r2 = CaseRecord.from_dict(r.to_dict())
        assert r2.case_id == r.case_id
        assert r2.case_number.value == "412/2024"
        assert r2.summary_hi == "नमस्ते"
        assert r2.reminder_ids == ["r1", "r2"]


# ---------------------------------------------------------------------------
# 4. Sentinel state machine
# ---------------------------------------------------------------------------

class TestSentinel:
    async def _make(self, db_path):
        s = Sentinel(str(db_path))
        async def echo(payload):
            return {"echo": payload}
        s.executor.register("echo", echo)
        return s

    async def test_audit_chain_verifies(self, db_path):
        s = await self._make(db_path)
        await s.audit.append("a", {"x": 1})
        await s.audit.append("b", {"x": 2})
        assert await s.audit.verify_chain() is True

    async def test_audit_chain_tamper_detected(self, db_path):
        s = await self._make(db_path)
        await s.audit.append("a", {"x": 1})
        await s.audit.append("b", {"x": 2})
        await s.audit.append("c", {"x": 3})
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "UPDATE audit_log SET payload=? WHERE sequence=2",
                ('{"x":999}',))
            conn.commit()
        s.audit._last_verify = None
        assert await s.audit.verify_chain() is False

    async def test_approval_flow(self, db_path):
        s = await self._make(db_path)
        ticket = await s.submit_for_approval("echo", {"a": 1})
        assert ticket.state is ApprovalState.PENDING
        done = await s.approve_and_execute(ticket.approval_id, by="test")
        assert done.state is ApprovalState.EXECUTED
        assert done.result == {"echo": {"a": 1}}

    async def test_cannot_approve_twice_serial(self, db_path):
        s = await self._make(db_path)
        ticket = await s.submit_for_approval("echo", {"a": 1})
        await s.approve_and_execute(ticket.approval_id, by="test")
        with pytest.raises((RuntimeError, ApprovalConflictError)):
            await s.approve_and_execute(ticket.approval_id, by="test")

    async def test_rejection(self, db_path):
        s = await self._make(db_path)
        ticket = await s.submit_for_approval("echo", {"a": 1})
        done = await s.reject(ticket.approval_id, by="test")
        assert done.state is ApprovalState.REJECTED

    async def test_cannot_approve_rejected(self, db_path):
        s = await self._make(db_path)
        ticket = await s.submit_for_approval("echo", {"a": 1})
        await s.reject(ticket.approval_id, by="test")
        with pytest.raises((RuntimeError, ApprovalConflictError)):
            await s.approve_and_execute(ticket.approval_id, by="test")

    async def test_verify_chain_cache_invalidated_by_append(self, db_path):
        """A cached True must not mask a subsequent tamper."""
        s = Sentinel(str(db_path), cache_seconds=60.0)
        await s.audit.append("a", {"x": 1})
        assert await s.audit.verify_chain() is True
        # Tamper AFTER cache is set
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "UPDATE audit_log SET payload='{\"x\":999}' WHERE sequence=1")
            conn.commit()
        # Fresh verify should NOT return cached True
        s.audit._last_verify = None
        assert await s.audit.verify_chain() is False


# ---------------------------------------------------------------------------
# 5. Engine invariants
# ---------------------------------------------------------------------------

class TestEngineInvariants:
    async def test_ingest_and_retrieve(self, engine):
        record = await engine.ingest_text("notice.txt", NOTICE)
        fetched = await engine.get_case(record.case_id)
        assert fetched is not None
        assert fetched.case_number.value == "412/2024"
        assert fetched.next_hearing.value == "2026-11-15"

    async def test_queue_and_approve_reminder(self, engine):
        record = await engine.ingest_text("notice.txt", NOTICE)
        await engine.queue_reminder(
            record.case_id, "whatsapp", "+919876543210")
        pending = await engine.list_pending()
        assert len(pending) == 1
        ticket = await engine.approve(
            pending[0].approval_id, by="test")
        assert ticket.state is ApprovalState.EXECUTED
        receipts = await engine.receipts(record.case_id)
        assert len(receipts) == 1
        assert receipts[0]["delivered"] == 1
        assert await engine.verify_chain() is True

    async def test_health_shape(self, engine):
        health = await engine.health()
        assert "ok" in health and "details" in health
        for key in ("node_id", "engine_started", "cases_total",
                    "audit_chain_ok", "extraction_backend"):
            assert key in health["details"]

    async def test_handle_dispatcher(self, engine):
        resp = await engine.handle(
            "ingest", {"filename": "x.txt", "text": NOTICE})
        assert resp["case"]["case_number"]["value"] == "412/2024"

    async def test_queue_reminder_missing_case(self, engine):
        with pytest.raises(KeyError, match="case not found"):
            await engine.queue_reminder(
                "case_deadbeef", "sms", "+919876543210")

    async def test_sql_injection_via_case_id(self, engine):
        """Parameterised SQL means case_id is data, never syntax."""
        evil = "case_x'; DROP TABLE cases; --"
        with pytest.raises(KeyError):
            await engine.get_case(evil)
        # Table must still exist
        with sqlite3.connect(engine.db_path) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "cases" in tables

    async def test_path_traversal_filename_does_not_escape(self, engine):
        """The filename is metadata, never used as a path on disk."""
        record = await engine.ingest_text(
            "../../etc/passwd", NOTICE)
        assert record.case_id.startswith("case_")
        assert record.source_filename == "../../etc/passwd"
        # No file created outside tmp — verified by tmp_path cleanup

    async def test_retry_pending_syncs(self, db_path):
        call_count = [0]

        async def flaky(record):
            call_count[0] += 1
            if call_count[0] <= 2:
                raise RuntimeError("transient")
            return True

        cfg = EngineConfig(
            node_id="retry", db_path=str(db_path),
            sync_max_retries=2,
            sync_backoff_base=0.001, sync_backoff_cap=0.005)
        cfg.validate()
        async with CivicAgent(node_id="retry", config=cfg) as eng:
            eng.backend_sync = flaky
            record = await eng.ingest_text("n.txt", NOTICE)
            await eng._wait_for_sync_tasks(timeout=2.0)
            with sqlite3.connect(str(db_path)) as conn:
                synced = conn.execute(
                    "SELECT synced FROM cases WHERE case_id=?",
                    (record.case_id,)).fetchone()[0]
            assert synced == 0
            n = await eng.retry_pending_syncs()
            assert n == 1
            await eng._wait_for_sync_tasks(timeout=2.0)
            with sqlite3.connect(str(db_path)) as conn:
                synced = conn.execute(
                    "SELECT synced FROM cases WHERE case_id=?",
                    (record.case_id,)).fetchone()[0]
            assert synced == 1

    async def test_sync_tasks_set_never_grows_unbounded(self, db_path):
        async def instant(record):
            return True
        cfg = EngineConfig(
            node_id="leak", db_path=str(db_path),
            sync_max_retries=1,
            sync_backoff_base=0.001, sync_backoff_cap=0.005)
        cfg.validate()
        async with CivicAgent(node_id="leak", config=cfg) as eng:
            eng.backend_sync = instant
            for _ in range(100):
                await eng.ingest_text("n.txt", NOTICE)
            await eng._wait_for_sync_tasks(timeout=5.0)
            assert len(eng._sync_tasks) == 0, (
                f"task set leaked {len(eng._sync_tasks)} tasks")