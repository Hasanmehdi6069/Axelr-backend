# axelr_paypal/schemas.py
"""Pydantic v2 schemas for all PayPal module entry points. Strict, forbid extra."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Currency = Literal[
    "USD", "EUR", "GBP", "CAD", "AUD", "JPY",
    "INR", "BRL", "MXN", "SGD", "HKD", "NZD",
]

IntentType = Literal["CAPTURE", "AUTHORIZE"]
ItemCategory = Literal["DIGITAL_GOODS", "PHYSICAL_GOODS", "DONATION"]


class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(..., min_length=1, max_length=127)
    quantity: int = Field(..., ge=1, le=1000)
    unit_amount: Decimal = Field(..., gt=0, le=Decimal("100000"))
    category: ItemCategory = "DIGITAL_GOODS"
    sku: str | None = Field(default=None, max_length=127)


class CreateOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    amount: Decimal = Field(..., gt=0, le=Decimal("1000000"))
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
    items: list[LineItem] = Field(default_factory=list, max_length=50)
    custom_id: str | None = Field(default=None, max_length=127)

    @field_validator("amount")
    @classmethod
    def _amount_two_dp(cls, v: Decimal) -> Decimal:
        if v != v.quantize(Decimal("0.01")):
            raise ValueError("amount must have at most 2 decimal places")
        return v

    @field_validator("items")
    @classmethod
    def _items_subtotal_matches(cls, items: list[LineItem], info: Any) -> list[LineItem]:
        if not items:
            return items
        subtotal = sum((i.unit_amount * i.quantity for i in items), Decimal("0"))
        amount = info.data.get("amount")
        if amount is not None and subtotal != amount:
            raise ValueError(
                f"items subtotal {subtotal} does not match amount {amount}"
            )
        return items


class CaptureOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    order_id: str = Field(..., min_length=6, max_length=64, pattern=r"^[A-Z0-9]+$")
    idempotency_key: str | None = Field(default=None, max_length=108)


class RefundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    capture_id: str = Field(..., min_length=6, max_length=64, pattern=r"^[A-Z0-9]+$")
    amount: Decimal | None = Field(default=None, gt=0, le=Decimal("1000000"))
    currency: Currency | None = None
    reason: str = Field(default="Customer request", max_length=255)
    confirm: bool = Field(..., description="Must be true to execute refund")


class WebhookEvent(BaseModel):
    """Loose model — we don't validate resource shape (PayPal varies by event)."""
    model_config = ConfigDict(extra="allow")
    id: str = Field(..., min_length=1, max_length=128)
    event_type: str = Field(..., min_length=1, max_length=128)
    resource_type: str | None = None
    resource: dict[str, Any] = Field(default_factory=dict)
    create_time: str | None = None


class StructuredError(BaseModel):
    success: Literal[False] = False
    error: dict[str, Any]