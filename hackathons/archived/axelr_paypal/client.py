"""Axelr PayPal — outbound integrations.

Two things live here because both are "make an HTTP call to somebody
else's API with retries and clear errors":
  1. PayPalClient — Orders v2, captures, refunds, webhook verification.
  2. AI extraction — OpenAI + Anthropic + regex fallback.

Honest note: we use `httpx` directly for both. We do NOT import the
`openai` or `anthropic` SDKs — that's why they're not in requirements.
If you swap to SDKs later, update requirements.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .core import get_core_module
from .schemas import (
    CreateOrderRequest,
    OrderItem,
    PaymentIntent,
)

logger = logging.getLogger("axelr.paypal.client")
ai_logger = logging.getLogger("axelr.paypal.ai")

PAYPAL_LIVE = "https://api-m.paypal.com"
PAYPAL_SANDBOX = "https://api-m.sandbox.paypal.com"
_TOKEN_SKEW_S = 60.0
_RETRYABLE = {500, 502, 503, 504}


class PayPalAPIError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


# ── Client ──────────────────────────────────────────────────────────────────
class PayPalClient:
    _instance: Optional["PayPalClient"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._initialized = False
        self._token_lock = asyncio.Lock()

    @property
    def _base_url(self) -> str:
        env = get_core_module().settings.environment
        return PAYPAL_LIVE if env == "live" else PAYPAL_SANDBOX

    @property
    def _http(self) -> httpx.AsyncClient:
        core = get_core_module()
        if core._http_client is None or core._http_client.is_closed:
            # Lazy-init so client can be used in tests without core.init().
            core.init() if not core._initialized else None
            if core._http_client is None:
                raise RuntimeError("http client not available")
        return core._http_client

    async def init(self) -> None:
        if self._initialized:
            return
        if not get_core_module().is_configured:
            logger.warning("PayPal credentials missing — client cannot call API")
            self._initialized = True
            return
        await self._ensure_token()
        self._initialized = True
        logger.info("PayPal client initialized env=%s",
                    get_core_module().settings.environment)

    async def _ensure_token(self) -> str:
        now = time.time()
        if self._token and now < (self._token_expires_at - _TOKEN_SKEW_S):
            return self._token
        async with self._token_lock:
            now = time.time()
            if self._token and now < (self._token_expires_at - _TOKEN_SKEW_S):
                return self._token
            settings = get_core_module().settings
            if not settings.client_id or not settings.client_secret:
                raise PayPalAPIError("PayPal credentials missing")
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.post(
                    f"{self._base_url}/v1/oauth2/token",
                    auth=(settings.client_id, settings.client_secret),
                    data={"grant_type": "client_credentials"},
                )
            if r.status_code >= 400:
                raise PayPalAPIError(f"auth failed status={r.status_code}",
                                     status=r.status_code)
            body = r.json()
            self._token = body["access_token"]
            self._token_expires_at = now + float(body.get("expires_in", 3600))
            return self._token

    async def _request(
        self, method: str, path: str, *,
        json_body: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        token = await self._ensure_token()
        headers = {"Authorization": f"Bearer {token}"}
        if idempotency_key:
            headers["PayPal-Request-Id"] = idempotency_key
        delay = 0.5
        last_err: Optional[Exception] = None
        for attempt in range(3):
            try:
                r = await self._http.request(
                    method, f"{self._base_url}{path}",
                    json=json_body, headers=headers,
                )
            except httpx.HTTPError as e:
                last_err = e
                if attempt < 2:
                    await _sleep(delay); delay *= 2; continue
                raise PayPalAPIError(f"network error: {e}") from e
            if r.status_code in _RETRYABLE and attempt < 2:
                await _sleep(delay); delay *= 2; continue
            if r.status_code >= 400:
                raise PayPalAPIError(
                    f"PayPal {r.status_code}: {r.text[:300]}",
                    status=r.status_code,
                )
            return r.json()
        raise PayPalAPIError(f"retries exhausted: {last_err}")

    async def create_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        validated = CreateOrderRequest(**payload)
        purchase_unit: Dict[str, Any] = {
            "reference_id": validated.reference_id or f"axelr_{int(time.time())}",
            "amount": {
                "currency_code": validated.currency,
                "value": str(validated.amount),
            },
        }
        if validated.items:
            purchase_unit["items"] = [
                {
                    "name": i.name,
                    "quantity": str(i.quantity),
                    "unit_amount": {
                        "currency_code": validated.currency,
                        "value": str(i.unit_amount),
                    },
                    "category": i.category,
                    **({"sku": i.sku} if i.sku else {}),
                }
                for i in validated.items
            ]
        body: Dict[str, Any] = {
            "intent": validated.intent,
            "purchase_units": [purchase_unit],
        }
        if validated.return_url or validated.cancel_url:
            body["application_context"] = {
                **({"return_url": validated.return_url} if validated.return_url else {}),
                **({"cancel_url": validated.cancel_url} if validated.cancel_url else {}),
            }
        logger.info("create_order amount=%s %s",
                    validated.amount, validated.currency)
        data = await self._request(
            "POST", "/v2/checkout/orders",
            json_body=body, idempotency_key=validated.idempotency_key,
        )
        approve = next(
            (l["href"] for l in data.get("links", []) if l.get("rel") == "approve"),
            None,
        )
        data["approve_url"] = approve
        return data

    async def get_order(self, order_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/v2/checkout/orders/{order_id}")

    async def capture_order(
        self, order_id: str, idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self._request(
            "POST", f"/v2/checkout/orders/{order_id}/capture",
            idempotency_key=idempotency_key,
        )

    async def create_refund(
        self, payload: Dict[str, Any], idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        from .schemas import RefundRequest
        validated = RefundRequest(**payload)
        body = {
            "amount": {
                "currency_code": validated.currency,
                "value": str(validated.amount),
            },
            **({"note_to_payer": validated.reason} if validated.reason else {}),
        }
        return await self._request(
            "POST", f"/v2/payments/captures/{validated.capture_id}/refund",
            json_body=body, idempotency_key=idempotency_key,
        )

    async def verify_webhook_signature(
        self, headers: Dict[str, str], body: str,
    ) -> bool:
        """Returns False if webhook_id missing — NEVER True to skip verification."""
        settings = get_core_module().settings
        if not settings.webhook_id:
            logger.warning("webhook_id missing — rejecting")
            return False
        # Header normalization: lowercase keys per PayPal docs.
        low = {k.lower(): v for k, v in headers.items()}
        required = [
            "paypal-auth-algo", "paypal-cert-url", "paypal-transmission-id",
            "paypal-transmission-sig", "paypal-transmission-time",
        ]
        if any(k not in low for k in required):
            logger.warning("webhook missing required headers")
            return False
        try:
            token = await self._ensure_token()
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.post(
                    f"{self._base_url}/v1/notifications/verify-webhook-signature",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "auth_algo": low["paypal-auth-algo"],
                        "cert_url": low["paypal-cert-url"],
                        "transmission_id": low["paypal-transmission-id"],
                        "transmission_sig": low["paypal-transmission-sig"],
                        "transmission_time": low["paypal-transmission-time"],
                        "webhook_id": settings.webhook_id,
                        "webhook_event": json.loads(body) if isinstance(body, str) else body,
                    },
                )
            if r.status_code != 200:
                return False
            return r.json().get("verification_status") == "SUCCESS"
        except Exception as e:
            logger.error("webhook verify error: %s", e)
            return False


async def _sleep(s: float) -> None:
    import asyncio
    await asyncio.sleep(s)


def get_paypal_client() -> PayPalClient:
    return PayPalClient()


# ── AI extraction ───────────────────────────────────────────────────────────
AMOUNT_PATTERNS: List[Tuple[str, str]] = [
    (r"\$\s*(\d+(?:\.\d{1,2})?)", "USD"),
    (r"€\s*(\d+(?:\.\d{1,2})?)", "EUR"),
    (r"£\s*(\d+(?:\.\d{1,2})?)", "GBP"),
    (r"(\d+\.\d{2})\s*USD", "USD"),
    (r"(\d+\.\d{2})\s*EUR", "EUR"),
    (r"(\d+\.\d{2})\s*GBP", "GBP"),
    (r"(\d+\.\d{2})\s*JPY", "JPY"),
    (r"(\d+\.\d{2})\s*(?:CAD|AUD|CHF|CNY)", "USD"),  # best-effort
    (r"(\d+)\s*(USD|EUR|GBP|JPY|CAD|AUD|CHF|CNY)", "USD"),  # "1000 EUR"
]
EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
NAME_PATTERN = re.compile(
    r"(?:from|payer|customer|bill to|invoice to|pay to)[:\s]+([A-Za-z][A-Za-z .'-]{1,60})",
    re.IGNORECASE,
)
DATE_PATTERN = re.compile(r"(?:due(?:\s+by)?|pay\s+by|deadline)[:\s]+(\d{4}-\d{2}-\d{2})",
                          re.IGNORECASE)

_SUB_KW = ("subscription", "monthly", "annual", "recurring", "subscribe")
_INV_KW = ("invoice", "bill", "payment due", "amount owed")
_PAYOUT_KW = ("payout", "send payment", "disburse", "transfer")


def _hash_raw_input(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _regex_fallback_extraction(text: str, raw_hash: str) -> PaymentIntent:
    amount: Optional[str] = None
    currency = "USD"
    for pat, cur in AMOUNT_PATTERNS:
        m = re.search(pat, text)
        if m:
            try:
                Decimal(m.group(1))
            except InvalidOperation:
                continue
            amount = m.group(1)
            currency = m.group(2).upper() if m.lastindex and m.lastindex >= 2 else cur
            break
    if not amount:
        raise ValueError("Failed to extract amount from text (regex fallback)")

    name_match = NAME_PATTERN.search(text)
    payer_name = name_match.group(1).strip() if name_match else "Unknown Payer"

    email_match = EMAIL_PATTERN.search(text)
    payer_email = email_match.group(0) if email_match else None

    date_match = DATE_PATTERN.search(text)
    due_date = date_match.group(1) if date_match else None

    low = text.lower()
    if any(k in low for k in _SUB_KW):
        ptype = "subscription"
    elif any(k in low for k in _PAYOUT_KW):
        ptype = "payout"
    else:
        ptype = "invoice"

    return PaymentIntent(
        payer_name=payer_name, payer_email=payer_email,
        amount=amount, currency=currency,
        items=[OrderItem(name="Extracted line", quantity=1, unit_amount=amount)],
        due_date=due_date, type=ptype, confidence=0.5,
        raw_input_hash=raw_hash,
    )


async def _extract_with_openai(
    text: str, api_key: str, raw_hash: str,
) -> PaymentIntent:
    prompt = (
        "Extract a JSON object with keys: payer_name, payer_email, amount "
        "(Decimal string), currency (3-letter ISO), items (array of "
        "{name, quantity, unit_amount}), due_date (ISO 8601 or null), type "
        "(invoice|subscription|payout), confidence (0-1). No prose."
    )
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "gpt-3.5-turbo-1106",
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": text},
                ],
            },
        )
    if r.status_code != 200:
        raise RuntimeError(f"openai {r.status_code}: {r.text[:200]}")
    data = json.loads(r.json()["choices"][0]["message"]["content"])
    data["raw_input_hash"] = raw_hash
    return PaymentIntent(**data)


async def _extract_with_anthropic(
    text: str, api_key: str, raw_hash: str,
) -> PaymentIntent:
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-3-haiku-20240307",
                "max_tokens": 1024,
                "messages": [{
                    "role": "user",
                    "content": (
                        f"Extract payment JSON from: {text}\n"
                        "Schema: payer_name, payer_email, amount (Decimal string), "
                        "currency (3-letter), items[{name,quantity,unit_amount}], "
                        "due_date, type, confidence. Return ONLY JSON."
                    ),
                }],
            },
        )
    if r.status_code != 200:
        raise RuntimeError(f"anthropic {r.status_code}: {r.text[:200]}")
    data = json.loads(r.json()["content"][0]["text"])
    data["raw_input_hash"] = raw_hash
    return PaymentIntent(**data)


async def extract_payment_intent(unstructured_text: str) -> PaymentIntent:
    raw_hash = _hash_raw_input(unstructured_text)
    settings = get_core_module().settings
    providers = []
    if settings.openai_api_key:
        providers.append(("openai", settings.openai_api_key))
    if settings.anthropic_api_key:
        providers.append(("anthropic", settings.anthropic_api_key))
    for name, key in providers:
        try:
            if name == "openai":
                out = await _extract_with_openai(unstructured_text, key, raw_hash)
            else:
                out = await _extract_with_anthropic(unstructured_text, key, raw_hash)
            ai_logger.info("extraction success provider=%s confidence=%s hash=%s",
                           name, out.confidence, raw_hash[:16])
            return out
        except Exception as e:
            ai_logger.warning("%s failed, trying next: %s", name, e)
    out = _regex_fallback_extraction(unstructured_text, raw_hash)
    ai_logger.info("regex fallback confidence=%.2f hash=%s",
                   out.confidence, raw_hash[:16])
    return out


__all__ = [
    "PayPalClient", "PayPalAPIError", "get_paypal_client",
    "extract_payment_intent", "_regex_fallback_extraction",
    "_hash_raw_input",
]
