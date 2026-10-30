"""AXELR Core Module - Core platform services health and lifecycle"""
import structlog
from typing import Dict, Any
from .config import initialize_all, get_adapter_statuses

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
    all_healthy = all(adapter.get("healthy", False) for adapter in adapter_statuses.values())
    initialized_count = len([a for a in adapter_statuses.values() if a.get("initialized", False)])
    total_adapters = len(adapter_statuses)
    
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
                "healthy": sum(1 for a in adapter_statuses.values() if a.get("healthy", False)),
                "statuses": adapter_statuses
            },
            "message": f"Core platform running with {initialized_count}/{total_adapters} adapters initialized"
        }
    }

async def shutdown() -> None:
    """Cleanup function - called by demo-master when demo completes"""
    global _module_shutdown
    if _module_shutdown:
        return
    
    logger.info("starting_core_shutdown")
    _module_shutdown = True
    logger.info("core_shutdown_complete")