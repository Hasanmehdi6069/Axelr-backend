# api/routes_chat.py
"""
AXELR — Chat & Extract Endpoints
=================================
The four user-facing AI endpoints:

    POST /api/chat             — cascade-routed chat
    POST /api/extract          — file-attached extraction
    POST /api/extract_stream   — SSE stream with token-by-token delivery

Plus file validation, schema discovery, and session persistence helpers.
"""

import base64
import contextlib
import csv
import io
import json
import re
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.persistence import PERSIST

from .ai_router import (
    generate_with_cascade,
    stream_ai_response,
)
from .auth import get_current_user
from .config import WORKSPACE_LLM_CONFIG
from .prompts import sanitize_input
from .quota import check_rate_limit, estimate_tokens
from .state import get_object_id, limiter, state
from .workspace_detector import detect_workspace

logger = structlog.get_logger("axelr.chat")

router = APIRouter(tags=["AI"])
# ── Internal twin endpoints (health, metrics, diagnose) ────────────────────
try:
    from core.twin_engine import twin_health_router as _twin_health_router
    if _twin_health_router is not None:
        router.include_router(_twin_health_router)
except Exception:  # pragma: no cover — twin is optional
    pass
# ═══════════════════════════════════════════════════════════════════════════
# Ghost Telemetry & Pre-Warm endpoints (Axelr Hot Swap Engine contract)
# ═══════════════════════════════════════════════════════════════════════════
class TelemetryPayload(BaseModel):
    ram_free_mb: int = 0
    ram_total_mb: int = 0
    cores: int = 1
    has_simd: bool = False
    has_wasm: bool = False
    has_webgpu: bool = False
    battery_pct: int | None = None
    thermal_state: str | None = None
    # Optional scope hints — the client may also send these as headers.
    task_type: str = ""
    workspace: str = "core"
    prompt_hint: str = ""

class PrewarmPayload(BaseModel):
    workspace: str = "core"
    task_type: str = "structuring"
    prompt_hint: str = ""

@router.post("/api/twin/telemetry", tags=["AI"])
@limiter.limit("120/minute")
async def twin_telemetry(
    body: TelemetryPayload, request: Request,
    user: dict = Depends(get_current_user),
):
    """
    Client → server device fingerprint. Returns a tier-aware, task-aware
    routing decision. Fields:
        category · memory_envelope_mb · headroom_mb · purge_policy
        context_window_hint · cache_prune_aggressive · suggested_models
        specialist_model · specialist_role · deep_think_enabled
        prewarm_hints · advisory · override_required · override_seconds
        accept_id · device_class · tier_honored · micro_volatile
    """
    from core.ghost_telemetry import accept_telemetry
    session_id = request.headers.get("x-session-id") or str(user.get("_id", ""))
    tier = (user.get("tier") if isinstance(user, dict) else None) or "free"
    # Optional scope hints from the client — task/workspace/prompt help
    # the specialist picker choose the right model for the current work.
    task_type = request.headers.get("x-task-type", "") or body.task_type or ""
    workspace = request.headers.get("x-workspace", "") or "core"
    prompt_hint = request.headers.get("x-prompt-hint", "")
    decision = accept_telemetry(
        body.model_dump(),
        session_id=session_id, tier=tier,
        task_type=task_type, workspace=workspace,
        prompt=prompt_hint,
    )
    return {"success": True, **decision.to_dict()}


@router.post("/api/twin/prewarm", tags=["AI"])
@limiter.limit("60/minute")
async def twin_prewarm(
    body: PrewarmPayload, request: Request,
    user: dict = Depends(get_current_user),
):
    """Client signals an imminent task. Server fires a task-specific warm-up."""
    from core.prewarm import signal as prewarm_signal
    workspace = body.workspace if body.workspace in (
        "data", "design", "core") else "core"
    task_type = body.task_type or "structuring"
    fired = prewarm_signal(workspace, task_type, body.prompt_hint)
    return {"success": True, "scheduled": fired}
# ═══════════════════════════════════════════════════════════════════════════
# Chat endpoint
# ═══════════════════════════════════════════════════════════════════════════
class ChatRequestBody(BaseModel):
    command: str
    workspace: str | None = None
    sessionId: str | None = None
    context: str | None = None
    provider: str = "gemini"
    max_tokens: int | None = None
    temperature: float | None = None
@router.get("/api/chat/history/{session_id}", tags=["AI"])
@limiter.limit("60/minute")
async def get_chat_history(
    session_id: str, request: Request,
    user: dict = Depends(get_current_user),
):
    """Fetch all chat messages for a given session"""
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    
    user_id = str(user["_id"])
    try:
        # Get all messages for this session from the database
        messages = await PERSIST.chats.get_session_messages(session_id, user_id)
        # Convert to the format the frontend expects
        frontend_messages = []
        for msg in messages:
            if msg.role == "user":
                frontend_messages.append({
                    "role": "user",
                    "text": msg.content,
                    "createdAt": datetime.fromtimestamp(msg.created_at, timezone.utc).isoformat()
                })
            elif msg.role == "assistant":
                frontend_messages.append({
                    "role": "model",
                    "text": msg.content,
                    "createdAt": datetime.fromtimestamp(msg.created_at, timezone.utc).isoformat(),
                    "variants": [msg.content],
                    "activeVariant": 0,
                    "canRegenerate": True
                })
        return {"success": True, "sessionId": session_id, "messages": frontend_messages}
    except Exception as e:
        logger.error("failed_to_fetch_chat_history session_id=%s error=%s", session_id, e)
        raise HTTPException(status_code=500, detail="Failed to fetch chat history")


@router.delete("/api/chat/session/{session_id}", tags=["AI"])
@limiter.limit("30/minute")
async def delete_chat_session(
    session_id: str, request: Request,
    user: dict = Depends(get_current_user),
):
    """Delete a chat session (only works for guest sessions to comply with your requirement)"""
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    
    user_id = str(user["_id"])
    # Only allow deletion if it's a guest session (user_id starts with guest_)
    if not user_id.startswith("guest_"):
        raise HTTPException(status_code=403, detail="Only guest sessions can be manually deleted")
    
    try:
        # Set expire_at to now for all messages in this session so MongoDB TTL purges them
        await PERSIST.chats.delete_guest_session(session_id, user_id)
        logger.info("guest_session_deleted session_id=%s user_id=%s", session_id, user_id)
        return {"success": True, "message": "Guest session deleted successfully"}
    except Exception as e:
        logger.error("failed_to_delete_guest_session session_id=%s error=%s", session_id, e)
        raise HTTPException(status_code=500, detail="Failed to delete session")


@router.post("/api/chat", tags=["AI"])
@limiter.limit("30/minute")
async def api_chat_main(
    body: ChatRequestBody, request: Request,
    user: dict = Depends(get_current_user),
):
    # Pass the authenticated user and their REAL tier through the cascade.
    # The previous revision hardcoded tier="free" / user=None, which meant
    # every paid user was routed through the free-tier provider pool and
    # every per-user quota counter was silently ignored.
    real_tier = (user.get("tier") if isinstance(user, dict) else None) or "free"
    return await generate_with_cascade(
        workspace=body.workspace or "core",
        user_id=str(user["_id"]) if user else None,
        session_id=body.sessionId,
        task_type="structuring",
        prompt=body.command,
        history=None, files=None,
        max_tokens=body.max_tokens or 2048,
        temp=body.temperature if body.temperature is not None else 0.4,
        tier=real_tier,
        user=user,
        context=body.context or "",
        request=request,
    )

# ═══════════════════════════════════════════════════════════════════════════
# File helpers
# ═══════════════════════════════════════════════════════════════════════════
def is_allowed_file(workspace: str, filename: str, content_type: str) -> bool:
    # Core workspace accepts all valid file types including code files
    if workspace == "core" or workspace is None:
        allowed_types = ("image/", "text/", "application/javascript", "application/json",
                         "application/xhtml+xml", "application/xml", "text/x-", "text/x-python", "application/x-python-code",
                         "application/pdf", "text/csv", "text/plain",
                         "application/vnd.ms-excel",
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         "application/msword",
                         "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        exts = (".csv", ".xls", ".xlsx", ".pdf", ".png", ".jpg", ".jpeg",
                ".gif", ".bmp", ".txt", ".doc", ".docx", ".html", ".htm", ".css", ".js", ".ts", ".jsx", ".tsx", ".vue", ".svelte",
                ".py", ".ipynb", ".java", ".c", ".cpp", ".h", ".hpp", ".go", ".rs",
                ".rb", ".php", ".swift", ".kt", ".scala", ".hs", ".lua", ".pl", ".r",
                ".sh", ".bash", ".zsh", ".json", ".yaml", ".yml", ".toml", ".ini",
                ".md", ".markdown", ".xml", ".svg", ".wasm", ".webp")
        return content_type.startswith(allowed_types) or filename.lower().endswith(exts)
    if workspace == "data":
        allowed_types = ("image/", "application/pdf", "text/csv", "text/plain",
                         "application/vnd.ms-excel",
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         "application/msword",
                         "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        exts = (".csv", ".xls", ".xlsx", ".pdf", ".png", ".jpg", ".jpeg",
                ".gif", ".bmp", ".txt", ".doc", ".docx")
        return content_type.startswith(allowed_types) or filename.lower().endswith(exts)
    if workspace == "design":
        allowed_types = ("image/", "text/", "application/javascript", "application/json",
                         "application/xhtml+xml", "application/xml", "text/x-", "text/x-python", "application/x-python-code")
        exts = (".html", ".htm", ".css", ".js", ".ts", ".jsx", ".tsx", ".vue", ".svelte",
                ".py", ".ipynb", ".java", ".c", ".cpp", ".h", ".hpp", ".go", ".rs",
                ".rb", ".php", ".swift", ".kt", ".scala", ".hs", ".lua", ".pl", ".r",
                ".sh", ".bash", ".zsh", ".json", ".yaml", ".yml", ".toml", ".ini",
                ".md", ".markdown", ".txt", ".xml", ".svg", ".wasm",
                ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
        return content_type.startswith(allowed_types) or filename.lower().endswith(exts)
    # Fallback for any other workspace - still accept all common file types
    allowed_types = ("image/", "text/", "application/javascript", "application/json",
                     "application/xhtml+xml", "application/xml", "text/x-", "text/x-python", "application/x-python-code",
                     "application/pdf", "text/csv", "text/plain")
    exts = (".csv", ".xls", ".xlsx", ".pdf", ".png", ".jpg", ".jpeg",
            ".gif", ".bmp", ".txt", ".doc", ".docx", ".html", ".htm", ".css", ".js", ".ts", ".jsx", ".tsx", ".vue", ".svelte",
            ".py", ".ipynb", ".java", ".c", ".cpp", ".h", ".hpp", ".go", ".rs",
            ".rb", ".php", ".swift", ".kt", ".scala", ".hs", ".lua", ".pl", ".r",
            ".sh", ".bash", ".zsh", ".json", ".yaml", ".yml", ".toml", ".ini",
            ".md", ".markdown", ".xml", ".svg", ".wasm", ".webp")
    return content_type.startswith(allowed_types) or filename.lower().endswith(exts)
# ═══════════════════════════════════════════════════════════════════════════
# Shared upload-limit enforcement (used by /api/extract and
# /api/extract_stream so the two paths cannot drift).
# ═══════════════════════════════════════════════════════════════════════════
_MAX_FILES        = 5
_MAX_FILE_BYTES   = 10 * 1024 * 1024
_MAX_TOTAL_BYTES  = 50 * 1024 * 1024
def _validate_uploads(workspace: str, files: list[UploadFile]) -> list[UploadFile]:
    """
    Enforces: file count, per-file MIME/extension, per-file size, total size.
    Raises HTTPException(400) with a stable shape. Returns the validated list.
    """
    if len(files) > _MAX_FILES:
        raise HTTPException(status_code=400,
                            detail=f"Too many files (max {_MAX_FILES})")

    valid: list[UploadFile] = []
    rejected: list[str] = []
    for f in files:
        if is_allowed_file(workspace, f.filename, f.content_type or ""):
            valid.append(f)
        else:
            rejected.append(f.filename)
    if rejected:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type(s) for {workspace}: {', '.join(rejected)}",
        )

    total = 0
    for f in valid:
        size = f.size or 0
        if size > _MAX_FILE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"File {f.filename} exceeds "
                       f"{_MAX_FILE_BYTES // (1024 * 1024)}MB",
            )
        total += size
    if total > _MAX_TOTAL_BYTES:
        raise HTTPException(status_code=400, detail="Total upload size too large")
    return valid

async def discover_schema(files: list[dict]) -> str | None:
    try:
        import openpyxl
    except ImportError:
        openpyxl = None
    for f in files:
        fn = f.get("filename", "").lower()
        mt = f.get("mimetype", "").lower()
        b64 = f.get("content_base64", "")
        if not b64:
            continue
        if fn.endswith(".csv") or "csv" in mt:
            try:
                content = base64.b64decode(b64).decode("utf-8", errors="ignore")
                reader = csv.reader(io.StringIO(content))
                headers = next(reader, [])
                if headers:
                    return f"CSV columns: {', '.join(headers)}"
            except Exception as e:
                logger.warning("csv_schema_discovery_failed error=%s", e)
        elif fn.endswith((".xls", ".xlsx")) or "spreadsheet" in mt:
            if openpyxl is None:
                continue
            try:
                content = base64.b64decode(b64)
                with io.BytesIO(content) as buf:
                    wb = openpyxl.load_workbook(buf, read_only=True)
                    sheet = wb.active
                    first_row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), None)
                    if first_row:
                        headers = [str(c) for c in first_row if c is not None]
                        return f"Excel columns: {', '.join(headers)}"
                    wb.close()
            except Exception as e:
                logger.warning("excel_schema_discovery_failed error=%s", e)
    return None


def generate_dependencies(code: str, language: str) -> str | None:
    if language == "python":
        imports = re.findall(r"^(?:from|import)\s+(\w+)", code, re.MULTILINE)
        return "\n".join(sorted(set(imports))) if imports else None
    if language in ("javascript", "typescript"):
        imports = re.findall(r'(?:require|import)\s*\(?\s*["\']([^"\']+)["\']', code)
        deps = {pkg: "*" for pkg in imports if not pkg.startswith(".")}
        return json.dumps({"dependencies": deps}, indent=2) if deps else None
    return None


_STOP_WORDS = {
    "the", "be", "to", "of", "and", "a", "in", "that", "have", "i", "it",
    "for", "not", "on", "with", "he", "as", "you", "do", "at", "this",
    "but", "his", "by", "from", "they", "we", "say", "her", "she", "or",
    "an", "will", "my", "one", "all", "would", "there", "their", "what",
    "so", "up", "out", "if", "about", "who", "get", "which", "go", "me",
}


def generate_chat_name(command: str, files: list[UploadFile] | None) -> str:
    if files:
        base = files[0].filename.split(".")[0]
        return base.replace("_", " ").replace("-", " ")[:50] or "File Chat"
    if command and command.strip():
        words = command.strip().split()
        meaningful = [w for w in words if w.lower() not in _STOP_WORDS and len(w) > 2]
        if meaningful:
            return " ".join(meaningful[:3])[:60]
        return " ".join(words[:3])[:60]
    return f"Chat_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"


# ═══════════════════════════════════════════════════════════════════════════
# Session persistence
# ═══════════════════════════════════════════════════════════════════════════
async def _persist_chat_session(
    user: dict, session_id: str | None, workspace: str,
    command: str, full_response: str, file_contents: list[dict],
    project_id: str | None = None,
) -> None:
    if not state.db_available:
        return
    ObjectId = get_object_id()
    try:
        new_user_msg = {
            "role": "user", "text": command,
            "attachedFiles": [f["filename"] for f in file_contents],
            "createdAt": datetime.now(timezone.utc),
        }
        new_model_msg = {
            "role": "model", "text": full_response.strip(),
            "variants": [full_response.strip()], "activeVariant": 0,
            "canRegenerate": True, "createdAt": datetime.now(timezone.utc),
        }
        if session_id and ObjectId and ObjectId.is_valid(session_id):
            await state.sessions_col.update_one(
                {"_id": ObjectId(session_id), "userId": user["_id"]},
                {"$push": {"messages": {"$each": [new_user_msg, new_model_msg]}}},
            )
            return
        filename = generate_chat_name(command, file_contents)
        new_session = {
            "userId": user["_id"], "filename": filename,
            "workspace": workspace, "status": "active", "isPinned": False,
            "messages": [new_user_msg, new_model_msg],
            "createdAt": datetime.now(timezone.utc),
        }
        if project_id and ObjectId and ObjectId.is_valid(project_id):
            new_session["projectId"] = ObjectId(project_id)
        await state.sessions_col.insert_one(new_session)
    except Exception as e:
        logger.error("chat_session_persist_failed error=%s", e)

# ═══════════════════════════════════════════════════════════════════════════
# Streaming extraction
# ═══════════════════════════════════════════════════════════════════════════
@router.put("/api/user/default-persona")
async def set_default_persona(payload: dict, user: dict = Depends(get_current_user)):
    pid = payload.get("default_persona_id")
    await state.users_col.update_one(
        {"_id": user["_id"]},
        {"$set": {"default_persona_id": pid}},
    )
    return {"success": True, "default_persona_id": pid}
@router.post("/api/extract_stream", response_model=None)
@limiter.limit("5/minute")
async def extract_stream(
    request: Request,
    user: dict = Depends(get_current_user),
    command: str = Form(...),
    workspace: str | None = Form(None),
    task_type: str | None = Form(None),
    sessionId: str | None = Form(None),
    projectId: str | None = Form(None),
    context: str | None = Form(None),
    files: list[UploadFile] = File(default=[]),
):

    allowed, reset_sec = await check_rate_limit(
        str(user["_id"]), user.get("tier", "free"), "extract_stream")
    if not allowed:
        raise HTTPException(status_code=429, detail={
            "code": "QUOTA_EXCEEDED",
            "message": f"Rate limit exceeded. Try again in {reset_sec} seconds.",
            "reset": reset_sec})
    if not state.db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")

    file_infos = [{"filename": f.filename, "mimetype": f.content_type or ""} for f in files]
    workspace = workspace or detect_workspace(command, file_infos)
    if workspace not in ("data", "design", "core"):
        workspace = "core"
    files = _validate_uploads(workspace, files)
    valid_files = files
    file_contents = []
    for f in valid_files:
        content_bytes = await f.read()
        file_contents.append({
            "filename": f.filename,
            "mimetype": f.content_type or "application/octet-stream",
            "content_base64": base64.b64encode(content_bytes).decode("utf-8"),
        })

    if task_type is None:
        task_type = {"data": "extraction", "design": "frontend"}.get(workspace, "structuring")
    if task_type not in ("extraction", "frontend", "structuring", "touch_fix"):
        task_type = "extraction" if workspace == "data" else "frontend"

    ObjectId = get_object_id()
    if sessionId and (not ObjectId or not ObjectId.is_valid(sessionId)):
        sessionId = None
    history = []
    if sessionId and ObjectId:
        session = await state.sessions_col.find_one(
            {"_id": ObjectId(sessionId), "userId": user["_id"]})
        if session:
            history = session.get("messages", [])

    combined_context = context or ""
    if state.context_registry and state.db_available:
        with contextlib.suppress(Exception):
            reg_context = await state.context_registry.get_context(user["_id"], workspace) or ""
            combined_context += f"\n{reg_context}"

    schema_info = await discover_schema(file_contents)
    if schema_info:
        combined_context += f"\nSchema info: {schema_info}\n"

    llm_config = WORKSPACE_LLM_CONFIG.get(workspace, WORKSPACE_LLM_CONFIG["core"])
    max_tokens = llm_config["max_tokens"]
    temp = llm_config["temperature"]
    full_response = ""

    # If command is empty but files are present, create a default command
    if not command and files:
        command = f"Analyze the uploaded file: {files[0].filename}"

    async def event_generator():
        nonlocal full_response
        sanitized = sanitize_input(command)
        async for event in stream_ai_response(
            workspace=workspace, task_type=task_type, prompt=sanitized,
            history=history, files=file_contents,
            max_tokens=max_tokens, temp=temp,
            tier=user.get("tier", "free"), user=user, context=combined_context,
        ):
            if "data: " in event and '"text":' in event:
                with contextlib.suppress(Exception):
                    payload = json.loads(event.split("data: ", 1)[1].strip())
                    if "text" in payload:
                        full_response += payload["text"]
            yield event

        await _persist_chat_session(
            user=user, session_id=sessionId, workspace=workspace,
            command=command, full_response=full_response,
            file_contents=file_contents, project_id=projectId,
        )
        yield "event: close\ndata: {}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ═══════════════════════════════════════════════════════════════════════════
# Non-streaming extraction
# ═══════════════════════════════════════════════════════════════════════════
@router.post("/api/extract", response_model=None)
@limiter.limit("100/minute")
async def extract(
    request: Request,
    user: dict = Depends(get_current_user),      # FIX B5 — auth restored
    command: str = Form(...),
    workspace: str | None = Form(None),
    task_type: str | None = Form(None),
    isRetry: str = Form("false"),
    sessionId: str | None = Form(None),
    projectId: str | None = Form(None),
    files: list[UploadFile] = File(default=[]),
):
    # FIX B5 — the previous hardcoded `test_user_id` object has been
    # removed. `user` now comes from `get_current_user` above.

    # If command is empty but files are present, create a default command
    if not command and files:
        command = f"Analyze the uploaded file: {files[0].filename}"

    allowed, reset_sec = await check_rate_limit(
        str(user["_id"]), user.get("tier", "free"), "extract")
    if not allowed:
        raise HTTPException(status_code=429, detail={
            "code": "QUOTA_EXCEEDED",
            "message": f"Rate limit exceeded. Try again in {reset_sec} seconds.",
            "reset": reset_sec})
    if not state.db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")

    file_infos = [{"filename": f.filename, "mimetype": f.content_type or ""} for f in files]
    workspace = workspace or detect_workspace(command, file_infos)
    if workspace not in ("data", "design", "core"):
        workspace = "core"
    files = _validate_uploads(workspace, files)
    total_size = sum((f.size or 0) for f in files)
    tier = user.get("tier", "free")
    is_design = workspace == "design"

    if tier == "free":
        data_limit, ui_limit = 5, 3
    elif tier == "pro":
        has_data = user.get("subTierOptions", {}).get("hasDataAccess", False)
        has_design = user.get("subTierOptions", {}).get("hasDesignAccess", False)
        if has_data and has_design: data_limit, ui_limit = 20, 15
        elif has_data: data_limit, ui_limit = 19, 0
        elif has_design: data_limit, ui_limit = 0, 13
        else: data_limit, ui_limit = 0, 0
    elif tier == "business":
        has_data = user.get("subTierOptions", {}).get("hasDataAccess", False)
        has_design = user.get("subTierOptions", {}).get("hasDesignAccess", False)
        if has_data and has_design: data_limit, ui_limit = 30, 25
        elif has_data: data_limit, ui_limit = 28, 0
        elif has_design: data_limit, ui_limit = 0, 20
        else: data_limit, ui_limit = 0, 0
    else:
        data_limit, ui_limit = 5, 0

    limit = ui_limit if is_design else data_limit
    quota_field = "quotas.dailyGenerationsUsed" if is_design else "quotas.dailyExtractionsUsed"
    parts = quota_field.split(".")
    current = user.get(parts[0], {}).get(parts[1], 0)
    if current > limit:
        await state.users_col.update_one({"_id": user["_id"]}, {"$set": {quota_field: limit}})
        current = limit
    if current >= limit:
        raise HTTPException(status_code=403,
                            detail={"code": "LIMIT_REACHED", "usage": current, "limit": limit})

    file_contents = []
    for f in files:
        content_bytes = await f.read()
        file_contents.append({
            "filename": f.filename,
            "mimetype": f.content_type or "application/octet-stream",
            "content_base64": base64.b64encode(content_bytes).decode("utf-8"),
        })

    if task_type is None:
        task_type = {"data": "extraction", "design": "frontend"}.get(workspace, "structuring")
    if task_type not in ("extraction", "frontend", "structuring", "touch_fix"):
        task_type = "extraction" if workspace == "data" else "frontend"

    ObjectId = get_object_id()
    if sessionId and (not ObjectId or not ObjectId.is_valid(sessionId)):
        sessionId = None

    current_session = None
    history = []
    if sessionId and ObjectId:
        current_session = await state.sessions_col.find_one(
            {"_id": ObjectId(sessionId), "userId": user["_id"]})
        if current_session:
            history = current_session.get("messages", [])
            if isRetry == "true" and history and history[-1].get("role") == "model":
                history = history[:-2]

    if state.intent_classifier:
        with contextlib.suppress(Exception):
            intent_result = await state.intent_classifier.classify(command, file_contents)
            new_ws = getattr(intent_result, "workspace", None)
            if new_ws in ("data", "design", "core"):
                workspace = new_ws

    context_str = ""
    if state.context_registry and state.db_available:
        with contextlib.suppress(Exception):
            context_str = await state.context_registry.get_context(user["_id"], workspace) or ""

    schema_info = await discover_schema(file_contents)
    if schema_info:
        context_str += f"\nSchema info: {schema_info}\n"

    llm_config = WORKSPACE_LLM_CONFIG.get(workspace, WORKSPACE_LLM_CONFIG["core"])
    max_tokens = llm_config["max_tokens"]
    temp = llm_config["temperature"]

    ai_result = await generate_with_cascade(
        workspace=workspace, task_type=task_type, prompt=command,
        history=history, files=file_contents,
        max_tokens=max_tokens, temp=temp, tier=tier,
        user=user, context=context_str,
    )
    if not ai_result.get("success"):
        raise HTTPException(status_code=503, detail="AI service unavailable")

    ai_text = ai_result["text"]
    provider = ai_result.get("provider")
    model_used = ai_result.get("model_used")

    is_code = ("```" in ai_text) or any(ext in ai_text for ext in (".py", ".js", ".html", ".css"))
    if state.critic_agent and is_code and state.code_guard is not None:
        with contextlib.suppress(Exception):
            language = "python" if workspace == "data" else "javascript"
            scan = await state.code_guard.scan(ai_text, language=language)
            if not (scan.syntax_ok and scan.score >= 60) and state.self_healer:
                heal = await state.self_healer.heal(
                    ai_text,
                    error=(scan.findings[0].message if scan.findings else "unknown"),
                    language=language, tier=tier, user=user)
                if heal.success:
                    ai_text = heal.final_code

    if is_code:
        deps = generate_dependencies(ai_text, "python" if workspace == "data" else "javascript")
        if deps:
            ai_text += f"\n\n**Generated Dependencies:**\n```\n{deps}\n```"

    structured: list = []
    json_match = re.search(r"\[JSON-DATA\](.*?)\[/JSON-DATA\]", ai_text, re.DOTALL)
    if json_match:
        with contextlib.suppress(Exception):
            structured = json.loads(json_match.group(1).strip())
        ai_text = re.sub(r"\[JSON-DATA\].*?\[/JSON-DATA\]", "", ai_text, flags=re.DOTALL).strip()

    if not ai_text:
        ai_text = "I am Axelr AI. How can I help you?"

    if provider != "local":
        prompt_tokens = estimate_tokens(command) + (estimate_tokens(str(history)) if history else 0)
        completion_tokens = estimate_tokens(ai_text)
        update_query: dict[str, Any] = {
            "$inc": {
                "tokenUsage.totalPromptTokens": prompt_tokens,
                "tokenUsage.totalCompletionTokens": completion_tokens,
                "tokenUsage.dailyPromptTokens": prompt_tokens,
                "tokenUsage.dailyCompletionTokens": completion_tokens,
                "storageBytesUsed": total_size,
            },
            "$set": {"lastUsageDate": datetime.now(timezone.utc)},
        }
        provider_map = {
            "gemini": "dailyGeminiQuota", "groq": "dailyGroqQuota",
            "cloudflare": "dailyCloudflareQuota", "openrouter": "dailyOpenRouterQuota",
            "mistral": "dailyMistralQuota", "huggingface": "dailyHuggingFaceQuota",
            "github_models": "dailyGithubQuota", "nrouter": "dailyNrouterQuota",
            "text_cortex": "dailyTextCortexQuota",
        }
        if provider in provider_map:
            update_query["$inc"][provider_map[provider]] = 1
        await state.users_col.update_one({"_id": user["_id"]}, update_query)

    session_id_out = None
    filename_out = "Export"
    if current_session:
        if isRetry == "true" and current_session.get("messages"):
            last_msg = current_session["messages"][-1]
            if last_msg.get("role") == "model":
                variants = last_msg.get("variants") or [last_msg.get("text", "")]
                variants.append(ai_text)
                last_msg["variants"] = variants
                last_msg["activeVariant"] = len(variants) - 1
                last_msg["text"] = ai_text
                await state.sessions_col.update_one(
                    {"_id": ObjectId(sessionId)},
                    {"$set": {"messages": current_session["messages"],
                              "structuredData": structured}})
            else:
                raise HTTPException(status_code=400, detail="Cannot retry")
        else:
            current_session["messages"].append({
                "role": "user", "text": command,
                "attachedFiles": [f.filename for f in files]})
            current_session["messages"].append({
                "role": "model", "text": ai_text,
                "variants": [ai_text], "activeVariant": 0,
                "canRegenerate": True, "createdAt": datetime.now(timezone.utc)})
            await state.sessions_col.update_one(
                {"_id": ObjectId(sessionId)},
                {"$set": {"messages": current_session["messages"],
                          "structuredData": structured}})
        session_id_out = sessionId
        filename_out = current_session.get("filename", "Export")
    else:
        filename = generate_chat_name(command, files)
        new_session = {
            "userId": user["_id"], "filename": filename,
            "workspace": workspace, "status": "active", "isPinned": False,
            "messages": [
                {"role": "user", "text": command,
                 "attachedFiles": [f.filename for f in files],
                 "createdAt": datetime.now(timezone.utc)},
                {"role": "model", "text": ai_text,
                 "variants": [ai_text], "activeVariant": 0,
                 "canRegenerate": True, "createdAt": datetime.now(timezone.utc)},
            ],
            "structuredData": structured,
            "createdAt": datetime.now(timezone.utc),
        }
        if projectId and ObjectId and ObjectId.is_valid(projectId):
            new_session["projectId"] = ObjectId(projectId)
        result = await state.sessions_col.insert_one(new_session)
        session_id_out = str(result.inserted_id)
        filename_out = filename

    return {
        "success": True, "text": ai_text,
        "sessionId": session_id_out, "structuredData": structured,
        "filename": f"{filename_out}.csv",
        "provider": provider, "model": model_used,
    }


__all__ = [
    "_persist_chat_session",
    "discover_schema",
    "generate_chat_name",
    "generate_dependencies",
    "is_allowed_file",
    "router",
]