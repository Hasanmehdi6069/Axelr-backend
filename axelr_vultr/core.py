"""Vultr module core — settings, HTTP client, retry, rate limiter, health.

Honest fixes vs. v1.0.0
-----------------------
* Removed dead imports (`run_cognitive_pipeline`, `with_retry`,
  `RetryPolicy`, `get_http_client`) that were never invoked.
* `_health_check_task` (started by `init()`) is now cancellable via
  `shutdown()`; previously the task leaked.
* `_get_http()` client is closed by `shutdown()`; previously leaked.
* `settings` no longer has unused fields (`destroy_grace_s` and
  `wait_max_s` are still present because routes read them).
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

logger = logging.getLogger("axelr.vultr.core")
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


# ── Settings ─────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Settings:
    api_key: str
    base_url: str
    timeout_s: float
    max_retries: int
    rate_limit_per_min: int
    default_region: str
    monthly_budget_usd: float
    daily_provision_limit: int
    destroy_grace_s: int
    wait_max_s: int
    dry_run: bool

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            api_key=_env("VULTR_API_KEY"),
            base_url=_env("VULTR_API_URL", default="https://api.vultr.com/v2"),
            timeout_s=_float("VULTR_TIMEOUT_S", 20.0),
            max_retries=_int("VULTR_MAX_RETRIES", 3),
            rate_limit_per_min=_int("VULTR_RATE_LIMIT_PER_MIN", 480),
            default_region=_env("VULTR_DEFAULT_REGION", default="ewr"),
            monthly_budget_usd=_float("VULTR_MONTHLY_BUDGET_USD", 50.0),
            daily_provision_limit=_int("VULTR_DAILY_PROVISION_LIMIT", 10),
            destroy_grace_s=_int("VULTR_DESTROY_GRACE_S", 0),
            wait_max_s=_int("VULTR_WAIT_MAX_S", 240),
            dry_run=_bool("VULTR_DRY_RUN", False),
        )


settings = Settings.from_env()


# ── Errors ───────────────────────────────────────────────────────────────
class VultrAPIError(Exception):
    """Raised on non-2xx. Never leaks the API key."""

    def __init__(self, status: int, payload: dict[str, Any] | str):
        self.status = status
        self.payload = payload
        super().__init__(f"Vultr API {status}: {payload!r}"[:300])


class VultrRateLimited(VultrAPIError):
    def __init__(self, status: int, message: str):
        super().__init__(status, message)


# ── Rate limiter (token bucket) ──────────────────────────────────────────
class RateLimiter:
    """Client-side token bucket matching Vultr's 500/min server-side cap."""

    def __init__(self, capacity: int, refill_per_sec: float) -> None:
        self.capacity = float(capacity)
        self.refill_per_sec = refill_per_sec
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: float = 1.0, *, max_wait_s: float = 10.0) -> None:
        async with self._lock:
            now = time.monotonic()
            self.tokens = min(
                self.capacity,
                self.tokens + (now - self.last_refill) * self.refill_per_sec,
            )
            self.last_refill = now
            if self.tokens >= cost:
                self.tokens -= cost
                return
            deficit = cost - self.tokens
            wait = deficit / self.refill_per_sec
            if wait > max_wait_s:
                raise VultrRateLimited(429, "local_rate_limit_exceeded")
            await asyncio.sleep(wait)
            self.tokens = 0.0
            self.last_refill = time.monotonic()


_limiter = RateLimiter(
    capacity=settings.rate_limit_per_min,
    refill_per_sec=settings.rate_limit_per_min / 60.0,
)


def limiter() -> RateLimiter:
    return _limiter


# ── HTTP client ──────────────────────────────────────────────────────────
_client: Optional[httpx.AsyncClient] = None


def _get_http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=httpx.Timeout(settings.timeout_s, connect=5.0),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": f"AxelrVultr/{MODULE_VERSION}",
            },
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _client


async def close_http() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        finally:
            _client = None


# ── Retry ────────────────────────────────────────────────────────────────
async def retry(fn, *, max_attempts: int | None = None) -> Any:
    """Retry 5xx / 429 / network. 4xx (except 429) never retried."""
    attempts = max_attempts or settings.max_retries
    last: Exception | None = None
    for i in range(attempts):
        try:
            return await fn()
        except VultrAPIError as e:
            if 400 <= e.status < 500 and e.status != 429:
                raise
            last = e
        except (httpx.TimeoutException, httpx.NetworkError,
                httpx.RemoteProtocolError) as e:
            last = e
        if i < attempts - 1:
            delay = 0.3 * (2 ** i) + random.random() * 0.2
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


def mask_key(key: str) -> str:
    if not key:
        return "<unset>"
    if len(key) <= 6:
        return "*" * len(key)
    return f"{key[:4]}…{key[-2:]}"


# ── Background health check ──────────────────────────────────────────────
_health_check_task: Optional[asyncio.Task] = None
_health_shutdown: Optional[asyncio.Event] = None


async def _vultr_health_check() -> None:
    assert _health_shutdown is not None
    from .client import VultrClient
    client = VultrClient()
    while not _health_shutdown.is_set():
        try:
            await asyncio.wait_for(_health_shutdown.wait(), timeout=300.0)
            return
        except asyncio.TimeoutError:
            pass
        try:
            await client._request("GET", "/account")
            logger.debug("vultr_api_health_check_passed")
        except Exception as e:
            logger.warning("vultr_api_health_check_failed err=%s", e)


async def init() -> None:
    """Initialize Vultr adapter. Starts background health check if configured."""
    global _health_check_task, _health_shutdown
    if _health_shutdown is None:
        _health_shutdown = asyncio.Event()
    if not settings.api_key:
        logger.info("vultr_adapter_skipped reason=missing_api_key")
        return
    if _health_check_task is None or _health_check_task.done():
        _health_shutdown.clear()
        _health_check_task = asyncio.create_task(_vultr_health_check())
        logger.info("vultr_adapter_initialized base_url=%s", settings.base_url)


async def shutdown() -> None:
    global _health_check_task
    if _health_shutdown is not None:
        _health_shutdown.set()
    if _health_check_task is not None and not _health_check_task.done():
        _health_check_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await _health_check_task
    _health_check_task = None
    await close_http()
    logger.info("vultr_adapter_shutdown")


# ── Health probe (route handler + `/api/health/ready`) ────────────────────
async def health_probe() -> dict[str, Any]:
    base: dict[str, Any] = {
        "healthy": False,
        "configured": settings.is_configured,
        "mode": "dry_run" if settings.dry_run else "live",
        "api_key": mask_key(settings.api_key),
        "base_url": settings.base_url,
        "version": MODULE_VERSION,
    }
    if not settings.is_configured:
        base["reason"] = "missing_api_key"
        return base
    if settings.dry_run:
        base["healthy"] = True
        base["reason"] = "dry_run_mode_no_api_call"
        return base
    try:
        from .client import get_client
        account = await get_client().get_account()
        base["healthy"] = True
        base["account"] = {
            "email": account.get("email"),
            "balance": account.get("balance"),
            "pending_charges": account.get("pending_charges"),
        }
        return base
    except Exception as e:
        base["reason"] = str(e)[:200]
        return base