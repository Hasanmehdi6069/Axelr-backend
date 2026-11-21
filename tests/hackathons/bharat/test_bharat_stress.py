"""Elite stress suite — high concurrency, network chaos, unicode
torture, idempotency races, and bounded-resource verification.

Every test is deterministic. No sleeps-of-hope, no external network.
Run:  pytest test_bharat_stress.py -q
"""

from __future__ import annotations

import asyncio
import pathlib
import sqlite3
import sys
import time

from bharat_agentic import (
    ApprovalConflictError,
    CivicAgent,
    EngineConfig,
)

sys.path.insert(0, str(pathlib.Path(__file__).parent))

NOTICE = (
    "IN THE COURT OF THE CHIEF JUDICIAL MAGISTRATE, PATNA\n"
    "Case No. 412/2024\n"
    "State of Bihar vs. Ramesh Kumar\n"
    "You are hereby directed to appear in person on 15/11/2026 at 10:30 AM."
)


def make_config(db_path: str, **kwargs):
    defaults = {
        "node_id": "test-node",
        "db_path": db_path,
        "max_text_bytes": 64 * 1024,
        "max_file_bytes": 128 * 1024,
        "max_text_chars": 10_000,
        "tool_timeout_seconds": 2.0,
        "health_timeout_seconds": 1.0,
        "sync_timeout_seconds": 1.0,
        "sync_max_retries": 3,
        "sync_backoff_base": 0.01,
        "rate_limit_per_second": 10_000.0,
        "audit_verify_cache_seconds": 0.0,
    }
    defaults.update(kwargs)
    cfg = EngineConfig(**defaults)
    cfg.validate()
    return cfg

# ---------------------------------------------------------------------------
# 1. High-volume ingestion
# ---------------------------------------------------------------------------


class TestHighVolumeIngest:
    async def test_100_concurrent_ingests(self, db_path):
        cfg = make_config(db_path)
        async with CivicAgent(node_id="test-node", config=cfg) as engine:
            async def one(i: int):
                return await engine.ingest_text(f"notice-{i}.txt", NOTICE.replace("412/2024", f"{412 + i}/2024"))

            results = await asyncio.gather(*[one(i) for i in range(100)])
            assert len(results) == 100
            assert len({r.case_id for r in results}) == 100
            cases = await engine.list_cases(limit=200)
            assert len(cases) == 100
            assert await engine.verify_chain() is True

    async def test_500kb_notice_within_limits(self, db_path):
        cfg = make_config(db_path, max_text_chars=1_000_000, max_text_bytes=1_000_000)
        async with CivicAgent(node_id="big", config=cfg) as eng:
            text = NOTICE + "\n" + ("filler " * 70_000)
            assert len(text) > 400_000
            record = await eng.ingest_text("big.txt", text)
            assert record.case_id.startswith("case_")

    async def test_unicode_torture(self, db_path):
        cfg = make_config(db_path)
        async with CivicAgent(node_id="unicode", config=cfg) as engine:
            text = (
                "न्यायालय का नोटिस 🇮🇳\n"
                + "😀🔥💯💥" * 100
                + "अनुच्छेद 14 के तहत कार्रवाई\n"
                + "αβγδεζηθικλμνξοπρστυφχψω" * 50
                + "你好世界" * 50
                + "السلام عليكم" * 50
            )
            record = await engine.ingest_text("unicode.txt", text)
            assert record.case_id.startswith("case_")
            assert await engine.verify_chain() is True


# ---------------------------------------------------------------------------
# 2. Approval storm
# ---------------------------------------------------------------------------


class TestApprovalStorm:
    async def test_50_tickets_50_parallel_approves(self, db_path):
        cfg = make_config(db_path)
        async with CivicAgent(node_id="storm", config=cfg) as engine:
            records = []
            for i in range(50):
                r = await engine.ingest_text(f"ticket-{i}.txt", NOTICE.replace("412/2024", f"{412 + i}/2024"))
                records.append(r)

            async def approve_one(r):
                return await engine.approve(r.case_id, "approved", "test")

            results = await asyncio.gather(*[approve_one(r) for r in records])
            assert len(results) == 50
            assert all(r.approved is True for r in results)
            assert await engine.verify_chain() is True

    async def test_same_ticket_100_parallel_approves(self, db_path):
        cfg = make_config(db_path)
        async with CivicAgent(node_id="same", config=cfg) as engine:
            record = await engine.ingest_text("same.txt", NOTICE)

            async def approve_once():
                try:
                    return await engine.approve(record.case_id, "approved", "test")
                except ApprovalConflictError:
                    return "conflict"

            results = await asyncio.gather(*[approve_once() for _ in range(100)])
            assert len([r for r in results if r != "conflict"]) == 1
            assert await engine.verify_chain() is True


# ---------------------------------------------------------------------------
# 3. Network chaos
# ---------------------------------------------------------------------------


class TestNetworkChaos:
    async def test_flaky_sync_with_50_percent_failure_rate(self, db_path):
        attempt = [0]

        async def flaky(record):
            attempt[0] += 1
            if attempt[0] % 2 == 0:
                raise RuntimeError("transient")
            return True

        cfg = make_config(db_path, sync_max_retries=8, sync_backoff_base=0.001, sync_backoff_cap=0.005)
        async with CivicAgent(node_id="chaos", config=cfg) as eng:
            eng.backend_sync = flaky
            record = await eng.ingest_text("n.txt", NOTICE)
            await eng._wait_for_sync_tasks(timeout=5.0)
            with sqlite3.connect(str(db_path)) as conn:
                row = conn.execute("SELECT synced FROM cases WHERE case_id = ?", (record.case_id,)).fetchone()
                assert row[0] == 1

    async def test_sync_timeout_does_not_hang(self, db_path):
        async def slow(record):
            await asyncio.sleep(30)
            return True

        cfg = make_config(
            db_path, sync_timeout_seconds=0.5, sync_max_retries=2, sync_backoff_base=0.001, sync_backoff_cap=0.005
        )
        async with CivicAgent(node_id="slow", config=cfg) as eng:
            eng.backend_sync = slow
            t0 = time.monotonic()
            record = await eng.ingest_text("n.txt", NOTICE)
            await eng._wait_for_sync_tasks(timeout=2.0)
            elapsed = time.monotonic() - t0
            assert elapsed < 5.0, f"sync hung too long: {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# 4. Rate-limit burst
# ---------------------------------------------------------------------------


class TestRateLimitBurst:
    async def test_500_concurrent_acquires_respect_rate(self, db_path):
        cfg = make_config(db_path, rate_limit_enabled=True, rate_limit_per_second=100.0)
        async with CivicAgent(node_id="rl", config=cfg) as eng:
            t0 = time.monotonic()
            await asyncio.gather(*[eng._rate_limit() for _ in range(500)])
            elapsed = time.monotonic() - t0
            assert elapsed >= 3.0, f"rate limit not respected: {elapsed:.2f}s for 500 at 100/s"


# ---------------------------------------------------------------------------
# 5. Audit under load
# ---------------------------------------------------------------------------


class TestAuditUnderLoad:
    async def test_200_concurrent_audit_appends(self, db_path):
        cfg = make_config(db_path)
        async with CivicAgent(node_id="audit", config=cfg) as eng:
            record = await eng.ingest_text("n.txt", NOTICE)

            async def audit_one(i):
                return await eng.audit_log(f"event-{i}", {"i": i})

            results = await asyncio.gather(*[audit_one(i) for i in range(200)])
            assert len(results) == 200
            assert await eng.verify_chain() is True

    async def test_ingest_and_audit_race_keeps_chain_intact(self, db_path):
        cfg = make_config(db_path)
        async with CivicAgent(node_id="race", config=cfg) as eng:
            async def ingest_one(i):
                return await eng.ingest_text(f"n-{i}.txt", NOTICE)

            async def audit_one(i):
                return await eng.audit_log(f"event-{i}", {"i": i})

            tasks = []
            for i in range(50):
                tasks.append(ingest_one(i))
                tasks.append(audit_one(i))

            await asyncio.gather(*tasks)
            assert await eng.verify_chain() is True


# ---------------------------------------------------------------------------
# 6. Idempotency race
# ---------------------------------------------------------------------------


class TestIdempotencyRace:
    async def test_100_concurrent_same_key_one_reminder(self, db_path):
        cfg = make_config(db_path)
        async with CivicAgent(node_id="idem", config=cfg) as eng:
            record = await eng.ingest_text("n.txt", NOTICE)

            async def one():
                return await eng.queue_reminder(
                    record.case_id, "sms", "+919876543210", idempotency_key="same-key"
                )

            results = await asyncio.gather(*[one() for _ in range(100)])
            reminder_ids = {r.reminder_id for r in results}
            assert len(reminder_ids) == 1, f"expected 1 reminder_id, got {len(reminder_ids)}"

    async def test_idempotency_survives_reopen(self, db_path):
        cfg = make_config(db_path)
        async with CivicAgent(node_id="idem", config=cfg) as eng:
            record = await eng.ingest_text("n.txt", NOTICE)
            r1 = await eng.queue_reminder(record.case_id, "sms", "+919876543210", idempotency_key="persist")
        async with CivicAgent(node_id="idem", config=cfg) as eng2:
            r2 = await eng2.queue_reminder(record.case_id, "sms", "+919876543210", idempotency_key="persist")
        assert r1.reminder_id == r2.reminder_id


# ---------------------------------------------------------------------------
# 7. Db lock contention
# ---------------------------------------------------------------------------


class TestDbLockContention:
    async def test_concurrent_reads_during_writes(self, db_path):
        cfg = make_config(db_path)
        async with CivicAgent(node_id="lock", config=cfg) as eng:
            record = await eng.ingest_text("n.txt", NOTICE)

            async def reader():
                return await eng.list_cases()

            async def writer(i):
                return await eng.ingest_text(f"n-{i}.txt", NOTICE)

            tasks = []
            for i in range(20):
                tasks.append(writer(i))
                tasks.append(reader())

            results = await asyncio.gather(*tasks)
            assert len(results) == 40
            assert await eng.verify_chain() is True


# ---------------------------------------------------------------------------
# 8. Graceful shutdown
# ---------------------------------------------------------------------------


class TestGracefulShutdown:
    async def test_shutdown_cancels_inflight_syncs(self, db_path):
        async def slow(record):
            await asyncio.sleep(30)
            return True

        cfg = make_config(db_path, sync_timeout_seconds=10.0)
        eng = CivicAgent(node_id="shut", config=cfg)
        await eng.start()
        eng.backend_sync = slow
        await eng.ingest_text("n.txt", NOTICE)
        await asyncio.sleep(0)
        await eng.shutdown()
        assert eng._sync_task is None or eng._sync_task.done()


# ---------------------------------------------------------------------------
# 9. Health under load
# ---------------------------------------------------------------------------


class TestHealthUnderLoad:
    async def test_50_concurrent_health_calls(self, db_path):
        cfg = make_config(db_path)
        async with CivicAgent(node_id="health", config=cfg) as eng:
            async def health_one():
                return await eng.health_check()

            results = await asyncio.gather(*[health_one() for _ in range(50)])
            assert len(results) == 50
            assert all(r["status"] == "ok" for r in results)