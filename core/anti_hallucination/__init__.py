"""AXELR AHE — public surface (v1.6)."""
from .agents import (
    AdjudicatorAgent, ExtractorAgent, SkepticAgent, EXTRACTOR_SYSTEM_PROMPT,
)
from .deterministic import DeterministicExtractor
from .bounding import (
    Chunk, DocumentBuffer, DocumentIngestor,
    canonicalize, detect_adversarial_unicode,
)
from .engine import (
    AntiHallucinationEngine, TIER_BUDGET, TierBudget,
    dispatch, healthz, readyz, livez, run_batch,
)
from .primitives import (
    REASON_OK, REASON_CHUNK_NOT_FOUND, REASON_EMPTY_SNIPPET,
    REASON_SHORT_SNIPPET, REASON_NOT_SUBSTRING, REASON_VALUE_NOT_DERIVABLE,
    REASON_STRICT_THRESHOLD, REASON_WEAK_VALUE, REASON_QUERY_ECHO,
    REASON_QUERY_SIMILAR, REASON_NEGATED, REASON_DETERMINISTIC,
    REASON_VERBATIM, REASON_SHAPE_MISMATCH,
    request_fingerprint, replay_key, chunk_hash, sign_root, project_budget,
    stable_tie_break, verify_invariants,
    CircuitBreaker, CrossRequestCache, QuotaTracker, TokenCaps,
    intern_str, freeze_gc_after_warmup,
)
from .verifiers import (
    ResearchSource, ResearchClaim, ResearchDefect, ResearchVerdict,
    extract_citations, is_vague, is_specific, verify_research,
    DesignDefect, DesignVerdict, verify_design,
)
from .schemas import (
    AxelrError, BoundingBox, CodeDialect, Confidence, DesignDefectError,
    DocumentParseError, DocumentType, ExtractedClaim, ExtractionRequest,
    JobNotFoundError, NoProviderAvailableError, QuotaExceededError,
    ResearchDefectError, SchemaViolationError, SourceCoordinate,
    TierRegistry, TierSpec, UnverifiableDataError, UnverifiablePayload,
    VerificationResult, VerifiedClaim, VerifiedPayload, VerifyTier,
    REGISTRY, compute_audit_hash, verify_audit_hash, tier_limits, payload_summary,
)
from .elite_features import (
    CausalChainVerifier, NumericalPrecisionGuard, EntityResolver,
    QuotationAttributionGuard, TemporalOrderingVerifier, UnitConsistencyChecker,
    StatisticalClaimVerifier, ContradictionDetector, ProvenanceChainVerifier,
    SensitivityClassifier, CrossLingualCanonicalizer, HallucinationPatternDetector,
    ClaimGranularityOptimizer, ConfidencePropagationEngine, AdversarialInputDetector,
    SemanticFingerprint, ClaimGraphBuilder, HedgingDetector, EliteFeaturesV2,
)

__all__ = [
    "ExtractorAgent", "SkepticAgent", "AdjudicatorAgent",
    "DeterministicExtractor", "EXTRACTOR_SYSTEM_PROMPT",
    "DocumentIngestor", "DocumentBuffer", "Chunk",
    "canonicalize", "detect_adversarial_unicode",
    "AntiHallucinationEngine", "TierBudget", "TIER_BUDGET",
    "dispatch", "healthz", "readyz", "livez", "run_batch",
    "REASON_OK", "REASON_CHUNK_NOT_FOUND", "REASON_EMPTY_SNIPPET",
    "REASON_SHORT_SNIPPET", "REASON_NOT_SUBSTRING",
    "REASON_VALUE_NOT_DERIVABLE", "REASON_STRICT_THRESHOLD",
    "REASON_WEAK_VALUE", "REASON_QUERY_ECHO", "REASON_QUERY_SIMILAR",
    "REASON_NEGATED", "REASON_DETERMINISTIC", "REASON_VERBATIM",
    "REASON_SHAPE_MISMATCH",
    "request_fingerprint", "replay_key", "chunk_hash", "sign_root",
    "project_budget", "stable_tie_break", "verify_invariants",
    "CircuitBreaker", "CrossRequestCache", "QuotaTracker", "TokenCaps",
    "intern_str", "freeze_gc_after_warmup",
    "ResearchSource", "ResearchClaim", "ResearchDefect", "ResearchVerdict",
    "extract_citations", "is_vague", "is_specific", "verify_research",
    "DesignDefect", "DesignVerdict", "verify_design",
    "AxelrError", "UnverifiableDataError", "SchemaViolationError",
    "DocumentParseError", "NoProviderAvailableError", "JobNotFoundError",
    "QuotaExceededError", "ResearchDefectError", "DesignDefectError",
    "BoundingBox", "SourceCoordinate", "DocumentType", "CodeDialect",
    "VerifyTier", "Confidence",
    "ExtractionRequest", "ExtractedClaim", "VerificationResult",
    "VerifiedClaim", "VerifiedPayload", "UnverifiablePayload",
    "TierSpec", "TierRegistry", "REGISTRY", "compute_audit_hash", "verify_audit_hash", "tier_limits", "payload_summary",
    "CausalChainVerifier", "NumericalPrecisionGuard", "EntityResolver",
    "QuotationAttributionGuard", "TemporalOrderingVerifier", "UnitConsistencyChecker",
    "StatisticalClaimVerifier", "ContradictionDetector", "ProvenanceChainVerifier",
    "SensitivityClassifier", "CrossLingualCanonicalizer", "HallucinationPatternDetector",
    "ClaimGranularityOptimizer", "ConfidencePropagationEngine", "AdversarialInputDetector",
    "SemanticFingerprint", "ClaimGraphBuilder", "HedgingDetector", "EliteFeaturesV2",
]