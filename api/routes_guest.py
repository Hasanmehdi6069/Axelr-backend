# api/routes_guest.py
"""
AXELR — Guest Session Endpoints
================================
Unauthenticated /api/guest/* — ephemeral sessions stored in Redis,
message-capped, 1h TTL.
"""
from __future__ import annotations

import base64
import contextlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from .ai_router import route_ai_request_parallel
from .config import WORKSPACE_LLM_CONFIG
from .routes_chat import discover_schema, is_allowed_file
from .state import (
    delete_redis_cache,
    get_redis_cache,
    set_redis_cache,
)
from .workspace_detector import detect_workspace

router = APIRouter(tags=["guest"])

_GUEST_TTL = 3600
_GUEST_MESSAGE_LIMIT = 5


async def _create_session() -> tuple[str, str]:
    sid = secrets.token_urlsafe(16)
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    await set_redis_cache(f"guest:{sid}", {
        "expires": expires.isoformat(), "messages": [], "structured": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }, ttl=_GUEST_TTL)
    return sid, expires.isoformat()


async def _load_session(sid: str) -> tuple[dict | None, datetime | None]:
    session = await get_redis_cache(f"guest:{sid}")
    if not session:
        return None, None
    try:
        expires_at = datetime.fromisoformat(session["expires"])
    except Exception:
        return None, None
    if datetime.now(timezone.utc) > expires_at:
        await delete_redis_cache(f"guest:{sid}")
        return None, None
    return session, expires_at


@router.post("/api/guest/session")
async def create_guest_session():
    sid, _ = await _create_session()
    return {"sessionId": sid, "expiresIn": _GUEST_TTL}


@router.get("/api/guest/session/{session_id}")
async def get_guest_session(session_id: str):
    session, expires_at = await _load_session(session_id)
    if not session or not expires_at:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "sessionId": session_id,
        "expiresIn": int((expires_at - datetime.now(timezone.utc)).total_seconds()),
        "messageCount": len(session.get("messages", [])),
        "hasStructured": session.get("structured") is not None,
    }


@router.post("/api/guest/extract")
async def guest_extract(
    command: str = Form(...),
    workspace: str | None = Form(None),
    sessionId: str | None = Form(None),
    files: list[UploadFile] = File([]),
):
    file_infos = [{"filename": f.filename, "mimetype": f.content_type or ""} for f in files]
    workspace = workspace or detect_workspace(command, file_infos)
    if workspace not in ("data", "design", "core"):
        workspace = "core"

    if not sessionId:
        sessionId, _ = await _create_session()
    session, expires_at = await _load_session(sessionId)
    if not session or not expires_at:
        raise HTTPException(status_code=404, detail="Session not found")
    if len(session.get("messages", [])) >= _GUEST_MESSAGE_LIMIT:
        raise HTTPException(status_code=403, detail={
            "code": "GUEST_LIMIT_REACHED",
            "message": "Guest sessions limited to 5 messages.",
            "limit": _GUEST_MESSAGE_LIMIT,
            "used": len(session.get("messages", [])),
        })

    valid = [f for f in files if is_allowed_file(workspace, f.filename, f.content_type or "")]
    file_contents = []
    for f in valid:
        content_bytes = await f.read()
        file_contents.append({
            "filename": f.filename,
            "mimetype": f.content_type or "application/octet-stream",
            "content_base64": base64.b64encode(content_bytes).decode("utf-8"),
        })

    context = ""
    schema_info = await discover_schema(file_contents)
    if schema_info:
        context = f"\nSchema info: {schema_info}\n"

    llm_config = WORKSPACE_LLM_CONFIG.get(workspace, WORKSPACE_LLM_CONFIG["core"])
    ai_result = await route_ai_request_parallel(
        workspace=workspace,
        task_type="extraction" if workspace == "data" else "frontend",
        prompt=command,
        history=session.get("messages", []),
        files=file_contents,
        max_tokens=llm_config["max_tokens"],
        temp=llm_config["temperature"],
        tier="free", user=None, context=context,
    )
    if not ai_result.get("success"):
        raise HTTPException(status_code=503, detail="AI service unavailable")

    ai_text = ai_result["text"]
    structured: list = []
    json_match = re.search(r"\[JSON-DATA\](.*?)\[/JSON-DATA\]", ai_text, re.DOTALL)
    if json_match:
        with contextlib.suppress(Exception):
            structured = json.loads(json_match.group(1).strip())
        ai_text = re.sub(r"\[JSON-DATA\].*?\[/JSON-DATA\]", "", ai_text, flags=re.DOTALL).strip()

    session["messages"].append({"role": "user", "text": command,
                                 "attachedFiles": [f.filename for f in valid]})
    session["messages"].append({"role": "model", "text": ai_text,
                                 "variants": [ai_text], "activeVariant": 0,
                                 "canRegenerate": True,
                                 "createdAt": datetime.now(timezone.utc).isoformat()})
    session["structured"] = structured

    remaining_ttl = int((expires_at - datetime.now(timezone.utc)).total_seconds())
    if remaining_ttl > 0:
        await set_redis_cache(f"guest:{sessionId}", session, ttl=remaining_ttl)

    remaining = max(0, _GUEST_MESSAGE_LIMIT - (len(session["messages"]) // 2))
    return {
        "success": True, "text": ai_text, "sessionId": sessionId,
        "structuredData": structured,
        "filename": f"Export_{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv",
        "provider": ai_result.get("provider"),
        "model": ai_result.get("model_used"),
        "remaining": remaining,
    }


__all__ = ["router"]