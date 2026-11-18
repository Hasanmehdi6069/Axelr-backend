"""FastAPI entry point.

Tasks: T4.1–T4.5. Serves the SPA and the /api surface from one process.

Run:
    uvicorn backend.main:app --host 0.0.0.0 --port 8080

Static mount:
    frontend/dist/ if present — single-binary demo. Otherwise the Vite
    dev server (localhost:5173) proxies /api here.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .core import (
    AppError,
    ProposalEditRequest,
    init_db,
)
from .pipeline import (
    approve_proposal,
    edit_proposal,
    execute_proposal,
    extract_document,
    generate_brief,
    get_proposal,
    ingest_file,
    list_audit,
    list_proposals,
    reject_proposal,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("founder_ops.main")


# ═══════════════════════════════════════════════════════════════════════
# Lifespan (T4.1)
# ═══════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
    logger.info("founder_ops_ready db=%s", os.getenv("FOUNDER_OPS_DB", "data/founder_ops.db"))
    yield
    logger.info("founder_ops_shutdown")


app = FastAPI(
    title="Founder Ops Copilot",
    version="1.0.0",
    lifespan=lifespan,
)


# ═══════════════════════════════════════════════════════════════════════
# CORS for dev (T4.5, N-3)
# ═══════════════════════════════════════════════════════════════════════

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════════════════════
# Central error handler (T4.3)
# ═══════════════════════════════════════════════════════════════════════

@app.exception_handler(AppError)
async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"code": exc.code, "message": exc.message},
    )


# ═══════════════════════════════════════════════════════════════════════
# Health (T4.2)
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/health")
async def health() -> dict[str, Any]:
    from .llm import PROMPT_VERSION
    return {
        "ok": True,
        "service": "founder-ops-copilot",
        "prompt_version": PROMPT_VERSION,
    }


# ═══════════════════════════════════════════════════════════════════════
# Ingest (T4.2, R-1..R-4)
# ═══════════════════════════════════════════════════════════════════════

@app.post("/api/ingest", status_code=201)
async def api_ingest(file: UploadFile = File(...)) -> dict[str, Any]:
    raw = await file.read()
    return await ingest_file(file.filename or "unnamed", raw)


@app.post("/api/documents/{document_id}/extract")
async def api_extract(document_id: str) -> dict[str, Any]:
    return await extract_document(document_id)


# ═══════════════════════════════════════════════════════════════════════
# Proposals (T4.2, R-11..R-15)
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/proposals")
async def api_list_proposals(
    status: str | None = None,
    document_id: str | None = None,
) -> dict[str, Any]:
    return {"proposals": await list_proposals(status, document_id)}


@app.get("/api/proposals/{proposal_id}")
async def api_get_proposal(proposal_id: str) -> dict[str, Any]:
    return await get_proposal(proposal_id)


@app.patch("/api/proposals/{proposal_id}")
async def api_edit_proposal(
    proposal_id: str, body: ProposalEditRequest,
) -> dict[str, Any]:
    return await edit_proposal(proposal_id, body.envelope)


@app.post("/api/proposals/{proposal_id}/approve")
async def api_approve(proposal_id: str) -> dict[str, Any]:
    return await approve_proposal(proposal_id)


@app.post("/api/proposals/{proposal_id}/reject")
async def api_reject(proposal_id: str) -> dict[str, Any]:
    return await reject_proposal(proposal_id)


# ═══════════════════════════════════════════════════════════════════════
# Execute (T4.2, R-16..R-18)
# ═══════════════════════════════════════════════════════════════════════

@app.post("/api/proposals/{proposal_id}/execute")
async def api_execute(proposal_id: str) -> dict[str, Any]:
    return await execute_proposal(proposal_id)


# ═══════════════════════════════════════════════════════════════════════
# Brief + Audit (T4.2, R-19..R-22)
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/brief")
async def api_brief() -> dict[str, Any]:
    return await generate_brief()


@app.get("/api/audit")
async def api_audit(limit: int = 100) -> dict[str, Any]:
    return {"rows": await list_audit(limit)}


# ═══════════════════════════════════════════════════════════════════════
# Static mount (T4.4)
# ═══════════════════════════════════════════════════════════════════════

_DIST = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
)
if os.path.isdir(_DIST):
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="spa")
    logger.info("static_mount dir=%s", _DIST)
else:
    logger.info("no_frontend_dist — dev mode expects Vite on :5173")


# ═══════════════════════════════════════════════════════════════════════
# Dev convenience — /api/health is the only route dev needs to verify.
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/ready")
async def ready() -> dict[str, Any]:
    """Lightweight readiness probe — separate from /health so a demo can
    distinguish 'process up' from 'DB reachable'."""
    from .core import connect
    try:
        async with connect() as db:
            await db.execute("SELECT 1")
        return {"ready": True}
    except Exception as e:
        raise HTTPException(503, f"db_unreachable: {str(e)[:120]}")