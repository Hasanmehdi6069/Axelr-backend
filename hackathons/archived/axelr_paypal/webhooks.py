"""Axelr PayPal — webhook verification, idempotent dispatch, state handlers.

Contract (matches routes.py):
    await verify_and_dispatch(headers: dict, body: bytes) -> dict

Returns:
    {"accepted": True,  "handled": bool, "event_id": str}
    {"accepted": True,  "duplicate": True, "event_id": str}
    {"accepted": False, "reason": str}

Honest note: signature verification is delegated to PayPal's
`/v1/notifications/verify-webhook-signature`. We fail CLOSED if it is
unreachable, misconfigured, or returns non-SUCCESS.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Awaitable, Callable, Dict

from .client import get_paypal_client
from .core import (
    get_core_module,
    get_transaction_store,
    log_audit_event,
)
from .schemas import TransactionState

logger = logging.getLogger("axelr.paypal.webhooks")

_IDEM_TTL_S = 86_400
_LOCAL_IDEM: Dict[str, float] = {}
_handlers: Dict[str, list[Callable[[Dict[str, Any]], Awaitable[None] | None]]] = {}


def register_event_handler(
    event_type: str,
    handler: Callable[[Dict[str, Any]], Awaitable[None] | None],
) -> None:
    _handlers.setdefault(event_type, []).append(handler)


def _evict_local() -> None:
    now = time.time()
    for k in [k for k, ts in _LOCAL_IDEM.items() if now - ts > _IDEM_TTL_S]:
        _LOCAL_IDEM.pop(k, None)


async def _seen_before(event_id: str) -> bool:
    """Return True iff event_id has been processed before."""
    core = get_core_module()
    store = get_transaction_store()
    if core.settings.redis_url and store.redis is not None:
        try:
            key = f"axelr:paypal:idem:{event_id}"
            fresh = await store.redis.set(key, "1", ex=_IDEM_TTL_S, nx=True)
            return not bool(fresh)
        except Exception as e:
            logger.warning("redis idem check failed, using local: %s", e)
    _evict_local()
    if event_id in _LOCAL_IDEM:
        return True
    _LOCAL_IDEM[event_id] = time.time()
    return False


async def verify_and_dispatch(
    headers: Dict[str, str],
    body: bytes,
) -> Dict[str, Any]:
    # Decode exactly once.
    if isinstance(body, (bytes, bytearray)):
        try:
            body_str = body.decode("utf-8")
        except UnicodeDecodeError:
            return {"accepted": False, "reason": "invalid_encoding"}
    else:
        body_str = str(body)

    client = get_paypal_client()
    try:
        valid = await client.verify_webhook_signature(headers, body_str)
    except Exception as e:
        logger.error("signature verify raised: %s", e)
        return {"accepted": False, "reason": "verify_error"}
    if not valid:
        logger.warning("signature rejected")
        return {"accepted": False, "reason": "signature_invalid"}

    try:
        event = json.loads(body_str)
    except json.JSONDecodeError:
        return {"accepted": False, "reason": "invalid_json"}

    event_id = event.get("id") or ""
    event_type = event.get("event_type") or ""
    if not event_id or not event_type:
        return {"accepted": False, "reason": "missing_id_or_type"}

    if await _seen_before(event_id):
        logger.info("duplicate event %s", event_id)
        return {"accepted": True, "duplicate": True, "event_id": event_id}

    handlers = _handlers.get(event_type, [])
    if not handlers:
        logger.info("unhandled type=%s id=%s", event_type, event_id)
        return {"accepted": True, "handled": False, "event_id": event_id}

    for h in handlers:
        try:
            res = h(event)
            if res is not None and hasattr(res, "__await__"):
                await res
        except Exception as e:
            logger.exception("handler error type=%s: %s", event_type, e)

    return {"accepted": True, "handled": True, "event_id": event_id}


# Backward-compat alias for older tests.
process_webhook_event = verify_and_dispatch


# ── Handlers ────────────────────────────────────────────────────────────────
async def _handle_order_approved(event: Dict[str, Any]) -> None:
    oid = event.get("resource", {}).get("id")
    if not oid:
        logger.warning("order_approved missing id")
        return
    store = get_transaction_store()
    txn = await store.get_by_paypal_order_id(oid)
    if txn is None:
        for t in await store.list_transactions():
            if t.paypal_order_id == oid:
                txn = t
                break
    if txn is None:
        logger.warning("no txn for order %s", oid)
        return
    if txn.state == TransactionState.APPROVED:
        logger.info("txn %s already APPROVED (dup)", txn.id)
        return
    await store.transition_state(
        txn.id,
        TransactionState.APPROVED,
        reason=f"webhook CHECKOUT.ORDER.APPROVED {oid}",
    )
    await log_audit_event(
        {
            "event_type": "webhook.order_approved",
            "metadata": {"order_id": oid, "transaction_id": txn.id},
        }
    )


async def _handle_capture_completed(event: Dict[str, Any]) -> None:
    res = event.get("resource", {})
    capture_id = res.get("id")
    order_id = res.get("supplementary_data", {}).get("related_ids", {}).get("order_id")
    if not capture_id or not order_id:
        logger.warning("capture_completed missing ids")
        return
    store = get_transaction_store()
    txn = await store.get_by_paypal_order_id(order_id)
    if txn is None:
        logger.warning("no txn for order %s", order_id)
        return
    if txn.state == TransactionState.CAPTURED:
        logger.info("txn %s already CAPTURED (dup)", txn.id)
        return
    txn = await store.transition_state(
        txn.id,
        TransactionState.CAPTURED,
        reason=f"webhook PAYMENT.CAPTURE.COMPLETED {capture_id}",
    )
    txn_data = txn.model_dump()
    txn_data["paypal_capture_id"] = capture_id
    await store._save(type(txn)(**txn_data))
    await store.add_paypal_capture_index(capture_id, txn.id)
    await log_audit_event(
        {
            "event_type": "webhook.capture_completed",
            "metadata": {"order_id": order_id, "capture_id": capture_id, "transaction_id": txn.id},
        }
    )


async def _handle_refund_completed(event: Dict[str, Any]) -> None:
    res = event.get("resource", {})
    refund_id = res.get("id")
    capture_id = res.get("supplementary_data", {}).get("related_ids", {}).get("capture_id")
    if not refund_id or not capture_id:
        logger.warning("refund_completed missing ids")
        return
    store = get_transaction_store()
    txn = await store.get_by_paypal_capture_id(capture_id)
    if txn is None:
        logger.warning("no txn for capture %s", capture_id)
        return
    if txn.state == TransactionState.REFUNDED:
        logger.info("txn %s already REFUNDED (dup)", txn.id)
        return
    txn = await store.transition_state(
        txn.id,
        TransactionState.REFUNDED,
        reason=f"webhook PAYMENT.REFUND.COMPLETED {refund_id}",
    )
    txn_data = txn.model_dump()
    txn_data["paypal_refund_id"] = refund_id
    await store._save(type(txn)(**txn_data))
    await log_audit_event(
        {
            "event_type": "webhook.refund_completed",
            "metadata": {"capture_id": capture_id, "refund_id": refund_id, "transaction_id": txn.id},
        }
    )


register_event_handler("CHECKOUT.ORDER.APPROVED", _handle_order_approved)
register_event_handler("PAYMENT.CAPTURE.COMPLETED", _handle_capture_completed)
register_event_handler("PAYMENT.REFUND.COMPLETED", _handle_refund_completed)


__all__ = [
    "verify_and_dispatch",
    "process_webhook_event",
    "register_event_handler",
]
