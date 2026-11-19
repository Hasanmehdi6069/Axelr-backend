"""axelr-insights-engine — Error taxonomy + validation.

Every error carries: status, code, details, retryable, timestamp.
Row-ID and data validation live here so any caller can validate
without importing the SDK.

Design:
  * `is_retryable(exc)` is the ONLY place retry policy is decided.
  * `normalize_error(exc)` maps any exception to our taxonomy.
  * `sanitize_row_id()` never raises; `ensure_valid_row_id()` does.
  * Every error implements `to_payload()` for structured logging.
"""
from __future__ import annotations

import hashlib
import re
import time
from typing import Any

# ═══════════════════════════════════════════════════════════════════════
# Base exception
# ═══════════════════════════════════════════════════════════════════════

class AppwriteError(Exception):
    status: int = 500
    code: str = "appwrite_error"
    default_message: str = "Appwrite error"
    retryable: bool = False

    def __init__(self, message: str | None = None, *, status: int | None = None,
                 code: str | None = None, details: dict[str, Any] | None = None,
                 retryable: bool | None = None) -> None:
        super().__init__(message or self.default_message)
        self.message = message or self.default_message
        if status is not None: self.status = status
        if code is not None: self.code = code
        if retryable is not None: self.retryable = retryable
        self.details = details or {}
        self.timestamp = time.time()

    def to_payload(self) -> dict[str, Any]:
        return {"name": self.__class__.__name__, "status": self.status,
                "code": self.code, "message": self.message,
                "details": self.details, "retryable": self.retryable,
                "timestamp": self.timestamp}

    def __repr__(self) -> str:
        return (f"{self.__class__.__name__}(status={self.status}, "
                f"code={self.code!r}, message={self.message!r})")


# ═══════════════════════════════════════════════════════════════════════
# Concrete taxonomy
# ═══════════════════════════════════════════════════════════════════════

class APIError(AppwriteError):
    default_message = "Appwrite API error"
    def __init__(self, message: str | dict | list | None = None, *, status: int = 500,
                 details: dict[str, Any] | None = None,
                 retryable: bool | None = None) -> None:
        if retryable is None:
            retryable = status in (408, 425, 429) or status >= 500
        if details is None:
            details = {"raw": message}
        text = message if isinstance(message, str) else f"HTTP {status}: {message}"
        super().__init__(text, status=status, code=f"http_{status}",
                         details=details, retryable=retryable)


class ConfigurationError(AppwriteError):
    status, code, default_message = 400, "configuration_error", "Missing or invalid configuration"

class ValidationFailed(AppwriteError):
    status, code, default_message = 422, "validation_failed", "Validation failed"

class NotFoundError(AppwriteError):
    status, code, default_message = 404, "not_found", "Resource not found"

class ConflictError(AppwriteError):
    status, code, default_message = 409, "conflict", "Resource conflict"

class UnauthorizedError(AppwriteError):
    status, code, default_message = 401, "unauthorized", "Authentication required"

class ForbiddenError(AppwriteError):
    status, code, default_message = 403, "forbidden", "Permission denied"

class RateLimitError(AppwriteError):
    status, code, default_message, retryable = 429, "rate_limited", "Rate limited", True

class CircuitBreakerOpenError(AppwriteError):
    status, code, default_message, retryable = 503, "circuit_open", "Circuit breaker is open", True

class OfflineError(AppwriteError):
    status, code, default_message, retryable = 503, "offline", "Backend unreachable", True

class TimeoutError_(AppwriteError):
    status, code, default_message, retryable = 504, "timeout", "Request timed out", True

class NetworkError(AppwriteError):
    status, code, default_message, retryable = 503, "network_error", "Network error", True

class ClientError(AppwriteError):
    status, code, default_message = 500, "client_error", "SDK call failed after retries"

class SchemaError(AppwriteError):
    status, code, default_message = 500, "schema_error", "Schema mismatch or missing"

class VersionConflictError(AppwriteError):
    status, code, default_message = 409, "version_conflict", "Optimistic lock failed"

class IdempotencyConflictError(AppwriteError):
    status, code, default_message = 409, "idempotency_conflict", "Idempotency key reused with different payload"


# ═══════════════════════════════════════════════════════════════════════
# Retry policy + classification
# ═══════════════════════════════════════════════════════════════════════

_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
_RETRYABLE_CODES = frozenset({"timeout", "network_error", "offline",
                              "rate_limited", "circuit_open"})
_RETRYABLE_NAMES = frozenset({"TimeoutError", "TimeoutError_", "NetworkError",
                              "ConnectionError", "BrokenPipeError",
                              "ConnectionResetError"})


def is_retryable(exc: BaseException) -> bool:
    if not exc: return False
    if isinstance(exc, AppwriteError):
        if exc.retryable: return True
        if exc.status in _RETRYABLE_STATUS: return True
        if exc.code in _RETRYABLE_CODES: return True
        return False
    if exc.__class__.__name__ in _RETRYABLE_NAMES: return True
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    if isinstance(status, int) and status in _RETRYABLE_STATUS: return True
    return False


def is_not_found(exc: BaseException) -> bool:
    return isinstance(exc, NotFoundError) or getattr(exc, "status", None) == 404

def is_conflict(exc: BaseException) -> bool:
    return isinstance(exc, ConflictError) or getattr(exc, "status", None) == 409

def is_auth_error(exc: BaseException) -> bool:
    return isinstance(exc, (UnauthorizedError, ForbiddenError))

def classify_error(exc: BaseException) -> str:
    """Return one of: retryable, auth, validation, not_found, conflict, other."""
    if is_retryable(exc): return "retryable"
    if is_auth_error(exc): return "auth"
    if isinstance(exc, ValidationFailed): return "validation"
    if is_not_found(exc): return "not_found"
    if is_conflict(exc): return "conflict"
    return "other"


# ═══════════════════════════════════════════════════════════════════════
# Normalisation
# ═══════════════════════════════════════════════════════════════════════

def normalize_error(exc: BaseException | Any, fallback: str = "Unknown error") -> AppwriteError:
    if isinstance(exc, AppwriteError): return exc
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None) or 500
    message = getattr(exc, "message", None) or str(exc) or fallback
    code = getattr(exc, "code", None) or f"http_{status}"
    if status == 401: return UnauthorizedError(message, details={"raw": str(exc)})
    if status == 403: return ForbiddenError(message, details={"raw": str(exc)})
    if status == 404: return NotFoundError(message, details={"raw": str(exc)})
    if status == 409: return ConflictError(message, details={"raw": str(exc)})
    if status == 422: return ValidationFailed(message, details={"raw": str(exc)})
    if status == 429: return RateLimitError(message, details={"raw": str(exc)})
    return APIError(message, status=status, details={"raw": str(exc), "code": code})


def wrap_sdk_error(exc: BaseException, **context: Any) -> AppwriteError:
    err = normalize_error(exc)
    if context: err.details = {**err.details, **context}
    return err


def to_result(exc: BaseException, **meta: Any) -> dict[str, Any]:
    err = normalize_error(exc)
    return {"ok": False, "error": err.message, "code": err.code,
            "status": err.status, "retryable": err.retryable, **meta}


def missing_env(names: str | list[str]) -> ConfigurationError:
    items = [names] if isinstance(names, str) else list(names)
    return ConfigurationError(f"missing required config: {', '.join(items)}",
                              details={"missing": items})


def api_error(status: int, message: str | None = None, **details: Any) -> APIError:
    return APIError(message or f"HTTP {status}", status=status, details=details or None)


# ═══════════════════════════════════════════════════════════════════════
# Row-ID + data validation (merged from validation.py)
# ═══════════════════════════════════════════════════════════════════════

_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,35}$")
_BAD_CHARS = re.compile(r"[^a-zA-Z0-9._-]")
_TABLE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,35}$")
MAX_ID_LEN = 36
MAX_FIELD_BYTES = 1_048_576


def is_valid_row_id(row_id: str | None) -> bool:
    return bool(row_id) and bool(_ID_RE.match(row_id))


def sanitize_row_id(row_id: str | None, *, fallback_prefix: str = "row") -> str:
    """Return a legal Appwrite row ID. Never raises.

    - Empty/None → ``{prefix}_{epoch_ms}``
    - Bad chars → replaced with ``-``
    - Too long → truncated with 8-char hash suffix
    - Leading non-alnum → prefixed with ``{prefix}_``
    """
    if not row_id:
        return f"{fallback_prefix}_{int(time.time() * 1000)}"
    cleaned = _BAD_CHARS.sub("-", row_id)
    if not cleaned[0].isalnum():
        cleaned = f"{fallback_prefix}_{cleaned}"
    if len(cleaned) > MAX_ID_LEN:
        digest = hashlib.sha256(row_id.encode()).hexdigest()[:8]
        cleaned = cleaned[: MAX_ID_LEN - 9] + "_" + digest
    return cleaned


def ensure_valid_row_id(row_id: str | None) -> str:
    if not is_valid_row_id(row_id):
        raise ValidationFailed(
            f"invalid row_id {row_id!r}: must be 1-{MAX_ID_LEN} chars, "
            f"start alphanumeric, contain only [a-zA-Z0-9._-]")
    assert row_id is not None
    return row_id


def make_row_id(*parts: str) -> str:
    """Build a sanitized, deterministic row ID from parts."""
    return sanitize_row_id("-".join(p for p in parts if p))


def validate_table_name(table: str) -> str:
    if not table or not _TABLE_RE.match(table):
        raise ValidationFailed(
            f"invalid table name {table!r}: must start with a letter, "
            f"contain only [a-zA-Z0-9_], max {MAX_ID_LEN} chars")
    return table


def validate_data(data: dict[str, Any], *, max_size: int = MAX_FIELD_BYTES) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValidationFailed("data must be a dict")
    out: dict[str, Any] = {}
    for k, v in data.items():
        if not isinstance(k, str) or not k:
            raise ValidationFailed(f"invalid field name: {k!r}")
        if isinstance(v, str) and len(v.encode("utf-8")) > max_size:
            raise ValidationFailed(
                f"field {k!r} exceeds max size {max_size} bytes")
        out[k] = v
    return out


# Backward-compat alias
TimeoutErrorAlias = TimeoutError_
