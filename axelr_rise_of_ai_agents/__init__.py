"""The Rise of AI Agents Deadlock Monitoring System — v1.1.0"""
from .core import (
    AgentInfo,
    DeadlockEvent,
    DeadlockMonitor,
    Resource,
    WaitForGraph,
    __version__,
    get_monitor,
    health,
    init,
    shutdown,
)

__all__ = [
    "__version__", "init", "shutdown", "health", "get_monitor",
    "DeadlockMonitor", "AgentInfo", "DeadlockEvent", "Resource", "WaitForGraph",
]