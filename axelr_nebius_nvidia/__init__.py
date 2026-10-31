"""Nebius × NVIDIA Inference Bridge — v1.1.0"""
from .core import (
    ConnectionPool,
    InferenceConnection,
    NebiusSettings,
    RegionCircuitBreaker,
    TokenBucketRateLimiter,
    __version__,
    get_connection_pool,
    get_rate_limiter,
    get_settings,
    health,
    init,
    is_configured,
    optimize_nvidia_model,
    shutdown,
    stream_inference,
)
from .routes import (
    handle_get_metrics,
    handle_health_check,
    handle_optimize_model,
    handle_stream_inference,
    on_shutdown,
    on_startup,
    register_routes,
)

# Maintain backward compatibility for existing code that imports handle_health
handle_health = handle_health_check

__all__ = [
    "__version__", "init", "shutdown", "health", "is_configured",
    "get_settings", "get_rate_limiter", "get_connection_pool",
    "NebiusSettings", "ConnectionPool", "InferenceConnection",
    "RegionCircuitBreaker", "TokenBucketRateLimiter",
    "stream_inference", "optimize_nvidia_model",
    "register_routes", "on_startup", "on_shutdown",
    "handle_health", "handle_health_check", "handle_stream_inference",
    "handle_optimize_model", "handle_get_metrics",
]