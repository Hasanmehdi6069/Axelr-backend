# api/routes_admin.py
"""AXELR — Admin, user profile, history, reports.
==============================================
Admin metrics · provider diagnostics · user profile/preferences ·
history CRUD · reports · PR report reader · debug endpoints.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import get_current_user
from .config import ADMIN_EMAIL
from .middleware import validate_all_providers
from .providers import (
    PROVIDER_CHAIN,
    PROVIDER_FUNC_MAP,
    PROVIDER_KEY_CHECK,
    PROVIDER_MODELS,
)
from .state import get_object_id, state

logger = logging.getLogger("axelr.admin")
router = APIRouter(tags=["admin"])

def _require_admin(user: dict) -> None:
    # User documents store `is_admin` (snake_case) per api.auth._new_user_doc.
    # This guard previously read `isAdmin` and rejected every request.
    if not (user.get("is_admin") or user.get("isAdmin")):
        raise HTTPException(status_code=403, detail="Admin only")


def _is_super_admin(user: dict) -> bool:
    return bool(user.get("is_admin") or user.get("isAdmin")) and \
        user.get("email") == ADMIN_EMAIL

# ═══════════════════════════════════════════════════════════════════════════
# Debug
# ═══════════════════════════════════════════════════════════════════════════
@router.get("/api/debug/env")
async def debug_env(user: dict = Depends(get_current_user)):
    _require_admin(user)
    return {
        "MONGO_URI": bool(state.db_available),
        "GOOGLE_CLIENT_ID": True,
        "db_available": state.db_available,
        "redis_available": bool(state.redis_client),
        "uptime": time.time() - (getattr(state, "start_time", None) or time.time()),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Provider diagnostics
# ═══════════════════════════════════════════════════════════════════════════
async def _probe_provider(name: str, func, prompt: str, model: str | None) -> dict:
    try:
        start = time.time()
        resp = await func(prompt, 5, 0.0, model)
        latency = (time.time() - start) * 1000
        if resp and resp.strip():
            return {"status": "healthy", "latency_ms": round(latency, 2)}
        return {"status": "unhealthy", "response": (resp or "")[:50] or "empty"}
    except Exception as e:
        return {"status": "error", "error": str(e)[:100]}


@router.get("/api/v1/diagnose")
async def diagnose_providers(user: dict = Depends(get_current_user)):
    _require_admin(user)
    results: dict = {}
    tasks: dict = {}
    for provider_name, func in PROVIDER_CHAIN:
        if provider_name == "local":
            continue
        if not PROVIDER_KEY_CHECK.get(provider_name, False):
            results[provider_name] = {"status": "skipped", "reason": "Not configured"}
            continue
        models = PROVIDER_MODELS.get(provider_name, [])
        if not models:
            results[provider_name] = {"status": "skipped", "reason": "No models"}
            continue
        tasks[provider_name] = asyncio.create_task(
            _probe_provider(provider_name, func, "Say 'OK'", models[0]),
        )
    for name, task in tasks.items():
        try:
            results[name] = await task
        except Exception as e:
            results[name] = {"status": "error", "error": str(e)[:100]}
    return {"providers": results}


@router.post("/api/admin/validate-provider")
async def validate_provider(provider_name: str, user: dict = Depends(get_current_user)):
    _require_admin(user)
    provider_func = PROVIDER_FUNC_MAP.get(provider_name)
    if not provider_func:
        raise HTTPException(400, "Unknown provider")
    if not PROVIDER_KEY_CHECK.get(provider_name, False):
        return {"status": "skipped", "reason": "Provider not configured"}
    models = PROVIDER_MODELS.get(provider_name, [])
    if not models:
        return {"status": "error", "reason": "No model configured"}
    try:
        start = time.time()
        resp = await asyncio.wait_for(
            provider_func("Say 'OK'", 5, 0.0, models[0]), timeout=5.0,
        )
        latency = (time.time() - start) * 1000
        if resp and resp.strip():
            return {"status": "healthy", "latency_ms": round(latency, 2),
                    "response_preview": resp[:100]}
        return {"status": "unhealthy", "response": (resp or "")[:50]}
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}


@router.get("/api/admin/provider-health")
async def provider_health_endpoint(force: bool = False,
                                    user: dict = Depends(get_current_user)):
    _require_admin(user)
    return {"status": "ok", "providers": await validate_all_providers(force=force)}


# ═══════════════════════════════════════════════════════════════════════════
# Health (public, detailed)
# ═══════════════════════════════════════════════════════════════════════════
@router.get("/api/health/detailed")
async def health_detailed():
    provider_status = {}
    for name in PROVIDER_CHAIN:
        name = name[0] if isinstance(name, tuple) else name
        is_available = True
        if state.circuit_breaker is not None:
            try:
                is_available = await state.circuit_breaker.is_available(name)
            except Exception:
                pass
        provider_status[name] = {
            "status": "available" if is_available else "circuit_open",
        }
    qstash_status = "configured" if (state.redis_client and True) else "disabled"
    return {
        "status": "operational" if state.db_available else "degraded",
        "timestamp": datetime.utcnow().isoformat(),
        "db": "connected" if state.db_available else "disconnected",
        "redis": "connected" if state.redis_client else "disabled",
        "qstash": qstash_status,
        "providers": provider_status,
        "uptime": time.time() - (getattr(state, "start_time", None) or time.time()),
    }


# ═══════════════════════════════════════════════════════════════════════════
# User profile & preferences
# ═══════════════════════════════════════════════════════════════════════════
@router.get("/api/user/profile")
async def get_profile(user: dict = Depends(get_current_user)):
    if not state.db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return {
        "tier": user.get("tier"),
        "dailyUsage": user.get("dailyUsage", 0),
        "dailyUiUxUsage": user.get("dailyUiUxUsage", 0),
        "customInstructions": user.get("customInstructions", ""),
        "quotas": user.get("quotas", {}),
        "subTierOptions": user.get("subTierOptions", {}),
        "tokenUsage": {
            "dailyPrompt": user.get("tokenUsage", {}).get("dailyPromptTokens", 0),
            "dailyCompletion": user.get("tokenUsage", {}).get("dailyCompletionTokens", 0),
            "totalPrompt": user.get("tokenUsage", {}).get("totalPromptTokens", 0),
            "totalCompletion": user.get("tokenUsage", {}).get("totalCompletionTokens", 0),
        },
        "isAdmin": user.get("isAdmin", False),
        "email": user.get("email"),
        "stripeCustomerId": user.get("stripeCustomerId"),
        "stripeSubscriptionId": user.get("stripeSubscriptionId"),
        "puter_enabled": user.get("puter_enabled", False),
        "preferences": user.get("preferences", {}),
    }


class InstructionsUpdate(BaseModel):
    instructions: str


@router.put("/api/user/instructions")
async def update_instructions(data: InstructionsUpdate,
                               user: dict = Depends(get_current_user)):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    await state.users_col.update_one(
        {"_id": user["_id"]},
        {"$set": {"customInstructions": data.instructions[:5000]}},
    )
    return {"success": True}


@router.delete("/api/user/delete")
async def delete_account(user: dict = Depends(get_current_user)):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    uid = user["_id"]
    await state.sessions_col.delete_many({"userId": uid})
    await state.reports_col.delete_many({"userId": uid})
    if state.pr_reports_col is not None:
        await state.pr_reports_col.delete_many({"userId": uid})
    await state.users_col.delete_one({"_id": uid})
    return {"success": True}


@router.get("/api/user/preferences")
async def get_preferences(user: dict = Depends(get_current_user)):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    prefs = user.get("preferences", {})
    return {"defaultWorkspace": prefs.get("defaultWorkspace", "data")}


class PreferencesUpdate(BaseModel):
    defaultWorkspace: str


@router.put("/api/user/preferences")
async def update_preferences(data: PreferencesUpdate,
                              user: dict = Depends(get_current_user)):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    if data.defaultWorkspace not in ("data", "design", "core"):
        raise HTTPException(400, "Invalid workspace")
    await state.users_col.update_one(
        {"_id": user["_id"]},
        {"$set": {"preferences.defaultWorkspace": data.defaultWorkspace}},
    )
    return {"success": True, "defaultWorkspace": data.defaultWorkspace}


class PuterToggle(BaseModel):
    enabled: bool


@router.post("/api/user/puter-toggle")
async def toggle_puter(data: PuterToggle, user: dict = Depends(get_current_user)):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    await state.users_col.update_one(
        {"_id": user["_id"]}, {"$set": {"puter_enabled": data.enabled}},
    )
    return {"success": True, "puter_enabled": data.enabled}


# ═══════════════════════════════════════════════════════════════════════════
# History
# ═══════════════════════════════════════════════════════════════════════════
class RenamePayload(BaseModel):
    action: str
    payload: str | None = None


class StatusUpdate(BaseModel):
    status: str


class VariantUpdate(BaseModel):
    msgId: str
    variantIndex: int


@router.delete("/api/history/delete-all")
async def delete_all_chats(user: dict = Depends(get_current_user)):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    await state.sessions_col.delete_many({"userId": user["_id"]})
    return {"success": True}


@router.get("/api/history")
async def list_history(workspace: str = "data", status: str = "active",
                        page: int = 1, limit: int = 20,
                        user: dict = Depends(get_current_user)):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    if workspace not in ("data", "design", "core"):
        workspace = "data"
    if status not in ("active", "archived", "trashed"):
        status = "active"
    skip = (page - 1) * limit
    query = {"userId": user["_id"], "status": status, "workspace": workspace}
    total = await state.sessions_col.count_documents(query)
    cursor = (state.sessions_col.find(query)
              .sort([("isPinned", -1), ("createdAt", -1)])
              .skip(skip).limit(limit))
    logs = await cursor.to_list(length=limit)
    for log in logs:
        log["_id"] = str(log["_id"])
        log["userId"] = str(log["userId"])
        for msg in log.get("messages", []):
            if "_id" in msg:
                msg["_id"] = str(msg["_id"])
    return {
        "success": True,
        "logs": logs,
        "pagination": {
            "page": page, "limit": limit, "total": total,
            "pages": (total + limit - 1) // limit,
        },
    }


@router.put("/api/history/{history_id}")
async def update_history(history_id: str, data: RenamePayload,
                          user: dict = Depends(get_current_user)):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    ObjectId = get_object_id()
    if not ObjectId or not ObjectId.is_valid(history_id):
        raise HTTPException(400, "Invalid ID")
    session = await state.sessions_col.find_one(
        {"_id": ObjectId(history_id), "userId": user["_id"]},
    )
    if not session:
        raise HTTPException(404, "Not found")
    if data.action == "rename" and data.payload:
        await state.sessions_col.update_one(
            {"_id": ObjectId(history_id)},
            {"$set": {"filename": data.payload[:100]}},
        )
    elif data.action == "pin":
        current = session.get("isPinned", False)
        await state.sessions_col.update_one(
            {"_id": ObjectId(history_id)},
            {"$set": {"isPinned": not current}},
        )
    else:
        raise HTTPException(400, "Invalid action")
    return {"success": True}


@router.put("/api/history/{history_id}/status")
async def update_status(history_id: str, data: StatusUpdate,
                         user: dict = Depends(get_current_user)):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    ObjectId = get_object_id()
    if not ObjectId or not ObjectId.is_valid(history_id):
        raise HTTPException(400, "Invalid ID")
    if data.status not in ("active", "archived", "trashed"):
        raise HTTPException(400, "Invalid status")
    update: dict[str, Any] = {"status": data.status}
    if data.status == "trashed":
        update["trashedAt"] = datetime.utcnow()
    result = await state.sessions_col.update_one(
        {"_id": ObjectId(history_id), "userId": user["_id"]},
        {"$set": update},
    )
    if result.modified_count == 0:
        raise HTTPException(404, "Not found")
    return {"success": True}


@router.delete("/api/history/{history_id}")
async def delete_history(history_id: str, user: dict = Depends(get_current_user)):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    ObjectId = get_object_id()
    if not ObjectId or not ObjectId.is_valid(history_id):
        raise HTTPException(400, "Invalid ID")
    result = await state.sessions_col.delete_one(
        {"_id": ObjectId(history_id), "userId": user["_id"], "status": "trashed"},
    )
    if result.deleted_count == 0:
        raise HTTPException(404, "Not found or not trashed")
    return {"success": True}


@router.put("/api/history/{history_id}/variant")
async def switch_variant(history_id: str, data: VariantUpdate,
                          user: dict = Depends(get_current_user)):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    ObjectId = get_object_id()
    if not ObjectId or not ObjectId.is_valid(history_id):
        raise HTTPException(400, "Invalid ID")
    session = await state.sessions_col.find_one(
        {"_id": ObjectId(history_id), "userId": user["_id"]},
    )
    if not session:
        raise HTTPException(404, "Not found")
    messages = session.get("messages", [])
    msg_index = next((i for i, m in enumerate(messages)
                      if str(m.get("_id")) == data.msgId), -1)
    if msg_index == -1:
        raise HTTPException(404, "Message not found")
    msg = messages[msg_index]
    variants = msg.get("variants", [])
    if data.variantIndex < 0 or data.variantIndex >= len(variants):
        raise HTTPException(400, "Invalid variant index")
    msg["activeVariant"] = data.variantIndex
    msg["text"] = variants[data.variantIndex]
    await state.sessions_col.update_one(
        {"_id": ObjectId(history_id)}, {"$set": {"messages": messages}},
    )
    return {"success": True}


# ═══════════════════════════════════════════════════════════════════════════
# Reports
# ═══════════════════════════════════════════════════════════════════════════
class ReportCreate(BaseModel):
    type: str = "feedback"
    description: str


@router.post("/api/reports")
async def create_report(data: ReportCreate, user: dict = Depends(get_current_user)):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    report = {
        "userId": user["_id"], "type": data.type,
        "description": data.description[:5000],
        "createdAt": datetime.utcnow(),
    }
    await state.reports_col.insert_one(report)
    return {"success": True}


__all__ = ["router"]