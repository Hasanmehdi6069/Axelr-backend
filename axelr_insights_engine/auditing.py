"""Audit façade — backend-agnostic.

Every function is a thin, documented wrapper over `get_backend()`. No
branching on backend here; no SDK calls; no serialization. If you find
yourself wanting to add `if appwrite:`, the abstraction has leaked and
the fix belongs in `storage_base` or the backend module, not here.

Public API
----------
Writes:   log_transaction, log_node_execution, log_pipeline_state,
          log_system_metric, log_batch_events
Reads:    list_transactions, get_latency_report, get_pipeline_summary,
          get_latest_pipeline_state, get_daily_event_counts
Extras:   detect_pipeline_anomaly (portable across backends)
Retention: purge_expired_audit
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import settings
from .storage_base import get_backend

logger = logging.getLogger("insights.audit")

KIND_TRANSACTION = "transaction"
KIND_NODE_EXECUTION = "node_execution"
KIND_PIPELINE_STATE = "pipeline_state"
KIND_SYSTEM_METRIC = "system_metric"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Writes ────────────────────────────────────────────────────────────

async def log_transaction(
    *, user_id: str, session_id: str, action: str,
    details: dict[str, Any] | None = None,
) -> str | None:
    """Record a user-initiated transaction."""
    return await get_backend().insert_audit(KIND_TRANSACTION, {
        "user_id": user_id,
        "session_id": session_id,
        "action": action,
        "details": details or {},
        "ts": _now(),
    })


async def log_node_execution(
    *, node_id: str, pipeline_id: str, latency_ms: float, status: str,
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str | None:
    """Record the execution of a pipeline node with performance metrics."""
    return await get_backend().insert_audit(KIND_NODE_EXECUTION, {
        "node_id": node_id,
        "pipeline_id": pipeline_id,
        "latency_ms": float(latency_ms),
        "status": status,
        "error": error,
        "metadata": metadata or {},
        "ts": _now(),
    })


async def log_pipeline_state(
    *, pipeline_id: str, state_snapshot: dict[str, Any],
) -> str | None:
    """Record a complete state snapshot for a pipeline."""
    return await get_backend().insert_audit(KIND_PIPELINE_STATE, {
        "pipeline_id": pipeline_id,
        "state": state_snapshot,
        "ts": _now(),
    })


async def log_system_metric(
    *, metric_name: str, metric_value: float,
    dimensions: dict[str, str] | None = None,
) -> str | None:
    """Record a system-level metric."""
    return await get_backend().insert_audit(KIND_SYSTEM_METRIC, {
        "metric_name": metric_name,
        "metric_value": metric_value,
        "dimensions": dimensions or {},
        "ts": _now(),
    })


async def log_batch_events(events: list[dict[str, Any]]) -> int:
    """Bulk insert. Non-dict entries are skipped, not coerced."""
    if not events:
        return 0
    now = _now()
    normalised: list[dict[str, Any]] = []
    for e in events:
        if not isinstance(e, dict):
            continue
        ev = dict(e)
        ev.setdefault("ts", now)
        ev.setdefault("kind", "unknown")
        normalised.append(ev)
    return await get_backend().insert_audit_batch(normalised)


# ── Reads ─────────────────────────────────────────────────────────────

async def list_transactions(
    *, limit: int = 50, user_id: str | None = None,
    action: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> list[dict[str, Any]]:
    """Query historical transaction events with flexible filtering."""
    rows = await get_backend().list_audit(
        kind=KIND_TRANSACTION, limit=limit, user_id=user_id,
        start=start_time, end=end_time,
    )
    if action:
        rows = [r for r in rows if r.get("action") == action]
    return rows


async def get_latency_report(
    *, pipeline_id: str | None = None, window_minutes: int = 60,
) -> dict[str, Any]:
    """Percentile latency report over the given window."""
    since = _now() - timedelta(minutes=window_minutes)
    stats = await get_backend().latency_stats(
        pipeline_id=pipeline_id, since=since,
    )
    return {"window_minutes": window_minutes, "pipeline_id": pipeline_id,
            **stats}


async def get_pipeline_summary(
    window_minutes: int = 60, limit: int = 50,
) -> list[dict[str, Any]]:
    """Health + performance summary for every pipeline in the window."""
    since = _now() - timedelta(minutes=window_minutes)
    return await get_backend().pipeline_stats(since=since, limit=limit)


async def get_latest_pipeline_state(
    pipeline_id: str,
) -> dict[str, Any] | None:
    return await get_backend().latest_pipeline_state(pipeline_id=pipeline_id)


async def get_daily_event_counts(days: int = 7) -> list[dict[str, Any]]:
    since = _now() - timedelta(days=days)
    return await get_backend().daily_counts(since=since)


# ── Anomaly detection ─────────────────────────────────────────────────

async def detect_pipeline_anomaly(
    pipeline_id: str, sensitivity: float = 2.0,
) -> dict[str, Any]:
    """3-sigma style anomaly detection using both backends' latency APIs.

    Honest note: `std ≈ (max − min) / 6` assumes roughly normal tails
    over a week of data. For heavy-tail latency distributions this
    under-detects. Good enough for a coarse "is it degraded?" signal;
    do not use for SLO alerting.
    """
    baseline = await get_latency_report(
        pipeline_id=pipeline_id, window_minutes=10080,
    )
    recent = await get_latency_report(
        pipeline_id=pipeline_id, window_minutes=60,
    )
    if (baseline["count"] or 0) < 100 or (recent["count"] or 0) < 10:
        return {
            "has_anomaly": False, "reason": "insufficient_data",
            "baseline_count": baseline["count"],
            "recent_count": recent["count"],
            "pipeline_id": pipeline_id,
        }
    mean, lo, hi = baseline["mean"], baseline["min"], baseline["max"]
    std = (hi - lo) / 6 if (hi is not None and lo is not None) else 0.0
    threshold = mean + sensitivity * std
    return {
        "has_anomaly": recent["mean"] > threshold,
        "pipeline_id": pipeline_id,
        "baseline_mean_ms": mean,
        "recent_mean_ms": recent["mean"],
        "threshold_ms": round(threshold, 2),
        "sensitivity": sensitivity,
    }


# ── Retention ─────────────────────────────────────────────────────────

async def purge_expired_audit(days: int | None = None) -> int:
    """Remove audit records older than the retention window.

    Mongo enforces this via TTL, so the call is a no-op there. Appwrite
    has no TTL, so this is the actual delete path.
    """
    return await get_backend().purge_audit(
        older_than_days=(
            days if days is not None else settings.audit_retention_days
        ),
    )