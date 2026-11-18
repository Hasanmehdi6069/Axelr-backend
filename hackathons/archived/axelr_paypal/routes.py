"""Axelr PayPal — FastAPI routes.

Honest note: auth delegates to `api.auth` (Axelr's host project). If those
functions don't exist in your project, either wire them or the endpoints
return 401. There is no fallback that silently trusts anonymous callers.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import (
    APIRouter, Depends, Header, HTTPException, Request, status,
)
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .core import (
    AuditEvent, get_core_module, get_transaction_store,
    log_audit,
)
from .client import (
    PayPalAPIError, extract_payment_intent, get_paypal_client,
)
from .schemas import (
    AIExtractionRequest, CaptureOrderRequest, CreateOrderRequest,
    PaymentIntent, RefundRequest, TransactionState,
)
from .webhooks import verify_and_dispatch

logger = logging.getLogger("axelr.paypal.routes")
router = APIRouter(prefix="/paypal", tags=["paypal"])
_bearer = HTTPBearer(auto_error=False)


def _err(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"success": False, "error": {"code": code, "message": message}},
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def get_paypal_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_appwrite_session: str | None = Header(None, alias="X-Appwrite-Session"),
) -> dict:
    if credentials is not None:
        try:
            from api.auth import get_current_user
            return await get_current_user(request, credentials)
        except Exception:
            pass
    if x_appwrite_session:
        try:
            from api.auth import verify_appwrite_session
            user = verify_appwrite_session(x_appwrite_session)
            if user:
                return user
        except Exception:
            pass
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required")


# ── Health (public) ─────────────────────────────────────────────────────────
@router.get("/health")
async def paypal_health() -> JSONResponse:
    report = get_core_module().health_probe()
    return JSONResponse(
        status_code=200 if report.healthy else 503,
        content=report.model_dump(),
    )


# ── AI extraction ──────────────────────────────────────────────────────────
@router.post("/ai/from-text")
async def extract_from_text(
    body: AIExtractionRequest,
    user: dict = Depends(get_paypal_user),
) -> JSONResponse:
    try:
        intent: PaymentIntent = await extract_payment_intent(body.unstructured_text)
        await log_audit(AuditEvent(
            event_type="ai.extraction",
            user_id=str(user.get("_id", "anon")),
            timestamp=_now(),
            metadata={
                "confidence": intent.confidence,
                "raw_input_hash": intent.raw_input_hash,
                "payment_type": intent.type,
            },
        ))
        if not body.approve:
            return JSONResponse(status_code=200, content={
                "success": True, "approved": False,
                "preview": intent.model_dump(),
                "message": "Set approve=true to create a PayPal order.",
            })
        store = get_transaction_store()
        txn = await store.create_transaction(
            amount=intent.amount, currency=intent.currency,
            items=[i.model_dump() for i in intent.items],
            metadata={"payment_type": intent.type, "payer_email": intent.payer_email},
        )
        txn = await store.transition_state(
            txn.id, TransactionState.VALIDATED, reason="AI extraction validated",
        )
        client = get_paypal_client()
        order = await client.create_order({
            "amount": intent.amount,
            "currency": intent.currency,
            "intent": "CAPTURE",
            "items": [i.model_dump() for i in intent.items],
            "idempotency_key": f"axelr_ai_{uuid.uuid4().hex[:16]}",
        })
        txn = await store.transition_state(
            txn.id, TransactionState.PAYPAL_CREATED,
            reason=f"PayPal order {order['id']}",
        )
        txn_data = txn.model_dump()
        txn_data["paypal_order_id"] = order["id"]
        await store._save(type(txn)(**txn_data))
        await store.add_paypal_order_index(order["id"], txn.id)
        await log_audit(AuditEvent(
            event_type="order.created_from_ai",
            user_id=str(user.get("_id", "anon")),
            timestamp=_now(),
            metadata={"transaction_id": txn.id, "order_id": order["id"]},
        ))
        return JSONResponse(status_code=201, content={
            "success": True, "approved": True,
            "transaction_id": txn.id,
            "paypal_order_id": order["id"],
            "approve_url": order.get("approve_url"),
            "payment_intent": intent.model_dump(),
        })
    except ValueError as e:
        return _err("EXTRACTION_FAILED", str(e), 400)
    except PayPalAPIError as e:
        return _err("PAYPAL_ERROR", str(e), 502)
    except Exception as e:
        logger.exception("ai_from_text failed: %s", e)
        return _err("INTERNAL", "AI extraction failed", 500)


# ── Orders ─────────────────────────────────────────────────────────────────
@router.post("/orders", status_code=201)
async def create_order(
    body: CreateOrderRequest, user: dict = Depends(get_paypal_user),
):
    if not get_core_module().is_configured:
        return _err("NOT_CONFIGURED", "PayPal credentials missing", 503)
    idem = body.idempotency_key or f"axelr_order_{uuid.uuid4().hex[:16]}"
    try:
        store = get_transaction_store()
        txn = await store.create_transaction(
            amount=body.amount, currency=body.currency,
            items=[i.model_dump() for i in body.items],
            metadata={"reference_id": body.reference_id},
        )
        txn = await store.transition_state(
            txn.id, TransactionState.VALIDATED, reason="schema validated",
        )
        client = get_paypal_client()
        payload = body.model_dump()
        payload["idempotency_key"] = idem
        order = await client.create_order(payload)
        txn = await store.transition_state(
            txn.id, TransactionState.PAYPAL_CREATED,
            reason=f"PayPal order {order['id']}",
        )
        txn_data = txn.model_dump()
        txn_data["paypal_order_id"] = order["id"]
        await store._save(type(txn)(**txn_data))
        await store.add_paypal_order_index(order["id"], txn.id)
        await log_audit(AuditEvent(
            event_type="order.created",
            user_id=str(user.get("_id", "anon")),
            timestamp=_now(),
            session_id=idem,
            metadata={"order_id": order["id"], "transaction_id": txn.id},
        ))
        return {
            "success": True,
            "transaction_id": txn.id,
            "order": {
                "id": order.get("id"),
                "status": order.get("status"),
                "intent": order.get("intent"),
                "approve_url": order.get("approve_url"),
                "links": order.get("links", []),
                "create_time": order.get("create_time"),
            },
            "idempotency_key": idem,
        }
    except ValueError as e:
        return _err("VALIDATION_ERROR", str(e), 400)
    except PayPalAPIError as e:
        return _err("PAYPAL_ERROR", str(e), 502)
    except Exception as e:
        logger.exception("create_order failed: %s", e)
        return _err("INTERNAL", "Order creation failed", 500)


@router.get("/orders/{order_id}")
async def get_order(order_id: str, user: dict = Depends(get_paypal_user)):
    if not get_core_module().is_configured:
        return _err("NOT_CONFIGURED", "PayPal credentials missing", 503)
    try:
        return {"success": True, "order": await get_paypal_client().get_order(order_id)}
    except PayPalAPIError as e:
        return _err("PAYPAL_ERROR", str(e), 502)
    except Exception as e:
        logger.exception("get_order failed: %s", e)
        return _err("INTERNAL", "Order fetch failed", 500)


@router.post("/orders/{order_id}/capture")
async def capture_order(
    order_id: str, body: CaptureOrderRequest | None = None,
    user: dict = Depends(get_paypal_user),
):
    if not get_core_module().is_configured:
        return _err("NOT_CONFIGURED", "PayPal credentials missing", 503)
    idem = (body.idempotency_key if (body and body.idempotency_key)
            else f"axelr_cap_{uuid.uuid4().hex[:16]}")
    store = get_transaction_store()
    txn = await store.get_by_paypal_order_id(order_id)
    
    # STATE CHECK BEFORE MONEY MOVEMENT
    if txn is not None and txn.state != TransactionState.APPROVED:
        return _err(
            "INVALID_STATE",
            f"Cannot capture from state {txn.state.value}; must be APPROVED",
            400,
        )
    try:
        result = await get_paypal_client().capture_order(order_id, idempotency_key=idem)
        if txn is not None:
            txn = await store.transition_state(
                txn.id, TransactionState.CAPTURED,
                reason=f"capture {result.get('id')}",
            )
            capture_id = (
                result.get("purchase_units", [{}])[0]
                      .get("payments", {})
                      .get("captures", [{}])[0]
                      .get("id")
            )
            if capture_id:
                txn_data = txn.model_dump()
                txn_data["paypal_capture_id"] = capture_id
                await store._save(type(txn)(**txn_data))
                await store.add_paypal_capture_index(capture_id, txn.id)
        await log_audit(AuditEvent(
            event_type="order.captured",
            user_id=str(user.get("_id", "anon")),
            timestamp=_now(),
            session_id=idem,
            metadata={"order_id": order_id,
                      "transaction_id": txn.id if txn else None},
        ))
        return {
            "success": True,
            "order": {
                "id": result.get("id"),
                "status": result.get("status"),
                "purchase_units": result.get("purchase_units", []),
                "payer": result.get("payer", {}),
            },
            "idempotency_key": idem,
        }
    except ValueError as e:
        return _err("INVALID_STATE", str(e), 400)
    except PayPalAPIError as e:
        return _err("PAYPAL_ERROR", str(e), 502)
    except Exception as e:
        logger.exception("capture failed: %s", e)
        return _err("INTERNAL", "Capture failed", 500)


# ── Refunds ────────────────────────────────────────────────────────────────
@router.post("/refunds", status_code=201)
async def refund_capture(
    body: RefundRequest, user: dict = Depends(get_paypal_user),
):
    if not get_core_module().is_configured:
        return _err("NOT_CONFIGURED", "PayPal credentials missing", 503)
    if not body.confirm:
        return _err("CONFIRM_REQUIRED", "Set confirm=true to execute refund", 400)
    idem = body.idempotency_key or f"axelr_refund_{uuid.uuid4().hex[:16]}"
    store = get_transaction_store()
    txn = await store.get_by_paypal_capture_id(body.capture_id)
    
    # STATE CHECK BEFORE MONEY MOVEMENT
    if txn is not None and txn.state != TransactionState.CAPTURED:
        return _err(
            "INVALID_STATE",
            f"Cannot refund from state {txn.state.value}; must be CAPTURED",
            400,
        )
    try:
        payload = body.model_dump()
        result = await get_paypal_client().create_refund(payload, idempotency_key=idem)
        if txn is not None:
            txn = await store.transition_state(
                txn.id, TransactionState.REFUNDED,
                reason=f"refund {result.get('id')}",
            )
        await log_audit(AuditEvent(
            event_type="payment.refunded",
            user_id=str(user.get("_id", "anon")),
            timestamp=_now(),
            session_id=idem,
            metadata={"capture_id": body.capture_id,
                      "transaction_id": txn.id if txn else None},
        ))
        return {
            "success": True,
            "refund": {
                "id": result.get("id"),
                "status": result.get("status"),
                "amount": result.get("amount", {}),
            },
            "idempotency_key": idem,
        }
    except ValueError as e:
        return _err("INVALID_STATE", str(e), 400)
    except PayPalAPIError as e:
        return _err("PAYPAL_ERROR", str(e), 502)
    except Exception as e:
        logger.exception("refund failed: %s", e)
        return _err("INTERNAL", "Refund failed", 500)


# ── Webhooks (public) ──────────────────────────────────────────────────────
@router.post("/webhooks")
async def paypal_webhook(request: Request):
    raw = await request.body()
    if len(raw) > 1_000_000:
        return _err("PAYLOAD_TOO_LARGE", "Body > 1MB", 413)
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        result = await verify_and_dispatch(headers, raw)
    except Exception as e:
        logger.exception("webhook unhandled: %s", e)
        return _err("INTERNAL", "Webhook processing failed", 500)
    if not result.get("accepted"):
        return JSONResponse(status_code=400, content={
            "success": False, "reason": result.get("reason"),
        })
    return JSONResponse(status_code=200, content={"success": True, **result})