# core/anti_hallucination/__init__.py
"""AXELR Anti-Hallucination Engine — public surface."""
from .agents import AdjudicatorAgent, ExtractorAgent, SkepticAgent
from .bounding import Chunk, DocumentBuffer, DocumentIngestor, canonicalize
from .engine import AntiHallucinationEngine
from .schemas import (
    AxelrError,
    BoundingBox,
    DocumentParseError,
    DocumentType,
    ExtractedClaim,
    ExtractionRequest,
    JobNotFoundError,
    NoProviderAvailableError,
    SchemaViolationError,
    SourceCoordinate,
    UnverifiableDataError,
    UnverifiablePayload,
    VerificationResult,
    VerifiedClaim,
    VerifiedPayload,
    compute_audit_hash,
)

__all__ = [
    "AntiHallucinationEngine", "DocumentIngestor", "DocumentBuffer", "Chunk",
    "canonicalize", "ExtractorAgent", "SkepticAgent", "AdjudicatorAgent",
    "AxelrError", "UnverifiableDataError", "SchemaViolationError",
    "DocumentParseError", "NoProviderAvailableError", "JobNotFoundError",
    "BoundingBox", "SourceCoordinate", "DocumentType", "ExtractionRequest",
    "ExtractedClaim", "VerificationResult", "VerifiedClaim",
    "VerifiedPayload", "UnverifiablePayload", "compute_audit_hash",
]
