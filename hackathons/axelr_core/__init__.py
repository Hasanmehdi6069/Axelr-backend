"""AXELR Core Package"""
from .core import (
    __version__,
    get_adapter_statuses,
    health,
    init,
    shutdown,
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
