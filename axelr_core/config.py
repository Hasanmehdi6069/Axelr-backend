"""AXELR Unified Adapter Configuration & Initialization
Centralizes initialization of all external adapters and providers to ensure
they are reported correctly by the /api/health/ready endpoint.
"""
import structlog
from typing import Any, Dict

logger = structlog.get_logger("axelr.config")

# Track initialized adapters
_ADAPTER_STATUSES: Dict[str, Dict[str, Any]] = {}

def get_adapter_statuses() -> Dict[str, Dict[str, Any]]:
    """Return current status of all initialized adapters."""
    return _ADAPTER_STATUSES.copy()

async def initialize_all() -> None:
    """Initialize all registered adapters and providers.
    This function is called during application startup to ensure all
    external integrations are initialized and their health status is
    tracked for the /api/health/ready endpoint.
    """
    logger.info("starting_adapter_initialization")
    _ADAPTER_STATUSES.clear()

    # Initialize Appwrite adapter (insights engine)
    await _init_appwrite_adapter()
    
    # Initialize PayPal adapter
    await _init_paypal_adapter()
    
    # Initialize Vultr/Nebius adapter
    await _init_vultr_adapter()

    logger.info("all_adapters_initialized", adapters=list(_ADAPTER_STATUSES.keys()))


async def _init_appwrite_adapter() -> None:
    """Initialize Appwrite storage adapter from insights engine."""
    try:
        from insights_engine.appwrite_adapter import init as appwrite_init
        await appwrite_init()
        _ADAPTER_STATUSES["appwrite"] = {
            "initialized": True,
            "healthy": True,
            "type": "storage"
        }
        logger.info("appwrite_adapter_initialized")
    except Exception as e:
        _ADAPTER_STATUSES["appwrite"] = {
            "initialized": False,
            "healthy": False,
            "error": str(e),
            "type": "storage"
        }
        logger.error("appwrite_adapter_init_failed", error=str(e))


async def _init_paypal_adapter() -> None:
    """Initialize PayPal payment adapter."""
    try:
        from api.paypal_adapter import init as paypal_init
        await paypal_init()
        _ADAPTER_STATUSES["paypal"] = {
            "initialized": True,
            "healthy": True,
            "type": "payment"
        }
        logger.info("paypal_adapter_initialized")
    except Exception as e:
        _ADAPTER_STATUSES["paypal"] = {
            "initialized": False,
            "healthy": False,
            "error": str(e),
            "type": "payment"
        }
        logger.warning("paypal_adapter_init_failed", error=str(e))


async def _init_vultr_adapter() -> None:
    """Initialize Vultr/Nebius cloud adapter."""
    try:
        from api.vultr_adapter import init as vultr_init
        await vultr_init()
        _ADAPTER_STATUSES["vultr"] = {
            "initialized": True,
            "healthy": True,
            "type": "cloud"
        }
        logger.info("vultr_adapter_initialized")
    except Exception as e:
        _ADAPTER_STATUSES["vultr"] = {
            "initialized": False,
            "healthy": False,
            "error": str(e),
            "type": "cloud"
        }
        logger.warning("vultr_adapter_init_failed", error=str(e))