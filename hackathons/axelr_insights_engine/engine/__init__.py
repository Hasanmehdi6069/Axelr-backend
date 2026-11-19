"""axelr-insights-engine — Public API.

The unified, elite persistence layer for every Axelr engine.
TablesDB only. Client-side. Production-ready.

Quick start:
    from axelr_insights_engine import init, get_engine
    await init()
    db = get_engine()
    await db.insert_for_user("axelr_insights", "user_1", {...})
"""
from __future__ import annotations

from typing import Any

from .config import DEFAULTS, AppwriteConfig, resolve_config
from .engine import (
    InsightsEngine,
    __version__,
    create_axelr_module,
    create_engine,
)
from .env import (
    AppwriteEnv,
    AuthEnv,
    Env,
    PayPalEnv,
    SwitchEnv,
    VultrEnv,
    env,
    load_env,
)
from .errors import (
    APIError,
    AppwriteError,
    CircuitBreakerOpenError,
    ClientError,
    ConfigurationError,
    ConflictError,
    ForbiddenError,
    IdempotencyConflictError,
    NetworkError,
    NotFoundError,
    OfflineError,
    RateLimitError,
    SchemaError,
    TimeoutError_,
    UnauthorizedError,
    ValidationFailed,
    VersionConflictError,
    is_retryable,
    normalize_error,
    to_result,
)
from .models import (
    AttributeSpec,
    AuditEventRow,
    BaseRow,
    DocumentRow,
    HealthReport,
    IndexSpec,
    InsightRow,
    QueryFilter,
    QueryOptions,
    SchemaDefinition,
)
from .resilience import (
    CallInfo,
    CBState,
    CircuitBreaker,
    HookRegistry,
    IdempotencyStore,
    LocalCache,
    MetricsCollector,
    RequestDeduplicator,
    RetryPolicy,
    Semaphore,
    StructuredLogger,
    TokenBucket,
    stable_hash,
)
from .sdk import AppwriteSDK
from .setup import (
    ALL_SCHEMAS,
    SCHEMA_AUDIT_EVENTS,
    SCHEMA_AXELR_INSIGHTS,
    SCHEMA_INDEXED_DOCUMENTS,
    ensure_all_tables,
    ensure_table,
    verify_all_tables,
    verify_table,
)
from .tables import TablesNamespace

# ═══════════════════════════════════════════════════════════════════════
# Singleton management
# ═══════════════════════════════════════════════════════════════════════

_engine: InsightsEngine | None = None


async def init(
    config: AppwriteConfig | dict[str, Any] | None = None,
    *,
    bootstrap: bool | None = None,
    sdk_factory: Any = None,
) -> InsightsEngine:
    """Initialize the singleton engine. Idempotent — returns existing."""
    global _engine
    if _engine is not None and _engine.initialized:
        return _engine
    _engine = InsightsEngine(
        config, sdk_factory=sdk_factory,
    )
    await _engine.init(bootstrap=bootstrap)
    return _engine


def get_engine() -> InsightsEngine:
    """Return the singleton engine. Raises if `init()` was never called."""
    if _engine is None or not _engine.initialized:
        raise RuntimeError(
            "engine not initialized — call `await init(config)` first"
        )
    return _engine


async def health_check(*, deep: bool = False) -> HealthReport:
    """Health check — safe to call before init."""
    if _engine is None:
        return HealthReport(
            healthy=False,
            mode="uninitialized",
            database_id="",
            circuit_state="unknown",
            cache_size=0,
            initialized=False,
            timestamp=0.0,
            reason="not_initialized",
        )
    return await _engine.health_check(deep=deep)


async def destroy() -> None:
    """Destroy the singleton engine."""
    global _engine
    if _engine is not None:
        await _engine.destroy()
        _engine = None


async def run_self_test(table: str = "audit_events") -> dict[str, Any]:
    """Run an end-to-end self test — great for demos."""
    return await get_engine().run_self_test(table=table)


def describe() -> str:
    """Human-readable engine summary."""
    if _engine is None:
        return f"Axelr Insights Engine v{__version__} | not initialized"
    return _engine.describe()


def export_metrics() -> dict[str, Any]:
    """Full metrics snapshot — safe to call before init."""
    if _engine is None:
        return {"engine": {"version": __version__, "initialized": False}}
    return _engine.export_metrics()


# ═══════════════════════════════════════════════════════════════════════
# Convenience — module factory
# ═══════════════════════════════════════════════════════════════════════

def as_axelr_module(
    config: AppwriteConfig | dict[str, Any] | None = None,
    **options: Any,
) -> dict[str, Any]:
    """Build an Axelr module object from a config — `createHackathonModule` compatible."""
    return create_axelr_module(config, **options)


# ═══════════════════════════════════════════════════════════════════════
# Public exports
# ═══════════════════════════════════════════════════════════════════════

__all__ = [
    # Version
    "__version__",
    # Singleton lifecycle
    "init", "get_engine", "destroy", "health_check",
    "describe", "export_metrics", "run_self_test",
    # Engine
    "InsightsEngine", "create_engine", "create_axelr_module", "as_axelr_module",
    # Config
    "AppwriteConfig", "DEFAULTS", "resolve_config",
    # Namespaces
    "TablesNamespace", "AppwriteSDK",
    # Models
    "QueryFilter", "QueryOptions", "SchemaDefinition",
    "AttributeSpec", "IndexSpec",
    "BaseRow", "AuditEventRow", "InsightRow", "DocumentRow", "HealthReport",
    # Schemas
    "ALL_SCHEMAS",
    "SCHEMA_AUDIT_EVENTS",
    "SCHEMA_INDEXED_DOCUMENTS",
    "SCHEMA_AXELR_INSIGHTS",
    "ensure_table", "ensure_all_tables",
    "verify_table", "verify_all_tables",
    # Resilience
    "TokenBucket", "CircuitBreaker", "CBState",
    "Semaphore", "LocalCache", "RequestDeduplicator",
    "MetricsCollector", "StructuredLogger", "RetryPolicy",
    "IdempotencyStore", "CallInfo", "HookRegistry", "stable_hash",
    # Errors
    "AppwriteError", "APIError", "ConfigurationError",
    "ValidationFailed", "NotFoundError", "ConflictError",
    "UnauthorizedError", "ForbiddenError",
    "RateLimitError", "CircuitBreakerOpenError",
    "OfflineError", "TimeoutError_", "NetworkError", "ClientError",
    "SchemaError", "VersionConflictError", "IdempotencyConflictError",
    "is_retryable", "normalize_error", "to_result",
    # Validation helpers (v1.1.0)
    "sanitize_row_id", "is_valid_row_id", "ensure_valid_row_id", "make_row_id",
    "validate_data", "validate_table_name",
    "is_not_found", "is_conflict", "is_auth_error", "classify_error",
    # Env system
    "env", "Env", "AppwriteEnv", "SwitchEnv", "VultrEnv", "PayPalEnv",
    "AuthEnv", "load_env",
]
