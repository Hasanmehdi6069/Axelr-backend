# axelr_paypal/routes.py
"""FastAPI routes for the PayPal module."""
from __future__ import annotations

import logging

from fastapi import (
    APIRouter, Depends, Header, HTTPException, Query, Request, status,
)
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .client import get_client
from .core import PayPalAPIError, new_request_id, settings
from .schemas import (
    CaptureOrderRequest, CreateOrderRequest, RefundRequest, WebhookEvent,
)
from .webhooks import verify_and_dispatch

logger = logging.getLogger("axelr.paypal.routes")
router = APIRouter(prefix="/paypal", tags=["paypal"])
_bearer = HTTPBearer(auto_error=False)


# ── Auth (JWT first, Appwrite session second) ───────────────────────────────

async def get_paypal_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_appwrite_session: str | None = Header(None, alias="X-Appwrite-Session"),
) -> dict:
    if credentials is not None:
        try:
            from api.auth import get_current_user
            return await get_current_user(request, credentials)
        except HTTPException:
            pass
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
    raise HTTPException(401, "Authentication required")


def _err(code: str, message: str, status: int, **extra) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"success": False, "error": {"code": code, "message": message, **extra}},
    )


# ── Health ──────────────────────────────────────────────────────────────────

@router.get("/health")
async def paypal_health() -> JSONResponse:
    """Public health — no auth. Safe for judge uptime check."""
    from .core import health_probe
    report = await health_probe()
    code = 200 if report.get("healthy") else 503
    return JSONResponse(status_code=code, content=report)


# ── Orders ──────────────────────────────────────────────────────────────────

@router.post("/orders", status_code=status.HTTP_201_CREATED)
async def create_order(
    body: CreateOrderRequest, user: dict = Depends(get_paypal_user),
):
    if not settings.is_configured:
        return _err("NOT_CONFIGURED", "PayPal credentials missing", 503)

    client = get_client()
    idem = new_request_id("axelr_order")
    items_payload = [
        {
            "name": i.name, "quantity": str(i.quantity),
            "unit_amount": {"currency_code": body.currency, "value": str(i.unit_amount)},
            "category": i.category,
            **({"sku": i.sku} if i.sku else {}),
        }
        for i in body.items
    ] or None

    try:
        from .auditing import AuditTimer
        async with AuditTimer(
            "paypal.create_order",
            user_id=str(user.get("_id", "anon")),
            session_id=idem,
        ):
            order = await client.create_order(
                amount=str(body.amount),
                currency=body.currency,
                intent=body.intent,
                description=body.description,
                reference_id=body.reference_id,
                return_url=body.return_url,
                cancel_url=body.cancel_url,
                items=items_payload,
                custom_id=body.custom_id,
                idempotency_key=idem,
            )
    except PayPalAPIError as e:
        return _err("PAYPAL_ERROR", str(e)[:400], 502, status=e.status)
    except Exception as e:
        logger.exception("create_order_failed err=%s", e)
        return _err("INTERNAL", "Order creation failed", 500)

    approve_link = next(
        (l["href"] for l in order.get("links", []) if l.get("rel") == "approve"),
        None,
    )
    return {
        "success": True,
        "order": {
            "id": order.get("id"),
            "status": order.get("status"),
            "intent": order.get("intent"),
            "approve_url": approve_link,
            "links": order.get("links", []),
            "create_time": order.get("create_time"),
        },
        "idempotency_key": idem,
    }


@router.get("/orders/{order_id}")
async def get_order(order_id: str, user: dict = Depends(get_paypal_user)):
    if not settings.is_configured:
        return _err("NOT_CONFIGURED", "PayPal credentials missing", 503)
    try:
        order = await get_client().get_order(order_id)
        return {"success": True, "order": order}
    except PayPalAPIError as e:
        return _err("PAYPAL_ERROR", str(e)[:400], 502, status=e.status)
    except Exception as e:
        logger.exception("get_order_failed order_id=%s err=%s", order_id, e)
        return _err("INTERNAL", "Order fetch failed", 500)


@router.post("/orders/{order_id}/capture")
async def capture_order(
    order_id: str, body: CaptureOrderRequest | None = None,
    user: dict = Depends(get_paypal_user),
):
    if not settings.is_configured:
        return _err("NOT_CONFIGURED", "PayPal credentials missing", 503)
    if body and body.order_id != order_id:
        return _err("BAD_REQUEST", "order_id mismatch between path and body", 400)
    idem = (body.idempotency_key if body else None) or new_request_id("axelr_cap")
    try:
        from .auditing import AuditTimer
        async with AuditTimer(
            "paypal.capture_order",
            user_id=str(user.get("_id", "anon")),
            session_id=idem,
        ):
            result = await get_client().capture_order(order_id, idempotency_key=idem)
    except PayPalAPIError as e:
        return _err("PAYPAL_ERROR", str(e)[:400], 502, status=e.status)
    except Exception as e:
        logger.exception("capture_order_failed order_id=%s err=%s", order_id, e)
        return _err("INTERNAL", "Capture failed", 500)
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


# ── Refunds ─────────────────────────────────────────────────────────────────

@router.post("/refunds", status_code=status.HTTP_201_CREATED)
async def refund_capture(
    body: RefundRequest, user: dict = Depends(get_paypal_user),
):
    if not settings.is_configured:
        return _err("NOT_CONFIGURED", "PayPal credentials missing", 503)
    if not body.confirm:
        return _err("CONFIRM_REQUIRED", "Set confirm=true to execute refund", 400)
    idem = new_request_id("axelr_refund")
    try:
        from .auditing import AuditTimer
        async with AuditTimer(
            "paypal.refund",
            user_id=str(user.get("_id", "anon")),
            session_id=idem,
        ):
            result = await get_client().refund_capture(
                body.capture_id,
                amount=str(body.amount) if body.amount else None,
                currency=body.currency if body.amount else None,
                reason=body.reason,
                idempotency_key=idem,
            )
    except PayPalAPIError as e:
        return _err("PAYPAL_ERROR", str(e)[:400], 502, status=e.status)
    except Exception as e:
        logger.exception("refund_failed capture_id=%s err=%s", body.capture_id, e)
        return _err("INTERNAL", "Refund failed", 500)
    return {
        "success": True,
        "refund": {
            "id": result.get("id"),
            "status": result.get("status"),
            "amount": result.get("amount", {}),
        },
        "idempotency_key": idem,
    }


# ── Webhooks ────────────────────────────────────────────────────────────────

@router.post("/webhooks")
async def paypal_webhook(request: Request):
    """Public endpoint — PayPal authenticates via signature headers."""
    raw = await request.body()
    if len(raw) > 1_000_000:
        return _err("PAYLOAD_TOO_LARGE", "Webhook body > 1MB", 413)
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        result = await verify_and_dispatch(headers, raw)
    except Exception as e:
        logger.exception("paypal_webhook_unhandled err=%s", e)
        return _err("INTERNAL", "Webhook processing failed", 500)
    if not result.get("accepted"):
        return JSONResponse(status_code=400, content={"success": False, "reason": result.get("reason")})
    return JSONResponse(status_code=200, content={"success": True, **result})