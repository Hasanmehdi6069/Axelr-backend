"""HTTP surface. Works identically on Mongo and Appwrite.

Auth precedence: Axelr JWT (Bearer) → Appwrite session (X-Appwrite-Session).
The sync Appwrite SDK call runs in a worker thread; the async event loop
is never blocked.

Rate limiting: two buckets — reads (500/min/user) and writes (100/min/user).
In-process only; for multi-worker deployments back this with Redis or
remove it and rate-limit at the gateway.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status, Response
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from prometheus_client import generate_latest, Counter, Gauge

from api.auth import get_current_user  # type: ignore
from api.state import state  # type: ignore

from . import auditing
from .config import settings
from .search import ensure_bootstrapped, search_cached
from .storage_base import RateLimiter, get_backend, init_backend

logger = logging.getLogger("insights.routes")
router = APIRouter(tags=["insights"])
_bearer = HTTPBearer(auto_error=False)

_read_limiter = RateLimiter(max_requests=500, window_seconds=60)
_write_limiter = RateLimiter(max_requests=100, window_seconds=60)

# Prometheus metrics for production observability
AUDIT_EVENTS_INSERTED = Counter("audit_events_inserted", "Number of audit events inserted")
AUDIT_BULK_FAILURES = Counter("audit_bulk_failures", "Number of bulk insert failures")
STORAGE_OPERATIONS = Counter("storage_operations", "Storage backend operations", ["backend", "operation", "status"])
STORAGE_HEALTH = Gauge("storage_health", "Storage backend health status", ["backend"])


async def _ensure_ready() -> None:
    # `init_backend()` is idempotent — call unconditionally.
    await init_backend()


# ── Auth ──────────────────────────────────────────────────────────────

def _verify_appwrite_session(session_secret: str) -> dict[str, Any] | None:
    """Sync SDK call — always invoke via `asyncio.to_thread`."""
    from appwrite.client import Client
    from appwrite.services.account import Account
    if not settings.appwrite_configured():
        return None
    try:
        client = (
            Client()
            .set_endpoint(settings.appwrite_endpoint)
            .set_project(settings.appwrite_project_id)
            .set_session(session_secret)
        )
        u = Account(client).get()
        email = (
            getattr(u, "email", None)
            or (u.get("email") if isinstance(u, dict) else None)
        )
        uid = (
            getattr(u, "id", None)
            or (u.get("$id") if isinstance(u, dict) else None)
        )
        if not uid:
            return None
        return {
            "email": email, "$id": uid,
            "is_admin": bool(email and email.lower() in settings.admin_emails),
        }
    except Exception as e:
        logger.info("appwrite session invalid: %s", str(e)[:120])
        return None


async def get_insights_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_appwrite_session: str | None = Header(None, alias="X-Appwrite-Session"),
) -> dict[str, Any]:
    user: dict[str, Any] | None = None
    if credentials is not None:
        try:
            user = await get_current_user(request, credentials)
        except HTTPException:
            pass
    if user is None and x_appwrite_session:
        user = await asyncio.to_thread(
            _verify_appwrite_session, x_appwrite_session,
        )
        if user is None:
            logger.warning("invalid X-Appwrite-Session")
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required")

    key = user.get("$id") or user.get("email") or "anon"
    if not await _read_limiter.check(key):
        raise HTTPException(429, "Rate limit exceeded")
    return user


def _require_admin(user: dict[str, Any]) -> None:
    if not (user.get("is_admin") or user.get("isAdmin")):
        raise HTTPException(403, "Admin only")


async def _require_writer(user: dict[str, Any] = Depends(get_insights_user)) -> dict[str, Any]:
    key = user.get("$id") or user.get("email") or "anon"
    if not await _write_limiter.check(key):
        raise HTTPException(429, "Write rate limit exceeded")
    return user


# ── Schemas ───────────────────────────────────────────────────────────

class TransactionIn(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=255)
    session_id: str = Field(..., min_length=1, max_length=255)
    action: str = Field(..., min_length=1, max_length=255)
    details: dict[str, Any] = Field(default_factory=dict)


class NodeExecutionIn(BaseModel):
    node_id: str = Field(..., min_length=1, max_length=255)
    pipeline_id: str = Field(..., min_length=1, max_length=255)
    latency_ms: float = Field(..., ge=0)
    status: str = Field(..., min_length=1, max_length=64)
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PipelineStateIn(BaseModel):
    pipeline_id: str = Field(..., min_length=1, max_length=255)
    state: dict[str, Any]


class AuditBatchIn(BaseModel):
    events: list[dict[str, Any]] = Field(
        ..., min_length=1, max_length=settings.audit_batch_max,
    )


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2048)
    top_k: int = Field(
        default=settings.search_top_k_default, ge=1, le=settings.search_top_k_max,
    )
    min_score: float | None = Field(default=None, ge=0, le=1)


class SearchHit(BaseModel):
    doc_id: str
    content: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    query: str
    hits: list[SearchHit]
    count: int


class DocumentIn(BaseModel):
    doc_id: str = Field(..., min_length=1, max_length=255)
    content: str = Field(..., min_length=1)
    meta: dict[str, Any] = Field(default_factory=dict)


# ── Writes ────────────────────────────────────────────────────────────

@router.post("/audit/transaction", status_code=201)
async def write_transaction(
    body: TransactionIn, user: dict = Depends(_require_writer),
):
    await _ensure_ready()
    rid = await auditing.log_transaction(
        user_id=body.user_id, session_id=body.session_id,
        action=body.action, details=body.details,
    )
    if rid is None:
        raise HTTPException(503, "Audit store unavailable")
    return {"ok": True, "id": rid}


@router.post("/audit/node-execution", status_code=201)
async def write_node_execution(
    body: NodeExecutionIn, user: dict = Depends(_require_writer),
):
    await _ensure_ready()
    rid = await auditing.log_node_execution(
        node_id=body.node_id, pipeline_id=body.pipeline_id,
        latency_ms=body.latency_ms, status=body.status,
        error=body.error, metadata=body.metadata,
    )
    if rid is None:
        raise HTTPException(503, "Audit store unavailable")
    return {"ok": True, "id": rid}


@router.post("/audit/pipeline-state", status_code=201)
async def write_pipeline_state(
    body: PipelineStateIn, user: dict = Depends(_require_writer),
):
    await _ensure_ready()
    rid = await auditing.log_pipeline_state(
        pipeline_id=body.pipeline_id, state_snapshot=body.state,
    )
    if rid is None:
        raise HTTPException(503, "Audit store unavailable")
    return {"ok": True, "id": rid}


@router.post("/audit/batch", status_code=202)
async def write_batch(
    body: AuditBatchIn, user: dict = Depends(_require_writer),
):
    await _ensure_ready()
    n = await auditing.log_batch_events(body.events)
    return {"ok": True, "inserted": n}


# ── Reads ─────────────────────────────────────────────────────────────

@router.get("/audit/transactions")
async def read_transactions(
    limit: int = Query(50, ge=1, le=500),
    user_id: str | None = Query(None),
    user: dict = Depends(get_insights_user),
):
    _require_admin(user)
    await _ensure_ready()
    rows = await auditing.list_transactions(limit=limit, user_id=user_id)
    return {"transactions": rows, "count": len(rows)}


@router.get("/audit/latency")
async def read_latency(
    pipeline_id: str | None = Query(None),
    window_minutes: int = Query(60, ge=1, le=1440),
    user: dict = Depends(get_insights_user),
):
    _require_admin(user)
    await _ensure_ready()
    return await auditing.get_latency_report(
        pipeline_id=pipeline_id, window_minutes=window_minutes,
    )


@router.get("/audit/pipelines")
async def read_pipelines(
    window_minutes: int = Query(60, ge=1, le=1440),
    user: dict = Depends(get_insights_user),
):
    _require_admin(user)
    await _ensure_ready()
    return {
        "pipelines": await auditing.get_pipeline_summary(
            window_minutes=window_minutes,
        ),
        "window_minutes": window_minutes,
    }


@router.get("/audit/pipelines/{pipeline_id}/state")
async def read_pipeline_state(
    pipeline_id: str, user: dict = Depends(get_insights_user),
):
    _require_admin(user)
    await _ensure_ready()
    doc = await auditing.get_latest_pipeline_state(pipeline_id)
    if doc is None:
        raise HTTPException(404, f"No state recorded for {pipeline_id}")
    return doc


@router.get("/audit/daily")
async def read_daily(
    days: int = Query(7, ge=1, le=90),
    user: dict = Depends(get_insights_user),
):
    _require_admin(user)
    await _ensure_ready()
    return {"days": await auditing.get_daily_event_counts(days=days)}


@router.get("/audit/stats")
async def read_stats(
    window_minutes: int = Query(60, ge=1, le=1440),
    user: dict = Depends(get_insights_user),
):
    """Aggregate used by the dashboard top cards."""
    _require_admin(user)
    await _ensure_ready()
    lat = await auditing.get_latency_report(window_minutes=window_minutes)
    pipes = await auditing.get_pipeline_summary(window_minutes=window_minutes)
    total = sum(p["total"] for p in pipes)
    errors = sum(p["errors"] for p in pipes)
    return {
        "window_minutes": window_minutes,
        "total_events": total,
        "error_rate": round(errors / total, 4) if total else 0.0,
        "avg_latency_ms": lat.get("mean"),
        "p95_latency_ms": lat.get("p95"),
        "latency_backend": lat.get("backend"),
    }


# ── Search & docs ─────────────────────────────────────────────────────

@router.post("/search", response_model=SearchResponse)
async def semantic_search(
    body: SearchRequest, user: dict = Depends(get_insights_user),
):
    hits = await search_cached(
        body.query, top_k=body.top_k, min_score=body.min_score,
    )
    return {"query": body.query, "hits": hits, "count": len(hits)}


@router.get("/documents")
async def list_documents(
    limit: int = Query(200, ge=1, le=500),
    user: dict = Depends(get_insights_user),
):
    idx = await ensure_bootstrapped()
    rows = await idx.list_documents(limit=limit)
    return {"documents": rows, "count": len(rows)}


@router.post("/documents", status_code=201)
async def upsert_document(
    body: DocumentIn, user: dict = Depends(_require_writer),
):
    _require_admin(user)
    idx = await ensure_bootstrapped()
    try:
        await idx.upsert(body.doc_id, body.content, body.meta)
    except RuntimeError as e:
        raise HTTPException(503, f"Storage unavailable: {e}")
    return {
        "doc_id": body.doc_id, "content": body.content, "meta": body.meta,
    }


@router.delete("/documents/{doc_id}", status_code=204)
async def delete_document(
    doc_id: str, user: dict = Depends(_require_writer),
):
    _require_admin(user)
    idx = await ensure_bootstrapped()
    if not await idx.remove(doc_id):
        raise HTTPException(404, "Document not found")
    return None


@router.post("/reindex", status_code=202)
async def reindex(user: dict = Depends(_require_writer)):
    _require_admin(user)
    idx = await ensure_bootstrapped()
    idx.force_rebuild()
    return {"ok": True, "documents": len(idx)}


@router.get("/health")
async def health():
    idx = await ensure_bootstrapped()
    backend_health: dict[str, Any] = {
        "healthy": False, "reason": "not_initialised",
    }
    try:
        await init_backend()
        backend_health = await get_backend().health()
    except Exception as e:
        backend_health = {"healthy": False, "reason": str(e)[:200]}
    return {
        "status": "ok" if backend_health.get("healthy") else "degraded",
        "indexed_documents": len(idx),
        "search_backend": settings.search_backend,
        "storage_backend": settings.storage_backend,
        "backend_health": backend_health,
        "appwrite_configured": settings.appwrite_configured(),
    }


@router.get("/metrics")
async def metrics():
    """Expose Prometheus metrics for production monitoring."""
    # Update storage health gauge before returning metrics
    try:
        await init_backend()
        backend_health = await get_backend().health()
        STORAGE_HEALTH.labels(backend=settings.storage_backend).set(1 if backend_health.get("healthy") else 0)
    except Exception:
        STORAGE_HEALTH.labels(backend=settings.storage_backend).set(0)
    return Response(generate_latest(), media_type="text/plain")


@router.get("/dashboard", include_in_schema=False)
async def dashboard():
    return FileResponse(Path(__file__).parent / "dashboard.html")