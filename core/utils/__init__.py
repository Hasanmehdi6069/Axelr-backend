# core/utils/__init__.py
"""
AXELR Core Utilities Package
============================
Collection of shared utility modules merged from standalone files.
Provides a clean, organized import surface while maintaining backward compatibility.
"""
from .errors_and_retry import (
    AxelrError, ProviderError, DeadlineExceeded, ValidationFailed,
    RateLimited, NoProviderAvailable, CircuitOpen, ConfigError,
    is_retryable, to_result,
    RetryPolicy, with_retry
)
from .security_and_streaming import (
    PIIType, sanitize, found_types,
    iter_payloads, stream_for_shape,
    ChunkedReader, read_upload_streaming,
    InFlightScrubber, scrub_stream,
    FirstTokenRace, RaceOutcome, StreamerFunc,
    CancellationToken, CancelledByClient, bind_to_request, cancellable,
    CHUNK_BYTES, MAX_INFLIGHT_CHUNKS
)
from .misc_utils import (
    # Warmup
    signal_warmup, pacemaker_loop,
    # Prewarm
    signal_prewarm, cancel_prewarm, prewarm_snapshot,
    # Error explainer
    ErrorSignature, detect_error, explain,
)
# RepoIndexer lives in core.repo_indexer — not re-exported here because
# it would create a cycle (repo_indexer imports from core.memory).
__all__ = [
    # Error classes
    "AxelrError", "ProviderError", "DeadlineExceeded", "ValidationFailed",
    "RateLimited", "NoProviderAvailable", "CircuitOpen", "ConfigError",
    # Error utilities
    "is_retryable", "to_result",
    # Retry classes
    "RetryPolicy", "with_retry",
    # PII types and functions
    "PIIType", "sanitize", "found_types",
    # SSE and streaming functions
    "iter_payloads", "stream_for_shape",
    "ChunkedReader", "read_upload_streaming",
    "InFlightScrubber", "scrub_stream",
    "FirstTokenRace", "RaceOutcome", "StreamerFunc",
    "CancellationToken", "CancelledByClient", "bind_to_request", "cancellable",
    "CHUNK_BYTES", "MAX_INFLIGHT_CHUNKS",
    # Warmup
    "signal_warmup", "pacemaker_loop",
    # Prewarm
    "signal_prewarm", "cancel_prewarm", "prewarm_snapshot",
    # Error explainer
    "ErrorSignature", "detect_error", "explain"
]