# api/routes_agents.py
"""
AXELR — Agentic & productivity routes.
======================================
Multi-agent orchestration, workflow engine, personas, knowledge graph,
projects, long-term memory, repo indexing, and test loops.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .auth import get_current_user
from .ai_router import route_ai_request_parallel
from .state import get_object_id, limiter, state

logger = logging.getLogger("axelr.agents")

router = APIRouter(tags=["agents"])


# ═══════════════════════════════════════════════════════════════════════════
# Multi-agent orchestrator
# ═══════════════════════════════════════════════════════════════════════════

class AgentRequest(BaseModel):
    task: str
    agents: list[dict[str, str]] | None = None
    workspace: str | None = "core"


def _get_orchestrator():
    if state.orchestrator is None:
        try:
            from core.routing import Orchestrator
            state.orchestrator = Orchestrator(route_ai_request_parallel, max_parallel=4)
        except Exception as e:
            logger.warning("orchestrator_lazy_init_failed error=%s", e)
            return None
    return state.orchestrator


@router.post("/api/agents/chat")
@limiter.limit("10/minute")
async def agent_chat(request: Request, data: AgentRequest,
                      user: dict = Depends(get_current_user)):
    orch = _get_orchestrator()
    if orch is None:
        raise HTTPException(status_code=503, detail="Orchestrator unavailable")
    return await orch.run(data.task, user.get("tier", "free"), user)


@router.post("/api/agents/stream")
@limiter.limit("10/minute")
async def agent_stream(request: Request, data: AgentRequest,
                        user: dict = Depends(get_current_user)):
    orch = _get_orchestrator()
    if orch is None:
        raise HTTPException(status_code=503, detail="Orchestrator unavailable")
    tier = user.get("tier", "free")

    async def event_gen():
        try:
            async for evt in orch.stream(data.task, tier, user):
                yield f"data: {json.dumps(evt)}\n\n"
        except asyncio.CancelledError:
            yield f"data: {json.dumps({'type': 'error', 'message': 'cancelled'})}\n\n"
            raise
        except Exception as e:
            logger.exception("agent_stream_failed error=%s", e)
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)[:300]})}\n\n"
        finally:
            yield "event: close\ndata: {}\n\n"

    return StreamingResponse(
        event_gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )








# ═══════════════════════════════════════════════════════════════════════════
# Long-term memory
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/api/memory/{session_id}", tags=["Elite"])
async def memory_endpoint(session_id: str, query: str = "",
                            user: dict = Depends(get_current_user)):
    mem = state.conversation_memory
    if mem is None:
        raise HTTPException(status_code=503, detail="Memory unavailable")
    if query:
        items = await mem.retrieve(str(user["_id"]), query, session_id=session_id)
    else:
        items = await mem.retrieve_recent(str(user["_id"]), session_id)
    return {"success": True, "items": [i.to_dict() for i in items]}


# ═══════════════════════════════════════════════════════════════════════════
# Repo indexer
# ═══════════════════════════════════════════════════════════════════════════

class RepoIndexRequest(BaseModel):
    repo_url: str
    branch: str | None = None
    force: bool = False


class RepoQueryRequest(BaseModel):
    repo_url: str
    query: str
    top_k: int = 10


@router.post("/api/repo/index", tags=["Elite"])
async def repo_index_endpoint(data: RepoIndexRequest,
                                user: dict = Depends(get_current_user)):
    idx = state.repo_indexer
    if idx is None:
        raise HTTPException(status_code=503, detail="Repo indexer unavailable")
    stats = await idx.index_repo(data.repo_url, data.branch, force=data.force)
    return {"success": True, **stats.to_dict()}


@router.post("/api/repo/query", tags=["Elite"])
async def repo_query_endpoint(data: RepoQueryRequest,
                                user: dict = Depends(get_current_user)):
    idx = state.repo_indexer
    if idx is None:
        raise HTTPException(status_code=503, detail="Repo indexer unavailable")
    chunks = await idx.query(data.repo_url, data.query,
                              top_k=max(1, min(data.top_k, 50)))
    return {"success": True, "chunks": [c.to_dict() for c in chunks]}


# ═══════════════════════════════════════════════════════════════════════════
# Test loop
# ═══════════════════════════════════════════════════════════════════════════

class TestLoopRequest(BaseModel):
    code: str
    language: str = "python"


@router.post("/api/test-loop/run", tags=["Elite"])
async def test_loop_endpoint(data: TestLoopRequest,
                                user: dict = Depends(get_current_user)):
    loop = state.test_loop
    if loop is None:
        raise HTTPException(status_code=503, detail="Test loop unavailable")
    result = await loop.run(data.code, data.language,
                             tier=user.get("tier", "free"), user=user)
    return {"success": True, **result.to_dict()}


__all__ = ["router"]