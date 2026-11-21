"""AXELR AHE — schemas (v1.7). Single source of truth.

Consolidated v1.5 audit fields + v1.6 tier registry + v1.7 defect errors
+ safe helpers. The duplicate v1.0 block that shadowed the v1.5 classes
has been removed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from threading import Lock
from typing import Any, Dict, List, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


# ═══════════════════════════════════════════════════════════════════════════
# Coordinates
# ═══════════════════════════════════════════════════════════════════════════
class BoundingBox(BaseModel):
    model_config = ConfigDict(frozen=True, slots=True)
    page: int = Field(..., ge=1)
    x0: float
    y0: float
    x1: float
    y1: float

    @model_validator(mode="after")
    def _valid(self) -> "BoundingBox":
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError("BoundingBox x1/y1 must be >= x0/y0")
        return self


class SourceCoordinate(BaseModel):
    model_config = ConfigDict(frozen=True, slots=True)
    document_id: str
    chunk_id: str
    char_start: int = Field(..., ge=0)
    char_end: int = Field(..., ge=0)
    bbox: Optional[BoundingBox] = None
    row_index: Optional[int] = None
    col_index: Optional[int] = None
    json_path: Optional[str] = None

    @model_validator(mode="after")
    def _range(self) -> "SourceCoordinate":
        if self.char_end < self.char_start:
            raise ValueError("char_end must be >= char_start")
        return self


# ═══════════════════════════════════════════════════════════════════════════
# Document types
# ═══════════════════════════════════════════════════════════════════════════
DocumentType = Literal["pdf", "csv", "json", "text", "html", "py", "js"]
CodeDialect = Literal["python", "javascript"]


# ═══════════════════════════════════════════════════════════════════════════
# Request
# ═══════════════════════════════════════════════════════════════════════════
class ExtractionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", slots=True)

    max_document_bytes: int = Field(default=25 * 1024 * 1024, ge=1, le=200 * 1024 * 1024)
    max_chunks: int = Field(default=10_000, ge=1, le=100_000)
    max_memory_bytes: int = Field(default=512 * 1024 * 1024, ge=1, le=4 * 1024 * 1024 * 1024)
    pipeline_timeout_s: float = Field(default=120.0, gt=0, le=600.0)

    document_id: str = Field(..., min_length=1, max_length=128)
    document_ids: List[str] = Field(default_factory=list)
    document_type: DocumentType
    document_uri: str = Field(..., min_length=1)
    query: str = Field(..., min_length=1, max_length=4000)
    target_fields: List[str] = Field(..., min_length=1, max_length=64)
    value_shapes: Dict[str, str] = Field(default_factory=dict)
    code_dialect: Optional[CodeDialect] = None

    async_mode: bool = True
    callback_url: Optional[str] = None
    client_request_id: Optional[str] = None
    workspace: str = "data"
    tier: str = "free"
    enforce_quota: bool = False

    @field_validator("target_fields")
    @classmethod
    def _unique(cls, v: List[str]) -> List[str]:
        if len(set(v)) != len(v):
            raise ValueError("target_fields must be unique")
        return v

    @model_validator(mode="after")
    def _shape_fields_valid(self) -> "ExtractionRequest":
        unknown = set(self.value_shapes) - set(self.target_fields)
        if unknown:
            raise ValueError(f"value_shapes keys must be subset of target_fields; unknown: {sorted(unknown)}")
        return self


# ═══════════════════════════════════════════════════════════════════════════
# Claims & verification
# ═══════════════════════════════════════════════════════════════════════════
class ExtractedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", slots=True)
    field_name: str = Field(..., min_length=1, max_length=128)
    value: str
    raw_snippet: str = Field(..., min_length=1)
    chunk_id: str = Field(..., min_length=1)
    trivial: bool = False


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", slots=True)
    verified: bool
    reason: str
    reason_code: str = "unknown"
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    matched_text: Optional[str] = None
    evidence_count: int = 1
    doc_diversity: int = 1
    negated: bool = False


class VerifiedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", slots=True)
    field_name: str
    value: str
    raw_snippet: str
    coordinate: SourceCoordinate
    verification: VerificationResult
    provenance: List[SourceCoordinate] = Field(default_factory=list)


class VerifiedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", slots=True)
    status: Literal["verified"] = "verified"
    document_id: str
    claims: List[VerifiedClaim]
    audit_hash: str = ""
    merkle_root: str = ""
    crc32: int = 0
    conformance_score: int = 0
    engine_version: str = "axelr-ahe/1.7"
    canon_version: str = "canon/2"
    asserted_at: str = ""
    asserted_monotonic: str = ""
    finalized_at: str = ""
    request_fingerprint: str = ""
    replay_key: str = ""
    signature: str = ""
    signature_scheme: str = "hmac-sha256-workspace"
    rejection_summary: Dict[str, int] = Field(default_factory=dict)
    chunk_hashes: Dict[str, str] = Field(default_factory=dict)
    circuit_open_skips: int = 0


class UnverifiablePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", slots=True)
    status: Literal["unverifiable_data"] = "unverifiable_data"
    message: str = "Source document does not contain explicit data point"
    details: Optional[Dict[str, Any]] = None
    document_id: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════
# Error taxonomy
# ═══════════════════════════════════════════════════════════════════════════
class AxelrError(Exception):
    http_status: int = 500
    error_code: str = "internal_error"
    default_message: str = "An internal error occurred"

    def __init__(self, message: Optional[str] = None, details: Optional[Dict[str, Any]] = None):
        super().__init__(message or self.default_message)
        self.message = message or self.default_message
        self.details = details or {}

    def to_payload(self, document_id: Optional[str] = None) -> Dict[str, Any]:
        return {
            "status": self.error_code,
            "message": self.message,
            "details": self.details or None,
            "document_id": document_id,
        }


class UnverifiableDataError(AxelrError):
    http_status = 422
    error_code = "unverifiable_data"
    default_message = "Source document does not contain explicit data point"


class SchemaViolationError(AxelrError):
    http_status = 422
    error_code = "schema_violation"
    default_message = "Model output violated enforced schema"


class DocumentParseError(AxelrError):
    http_status = 400
    error_code = "document_parse_error"
    default_message = "Unable to deterministically parse source document"


class NoProviderAvailableError(AxelrError):
    http_status = 503
    error_code = "no_provider_available"
    default_message = "No upstream model is currently available for verification"


class JobNotFoundError(AxelrError):
    http_status = 404
    error_code = "job_not_found"
    default_message = "Job not found or expired"


class QuotaExceededError(AxelrError):
    http_status = 429
    error_code = "quota_exceeded"
    default_message = "Monthly quota exceeded"


class ResearchDefectError(AxelrError):
    http_status = 422
    error_code = "research_defect"
    default_message = "Research verification defects"


class DesignDefectError(AxelrError):
    http_status = 422
    error_code = "design_defect"
    default_message = "Design verification defects"


# ═══════════════════════════════════════════════════════════════════════════
# Confidence & tier registry (v1.6)
# ═══════════════════════════════════════════════════════════════════════════
class Confidence(str, Enum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class VerifyTier(str, Enum):
    FREE = "free"
    GUEST = "guest"
    PRO = "pro"
    BUSINESS = "business"
    ENTERPRISE = "enterprise"


@dataclass(frozen=True)
class TierSpec:
    name: str
    max_chunk_chars: int
    max_claims: int
    retries: int
    allow_llm: bool
    max_files_per_prompt: int
    monthly_queries: int
    monthly_mb: int
    confidence_floor: Confidence = Confidence.HIGH


class TierRegistry:
    """Machine-readable tier inventory (manifesto promise)."""

    def __init__(self) -> None:
        self._tiers: Dict[str, TierSpec] = {}
        self._lock = Lock()

    def register(self, spec: TierSpec) -> None:
        with self._lock:
            self._tiers[spec.name.lower()] = spec

    def get(self, name: str) -> Optional[TierSpec]:
        with self._lock:
            return self._tiers.get(name.lower())

    def inventory(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {
                    "name": t.name,
                    "max_claims": t.max_claims,
                    "max_chunk_chars": t.max_chunk_chars,
                    "retries": t.retries,
                    "allow_llm": t.allow_llm,
                    "monthly_queries": t.monthly_queries,
                    "monthly_mb": t.monthly_mb,
                    "confidence_floor": t.confidence_floor.value,
                }
                for t in self._tiers.values()
            ]

    def __contains__(self, name: str) -> bool:
        return name.lower() in self._tiers


REGISTRY = TierRegistry()


# ═══════════════════════════════════════════════════════════════════════════
# Audit hash + helpers
# ═══════════════════════════════════════════════════════════════════════════
def compute_audit_hash(payload: VerifiedPayload) -> str:
    h = hashlib.sha256()
    h.update(payload.engine_version.encode("utf-8"))
    h.update(b"\x1f")
    h.update(payload.canon_version.encode("utf-8"))
    h.update(b"\x1f")
    h.update(payload.document_id.encode("utf-8"))
    h.update(b"\x1f")
    for c in sorted(
        payload.claims, key=lambda x: (x.field_name, x.coordinate.char_start, x.coordinate.char_end, x.value)
    ):
        h.update(c.field_name.encode("utf-8"))
        h.update(b"\x00")
        h.update(c.value.encode("utf-8"))
        h.update(b"\x00")
        h.update(c.coordinate.chunk_id.encode("utf-8"))
        h.update(b"\x00")
        h.update(str(c.coordinate.char_start).encode("ascii"))
        h.update(b"-")
        h.update(str(c.coordinate.char_end).encode("ascii"))
        h.update(b"\x1e")
    return h.hexdigest()


def verify_audit_hash(payload: VerifiedPayload) -> bool:
    """Verify the payload's audit_hash matches the recomputed hash."""
    if not payload.audit_hash:
        return False
    return compute_audit_hash(payload) == payload.audit_hash


def safe_sha256_hex(data: bytes) -> str:
    """Deterministic SHA-256 hex."""
    return hashlib.sha256(data).hexdigest()


def tier_limits(tier: str) -> Dict[str, Any]:
    """Lookup limits for a tier. Raises SchemaViolationError if unknown."""
    spec = REGISTRY.get(tier)
    if spec is None:
        raise SchemaViolationError(
            f"Unknown tier {tier!r}",
            {"available": [t["name"] for t in REGISTRY.inventory()]},
        )
    return {
        "max_chunk_chars": spec.max_chunk_chars,
        "max_claims": spec.max_claims,
        "retries": spec.retries,
        "allow_llm": spec.allow_llm,
        "monthly_queries": spec.monthly_queries,
        "monthly_mb": spec.monthly_mb,
        "confidence_floor": spec.confidence_floor.value,
    }


def payload_summary(payload: VerifiedPayload) -> Dict[str, Any]:
    """Compact machine-readable summary of a verified payload."""
    return {
        "document_id": payload.document_id,
        "engine_version": payload.engine_version,
        "claim_count": len(payload.claims),
        "conformance_score": payload.conformance_score,
        "merkle_root": payload.merkle_root[:16],
        "audit_hash": payload.audit_hash[:16],
        "rejection_summary": dict(payload.rejection_summary),
        "fields": sorted({c.field_name for c in payload.claims}),
    }


__all__ = [
    "BoundingBox",
    "SourceCoordinate",
    "DocumentType",
    "CodeDialect",
    "ExtractionRequest",
    "ExtractedClaim",
    "VerificationResult",
    "VerifiedClaim",
    "VerifiedPayload",
    "UnverifiablePayload",
    "AxelrError",
    "UnverifiableDataError",
    "SchemaViolationError",
    "DocumentParseError",
    "NoProviderAvailableError",
    "JobNotFoundError",
    "QuotaExceededError",
    "ResearchDefectError",
    "DesignDefectError",
    "Confidence",
    "VerifyTier",
    "TierSpec",
    "TierRegistry",
    "REGISTRY",
    "compute_audit_hash",
    "verify_audit_hash",
    "safe_sha256_hex",
    "tier_limits",
    "payload_summary",
]
