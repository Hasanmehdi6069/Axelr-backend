"""Axelr Vultr Integration — v1.1.0

Compute provisioning with blueprints, cost guards, lifecycle, orphan
detection.
"""
from .client import VultrClient, get_client
from .core import (
    MODULE_VERSION,
    Settings,
    VultrAPIError,
    VultrRateLimited,
    close_http,
    health_probe,
    init,
    settings,
    shutdown,
)
from .lifecycle import (
    InstanceState,
    cleanup_orphans,
    find_orphans,
    get_registry,
    register_instance,
    unregister_instance,
    wait_for_active,
)
from .routes import router
from .schemas import (
    BlueprintResponse,
    CleanupRequest,
    CostEstimate,
    CreateInstanceRequest,
    InstanceResponse,
    OrphanInstance,
    PlanResponse,
    RegionResponse,
)
from .templates import BLUEPRINTS, Blueprint, list_blueprints, resolve_blueprint

__version__ = MODULE_VERSION

__all__ = [
    "__version__", "MODULE_VERSION",
    "init", "shutdown", "health_probe", "close_http",
    "settings", "Settings",
    "VultrAPIError", "VultrRateLimited",
    "VultrClient", "get_client",
    "Blueprint", "BLUEPRINTS", "list_blueprints", "resolve_blueprint",
    "InstanceState", "wait_for_active",
    "register_instance", "unregister_instance",
    "find_orphans", "cleanup_orphans", "get_registry",
    "CreateInstanceRequest", "InstanceResponse", "BlueprintResponse",
    "RegionResponse", "PlanResponse", "CostEstimate", "CleanupRequest",
    "OrphanInstance",
    "router",
]