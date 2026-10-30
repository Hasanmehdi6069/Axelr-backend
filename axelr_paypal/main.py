"""Appwrite Function entry + local demo runner.

Honest note: the original had `sys.exit(1)` at import time if
`shared_check_deps` failed. That kills the *parent* process — appwrite
runtime, pytest, any test harness — on import. That's a real bug. Replaced
with a check that runs **only inside `main()`**, and fails gracefully.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("axelr.paypal.fn")


class _MockRequest:
    def __init__(self, body: dict | None = None, headers: dict | None = None):
        self.bodyJson = body or {}
        self.headers = headers or {}


class _MockResponse:
    def json(self, data: dict, status_code: int = 200) -> dict:
        return {"body": json.dumps(data), "statusCode": status_code}


class AppwriteContext:
    def __init__(self, body: dict | None = None, headers: dict | None = None):
        self.req = _MockRequest(body, headers)
        self.res = _MockResponse()
        self.log = lambda m: logger.info(m)
        self.error = lambda m: logger.error(m)


async def _act_health(_body: dict) -> tuple[dict, int]:
    from .core import health_probe
    report = await health_probe()
    return report, 200 if report.get("healthy") else 503


async def _act_create_order(body: dict) -> tuple[dict, int]:
    from .client import get_client
    from .core import PayPalAPIError, new_request_id, settings
    from .schemas import CreateOrderRequest
    if not settings.is_configured:
        return {"success": False, "error": {"code": "NOT_CONFIGURED"}}, 503
    try:
        req = CreateOrderRequest(**{k: v for k, v in body.items() if k != "action"})
    except Exception as e:
        return {"success": False,
                "error": {"code": "VALIDATION", "message": str(e)[:300]}}, 422
    idem = new_request_id()
    try:
        order = await get_client().create_order(
            amount=str(req.amount), currency=req.currency,
            description=req.description, reference_id=req.reference_id,
            return_url=req.return_url, cancel_url=req.cancel_url,
            idempotency_key=idem,
        )
    except PayPalAPIError as e:
        return {"success": False,
                "error": {"code": "PAYPAL", "message": str(e)[:400]}}, 502
    approve = next(
        (l["href"] for l in order.get("links", []) if l.get("rel") == "approve"),
        None,
    )
    return {"success": True,
            "order": {"id": order.get("id"),
                      "status": order.get("status"),
                      "approve_url": approve}}, 201


async def _act_get_order(body: dict) -> tuple[dict, int]:
    from .client import get_client
    from .core import PayPalAPIError, settings
    if not settings.is_configured:
        return {"success": False, "error": {"code": "NOT_CONFIGURED"}}, 503
    oid = body.get("order_id")
    if not oid:
        return {"success": False, "error": {"code": "MISSING_ORDER_ID"}}, 400
    try:
        return {"success": True, "order": await get_client().get_order(oid)}, 200
    except PayPalAPIError as e:
        return {"success": False,
                "error": {"code": "PAYPAL", "message": str(e)[:400]}}, 502


async def _act_capture_order(body: dict) -> tuple[dict, int]:
    from .client import get_client
    from .core import PayPalAPIError, new_request_id, settings
    if not settings.is_configured:
        return {"success": False, "error": {"code": "NOT_CONFIGURED"}}, 503
    oid = body.get("order_id")
    if not oid:
        return {"success": False, "error": {"code": "MISSING_ORDER_ID"}}, 400
    try:
        r = await get_client().capture_order(oid, idempotency_key=new_request_id())
        return {"success": True,
                "order": {"id": r.get("id"), "status": r.get("status")}}, 200
    except PayPalAPIError as e:
        return {"success": False,
                "error": {"code": "PAYPAL", "message": str(e)[:400]}}, 502


async def _act_refund(body: dict) -> tuple[dict, int]:
    from .client import get_client
    from .core import PayPalAPIError, new_request_id, settings
    from .schemas import RefundRequest
    if not settings.is_configured:
        return {"success": False, "error": {"code": "NOT_CONFIGURED"}}, 503
    if not body.get("confirm"):
        return {"success": False, "error": {"code": "CONFIRM_REQUIRED"}}, 400
    try:
        req = RefundRequest(**{k: v for k, v in body.items() if k != "action"})
    except Exception as e:
        return {"success": False,
                "error": {"code": "VALIDATION", "message": str(e)[:300]}}, 422
    try:
        r = await get_client().refund_capture(
            req.capture_id,
            amount=str(req.amount) if req.amount else None,
            currency=req.currency if req.amount else None,
            reason=req.reason,
            idempotency_key=new_request_id(),
        )
        return {"success": True,
                "refund": {"id": r.get("id"), "status": r.get("status")}}, 201
    except PayPalAPIError as e:
        return {"success": False,
                "error": {"code": "PAYPAL", "message": str(e)[:400]}}, 502


_ACTIONS = {
    "health": _act_health,
    "create_order": _act_create_order,
    "get_order": _act_get_order,
    "capture_order": _act_capture_order,
    "refund": _act_refund,
}


async def main(context: AppwriteContext) -> dict[str, Any]:
    body: dict = context.req.bodyJson or {}
    action = body.get("action", "health")
    handler = _ACTIONS.get(action)
    if handler is None:
        return context.res.json({
            "success": False,
            "error": {"code": "UNKNOWN_ACTION", "message": f"action={action}"},
            "available_actions": sorted(_ACTIONS),
        }, 400)
    try:
        result, status = await handler(body)
        return context.res.json(result, status)
    except Exception as e:
        logger.exception("paypal_fn_unhandled action=%s err=%s", action, e)
        return context.res.json({
            "success": False,
            "error": {"code": "INTERNAL", "message": str(e)[:300]},
        }, 500)


async def _local_demo() -> None:
    from .core import init, shutdown
    await init()
    try:
        print("=== health ===")
        print(await main(AppwriteContext({"action": "health"})))
        print("\n=== unknown action ===")
        print(await main(AppwriteContext({"action": "nope"})))
        if os.getenv("PAYPAL_CLIENT_ID"):
            print("\n=== create_order ===")
            print(await main(AppwriteContext({
                "action": "create_order",
                "amount": "9.99", "currency": "USD",
                "description": "Local demo order",
                "return_url": "https://axelr.in/ok",
                "cancel_url": "https://axelr.in/cancel",
            })))
        else:
            print("\n(skipping create_order — PAYPAL_CLIENT_ID not set)")
    finally:
        await shutdown()


if __name__ == "__main__":
    asyncio.run(_local_demo())