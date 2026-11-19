# api/__init__.py
"""AXELR API package — public surface re-exports.

Re-exports only; no logic. The canonical lifespan lives in ``api.lifecycle``.
"""
from .lifecycle import (
    background_health_check,
    install_boot,
    lifespan,
    pr_defense_cleanup,
    validate_all_providers,
)
from .limits import (
    TIERS,
    RateLimiter,
    TierLimits,
    check_and_update_quota,
    check_rate_limit,
    estimate_tokens,
    get_rate_limiter,
    get_tier,
    rate_limit_middleware,
)
from .middleware import (
    MONITOR_PATHS,
    register_exception_handlers,
    register_middleware,
    register_monitor_routes,
)
from .state import (
    RateLimitExceeded,
    _rate_limit_exceeded_handler,
    delete_redis_cache,
    get_object_id,
    get_redis_cache,
    init_db,
    init_qstash,
    init_redis,
    limiter,
    set_redis_cache,
    state,
)

__version__ = "27.0"

__all__ = [
    "MONITOR_PATHS", "RateLimitExceeded", "RateLimiter", "TIERS",
    "TierLimits", "__version__", "_rate_limit_exceeded_handler",
    "background_health_check", "check_and_update_quota", "check_rate_limit",
    "delete_redis_cache", "estimate_tokens", "get_object_id",
    "get_rate_limiter", "get_redis_cache", "get_tier", "init_db",
    "init_qstash", "init_redis", "install_boot", "lifespan", "limiter",
    "pr_defense_cleanup", "rate_limit_middleware",
    "register_exception_handlers", "register_middleware",
    "register_monitor_routes", "set_redis_cache", "state",
    "validate_all_providers",
]
