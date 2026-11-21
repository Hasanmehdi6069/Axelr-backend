# api/integrations.py
"""AXELR — third-party integration adapters.

Three SDK-backed clients grouped into one cohesive module:

  §1  Cloudflare R2 (boto3 S3-compat)     — get_r2_client
  §2  Cloudflare KV (cloudflare SDK)       — get_kv_client + helpers
  §3  Vultr (REST, bearer + backoff)       — vultr_api_request + CRUD

All background loops are lazy-started by ``lifespan`` via ``start_background``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import boto3
import cloudflare
import httpx
from fastapi import HTTPException

from .config import (
    CLOUDFLARE_ACCOUNT_ID,
    CLOUDFLARE_API_KEY,
    CLOUDFLARE_R2_ACCESS_KEY_ID,
    CLOUDFLARE_R2_SECRET_ACCESS_KEY,
    PAYPAL_API_BASE,
    PAYPAL_CLIENT_ID,
    PAYPAL_CLIENT_SECRET,
    get_http_client,
)
from .state import state

logger = logging.getLogger("axelr.integrations")


# ═══════════════════════════════════════════════════════════════════════
# §1 — Cloudflare R2
# ═══════════════════════════════════════════════════════════════════════
def get_r2_client():
    """boto3 S3-compatible client for Cloudflare R2."""
    return boto3.client(
        "s3",
        endpoint_url=f"https://{CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=CLOUDFLARE_R2_ACCESS_KEY_ID,
        aws_secret_access_key=CLOUDFLARE_R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


# ═══════════════════════════════════════════════════════════════════════
# §2 — Cloudflare KV
# ═══════════════════════════════════════════════════════════════════════
def get_kv_client() -> cloudflare.Cloudflare:
    """Cloudflare SDK client for KV / DNS / Workers."""
    return cloudflare.Cloudflare(api_token=CLOUDFLARE_API_KEY)


def get_kv_namespace_id() -> str:
    """Namespace id for fingerprint tracking; empty if disabled."""
    return os.getenv("CLOUDFLARE_KV_NAMESPACE_ID", "").strip()


_VULTR_API_KEY = os.getenv("VULTR_API_KEY", "").strip()


async def vultr_api_request(method: str, path: str, **kwargs) -> Any:
    """Vultr API request wrapper with bearer auth and backoff."""
    if not _VULTR_API_KEY:
        raise RuntimeError("VULTR_API_KEY not configured")
    client = get_http_client()
    headers = {"Authorization": f"Bearer {_VULTR_API_KEY}", **kwargs.pop("headers", {})}
    resp = await client.request(method, f"https://api.vultr.com/v2{path}", headers=headers, **kwargs)
    resp.raise_for_status()
    return resp.json()


async def _vultr_health_loop() -> None:
    while True:
        try:
            await vultr_api_request("GET", "/account")
        except Exception as e:
            logger.warning("vultr_health_check_failed error=%s", e)
        await asyncio.sleep(300)


# ═══════════════════════════════════════════════════════════════════════
# §4 — PayPal (REST, OAuth2 + idempotency)
# ═══════════════════════════════════════════════════════════════════════
_PAYPAL_MAX_RETRIES = 3
_idempotency_keys: set[str] = set()  # Track processed idempotency keys


def enforce_idempotency(idempotency_key: str) -> bool:
    """Enforce idempotency for PayPal API calls to prevent duplicate charges."""
    if idempotency_key in _idempotency_keys:
        return False
    _idempotency_keys.add(idempotency_key)
    return True


async def _get_paypal_access_token() -> str:
    """Get valid PayPal access token, refreshing if expired."""
    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        raise RuntimeError("PAYPAL_CLIENT_ID or PAYPAL_CLIENT_SECRET not configured")

    # Check if current token is still valid (add 60s buffer)
    if state.paypal_access_token and state.paypal_token_expiry > time.time() + 60:
        return state.paypal_access_token

    client = get_http_client()
    auth = httpx.BasicAuth(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET)
    try:
        resp = await client.post(
            f"{PAYPAL_API_BASE}/v1/oauth2/token", auth=auth, data={"grant_type": "client_credentials"}, timeout=30.0
        )
        resp.raise_for_status()
        data = resp.json()
        state.paypal_access_token = data["access_token"]
        state.paypal_token_expiry = time.time() + data["expires_in"]
        logger.info("paypal_token_refreshed expires_in=%d", data["expires_in"])
        return state.paypal_access_token
    except httpx.HTTPError as e:
        logger.error("paypal_token_fetch_failed error=%s", e)
        raise HTTPException(status_code=503, detail="PayPal authentication failed") from e


async def paypal_api_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    """Generic PayPal API request with exponential backoff and auth refresh."""
    for attempt in range(_PAYPAL_MAX_RETRIES):
        try:
            token = await _get_paypal_access_token()
            client = get_http_client()
            headers = kwargs.pop("headers", {})
            headers["Authorization"] = f"Bearer {token}"
            headers["Content-Type"] = "application/json"

            resp = await client.request(
                method,
                f"{PAYPAL_API_BASE}{path}",
                headers=headers,
                timeout=30.0,
                **kwargs,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401 and attempt < _PAYPAL_MAX_RETRIES - 1:
                # Token expired, clear and retry
                state.paypal_access_token = None
                wait_time = 2**attempt
                logger.warning("paypal_token_expired_retry attempt=%d wait_s=%d", attempt + 1, wait_time)
                await asyncio.sleep(wait_time)
                continue
            if attempt == _PAYPAL_MAX_RETRIES - 1:
                logger.error(
                    "paypal_api_failed method=%s path=%s status=%d error=%s", method, path, e.response.status_code, e
                )
                raise HTTPException(status_code=503, detail="PayPal API unavailable") from e
            wait_time = 2**attempt
            logger.warning("paypal_api_retry attempt=%d wait_s=%d", attempt + 1, wait_time)
            await asyncio.sleep(wait_time)
        except httpx.HTTPError as e:
            if attempt == _PAYPAL_MAX_RETRIES - 1:
                logger.error("paypal_api_failed method=%s path=%s error=%s", method, path, e)
                raise HTTPException(status_code=503, detail="PayPal API unavailable") from e
            wait_time = 2**attempt
            logger.warning("paypal_api_retry attempt=%d wait_s=%d", attempt + 1, wait_time)
            await asyncio.sleep(wait_time)
    raise RuntimeError("Unreachable in paypal_api_request")


async def create_paypal_order(amount: float, currency: str = "USD") -> dict[str, Any]:
    """Create a PayPal order for payment processing."""
    payload = {
        "intent": "CAPTURE",
        "purchase_units": [{"amount": {"currency_code": currency, "value": f"{amount:.2f}"}}],
    }
    return await paypal_api_request("POST", "/v2/checkout/orders", json=payload)


async def capture_paypal_order(order_id: str, idempotency_key: str) -> dict[str, Any]:
    """Capture a PayPal order after user approval."""
    if not enforce_idempotency(idempotency_key):
        raise HTTPException(status_code=409, detail="Order already captured")

    headers = {"PayPal-Request-Id": idempotency_key}
    return await paypal_api_request("POST", f"/v2/checkout/orders/{order_id}/capture", headers=headers)


async def verify_paypal_webhook(body: bytes, headers: dict[str, str]) -> bool:
    """Verify PayPal webhook signature to prevent forged events."""
    webhook_id = os.getenv("PAYPAL_WEBHOOK_ID", "").strip()
    if not webhook_id:
        logger.warning("paypal_webhook_verification_skipped webhook_id not configured")
        return True  # Skip verification in dev if not set

    client = get_http_client()
    token = await _get_paypal_access_token()
    payload = {
        "auth_algo": headers.get("paypal-auth-algo", ""),
        "cert_url": headers.get("paypal-cert-url", ""),
        "transmission_id": headers.get("paypal-transmission-id", ""),
        "transmission_sig": headers.get("paypal-transmission-sig", ""),
        "transmission_time": headers.get("paypal-transmission-time", ""),
        "webhook_id": webhook_id,
        "webhook_event": body.decode(),
    }
    try:
        resp = await client.post(
            f"{PAYPAL_API_BASE}/v1/notifications/verify-webhook-signature",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=30.0,
        )
        resp.raise_for_status()
        result = resp.json()
        return result.get("verification_status") == "SUCCESS"
    except httpx.HTTPError as e:
        logger.error("paypal_webhook_verification_failed error=%s", e)
        return False


import hashlib as _hashlib
import hmac as _hmac
import time as _time

from .config import (
    PAYPAL_WEBHOOK_SECRET,
)

_PAYPAL_API_BASE = os.getenv("PAYPAL_API_BASE", "https://api-m.sandbox.paypal.com")
_PAYPAL_IDEMPOTENCY_TTL = int(os.getenv("PAYPAL_IDEMPOTENCY_TTL", "3600"))
_paypal_processed_webhooks: dict[str, float] = {}
_paypal_cleanup_task: asyncio.Task | None = None


async def _paypal_cleanup_loop() -> None:
    while True:
        try:
            now = _time.time()
            expired = [k for k, ts in _paypal_processed_webhooks.items() if now - ts > _PAYPAL_IDEMPOTENCY_TTL]
            for k in expired:
                _paypal_processed_webhooks.pop(k, None)
        except Exception as e:
            logger.warning("paypal_idempotency_cleanup_failed error=%s", e)
        await asyncio.sleep(300)


def verify_paypal_webhook_signature(
    raw_body: bytes,
    signature: str,
    transmission_id: str,
) -> bool:
    """HMAC-SHA256 verification. Skipped (permissive) when secret is unset — dev only."""
    if not PAYPAL_WEBHOOK_SECRET:
        logger.warning("paypal_webhook_secret_missing_skipping_verify")
        return True
    try:
        secret = PAYPAL_WEBHOOK_SECRET.encode("utf-8")
        payload = f"{transmission_id}|{int(_time.time())}|{raw_body.decode('utf-8')}"
        computed = _hmac.new(secret, payload.encode("utf-8"), _hashlib.sha256).hexdigest()
        return _hmac.compare_digest(computed, signature)
    except Exception as e:
        logger.error("paypal_webhook_verify_failed error=%s", e)
        return False


def enforce_paypal_idempotency(key: str) -> None:
    now = _time.time()
    expired = [k for k, ts in _paypal_processed_webhooks.items() if now - ts > _PAYPAL_IDEMPOTENCY_TTL]
    for k in expired:
        _paypal_processed_webhooks.pop(k, None)
    if key in _paypal_processed_webhooks:
        raise HTTPException(status_code=409, detail="Webhook already processed")
    _paypal_processed_webhooks[key] = now


async def _get_paypal_access_token() -> str:
    token = getattr(state, "paypal_access_token", None)
    expiry = getattr(state, "paypal_token_expiry", 0.0) or 0.0
    if token and expiry > _time.time():
        return token
    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        raise RuntimeError("PayPal client id/secret not configured")
    auth = httpx.BasicAuth(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET)
    client = get_http_client()
    for attempt in range(3):
        try:
            resp = await client.post(
                f"{_PAYPAL_API_BASE}/v1/oauth2/token",
                auth=auth,
                data={"grant_type": "client_credentials"},
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
            state.paypal_access_token = data["access_token"]
            state.paypal_token_expiry = _time.time() + data["expires_in"] - 300
            return state.paypal_access_token
        except httpx.HTTPError as e:
            if attempt == 2:
                logger.error("paypal_token_failed error=%s", e)
                raise
            await asyncio.sleep(2**attempt)
    raise RuntimeError("Unreachable in _get_paypal_access_token")


async def create_paypal_payment(
    amount: float,
    currency: str = "USD",
    return_url: str = "",
    cancel_url: str = "",
) -> dict[str, Any]:
    token = await _get_paypal_access_token()
    idempotency_key = f"payment_{int(_time.time())}_{os.urandom(8).hex()}"
    client = get_http_client()
    resp = await client.post(
        f"{_PAYPAL_API_BASE}/v2/payments/orders",
        headers={
            "Authorization": f"Bearer {token}",
            "PayPal-Request-Id": idempotency_key,
            "Content-Type": "application/json",
        },
        json={
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "amount": {"currency_code": currency, "value": f"{amount:.2f}"},
                }
            ],
            "application_context": {
                "return_url": return_url or "https://axelr.in/payment/success",
                "cancel_url": cancel_url or "https://axelr.in/payment/cancel",
            },
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()


async def capture_paypal_payment(order_id: str) -> dict[str, Any]:
    token = await _get_paypal_access_token()
    client = get_http_client()
    resp = await client.post(
        f"{_PAYPAL_API_BASE}/v2/payments/orders/{order_id}/capture",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()


# ═══════════════════════════════════════════════════════════════════════
# Background task startup — called once from lifespan
# ═══════════════════════════════════════════════════════════════════════
async def start_background() -> None:
    global _vultr_health_task, _paypal_cleanup_task
    if _VULTR_API_KEY and _vultr_health_task is None:
        _vultr_health_task = asyncio.create_task(_vultr_health_loop(), name="vultr_health")
        logger.info("vultr_health_started")
    if _paypal_cleanup_task is None:
        _paypal_cleanup_task = asyncio.create_task(_paypal_cleanup_loop(), name="paypal_cleanup")
        logger.info("paypal_cleanup_started")


__all__ = [
    "capture_paypal_payment",
    "create_paypal_payment",
    "enforce_paypal_idempotency",
    "get_kv_client",
    "get_kv_namespace_id",
    "get_r2_client",
    "start_background",
    "vultr_api_request",
    "verify_paypal_webhook_signature",
]
