"""Axelr PayPal — Pydantic v2 schemas.

Single source of truth for money-as-Decimals. Nothing in this file ever
uses `float` for money. Every Decimal is a `str` in transit.

Honest note: validation runs without credentials, so this file is
unit-testable offline. It cannot validate that the *server* accepts
the amount — only that the shape and arithmetic are sane.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import (
    BaseModel, ConfigDict, Field, field_validator, model_validator,
)

SUPPORTED_CURRENCIES: frozenset[str] = frozenset(
    {"USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "CNY"}
)
CURRENCY_PRECISION: Dict[str, int] = {
    "JPY": 0, "USD": 2, "EUR": 2, "GBP": 2,
    "CAD": 2, "AUD": 2, "CHF": 2, "CNY": 2,
}


class TransactionState(str, Enum):
    INITIATED = "INITIATED"
    VALIDATED = "VALIDATED"
    PAYPAL_CREATED = "PAYPAL_CREATED"
    APPROVED = "APPROVED"
    CAPTURED = "CAPTURED"
    REFUNDED = "REFUNDED"


VALID_TRANSITIONS: Dict[TransactionState, set[TransactionState]] = {
    TransactionState.INITIATED:      {TransactionState.VALIDATED},
    TransactionState.VALIDATED:      {TransactionState.PAYPAL_CREATED},
    TransactionState.PAYPAL_CREATED: {TransactionState.APPROVED},
    TransactionState.APPROVED:       {TransactionState.CAPTURED},
    TransactionState.CAPTURED:       {TransactionState.REFUNDED},
    TransactionState.REFUNDED:       set(),
}


def _validate_decimal_str(v: str, field: str) -> str:
    try:
        d = Decimal(v)
    except (InvalidOperation, TypeError):
        raise ValueError(f"{field} must be a valid Decimal string")
    if not d.is_finite():
        raise ValueError(f"{field} must be a finite number")
    if d <= 0:
        raise ValueError(f"{field} must be > 0")
    return v


class OrderItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(..., min_length=1, max_length=127)
    quantity: int = Field(..., ge=1)
    unit_amount: str = Field(..., description="Decimal string, never float")
    category: str = Field("DIGITAL_GOODS", max_length=32)
    sku: Optional[str] = Field(None, max_length=64)

    @field_validator("unit_amount")
    @classmethod
    def _v_unit_amount(cls, v: str) -> str:
        return _validate_decimal_str(v, "unit_amount")


class CreateOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    amount: str
    currency: str = Field(..., min_length=3, max_length=3)
    intent: str = Field("CAPTURE")
    description: Optional[str] = Field(None, max_length=255)
    reference_id: Optional[str] = Field(None, max_length=127)
    return_url: Optional[str] = None
    cancel_url: Optional[str] = None
    items: List[OrderItem] = Field(default_factory=list)
    custom_id: Optional[str] = None
    idempotency_key: Optional[str] = Field(None, max_length=64)

    @field_validator("amount")
    @classmethod
    def _v_amount(cls, v: str) -> str:
        return _validate_decimal_str(v, "total amount")

    @field_validator("currency")
    @classmethod
    def _v_currency(cls, v: str) -> str:
        u = v.upper()
        if u not in SUPPORTED_CURRENCIES:
            raise ValueError(f"unsupported currency {v}")
        return u

    @field_validator("intent")
    @classmethod
    def _v_intent(cls, v: str) -> str:
        if v not in {"CAPTURE", "AUTHORIZE"}:
            raise ValueError("intent must be CAPTURE or AUTHORIZE")
        return v

    @model_validator(mode="after")
    def _v_items_total(self) -> "CreateOrderRequest":
        if not self.items:
            return self
        p = CURRENCY_PRECISION.get(self.currency, 2)
        q = Decimal(f"1.{'0' * p}")
        items_total = Decimal("0")
        for it in self.items:
            items_total += Decimal(it.unit_amount) * Decimal(it.quantity)
        if items_total.quantize(q) != Decimal(self.amount).quantize(q):
            raise ValueError(
                f"sum of item amounts ({items_total.normalize()}) does not match "
                f"order total ({Decimal(self.amount).normalize()}) for {self.currency}"
            )
        return self


class CaptureOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str
    idempotency_key: Optional[str] = Field(None, max_length=64)


class RefundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capture_id: str
    amount: str
    currency: str = Field(..., min_length=3, max_length=3)
    reason: Optional[str] = Field(None, max_length=255)
    confirm: bool
    idempotency_key: Optional[str] = Field(None, max_length=64)

    @field_validator("confirm")
    @classmethod
    def _v_confirm(cls, v: bool) -> bool:
        if not v:
            raise ValueError("confirm must be True to process refund")
        return v

    @field_validator("amount")
    @classmethod
    def _v_amount(cls, v: str) -> str:
        return _validate_decimal_str(v, "refund amount")

    @field_validator("currency")
    @classmethod
    def _v_currency(cls, v: str) -> str:
        u = v.upper()
        if u not in SUPPORTED_CURRENCIES:
            raise ValueError(f"unsupported currency {v}")
        return u


class PaymentIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payer_name: str = Field(..., min_length=1)
    payer_email: Optional[str] = None
    amount: str
    currency: str = Field(..., min_length=3, max_length=3)
    items: List[OrderItem] = Field(..., min_length=1)
    due_date: Optional[str] = None
    type: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    raw_input_hash: str = Field(..., min_length=64, max_length=64)

    @field_validator("amount")
    @classmethod
    def _v_amount(cls, v: str) -> str:
        return _validate_decimal_str(v, "extracted amount")

    @field_validator("currency")
    @classmethod
    def _v_currency(cls, v: str) -> str:
        u = v.upper()
        if u not in SUPPORTED_CURRENCIES:
            raise ValueError(f"unsupported currency {v}")
        return u

    @field_validator("type")
    @classmethod
    def _v_type(cls, v: str) -> str:
        if v not in {"invoice", "subscription", "payout"}:
            raise ValueError("type must be invoice, subscription, or payout")
        return v


class AIExtractionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    unstructured_text: str = Field(..., min_length=1, max_length=10_000)
    approve: bool = Field(False)


class WebhookEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    event_type: str
    resource_type: str
    resource: Dict[str, Any]
    summary: Optional[str] = None
    create_time: str
    links: Optional[List[Dict[str, Any]]] = None


__all__ = [
    "SUPPORTED_CURRENCIES", "CURRENCY_PRECISION",
    "TransactionState", "VALID_TRANSITIONS",
    "OrderItem", "CreateOrderRequest", "CaptureOrderRequest", "RefundRequest",
    "PaymentIntent", "AIExtractionRequest", "WebhookEvent",
]