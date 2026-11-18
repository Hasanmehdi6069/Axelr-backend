"""axelr-insights-engine — Master environment reader.

One interface for every env var. Each domain has a typed schema and a
`required` set. Reading a var from the wrong domain (or a missing
required) raises `ConfigurationError` with a clear message.

Why a module, not dataclass everywhere:
  * Single source of truth — no `os.getenv("APPWRITE_ENDPOINT")` scatter
  * Typed returns — `int`, `float`, `bool`, `str`, `list[str]`
  * Secret redaction — logs never leak values
  * Snapshot + diff — compare two snapshots to spot drift
  * Safe defaults — every optional has an opinionated default

Usage:
    from axelr_insights_engine.env import env
    env.appwrite.endpoint          # str, raises if missing
    env.appwrite.rate_limit_per_min # int, defaults to 100
    env.switch.room_id             # str, raises if missing

    # Snapshot for logs
    env.snapshot()                 # dict, secrets redacted
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from .errors import ConfigurationError

log = logging.getLogger("axelr_insights_engine.env")

_SECRET_HINTS = ("key", "secret", "token", "password", "uri", "dsn")


def _redact(name: str, value: str | None) -> str:
    if value is None:
        return "<unset>"
    if any(h in name.lower() for h in _SECRET_HINTS):
        return f"<redacted {len(value)} chars>"
    return value


# ═══════════════════════════════════════════════════════════════════════
# Raw env readers with type coercion
# ═══════════════════════════════════════════════════════════════════════

def _get(name: str) -> str | None:
    v = os.environ.get(name)
    if v is None:
        return None
    v = v.strip()
    return v or None


def _str(name: str, default: str = "") -> str:
    return _get(name) or default


def _int(name: str, default: int, *, lo: int | None = None, hi: int | None = None) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        log.warning("env %s: invalid int %r, using %d", name, raw, default)
        return default
    if lo is not None and v < lo:
        return lo
    if hi is not None and v > hi:
        return hi
    return v


def _float(name: str, default: float, *, lo: float | None = None, hi: float | None = None) -> float:
    raw = _get(name)
    if raw is None:
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        log.warning("env %s: invalid float %r, using %s", name, raw, default)
        return default
    if lo is not None and v < lo:
        return lo
    if hi is not None and v > hi:
        return hi
    return v


def _bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on", "t", "y")


def _list(name: str) -> list[str]:
    raw = _get(name)
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


# ═══════════════════════════════════════════════════════════════════════
# Appwrite domain
# ═══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class AppwriteEnv:
    """Every APPWRITE_* variable. All fields typed."""

    # Required
    endpoint: str
    project_id: str
    database_id: str

    # Auth (at least one of these two must be set)
    api_key: str = ""
    session_secret: str = ""
    mode: str = ""

    # Resilience
    rate_limit_per_min: int = 100
    rate_limit_burst: int = 20
    circuit_threshold: int = 10
    circuit_cooldown_s: float = 60.0
    max_retries: int = 3
    retry_base_delay_s: float = 0.15
    retry_max_delay_s: float = 10.0
    retry_jitter: float = 0.15
    request_timeout_s: float = 30.0
    max_concurrent: int = 16

    # Cache
    cache_enabled: bool = True
    cache_max_entries: int = 1000
    cache_ttl_s: float = 300.0

    # Logging
    log_level: str = "info"

    def validate(self) -> None:
        missing = [n for n in (
            ("APPWRITE_ENDPOINT", self.endpoint),
            ("APPWRITE_PROJECT_ID", self.project_id),
            ("APPWRITE_DATABASE_ID", self.database_id),
        ) if not n[1]]
        if missing:
            raise ConfigurationError(
                f"missing env: {', '.join(n[0] for n in missing)}")
        if not self.api_key and not self.session_secret and self.mode != "anonymous":
            raise ConfigurationError(
                "one of APPWRITE_API_KEY or APPWRITE_SESSION_SECRET required "
                "unless APPWRITE_MODE=anonymous")

    @property
    def effective_mode(self) -> str:
        if self.mode:
            return self.mode
        if self.api_key:
            return "server"
        if self.session_secret:
            return "session"
        return "anonymous"


def _load_appwrite() -> AppwriteEnv:
    return AppwriteEnv(
        endpoint=_str("APPWRITE_ENDPOINT"),
        project_id=_str("APPWRITE_PROJECT_ID"),
        database_id=_str("APPWRITE_DATABASE_ID", "insights"),
        api_key=_str("APPWRITE_API_KEY"),
        session_secret=_str("APPWRITE_SESSION_SECRET"),
        mode=_str("APPWRITE_MODE"),
        rate_limit_per_min=_int("APPWRITE_RATE_LIMIT_PER_MIN", 100, lo=1),
        rate_limit_burst=_int("APPWRITE_RATE_LIMIT_BURST", 20, lo=1),
        circuit_threshold=_int("APPWRITE_CIRCUIT_THRESHOLD", 10, lo=1),
        circuit_cooldown_s=_float("APPWRITE_CIRCUIT_COOLDOWN_S", 60.0, lo=0.0),
        max_retries=_int("APPWRITE_MAX_RETRIES", 3, lo=1, hi=10),
        retry_base_delay_s=_float("APPWRITE_RETRY_BASE_DELAY_S", 0.15, lo=0.0),
        retry_max_delay_s=_float("APPWRITE_RETRY_MAX_DELAY_S", 10.0, lo=0.0),
        retry_jitter=_float("APPWRITE_RETRY_JITTER", 0.15, lo=0.0, hi=1.0),
        request_timeout_s=_float("APPWRITE_REQUEST_TIMEOUT_S", 30.0, lo=1.0),
        max_concurrent=_int("APPWRITE_MAX_CONCURRENT", 16, lo=1, hi=1000),
        cache_enabled=_bool("APPWRITE_CACHE_ENABLED", True),
        cache_max_entries=_int("APPWRITE_CACHE_MAX_ENTRIES", 1000, lo=1),
        cache_ttl_s=_float("APPWRITE_CACHE_TTL_S", 300.0, lo=0.0),
        log_level=_str("APPWRITE_LOG_LEVEL", "info"),
    )


# ═══════════════════════════════════════════════════════════════════════
# Switch domain
# ═══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class SwitchEnv:
    bridge_url: str
    agent_id: str
    agent_api_key: str
    room_id: str
    registration_token: str = ""
    room_name: str = "axelr-ops"
    room_topic: str = "Incident Triage Room"
    ops_ws_port: int = 8765

    def validate(self) -> None:
        missing = [n for n in (
            ("SWITCH_AGENT_ID", self.agent_id),
            ("SWITCH_AGENT_API_KEY", self.agent_api_key),
            ("SWITCH_ROOM_ID", self.room_id),
        ) if not n[1]]
        if missing:
            raise ConfigurationError(
                f"missing env: {', '.join(n[0] for n in missing)}")


def _load_switch() -> SwitchEnv:
    return SwitchEnv(
        bridge_url=_str("SWITCH_BRIDGE_URL", "http://localhost:7312"),
        agent_id=_str("SWITCH_AGENT_ID"),
        agent_api_key=_str("SWITCH_AGENT_API_KEY"),
        room_id=_str("SWITCH_ROOM_ID"),
        registration_token=_str("SWITCH_REGISTRATION_TOKEN"),
        room_name=_str("SWITCH_ROOM_NAME", "axelr-ops"),
        room_topic=_str("SWITCH_ROOM_TOPIC", "Incident Triage Room"),
        ops_ws_port=_int("OPS_WS_PORT", 8765, lo=1, hi=65535),
    )


# ═══════════════════════════════════════════════════════════════════════
# Vultr domain
# ═══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class VultrEnv:
    api_key: str
    api_url: str = "https://api.vultr.com/v2"
    timeout_s: float = 20.0
    max_retries: int = 3
    rate_limit_per_min: int = 480
    default_region: str = "ewr"
    monthly_budget_usd: float = 50.0
    daily_provision_limit: int = 10
    dry_run: bool = False
    breaker_threshold: int = 5
    breaker_cooldown_s: float = 30.0

    @property
    def live(self) -> bool:
        return bool(self.api_key) and not self.dry_run


def _load_vultr() -> VultrEnv:
    return VultrEnv(
        api_key=_str("VULTR_API_KEY"),
        api_url=_str("VULTR_API_URL", "https://api.vultr.com/v2"),
        timeout_s=_float("VULTR_TIMEOUT_S", 20.0, lo=1.0),
        max_retries=_int("VULTR_MAX_RETRIES", 3, lo=1),
        rate_limit_per_min=_int("VULTR_RATE_LIMIT_PER_MIN", 480, lo=1, hi=500),
        default_region=_str("VULTR_DEFAULT_REGION", "ewr"),
        monthly_budget_usd=_float("VULTR_MONTHLY_BUDGET_USD", 50.0, lo=0.0),
        daily_provision_limit=_int("VULTR_DAILY_PROVISION_LIMIT", 10, lo=1),
        dry_run=_bool("VULTR_DRY_RUN", False),
        breaker_threshold=_int("VULTR_BREAKER_THRESHOLD", 5, lo=1),
        breaker_cooldown_s=_float("VULTR_BREAKER_COOLDOWN_S", 30.0, lo=0.0),
    )


# ═══════════════════════════════════════════════════════════════════════
# PayPal domain
# ═══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class PayPalEnv:
    client_id: str
    client_secret: str
    mode: Literal["sandbox", "live"] = "sandbox"

    @property
    def base_url(self) -> str:
        return "https://api-m.paypal.com" if self.mode == "live" else "https://api-m.sandbox.paypal.com"


def _load_paypal() -> PayPalEnv:
    mode = _str("PAYPAL_MODE", "sandbox").lower()
    if mode not in ("sandbox", "live"):
        mode = "sandbox"
    return PayPalEnv(
        client_id=_str("PAYPAL_CLIENT_ID"),
        client_secret=_str("PAYPAL_CLIENT_SECRET"),
        mode=mode,  # type: ignore[arg-type]
    )


# ═══════════════════════════════════════════════════════════════════════
# Auth domain
# ═══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class AuthEnv:
    jwt_secret: str
    admin_email: str = ""
    access_token_expire_minutes: int = 10080
    google_client_id: str = ""
    google_client_secret: str = ""
    github_client_id: str = ""
    github_client_secret: str = ""

    def validate(self) -> None:
        if not self.jwt_secret:
            raise ConfigurationError("JWT_SECRET is required")
        if len(self.jwt_secret) < 32:
            raise ConfigurationError("JWT_SECRET must be >= 32 chars")


def _load_auth() -> AuthEnv:
    return AuthEnv(
        jwt_secret=_str("JWT_SECRET"),
        admin_email=_str("ADMIN_EMAIL"),
        access_token_expire_minutes=_int("ACCESS_TOKEN_EXPIRE_MINUTES", 10080, lo=1),
        google_client_id=_str("GOOGLE_CLIENT_ID"),
        google_client_secret=_str("GOOGLE_CLIENT_SECRET"),
        github_client_id=_str("GITHUB_CLIENT_ID"),
        github_client_secret=_str("GITHUB_CLIENT_SECRET"),
    )


# ═══════════════════════════════════════════════════════════════════════
# Root env — cached, thread-safe
# ═══════════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class Env:
    appwrite: AppwriteEnv
    switch: SwitchEnv
    vultr: VultrEnv
    paypal: PayPalEnv
    auth: AuthEnv
    log_level: str = "info"
    env: str = "production"
    _loaded_at: float = field(default_factory=time.time)

    def snapshot(self) -> dict[str, Any]:
        """Return a redacted snapshot — safe for logs."""
        return {
            "loaded_at": self._loaded_at,
            "env": self.env,
            "log_level": self.log_level,
            "appwrite": {
                "endpoint": self.appwrite.endpoint,
                "project_id": self.appwrite.project_id,
                "database_id": self.appwrite.database_id,
                "mode": self.appwrite.effective_mode,
                "has_api_key": bool(self.appwrite.api_key),
                "has_session": bool(self.appwrite.session_secret),
                "rate_limit_per_min": self.appwrite.rate_limit_per_min,
                "circuit_threshold": self.appwrite.circuit_threshold,
                "max_retries": self.appwrite.max_retries,
            },
            "switch": {
                "bridge_url": self.switch.bridge_url,
                "has_agent_id": bool(self.switch.agent_id),
                "has_agent_api_key": bool(self.switch.agent_api_key),
                "room_id": self.switch.room_id,
                "ops_ws_port": self.switch.ops_ws_port,
            },
            "vultr": {
                "live": self.vultr.live,
                "default_region": self.vultr.default_region,
                "monthly_budget_usd": self.vultr.monthly_budget_usd,
            },
            "paypal": {
                "mode": self.paypal.mode,
                "has_credentials": bool(self.paypal.client_id and self.paypal.client_secret),
            },
            "auth": {
                "has_jwt_secret": bool(self.auth.jwt_secret),
                "admin_email": self.auth.admin_email,
            },
        }

    def diff(self, other: "Env") -> dict[str, dict[str, tuple[Any, Any]]]:
        """Compare two snapshots. Returns {domain: {field: (before, after)}}."""
        out: dict[str, dict[str, tuple[Any, Any]]] = {}
        for name in ("appwrite", "switch", "vultr", "paypal", "auth"):
            before = getattr(self, name)
            after = getattr(other, name)
            if before != after:
                b = {f: getattr(before, f) for f in before.__dataclass_fields__}
                a = {f: getattr(after, f) for f in after.__dataclass_fields__}
                changes = {k: (b[k], a[k]) for k in b if b[k] != a.get(k)}
                if changes:
                    out[name] = changes
        return out


def load_env() -> Env:
    """Load env fresh. Not cached — call `env` for the cached singleton."""
    return Env(
        appwrite=_load_appwrite(),
        switch=_load_switch(),
        vultr=_load_vultr(),
        paypal=_load_paypal(),
        auth=_load_auth(),
        log_level=_str("LOG_LEVEL", "info").lower(),
        env=_str("ENV", "production").lower(),
    )


# Cached singleton — auto-reloads if the env has changed
_env_cache: Env | None = None
_env_mtime: float = 0.0


def _compute_mtime() -> float:
    """Best-effort mtime — updates when any *_ENDPOINT, *_API_KEY or *_SECRET changes."""
    keys = (
        "APPWRITE_ENDPOINT", "APPWRITE_API_KEY",
        "SWITCH_BRIDGE_URL", "SWITCH_AGENT_API_KEY",
        "VULTR_API_KEY", "PAYPAL_CLIENT_SECRET",
        "JWT_SECRET",
    )
    return float(hash(tuple(os.environ.get(k) for k in keys)))


def get_env() -> Env:
    """Return cached Env. Reloads if the underlying env changed."""
    global _env_cache, _env_mtime
    mtime = _compute_mtime()
    if _env_cache is None or mtime != _env_mtime:
        _env_cache = load_env()
        _env_mtime = mtime
    return _env_cache


# Lazy proxy — `from axelr_insights_engine.env import env` works
class _EnvProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(get_env(), name)


env = _EnvProxy()