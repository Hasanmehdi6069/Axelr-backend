"""Founder Ops Copilot — core primitives.

Implements tasks T1.1–T1.6. Everything that touches the DB, defines a
config value, or emits an audit row lives here so the pipeline modules
stay focused on business logic.

Requirement coverage:
    R-8, R-9   → ProposalEnvelope discriminated union
    R-21, R-22 → audit.record() — append-only
    N-2        → Settings.from_env()
    N-6        → new_id / now_ms / sha256_hex log helpers
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, Literal, Union

import aiosqlite
from pydantic import BaseModel, Field, TypeAdapter

logger = logging.getLogger("founder_ops.core")


# ═══════════════════════════════════════════════════════════════════════
# Config (T1.1, N-2)
# ═══════════════════════════════════════════════════════════════════════


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    """Immutable, env-driven settings. No secret has a default."""

    ollama_url: str
    ollama_model: str
    cloud_fallback_enabled: bool
    cloud_base_url: str
    cloud_api_key: str
    cloud_model: str
    db_path: str
    max_upload_bytes: int
    llm_timeout_s: float

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            ollama_url=os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/"),
            ollama_model=os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct"),
            cloud_fallback_enabled=_bool("CLOUD_FALLBACK_ENABLED", False),
            cloud_base_url=os.getenv("CLOUD_BASE_URL", "").rstrip("/"),
            cloud_api_key=os.getenv("CLOUD_API_KEY", ""),
            cloud_model=os.getenv("CLOUD_MODEL", "gpt-4o-mini"),
            db_path=os.getenv("FOUNDER_OPS_DB", "data/founder_ops.db"),
            max_upload_bytes=int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024))),
            llm_timeout_s=float(os.getenv("LLM_TIMEOUT_S", "120")),
        )


settings = Settings.from_env()


# ═══════════════════════════════════════════════════════════════════════
# Errors (T1.2, N-1)
# ═══════════════════════════════════════════════════════════════════════


class AppError(Exception):
    """Base class for all application errors surfaced to HTTP."""

    status: int = 500
    code: str = "internal_error"
    default_message: str = "Internal error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)
        self.message = message or self.default_message


class NotFound(AppError):
    status, code, default_message = 404, "not_found", "Resource not found"


class Conflict(AppError):
    status, code, default_message = 409, "conflict", "State transition not allowed"


class Unsupported(AppError):
    status, code, default_message = 415, "unsupported_media", "Unsupported media type"


class PayloadTooLarge(AppError):
    status, code, default_message = 413, "payload_too_large", "Payload too large"


class ValidationFailed(AppError):
    status, code, default_message = 422, "validation_failed", "Validation failed"


class LLMUnavailable(AppError):
    status, code, default_message = 503, "llm_unavailable", "LLM unavailable"


class ExecutionRefused(AppError):
    status, code, default_message = 409, "execution_refused", "Execution refused"


# ═══════════════════════════════════════════════════════════════════════
# Utilities (T1.6, N-6)
# ═══════════════════════════════════════════════════════════════════════


def now_ms() -> int:
    return int(time.time() * 1000)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def new_id(prefix: str) -> str:
    """Short, sortable-ish IDs. Prefix namespaces collision domains."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ═══════════════════════════════════════════════════════════════════════
# Pydantic models (T1.4, R-8, R-9, design §4)
# ═══════════════════════════════════════════════════════════════════════


class TaskPayload(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    assignee: str = Field(default="", max_length=100)
    due_date: date | None = None
    priority: Literal["low", "medium", "high"] = "medium"


class ExpensePayload(BaseModel):
    vendor: str = Field(..., min_length=1, max_length=120)
    amount: float = Field(..., ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    category: Literal["software", "travel", "meals", "payroll", "other"] = "other"
    date: date


class ReminderPayload(BaseModel):
    text: str = Field(..., min_length=1, max_length=300)
    when: date


class _EnvelopeBase(BaseModel):
    confidence: float = Field(..., ge=0, le=1)
    reasoning: str = Field(..., min_length=1, max_length=500)


class TaskEnvelope(_EnvelopeBase):
    kind: Literal["task"]
    task: TaskPayload


class ExpenseEnvelope(_EnvelopeBase):
    kind: Literal["expense"]
    expense: ExpensePayload


class ReminderEnvelope(_EnvelopeBase):
    kind: Literal["reminder"]
    reminder: ReminderPayload


ProposalEnvelope = Annotated[
    Union[TaskEnvelope, ExpenseEnvelope, ReminderEnvelope],
    Field(discriminator="kind"),
]

# Single adapter — used by llm.py (per-item) and pipeline.review (edit).
ENVELOPE_ADAPTER: TypeAdapter[Any] = TypeAdapter(ProposalEnvelope)


class ProposalEditRequest(BaseModel):
    """Founder-supplied edit — validated against the discriminated union."""

    envelope: ProposalEnvelope


# ═══════════════════════════════════════════════════════════════════════
# Database (T1.3, design §3)
# ═══════════════════════════════════════════════════════════════════════

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id           TEXT PRIMARY KEY,
    filename     TEXT NOT NULL,
    ext          TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    raw_text     TEXT NOT NULL,
    size_bytes   INTEGER NOT NULL,
    uploaded_at  INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'ingested',
    error        TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
    id               TEXT PRIMARY KEY,
    document_id      TEXT NOT NULL REFERENCES documents(id),
    kind             TEXT NOT NULL,
    payload          TEXT NOT NULL,
    confidence       REAL NOT NULL,
    reasoning        TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    decided_at       INTEGER,
    executed_at      INTEGER,
    execution_result TEXT
);
CREATE INDEX IF NOT EXISTS ix_proposals_status   ON proposals(status);
CREATE INDEX IF NOT EXISTS ix_proposals_document ON proposals(document_id);
CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    subject TEXT NOT NULL,
    detail  TEXT
);
"""


async def init_db() -> None:
    """Idempotent. Called from FastAPI lifespan on startup."""
    parent = os.path.dirname(settings.db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    async with aiosqlite.connect(settings.db_path) as db:
        await db.executescript(_SCHEMA)
        await db.commit()


def connect() -> aiosqlite.Connection:
    return aiosqlite.connect(settings.db_path)


# ═══════════════════════════════════════════════════════════════════════
# Audit (T1.5, R-21, R-22)
# ═══════════════════════════════════════════════════════════════════════


async def record_audit(
    actor: str,
    action: str,
    subject: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one audit row. There is deliberately no update/delete path —
    R-22 requires the log be append-only. Grep confirms only INSERT and
    SELECT appear against `audit_log` anywhere in the codebase."""
    async with connect() as db:
        await db.execute(
            "INSERT INTO audit_log (ts, actor, action, subject, detail) VALUES (?,?,?,?,?)",
            (now_ms(), actor, action, subject, json.dumps(detail or {})),
        )
        await db.commit()
    logger.info(
        "audit actor=%s action=%s subject=%s detail=%s",
        actor,
        action,
        subject,
        detail or {},
    )


async def list_audit(limit: int = 100) -> list[dict[str, Any]]:
    """Read-only. Used by the ActivityFeed component (T5.7)."""
    async with connect() as db:
        cur = await db.execute(
            "SELECT id, ts, actor, action, subject, detail FROM audit_log ORDER BY id DESC LIMIT ?",
            (min(max(limit, 1), 500),),
        )
        rows = await cur.fetchall()
    return [
        {
            "id": r[0],
            "ts": r[1],
            "actor": r[2],
            "action": r[3],
            "subject": r[4],
            "detail": json.loads(r[5]) if r[5] else None,
        }
        for r in rows
    ]
