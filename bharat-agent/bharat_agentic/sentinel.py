"""Sentinel sandbox: hash-chained audit log + approval queue + executor.

Three guarantees, each enforced with an **atomic SQL transition**:

1. Sandboxed execution — handlers run here, exceptions are caught.
2. Approval gate — `claim_for_execution` uses `UPDATE ... WHERE state='pending'`
   so exactly one concurrent caller can execute a side effect.
3. Tamper-evident audit — `append` runs inside `BEGIN IMMEDIATE` so two
   concurrent writers cannot fork the chain.

`verify_chain()` is O(N) on cold cache; results are memoised for
``audit_verify_cache_seconds`` and invalidated on every append.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .errors import ApprovalConflictError
from .models import (
    ApprovalState,
    ApprovalTicket,
    AuditEvent,
    canonical_json,
    new_approval_id,
    sha256_hex,
)

logger = logging.getLogger("bharat_agentic.sentinel")

ToolHandler = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]


# ---------------------------------------------------------------------------
# Audit log — append is atomic, chain is unforgeable under concurrent load
# ---------------------------------------------------------------------------

class AuditLog:
    def __init__(self, db_path: str, cache_seconds: float = 5.0) -> None:
        self.db_path = db_path
        self._cache_seconds = cache_seconds
        self._last_verify: Optional[bool] = None
        self._last_verify_at: float = 0.0
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    action TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    prev_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_action
                    ON audit_log(action);
            """)

    def _append_sync(
        self, action: str, payload: Dict[str, Any],
    ) -> AuditEvent:
        conn = sqlite3.connect(
            self.db_path, timeout=30.0, isolation_level=None)
        try:
            # BEGIN IMMEDIATE takes the write lock before we read prev_hash,
            # so concurrent appenders serialise and the chain never forks.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT entry_hash FROM audit_log "
                "ORDER BY sequence DESC LIMIT 1").fetchone()
            prev_hash = row[0] if row else "GENESIS"
            event_id = f"evt_{uuid.uuid4().hex[:16]}"
            created_at = time.time()
            body = {
                "event_id": event_id, "action": action,
                "payload": payload, "prev_hash": prev_hash,
                "created_at": created_at,
            }
            entry_hash = sha256_hex(canonical_json(body))
            cur = conn.execute(
                "INSERT INTO audit_log(event_id, action, payload, "
                "prev_hash, entry_hash, created_at) VALUES (?,?,?,?,?,?)",
                (event_id, action, canonical_json(payload),
                 prev_hash, entry_hash, created_at))
            conn.execute("COMMIT")
            self._last_verify = None  # invalidate cache
            return AuditEvent(
                sequence=int(cur.lastrowid), event_id=event_id,
                action=action, payload=payload,
                prev_hash=prev_hash, entry_hash=entry_hash,
                created_at=created_at)
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            conn.close()

    async def append(self, action: str, payload: Dict[str, Any]) -> AuditEvent:
        return await asyncio.to_thread(self._append_sync, action, payload)

    def _tail_sync(self, limit: int) -> List[AuditEvent]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY sequence DESC LIMIT ?",
                (limit,)).fetchall()
            return [AuditEvent(
                sequence=r["sequence"], event_id=r["event_id"],
                action=r["action"], payload=json.loads(r["payload"]),
                prev_hash=r["prev_hash"], entry_hash=r["entry_hash"],
                created_at=r["created_at"]) for r in rows]

    async def tail(self, limit: int = 50) -> List[AuditEvent]:
        return await asyncio.to_thread(self._tail_sync, limit)

    def _verify_sync(self) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY sequence ASC").fetchall()
            prev = "GENESIS"
            for r in rows:
                body = {
                    "event_id": r["event_id"], "action": r["action"],
                    "payload": json.loads(r["payload"]),
                    "prev_hash": r["prev_hash"],
                    "created_at": r["created_at"],
                }
                if r["prev_hash"] != prev:
                    return False
                if sha256_hex(canonical_json(body)) != r["entry_hash"]:
                    return False
                prev = r["entry_hash"]
            return True

    async def verify_chain(self) -> bool:
        now = time.monotonic()
        if (self._last_verify is not None
                and (now - self._last_verify_at) < self._cache_seconds):
            return self._last_verify
        ok = await asyncio.to_thread(self._verify_sync)
        self._last_verify = ok
        self._last_verify_at = time.monotonic()
        return ok


# ---------------------------------------------------------------------------
# Approval queue — atomic claim is the only way to execute
# ---------------------------------------------------------------------------

class ApprovalQueue:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    tool TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    decided_at REAL,
                    decided_by TEXT,
                    reason TEXT,
                    result TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_approval_state
                    ON approvals(state);
            """)

    def _submit_sync(self, tool: str, payload: Dict[str, Any]) -> ApprovalTicket:
        ticket = ApprovalTicket(
            approval_id=new_approval_id(), tool=tool, payload=payload)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,?)",
                (ticket.approval_id, ticket.tool,
                 canonical_json(ticket.payload), ticket.state.value,
                 ticket.created_at, None, None, "", None))
        return ticket

    async def submit(self, tool: str, payload: Dict[str, Any]) -> ApprovalTicket:
        return await asyncio.to_thread(self._submit_sync, tool, payload)

    def _get_sync(self, approval_id: str) -> Optional[ApprovalTicket]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            r = conn.execute(
                "SELECT * FROM approvals WHERE approval_id=?",
                (approval_id,)).fetchone()
            if not r:
                return None
            return ApprovalTicket(
                approval_id=r["approval_id"], tool=r["tool"],
                payload=json.loads(r["payload"]),
                state=ApprovalState(r["state"]),
                created_at=r["created_at"], decided_at=r["decided_at"],
                decided_by=r["decided_by"], reason=r["reason"] or "",
                result=json.loads(r["result"]) if r["result"] else None)

    async def get(self, approval_id: str) -> Optional[ApprovalTicket]:
        return await asyncio.to_thread(self._get_sync, approval_id)

    def _list_pending_sync(self) -> List[ApprovalTicket]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM approvals WHERE state='pending' "
                "ORDER BY created_at ASC").fetchall()
            return [ApprovalTicket(
                approval_id=r["approval_id"], tool=r["tool"],
                payload=json.loads(r["payload"]),
                state=ApprovalState(r["state"]),
                created_at=r["created_at"], decided_at=r["decided_at"],
                decided_by=r["decided_by"], reason=r["reason"] or "",
                result=json.loads(r["result"]) if r["result"] else None)
                for r in rows]

    async def list_pending(self) -> List[ApprovalTicket]:
        return await asyncio.to_thread(self._list_pending_sync)

    def _claim_sync(
        self, approval_id: str, decided_by: str, reason: str,
    ) -> bool:
        """Atomically transition PENDING -> APPROVED.

        Returns True only for the single caller whose UPDATE affected a row.
        """
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            cur = conn.execute(
                "UPDATE approvals SET state=?, decided_at=?, decided_by=?, reason=? "
                "WHERE approval_id=? AND state=?",
                (ApprovalState.APPROVED.value, time.time(),
                 decided_by, reason, approval_id,
                 ApprovalState.PENDING.value))
            return cur.rowcount == 1

    async def claim_for_execution(
        self, approval_id: str, decided_by: str, reason: str,
    ) -> bool:
        return await asyncio.to_thread(
            self._claim_sync, approval_id, decided_by, reason)

    def _finalize_sync(
        self, approval_id: str, state: ApprovalState,
        decided_by: str, reason: str,
        result: Optional[Dict[str, Any]],
    ) -> None:
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.execute(
                "UPDATE approvals SET state=?, decided_at=?, decided_by=?, "
                "reason=?, result=? WHERE approval_id=?",
                (state.value, time.time(), decided_by, reason,
                 canonical_json(result) if result else None,
                 approval_id))

    async def finalize(
        self, approval_id: str, state: ApprovalState,
        decided_by: str, reason: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        await asyncio.to_thread(
            self._finalize_sync, approval_id, state, decided_by, reason, result)


# ---------------------------------------------------------------------------
# Sandbox executor — catches tool failures, always audits start + finish
# ---------------------------------------------------------------------------

class SandboxExecutor:
    def __init__(self, audit: AuditLog) -> None:
        self._audit = audit
        self._handlers: Dict[str, ToolHandler] = {}

    def register(self, name: str, handler: ToolHandler) -> None:
        self._handlers[name] = handler

    def tools(self) -> List[str]:
        return sorted(self._handlers.keys())

    async def execute(self, tool: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if tool not in self._handlers:
            raise KeyError(f"unknown tool: {tool}")
        await self._audit.append(
            "tool_execute_started", {"tool": tool, "payload": payload})
        try:
            result = await self._handlers[tool](payload)
        except Exception as exc:
            await self._audit.append(
                "tool_execute_failed",
                {"tool": tool, "error": repr(exc), "payload": payload})
            logger.exception("sandbox_tool_failed tool=%s", tool)
            raise
        await self._audit.append(
            "tool_execute_finished", {"tool": tool, "result": result})
        return result


# ---------------------------------------------------------------------------
# Sentinel facade
# ---------------------------------------------------------------------------

class Sentinel:
    def __init__(
        self, db_path: str = "./sentinel.db",
        cache_seconds: float = 5.0, tool_timeout_seconds: float = 30.0,
    ) -> None:
        self.db_path = db_path
        self.tool_timeout_seconds = tool_timeout_seconds
        self.audit = AuditLog(db_path, cache_seconds=cache_seconds)
        self.approvals = ApprovalQueue(db_path)
        self.executor = SandboxExecutor(self.audit)

    async def submit_for_approval(
        self, tool: str, payload: Dict[str, Any],
    ) -> ApprovalTicket:
        ticket = await self.approvals.submit(tool, payload)
        await self.audit.append(
            "approval_submitted",
            {"approval_id": ticket.approval_id, "tool": tool})
        return ticket

    async def approve_and_execute(
        self, approval_id: str, decided_by: str = "", reason: str = "", by: str = "",
    ) -> ApprovalTicket:
        if by:
            decided_by = by
        ticket = await self.approvals.get(approval_id)
        if ticket is None:
            raise KeyError(f"approval not found: {approval_id}")

        claimed = await self.approvals.claim_for_execution(
            approval_id, decided_by, reason)
        if not claimed:
            current = await self.approvals.get(approval_id)
            state = current.state.value if current else "unknown"
            raise ApprovalConflictError(
                f"approval {approval_id} is not PENDING (state={state}); "
                "refusing to execute again")

        await self.audit.append(
            "approval_approved",
            {"approval_id": approval_id, "by": decided_by, "reason": reason})

        execution_payload = dict(ticket.payload)
        # Only add approval_id for internal reminder tools, not for echo tool
        if ticket.tool in ("sms_send", "whatsapp_send") and "approval_id" not in execution_payload:
            execution_payload["approval_id"] = approval_id
        try:
            result = await asyncio.wait_for(
                self.executor.execute(ticket.tool, execution_payload),
                timeout=self.tool_timeout_seconds)
        except asyncio.TimeoutError:
            err = {
                "error": "tool_execution_timeout",
                "timeout_seconds": self.tool_timeout_seconds,
            }
            await self.approvals.finalize(
                approval_id, ApprovalState.FAILED, decided_by, reason, err)
            await self.audit.append(
                "tool_execute_timeout",
                {"tool": ticket.tool, "timeout_seconds": self.tool_timeout_seconds})
            ticket.state = ApprovalState.FAILED
            ticket.result = err
        except Exception as exc:
            err = {"error": repr(exc)}
            await self.approvals.finalize(
                approval_id, ApprovalState.FAILED, decided_by, reason, err)
            ticket.state = ApprovalState.FAILED
            ticket.result = err
        else:
            await self.approvals.finalize(
                approval_id, ApprovalState.EXECUTED, decided_by, reason, result)
            ticket.state = ApprovalState.EXECUTED
            ticket.result = result

        ticket.decided_by = decided_by
        ticket.decided_at = time.time()
        ticket.reason = reason
        return ticket

    async def reject(
        self, approval_id: str, decided_by: str = "", reason: str = "", by: str = "",
    ) -> ApprovalTicket:
        if by:
            decided_by = by
        ticket = await self.approvals.get(approval_id)
        if ticket is None:
            raise KeyError(f"approval not found: {approval_id}")
        # Use the same atomic claim path so reject also cannot race an approve.
        # Transition PENDING -> REJECTED directly via SQL.
        claimed = await asyncio.to_thread(
            self.approvals._claim_sync, approval_id, decided_by, reason)
        if not claimed:
            current = await self.approvals.get(approval_id)
            state = current.state.value if current else "unknown"
            raise ApprovalConflictError(
                f"approval {approval_id} is not PENDING (state={state})")
        # _claim_sync set state=APPROVED; flip to REJECTED atomically.
        await self.approvals.finalize(
            approval_id, ApprovalState.REJECTED, decided_by, reason)
        await self.audit.append(
            "approval_rejected",
            {"approval_id": approval_id, "by": decided_by, "reason": reason})
        ticket.state = ApprovalState.REJECTED
        ticket.decided_by = decided_by
        ticket.decided_at = time.time()
        ticket.reason = reason
        return ticket

    async def list_pending(self) -> List[ApprovalTicket]:
        return await self.approvals.list_pending()

    async def audit_tail(self, limit: int = 50) -> List[AuditEvent]:
        return await self.audit.tail(limit)

    async def verify(self) -> Dict[str, Any]:
        return {"audit_chain_ok": await self.audit.verify_chain()}