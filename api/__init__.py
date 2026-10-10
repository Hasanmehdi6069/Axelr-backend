# api/__init__.py
"""
AXELR API Package
=================
Surface only. Assembly work is done by `api.warhead` (app factory).

`_boot` auto-installs on import; that is the ONLY importer-side effect.
"""
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

# Single consolidated boot layer. Must be the LAST import — installs patches.
from . import _boot  # noqa: F401,E402

__version__ = "25.0"

__all__ = [
    "__version__",
    "lifespan",
    "register_middleware",
    "register_exception_handlers",
    "register_monitor_routes",
    "MONITOR_PATHS",
    "validate_all_providers",
    "background_health_check",
    "pr_defense_cleanup",
    "state",
    "limiter",
    "RateLimitExceeded",
    "_rate_limit_exceeded_handler",
    "get_object_id",
    "get_redis_cache",
    "set_redis_cache",
    "delete_redis_cache",
    "init_redis",
    "init_db",
    "init_qstash",
]