
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
from .lifecycle import validate_all_providers
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
# Fraud & ban management (admin only)
# ═══════════════════════════════════════════════════════════════════════════
@router.get("/api/admin/fraud/fingerprints")
async def list_banned_fingerprints(user: dict = Depends(get_current_user)):
    _require_admin(user)
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    banned = await state.users_col.find({"banned": True}).to_list(length=100)
    return {
        "banned": [{
            "email": u.get("email"),
            "fingerprint": u.get("fingerprint"),
            "ip": u.get("last_ip"),
            "banned_at": u.get("banned_at")
        } for u in banned]
    }


@router.get("/api/admin/fraud/banned")
async def list_banned_users(user: dict = Depends(get_current_user)):
    _require_admin(user)
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    banned = await state.users_col.find({"banned": True}).to_list(length=100)
    return {
        "users": [{
            "_id": str(u["_id"]),
            "email": u["email"],
            "fingerprint": u.get("fingerprint"),
            "banned_at": u.get("banned_at")
        } for u in banned]
    }


@router.post("/api/admin/fraud/ban/{fingerprint}")
async def ban_by_fingerprint(fingerprint: str, user: dict = Depends(get_current_user)):
    _require_admin(user)
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    result = await state.users_col.update_many(
        {"fingerprint": fingerprint},
        {"$set": {"banned": True, "banned_at": datetime.utcnow()}}
    )
    return {"success": True, "banned_users": result.modified_count}


@router.post("/api/admin/fraud/unban/{fingerprint}")
async def unban_by_fingerprint(fingerprint: str, user: dict = Depends(get_current_user)):
    _require_admin(user)
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    result = await state.users_col.update_many(
        {"fingerprint": fingerprint},
        {"$set": {"banned": False, "unbanned_at": datetime.utcnow()}}
    )
    return {"success": True, "unbanned_users": result.modified_count}


@router.get("/api/admin/fraud/ip/{ip}")
async def get_users_by_ip(ip: str, user: dict = Depends(get_current_user)):
    _require_admin(user)
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    users = await state.users_col.find({"last_ip": ip}).to_list(length=50)
    return {
        "ip": ip,
        "users": [{
            "_id": str(u["_id"]),
            "email": u["email"],
            "fingerprint": u.get("fingerprint"),
            "created_at": u.get("created_at")
        } for u in users]
    }


@router.get("/api/admin/fraud/metrics")
async def get_fraud_metrics(user: dict = Depends(get_current_user)):
    _require_admin(user)
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    total_banned = await state.users_col.count_documents({"banned": True})
    unique_fingerprints = len(await state.users_col.distinct("fingerprint", {"banned": True}))
    return {
        "total_banned_users": total_banned,
        "unique_banned_fingerprints": unique_fingerprints
    }


# ═══════════════════════════════════════════════════════════════════════════
# Tier management (admin only)
# ═══════════════════════════════════════════════════════════════════════════
class GrantTierRequest(BaseModel):
    user_id: str
    tier: str
    period: str = "monthly"


@router.post("/api/admin/users/grant-tier")
async def grant_tier_to_user(data: GrantTierRequest, user: dict = Depends(get_current_user)):
    _require_admin(user)
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    
    from .config import VALID_TIERS, VALID_PERIODS
    if data.tier not in VALID_TIERS or data.period not in VALID_PERIODS:
        raise HTTPException(400, "Invalid tier or period")
    
    ObjectId = get_object_id()
    if not ObjectId or not ObjectId.is_valid(data.user_id):
        raise HTTPException(400, "Invalid user ID")
    
    result = await state.users_col.update_one(
        {"_id": ObjectId(data.user_id)},
        {"$set": {"tier": data.tier, "billing_period": data.period, "tier_granted_at": datetime.utcnow()}}
    )
    if result.modified_count == 0:
        raise HTTPException(404, "User not found")
    return {"success": True, "tier": data.tier, "period": data.period}


class SwitchViewTierRequest(BaseModel):
    user_id: str
    view_tier: str


@router.post("/api/admin/users/switch-view-tier")
async def switch_user_view_tier(data: SwitchViewTierRequest, user: dict = Depends(get_current_user)):
    _require_admin(user)
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    
    ObjectId = get_object_id()
    if not ObjectId or not ObjectId.is_valid(data.user_id):
        raise HTTPException(400, "Invalid user ID")
    
    result = await state.users_col.update_one(
        {"_id": ObjectId(data.user_id)},
        {"$set": {"view_tier": data.view_tier}}
    )
    if result.modified_count == 0:
        raise HTTPException(404, "User not found")
    return {"success": True, "view_tier": data.view_tier}


@router.get("/api/admin/users/list")
async def list_all_users(page: int = 1, limit: int = 50, user: dict = Depends(get_current_user)):
    _require_admin(user)
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    
    skip = (page - 1) * limit
    total = await state.users_col.count_documents({})
    cursor = state.users_col.find({}).skip(skip).limit(limit).sort("createdAt", -1)
    users = await cursor.to_list(length=limit)
    
    return {
        "success": True,
        "users": [{
            "_id": str(u["_id"]),
            "email": u["email"],
            "tier": u.get("tier"),
            "is_admin": u.get("is_admin", False),
            "banned": u.get("banned", False),
            "created_at": u.get("createdAt"),
            "last_ip": u.get("last_ip")
        } for u in users],
        "pagination": {
            "page": page, "limit": limit, "total": total,
            "pages": (total + limit - 1) // limit
        }
    }


# ═══════════════════════════════════════════════════════════════════════════
# Founder tier seeding
# ═══════════════════════════════════════════════════════════════════════════
async def _seed_founder_tiers() -> None:
    """Seed founder tier to admin user on startup."""
    if not state.db_available or not state.users_col:
        logger.warning("founder_seed_skipped database not available")
        return
    
    admin_email = ADMIN_EMAIL
    if not admin_email:
        logger.warning("founder_seed_skipped ADMIN_EMAIL not configured")
        return
    
    # Update admin user to have founder tier if not already set
    result = await state.users_col.update_one(
        {"email": admin_email},
        {"$setOnInsert": {
            "email": admin_email,
            "tier": "founder",
            "is_admin": True,
            "createdAt": datetime.utcnow(),
            "founder_seeded": True
        }},
        upsert=True
    )
    if result.upserted_id:
        logger.info("founder_user_created email=%s", admin_email)
    elif result.modified_count > 0:
        logger.info("founder_tier_updated email=%s", admin_email)
    else:
        logger.debug("founder_user_already_exists email=%s", admin_email)


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


# ═══════════════════════════════════════════════════════════════════════════
# Tier management (restored)
# ═══════════════════════════════════════════════════════════════════════════
class GrantTierRequest(BaseModel):
    email: str
    tier: str
    sub_tier: str | None = "full"
    reason: str | None = None
    expires_at: str | None = None


class SwitchViewTierRequest(BaseModel):
    tier: str


_VALID_TIERS = ("free", "pro", "business", "enterprise")


@router.post("/api/admin/users/grant-tier")
async def grant_tier_to_user(data: GrantTierRequest,
                             user: dict = Depends(get_current_user)):
    _require_admin(user)
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    if data.tier not in _VALID_TIERS:
        raise HTTPException(400, f"Invalid tier; must be one of {_VALID_TIERS}")
    target = await state.users_col.find_one({"email": data.email})
    if not target:
        raise HTTPException(404, "User not found")
    update_data = {
        "tier": data.tier,
        "subTier": data.sub_tier or "full",
        "manually_provisioned": True,
        "provisioned_by": user["email"],
        "provisioned_at": datetime.utcnow().isoformat(),
        "provision_reason": data.reason or "Manual admin provisioning",
    }
    if data.expires_at:
        update_data["tier_expires_at"] = data.expires_at
    await state.users_col.update_one({"_id": target["_id"]}, {"$set": update_data})
    logger.info("admin_grant_tier admin=%s target=%s tier=%s",
                user["email"], data.email, data.tier)
    return {"success": True, "user": {"email": data.email, "tier": data.tier,
                                       "sub_tier": data.sub_tier}}


@router.post("/api/admin/switch-view-tier")
async def switch_view_tier(data: SwitchViewTierRequest,
                            user: dict = Depends(get_current_user)):
    _require_admin(user)
    if data.tier not in _VALID_TIERS:
        raise HTTPException(400, f"Invalid tier; must be one of {_VALID_TIERS}")
    await state.users_col.update_one(
        {"_id": user["_id"]},
        {"$set": {"preview_tier": data.tier,
                  "preview_tier_set_at": datetime.utcnow().isoformat()}},
    )
    return {"success": True, "preview_tier": data.tier}


@router.get("/api/admin/users/list")
async def list_managed_users(managed_only: bool = False,
                              user: dict = Depends(get_current_user)):
    _require_admin(user)
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    query = {"manually_provisioned": True} if managed_only else {}
    projection = {"email": 1, "tier": 1, "subTier": 1,
                  "provisioned_by": 1, "provisioned_at": 1,
                  "tier_expires_at": 1, "provision_reason": 1}
    cursor = state.users_col.find(query, projection).limit(100)
    out: list[dict] = []
    async for doc in cursor:
        doc["id"] = str(doc.pop("_id"))
        out.append(doc)
    return {"users": out, "count": len(out)}


# ═══════════════════════════════════════════════════════════════════════════
# Fraud admin (restored — requires Cloudflare KV config)
# ═══════════════════════════════════════════════════════════════════════════
def _kv_ns():
    from .config import CLOUDFLARE_KV_NAMESPACE_ID
    if not CLOUDFLARE_KV_NAMESPACE_ID:
        raise HTTPException(503, "KV not configured")
    from .integrations import get_kv_client
    return get_kv_client(), CLOUDFLARE_KV_NAMESPACE_ID


@router.get("/api/admin/fraud/fingerprints")
async def list_fingerprints(
    limit: int = 20, offset: int = 0,
    user: dict = Depends(get_current_user),
):
    _require_admin(user)
    try:
        from .fraud_detection import get_fingerprint_data
        kv, ns = _kv_ns()
        keys = kv.workers.kv.namespaces.keys.list(
            namespace_id=ns, prefix="fp:", limit=limit + offset,
        )
        all_keys = keys.get("keys", [])
        fingerprints = []
        for key in all_keys[offset:offset + limit]:
            try:
                d = await get_fingerprint_data(kv, ns, key["name"][3:])
                fingerprints.append({
                    "fingerprint_id": d.fingerprint[:16],
                    "risk_score": d.risk_score,
                    "is_banned": d.is_banned,
                    "account_count": len(d.associated_user_ids),
                })
            except Exception:
                continue
        return {"total": len(all_keys), "fingerprints": fingerprints}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("fingerprints_failed error=%s", e)
        raise HTTPException(500, "Failed to retrieve fingerprints")


@router.get("/api/admin/fraud/banned")
async def list_banned(user: dict = Depends(get_current_user)):
    _require_admin(user)
    from .fraud_detection import get_fingerprint_data
    kv, ns = _kv_ns()
    banned: list[dict] = []
    keys = kv.workers.kv.namespaces.keys.list(namespace_id=ns, prefix="fp:")
    for key in keys.get("keys", []):
        try:
            d = await get_fingerprint_data(kv, ns, key["name"][3:])
            if d.is_banned and d.ban_expires_at:
                banned.append({
                    "fingerprint_id": d.fingerprint[:16],
                    "risk_score": d.risk_score,
                    "ban_expires": d.ban_expires_at.isoformat(),
                })
        except Exception:
            continue
    return {"count": len(banned), "banned_fingerprints": banned}


@router.post("/api/admin/fraud/unban/{fingerprint}")
async def unban_fp(fingerprint: str, user: dict = Depends(get_current_user)):
    _require_admin(user)
    from .fraud_detection import get_fingerprint_data, save_fingerprint_data
    kv, ns = _kv_ns()
    d = await get_fingerprint_data(kv, ns, fingerprint)
    d.is_banned = False
    d.ban_expires_at = None
    d.risk_score = max(0, d.risk_score - 30)
    await save_fingerprint_data(kv, ns, d)
    return {"status": "success"}


@router.post("/api/admin/fraud/ban/{fingerprint}")
async def ban_fp(fingerprint: str, duration_hours: int = 168,
                 user: dict = Depends(get_current_user)):
    from datetime import timedelta
    _require_admin(user)
    from .fraud_detection import get_fingerprint_data, save_fingerprint_data
    kv, ns = _kv_ns()
    d = await get_fingerprint_data(kv, ns, fingerprint)
    d.is_banned = True
    d.ban_expires_at = datetime.utcnow() + timedelta(hours=duration_hours)
    d.risk_score = 100
    await save_fingerprint_data(kv, ns, d)
    return {"status": "success", "ban_expires": d.ban_expires_at.isoformat()}


@router.get("/api/admin/fraud/metrics")
async def fraud_metrics(user: dict = Depends(get_current_user)):
    _require_admin(user)
    from .fraud_detection import get_fingerprint_data
    kv, ns = _kv_ns()
    total = banned = high = medium = accounts = 0
    keys = kv.workers.kv.namespaces.keys.list(namespace_id=ns, prefix="fp:")
    for key in keys.get("keys", []):
        try:
            d = await get_fingerprint_data(kv, ns, key["name"][3:])
            total += 1
            accounts += len(d.associated_user_ids)
            if d.is_banned:
                banned += 1
            if d.risk_score >= 70:
                high += 1
            elif 40 <= d.risk_score < 70:
                medium += 1
        except Exception:
            continue
    return {
        "total_tracked_fingerprints": total,
        "total_associated_accounts": accounts,
        "currently_banned": banned,
        "high_risk_count": high,
        "medium_risk_count": medium,
    }


__all__ = ["router"]