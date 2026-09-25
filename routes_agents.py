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
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .auth import get_current_user
from .state import get_object_id, limiter, state
from .routes_core import route_ai_request_parallel

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
# Workflow engine
# ═══════════════════════════════════════════════════════════════════════════

class WorkflowStep(BaseModel):
    name: str
    prompt: str
    model: str | None = None
    temperature: float | None = 0.2


class WorkflowRequest(BaseModel):
    steps: list[WorkflowStep]
    workspace: str | None = "core"


@router.post("/api/workflow/run")
async def run_workflow(data: WorkflowRequest, user: dict = Depends(get_current_user)):
    if not data.steps:
        raise HTTPException(400, "No steps provided")

    async def event_gen():
        accumulated = ""
        for idx, step in enumerate(data.steps):
            yield f"data: {json.dumps({'step': step.name, 'status': 'started', 'index': idx})}\n\n"
            try:
                prompt = (step.prompt.format(context=accumulated)
                          if "{context}" in step.prompt else step.prompt)
                result = await route_ai_request_parallel(
                    workspace=data.workspace or "core",
                    task_type="structuring", prompt=prompt,
                    history=[], files=[], max_tokens=4096,
                    temp=step.temperature or 0.2,
                    tier=user.get("tier", "free"), user=user,
                )
                output = result.get("text", "")
                accumulated += f"\n\n### {step.name}\n{output}"
                yield f"data: {json.dumps({'step': step.name, 'status': 'completed', 'output': output, 'index': idx})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'step': step.name, 'status': 'error', 'error': str(e), 'index': idx})}\n\n"
                break
        yield f"data: {json.dumps({'status': 'done', 'final': accumulated})}\n\n"
        yield "event: close\ndata: {}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# ═══════════════════════════════════════════════════════════════════════════
# Knowledge graph
# ═══════════════════════════════════════════════════════════════════════════

class KnowledgeItem(BaseModel):
    key: str
    value: str
    tags: list[str] | None = []


@router.post("/api/knowledge")
async def save_knowledge(item: KnowledgeItem, user: dict = Depends(get_current_user)):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    collection = state.db.get_collection("knowledge")
    await collection.update_one(
        {"userId": user["_id"], "key": item.key},
        {"$set": {"value": item.value, "tags": item.tags,
                  "updatedAt": datetime.utcnow()}},
        upsert=True,
    )
    if state.redis_client:
        await state.redis_client.setex(
            f"knowledge:{user['_id']}:{item.key}", 86400, item.value,
        )
    return {"success": True}


@router.get("/api/knowledge")
async def get_knowledge(user: dict = Depends(get_current_user)):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    collection = state.db.get_collection("knowledge")
    cursor = collection.find({"userId": user["_id"]})
    items = await cursor.to_list(length=100)
    for it in items:
        it["_id"] = str(it["_id"])
        it.pop("userId", None)
    return {"knowledge": items}


@router.get("/api/knowledge/search")
async def search_knowledge(q: str, user: dict = Depends(get_current_user)):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    collection = state.db.get_collection("knowledge")
    await collection.create_index([("value", "text"), ("key", "text")])
    cursor = collection.find({"userId": user["_id"], "$text": {"$search": q}})
    items = await cursor.to_list(length=20)
    for it in items:
        it["_id"] = str(it["_id"])
        it.pop("userId", None)
    return {"results": items}


@router.delete("/api/knowledge/{knowledge_id}")
async def delete_knowledge(knowledge_id: str,
                            user: dict = Depends(get_current_user)):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    ObjectId = get_object_id()
    if not ObjectId or not ObjectId.is_valid(knowledge_id):
        raise HTTPException(400, "Invalid ID")
    collection = state.db.get_collection("knowledge")
    res = await collection.delete_one({"_id": ObjectId(knowledge_id),
                                        "userId": user["_id"]})
    if res.deleted_count == 0:
        raise HTTPException(404, "Knowledge not found")
    return {"success": True}


# ═══════════════════════════════════════════════════════════════════════════
# Personas
# ═══════════════════════════════════════════════════════════════════════════

class Persona(BaseModel):
    name: str
    description: str | None = ""
    system_prompt: str
    is_public: bool = False


@router.post("/api/personas")
async def create_persona(data: Persona, user: dict = Depends(get_current_user)):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    collection = state.db.get_collection("personas")
    doc = data.model_dump()
    doc["userId"] = user["_id"]
    doc["createdAt"] = datetime.utcnow()
    result = await collection.insert_one(doc)
    return {"success": True, "id": str(result.inserted_id)}


@router.get("/api/personas")
async def list_personas(user: dict = Depends(get_current_user)):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    collection = state.db.get_collection("personas")
    cursor = collection.find({"$or": [{"userId": user["_id"]}, {"is_public": True}]})
    personas = await cursor.to_list(length=100)
    for p in personas:
        p["_id"] = str(p["_id"])
        p.pop("userId", None)
    return {"personas": personas}


@router.get("/api/personas/{persona_id}")
async def get_persona(persona_id: str, user: dict = Depends(get_current_user)):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    ObjectId = get_object_id()
    if not ObjectId or not ObjectId.is_valid(persona_id):
        raise HTTPException(400, "Invalid ID")
    collection = state.db.get_collection("personas")
    persona = await collection.find_one({
        "_id": ObjectId(persona_id),
        "$or": [{"userId": user["_id"]}, {"is_public": True}],
    })
    if not persona:
        raise HTTPException(404, "Persona not found")
    persona["_id"] = str(persona["_id"])
    persona.pop("userId", None)
    return persona


# ═══════════════════════════════════════════════════════════════════════════
# Projects
# ═══════════════════════════════════════════════════════════════════════════

class ProjectCreate(BaseModel):
    name: str
    workspace: str


class ProjectUpdate(BaseModel):
    name: str | None = None
    assets: list[str] | None = None


@router.post("/api/projects")
async def create_project(data: ProjectCreate, user: dict = Depends(get_current_user)):
    if not state.db_available or state.projects_col is None:
        raise HTTPException(503, "Projects collection unavailable")
    project = {
        "userId": user["_id"], "name": data.name,
        "workspace": data.workspace, "assets": [],
        "createdAt": datetime.utcnow(),
    }
    result = await state.projects_col.insert_one(project)
    return {"success": True, "projectId": str(result.inserted_id)}


@router.get("/api/projects")
async def list_projects(user: dict = Depends(get_current_user)):
    if not state.db_available or state.projects_col is None:
        raise HTTPException(503, "Projects collection unavailable")
    cursor = state.projects_col.find({"userId": user["_id"]}).sort("createdAt", -1)
    projects = await cursor.to_list(length=100)
    for p in projects:
        p["_id"] = str(p["_id"])
        p["userId"] = str(p["userId"])
    return {"projects": projects}


@router.put("/api/projects/{project_id}")
async def update_project(project_id: str, data: ProjectUpdate,
                          user: dict = Depends(get_current_user)):
    if not state.db_available or state.projects_col is None:
        raise HTTPException(503, "Projects collection unavailable")
    ObjectId = get_object_id()
    if not ObjectId or not ObjectId.is_valid(project_id):
        raise HTTPException(400, "Invalid project ID")
    update: dict[str, Any] = {}
    if data.name is not None:
        update["name"] = data.name
    if data.assets is not None:
        update["assets"] = data.assets
    result = await state.projects_col.update_one(
        {"_id": ObjectId(project_id), "userId": user["_id"]},
        {"$set": update},
    )
    if result.modified_count == 0:
        raise HTTPException(404, "Project not found")
    return {"success": True}


@router.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, user: dict = Depends(get_current_user)):
    if not state.db_available or state.projects_col is None:
        raise HTTPException(503, "Projects collection unavailable")
    ObjectId = get_object_id()
    if not ObjectId or not ObjectId.is_valid(project_id):
        raise HTTPException(400, "Invalid project ID")
    result = await state.projects_col.delete_one(
        {"_id": ObjectId(project_id), "userId": user["_id"]},
    )
    if result.deleted_count == 0:
        raise HTTPException(404, "Project not found")
    return {"success": True}


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