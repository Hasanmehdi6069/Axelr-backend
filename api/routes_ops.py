# api/routes_ops.py
"""
AXELR — Operational routers (system · guest · RAG · hooks · indexer).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
    Response,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .auth import get_current_user
from .lifecycle import validate_all_providers
from .providers import get_adapter_statuses
from .state import (
    delete_redis_cache,
    get_redis_cache,
    set_redis_cache,
    state,
)

logger = structlog.get_logger("axelr.routes.ops")
router = APIRouter()

_START_TS = time.time()


# ═══════════════════════════════════════════════════════════════════════════
# §1 — System / health
# ═══════════════════════════════════════════════════════════════════════════
@router.get("/api/health", tags=["system"])
async def health_root() -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "status": "operational",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ping": "/api/health/ping",
            "live": "/api/health/live",
            "ready": "/api/health/ready",
            "detailed": "/api/health/detailed",
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/health/ping", tags=["system"])
async def health_ping() -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={"ok": True, "ts": int(time.time())},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/health/live", tags=["system"])
async def health_live() -> JSONResponse:
    return JSONResponse(status_code=200, content={"status": "live"})


@router.get("/api/health/ready", tags=["system"])
async def health_ready() -> JSONResponse:
    db = "connected" if state.db_available else "disconnected"
    redis = await get_redis_cache(key="*")
    try:
        adapters = get_adapter_statuses()
    except Exception as e:
        logger.warning("adapter_status_failed", error=str(e))
        adapters = {}
    adapter_issues = [n for n, s in adapters.items() if not s.get("healthy", False)]
    overall = "operational" if (state.db_available and not adapter_issues) else "degraded"
    return JSONResponse(
        status_code=200,
        content={
            "status": overall,
            "db": db,
            "redis": redis,
            "adapters": adapters,
            "uptime_s": round(time.time() - _START_TS, 2),
        },
    )


@router.get("/api/adapters/status", tags=["system"])
async def adapters_status() -> dict[str, Any]:
    return get_adapter_statuses()


@router.get("/api/admin/provider-health", tags=["system"])
async def provider_health_endpoint(
    force: bool = False,
    user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    if not (user.get("is_admin") or user.get("isAdmin")):
        raise HTTPException(403, "Admin only")
    return {"status": "ok", "providers": await validate_all_providers(force=force)}


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Guest sessions
# ═══════════════════════════════════════════════════════════════════════════
_GUEST_TTL = 3600


@router.post("/api/guest/session", tags=["guest"])
async def create_guest_session() -> dict[str, Any]:
    sid = f"guest_{secrets.token_urlsafe(16)}"
    expires = datetime.now(timezone.utc) + timedelta(seconds=_GUEST_TTL)
    await set_redis_cache(
        f"guest:{sid}",
        {
            "expires": expires.isoformat(),
            "messages": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "permissions": [],
        },
        ttl=_GUEST_TTL,
    )
    return {"sessionId": sid, "expiresIn": _GUEST_TTL}


@router.get("/api/guest/session/{session_id}", tags=["guest"])
async def get_guest_session(session_id: str) -> dict[str, Any]:
    session = await get_redis_cache(f"guest:{session_id}")
    if not session:
        return {
            "sessionId": session_id,
            "expiresIn": 3600,
            "messageCount": 0,
            "permissions": [],
        }
    expires = datetime.fromisoformat(session["expires"])
    remaining = int((expires - datetime.now(timezone.utc)).total_seconds())
    if remaining <= 0:
        await delete_redis_cache(f"guest:{session_id}")
        return {
            "sessionId": session_id,
            "expiresIn": 3600,
            "messageCount": 0,
            "permissions": [],
        }
    return {
        "sessionId": session_id,
        "expiresIn": remaining,
        "messageCount": len(session.get("messages", [])),
        "permissions": session.get("permissions", []),
    }


@router.get("/api/guest/permissions/{session_id}", tags=["guest"])
async def get_guest_permissions(session_id: str) -> dict[str, Any]:
    session = await get_redis_cache(f"guest:{session_id}")
    if not session:
        return {
            "sessionId": session_id,
            "permissions": [],
        }
    return {
        "sessionId": session_id,
        "permissions": session.get("permissions", []),
    }


class ConsentUpdateRequest(BaseModel):
    permissions: dict[str, bool]


@router.post("/api/guest/permissions/{session_id}", tags=["guest"])
async def update_guest_permissions(
    session_id: str,
    request: ConsentUpdateRequest,
) -> dict[str, Any]:
    session = await get_redis_cache(f"guest:{session_id}")
    if not session:
        raise HTTPException(404, "Session not found")
    granted = [k for k, v in request.permissions.items() if v]
    session["permissions"] = granted
    expires = datetime.fromisoformat(session["expires"])
    remaining = int((expires - datetime.now(timezone.utc)).total_seconds())
    if remaining > 0:
        await set_redis_cache(f"guest:{session_id}", session, ttl=remaining)
    return {"success": True, "sessionId": session_id, "permissions": granted}


# ═══════════════════════════════════════════════════════════════════════════
# §3 — RAG (BM25)
# ═══════════════════════════════════════════════════════════════════════════
class RagDoc(BaseModel):
    id: str = Field(..., min_length=1, max_length=128)
    text: str = Field(..., min_length=1, max_length=8000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RagAddRequest(BaseModel):
    docs: list[RagDoc] = Field(..., min_length=1, max_length=500)


class RagQueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    top_k: int = Field(default=10, ge=1, le=50)
    scope: str | None = Field(default=None, max_length=64)


@router.post("/api/rag/add", tags=["rag"])
async def rag_add(
    body: RagAddRequest,
    user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    from core.rank_bm25 import get_bm25_index

    idx = get_bm25_index()
    uid = str(user.get("_id", "anon"))
    added = 0
    for d in body.docs:
        meta = dict(d.metadata)
        meta.setdefault("user_id", uid)
        if idx.add(d.id, d.text, meta):
            added += 1
    return {"success": True, "added": added, "stats": idx.stats()}


@router.post("/api/rag/query", tags=["rag"])
async def rag_query(
    body: RagQueryRequest,
    user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    from core.rank_bm25 import get_bm25_index

    idx = get_bm25_index()
    uid = str(user.get("_id", "anon"))
    scope = body.scope

    def _filter(meta: dict[str, Any]) -> bool:
        if scope == "public":
            return meta.get("scope") == "public"
        return meta.get("user_id") == uid or meta.get("scope") == "public"

    hits = idx.query(body.query, top_k=body.top_k, filter_fn=_filter)
    return {"success": True, "hits": [h.to_dict() for h in hits]}


@router.get("/api/rag/stats", tags=["rag"])
async def rag_stats(user: dict = Depends(get_current_user)) -> dict[str, Any]:
    from core.rank_bm25 import get_bm25_index

    return {"success": True, **get_bm25_index().stats()}


@router.delete("/api/rag/clear", tags=["rag"])
async def rag_clear(user: dict = Depends(get_current_user)) -> dict[str, Any]:
    from core.rank_bm25 import get_bm25_index

    if not (user.get("is_admin") or user.get("isAdmin")):
        return {"success": False, "message": "admin_only"}
    idx = get_bm25_index()
    idx.clear()
    return {"success": True, "stats": idx.stats()}


# ═══════════════════════════════════════════════════════════════════════════
# §4 — Hooks (verify + collab SSE + warmup)
# ═══════════════════════════════════════════════════════════════════════════
_SOURCE_MAX = 200_000
_CLAIM_MAX = 4_000


class VerifyRequest(BaseModel):
    source: str = Field(..., min_length=1, max_length=_SOURCE_MAX)
    claim: str = Field(..., min_length=1, max_length=_CLAIM_MAX)
    normalize: bool = True


def _norm(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip().casefold()


@router.post("/api/hooks/verify", tags=["hooks"])
async def hooks_verify(payload: VerifyRequest) -> dict[str, Any]:
    src = payload.source if not payload.normalize else _norm(payload.source)
    claim = payload.claim if not payload.normalize else _norm(payload.claim)
    grounded = claim in src
    return {
        "success": True,
        "grounded": grounded,
        "source_offset": src.find(claim) if grounded else -1,
        "claim_length": len(claim),
        "checked_at": time.time(),
    }


def _room_key(room: str) -> str:
    return f"axelr:collab:{room}"


@router.get("/api/collab/{room}/sse", tags=["hooks"])
async def collab_sse(
    room: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    if not re.match(r"^[a-zA-Z0-9_\-]{1,64}$", room):
        raise HTTPException(400, "Invalid room id")
    if state.redis_client is None:
        raise HTTPException(503, "Collaboration bus unavailable")

    from fastapi.responses import StreamingResponse

    pubsub = state.redis_client.pubsub()
    channel = _room_key(room)
    await pubsub.subscribe(channel)

    async def _stream():
        try:
            yield f"event: open\ndata: {json.dumps({'room': room})}\n\n".encode()
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(
                        pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0),
                        timeout=2.0,
                    )
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                if not msg or msg.get("type") != "message":
                    continue
                data = msg.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8", "replace")
                yield f"event: token\ndata: {data}\n\n".encode()
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
            except Exception:
                pass

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.post("/api/warmup", include_in_schema=False, tags=["hooks"])
async def warmup_signal(x_warmup_token: str | None = Header(default=None)):
    from core.warmup import signal_warmup

    expected = (os.getenv("WARMUP_TOKEN") or "").strip()
    if expected and x_warmup_token != expected:
        raise HTTPException(403, "Invalid warmup token")
    ok = await signal_warmup(state.redis_client)
    return {"success": ok}


@router.post("/api/hooks/github", tags=["webhooks"])
async def github_webhook(request: Request) -> Response:
    """GitHub webhook — enforce HMAC if GITHUB_WEBHOOK_SECRET is set."""
    body = await request.body()
    secret = (os.getenv("GITHUB_WEBHOOK_SECRET") or "").strip()
    if secret:
        import hashlib as _h
        import hmac as _hm

        sig = request.headers.get("x-hub-signature-256", "")
        expected = "sha256=" + _hm.new(secret.encode(), body, _h.sha256).hexdigest()
        if not _hm.compare_digest(sig, expected):
            raise HTTPException(401, "bad signature")
    return Response(status_code=200)


# ═══════════════════════════════════════════════════════════════════════════
# §5 — Indexer
# ═══════════════════════════════════════════════════════════════════════════
class IndexDocumentRequest(BaseModel):
    content: str = Field(..., min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.post("/api/indexer/add", tags=["indexer"])
async def add_document_to_index(
    request: IndexDocumentRequest,
    user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        from core.rank_bm25 import get_bm25_index

        idx = get_bm25_index()
        doc_id = f"doc_{secrets.token_hex(8)}"
        meta = dict(request.metadata)
        meta["user_id"] = str(user.get("_id", "anon"))
        idx.add(doc_id, request.content, meta)
        return {"documentId": doc_id, "indexed": True}
    except Exception as e:
        logger.warning("indexer_add_failed", error=str(e))
        raise HTTPException(503, "Indexer unavailable")


__all__ = ["router"]
