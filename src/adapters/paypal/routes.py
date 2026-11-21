"""FastAPI routes for PayPal module with proper auth, idempotency, and state management"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .auditing import get_audit_logger
from .axelr_ai import extract_payment_intent
from .client import get_paypal_client
from .core import HealthStatus, get_core_module
from .schemas import CreateOrderRequest, RefundRequest
from .transactions import Transaction, TransactionState, get_transaction_store
from .webhooks import get_webhook_handler

logger = logging.getLogger("axelr.paypal.routes")
router = APIRouter(prefix="/paypal", tags=["paypal"])


# Auth dependency (simplified - in production integrate with your auth system)
async def require_auth(request: Request) -> None:
    """Enforce authentication on all money-moving routes"""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        get_audit_logger().log_auth_event("invalid_auth_header", success=False, source_ip=request.client.host)
        raise HTTPException(status_code=401, detail="Missing or invalid authorization header")
    # Add full JWT validation in production


# Public routes (no auth)
@router.get("/health", response_model=HealthStatus)
async def health_check() -> HealthStatus:
    """Public health check endpoint"""
    core = get_core_module()
    return core.health_probe()


@router.post("/webhook")
async def paypal_webhook(request: Request):
    """Public webhook endpoint for PayPal events"""
    handler = get_webhook_handler()
    return await handler.process_webhook(request)


# AI extraction endpoint (requires auth)
class AIExtractionRequest(BaseModel):
    unstructured_text: str = Field(
        ..., min_length=10, max_length=10000, description="Raw text to extract payment intent from"
    )


class AIExtractionResponse(BaseModel):
    payment_intent: Dict[str, Any]
    requires_approval: bool = True
    message: str = "Extraction complete - review and approve to create order"


@router.post("/ai/from-text", response_model=AIExtractionResponse)
async def extract_from_text(request: AIExtractionRequest, _=Depends(require_auth)):
    """Extract payment intent from unstructured text, returns preview requiring human approval"""
    audit = get_audit_logger()
    try:
        extracted = await extract_payment_intent(request.unstructured_text)
        audit.log_ai_extraction(
            input_hash=extracted["_audit"]["input_hash"], confidence=extracted["confidence"], success=True
        )
        return {
            "payment_intent": extracted,
            "requires_approval": True,
            "message": "Extraction complete - review and approve to create order",
        }
    except Exception as e:
        logger.error(f"AI extraction failed: {str(e)}")
        audit.log_ai_extraction(input_hash="", confidence=0, success=False, error=str(e))
        raise HTTPException(status_code=400, detail=f"Extraction failed: {str(e)}")


# Order management routes (require auth)
class CreateOrderResponse(BaseModel):
    transaction_id: str
    paypal_order_id: str
    approve_url: Optional[str]
    state: str
    amount: str
    currency: str


@router.post("/orders", response_model=CreateOrderResponse)
async def create_order(request: CreateOrderRequest, _=Depends(require_auth)):
    """Create a new PayPal order and transaction"""
    client = get_paypal_client()
    store = get_transaction_store()
    audit = get_audit_logger()

    try:
        # Create transaction and transition through states
        txn = await store.create_transaction(
            amount=request.amount,
            currency=request.currency,
            items=[item.model_dump() for item in request.items],
            metadata={"description": request.description},
        )
        await store.transition_state(txn.id, TransactionState.VALIDATED, "Input validation passed")

        # Create PayPal order
        paypal_order = await client.create_order(request.model_dump())
        await store.transition_state(
            txn.id, TransactionState.PAYPAL_CREATED, f"PayPal order created: {paypal_order['id']}"
        )

        # Update transaction with PayPal ID
        txn_data = await store.get_transaction(txn.id)
        if txn_data:
            txn_data.paypal_order_id = paypal_order["id"]
            await store._save(txn_data)

        audit.log_money_movement(
            operation="order_created", transaction_id=txn.id, amount=request.amount, currency=request.currency
        )

        return {
            "transaction_id": txn.id,
            "paypal_order_id": paypal_order["id"],
            "approve_url": paypal_order.get("approve_url"),
            "state": TransactionState.PAYPAL_CREATED,
            "amount": request.amount,
            "currency": request.currency,
        }
    except Exception as e:
        logger.error(f"Create order failed: {str(e)}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Failed to create order: {str(e)}")


@router.get("/orders/{txn_id}", response_model=Transaction)
async def get_order(txn_id: str, _=Depends(require_auth)):
    """Get transaction details by ID"""
    store = get_transaction_store()
    txn = await store.get_transaction(txn_id)
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return txn


class CaptureOrderRequest(BaseModel):
    idempotency_key: Optional[str] = None


@router.post("/orders/{txn_id}/capture")
async def capture_order(txn_id: str, request: CaptureOrderRequest, _=Depends(require_auth)):
    """Capture a previously approved PayPal order"""
    client = get_paypal_client()
    store = get_transaction_store()
    audit = get_audit_logger()

    # Look up transaction by PayPal order ID (txn_id in route is PayPal order ID)
    txn = await store.get_by_paypal_order_id(txn_id)
    if not txn:
        # Fallback to direct transaction ID lookup
        txn = await store.get_transaction(txn_id)
        if not txn:
            raise HTTPException(status_code=404, detail="Transaction not found")

    if txn.state != TransactionState.APPROVED:
        raise HTTPException(
            status_code=400, detail=f"Cannot capture transaction in state {txn.state}, must be APPROVED"
        )

    try:
        capture = await client.capture_order(txn.paypal_order_id, request.idempotency_key)
        await store.transition_state(txn.id, TransactionState.CAPTURED, "PayPal capture completed")

        # Update transaction with capture ID
        txn_data = await store.get_transaction(txn.id)
        if txn_data:
            txn_data.paypal_capture_id = capture["id"]
            await store._save(txn_data)

        audit.log_money_movement(
            operation="order_captured", transaction_id=txn.id, amount=txn.amount, currency=txn.currency
        )
        return {"success": True, "capture_id": capture["id"], "state": TransactionState.CAPTURED}
    except Exception as e:
        logger.error(f"Capture failed: {str(e)}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Failed to capture order: {str(e)}")


# Refund route (require auth)
@router.post("/refunds")
async def create_refund(request: RefundRequest, _=Depends(require_auth)):
    """Create a refund for a captured transaction, requires confirm=True"""
    client = get_paypal_client()
    store = get_transaction_store()
    audit = get_audit_logger()

    if not request.confirm:
        raise HTTPException(status_code=400, detail="Refund requires confirm=True to execute")

    # Find transaction by capture ID (simplified)
    txn = None
    if store.redis:
        all_keys = await store.redis.keys("txn:*")
        for key in all_keys:
            data = await store.redis.get(key)
            if data:
                t = Transaction(**json.loads(data))
                if t.paypal_capture_id == request.capture_id:
                    txn = t
                    break
    else:
        for key, data in store.in_memory_store.items():
            if data.get("paypal_capture_id") == request.capture_id:
                txn = Transaction(**data)
                break

    if not txn:
        raise HTTPException(status_code=404, detail="No transaction found with that capture ID")

    if txn.state != TransactionState.CAPTURED:
        raise HTTPException(status_code=400, detail=f"Cannot refund transaction in state {txn.state}, must be CAPTURED")

    try:
        refund = await client.create_refund(request.model_dump())
        await store.transition_state(txn.id, TransactionState.REFUNDED, f"PayPal refund created: {refund['id']}")

        audit.log_money_movement(
            operation="refund_initiated", transaction_id=txn.id, amount=request.amount, currency=request.currency
        )
        return {"success": True, "refund_id": refund["id"], "state": TransactionState.REFUNDED}
    except Exception as e:
        logger.error(f"Refund failed: {str(e)}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Failed to create refund: {str(e)}")
