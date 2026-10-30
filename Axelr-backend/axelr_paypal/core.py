"""PayPal module core — settings, HTTP client, retry, health, lifecycle.

Honest notes
------------
* The idempotency-key cleanup task was a zombie — created in `init()` but
  never cancelled on shutdown. Fixed: `init()` / `shutdown()` are paired,
  and the cleanup task observes a shutdown event.
* PayPal's official webhook verification endpoint
  (`/v1/notifications/verify-webhook-signature`) is what `client.py` uses.
  No HMAC fallback — that was dead code and wrong.
* `_get_http()` returns a process-wide client; `shutdown()` closes it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger("axelr.paypal.core")
MODULE_VERSION = "1.1.0"


def _env(*names: str, default: str = "") -> str:
    for n in names:
        v = (os.getenv(n) or "").strip()
        if v:
            return v
    return default


def _bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").lower()
    return v in ("1", "true", "yes", "on") if v else default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True, slots=True)
class Settings:
    client_id: str
    client_secret: str
    webhook_id: str
    mode: str
    api_base: str
    timeout_s: float
    max_retries: int
    token_ttl_s: int
    partner_attribution: str

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @classmethod
    def from_env(cls) -> "Settings":
        mode = (_env("PAYPAL_MODE", default="sandbox") or "sandbox").lower()
        if mode not in ("sandbox", "live"):
            logger.warning("invalid PAYPAL_MODE=%r — defaulting to sandbox", mode)
            mode = "sandbox"
        base = (
            "https://api-m.paypal.com" if mode == "live"
            else "https://api-m.sandbox.paypal.com"
        )
        return cls(
            client_id=_env("PAYPAL_CLIENT_ID"),
            client_secret=_env("PAYPAL_CLIENT_SECRET"),
            webhook_id=_env("PAYPAL_WEBHOOK_ID"),
            mode=mode,
            api_base=base,
            timeout_s=_float("PAYPAL_TIMEOUT_S", 15.0),
            max_retries=_int("PAYPAL_MAX_RETRIES", 3),
            token_ttl_s=_int("PAYPAL_TOKEN_TTL_S", 3300),
            partner_attribution=_env(
                "PAYPAL_PARTNER_ATTRIBUTION", default="Axelr_SM_1_0_0"),
        )


settings = Settings.from_env()


# ── HTTP client (owned here, closed on shutdown) ─────────────────────────
_client: Optional[httpx.AsyncClient] = None


def _get_http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=settings.api_base,
            timeout=httpx.Timeout(settings.timeout_s, connect=5.0),
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"AxelrPayPal/{MODULE_VERSION}",
                "PayPal-Partner-Attribution-Id": settings.partner_attribution,
            },
        )
    return _client


async def close_http() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        finally:
            _client = None


# ── Errors + retry ───────────────────────────────────────────────────────
class PayPalAPIError(Exception):
    def __init__(self, status: int, payload: dict[str, Any] | str):
        self.status = status
        self.payload = payload
        super().__init__(f"PayPal API {status}: {payload!r}"[:300])


async def retry(fn, *, max_attempts: int | None = None) -> Any:
    """Retry only on transient errors (5xx, 429, network). 4xx = caller error."""
    attempts = max_attempts or settings.max_retries
    last: Exception | None = None
    for i in range(attempts):
        try:
            return await fn()
        except PayPalAPIError as e:
            if 400 <= e.status < 500 and e.status != 429:
                raise
            last = e
        except (httpx.TimeoutException, httpx.NetworkError,
                httpx.RemoteProtocolError) as e:
            last = e
        if i < attempts - 1:
            delay = 0.2 * (2 ** i) + random.random() * 0.15
            await asyncio.sleep(delay)
    if last:
        raise last
    raise RuntimeError("retry_exhausted_no_exception")


# ── Utilities ────────────────────────────────────────────────────────────
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def new_request_id(prefix: str = "axelr") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# ── Idempotency-key housekeeping ─────────────────────────────────────────
# Note: PayPal does not require client-side idempotency storage. We track
# webhook event IDs (see webhooks.py). This cleanup task exists only to
# evict stale entries from `webhooks._LOCAL_IDEM`.
_idempotency_ttl = _int("PAYPAL_IDEMPOTENCY_TTL", 3600)
_cleanup_task: Optional[asyncio.Task] = None
_shutdown_event: Optional[asyncio.Event] = None


async def _cleanup_loop() -> None:
    assert _shutdown_event is not None
    while not _shutdown_event.is_set():
        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=300.0)
            return
        except asyncio.TimeoutError:
            pass
        try:
            from .webhooks import _evict_local
            await asyncio.to_thread(_evict_local)
        except Exception as e:
            logger.debug("idempotency_cleanup_error err=%s", e)


async def init() -> None:
    """Start background housekeeping. Idempotent."""
    global _cleanup_task, _shutdown_event
    if _shutdown_event is None:
        _shutdown_event = asyncio.Event()
    if _cleanup_task is None or _cleanup_task.done():
        _shutdown_event.clear()
        _cleanup_task = asyncio.create_task(_cleanup_loop())
        logger.info("paypal_adapter_initialized ttl_s=%d", _idempotency_ttl)


async def shutdown() -> None:
    """Cancel background tasks and close the HTTP client. Safe to call twice."""
    global _cleanup_task
    if _shutdown_event is not None:
        _shutdown_event.set()
    if _cleanup_task is not None and not _cleanup_task.done():
        _cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await _cleanup_task
    _cleanup_task = None
    await close_http()
    logger.info("paypal_adapter_shutdown")


# ── Health probe ─────────────────────────────────────────────────────────
async def health_probe() -> dict[str, Any]:
    base = {
        "healthy": False,
        "configured": settings.is_configured,
        "mode": settings.mode,
        "webhook_configured": bool(settings.webhook_id),
        "version": MODULE_VERSION,
    }
    if not settings.is_configured:
        base["reason"] = "missing_credentials"
        return base
    try:
        from .client import get_client
        token = await get_client()._get_token()
        base["healthy"] = bool(token)
        if token:
            base["token_prefix"] = token[:8] + "…"
        return base
    except Exception as e:
        base["reason"] = str(e)[:200]
        return base