"""Bharat Civic Agent — the engine.

Runs in-process with your existing backend (axelr). No HTTP server, no new
port. Three-line integration:

    from bharat_agentic import CivicAgent
    engine = CivicAgent(node_id="axelr-prod-1", db_path="/var/data/civic.db")
    await engine.start()
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from bharat_agentic.config import EngineConfig
from bharat_agentic.errors import (
    BharatAgentError,
    InputTooLargeError,
)
from bharat_agentic.extraction import CaseExtractor
from bharat_agentic.models import (
    ApprovalTicket,
    AuditEvent,
    CaseMetric,
    CaseRecord,
    CaseStatus,
    CaseSummary,
    FieldMatch,
    MetricsReport,
    ReminderChannel,
    ReminderRequest,
    new_reminder_id,
)

from .reminders import list_receipts, register_reminder_tools
from .sentinel import Sentinel
from .summarizer import CaseSummarizer

logger = logging.getLogger("bharat_agentic.engine")

BackendSync = Callable[[CaseRecord], Awaitable[bool]]
BackendHealth = Callable[[], Awaitable[bool]]


# ---------------------------------------------------------------------------
# SQLite persistence
# ---------------------------------------------------------------------------

_CASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    synced INTEGER DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cases_synced ON cases(synced);
CREATE INDEX IF NOT EXISTS idx_cases_updated ON cases(updated_at);

CREATE TABLE IF NOT EXISTS reminder_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    reminder_id TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    to_addr TEXT NOT NULL,
    english TEXT NOT NULL DEFAULT '',
    hindi TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
"""


def _init_db(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_CASE_SCHEMA)


def _save_case(db_path: str, record: CaseRecord) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cases(case_id, payload, synced, updated_at) VALUES (?,?,?,?)",
            (record.case_id, json.dumps(record.to_dict(), default=str), 0, time.time()),
        )


def _load_case(db_path: str, case_id: str) -> Optional[CaseRecord]:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT payload FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if not row:
            return None
        return CaseRecord.from_dict(json.loads(row[0]))


def _list_cases(db_path: str, limit: int = 100) -> List[CaseRecord]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT payload FROM cases ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [CaseRecord.from_dict(json.loads(r[0])) for r in rows]


def _mark_synced(db_path: str, case_id: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE cases SET synced=1 WHERE case_id=?", (case_id,))


def _count_cases(db_path: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0])


def _append_reminder_id_sync(db_path: str, case_id: str, approval_id: str) -> None:
    """Atomic read-modify-write on reminder_ids so concurrent queue_reminder
    calls on the same case never lose an append."""
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT payload FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if not row:
            raise KeyError(f"case not found: {case_id}")
        record = CaseRecord.from_dict(json.loads(row[0]))
        if approval_id not in record.reminder_ids:
            record.reminder_ids.append(approval_id)
        conn.execute(
            "UPDATE cases SET payload=?, updated_at=? WHERE case_id=?",
            (json.dumps(record.to_dict(), default=str), time.time(), case_id),
        )
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Token bucket
# ---------------------------------------------------------------------------


class _TokenBucket:
    def __init__(self, rate_per_second: float) -> None:
        self.rate = rate_per_second
        self.capacity = max(1, int(rate_per_second))
        self._tokens = float(self.capacity)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
            await asyncio.sleep(wait)


# ---------------------------------------------------------------------------
# CivicAgent
# ---------------------------------------------------------------------------


class CivicAgent:
    """Library engine. Instantiate one per host application process."""

    def __init__(
        self,
        node_id: str,
        db_path: Optional[str] = None,
        backend_sync: Optional[BackendSync] = None,
        backend_health: Optional[BackendHealth] = None,
        config: Optional[EngineConfig] = None,
    ) -> None:
        if config is None:
            config = EngineConfig(node_id=node_id, db_path=db_path or "./bharat_civic.db")
            config.validate()
        self.config = config
        self.node_id = config.node_id
        self.db_path = config.db_path
        self.backend_sync = backend_sync
        self.backend_health = backend_health
        self.sentinel = Sentinel(
            db_path=config.db_path,
            cache_seconds=config.audit_verify_cache_seconds,
            tool_timeout_seconds=config.tool_timeout_seconds,
        )
        self.extractor = CaseExtractor()
        self.summarizer = CaseSummarizer()
        self._started = False
        self._sync_tasks: set[asyncio.Task] = set()
        self._bucket = _TokenBucket(config.rate_limit_per_second) if config.rate_limit_enabled else None

    # ----- lifecycle -----

    async def start(self) -> None:
        _init_db(self.db_path)
        register_reminder_tools(self.sentinel, self.db_path)
        self._started = True
        logger.info(
            "civic_agent_started node=%s backend_sync=%s rate_limit=%s",
            self.node_id,
            self.backend_sync is not None,
            self.config.rate_limit_enabled,
        )

    async def stop(self) -> None:
        for task in list(self._sync_tasks):
            task.cancel()
        if self._sync_tasks:
            await asyncio.gather(*self._sync_tasks, return_exceptions=True)
        self._sync_tasks.clear()
        self._started = False
        logger.info("civic_agent_stopped node=%s", self.node_id)

    async def __aenter__(self) -> "CivicAgent":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    async def _rate_limit(self) -> None:
        if self._bucket is not None:
            await self._bucket.acquire()

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("engine not started; call `await engine.start()`")

    def _enforce_text_size(self, text: str) -> None:
        if not text or not text.strip():
            raise ValueError("document text is empty")
        if len(text) > self.config.max_text_chars:
            raise InputTooLargeError(f"text length {len(text)} exceeds max_text_chars={self.config.max_text_chars}")
        if len(text.encode("utf-8")) > self.config.max_text_bytes:
            raise InputTooLargeError(
                f"text bytes {len(text.encode('utf-8'))} exceeds max_text_bytes={self.config.max_text_bytes}"
            )

    # ----- ingestion -----

    async def ingest_text(self, filename: str, text: str) -> CaseRecord:
        self._require_started()
        await self._rate_limit()
        self._enforce_text_size(text)
        record = await self.extractor.extract_text(filename, text)
        await self._finalise(record, filename)
        return record

    async def ingest(self, filename: str, content: bytes) -> CaseRecord:
        self._require_started()
        await self._rate_limit()
        if not content:
            raise ValueError("document content is empty")
        if len(content) > self.config.max_file_bytes:
            raise InputTooLargeError(f"file bytes {len(content)} exceeds max_file_bytes={self.config.max_file_bytes}")
        record = await self.extractor.extract(filename, content)
        await self._finalise(record, filename)
        return record

    def _create_done_callback(self, task: asyncio.Task) -> Callable[[asyncio.Task], None]:
        def callback(t: asyncio.Task) -> None:
            self._sync_tasks.discard(task)

        return callback

    async def _finalise(self, record: CaseRecord, filename: str) -> None:
        record.source_filename = filename
        summary = self.summarizer.summarise(record)
        record.status = CaseStatus.NEEDS_REVIEW if record.needs_review else CaseStatus.SUMMARIZED
        await asyncio.to_thread(_save_case, self.db_path, record)
        await self.sentinel.audit.append(
            "case_ingested",
            {
                "case_id": record.case_id,
                "filename": filename,
                "status": record.status.value,
                "template_only": summary.template_only,
            },
        )
        if self.backend_sync is not None:
            task = asyncio.create_task(self._sync_with_retry(record))
            self._sync_tasks.add(task)
            task.add_done_callback(self._create_done_callback(task))

    async def _sync_with_retry(self, record: CaseRecord) -> None:
        cfg = self.config
        last_err: Optional[BaseException] = None
        for attempt in range(1, cfg.sync_max_retries + 1):
            try:
                ok = await asyncio.wait_for(
                    self.backend_sync(record),  # type: ignore[misc]
                    timeout=cfg.sync_timeout_seconds,
                )
                if ok:
                    await asyncio.to_thread(_mark_synced, self.db_path, record.case_id)
                    await self.sentinel.audit.append("case_synced", {"case_id": record.case_id, "attempt": attempt})
                    return
                logger.warning("backend_sync_returned_false case=%s attempt=%d", record.case_id, attempt)
            except asyncio.TimeoutError as exc:
                last_err = exc
                logger.warning(
                    "backend_sync_timeout case=%s attempt=%d timeout_s=%.1f",
                    record.case_id,
                    attempt,
                    cfg.sync_timeout_seconds,
                )
            except Exception as exc:
                last_err = exc
                logger.warning("backend_sync_error case=%s attempt=%d err=%r", record.case_id, attempt, exc)
            if attempt < cfg.sync_max_retries:
                wait = min(cfg.sync_backoff_cap, cfg.sync_backoff_base * (2 ** (attempt - 1)))
                logger.debug("backend_sync_backoff case=%s attempt=%d wait_s=%.3f", record.case_id, attempt, wait)
                # Use asyncio.sleep with minimal jitter to avoid test flakiness
                await asyncio.sleep(wait)
        await self.sentinel.audit.append(
            "case_sync_failed",
            {"case_id": record.case_id, "attempts": cfg.sync_max_retries, "last_error": repr(last_err)},
        )
        logger.error("backend_sync_exhausted case=%s err=%r", record.case_id, last_err)

    async def _wait_for_sync_tasks(self, timeout: float = 5.0) -> None:
        """Wait for all pending sync tasks to complete."""
        if not self._sync_tasks:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*list(self._sync_tasks), return_exceptions=True), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def retry_pending_syncs(self, limit: int = 100) -> int:
        """Re-enqueue sync for up to `limit` unsynced cases. Returns count."""

        def _pick() -> List[CaseRecord]:
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT payload FROM cases WHERE synced=0 ORDER BY updated_at ASC LIMIT ?", (limit,)
                ).fetchall()
                return [CaseRecord.from_dict(json.loads(r[0])) for r in rows]

        records = await asyncio.to_thread(_pick)
        if self.backend_sync is None:
            return 0
        for record in records:
            task = asyncio.create_task(self._sync_with_retry(record))
            self._sync_tasks.add(task)
            task.add_done_callback(self._create_done_callback(task))
        return len(records)

    # ----- read -----

    async def get_case(self, case_id: str) -> Optional[CaseRecord]:
        if not case_id.startswith("case_") or not case_id[5:].isalnum():
            raise KeyError(f"case not found: {case_id}")
        return await asyncio.to_thread(_load_case, self.db_path, case_id)

    async def list_cases(self, limit: int = 100) -> List[CaseRecord]:
        return await asyncio.to_thread(_list_cases, self.db_path, limit)

    # ----- reminders -----

    def _lookup_idempotency_sync(
        self,
        key: str,
    ) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM reminder_idempotency WHERE idempotency_key=?", (key,)).fetchone()
            return dict(row) if row else None

    def _insert_idempotency_sync(
        self,
        key: str,
        reminder_id: str,
        approval_id: str,
        case_id: str,
        channel: str,
        to: str,
        english: str,
        hindi: str,
    ) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO reminder_idempotency VALUES (?,?,?,?,?,?,?,?,?)",
                    (key, reminder_id, approval_id, case_id, channel, to, english, hindi, time.time()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    async def queue_reminder(
        self,
        case_id: str,
        channel: str,
        to: str,
        english: Optional[str] = None,
        hindi: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> ReminderRequest:
        await self._rate_limit()
        record = await self.get_case(case_id)
        if record is None:
            raise KeyError(f"case not found: {case_id}")
        ch = ReminderChannel(channel)

        if idempotency_key:
            prior = await asyncio.to_thread(self._lookup_idempotency_sync, idempotency_key)
            if prior:
                if prior["case_id"] != case_id or prior["channel"] != channel or prior["to_addr"] != to:
                    raise BharatAgentError("idempotency_key reused with different payload")
                return ReminderRequest(
                    reminder_id=prior["reminder_id"],
                    case_id=case_id,
                    channel=ch,
                    to=to,
                    english=prior["english"] or "",
                    hindi=prior["hindi"] or "",
                )

        summary = CaseSummary(
            case_id=record.case_id,
            english=english or record.summary_en or "",
            hindi=hindi or record.summary_hi or "",
        )
        if not summary.english and not summary.hindi:
            summary = self.summarizer.summarise(record)
        reminder_id = new_reminder_id()
        payload = {
            "reminder_id": reminder_id,
            "case_id": case_id,
            "to": to,
            "body": f"{summary.hindi}\n\n{summary.english}",
        }
        tool = "sms_send" if ch is ReminderChannel.SMS else "whatsapp_send"
        ticket = await self.sentinel.submit_for_approval(tool, payload)
        payload["approval_id"] = ticket.approval_id

        if idempotency_key:
            inserted = await asyncio.to_thread(
                self._insert_idempotency_sync,
                idempotency_key,
                reminder_id,
                ticket.approval_id,
                case_id,
                channel,
                to,
                summary.english,
                summary.hindi,
            )
            if not inserted:
                # Lost the race — another worker won. Return theirs.
                prior = await asyncio.to_thread(self._lookup_idempotency_sync, idempotency_key)
                if prior:
                    return ReminderRequest(
                        reminder_id=prior["reminder_id"],
                        case_id=case_id,
                        channel=ch,
                        to=to,
                        english=prior["english"] or "",
                        hindi=prior["hindi"] or "",
                    )

        await asyncio.to_thread(_append_reminder_id_sync, self.db_path, case_id, ticket.approval_id)
        return ReminderRequest(
            reminder_id=reminder_id, case_id=case_id, channel=ch, to=to, english=summary.english, hindi=summary.hindi
        )

    async def approve(
        self,
        approval_id: str,
        by: str = "operator",
        reason: str = "",
    ) -> ApprovalTicket:
        await self._rate_limit()
        return await self.sentinel.approve_and_execute(approval_id, by, reason)

    async def reject(
        self,
        approval_id: str,
        by: str = "operator",
        reason: str = "",
    ) -> ApprovalTicket:
        await self._rate_limit()
        return await self.sentinel.reject(approval_id, by, reason)

    async def list_pending(self) -> List[ApprovalTicket]:
        return await self.sentinel.list_pending()

    async def receipts(self, case_id: str) -> List[Dict[str, Any]]:
        return await list_receipts(self.db_path, case_id)

    # ----- audit -----

    async def audit_tail(self, limit: int = 50) -> List[AuditEvent]:
        return await self.sentinel.audit_tail(limit)

    async def verify_chain(self) -> bool:
        return await self.sentinel.audit.verify_chain()

    # ----- health -----

    async def health(self) -> Dict[str, Any]:
        cases_total = await asyncio.to_thread(_count_cases, self.db_path)
        backend_ok = True
        backend_error: Optional[str] = None
        if self.backend_health is not None:
            try:
                backend_ok = await asyncio.wait_for(
                    self.backend_health(),  # type: ignore[misc]
                    timeout=self.config.health_timeout_seconds,
                )
            except asyncio.TimeoutError:
                backend_ok = False
                backend_error = f"backend_health timed out after {self.config.health_timeout_seconds}s"
            except Exception as exc:
                backend_ok = False
                backend_error = repr(exc)
        return {
            "ok": backend_ok,
            "details": {
                "node_id": self.node_id,
                "engine_started": self._started,
                "cases_total": cases_total,
                "backend_sync_configured": self.backend_sync is not None,
                "backend_health_configured": self.backend_health is not None,
                "backend_error": backend_error,
                "extraction_backend": self.extractor.describe()["backend"],
                "audit_chain_ok": await self.verify_chain(),
                "rate_limit_enabled": self.config.rate_limit_enabled,
                "rate_limit_per_second": self.config.rate_limit_per_second,
            },
        }

    # ----- RPC dispatcher -----

    def _require(self, payload: Dict[str, Any], *keys: str) -> None:
        missing = [k for k in keys if k not in payload or payload[k] is None]
        if missing:
            raise ValueError(f"missing required payload field(s): {', '.join(missing)}")

    async def handle(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        await self._rate_limit()
        if action == "ingest":
            self._require(payload, "text")
            record = await self.ingest_text(payload.get("filename", "inline.txt"), payload["text"])
            return {"case": record.to_dict()}
        if action == "get_case":
            self._require(payload, "case_id")
            record = await self.get_case(payload["case_id"])
            if record is None:
                raise KeyError(payload["case_id"])
            return {"case": record.to_dict()}
        if action == "queue_reminder":
            self._require(payload, "case_id", "phone")
            req = await self.queue_reminder(
                payload["case_id"],
                payload.get("channel", "whatsapp"),
                payload["phone"],
                idempotency_key=payload.get("idempotency_key"),
            )
            return {"reminder": req.to_dict()}
        if action == "approve":
            self._require(payload, "approval_id")
            ticket = await self.approve(
                payload["approval_id"], by=payload.get("by", "operator"), reason=payload.get("reason", "")
            )
            return {"approval": ticket.to_dict()}
        if action == "reject":
            self._require(payload, "approval_id")
            ticket = await self.reject(
                payload["approval_id"], by=payload.get("by", "operator"), reason=payload.get("reason", "")
            )
            return {"approval": ticket.to_dict()}
        if action == "list_pending":
            pending = await self.list_pending()
            return {"pending": [t.to_dict() for t in pending]}
        if action == "audit":
            events = await self.audit_tail(int(payload.get("limit", 50)))
            return {
                "events": [e.to_dict() for e in events],
                "chain": {"audit_chain_ok": await self.verify_chain()},
            }
        if action == "health":
            return await self.health()
        raise ValueError(f"unknown action: {action}")


# ---------------------------------------------------------------------------
# Metrics harness
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SeedCase:
    id: str
    filename: str
    text: str
    expected: Dict[str, Optional[str]]
    phone: str
    channel: ReminderChannel = ReminderChannel.WHATSAPP


def load_seed_cases(path: str) -> List[SeedCase]:
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    return [
        SeedCase(
            id=item["id"],
            filename=item["filename"],
            text=item["text"],
            expected=item["expected"],
            phone=item.get("phone", "+919000000000"),
            channel=ReminderChannel(item.get("channel", "whatsapp")),
        )
        for item in payload
    ]


def _compare(expected: Optional[str], actual: Optional[str]) -> bool:
    if expected is None and actual is None:
        return True
    if expected is None or actual is None:
        return False
    return expected.strip().lower() == actual.strip().lower()


async def _run_one(seed: SeedCase, engine: CivicAgent) -> CaseMetric:
    t0 = time.perf_counter()
    record = await engine.ingest_text(seed.filename, seed.text)
    fields = [
        FieldMatch(
            "case_number",
            seed.expected.get("case_number"),
            record.case_number.value,
            _compare(seed.expected.get("case_number"), record.case_number.value),
        ),
        FieldMatch(
            "court",
            seed.expected.get("court"),
            record.court.value,
            _compare(seed.expected.get("court"), record.court.value),
        ),
        FieldMatch(
            "next_hearing",
            seed.expected.get("next_hearing"),
            record.next_hearing.value,
            _compare(seed.expected.get("next_hearing"), record.next_hearing.value),
        ),
        FieldMatch(
            "required_action",
            seed.expected.get("required_action"),
            record.required_action.value,
            _compare(seed.expected.get("required_action"), record.required_action.value),
        ),
    ]
    reminder_delivered = False
    reminder_error: Optional[str] = None
    try:
        req = await engine.queue_reminder(record.case_id, seed.channel.value, seed.phone)
        # Re-read the case to get the freshest approval id.
        fresh = await engine.get_case(record.case_id)
        if fresh is None or not fresh.reminder_ids:
            raise BharatAgentError("reminder id was not recorded on case")
        ticket_id = fresh.reminder_ids[-1]
        await engine.approve(ticket_id, by="metrics-harness", reason="auto-approved in metrics run")
        receipts = await engine.receipts(record.case_id)
        reminder_delivered = bool(receipts and receipts[0]["delivered"])
    except Exception as exc:
        reminder_error = repr(exc)
        logger.warning("metrics_reminder_failed case=%s err=%r", seed.id, exc)
    elapsed = time.perf_counter() - t0
    metric = CaseMetric(
        case_id=seed.id,
        processing_seconds=elapsed,
        fields=fields,
        reminder_delivered=reminder_delivered,
        reminder_channel=seed.channel.value,
    )
    if reminder_error:
        # Attach to `field` list as a synthetic marker for visibility.
        metric.fields.append(FieldMatch("reminder_error", None, reminder_error, False))
    return metric


async def run_metrics(
    seed_path: str,
    temp_db: Optional[str] = None,
) -> MetricsReport:
    """Run the 20-case harness on a fresh temp DB and return a report."""
    from pathlib import Path

    # Resolve seed path robustly
    seed_file = Path(seed_path)
    if not seed_file.is_file():
        # Try common locations
        candidates = [
            Path.cwd() / seed_path,
            Path(__file__).resolve().parent.parent / "seed_cases" / seed_path,
            Path(__file__).resolve().parent.parent.parent / "seed_cases" / seed_path,
            Path(__file__).resolve().parent.parent.parent.parent / "seed_cases" / seed_path,
        ]
        for c in candidates:
            if c.is_file():
                seed_file = c
                break

    cleanup = temp_db is None
    if temp_db is None:
        fd, temp_db = tempfile.mkstemp(suffix=".db", prefix="bharat_metrics_")
        os.close(fd)
    if os.path.exists(temp_db):
        try:
            os.remove(temp_db)
        except OSError:
            pass
    try:
        config = EngineConfig(node_id="metrics-runner", db_path=temp_db)
        config.validate()
        async with CivicAgent(
            node_id="metrics-runner",
            db_path=temp_db,
            config=config,
        ) as engine:
            seeds = load_seed_cases(str(seed_file))
            results = [await _run_one(s, engine) for s in seeds]
        return _summarise(results)
    finally:
        if cleanup and os.path.exists(temp_db):
            try:
                os.remove(temp_db)
            except OSError:
                pass


def _summarise(results: List[CaseMetric]) -> MetricsReport:
    total = sum(len(r.fields) for r in results)
    correct = sum(1 for r in results for f in r.fields if f.matched)
    per_field: Dict[str, float] = {}
    for name in ("case_number", "court", "next_hearing", "required_action"):
        matches = [f.matched for r in results for f in r.fields if f.field == name]
        per_field[name] = sum(1 for m in matches if m) / len(matches) if matches else 0.0
    delivered = sum(1 for r in results if r.reminder_delivered)
    return MetricsReport(
        generated_at=time.time(),
        total_cases=len(results),
        extraction_accuracy=correct / total if total else 0.0,
        per_field_accuracy=per_field,
        reminders_sent=len(results),
        reminder_delivery_rate=delivered / len(results) if results else 0.0,
        average_processing_time_seconds=(sum(r.processing_seconds for r in results) / len(results) if results else 0.0),
        cases=results,
    )


# ---------------------------------------------------------------------------
# Module-level convenience singleton
# ---------------------------------------------------------------------------

_engine: Optional[CivicAgent] = None


async def init_engine(
    node_id: str,
    db_path: str = "./bharat_civic.db",
    backend_sync: Optional[BackendSync] = None,
    backend_health: Optional[BackendHealth] = None,
    config: Optional[EngineConfig] = None,
) -> CivicAgent:
    global _engine
    if _engine is not None:
        await _engine.stop()
    _engine = CivicAgent(
        node_id=node_id, db_path=db_path, backend_sync=backend_sync, backend_health=backend_health, config=config
    )
    await _engine.start()
    return _engine


def get_engine() -> Optional[CivicAgent]:
    return _engine


async def shutdown_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.stop()
        _engine = None
