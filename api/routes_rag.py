# api/routes_rag.py
"""
AXELR — Local RAG endpoints (BM25)
===================================
Exposes core.rank_bm25 via four endpoints. No model calls, no LLM tokens,
no cloud dependency. Purely local keyword retrieval.

Endpoints:
    POST   /api/rag/add      — bulk-add documents
    POST   /api/rag/query    — retrieve ranked hits
    DELETE /api/rag/clear    — clear the index
    GET    /api/rag/stats    — index statistics
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from .auth import get_current_user
from .state import limiter

logger = logging.getLogger("axelr.rag")
router = APIRouter(tags=["rag"])

_MAX_DOCS_PER_REQUEST = 500


class RagDoc(BaseModel):
    id: str = Field(..., min_length=1, max_length=128)
    text: str = Field(..., min_length=1, max_length=8000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RagAddRequest(BaseModel):
    docs: list[RagDoc] = Field(..., min_length=1,
                                max_length=_MAX_DOCS_PER_REQUEST)


class RagQueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    top_k: int = Field(default=10, ge=1, le=50)
    scope: str | None = Field(default=None, max_length=64)


@router.post("/api/rag/add")
@limiter.limit("30/minute")
async def rag_add(request: Request, body: RagAddRequest,
                  user: dict = Depends(get_current_user)):
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


@router.post("/api/rag/query")
@limiter.limit("120/minute")
async def rag_query(request: Request, body: RagQueryRequest,
                    user: dict = Depends(get_current_user)):
    from core.rank_bm25 import get_bm25_index
    idx = get_bm25_index()
    uid = str(user.get("_id", "anon"))
    scope = body.scope

    def _filter(meta: dict[str, Any]) -> bool:
        # Users can only see their own docs unless scope explicitly
        # targets a shared corpus (e.g. "public").
        if scope == "public":
            return meta.get("scope") == "public"
        return meta.get("user_id") == uid or meta.get("scope") == "public"

    hits = idx.query(body.query, top_k=body.top_k, filter_fn=_filter)
    return {"success": True, "hits": [h.to_dict() for h in hits]}


@router.delete("/api/rag/clear")
@limiter.limit("5/minute")
async def rag_clear(request: Request,
                    user: dict = Depends(get_current_user)):
    from core.rank_bm25 import get_bm25_index
    idx = get_bm25_index()
    # Only superadmin gets the global clear; normal users get a no-op
    # response so the endpoint exists without being a footgun.
    if not (user.get("is_admin") or user.get("isAdmin")):
        return {"success": False, "message": "admin_only"}
    idx.clear()
    return {"success": True, "stats": idx.stats()}


@router.get("/api/rag/stats")
@limiter.limit("30/minute")
async def rag_stats(request: Request,
                    user: dict = Depends(get_current_user)):
    from core.rank_bm25 import get_bm25_index
    return {"success": True, **get_bm25_index().stats()}


__all__ = ["router"]