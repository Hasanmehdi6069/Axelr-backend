"""AXELR Core Package"""
from .core import (
    __version__,
    init,
    shutdown,
    health,
    get_adapter_statuses,
)

# Maintain backward compatibility with verification scripts expecting health_probe
health_probe = health

__all__ = [
    "__version__",
    "init",
    "shutdown",
    "health",
    "health_probe",
    "get_adapter_statuses",
]