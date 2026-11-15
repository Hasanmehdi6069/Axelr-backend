"""Bharat Agentic AI Resilient Persistence — v2.0.0

The ``core`` compatibility layer from v1.1 was never shipped. This module
exports only symbols that actually exist in the package. If a downstream
consumer depended on ``ResilientPersistenceLayer``/``WALEntry``, ship
``bharat_agentic.core`` alongside this package.
"""
from __future__ import annotations

__version__ = "2.0.0"

from .config import EngineConfig, configure_logging
from .engine import (
    BackendHealth,
    BackendSync,
    CivicAgent,
    get_engine,
    init_engine,
    run_metrics,
    shutdown_engine,
)
from .errors import (
    ApprovalConflictError,
    BharatAgentError,
    ConfigError,
    IdempotencyConflictError,
    InputTooLargeError,
    RateLimitExceededError,
    SyncExhaustedError,
)
from .models import (
    ApprovalState,
    ApprovalTicket,
    AuditEvent,
    CaseRecord,
    CaseStatus,
    CaseSummary,
    Confidence,
    ExtractedField,
    MetricsReport,
    ReminderChannel,
    ReminderReceipt,
    ReminderRequest,
)

__all__ = [
    "__version__",
    # config
    "EngineConfig", "configure_logging",
    # engine
    "CivicAgent", "MetricsReport", "init_engine", "get_engine",
    "shutdown_engine", "run_metrics", "BackendSync", "BackendHealth",
    # errors
    "BharatAgentError", "ConfigError", "ApprovalConflictError",
    "RateLimitExceededError", "InputTooLargeError",
    "SyncExhaustedError", "IdempotencyConflictError",
    # models
    "ApprovalState", "ApprovalTicket", "AuditEvent",
    "CaseRecord", "CaseStatus", "CaseSummary", "Confidence",
    "ExtractedField", "ReminderChannel", "ReminderReceipt",
    "ReminderRequest",
]