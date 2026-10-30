"""Axelr Insights Engine — v3.2.0"""
from .core import (
    __version__,
    init,
    shutdown,
    health,
)

# Maintain backward compatibility with verification scripts expecting health_probe
health_probe = health
from .auditing import log_node_execution, list_transactions, purge_expired_audit
from .config import Settings, settings
from .routes import router as insights_router
from .search import ensure_bootstrapped, search_cached
from .storage_appwrite import AppwriteBackend
from .storage_base import (
    Backend,
    close_backend,
    get_backend,
    init_backend,
)
from .storage_mongo import MongoBackend

__all__ = [
    "__version__",
    "init", "shutdown", "health", "health_probe",
    "Settings", "settings",
    "init_backend", "get_backend", "close_backend",
    "insights_router",
    "Backend", "AppwriteBackend", "MongoBackend",
    "search_cached", "ensure_bootstrapped",
    "insert_audit", "list_audit", "purge_audit",
]