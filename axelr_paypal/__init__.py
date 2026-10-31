"""Axelr PayPal Integration — v1.1.0

Orders v2, Captures, Refunds, Webhook verification + idempotent dispatch.
"""
from .client import PayPalClient, get_client
from .core import (
    MODULE_VERSION,
    PayPalAPIError,
    close_http,
    health_probe,
    init,
    settings,
    shutdown,
)
from .routes import router
from .schemas import (
    CaptureOrderRequest,
    CreateOrderRequest,
    LineItem,
    RefundRequest,
    WebhookEvent,
)
from .webhooks import verify_and_dispatch

__version__ = MODULE_VERSION

__all__ = [
    "__version__", "MODULE_VERSION",
    "init", "shutdown", "health_probe", "close_http",
    "settings", "PayPalAPIError",
    "PayPalClient", "get_client",
    "CreateOrderRequest", "CaptureOrderRequest", "RefundRequest",
    "WebhookEvent", "LineItem",
    "verify_and_dispatch",
    "router",
]