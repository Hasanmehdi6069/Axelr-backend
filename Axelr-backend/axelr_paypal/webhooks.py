# axelr_paypal/webhooks.py
"""Webhook signature verification + idempotent event dispatch."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Awaitable, Callable

from .client import get_client
from .core import settings

logger = logging.getLogger("axelr.paypal.webhooks")

# ── Idempotency store (Redis first, in-memory fallback) ─────────────────────

_IDEM_TTL_S = 86_400
_LOCAL_IDEM: dict[str, float] = {}
_IDEM_PREFIX = "axelr:paypal:idem:"


def _evict_local() -> None:
    now = time.time()
    stale = [k for k, ts in _LOCAL_IDEM.items() if now - ts > _IDEM_TTL_S]
    for k in stale:
        _LOCAL_IDEM.pop(k, None)


async def _seen_before(event_id: str) -> bool:
    """Return True if this event was already processed. Marks it if new."""
    # Try Redis via parent state
    try:
        from api.state import state  # type: ignore
        redis = getattr(state, "redis_client", None)
        if redis is not None:
            key = f"{_IDEM_PREFIX}{event_id}"
            # SET NX returns True on fresh key
            fresh = await redis.set(key, "1", ex=_IDEM_TTL_S, nx=True)
            return not bool(fresh)
    except Exception:
        pass
    # Fallback to in-memory
    _evict_local()
    if event_id in _LOCAL_IDEM:
        return True
    _LOCAL_IDEM[event_id] = time.time()
    return False


# ── Handlers ────────────────────────────────────────────────────────────────

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


async def _audit(action: str, event: dict[str, Any], **details: Any) -> None:
    try:
        from Axelr_insights.engine.auditing import log_transaction
        await log_transaction(
            user_id=details.get("user_id", "system"),
            session_id=details.get("session_id", "paypal_webhook"),
            action=action,
            details={"event_id": event.get("id"), **details},
        )
    except Exception as e:
        logger.debug("paypal_audit_skipped err=%s", e)


async def _handle_order_approved(event: dict[str, Any]) -> dict[str, Any]:
    resource = event.get("resource", {}) or {}
    order_id = resource.get("id")
    logger.info("paypal_event_order_approved order_id=%s", order_id)
    await _audit("paypal.order_approved", event, order_id=order_id)
    return {"order_id": order_id, "action": "log_only"}


async def _handle_capture_completed(event: dict[str, Any]) -> dict[str, Any]:
    resource = event.get("resource", {}) or {}
    capture_id = resource.get("id")
    amount = (resource.get("amount") or {})
    logger.info(
        "paypal_event_capture_completed capture_id=%s amount=%s %s",
        capture_id, amount.get("value"), amount.get("currency_code"),
    )
    await _audit(
        "paypal.capture_completed", event,
        capture_id=capture_id,
        amount=amount.get("value"),
        currency=amount.get("currency_code"),
    )
    return {"capture_id": capture_id, "status": resource.get("status")}


async def _handle_capture_refunded(event: dict[str, Any]) -> dict[str, Any]:
    resource = event.get("resource", {}) or {}
    refund_id = resource.get("id")
    await _audit("paypal.capture_refunded", event, refund_id=refund_id)
    return {"refund_id": refund_id, "status": resource.get("status")}


_HANDLERS: dict[str, Handler] = {
    "CHECKOUT.ORDER.APPROVED": _handle_order_approved,
    "PAYMENT.CAPTURE.COMPLETED": _handle_capture_completed,
    "PAYMENT.CAPTURE.REFUNDED": _handle_capture_refunded,
    "PAYMENT.CAPTURE.DENIED": _handle_capture_refunded,   # audit-only
}


# ── Public dispatch ─────────────────────────────────────────────────────────

async def verify_and_dispatch(
    headers: dict[str, str], raw_body: bytes,
) -> dict[str, Any]:
    """Full pipeline: signature verify → idempotency → handler dispatch."""
    client = get_client()
    verified = await client.verify_webhook(headers=headers, raw_body=raw_body)
    if not verified:
        return {"accepted": False, "reason": "signature_invalid"}

    try:
        event = json.loads(raw_body.decode("utf-8"))
    except Exception as e:
        return {"accepted": False, "reason": f"invalid_json:{e}"}

    event_id = event.get("id") or ""
    event_type = event.get("event_type") or ""
    if not event_id or not event_type:
        return {"accepted": False, "reason": "missing_id_or_type"}

    if await _seen_before(event_id):
        logger.info("paypal_webhook_duplicate event_id=%s", event_id)
        return {"accepted": True, "duplicate": True, "event_id": event_id}

    handler = _HANDLERS.get(event_type)
    if handler is None:
        logger.info("paypal_webhook_unhandled type=%s id=%s", event_type, event_id)
        await _audit("paypal.unhandled_event", event, event_type=event_type)
        return {"accepted": True, "handled": False, "event_id": event_id}

    try:
        result = await handler(event)
    except Exception as e:
        logger.exception("paypal_webhook_handler_failed type=%s err=%s", event_type, e)
        return {"accepted": True, "handled": False, "event_id": event_id,
                "reason": f"handler_error:{e}"[:200]}

    return {"accepted": True, "handled": True, "event_id": event_id, "result": result}