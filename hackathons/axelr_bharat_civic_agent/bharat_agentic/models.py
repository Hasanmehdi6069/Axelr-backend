"""Shared data models for the Bharat Civic Agent.

Every container is ``dataclass(slots=True)`` with explicit
``to_dict``/``from_dict`` so instances round-trip cleanly through SQLite
JSON blobs. Nested extraction results are :class:`ExtractedField` (never raw
dicts) so confidence and source-spans survive.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class CaseStatus(str, Enum):
    INGESTED = "ingested"
    EXTRACTED = "extracted"
    SUMMARIZED = "summarized"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReminderChannel(str, Enum):
    SMS = "sms"
    WHATSAPP = "whatsapp"


class ApprovalState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Extraction result per field
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ExtractedField:
    value: Optional[str] = None
    confidence: float = 0.0
    source_span: str = ""
    notes: str = ""

    @property
    def level(self) -> Confidence:
        if self.confidence >= 0.80:
            return Confidence.HIGH
        if self.confidence >= 0.50:
            return Confidence.MEDIUM
        return Confidence.LOW

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ExtractedField":
        return cls(
            value=d.get("value"),
            confidence=float(d.get("confidence", 0.0)),
            source_span=d.get("source_span", ""),
            notes=d.get("notes", ""),
        )


# ---------------------------------------------------------------------------
# Case record
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class CaseRecord:
    case_id: str
    source_filename: str
    raw_text: str
    status: CaseStatus = CaseStatus.INGESTED
    created_at: float = field(default_factory=time.time)
    case_number: ExtractedField = field(default_factory=ExtractedField)
    court: ExtractedField = field(default_factory=ExtractedField)
    next_hearing: ExtractedField = field(default_factory=ExtractedField)
    required_action: ExtractedField = field(default_factory=ExtractedField)
    petitioner: ExtractedField = field(default_factory=ExtractedField)
    respondent: ExtractedField = field(default_factory=ExtractedField)
    extraction_warnings: List[str] = field(default_factory=list)
    summary_en: Optional[str] = None
    summary_hi: Optional[str] = None
    reminder_ids: List[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        critical = (self.case_number, self.court, self.next_hearing)
        return any(f.level is Confidence.LOW for f in critical)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "source_filename": self.source_filename,
            "raw_text": self.raw_text,
            "status": self.status.value,
            "created_at": self.created_at,
            "case_number": self.case_number.to_dict(),
            "court": self.court.to_dict(),
            "next_hearing": self.next_hearing.to_dict(),
            "required_action": self.required_action.to_dict(),
            "petitioner": self.petitioner.to_dict(),
            "respondent": self.respondent.to_dict(),
            "extraction_warnings": list(self.extraction_warnings),
            "summary_en": self.summary_en,
            "summary_hi": self.summary_hi,
            "reminder_ids": list(self.reminder_ids),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CaseRecord":
        return cls(
            case_id=d["case_id"],
            source_filename=d.get("source_filename", ""),
            raw_text=d.get("raw_text", ""),
            status=CaseStatus(d.get("status", "ingested")),
            created_at=float(d.get("created_at", time.time())),
            case_number=ExtractedField.from_dict(d["case_number"]),
            court=ExtractedField.from_dict(d["court"]),
            next_hearing=ExtractedField.from_dict(d["next_hearing"]),
            required_action=ExtractedField.from_dict(d["required_action"]),
            petitioner=ExtractedField.from_dict(d.get("petitioner", {})),
            respondent=ExtractedField.from_dict(d.get("respondent", {})),
            extraction_warnings=list(d.get("extraction_warnings", [])),
            summary_en=d.get("summary_en"),
            summary_hi=d.get("summary_hi"),
            reminder_ids=list(d.get("reminder_ids", [])),
        )


# ---------------------------------------------------------------------------
# Summary, reminders, approvals, audit, metrics
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class CaseSummary:
    case_id: str
    english: str
    hindi: str
    generated_at: float = field(default_factory=time.time)
    template_only: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ReminderRequest:
    reminder_id: str
    case_id: str
    channel: ReminderChannel
    to: str
    english: str
    hindi: str
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reminder_id": self.reminder_id,
            "case_id": self.case_id,
            "channel": self.channel.value,
            "to": self.to,
            "english": self.english,
            "hindi": self.hindi,
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class ReminderReceipt:
    reminder_id: str
    approval_id: str
    channel: ReminderChannel
    status: ApprovalState
    provider_message_id: Optional[str]
    delivered: bool
    latency_ms: float
    recorded_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reminder_id": self.reminder_id,
            "approval_id": self.approval_id,
            "channel": self.channel.value,
            "status": self.status.value,
            "provider_message_id": self.provider_message_id,
            "delivered": self.delivered,
            "latency_ms": round(self.latency_ms, 2),
            "recorded_at": self.recorded_at,
        }


@dataclass(slots=True)
class AuditEvent:
    sequence: int
    event_id: str
    action: str
    payload: Dict[str, Any]
    prev_hash: str
    entry_hash: str
    created_at: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ApprovalTicket:
    approval_id: str
    tool: str
    payload: Dict[str, Any]
    state: ApprovalState = ApprovalState.PENDING
    created_at: float = field(default_factory=time.time)
    decided_at: Optional[float] = None
    decided_by: Optional[str] = None
    reason: str = ""
    result: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "tool": self.tool,
            "payload": self.payload,
            "state": self.state.value,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "reason": self.reason,
            "result": self.result,
        }


@dataclass(slots=True)
class FieldMatch:
    field: str
    expected: Optional[str]
    actual: Optional[str]
    matched: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CaseMetric:
    case_id: str
    processing_seconds: float
    fields: List[FieldMatch] = field(default_factory=list)
    reminder_delivered: bool = False
    reminder_channel: str = ""

    @property
    def field_accuracy(self) -> float:
        if not self.fields:
            return 0.0
        return sum(1 for f in self.fields if f.matched) / len(self.fields)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "processing_seconds": round(self.processing_seconds, 4),
            "field_accuracy": round(self.field_accuracy, 4),
            "fields": [f.to_dict() for f in self.fields],
            "reminder_delivered": self.reminder_delivered,
            "reminder_channel": self.reminder_channel,
        }


@dataclass(slots=True)
class MetricsReport:
    generated_at: float
    total_cases: int
    extraction_accuracy: float
    per_field_accuracy: Dict[str, float]
    reminders_sent: int
    reminder_delivery_rate: float
    average_processing_time_seconds: float
    cases: List[CaseMetric] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": datetime.fromtimestamp(
                self.generated_at, tz=timezone.utc).isoformat(timespec="seconds"),
            "total_cases": self.total_cases,
            "extraction_accuracy": round(self.extraction_accuracy, 4),
            "per_field_accuracy": {
                k: round(v, 4) for k, v in self.per_field_accuracy.items()},
            "reminders_sent": self.reminders_sent,
            "reminder_delivery_rate": round(self.reminder_delivery_rate, 4),
            "average_processing_time_seconds": round(
                self.average_processing_time_seconds, 4),
            "cases": [c.to_dict() for c in self.cases],
        }

    def to_markdown(self) -> str:
        lines = [
            "# Bharat Civic Agent — Impact Metrics",
            "",
            f"Generated: {datetime.fromtimestamp(self.generated_at, tz=timezone.utc).isoformat(timespec='seconds')}",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Total cases | {self.total_cases} |",
            f"| **Extraction accuracy** | **{self.extraction_accuracy:.2%}** |",
            f"| Reminders sent | {self.reminders_sent} |",
            f"| **Reminder delivery rate** | **{self.reminder_delivery_rate:.2%}** |",
            f"| **Avg. processing time** | **{self.average_processing_time_seconds:.3f}s** |",
            "",
            "## Per-field accuracy",
            "",
            "| Field | Accuracy |",
            "|---|---|",
        ]
        for k, v in self.per_field_accuracy.items():
            lines.append(f"| {k} | {v:.2%} |")
        lines.extend(["", "## Per-case breakdown", "",
                      "| Case | Field acc. | Time (s) | Reminder |",
                      "|---|---|---|---|"])
        for c in self.cases:
            lines.append(
                f"| {c.case_id} | {c.field_accuracy:.2%} | "
                f"{c.processing_seconds:.3f} | "
                f"{'✅' if c.reminder_delivered else '❌'} |")
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# IDs + crypto helpers
# ---------------------------------------------------------------------------

def new_case_id() -> str:
    return f"case_{uuid.uuid4().hex[:12]}"


def new_reminder_id() -> str:
    return f"rem_{uuid.uuid4().hex[:12]}"


def new_approval_id() -> str:
    return f"apr_{uuid.uuid4().hex[:12]}"


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_iso_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
