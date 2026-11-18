# axelr_vultr/auditing.py v3.0.0 - HackGlobal Elite Tier
"""Elite audit system — every Vultr operation lands in Axelr Insights with full observability, metrics tracking, and audit persistence."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger("axelr.vultr.auditing")


class AuditSeverity(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AuditCategory(str, Enum):
    API_CALL = "api_call"
    INSTANCE_CREATE = "instance_create"
    INSTANCE_DELETE = "instance_delete"
    INSTANCE_UPDATE = "instance_update"
    DEPLOYMENT = "deployment"
    HEALTH_CHECK = "health_check"
    RECOVERY = "recovery"
    COST_OPTIMIZATION = "cost_optimization"
    CACHE_OPERATION = "cache_operation"
    SECURITY = "security"
    SYSTEM = "system"


@dataclass(frozen=True)
class AuditEntry:
    audit_id: str
    timestamp: str
    action: str
    category: AuditCategory
    severity: AuditSeverity
    user_id: str
    session_id: str
    node_id: str
    pipeline_id: str
    latency_ms: float | None
    status: str
    error: str | None
    details: dict[str, Any]
    version: str = "3.0.0-elite"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))


class AuditStore:
    def __init__(self, max_entries: int = 10000):
        self._entries: list[AuditEntry] = []
        self._max_entries = max_entries
        self._lock = asyncio.Lock()
        self._metrics: dict[str, dict[str, Any]] = {}

    async def add(self, entry: AuditEntry) -> None:
        async with self._lock:
            self._entries.append(entry)
            if len(self._entries) > self._max_entries:
                self._entries.pop(0)
            self._update_metrics(entry)

    def _update_metrics(self, entry: AuditEntry) -> None:
        action_key = f"{entry.category.value}:{entry.action}"
        if action_key not in self._metrics:
            self._metrics[action_key] = {
                "count": 0, "success_count": 0, "error_count": 0,
                "total_latency_ms": 0.0, "min_latency_ms": float("inf"),
                "max_latency_ms": 0.0, "avg_latency_ms": 0.0,
                "p95_latency_ms": 0.0, "p99_latency_ms": 0.0,
            }
        m = self._metrics[action_key]
        m["count"] += 1
        if entry.status == "ok":
            m["success_count"] += 1
        else:
            m["error_count"] += 1
        if entry.latency_ms is not None:
            m["total_latency_ms"] += entry.latency_ms
            m["min_latency_ms"] = min(m["min_latency_ms"], entry.latency_ms)
            m["max_latency_ms"] = max(m["max_latency_ms"], entry.latency_ms)
            m["avg_latency_ms"] = m["total_latency_ms"] / m["count"]

    def get_recent(self, limit: int = 100, category: AuditCategory | None = None, status: str | None = None) -> list[AuditEntry]:
        filtered = self._entries
        if category:
            filtered = [e for e in filtered if e.category == category]
        if status:
            filtered = [e for e in filtered if e.status == status]
        return list(reversed(filtered[-limit:]))

    def get_metrics(self) -> dict[str, dict[str, Any]]:
        return dict(self._metrics)

    def get_summary(self) -> dict[str, Any]:
        total = len(self._entries)
        success = sum(1 for e in self._entries if e.status == "ok")
        error = total - success
        categories = {}
        for e in self._entries:
            cat = e.category.value
            categories[cat] = categories.get(cat, 0) + 1
        return {
            "total_entries": total, "success_count": success, "error_count": error,
            "success_rate": round(success / total * 100, 2) if total > 0 else 0.0,
            "categories": categories, "metrics_count": len(self._metrics),
        }


AUDIT_STORE = AuditStore()


def _norm_error(err: Any) -> str | None:
    if err is None:
        return None
    if isinstance(err, str):
        return err[:1000]
    try:
        return json.dumps(err, default=str)[:1000]
    except Exception:
        return str(err)[:1000]


async def log_vultr_op(
    *,
    action: str,
    category: AuditCategory = AuditCategory.API_CALL,
    severity: AuditSeverity = AuditSeverity.INFO,
    user_id: str = "system",
    session_id: str = "vultr",
    node_id: str = "vultr",
    pipeline_id: str = "vultr_compute",
    latency_ms: float | None = None,
    status: str = "ok",
    error: Any = None,
    details: dict[str, Any] | None = None,
) -> AuditEntry:
    audit_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()
    entry = AuditEntry(
        audit_id=audit_id, timestamp=timestamp, action=action, category=category,
        severity=severity, user_id=user_id, session_id=session_id, node_id=node_id,
        pipeline_id=pipeline_id, latency_ms=latency_ms, status=status,
        error=_norm_error(error), details=details or {},
    )
    logger.info("vultr_audit action=%s status=%s latency=%.2fms id=%s",
                action, status, latency_ms or 0, audit_id)
    await AUDIT_STORE.add(entry)
    try:
        from Axelr_insights.engine.auditing import log_node_execution, log_transaction
        await log_transaction(user_id=user_id, session_id=session_id, action=action, details=details or {})
        if latency_ms is not None:
            await log_node_execution(node_id=node_id, pipeline_id=pipeline_id, latency_ms=latency_ms,
                                    status=status, error=error, metadata=details or {})
    except Exception as e:
        logger.debug("vultr_insights_import_failed err=%s", e)
    return entry


class AuditTimer:
    def __init__(self, action: str, category: AuditCategory = AuditCategory.API_CALL,
                 severity: AuditSeverity = AuditSeverity.INFO, **details: Any) -> None:
        self.action = action
        self.category = category
        self.severity = severity
        self.details = details
        self._t0 = 0.0

    async def __aenter__(self) -> "AuditTimer":
        self._t0 = time.perf_counter()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        latency = (time.perf_counter() - self._t0) * 1000
        status = "ok" if exc is None else "error"
        error_msg = f"{exc_type.__name__}: {str(exc)}" if exc else None
        severity = AuditSeverity.ERROR if exc else self.severity
        await log_vultr_op(
            action=self.action, category=self.category, severity=severity,
            user_id=str(self.details.get("user_id", "system")),
            session_id=str(self.details.get("session_id", "vultr")),
            latency_ms=latency, status=status, error=error_msg, details=self.details,
        )


class EliteAuditLogger:
    @staticmethod
    async def log_instance_create(user_id: str, session_id: str, plan: str, region: str,
                                   instance_id: str | None = None, status: str = "ok", **kwargs) -> None:
        await log_vultr_op(
            action="instance_create", category=AuditCategory.INSTANCE_CREATE,
            severity=AuditSeverity.INFO, user_id=user_id, session_id=session_id,
            status=status, details={"plan": plan, "region": region, "instance_id": instance_id, **kwargs},
        )

    @staticmethod
    async def log_instance_delete(user_id: str, session_id: str, instance_id: str,
                                   status: str = "ok", **kwargs) -> None:
        await log_vultr_op(
            action="instance_delete", category=AuditCategory.INSTANCE_DELETE,
            severity=AuditSeverity.INFO, user_id=user_id, session_id=session_id,
            status=status, details={"instance_id": instance_id, **kwargs},
        )

    @staticmethod
    async def log_health_check(instance_id: str, health_status: str,
                                response_time_ms: float | None = None, **kwargs) -> None:
        await log_vultr_op(
            action="health_check", category=AuditCategory.HEALTH_CHECK,
            severity=AuditSeverity.INFO if health_status == "healthy" else AuditSeverity.WARNING,
            user_id="system", session_id="health_monitor",
            status=health_status, latency_ms=response_time_ms,
            details={"instance_id": instance_id, "health_status": health_status, **kwargs},
        )

    @staticmethod
    async def log_recovery_trigger(instance_id: str, reason: str,
                                    recovery_type: str, status: str = "ok", **kwargs) -> None:
        await log_vultr_op(
            action="recovery_trigger", category=AuditCategory.RECOVERY,
            severity=AuditSeverity.WARNING, user_id="system", session_id="auto_recovery",
            status=status, details={"instance_id": instance_id, "reason": reason,
                                     "recovery_type": recovery_type, **kwargs},
        )

    @staticmethod
    async def log_cost_optimization(user_id: str, session_id: str, savings_estimate_usd: float,
                                     recommendations: list[str], **kwargs) -> None:
        await log_vultr_op(
            action="cost_optimization", category=AuditCategory.COST_OPTIMIZATION,
            severity=AuditSeverity.INFO, user_id=user_id, session_id=session_id,
            details={"savings_estimate_usd": savings_estimate_usd, "recommendations": recommendations, **kwargs},
        )

    @staticmethod
    async def log_cache_operation(operation: str, key: str, hit: bool | None = None,
                                   status: str = "ok", **kwargs) -> None:
        await log_vultr_op(
            action=f"cache_{operation}", category=AuditCategory.CACHE_OPERATION,
            severity=AuditSeverity.DEBUG, user_id="system", session_id="cache",
            status=status, details={"operation": operation, "key": key, "hit": hit, **kwargs},
        )

    @staticmethod
    async def log_security_event(event_type: str, severity: AuditSeverity,
                                  details: dict[str, Any], **kwargs) -> None:
        await log_vultr_op(
            action=f"security_{event_type}", category=AuditCategory.SECURITY,
            severity=severity, user_id="system", session_id="security_hub",
            details={"event_type": event_type, **details, **kwargs},
        )

    @staticmethod
    async def log_deployment(user_id: str, session_id: str, blueprint: str,
                              region: str, count: int, status: str = "ok", **kwargs) -> None:
        await log_vultr_op(
            action="deployment", category=AuditCategory.DEPLOYMENT,
            severity=AuditSeverity.INFO, user_id=user_id, session_id=session_id,
            status=status, details={"blueprint": blueprint, "region": region, "count": count, **kwargs},
        )


def get_audit_store() -> AuditStore:
    return AUDIT_STORE