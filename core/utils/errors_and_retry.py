# core/utils/errors_and_retry.py
"""
AXELR — Unified Error Taxonomy + Retry Policy
==============================================
Merged from core/errors.py and core/retry.py.
One comprehensive module for all error handling and retry logic in the system.

Error taxonomy provides a unified exception hierarchy for the entire request path.
Retry policy replaces four hand-rolled backoff loops with one tested primitive.
"""
from __future__ import annotations
import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger("axelr.utils.errors_and_retry")

__all__ = [
    # Error classes
    "AxelrError", "ProviderError", "DeadlineExceeded", "ValidationFailed",
    "RateLimited", "NoProviderAvailable", "CircuitOpen", "ConfigError",
    # Error utilities
    "is_retryable", "to_result",
    # Retry classes
    "RetryPolicy", "with_retry",
    # Predefined policies
    "POLICY_DEFAULT", "POLICY_NETWORK", "POLICY_IDEMPOTENT"
]

# ──────────────────────────────────────────────────────────────────────────────
# ERROR TAXONOMY
# ──────────────────────────────────────────────────────────────────────────────
class AxelrError(Exception):
    status: int = 500
    code: str = "internal_error"
    default_message: str = "Internal error"

    def __init__(self, message: str | None = None,
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(message or self.default_message)
        self.message = message or self.default_message
        self.details = details or {}

    # Compat with anti_hallucination.schemas.AxelrError field names
    @property
    def http_status(self) -> int:
        return self.status

    @property
    def error_code(self) -> str:
        return self.code

    def to_payload(self, document_id: str | None = None) -> dict[str, Any]:
        return {"status": self.code, "message": self.message,
                "details": self.details or None, "document_id": document_id}


class ProviderError(AxelrError):
    status, code, default_message = 503, "provider_error", "Upstream provider failed"


class DeadlineExceeded(AxelrError):
    status, code, default_message = 504, "deadline_exceeded", "Request deadline exceeded"


class ValidationFailed(AxelrError):
    status, code, default_message = 422, "validation_failed", "Output failed validation"


class RateLimited(AxelrError):
    status, code, default_message = 429, "rate_limited", "Provider rate-limited"


class NoProviderAvailable(AxelrError):
    status, code, default_message = 503, "no_provider_available", "No provider available"


class CircuitOpen(AxelrError):
    status, code, default_message = 503, "circuit_open", "Circuit breaker open"


class ConfigError(AxelrError):
    status, code, default_message = 500, "config_error", "Configuration error"


_RETRYABLE = (DeadlineExceeded, RateLimited, ProviderError)


def is_retryable(exc: BaseException) -> bool:
    """True iff retrying the same call is likely to succeed."""
    if isinstance(exc, _RETRYABLE):
        return True
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    # httpx status-code heuristics without importing httpx
    resp = getattr(exc, "response", None)
    if resp is not None:
        code = getattr(resp, "status_code", 0)
        return code in (408, 429) or code >= 500
    return False


def to_result(exc: BaseException, *, provider: str = "none",
              tier: str = "stub") -> dict[str, Any]:
    """Convert any exception to the route_fn return shape. Never raises."""
    if isinstance(exc, AxelrError):
        msg = exc.message
    else:
        msg = f"{type(ex).__name__}: {str(exc)[:200]}"
    return {"success": False, "text": "", "provider": provider,
            "model_used": "", "error": msg, "tier": tier, "degraded": True}

# ──────────────────────────────────────────────────────────────────────────────
# RETRY POLICY
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_s: float = 0.35
    max_delay_s: float = 8.0
    jitter: float = 0.15
    retry_on: tuple[type[BaseException], ...] = ()
    no_retry_on: tuple[type[BaseException], ...] = ()

    def should_retry(self, exc: BaseException) -> bool:
        if isinstance(exc, self.no_retry_on):
            return False
        if self.retry_on:
            return isinstance(exc, self.retry_on)
        return is_retryable(exc)

    def delay_for(self, attempt: int) -> float:
        exp = min(self.max_delay_s, self.base_delay_s * (2 ** (attempt - 1)))
        return exp * (1.0 + random.uniform(-self.jitter, self.jitter))


# Predefined retry policies for common use cases
POLICY_DEFAULT = RetryPolicy()
POLICY_NETWORK = RetryPolicy(max_attempts=4, base_delay_s=0.5, max_delay_s=12.0)
POLICY_IDEMPOTENT = RetryPolicy(max_attempts=2, base_delay_s=0.2, max_delay_s=2.0)


async def with_retry(
    fn: Callable[..., Awaitable[Any]],
    *args: Any,
    policy: RetryPolicy | None = None,
    on_retry: Callable[[int, BaseException], None] | None = None,
    **kw: Any,
) -> Any:
    """Call fn(*args, **kw) with the given policy. Raises the last exception."""
    policy = policy or RetryPolicy()
    last: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await fn(*args, **kw)
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            last = e
            if attempt >= policy.max_attempts or not policy.should_retry(e):
                raise
            if on_retry is not None:
                try: on_retry(attempt, e)
                except Exception: pass
            await asyncio.sleep(policy.delay_for(attempt))
    assert last is not None
    raise last