"""AXELR Core Module - Core platform services health and lifecycle"""

from typing import Any, Dict

import structlog

from .config import get_adapter_statuses, initialize_all

logger = structlog.get_logger("axelr.core")
__version__ = "1.0.0"

# Module state tracking
_module_initialized = False
_module_shutdown = False


async def init() -> None:
    """Initialize the core AXELR platform - called by demo-master at startup"""
    global _module_initialized
    if _module_initialized:
        logger.info("core_already_initialized")
        return

    logger.info("starting_core_initialization")
    await initialize_all()
    _module_initialized = True
    logger.info("core_initialization_complete")


def health() -> Dict[str, Any]:
    """Health check function - called by demo-master to verify module health"""
    adapter_statuses = get_adapter_statuses()
    total_adapters = len(adapter_statuses)
    # FIX: empty registry → "not_configured", NOT "healthy"
    if total_adapters == 0:
        return {
            "ok": True,
            "details": {
                "module": "axelr",
                "status": "not_configured",
                "version": __version__,
                "initialized": _module_initialized,
                "adapters": {"total": 0, "initialized": 0, "healthy": 0, "statuses": {}},
                "message": "No adapters configured (set ENV vars for Appwrite / PayPal / Vultr)",
            },
        }
    all_healthy = all(a.get("healthy", False) for a in adapter_statuses.values())
    initialized_count = sum(1 for a in adapter_statuses.values() if a.get("initialized", False))
    healthy_count = sum(1 for a in adapter_statuses.values() if a.get("healthy", False))
    return {
        "ok": True,
        "details": {
            "module": "axelr",
            "status": "healthy" if all_healthy else "degraded",
            "version": __version__,
            "initialized": _module_initialized,
            "adapters": {
                "total": total_adapters,
                "initialized": initialized_count,
                "healthy": healthy_count,
                "statuses": adapter_statuses,
            },
            "message": f"Core platform running with {initialized_count}/{total_adapters} adapters initialized",
        },
    }


async def shutdown() -> None:
    """Cleanup function - called by demo-master when demo completes"""
    global _module_shutdown
    if _module_shutdown:
        return

    logger.info("starting_core_shutdown")
    _module_shutdown = True
    logger.info("core_shutdown_complete")
