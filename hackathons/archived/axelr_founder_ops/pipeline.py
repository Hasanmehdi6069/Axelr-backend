"""Ingest → extract → review → execute → brief.

Tasks: T3.1–T3.7. Requirements: R-1..R-20.

This is the business-logic layer. It knows nothing about HTTP, and it
owns the state machine that enforces HETIC's human-in-the-loop gate.

The line that matters most in the entire codebase:

    backend/pipeline.py::execute_proposal
        if proposal["status"] != "approved":
            raise ExecutionRefused(...)

Grep for `R-16` and you land here. That is the Kiroween traceability.
"""

from __future__ import annotations

import io
import json
import logging
import os
from datetime import date, timedelta
from typing import Any

from .core import (
    Conflict,
    ExecutionRefused,
    LLMUnavailable,
    NotFound,
    PayloadTooLarge,
    Unsupported,
    connect,
    list_audit,
    new_id,
    now_ms,
    record_audit,
    settings,
    sha256_hex,
)
from .llm import llm

logger = logging.getLogger("founder_ops.pipeline")

_ALLOWED_EXT = {".pdf", ".docx", ".txt", ".md", ".png"}

_SINKS = {
    "task": "data/executed_tasks.jsonl",
    "expense": "data/expenses.jsonl",
    "reminder": "data/reminders.jsonl",
}

_OPEN_STATES = ("pending", "edited", "approved")
_CLOSED_STATES = ("executed", "rejected")


# ═══════════════════════════════════════════════════════════════════════
# T3.1 — Ingest (R-1..R-4)
# ═══════════════════════════════════════════════════════════════════════


async def ingest_file(filename: str, raw: bytes) -> dict[str, Any]:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in _ALLOWED_EXT:
        raise Unsupported(f"extension {ext!r} not allowed (R-3)")

    if len(raw) > settings.max_upload_bytes:
        raise PayloadTooLarge(f"file {len(raw)}B exceeds {settings.max_upload_bytes}B (R-2)")

    digest = sha256_hex(raw)
    async with connect() as db:
        cur = await db.execute("SELECT id FROM documents WHERE content_hash = ?", (digest,))
        existing = await cur.fetchone()
    if existing:
        # R-4: dedup by hash. Return existing id; no re-store.
        await record_audit("founder", "ingest.dedup", existing[0])
        return {"document_id": existing[0], "dedup": True}

    text = _extract_text(ext, raw, filename)
    doc_id = new_id("doc")
    async with connect() as db:
        await db.execute(
            "INSERT INTO documents (id, filename, ext, content_hash,"
            " raw_text, size_bytes, uploaded_at, status)"
            " VALUES (?,?,?,?,?,?,?, 'ingested')",
            (doc_id, filename or "unnamed", ext, digest, text, len(raw), now_ms()),
        )
        await db.commit()

    await record_audit(
        "founder", "ingest.created", doc_id, {"filename": filename, "bytes": len(raw), "chars": len(text)}
    )
    return {"document_id": doc_id, "dedup": False, "chars": len(text), "bytes": len(raw)}


def _extract_text(ext: str, raw: bytes, filename: str) -> str:
    try:
        if ext == ".pdf":
            from pypdf import PdfReader

            return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(raw)).pages)
        if ext == ".docx":
            import docx

            return "\n".join(p.text for p in docx.Document(io.BytesIO(raw)).paragraphs)
        if ext in (".txt", ".md"):
            return raw.decode("utf-8", errors="replace")
        if ext == ".png":
            # R-3, design §10: OCR out of scope for v0. Accepted, stubbed.
            return f"[image:{filename}] (no OCR in v0 — text extraction skipped)"
    except Exception as e:
        logger.warning("text_extraction_failed ext=%s err=%s", ext, e)
        return f"[extraction_failed:{type(e).__name__}]"
    return ""


# ═══════════════════════════════════════════════════════════════════════
# T3.2 — Extract (R-5..R-10)
# ═══════════════════════════════════════════════════════════════════════


async def extract_document(document_id: str) -> dict[str, Any]:
    async with connect() as db:
        cur = await db.execute(
            "SELECT raw_text, status FROM documents WHERE id = ?",
            (document_id,),
        )
        row = await cur.fetchone()
    if row is None:
        raise NotFound("document_not_found (R-5)")
    raw_text, doc_status = row
    if doc_status == "extracted":
        return {"document_id": document_id, "already_extracted": True}

    try:
        valid, dropped = await llm.extract(raw_text)
    except LLMUnavailable as e:
        # R-7, R-10: mark failed, do not persist partial proposals.
        async with connect() as db:
            await db.execute(
                "UPDATE documents SET status='failed', error=? WHERE id=?",
                (str(e), document_id),
            )
            await db.commit()
        await record_audit("founder", "extract.failed", document_id, {"error": str(e)})
        raise

    ts = now_ms()
    created_ids: list[str] = []
    async with connect() as db:
        for env in valid:
            pid = new_id("prop")
            body = getattr(env, env.kind).model_dump(mode="json")
            await db.execute(
                "INSERT INTO proposals (id, document_id, kind, payload,"
                " confidence, reasoning, status, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?, 'pending', ?, ?)",
                (pid, document_id, env.kind, json.dumps(body), env.confidence, env.reasoning, ts, ts),
            )
            created_ids.append(pid)
        await db.execute(
            "UPDATE documents SET status='extracted', error=NULL WHERE id=?",
            (document_id,),
        )
        await db.commit()

    await record_audit("founder", "extract.ok", document_id, {"proposals": len(created_ids), "dropped": dropped})
    return {
        "document_id": document_id,
        "proposals_created": len(created_ids),
        "dropped": dropped,
    }


# ═══════════════════════════════════════════════════════════════════════
# T3.3–T3.5 — Review (R-11..R-15, design §5)
# ═══════════════════════════════════════════════════════════════════════


async def list_proposals(
    status: str | None = None,
    document_id: str | None = None,
) -> list[dict[str, Any]]:
    clauses, args = [], []
    if status:
        clauses.append("status = ?")
        args.append(status)
    if document_id:
        clauses.append("document_id = ?")
        args.append(document_id)
    sql = "SELECT * FROM proposals"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC"
    async with connect() as db:
        cur = await db.execute(sql, args)
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


async def get_proposal(proposal_id: str) -> dict[str, Any]:
    async with connect() as db:
        cur = await db.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
        row = await cur.fetchone()
    if row is None:
        raise NotFound("proposal_not_found")
    return _row_to_dict(row)


async def edit_proposal(
    proposal_id: str,
    envelope: Any,
    actor: str = "founder",
) -> dict[str, Any]:
    current = await get_proposal(proposal_id)
    if current["status"] not in ("pending", "edited"):
        # R-13, design §5: refuse edit after approval/execution/rejection.
        raise Conflict(f"cannot_edit_status_{current['status']} (R-13)")
    body = getattr(envelope, envelope.kind).model_dump(mode="json")
    ts = now_ms()
    async with connect() as db:
        await db.execute(
            "UPDATE proposals SET kind=?, payload=?, confidence=?,"
            " reasoning=?, status='edited', updated_at=? WHERE id=?",
            (envelope.kind, json.dumps(body), envelope.confidence, envelope.reasoning, ts, proposal_id),
        )
        await db.commit()
    await record_audit(actor, "proposal.edited", proposal_id, {"kind": envelope.kind})
    return await get_proposal(proposal_id)


async def approve_proposal(
    proposal_id: str,
    actor: str = "founder",
) -> dict[str, Any]:
    current = await get_proposal(proposal_id)
    if current["status"] not in ("pending", "edited"):
        raise Conflict(f"cannot_approve_status_{current['status']} (R-14)")
    ts = now_ms()
    async with connect() as db:
        await db.execute(
            "UPDATE proposals SET status='approved', decided_at=?, updated_at=? WHERE id=?",
            (ts, ts, proposal_id),
        )
        await db.commit()
    await record_audit(actor, "proposal.approved", proposal_id)
    return await get_proposal(proposal_id)


async def reject_proposal(
    proposal_id: str,
    actor: str = "founder",
) -> dict[str, Any]:
    current = await get_proposal(proposal_id)
    if current["status"] in _CLOSED_STATES:
        raise Conflict(f"cannot_reject_status_{current['status']} (R-15)")
    ts = now_ms()
    async with connect() as db:
        await db.execute(
            "UPDATE proposals SET status='rejected', decided_at=?, updated_at=? WHERE id=?",
            (ts, ts, proposal_id),
        )
        await db.commit()
    await record_audit(actor, "proposal.rejected", proposal_id)
    return await get_proposal(proposal_id)


# ═══════════════════════════════════════════════════════════════════════
# T3.6 — Execute (R-16..R-18)
# ═══════════════════════════════════════════════════════════════════════


async def execute_proposal(
    proposal_id: str,
    actor: str = "founder",
) -> dict[str, Any]:
    """Run one proposal through its sink, but ONLY if approved.

    This guard is the entire HETIC H-3 checkpoint. It is checked at the
    adapter boundary — not at the route, not at the UI — so that *no*
    code path (future or current) can bypass it. See R-16, R-17.
    """
    proposal = await get_proposal(proposal_id)

    # ═══════════════ R-16 / R-17 — THE GUARD ═══════════════
    if proposal["status"] != "approved":
        await record_audit(actor, "execute.refused", proposal_id, {"status": proposal["status"]})
        raise ExecutionRefused(f"status_{proposal['status']}_not_approved (R-16)")
    # ═══════════════════════════════════════════════════════

    kind = proposal["kind"]
    sink = _SINKS[kind]
    os.makedirs(os.path.dirname(sink), exist_ok=True)
    line = {
        "proposal_id": proposal["id"],
        "kind": kind,
        "executed_at": now_ms(),
        "payload": proposal["payload"],
    }
    with open(sink, "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")

    ts = now_ms()
    result = {"sink": sink, "line": line}
    async with connect() as db:
        await db.execute(
            "UPDATE proposals SET status='executed', executed_at=?, execution_result=?, updated_at=? WHERE id=?",
            (ts, json.dumps(result), ts, proposal_id),
        )
        await db.commit()
    await record_audit(actor, "proposal.executed", proposal_id, {"sink": sink})
    return {"executed": True, "result": result}


# ═══════════════════════════════════════════════════════════════════════
# T3.7 — Daily brief (R-19, R-20)
# ═══════════════════════════════════════════════════════════════════════


async def generate_brief() -> dict[str, Any]:
    rows = await list_proposals()
    actionable = [r for r in rows if r["status"] in _OPEN_STATES]
    today = date.today()

    groups: dict[str, list[dict[str, Any]]] = {
        "today": [],
        "this_week": [],
        "later": [],
        "no_date": [],
    }
    for r in actionable:
        bucket = _bucket(r, today)
        groups[bucket].append(
            {
                "id": r["id"],
                "kind": r["kind"],
                "status": r["status"],
                "title": _title(r),
                "confidence": r["confidence"],
                "reasoning": r["reasoning"][:140],
            }
        )

    return {
        "generated_at": now_ms(),
        "total": len(actionable),
        "groups": groups,
    }


def _bucket(r: dict[str, Any], today: date) -> str:
    d = _due_date(r)
    if d is None:
        return "no_date"
    if d <= today:
        return "today"
    if d <= today + timedelta(days=7):
        return "this_week"
    return "later"


def _due_date(r: dict[str, Any]) -> date | None:
    p = r["payload"]
    raw = p.get("due_date") or p.get("date") or p.get("when")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


def _title(r: dict[str, Any]) -> str:
    p = r["payload"]
    return p.get("title") or p.get("vendor") or p.get("text") or r["id"]


# ═══════════════════════════════════════════════════════════════════════
# Row helper
# ═══════════════════════════════════════════════════════════════════════


def _row_to_dict(row: Any) -> dict[str, Any]:
    (pid, did, kind, payload, conf, reason, status, created, updated, decided, executed, result) = row
    return {
        "id": pid,
        "document_id": did,
        "kind": kind,
        "payload": json.loads(payload),
        "confidence": conf,
        "reasoning": reason,
        "status": status,
        "created_at": created,
        "updated_at": updated,
        "decided_at": decided,
        "executed_at": executed,
        "execution_result": json.loads(result) if result else None,
    }


# Re-export audit helper so main.py imports from one place (routes).
__all__ = [
    "ingest_file",
    "extract_document",
    "list_proposals",
    "get_proposal",
    "edit_proposal",
    "approve_proposal",
    "reject_proposal",
    "execute_proposal",
    "generate_brief",
    "list_audit",
]
