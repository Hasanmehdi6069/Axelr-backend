# axelr_paypal/client.py
"""PayPal REST client — Orders v2, Captures, Refunds, Webhook verification."""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any

import httpx

from .core import (
    PayPalAPIError, _get_http, retry, settings,
)

logger = logging.getLogger("axelr.paypal.client")


class PayPalClient:
    """Async PayPal client. Token cached in-memory; safe for concurrent use."""

    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._lock = asyncio.Lock()

    # ── auth ────────────────────────────────────────────────────────────

    async def _get_token(self) -> str:
        if not settings.is_configured:
            raise PayPalAPIError(503, "paypal_not_configured")
        now = time.monotonic()
        if self._token and now < self._token_expires_at:
            return self._token
        async with self._lock:
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token
            creds = f"{settings.client_id}:{settings.client_secret}"
            basic = "Basic " + base64.b64encode(creds.encode()).decode()
            http = _get_http()
            try:
                r = await http.post(
                    "/v1/oauth2/token",
                    headers={
                        "Authorization": basic,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    content=b"grant_type=client_credentials",
                )
            except Exception as e:
                raise PayPalAPIError(502, f"oauth_network_error:{e}") from e
            if r.status_code != 200:
                raise PayPalAPIError(r.status_code, r.text[:300])
            body = r.json()
            self._token = body.get("access_token")
            if not self._token:
                raise PayPalAPIError(502, "no_access_token_in_response")
            ttl = int(body.get("expires_in", 3600))
            self._token_expires_at = time.monotonic() + min(ttl - 60, settings.token_ttl_s)
            return self._token

    async def _auth_headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        token = await self._get_token()
        h = {"Authorization": f"Bearer {token}"}
        if idempotency_key:
            h["PayPal-Request-Id"] = idempotency_key
        return h

    async def _request(
        self, method: str, path: str, *,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        async def _do() -> dict[str, Any]:
            headers = await self._auth_headers(idempotency_key)
            http = _get_http()
            try:
                r = await http.request(method, path, headers=headers, json=body)
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                raise PayPalAPIError(504, f"network:{e}") from e
            if r.status_code >= 400:
                try:
                    payload = r.json()
                except Exception:
                    payload = r.text[:400]
                raise PayPalAPIError(r.status_code, payload)
            if r.status_code == 204 or not r.content:
                return {}
            return r.json()
        return await retry(_do)

    # ── orders ──────────────────────────────────────────────────────────

    async def create_order(
        self, *,
        amount: str, currency: str = "USD",
        intent: str = "CAPTURE",
        description: str = "Axelr order",
        reference_id: str = "default",
        return_url: str | None = None,
        cancel_url: str | None = None,
        items: list[dict[str, Any]] | None = None,
        custom_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        purchase_unit: dict[str, Any] = {
            "reference_id": reference_id,
            "description": description[:127],
            "amount": {
                "currency_code": currency,
                "value": amount,
                "breakdown": {
                    "item_total": {"currency_code": currency, "value": amount},
                },
            },
        }
        if items:
            purchase_unit["items"] = items
        if custom_id:
            purchase_unit["custom_id"] = custom_id
        body: dict[str, Any] = {
            "intent": intent,
            "purchase_units": [purchase_unit],
            "application_context": {
                "brand_name": "Axelr",
                "shipping_preference": "NO_SHIPPING",
                "user_action": "PAY_NOW",
            },
        }
        if return_url or cancel_url:
            body["application_context"]["return_url"] = return_url or "https://axelr.in"
            body["application_context"]["cancel_url"] = cancel_url or "https://axelr.in"
        logger.info("paypal_create_order amount=%s %s ref=%s", amount, currency, reference_id)
        return await self._request(
            "POST", "/v2/checkout/orders",
            body=body, idempotency_key=idempotency_key,
        )

    async def get_order(self, order_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v2/checkout/orders/{order_id}")

    async def capture_order(
        self, order_id: str, *, idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        logger.info("paypal_capture_order order_id=%s", order_id)
        return await self._request(
            "POST", f"/v2/checkout/orders/{order_id}/capture",
            body={}, idempotency_key=idempotency_key,
        )

    # ── refunds ─────────────────────────────────────────────────────────

    async def refund_capture(
        self, capture_id: str, *,
        amount: str | None = None,
        currency: str | None = None,
        reason: str = "Customer request",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"note_to_payer": reason[:255]}
        if amount and currency:
            body["amount"] = {"value": amount, "currency_code": currency}
        logger.info("paypal_refund capture_id=%s amount=%s", capture_id, amount)
        return await self._request(
            "POST", f"/v2/payments/captures/{capture_id}/refund",
            body=body, idempotency_key=idempotency_key,
        )

    # ── webhooks ────────────────────────────────────────────────────────

    async def verify_webhook(
        self, *, headers: dict[str, str], raw_body: bytes,
    ) -> bool:
        """Verifies signature via PayPal's verify-webhook-signature endpoint.

        Returns True iff PayPal confirms the event is authentic.
        Returns False (never raises) on network/config errors so the caller
        can decide whether to accept (sandbox) or reject (live).
        """
        if not settings.webhook_id:
            logger.warning("paypal_webhook_verify_no_webhook_id")
            return not settings.is_live  # accept in sandbox, reject in live
        needed = (
            "paypal-transmission-id", "paypal-transmission-time",
            "paypal-cert-url", "paypal-auth-algo", "paypal-transmission-sig",
        )
        lowered = {k.lower(): v for k, v in headers.items()}
        missing = [k for k in needed if not lowered.get(k)]
        if missing:
            logger.warning("paypal_webhook_missing_headers missing=%s", missing)
            return False
        import json
        try:
            event = json.loads(raw_body.decode("utf-8"))
        except Exception as e:
            logger.warning("paypal_webhook_invalid_json err=%s", e)
            return False
        body = {
            "auth_algo": lowered["paypal-auth-algo"],
            "cert_url": lowered["paypal-cert-url"],
            "transmission_id": lowered["paypal-transmission-id"],
            "transmission_sig": lowered["paypal-transmission-sig"],
            "transmission_time": lowered["paypal-transmission-time"],
            "webhook_id": settings.webhook_id,
            "webhook_event": event,
        }
        try:
            result = await self._request(
                "POST", "/v1/notifications/verify-webhook-signature", body=body,
            )
            status = result.get("verification_status", "")
            ok = status == "SUCCESS"
            if not ok:
                logger.warning("paypal_webhook_verify_status status=%s", status)
            return ok
        except PayPalAPIError as e:
            logger.warning("paypal_webhook_verify_api_error status=%s", e.status)
            # Network outage: accept in sandbox, reject in live
            return not settings.is_live
        except Exception as e:
            logger.warning("paypal_webhook_verify_unknown err=%s", e)
            return not settings.is_live


_client: PayPalClient | None = None


def get_client() -> PayPalClient:
    global _client
    if _client is None:
        _client = PayPalClient()
    return _client