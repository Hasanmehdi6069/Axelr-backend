# api/__init__.py
"""
AXELR API Package
=================
Surface only. Assembly work is done by `api.warhead` (app factory).

`_boot` auto-installs on import; that is the ONLY importer-side effect.
"""
# Single consolidated boot layer. Must be the LAST import — installs patches.
from . import _boot  # noqa: F401
from .lifespan import (
    background_health_check,
    lifespan,
    pr_defense_cleanup,
    validate_all_providers,
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

__version__ = "25.0"

__all__ = [
    "MONITOR_PATHS",
    "RateLimitExceeded",
    "__version__",
    "_rate_limit_exceeded_handler",
    "background_health_check",
    "delete_redis_cache",
    "get_object_id",
    "get_redis_cache",
    "init_db",
    "init_qstash",
    "init_redis",
    "lifespan",
    "limiter",
    "pr_defense_cleanup",
    "register_exception_handlers",
    "register_middleware",
    "register_monitor_routes",
    "set_redis_cache",
    "state",
    "validate_all_providers",
]