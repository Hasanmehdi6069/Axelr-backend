"""Axelr PayPal — FastAPI app, Appwrite function entry, and package exports.

Two run modes:
  1. `uvicorn axelr_paypal.main:app` — FastAPI server.
  2. Appwrite function entry: `await main(context)`.

Honest note: startup does not raise if credentials are missing — the
health endpoint reports `healthy: false` instead. That's intentional so
healthchecks and CI can boot the app without secrets.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from .core import (
    MODULE_VERSION, ModuleSettings,
    get_core_module, init as core_init, shutdown as core_shutdown,
)
from .routes import router as paypal_router

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("axelr.paypal.main")

# Re-export public API — this file doubles as the package entry.
from .client import (  # noqa: E402
    PayPalAPIError, PayPalClient, get_paypal_client,
    extract_payment_intent,
)
from .core import (  # noqa: E402
    AuditEvent, HealthStatus, PayPalCoreModule, Transaction, TransactionStore,
    get_audit_logs, get_audit_stats, get_transaction_store,
    health_probe, is_healthy, log_audit, log_audit_event,
)
from .schemas import (  # noqa: E402
    AIExtractionRequest, CaptureOrderRequest, CreateOrderRequest,
    OrderItem, PaymentIntent, RefundRequest, TransactionState, WebhookEvent,
)
from .webhooks import verify_and_dispatch  # noqa: E402

__version__ = MODULE_VERSION


@asynccontextmanager
async def lifespan(app: FastAPI):
    core_init()
    try:
        yield
    finally:
        await core_shutdown()


def create_app() -> FastAPI:
    application = FastAPI(
        title="Axelr PayPal Module",
        version=MODULE_VERSION,
        lifespan=lifespan,
    )
    application.include_router(paypal_router)

    @application.get("/health")
    async def root_health() -> dict:
        report = get_core_module().health_probe()
        return report.model_dump()

    return application


app = create_app()


# ── Appwrite function entry ────────────────────────────────────────────────
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


async def _act_health(_body: dict) -> tuple[dict, int]:
    report = get_core_module().health_probe()
    return report.model_dump(), 200 if report.healthy else 503


async def _act_create_order(body: dict) -> tuple[dict, int]:
    if not get_core_module().is_configured:
        return {"success": False, "error": {"code": "NOT_CONFIGURED"}}, 503
    try:
        req = CreateOrderRequest(**{k: v for k, v in body.items() if k != "action"})
    except Exception as e:
        return {"success": False,
                "error": {"code": "VALIDATION", "message": str(e)[:300]}}, 422
    try:
        order = await get_paypal_client().create_order(req.model_dump())
    except PayPalAPIError as e:
        return {"success": False,
                "error": {"code": "PAYPAL", "message": str(e)[:400]}}, 502
    return {"success": True, "order": {
        "id": order.get("id"), "status": order.get("status"),
        "approve_url": order.get("approve_url"),
    }}, 201


_ACTIONS = {"health": _act_health, "create_order": _act_create_order}


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
        logger.exception("appwrite fn unhandled action=%s err=%s", action, e)
        return context.res.json({
            "success": False,
            "error": {"code": "INTERNAL", "message": str(e)[:300]},
        }, 500)


async def _local_demo() -> None:
    await asyncio.to_thread(core_init)
    try:
        print("=== health ===")
        print(await main(AppwriteContext({"action": "health"})))
        if os.getenv("PAYPAL_CLIENT_ID"):
            print("\n=== create_order ===")
            print(await main(AppwriteContext({
                "action": "create_order",
                "amount": "9.99", "currency": "USD",
            })))
        else:
            print("\n(skipping create_order — PAYPAL_CLIENT_ID unset)")
    finally:
        await core_shutdown()


if __name__ == "__main__":
    asyncio.run(_local_demo())


__all__ = [
    "app", "create_app", "main", "AppwriteContext",
    "__version__", "MODULE_VERSION",
    # core
    "ModuleSettings", "HealthStatus", "PayPalCoreModule",
    "Transaction", "TransactionStore",
    "get_core_module", "get_transaction_store",
    "is_healthy", "health_probe",
    "AuditEvent", "log_audit", "log_audit_event",
    "get_audit_logs", "get_audit_stats",
    # client / ai
    "PayPalClient", "PayPalAPIError", "get_paypal_client",
    "extract_payment_intent",
    # schemas
    "AIExtractionRequest", "CaptureOrderRequest", "CreateOrderRequest",
    "OrderItem", "PaymentIntent", "RefundRequest",
    "TransactionState", "WebhookEvent",
    # webhooks
    "verify_and_dispatch",
]