<<<<<<< HEAD
"""AXELR AHE — schemas (v1.5). Single source of truth."""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Literal, Optional

from pydantic import (
    BaseModel, ConfigDict, Field, field_validator, model_validator,
)


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


DocumentType = Literal["pdf", "csv", "json", "text", "html", "py", "js"]
CodeDialect = Literal["python", "javascript"]


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
            raise ValueError(
                f"value_shapes keys must be subset of target_fields; "
                f"unknown: {sorted(unknown)}"
            )
        return self


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
    engine_version: str = "axelr-ahe/1.5"
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


class AxelrError(Exception):
    http_status: int = 500
    error_code: str = "internal_error"
    default_message: str = "An internal error occurred"

    def __init__(self, message: Optional[str] = None,
                 details: Optional[Dict[str, Any]] = None):
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


def compute_audit_hash(payload: VerifiedPayload) -> str:
    h = hashlib.sha256()
    h.update(payload.engine_version.encode("utf-8")); h.update(b"\x1f")
    h.update(payload.canon_version.encode("utf-8"));   h.update(b"\x1f")
    h.update(payload.document_id.encode("utf-8"));     h.update(b"\x1f")
    for c in sorted(payload.claims,
                    key=lambda x: (x.field_name, x.coordinate.char_start,
                                   x.coordinate.char_end, x.value)):
        h.update(c.field_name.encode("utf-8")); h.update(b"\x00")
        h.update(c.value.encode("utf-8"));      h.update(b"\x00")
        h.update(c.coordinate.chunk_id.encode("utf-8")); h.update(b"\x00")
        h.update(str(c.coordinate.char_start).encode("ascii")); h.update(b"-")
        h.update(str(c.coordinate.char_end).encode("ascii"));   h.update(b"\x1e")
    return h.hexdigest()


__all__ = [
    "BoundingBox", "SourceCoordinate", "DocumentType", "CodeDialect",
    "ExtractionRequest", "ExtractedClaim", "VerificationResult",
    "VerifiedClaim", "VerifiedPayload", "UnverifiablePayload",
    "AxelrError", "UnverifiableDataError", "SchemaViolationError",
    "DocumentParseError", "NoProviderAvailableError", "JobNotFoundError",
    "QuotaExceededError", "compute_audit_hash",
]
=======
"""
AXELR Anti-Hallucination Engine — schemas.
===========================================
Single source of truth for:
  * Structural coordinate models (BoundingBox, SourceCoordinate)
  * Request/Claim/Verification models (Pydantic V2, extra="forbid")
  * The standard error taxonomy (AxelrError and subclasses)
  * compute_audit_hash() — reproducible SHA-256 over the verified payload
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


# ── Structural coordinates ────────────────────────────────────────────────
class BoundingBox(BaseModel):
    model_config = ConfigDict(frozen=True)
    page: int = Field(..., ge=1)     # 1-indexed: matches viewers, Drive #page=N, and the Sheets addon
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
    model_config = ConfigDict(frozen=True)
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


# ── Request / response models ─────────────────────────────────────────────
DocumentType = Literal["pdf", "csv", "json", "text"]


class ExtractionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(..., min_length=1, max_length=128)
    document_type: DocumentType
    document_uri: str = Field(..., min_length=1)
    query: str = Field(..., min_length=1, max_length=4000)
    target_fields: List[str] = Field(..., min_length=1, max_length=64)
    async_mode: bool = True
    callback_url: Optional[str] = None
    client_request_id: Optional[str] = None
    workspace: str = "data"     # ← used for provider ranking
    tier: str = "free"

    @field_validator("target_fields")
    @classmethod
    def _unique(cls, v: List[str]) -> List[str]:
        if len(set(v)) != len(v):
            raise ValueError("target_fields must be unique")
        return v


class ExtractedClaim(BaseModel):
    """Raw proposer output. `extra=forbid` — the LLM may ONLY set these keys."""
    model_config = ConfigDict(extra="forbid")
    field_name: str = Field(..., min_length=1, max_length=128)
    value: str
    raw_snippet: str = Field(..., min_length=1)
    chunk_id: str = Field(..., min_length=1)


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verified: bool
    reason: str
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    matched_text: Optional[str] = None


class VerifiedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_name: str
    value: str
    raw_snippet: str
    coordinate: SourceCoordinate
    verification: VerificationResult


class VerifiedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["verified"] = "verified"
    document_id: str
    claims: List[VerifiedClaim]
    audit_hash: str = ""
    engine_version: str = "axelr-ahe/1.0"


class UnverifiablePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["unverifiable_data"] = "unverifiable_data"
    message: str = "Source document does not contain explicit data point"
    details: Optional[Dict[str, Any]] = None
    document_id: Optional[str] = None


# ── Error taxonomy ────────────────────────────────────────────────────────
class AxelrError(Exception):
    http_status: int = 500
    error_code: str = "internal_error"
    default_message: str = "An internal error occurred"

    def __init__(self, message: Optional[str] = None,
                 details: Optional[Dict[str, Any]] = None):
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


# ── Audit hash ────────────────────────────────────────────────────────────
def compute_audit_hash(payload: VerifiedPayload) -> str:
    h = hashlib.sha256()
    h.update(payload.document_id.encode("utf-8"))
    h.update(b"\x1f")
    for c in sorted(payload.claims,
                    key=lambda x: (x.field_name, x.coordinate.char_start)):
        h.update(c.field_name.encode("utf-8")); h.update(b"\x00")
        h.update(c.value.encode("utf-8")); h.update(b"\x00")
        h.update(c.coordinate.chunk_id.encode("utf-8")); h.update(b"\x00")
        h.update(str(c.coordinate.char_start).encode("ascii")); h.update(b"-")
        h.update(str(c.coordinate.char_end).encode("ascii")); h.update(b"\x1e")
    return h.hexdigest()


__all__ = [
    "BoundingBox", "SourceCoordinate", "DocumentType", "ExtractionRequest",
    "ExtractedClaim", "VerificationResult", "VerifiedClaim",
    "VerifiedPayload", "UnverifiablePayload",
    "AxelrError", "UnverifiableDataError", "SchemaViolationError",
    "DocumentParseError", "NoProviderAvailableError", "JobNotFoundError",
    "compute_audit_hash",
]
>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
