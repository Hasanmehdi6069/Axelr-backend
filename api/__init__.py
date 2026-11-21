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
    "MONITOR_PATHS",
    "RateLimitExceeded",
    "RateLimiter",
    "TIERS",
    "TierLimits",
    "__version__",
    "_rate_limit_exceeded_handler",
    "background_health_check",
    "check_and_update_quota",
    "check_rate_limit",
    "delete_redis_cache",
    "estimate_tokens",
    "get_object_id",
    "get_rate_limiter",
    "get_redis_cache",
    "get_tier",
    "init_db",
    "init_qstash",
    "init_redis",
    "install_boot",
    "lifespan",
    "limiter",
    "pr_defense_cleanup",
    "rate_limit_middleware",
    "register_exception_handlers",
    "register_middleware",
    "register_monitor_routes",
    "set_redis_cache",
    "state",
    "validate_all_providers",
]


import sys
from types import ModuleType

from .workspace_engine import (
    ENGINE_VERSION,
    compute_full_detection,
    detect_workspace,
    detect_workspace_full,
    extract_features,
    is_neutral_file,
    score_file,
    score_text,
)

_workspace_detector_module = ModuleType("api.workspace_detector")
_workspace_detector_module.__all__ = [
    "ENGINE_VERSION",
    "compute_full_detection",
    "detect_workspace",
    "detect_workspace_full",
    "extract_features",
    "is_neutral_file",
    "score_file",
    "score_text",
]
_workspace_detector_module.ENGINE_VERSION = ENGINE_VERSION
_workspace_detector_module.compute_full_detection = compute_full_detection
_workspace_detector_module.detect_workspace = detect_workspace
_workspace_detector_module.detect_workspace_full = detect_workspace_full
_workspace_detector_module.extract_features = extract_features
_workspace_detector_module.is_neutral_file = is_neutral_file
_workspace_detector_module.score_file = score_file
_workspace_detector_module.score_text = score_text
sys.modules["api.workspace_detector"] = _workspace_detector_module