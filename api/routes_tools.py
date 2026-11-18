# api/routes_tools.py
"""
AXELR — Tooling surface (production).
=====================================
Post-consolidation tooling endpoints. Legacy migration shims, demo
generators, and the DB-polling session stream have been removed.

Surviving surface:
  * /api/execute-code, /api/touch_fix, /api/refine-response
  * /api/summarize-chat, /api/suggestions
  * elite tools: translate-code, mermaid, scan-pii, meeting-minutes,
    decision-matrix

Every handler is fenced by a hard 15s wall-clock deadline that maps to
HTTP 504 (STAGE 4.1).
"""
from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, TypeVar

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .ai_router import route_ai_request_parallel
from .auth import get_current_user
from .state import (
    get_object_id,
    state,
)

logger = structlog.get_logger("axelr.tools")

router = APIRouter(tags=["tools"])

# ── Hard execution fence (STAGE 4.1) ────────────────────────────────────────
SERVER_HANDLER_TIMEOUT_S: float = 15.0

_F = TypeVar("_F", bound=Callable[..., Awaitable[Any]])


def hard_timeout(seconds: float = SERVER_HANDLER_TIMEOUT_S) -> Callable[[_F], _F]:
    """Wrap an async endpoint with a hard wall-clock deadline → HTTP 504."""
    def decorator(fn: _F) -> _F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await asyncio.wait_for(fn(*args, **kwargs), timeout=seconds)
            except asyncio.TimeoutError as exc:
                logger.warning(
                    "handler_timeout", handler=fn.__name__, timeout=seconds,
                )
                raise HTTPException(status_code=504, detail="gateway_timeout") from exc
        return wrapper  # type: ignore[return-value]
    return decorator


# Guarded worker-client import (same pattern used in routes_core.py).
try:
    from core.io_pipeline import execute_code_on_worker
except Exception:
    async def execute_code_on_worker(language, code, timeout=8):
        return {"success": False, "output": "", "error": "worker_unavailable"}


# ═══════════════════════════════════════════════════════════════════════════
# Execute code
# ═══════════════════════════════════════════════════════════════════════════

class ExecuteRequest(BaseModel):
    language: str
    code: str
    timeout: int | None = 5


@router.post("/api/execute-code")
@hard_timeout()
async def execute_code_endpoint(
    data: ExecuteRequest, user: dict = Depends(get_current_user),
):
    if len(data.code) > 40_000:
        raise HTTPException(status_code=413, detail="code_too_large")
    timeout = max(1, min(int(data.timeout or 5), 10))
    return await execute_code_on_worker(data.language, data.code, timeout)


# ═══════════════════════════════════════════════════════════════════════════
# Touch-fix
# ═══════════════════════════════════════════════════════════════════════════

class TouchFixRequest(BaseModel):
    code: str
    error_message: str
    task_type: str | None = "touch_fix"
    diff: str | None = None


@router.post("/api/touch_fix")
@hard_timeout()
async def touch_fix_endpoint(
    data: TouchFixRequest, user: dict = Depends(get_current_user),
):
    engine = state.touch_fix_engine
    if engine is None:
        try:
            from core.code_intel import TouchFixEngine
            engine = TouchFixEngine(route_func=route_ai_request_parallel)
            state.touch_fix_engine = engine
        except Exception:
            raise HTTPException(status_code=503, detail="TouchFix unavailable")

    if data.diff:
        return {"success": True, "fixed_code": engine.apply_diff(data.code, data.diff)}

    fixed_code = await engine.fix_block(
        full_code=data.code, error_block=data.code,
        error_message=data.error_message, language="html",
        tier=user.get("tier", "free"), user=user,
    )
    return {"success": True, "fixed_code": fixed_code}


# ═══════════════════════════════════════════════════════════════════════════
# Refine / summarize-chat / suggestions
# ═══════════════════════════════════════════════════════════════════════════

class RefineRequest(BaseModel):
    sessionId: str
    msgId: str
    newText: str
    originalText: str


@router.post("/api/refine-response")
@hard_timeout()
async def refine_response(
    data: RefineRequest, user: dict = Depends(get_current_user),
):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    ObjectId = get_object_id()
    if not ObjectId or not ObjectId.is_valid(data.sessionId):
        raise HTTPException(400, "Invalid session ID")
    session = await state.sessions_col.find_one(
        {"_id": ObjectId(data.sessionId), "userId": user["_id"]}
    )
    if not session:
        raise HTTPException(404, "Session not found")

    messages = session.get("messages", [])
    msg_index = next(
        (i for i, m in enumerate(messages) if str(m.get("_id")) == data.msgId),
        -1,
    )
    if msg_index == -1:
        raise HTTPException(404, "Message not found")

    messages[msg_index]["text"] = data.newText
    messages = messages[: msg_index + 1]

    history = messages[:-1]
    command = messages[-1].get("text", "")
    workspace = session.get("workspace", "core")

    result = await route_ai_request_parallel(
        workspace=workspace, task_type="structuring", prompt=command,
        history=history, files=[], max_tokens=2048, temp=0.5,
        tier=user.get("tier", "free"), user=user, context="",
    )
    if not result.get("success"):
        raise HTTPException(503, "AI service unavailable")

    new_response = result["text"]
    messages.append({
        "role": "model", "text": new_response,
        "variants": [new_response], "activeVariant": 0,
        "canRegenerate": True, "createdAt": datetime.utcnow(),
    })
    await state.sessions_col.update_one(
        {"_id": ObjectId(data.sessionId)},
        {"$set": {"messages": messages}},
    )
    return {"success": True, "refined": new_response}


@router.post("/api/summarize-chat")
@hard_timeout()
async def summarize_chat(
    session_id: str, user: dict = Depends(get_current_user),
):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    ObjectId = get_object_id()
    if not ObjectId or not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")
    session = await state.sessions_col.find_one(
        {"_id": ObjectId(session_id), "userId": user["_id"]}
    )
    if not session:
        raise HTTPException(404, "Session not found")
    messages = session.get("messages", [])
    if not messages:
        return {"summary": "No messages to summarize."}

    conv = "\n".join(
        f"{m['role']}: {m['text']}" for m in messages if m.get("text")
    )
    prompt = (
        "Summarize the following conversation concisely (max 300 words). "
        "Include key decisions, questions, and answers.\n\n"
        f"{conv}"
    )
    result = await route_ai_request_parallel(
        workspace="core", task_type="summarize", prompt=prompt,
        history=[], files=[], max_tokens=1024, temp=0.2,
        tier=user.get("tier", "free"), user=user,
    )
    return {"success": True, "summary": result.get("text", "Unable to summarize.")}


@router.get("/api/suggestions")
@hard_timeout()
async def get_suggestions(
    workspace: str = "data", user: dict = Depends(get_current_user),
):
    suggestions = {
        "data": [
            "Extract key metrics from this invoice",
            "Analyze sales data and identify trends",
            "Clean and transform this dataset",
            "Generate a summary of this CSV file",
            "Compare these two spreadsheets",
        ],
        "design": [
            "Design a responsive navbar with dropdown",
            "Create a dark mode toggle button",
            "Generate a pricing card component",
            "Build a login form with validation",
            "Make this existing page mobile-friendly",
        ],
        "core": [
            "Summarize this text",
            "Explain this concept in simple terms",
            "Draft a professional email",
            "Provide a step-by-step guide",
            "Brainstorm ideas for a project",
        ],
    }
    return {"suggestions": suggestions.get(workspace, suggestions["core"])}


# ═══════════════════════════════════════════════════════════════════════════
# Elite tools
# ═══════════════════════════════════════════════════════════════════════════

class CodeTranslateRequest(BaseModel):
    code: str
    source_lang: str
    target_lang: str


@router.post("/api/tools/translate-code")
@hard_timeout()
async def tool_translate_code(
    data: CodeTranslateRequest, user: dict = Depends(get_current_user),
):
    prompt = (
        f"Translate the following code from {data.source_lang} to {data.target_lang}. "
        f"Preserve idiomatic patterns, comments, and safety. Return only the code block.\n\n"
        f"```{data.source_lang}\n{data.code}\n```"
    )
    res = await route_ai_request_parallel(
        "design", "structuring", prompt, [], [], 4096, 0.2,
        user.get("tier", "free"), user,
    )
    return {"success": True, "translated_code": res["text"]}


class MermaidRequest(BaseModel):
    process_description: str


@router.post("/api/tools/mermaid")
@hard_timeout()
async def tool_mermaid_generator(
    data: MermaidRequest, user: dict = Depends(get_current_user),
):
    prompt = (
        f"Generate a syntactically valid Mermaid.js diagram representing this process:\n\n"
        f"{data.process_description}\n\nOutput ONLY a valid ```mermaid code block."
    )
    res = await route_ai_request_parallel(
        "core", "structuring", prompt, [], [], 2048, 0.2,
        user.get("tier", "free"), user,
    )
    return {"success": True, "mermaid": res["text"]}


class PIIScanRequest(BaseModel):
    document_text: str


@router.post("/api/tools/scan-pii")
@hard_timeout()
async def tool_pii_scanner(
    data: PIIScanRequest, user: dict = Depends(get_current_user),
):
    prompt = (
        "Audit this text for PII including names, emails, phones, IPs, credentials, "
        "and financial references. Return a clean JSON array: "
        "[{'type': str, 'value': str, 'risk': 'low'|'medium'|'high'}].\n\n"
        f"Text:\n{data.document_text[:8000]}"
    )
    res = await route_ai_request_parallel(
        "data", "extraction", prompt, [], [], 2048, 0.1,
        user.get("tier", "free"), user,
    )
    return {"success": True, "scan_report": res["text"]}


class MeetingMinutesRequest(BaseModel):
    transcript: str


@router.post("/api/tools/meeting-minutes")
@hard_timeout()
async def tool_meeting_minutes(
    data: MeetingMinutesRequest, user: dict = Depends(get_current_user),
):
    prompt = (
        "Extract structured meeting minutes:\n"
        "1. Executive Summary\n2. Key Decisions\n"
        "3. Action Items Table: Task, Owner, Priority, Target Date.\n\n"
        f"Transcript:\n{data.transcript[:10000]}"
    )
    res = await route_ai_request_parallel(
        "core", "structuring", prompt, [], [], 4096, 0.2,
        user.get("tier", "free"), user,
    )
    return {"success": True, "minutes": res["text"]}


class DecisionMatrixRequest(BaseModel):
    options: list[str]
    criteria: list[str]
    context: str | None = ""


@router.post("/api/tools/decision-matrix")
@hard_timeout()
async def tool_decision_matrix(
    data: DecisionMatrixRequest, user: dict = Depends(get_current_user),
):
    prompt = (
        f"Build a weighted Decision Matrix comparing: {', '.join(data.options)}.\n"
        f"Criteria: {', '.join(data.criteria)}.\nContext: {data.context}\n\n"
        "Provide a Markdown table with weights (1-5), ratings (1-10), total scores, "
        "and a decisive recommendation."
    )
    res = await route_ai_request_parallel(
        "data", "extraction", prompt, [], [], 4096, 0.3,
        user.get("tier", "free"), user,
    )
    return {"success": True, "matrix": res["text"]}


__all__ = [
    "CodeTranslateRequest",
    "DecisionMatrixRequest",
    "ExecuteRequest",
    "MeetingMinutesRequest",
    "MermaidRequest",
    "PIIScanRequest",
    "RefineRequest",
    "TouchFixRequest",
    "router",
]