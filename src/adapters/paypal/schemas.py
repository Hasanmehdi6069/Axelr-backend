"""Pydantic v2 schemas for all PayPal module entry points. Strict, forbid extra."""
from __future__ import annotations

from enum import Enum
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

Currency = Literal[
    "USD", "EUR", "GBP", "CAD", "AUD", "JPY",
    "INR", "BRL", "MXN", "SGD", "HKD", "NZD",
]

IntentType = Literal["CAPTURE", "AUTHORIZE"]
ItemCategory = Literal["DIGITAL_GOODS", "PHYSICAL_GOODS", "DONATION"]


class TransactionState(str, Enum):
    INITIATED = "INITIATED"
    VALIDATED = "VALIDATED"
    PAYPAL_CREATED = "PAYPAL_CREATED"
    APPROVED = "APPROVED"
    CAPTURED = "CAPTURED"
    REFUNDED = "REFUNDED"

class PaymentIntent(BaseModel):
    """Model representing a parsed payment intent from natural language"""
    amount: str
    currency: Currency
    confidence: float
    raw_input: str
    extracted_from: str
    payer_name: str | None = None
    payer_email: str | None = None
    items: list[OrderItem] = Field(default_factory=list)
    due_date: str | None = None
    type: str = "invoice"
    raw_input_hash: str | None = None


class OrderItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(..., min_length=1, max_length=127)
    quantity: int = Field(..., ge=1, le=1000)
    unit_amount: Decimal = Field(..., gt=0, le=Decimal("100000"))
    category: ItemCategory = "DIGITAL_GOODS"
    sku: str | None = Field(default=None, max_length=127)


class CreateOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    amount: str = Field(..., description="Decimal amount as string, e.g. '99.99'")
    currency: Currency = "USD"
    intent: IntentType = "CAPTURE"
    description: str = Field(default="Axelr order", max_length=127)
    reference_id: str = Field(
        default="default",
        min_length=1, max_length=255,
        pattern=r"^[A-Za-z0-9_\-\.]+$",
    )
    return_url: str | None = Field(default=None, max_length=2048)
    cancel_url: str | None = Field(default=None, max_length=2048)
    items: list[OrderItem] = Field(default_factory=list, max_length=50)
    custom_id: str | None = Field(default=None, max_length=127)

    @field_validator("amount")
    @classmethod
    def validate_amount_decimal(cls, v: str) -> str:
        """Validate amount is a properly formatted Decimal string"""
        try:
            dec = Decimal(v)
        except Exception as e:
            raise ValueError(f"total amount must be a valid Decimal string: {v}") from e

        # Check for NaN or Infinity
        if not dec.is_finite():
            raise ValueError(f"Amount must be finite, got {v}")

        if dec <= 0 or dec > Decimal("1000000"):
            raise ValueError(f"Amount must be >0 and <=1000000, got {dec}")

        if dec != dec.quantize(Decimal("0.01")):
            raise ValueError("Amount must have at most 2 decimal places")
        return v

    @model_validator(mode="after")
    def validate_items_total(self) -> "CreateOrderRequest":
        """Validate sum of items matches total order amount within currency precision"""
        if not self.items:
            return self

        order_amount = Decimal(self.amount)
        items_total = sum((item.unit_amount * item.quantity for item in self.items), Decimal("0"))

        # Allow for 0.01 currency precision difference
        if abs(items_total - order_amount) > Decimal("0.01"):
            raise ValueError(
                f"Items total {items_total} does not match order total {order_amount}"
            )
        return self




class CaptureOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    order_id: str = Field(..., min_length=6, max_length=64, pattern=r"^[A-Z0-9]+$")
    idempotency_key: str | None = Field(default=None, max_length=108)


class RefundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    capture_id: str = Field(..., min_length=1, max_length=64)
    amount: str = Field(..., description="Decimal amount as string, e.g. '99.99'")
    currency: Currency = "USD"
    reason: str | None = Field(default=None, max_length=255)
    confirm: bool = Field(..., description="Must be true to execute refund")

    @field_validator("confirm")
    @classmethod
    def validate_confirm_true(cls, v: bool) -> bool:
        """Validate confirm is True"""
        if not v:
            raise ValueError("confirm must be True to process refund")
        return v

    @field_validator("amount")
    @classmethod
    def validate_amount_decimal(cls, v: str) -> str:
        """Validate amount is a properly formatted Decimal string"""
        try:
            dec = Decimal(v)
        except Exception as e:
            raise ValueError("total amount must be a valid Decimal string") from e

        if not dec.is_finite():
            raise ValueError("Amount must be finite")

        if dec <= 0 or dec > Decimal("1000000"):
            raise ValueError(f"Amount must be >0 and <=1000000, got {dec}")

        if dec != dec.quantize(Decimal("0.01")):
            raise ValueError("Amount must have at most 2 decimal places")
        return v


class WebhookEvent(BaseModel):
    """Strict PayPal webhook payload validation for all entry webhooks"""
    model_config = ConfigDict(extra="forbid")
    id: str = Field(..., min_length=1, max_length=128)
    event_type: str = Field(..., min_length=1, max_length=128)
    resource_type: str | None = Field(None)
    resource: dict[str, Any] = Field(default_factory=dict)
    create_time: str | None = None
    summary: str | None = None


class PayPalWebhookPayload(BaseModel):
    """Top-level webhook payload validation that enforces strict schema"""
    model_config = ConfigDict(extra="forbid")
    event_type: str = Field(..., min_length=1, max_length=128)
    webhook_event: WebhookEvent
    verify_signature: bool = Field(default=True)


def validate_paypal_webhook(payload: dict[str, Any]) -> PayPalWebhookPayload:
    """Validate an incoming PayPal webhook against strict Pydantic schemas"""
    # Handle real PayPal webhook format (flat structure)
    if "webhook_event" not in payload and "event_type" in payload:
        # Convert flat format to nested format
        normalized_payload = {
            "event_type": payload.get("event_type"),
            "webhook_event": {
                "id": payload.get("id"),
                "event_type": payload.get("event_type"),
                "resource_type": payload.get("resource_type"),
                "resource": payload.get("resource", {}),
                "create_time": payload.get("create_time"),
                "summary": payload.get("summary")
            },
            "verify_signature": True
        }
        return PayPalWebhookPayload(**normalized_payload)
    # Handle existing nested format
    try:
        return PayPalWebhookPayload(**payload)
    except ValidationError as e:
        raise ValueError(f"PayPal webhook validation failed: {str(e)}") from e


class StructuredError(BaseModel):
    success: Literal[False] = False
    error: dict[str, Any]