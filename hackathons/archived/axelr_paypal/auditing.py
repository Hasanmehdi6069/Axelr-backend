"""
Axelr Billing Auditing - Comprehensive audit logging for financial operations
Implements immutable audit trails for all billing transactions
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("axelr.billing.auditing")


@dataclass(slots=True)
class AuditEntry:
    """Immutable audit log entry for billing operations"""

    id: str
    operation: str
    user_id: str
    session_id: str
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_ms: Optional[float] = None
    success: bool = False
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def complete(self, success: bool = True, error: Optional[str] = None) -> None:
        """Mark audit entry as completed with results"""
        self.end_time = datetime.now(timezone.utc)
        self.duration_ms = (self.end_time - self.start_time).total_seconds() * 1000
        self.success = success
        self.error = error

        # Log the completed entry
        log_msg = (
            f"AUDIT: {self.operation} | user={self.user_id} | session={self.session_id} | "
            f"duration={self.duration_ms:.2f}ms | success={success}"
        )
        if error:
            log_msg += f" | error={error}"
            logger.warning(log_msg)
        else:
            logger.info(log_msg)


# In-memory audit log (in production, write to immutable distributed log)
_audit_log: List[AuditEntry] = []
_audit_lock = asyncio.Lock()


@asynccontextmanager
async def AuditTimer(operation: str, user_id: str, session_id: str, **metadata):
    """
    Async context manager for auditing billing operations.
    Automatically logs start, completion, duration, and success/failure.
    """
    import uuid

    entry = AuditEntry(
        id=f"audit_{uuid.uuid4().hex[:16]}",
        operation=operation,
        user_id=user_id,
        session_id=session_id,
        start_time=datetime.now(timezone.utc),
        metadata=metadata,
    )

    async with _audit_lock:
        _audit_log.append(entry)

    logger.debug(f"Starting audit: {operation} | user={user_id} | session={session_id}")

    try:
        yield entry
        entry.complete(success=True)
    except Exception as e:
        entry.complete(success=False, error=str(e))
        raise


def get_audit_logs(
    user_id: Optional[str] = None, operation: Optional[str] = None, limit: int = 100
) -> List[AuditEntry]:
    """Query audit logs with optional filtering"""
    results = _audit_log.copy()

    if user_id:
        results = [e for e in results if e.user_id == user_id]
    if operation:
        results = [e for e in results if e.operation == operation]

    # Return most recent first
    results.sort(key=lambda e: e.start_time, reverse=True)
    return results[:limit]


def get_audit_stats() -> Dict[str, Any]:
    """Get statistics about audit logs for health monitoring"""
    total_entries = len(_audit_log)
    operations = {}
    success_rate = 0.0
    if total_entries > 0:
        successful = sum(1 for e in _audit_log if e.success)
        success_rate = successful / total_entries
        # Count occurrences of each operation
        for entry in _audit_log:
            if entry.operation not in operations:
                operations[entry.operation] = 0
            operations[entry.operation] += 1

    return {
        "total_entries": total_entries,
        "operations": operations,
        "success_rate": round(success_rate, 4),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


async def log_audit_event(event: Dict[str, Any]) -> None:
    """Directly log an audit event without the timer for one-off events like AI extraction"""
    import uuid

    entry = AuditEntry(
        id=f"audit_{uuid.uuid4().hex[:16]}",
        operation=event.get("event_type", "unknown"),
        user_id=event.get("user_id", "anon"),
        session_id=event.get("session_id", f"session_{uuid.uuid4().hex[:8]}"),
        start_time=datetime.now(timezone.utc),
        metadata=event.get("metadata", {}),
    )
    entry.complete(success=True)

    async with _audit_lock:
        _audit_log.append(entry)

    logger.debug(f"Logged audit event: {entry.operation} | id={entry.id}")
