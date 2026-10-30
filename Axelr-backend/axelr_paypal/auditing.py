# axelr_paypal/auditing.py
"""Thin bridge to the Axelr Insights Engine audit pipeline.

All calls are best-effort — never block a payment operation on audit writes.
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("axelr.paypal.auditing")


async def log_paypal_op(
    *,
    action: str,
    user_id: str,
    session_id: str,
    node_id: str = "paypal",
    pipeline_id: str = "paypal_pipeline",
    latency_ms: float | None = None,
    status: str = "ok",
    error: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Dual-write: transaction + node execution. Silent on failure."""
    try:
        from Axelr_insights.engine.auditing import (
            log_node_execution, log_transaction,
        )
    except Exception as e:
        logger.debug("paypal_audit_import_failed err=%s", e)
        return

    try:
        await log_transaction(
            user_id=user_id,
            session_id=session_id,
            action=action,
            details=details or {},
        )
    except Exception as e:
        logger.debug("paypal_audit_tx_failed err=%s", e)

    if latency_ms is not None:
        try:
            await log_node_execution(
                node_id=node_id,
                pipeline_id=pipeline_id,
                latency_ms=latency_ms,
                status=status,
                error=error,
                metadata=details or {},
            )
        except Exception as e:
            logger.debug("paypal_audit_node_failed err=%s", e)


class AuditTimer:
    """Context manager — records latency + status into the audit stream."""

    def __init__(self, action: str, **details: Any) -> None:
        self.action = action
        self.details = details
        self._t0 = 0.0

    async def __aenter__(self) -> "AuditTimer":
        self._t0 = time.monotonic()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        latency = (time.monotonic() - self._t0) * 1000
        status = "ok" if exc is None else "error"
        await log_paypal_op(
            action=self.action,
            user_id=str(self.details.get("user_id", "system")),
            session_id=str(self.details.get("session_id", "paypal")),
            latency_ms=latency,
            status=status,
            error=str(exc)[:200] if exc else None,
            details=self.details,
        )