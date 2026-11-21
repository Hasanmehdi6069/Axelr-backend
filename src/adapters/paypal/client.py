"""PayPal API client with retry logic and strict validation"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from .core import get_core_module
from .retry import paypal_retry_wrapper
from .schemas import (
    CreateOrderRequest,
    PayPalWebhookPayload,
    RefundRequest,
    validate_paypal_webhook,
)

logger = logging.getLogger("axelr.paypal.client")


class PayPalAPIError(Exception):
    """Custom exception for PayPal API errors"""

    def __init__(self, message: str, status_code: int = None, response: Dict[str, Any] = None):
        self.message = message
        self.status_code = status_code
        self.response = response
        super().__init__(message)


def _hash_raw_input(input_str: str) -> str:
    """Hash raw input string for payment intent matching"""
    import hashlib

    return hashlib.sha256(input_str.encode("utf-8")).hexdigest()


def _regex_fallback_extraction(input_str: str, raw_input_hash: str) -> Any:
    """Fallback regex extraction for payment intent when parsing fails"""
    import re

    from .schemas import OrderItem, PaymentIntent

    amount_pattern = r"\$?(\d+(?:\.\d{1,2})?)"
    currency_pattern = r"(USD|EUR|GBP|INR|CAD|AUD|JPY|BRL|MXN|SGD|HKD|NZD)"
    amounts = re.findall(amount_pattern, input_str)
    currencies = re.findall(currency_pattern, input_str)
    amount_str = amounts[0] if amounts else "0.00"
    currency = currencies[0] if currencies else "USD"
    return PaymentIntent(
        amount=amount_str,
        currency=currency,
        confidence=0.5,
        raw_input=input_str,
        extracted_from="regex_fallback",
        items=[OrderItem(name="Extracted Item", quantity=1, unit_amount=amount_str)],
        raw_input_hash=raw_input_hash,
    )


async def _extract_with_openai(text: str) -> Any:
    """Extract payment intent using OpenAI API (failover support)"""
    from .axelr_ai import _extract_with_openai as ai_extract_openai

    return await ai_extract_openai(text)


async def _extract_with_anthropic(text: str) -> Any:
    """Extract payment intent using Anthropic API (failover support)"""
    from .axelr_ai import _extract_with_anthropic as ai_extract_anthropic

    return await ai_extract_anthropic(text)


async def extract_payment_intent(input_str: str) -> Any:
    """Extract payment intent from natural language input"""
    from .axelr_ai import extract_payment_intent as ai_extract

    raw_input_hash = _hash_raw_input(input_str)
    # Try AI extraction first, fall back to regex
    try:
        intent = await ai_extract(input_str)
        # If it's already a PaymentIntent object, just return it
        from .schemas import PaymentIntent

        if isinstance(intent, PaymentIntent):
            # Ensure raw_input_hash is set
            intent.raw_input_hash = raw_input_hash
            return intent
        # If it's a dict, convert to PaymentIntent object
        from .schemas import OrderItem

        # Filter out audit fields
        filtered = {k: v for k, v in intent.items() if not k.startswith("_")}
        # Convert items to OrderItem objects
        if "items" in filtered:
            filtered["items"] = [OrderItem(**item) for item in filtered["items"]]
        # Add required fields if missing
        filtered.setdefault("raw_input", input_str)
        filtered.setdefault("extracted_from", "ai_extraction")
        filtered.setdefault("raw_input_hash", raw_input_hash)
        return PaymentIntent(**filtered)
    except Exception as e:
        logger.warning(f"AI extraction failed, falling back to regex: {str(e)}")
        return _regex_fallback_extraction(input_str, raw_input_hash)


class PayPalClient:
    """Production-grade PayPal API client with automatic retries and validation"""

    _instance: Optional["PayPalClient"] = None

    def __init__(self):
        self.core = get_core_module()
        self.base_url = (
            "https://api-m.paypal.com"
            if self.core.settings.environment == "live"
            else "https://api-m.sandbox.paypal.com"
        )
        self.access_token: Optional[str] = None
        self._initialized = False

        if not self.core.settings.client_id or not self.core.settings.client_secret:
            logger.warning("PayPal credentials not set - client cannot make real API calls")
        else:
            logger.info(f"PayPal client initialized - environment: {self.core.settings.environment}")

    @classmethod
    def get_instance(cls) -> "PayPalClient":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @paypal_retry_wrapper(max_retries=3)
    async def _authenticate(self) -> None:
        """Authenticate with PayPal to get access token"""
        if not self.core.settings.client_id or not self.core.settings.client_secret:
            raise RuntimeError("Cannot authenticate without PayPal credentials")

        auth = httpx.BasicAuth(self.core.settings.client_id, self.core.settings.client_secret)
        response = await self.core._http_client.post(
            f"{self.base_url}/v1/oauth2/token", auth=auth, data={"grant_type": "client_credentials"}
        )
        response.raise_for_status()
        token_data = response.json()
        self.access_token = token_data["access_token"]
        logger.info("Successfully authenticated with PayPal API")

    async def init(self) -> None:
        """Initialize the client and authenticate if credentials exist"""
        if self._initialized:
            return

        if self.core.settings.client_id and self.core.settings.client_secret:
            await self._authenticate()
        self._initialized = True
        logger.info("✅ PayPal client fully initialized")

    @paypal_retry_wrapper()
    async def create_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Create a PayPal order with full validation"""
        validated = CreateOrderRequest(**payload)

        # Build PayPal API-compliant payload
        paypal_payload = {
            "intent": validated.intent,
            "purchase_units": [
                {
                    "reference_id": validated.reference_id,
                    "amount": {"currency_code": validated.currency, "value": str(validated.amount)},
                    "items": [
                        {
                            "name": item.name,
                            "quantity": str(item.quantity),
                            "unit_amount": {"currency_code": validated.currency, "value": str(item.unit_amount)},
                            "category": item.category,
                            "sku": item.sku,
                        }
                        for item in validated.items
                    ],
                }
            ],
            "application_context": {"return_url": validated.return_url, "cancel_url": validated.cancel_url},
        }

        logger.info(f"Creating PayPal order: amount={validated.amount} {validated.currency}")

        if not self.access_token:
            raise RuntimeError("Client not authenticated - cannot create order")

        response = await self.core._http_client.post(
            f"{self.base_url}/v2/checkout/orders",
            json=paypal_payload,
            headers={"Authorization": f"Bearer {self.access_token}"},
        )
        response.raise_for_status()
        order_data = response.json()

        # Extract approve URL
        approve_url = next((link["href"] for link in order_data["links"] if link["rel"] == "approve"), None)
        order_data["approve_url"] = approve_url

        return order_data

    @paypal_retry_wrapper()
    async def capture_order(self, order_id: str, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """Capture an existing PayPal order"""
        logger.info(f"Capturing PayPal order: {order_id}")

        if not self.access_token:
            raise RuntimeError("Client not authenticated - cannot capture order")

        headers = {"Authorization": f"Bearer {self.access_token}"}
        if idempotency_key:
            headers["PayPal-Request-Id"] = idempotency_key

        response = await self.core._http_client.post(
            f"{self.base_url}/v2/checkout/orders/{order_id}/capture", headers=headers
        )
        response.raise_for_status()
        return response.json()

    @paypal_retry_wrapper()
    async def create_refund(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Create a refund for a captured payment"""
        validated = RefundRequest(**payload)
        if not validated.confirm:
            raise ValueError("Refund requires confirm=True to execute")

        logger.info(
            f"Creating PayPal refund for capture: {validated.capture_id}, amount={validated.amount} {validated.currency}"
        )

        if not self.access_token:
            raise RuntimeError("Client not authenticated - cannot create refund")

        refund_payload = {
            "amount": {"currency_code": validated.currency, "value": str(validated.amount)},
            "note_to_payer": validated.reason,
        }

        headers = {"Authorization": f"Bearer {self.access_token}"}
        if payload.get("idempotency_key"):
            headers["PayPal-Request-Id"] = payload["idempotency_key"]

        response = await self.core._http_client.post(
            f"{self.base_url}/v2/payments/captures/{validated.capture_id}/refund", json=refund_payload, headers=headers
        )
        response.raise_for_status()
        return response.json()

    @paypal_retry_wrapper()
    async def verify_webhook_signature(self, headers: Dict[str, str], body: str) -> bool:
        """Verify PayPal webhook signature using PayPal's API"""
        if not self.core.settings.webhook_id:
            logger.warning("Webhook ID not set - signature verification failed")
            return False

        if not self.access_token:
            raise RuntimeError("Client not authenticated - cannot verify webhook")

        verify_payload = {
            "auth_algo": headers.get("PAYPAL-AUTH-ALGO"),
            "cert_url": headers.get("PAYPAL-CERT-URL"),
            "transmission_id": headers.get("PAYPAL-TRANSMISSION-ID"),
            "transmission_sig": headers.get("PAYPAL-TRANSMISSION-SIG"),
            "transmission_time": headers.get("PAYPAL-TRANSMISSION-TIME"),
            "webhook_id": self.core.settings.webhook_id,
            "webhook_event": body,
        }

        response = await self.core._http_client.post(
            f"{self.base_url}/v1/notifications/verify-webhook-signature",
            json=verify_payload,
            headers={"Authorization": f"Bearer {self.access_token}"},
        )
        result = response.json()
        return result.get("verification_status") == "SUCCESS"

    @paypal_retry_wrapper()
    async def process_webhook(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Process and validate an incoming PayPal webhook"""
        validated: PayPalWebhookPayload = validate_paypal_webhook(payload)

        logger.info(f"Processing PayPal webhook: {validated.event_type}")
        return {"success": True, "event_type": validated.event_type, "processed": True}


def get_paypal_client() -> PayPalClient:
    """Get the singleton PayPal client instance"""
    return PayPalClient.get_instance()
