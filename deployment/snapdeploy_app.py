# snapdeploy_app.py
"""
SnapDeploy inference service.
Runs the HotSwapEngine on port 8081, co-located with Bifrost on 8080.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.hot_swap_engine import get_hot_swap_engine

_API_KEY = (os.getenv("SNAPDEPLOY_INFERENCE_KEY") or "").strip()


class InferRequest(BaseModel):
    workspace: str = Field(default="core", max_length=32)
    task_type: str = Field(default="structuring", max_length=32)
    prompt: str = Field(..., min_length=1, max_length=4000)
    tier: str = Field(default="free", max_length=16)


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = get_hot_swap_engine()
    await engine.start()
    app.state.engine = engine
    yield
    await engine.shutdown()


app = FastAPI(title="Axelr Local Inference", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    snap = get_hot_swap_engine().snapshot()
    return {"status": "ok", **snap}


@app.post("/infer")
async def infer(req: InferRequest, request: Request) -> JSONResponse:
    if _API_KEY:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer ") or auth[7:].strip() != _API_KEY:
            raise HTTPException(status_code=401, detail="unauthorized")

    engine = request.app.state.engine
    text = await engine.infer(
        req.workspace, req.task_type, req.prompt, tier=req.tier,
    )
    return JSONResponse(
        status_code=200,
        content={
            "text": text,
            "resident": engine.snapshot().get("resident"),
        },
    )