"""axelr-insights-engine — Configuration.

Reads from an explicit config dict OR environment variables.
Freezes at construction — immutable after init.

Client-side design:
  * Session mode: user-scoped access via session secret (browser/desktop)
  * Server mode:  admin access via API key (admin tooling)
  * Anonymous:    read-only public data (no credentials)

A missing required field is a hard error — never a silent default for
credentials.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal

from .errors import ConfigurationError, missing_env

Mode = Literal["server", "session", "anonymous"]

_REQUIRED_ENV = {
    "endpoint": "APPWRITE_ENDPOINT",
    "project_id": "APPWRITE_PROJECT_ID",
    "database_id": "APPWRITE_DATABASE_ID",
}


# ── Env readers ───────────────────────────────────────────────────────

def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v else None


def _str_env(name: str, default: str = "") -> str:
    v = _env(name)
    return v if v is not None else default


def _int_env(name: str, default: int, *, lo: int | None = None, hi: int | None = None) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    if lo is not None and v < lo:
        return lo
    if hi is not None and v > hi:
        return hi
    return v


def _float_env(name: str, default: float, *, lo: float | None = None, hi: float | None = None) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    if lo is not None and v < lo:
        return lo
    if hi is not None and v > hi:
        return hi
    return v


def _bool_env(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on", "t", "y")


# ── Config ────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class AppwriteConfig:
    """Immutable, validated configuration for the Insights Engine."""

    # Required
    endpoint: str
    project_id: str
    database_id: str

    # Auth (at least one form when acting on protected data)
    api_key: str = ""
    session_secret: str = ""

    # Mode — auto-detected if not explicit
    mode: Mode = "anonymous"

    # Rate limiting
    rate_limit_per_min: int = 100
    rate_limit_burst: int = 20

    # Circuit breaker
    circuit_threshold: int = 10
    circuit_cooldown_s: float = 60.0

    # Retry
    max_retries: int = 3
    retry_base_delay_s: float = 0.15
    retry_max_delay_s: float = 10.0
    retry_jitter: float = 0.15

    # Timeouts
    request_timeout_s: float = 30.0

    # Concurrency
    max_concurrent_requests: int = 16

    # Cache
    cache_enabled: bool = True
    cache_max_entries: int = 1000
    cache_ttl_s: float = 300.0

    # Logging
    log_level: str = "info"

    # Pagination defaults
    default_page_size: int = 100
    max_page_size: int = 5000

    # Free-form extra config (e.g. tenant id, feature flags)
    extras: dict[str, Any] = field(default_factory=dict)

    # ── Validation ──────────────────────────────────────────────────

    def __post_init__(self) -> None:
        missing: list[str] = []
        for field_name, env_name in _REQUIRED_ENV.items():
            if not getattr(self, field_name):
                missing.append(env_name)
        if missing:
            raise missing_env(missing)

        if not self.endpoint.startswith(("http://", "https://")):
            raise ConfigurationError(f"endpoint must be http(s) URL, got: {self.endpoint!r}")

        # Validate mode
        if self.mode not in ("server", "session", "anonymous"):
            raise ConfigurationError(
                f"invalid mode {self.mode!r}; must be 'server', 'session', or 'anonymous'"
            )

        # Validate mode-auth consistency
        if self.mode == "server" and not self.api_key:
            raise ConfigurationError("mode='server' requires api_key")
        if self.mode == "session" and not self.session_secret:
            # Not fatal — session can be set later at runtime
            pass

        # Validate numeric ranges
        if self.rate_limit_per_min < 1:
            raise ConfigurationError("rate_limit_per_min must be >= 1")
        if self.circuit_threshold < 1:
            raise ConfigurationError("circuit_threshold must be >= 1")
        if self.max_retries < 1:
            raise ConfigurationError("max_retries must be >= 1")
        if self.default_page_size < 1 or self.default_page_size > 100:
            raise ConfigurationError("default_page_size must be 1..100")
        if self.max_page_size < self.default_page_size:
            raise ConfigurationError("max_page_size must be >= default_page_size")

    # ── Factories ───────────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> "AppwriteConfig":
        """Read every field from environment variables."""
        from .env import env as _env
        a = _env.appwrite
        return cls(
            endpoint=a.endpoint,
            project_id=a.project_id,
            database_id=a.database_id,
            api_key=a.api_key,
            session_secret=a.session_secret,
            mode=a.effective_mode,
            rate_limit_per_min=a.rate_limit_per_min,
            rate_limit_burst=a.rate_limit_burst,
            circuit_threshold=a.circuit_threshold,
            circuit_cooldown_s=a.circuit_cooldown_s,
            max_retries=a.max_retries,
            retry_base_delay_s=a.retry_base_delay_s,
            retry_max_delay_s=a.retry_max_delay_s,
            retry_jitter=a.retry_jitter,
            request_timeout_s=a.request_timeout_s,
            max_concurrent_requests=a.max_concurrent,
            cache_enabled=a.cache_enabled,
            cache_max_entries=a.cache_max_entries,
            cache_ttl_s=a.cache_ttl_s,
            log_level=a.log_level,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppwriteConfig":
        """Build from an explicit dict. Unknown keys go to `extras`."""
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        extras = {k: v for k, v in data.items() if k not in known and k != "extras"}
        if "extras" in data:
            extras.update(data["extras"])
        kwargs = {k: v for k, v in data.items() if k in known and k != "extras"}

        # Auto-detect mode if not given
        if "mode" not in kwargs:
            if kwargs.get("api_key"):
                kwargs["mode"] = "server"
            elif kwargs.get("session_secret"):
                kwargs["mode"] = "session"
            else:
                kwargs["mode"] = "anonymous"

        return cls(**kwargs, extras=extras)

    # ── Derived properties ──────────────────────────────────────────

    @property
    def is_server_mode(self) -> bool:
        return self.mode == "server" and bool(self.api_key)

    @property
    def is_session_mode(self) -> bool:
        return self.mode == "session" and bool(self.session_secret)

    @property
    def is_anonymous(self) -> bool:
        return self.mode == "anonymous"

    @property
    def auth_kind(self) -> str:
        if self.is_server_mode:
            return "api_key"
        if self.is_session_mode:
            return "session"
        return "none"

    # ── Redaction ───────────────────────────────────────────────────

    def to_safe_dict(self) -> dict[str, Any]:
        """Safe for logging — never leaks credentials."""
        return {
            "endpoint": self.endpoint,
            "project_id": self.project_id,
            "database_id": self.database_id,
            "mode": self.mode,
            "auth_kind": self.auth_kind,
            "has_api_key": bool(self.api_key),
            "has_session": bool(self.session_secret),
            "rate_limit_per_min": self.rate_limit_per_min,
            "circuit_threshold": self.circuit_threshold,
            "max_retries": self.max_retries,
            "cache_enabled": self.cache_enabled,
            "extras": self.extras,
        }

    def __repr__(self) -> str:
        return f"AppwriteConfig({self.to_safe_dict()!r})"


# ── Defaults exported for reuse ───────────────────────────────────────

DEFAULTS: dict[str, Any] = {
    "rate_limit_per_min": 100,
    "rate_limit_burst": 20,
    "circuit_threshold": 10,
    "circuit_cooldown_s": 60.0,
    "max_retries": 3,
    "retry_base_delay_s": 0.15,
    "retry_max_delay_s": 10.0,
    "request_timeout_s": 30.0,
    "cache_max_entries": 1000,
    "cache_ttl_s": 300.0,
    "default_page_size": 100,
    "max_page_size": 5000,
}


def resolve_config(source: AppwriteConfig | dict[str, Any] | None) -> AppwriteConfig:
    """Resolve any config source into an AppwriteConfig."""
    if isinstance(source, AppwriteConfig):
        return source
    if isinstance(source, dict):
        return AppwriteConfig.from_dict(source)
    if source is None:
        return AppwriteConfig.from_env()
    raise ConfigurationError(f"cannot resolve config from {type(source).__name__}")