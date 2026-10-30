"""AXELR Anti-Hallucination Engine — public surface (v1.5)."""
from .agents import AdjudicatorAgent, ExtractorAgent, SkepticAgent
from .bounding import (
    Chunk, DocumentBuffer, DocumentIngestor,
    canonicalize, detect_adversarial_unicode,
)
from .deterministic import DeterministicExtractor
from .engine import TIER_BUDGET, AntiHallucinationEngine, TierBudget
from ._runtime import (
    CircuitBreaker, CrossRequestCache,
    freeze_gc_after_warmup, intern_str,
)
from ._verify import (
    request_fingerprint, replay_key, chunk_hash, sign_root, project_budget,
    stable_tie_break, verify_invariants,
    REASON_OK, REASON_CHUNK_NOT_FOUND, REASON_EMPTY_SNIPPET,
    REASON_SHORT_SNIPPET, REASON_NOT_SUBSTRING, REASON_VALUE_NOT_DERIVABLE,
    REASON_STRICT_THRESHOLD, REASON_WEAK_VALUE, REASON_QUERY_ECHO,
    REASON_QUERY_SIMILAR, REASON_NEGATED, REASON_DETERMINISTIC,
    REASON_VERBATIM, REASON_SHAPE_MISMATCH,
)
from .schemas import (
    AxelrError, BoundingBox, DocumentParseError, DocumentType, CodeDialect,
    ExtractedClaim, ExtractionRequest, JobNotFoundError,
    NoProviderAvailableError, QuotaExceededError, SchemaViolationError,
    SourceCoordinate, UnverifiableDataError, UnverifiablePayload,
    VerificationResult, VerifiedClaim, VerifiedPayload, compute_audit_hash,
)

__all__ = [
    "AntiHallucinationEngine", "TierBudget", "TIER_BUDGET",
    "DocumentIngestor", "DocumentBuffer", "Chunk",
    "canonicalize", "detect_adversarial_unicode",
    "DeterministicExtractor",
    "ExtractorAgent", "SkepticAgent", "AdjudicatorAgent",
    "freeze_gc_after_warmup", "intern_str",
    "CircuitBreaker", "CrossRequestCache",
    "request_fingerprint", "replay_key", "chunk_hash", "sign_root",
    "project_budget", "stable_tie_break", "verify_invariants",
    "REASON_OK", "REASON_CHUNK_NOT_FOUND", "REASON_EMPTY_SNIPPET",
    "REASON_SHORT_SNIPPET", "REASON_NOT_SUBSTRING",
    "REASON_VALUE_NOT_DERIVABLE", "REASON_STRICT_THRESHOLD",
    "REASON_WEAK_VALUE", "REASON_QUERY_ECHO", "REASON_QUERY_SIMILAR",
    "REASON_NEGATED", "REASON_DETERMINISTIC", "REASON_VERBATIM",
    "REASON_SHAPE_MISMATCH",
    "AxelrError", "UnverifiableDataError", "SchemaViolationError",
    "DocumentParseError", "NoProviderAvailableError", "JobNotFoundError",
    "QuotaExceededError",
    "BoundingBox", "SourceCoordinate", "DocumentType", "CodeDialect",
    "ExtractionRequest", "ExtractedClaim", "VerificationResult",
    "VerifiedClaim", "VerifiedPayload", "UnverifiablePayload",
    "compute_audit_hash",
]