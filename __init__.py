# api/__init__.py
"""
AXELR API Package
=================
Modular FastAPI backend with complete separation of concerns.

This module is the package surface. It does NOT import routers or
provider modules eagerly — that work is done by ``app.py`` at assembly
time. Only lightweight, dependency-free helpers are re-exported here.
"""

from .middleware import (
    lifespan,
    register_middleware,
    register_exception_handlers,
    register_monitor_routes,
    MONITOR_PATHS,
    validate_all_providers,
    background_health_check,
    pr_defense_cleanup,
)
from .state import (
    state,
    limiter,
    RateLimitExceeded,
    _rate_limit_exceeded_handler,
    get_object_id,
    get_redis_cache,
    set_redis_cache,
    delete_redis_cache,
    init_redis,
    init_db,
    init_qstash,
)

__version__ = "24.4"

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