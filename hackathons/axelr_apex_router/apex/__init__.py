"""Apex Router — hybrid inference across on-device, local, and cloud tiers."""
from .router import ApexRouter, RouteResult, classify_task
from .tiers import (
    BaseTier, FirebaseTier, LocalTier, NebiusTier, TierResult,
    parse_strict_json,
)
from .telemetry import aggregate, log_route, read_all, reset as reset_telemetry

__version__ = "1.0.0"
__all__ = [
    "__version__",
    "ApexRouter", "RouteResult", "classify_task",
    "BaseTier", "FirebaseTier", "LocalTier", "NebiusTier", "TierResult",
    "parse_strict_json",
    "aggregate", "log_route", "read_all", "reset_telemetry",
]