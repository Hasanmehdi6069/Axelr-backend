"""Elite stress suite — high concurrency, network chaos, unicode
torture, idempotency races, and bounded-resource verification.

Every test is deterministic. No sleeps-of-hope, no external network.
Run:  pytest test_bharat_stress.py -q
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from bharat_agentic import (
    ApprovalConflictError,
    ApprovalState,
    CivicAgent,
    EngineConfig,
)
from conftest import NOTICE, NOTICE_HINDI, make_config


# ---------------------------------------------------------------------------
# 1. High-volume ingestion
# ---------------------------------------------------------------------------

class TestHighVolumeIngest:
    async def test_100_concurrent_ingests(self, engine):
        async def one(i: int):
            return await engine.ingest_text(
                f"notice-{i}.txt",
                NOTICE.replace("412/2024", f"{412+i}/2024"))

        results = await asyncio.gather(*[one(i) for i in range(100)])
        assert len(results) == 100
        assert len({r.case_id for r in results}) == 100
        cases = await engine.list_cases(limit=200)
        assert len(cases) == 100
        assert await engine.verify_chain() is True

    async def test_500kb_notice_within_limits(self, db_path):
        cfg = make_config(
            db_path, max_text_chars=1_000_000,
            max_text_bytes=1_000_000)
        async with CivicAgent(node_id="big", config=cfg) as eng:
            text = NOTICE + "\n" + ("filler " * 70_000)
            assert len(text) > 400_000
            record = await eng.ingest_text("big.txt", text)
            assert record.case_id.startswith("case_")

    async def test_unicode_torture(self, engine):
        text = (
            "IN THE DISTRICT COURT, DELHI\n"
            "Case No. 45/2023\n"
            "State vs. Sunita\n"
            "You are directed to appear in person on 22/12/2026. "
            "🙏🏽 नमस्ते வணக்கம் 안녕 مرحبا "
            "\x00\x01\x02\x03 "
            "𝕌𝕟𝕚𝕔𝕠𝕕𝕖 ℍ𝕖𝕝𝕝𝕠 \u200f\u200e"
        )
        record = await engine.ingest_text("unicode.txt", text)
        assert record.case_number.value == "45/2023"
        assert record.summary_en
        assert record.summary_hi


# ---------------------------------------------------------------------------
# 2. Approval concurrency storm
# ---------------------------------------------------------------------------

class TestApprovalStorm:
    async def test_50_tickets_50_parallel_approves(self, engine):
        record = await engine.ingest_text("n.txt", NOTICE)
        tickets = []
        for i in range(50):
            await engine.queue_reminder(
                record.case_id, "sms", f"+9198765432{i:02d}")
            fresh = await engine.get_case(record.case_id)
            tickets.append(fresh.reminder_ids[-1])
        results = await asyncio.gather(
            *[engine.approve(t, by=f"op-{i}")
              for i, t in enumerate(tickets)],
            return_exceptions=True)
        wins = [r for r in results if not isinstance(r, Exception)]
        assert len(wins) == 50, (
            f"expected 50 wins, got {len(wins)}; "
            f"errors={[r for r in results if isinstance(r, Exception)]}")
        receipts = await engine.receipts(record.case_id)
        assert len(receipts) == 50
        assert await engine.verify_chain() is True

    async def test_same_ticket_100_parallel_approves(self, engine):
        record = await engine.ingest_text("n.txt", NOTICE)
        await engine.queue_reminder(
            record.case_id, "sms", "+919876543210")
        fresh = await engine.get_case(record.case_id)
        ticket_id = fresh.reminder_ids[-1]

        async def try_approve(i: int):
            try:
                return await engine.approve(
                    ticket_id, by=f"op-{i}")
            except ApprovalConflictError:
                return None

        results = await asyncio.gather(
            *[try_approve(i) for i in range(100)])
        winners = [r for r in results if r is not None]
        assert len(winners) == 1, (
            f"exactly one approver must win, got {len(winners)}")
        receipts = await engine.receipts(record.case_id)
        assert len(receipts) == 1


# ---------------------------------------------------------------------------
# 3. Network chaos — flaky sync
# ---------------------------------------------------------------------------

class TestNetworkChaos:
    async def test_flaky_sync_with_50_percent_failure_rate(self, db_path):
        attempt = [0]
        async def flaky(record):
            attempt[0] += 1
            if attempt[0] % 2 == 0:
                raise RuntimeError("transient")
            return True
        cfg = make_config(
            db_path, sync_max_retries=8, sync_backoff_base=0.001,
            sync_backoff_cap=0.005)
        async with CivicAgent(node_id="chaos", config=cfg) as eng:
            eng.backend_sync = flaky
            record = await eng.ingest_text("n.txt", NOTICE)
            await eng._wait_for_sync_tasks(timeout=5.0)
            with sqlite3.connect(str(db_path)) as conn:
                synced = conn.execute(
                    "SELECT synced FROM cases WHERE case_id=?",
                    (record.case_id,)).fetchone()[0]
            assert synced == 1, (
                f"retry should have succeeded; attempts={attempt[0]}")

    async def test_sync_timeout_does_not_hang(self, db_path):
        async def slow(record):
            await asyncio.sleep(30)
            return True
        cfg = make_config(
            db_path, sync_timeout_seconds=0.5,
            sync_max_retries=2, sync_backoff_base=0.001,
            sync_backoff_cap=0.005)
        async with CivicAgent(node_id="slow", config=cfg) as eng:
            eng.backend_sync = slow
            t0 = time.monotonic()
            await eng.ingest_text("n.txt", NOTICE)
            await eng._wait_for_sync_tasks(timeout=5.0)
            elapsed = time.monotonic() - t0
            assert elapsed < 5.0, (
                f"sync task hung: {elapsed:.1f}s")


# ---------------------------------------------------------------------------
# 4. Rate limit under burst
# ---------------------------------------------------------------------------

class TestRateLimitBurst:
    async def test_500_concurrent_acquires_respect_rate(self, db_path):
        cfg = make_config(
            db_path, rate_limit_enabled=True,
            rate_limit_per_second=100.0)
        async with CivicAgent(node_id="rl", config=cfg) as eng:
            t0 = time.monotonic()
            await asyncio.gather(
                *[eng._rate_limit() for _ in range(500)])
            elapsed = time.monotonic() - t0
            # Burst=100, 400 more at 100/s = ~4s
            assert elapsed >= 3.5, (
                f"rate limit fired too fast: {elapsed:.2f}s")
            assert elapsed < 15.0, (
                f"rate limit fired too slow: {elapsed:.2f}s")


# ---------------------------------------------------------------------------
# 5. Audit under massive concurrent load
# ---------------------------------------------------------------------------

class TestAuditUnderLoad:
    async def test_200_concurrent_audit_appends(self, engine):
        async def one(i: int):
            await engine.sentinel.audit.append(
                "load_test", {"i": i})
        await asyncio.gather(*[one(i) for i in range(200)])
        assert await engine.verify_chain() is True
        tail = await engine.audit_tail(limit=500)
        load = [e for e in tail if e.action == "load_test"]
        assert len(load) == 200

    async def test_ingest_and_audit_race_keeps_chain_intact(self, engine):
        async def ingest(i: int):
            await engine.ingest_text(
                f"n{i}.txt",
                NOTICE.replace("412/2024", f"{i}/2024"))
        async def audit(i: int):
            await engine.sentinel.audit.append(
                "custom", {"i": i})
        await asyncio.gather(
            *[ingest(i) for i in range(25)],
            *[audit(i) for i in range(25)])
        assert await engine.verify_chain() is True


# ---------------------------------------------------------------------------
# 6. Idempotency under concurrency
# ---------------------------------------------------------------------------

class TestIdempotencyRace:
    async def test_100_concurrent_same_key_one_reminder(self, engine):
        record = await engine.ingest_text("n.txt", NOTICE)
        async def one():
            return await engine.queue_reminder(
                record.case_id, "sms", "+919876543210",
                idempotency_key="race-key")
        results = await asyncio.gather(*[one() for _ in range(100)])
        reminder_ids = {r.reminder_id for r in results}
        assert len(reminder_ids) == 1, (
            f"expected 1 reminder_id, got {len(reminder_ids)}")

    async def test_idempotency_survives_reopen(self, db_path):
        cfg = make_config(db_path)
        async with CivicAgent(node_id="idem", config=cfg) as eng:
            record = await eng.ingest_text("n.txt", NOTICE)
            r1 = await eng.queue_reminder(
                record.case_id, "sms", "+919876543210",
                idempotency_key="persist")
        async with CivicAgent(node_id="idem", config=cfg) as eng2:
            r2 = await eng2.queue_reminder(
                record.case_id, "sms", "+919876543210",
                idempotency_key="persist")
            assert r1.reminder_id == r2.reminder_id


# ---------------------------------------------------------------------------
# 7. DB lock contention — WAL keeps reads flowing during writes
# ---------------------------------------------------------------------------

class TestDbLockContention:
    async def test_concurrent_reads_during_writes(self, engine):
        async def writer(i: int):
            await engine.ingest_text(
                f"w{i}.txt",
                NOTICE.replace("412/2024", f"W{i}/2024"))
        async def reader():
            return await engine.list_cases(limit=1000)
        results = await asyncio.gather(
            *[writer(i) for i in range(30)],
            *[reader() for _ in range(30)],
            return_exceptions=True)
        errors = [r for r in results if isinstance(r, Exception)]
        assert not errors, f"DB contention errors: {errors}"


# ---------------------------------------------------------------------------
# 8. Graceful shutdown
# ---------------------------------------------------------------------------

class TestGracefulShutdown:
    async def test_shutdown_cancels_inflight_syncs(self, db_path):
        async def slow(record):
            await asyncio.sleep(30)
            return True
        cfg = make_config(
            db_path, sync_timeout_seconds=10.0)
        eng = CivicAgent(node_id="shut", config=cfg)
        await eng.start()
        eng.backend_sync = slow
        await eng.ingest_text("n.txt", NOTICE)
        await asyncio.sleep(0)  # let sync task start
        assert len(eng._sync_tasks) >= 1
        t0 = time.monotonic()
        await eng.stop()
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"shutdown took {elapsed:.2f}s"
        assert len(eng._sync_tasks) == 0


# ---------------------------------------------------------------------------
# 9. Health endpoint under load
# ---------------------------------------------------------------------------

class TestHealthUnderLoad:
    async def test_50_concurrent_health_calls(self, engine):
        await engine.ingest_text("n.txt", NOTICE)
        results = await asyncio.gather(
            *[engine.health() for _ in range(50)],
            return_exceptions=True)
        errors = [r for r in results if isinstance(r, Exception)]
        assert not errors, f"health errors: {errors}"
        for r in results:
            assert r["details"]["cases_total"] == 1
            assert r["details"]["audit_chain_ok"] is True