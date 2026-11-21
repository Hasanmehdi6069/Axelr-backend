"""FastAPI router for Sentinel — mounted at /sentinel in the main app.

Four endpoints, no auth duplication (reuses Vultr's get_vultr_user):

  GET  /sentinel/approvals              — list pending
  POST /sentinel/approvals/{id}/approve — approve → execute
  POST /sentinel/approvals/{id}/reject  — reject
  POST /sentinel/propose                — poll Confluence, populate queue

The execution path is identical to the MCP tool path — same dispatch(),
same audit, same WAL, same engine.
"""

from __future__ import annotations

from axelr_vultr.routes import get_vultr_user
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .atlassian import (
    _QUEUE,
    PendingApproval,
    approve,
    list_pending,
    propose,
    reject,
)
from .engine import Engine

router = APIRouter(prefix="/sentinel", tags=["sentinel"])
_engine = Engine()


class ApproveRequest(BaseModel):
    approver: str = Field(default="human", max_length=64)


class RejectRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


class ProposeRequest(BaseModel):
    max_pages: int = Field(default=5, ge=1, le=20)
    queue: bool = Field(default=True, description="Add to approval queue")


@router.get("/health")
async def sentinel_health() -> dict:
    return {"success": True, **(await _engine.health())}


@router.get("/approvals")
async def approvals_list(user: dict = Depends(get_vultr_user)) -> dict:
    return {"success": True, "pending": await list_pending()}


@router.post("/approvals/{proposal_id}/approve")
async def approvals_approve(proposal_id: str, body: ApproveRequest, user: dict = Depends(get_vultr_user)) -> dict:
    result = await approve(proposal_id, approver=body.approver, engine=_engine)
    if not result.get("ok"):
        raise HTTPException(400, detail=result.get("error", "approval_failed"))
    return {"success": True, **result}


@router.post("/approvals/{proposal_id}/reject")
async def approvals_reject(proposal_id: str, body: RejectRequest, user: dict = Depends(get_vultr_user)) -> dict:
    result = await reject(proposal_id, reason=body.reason)
    if not result.get("ok"):
        raise HTTPException(400, detail=result.get("error", "rejection_failed"))
    return {"success": True, **result}


@router.post("/propose")
async def propose_actions(body: ProposeRequest, user: dict = Depends(get_vultr_user)) -> dict:
    result = await propose(_engine, {"max_pages": body.max_pages})
    if body.queue:
        for p in result["proposals"]:
            await _QUEUE.add(
                PendingApproval(
                    proposal_id=p["proposal_id"],
                    action=p["action"],
                    params=p["params"],
                    category=p["category"],
                    reason=p["reason"],
                )
            )
    return {"success": True, **result, "queued": len(result["proposals"]) if body.queue else 0}
