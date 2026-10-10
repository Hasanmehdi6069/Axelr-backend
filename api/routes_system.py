# api/routes_system.py
"""
AXELR — System routes (health · ping · metrics · usage)
=========================================================
Both containers mount this router. `/api/health/ping` is the ping
endpoint used by uptime monitors; it is exempt from body-size and
logging middleware so it does not pollute console metrics.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .state import state

router = APIRouter(tags=["system"])

_START_TS = time.time()


@router.get("/api/health/ping")
async def health_ping() -> JSONResponse:
    """Lightweight 200 OK. Never touches DB. Never logs."""
    return JSONResponse(
        status_code=200,
        content={"ok": True, "ts": int(time.time())},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/health/live")
async def health_live() -> JSONResponse:
    return JSONResponse(status_code=200, content={"status": "live"})


@router.get("/api/health/ready")
async def health_ready() -> JSONResponse:
    db = "connected" if state.db_available else "disconnected"
    redis = "connected" if state.redis_client else "disabled"
    overall = "operational" if state.db_available else "degraded"
    return JSONResponse(
        status_code=200,
        content={"status": overall, "db": db, "redis": redis,
                 "uptime_s": round(time.time() - _START_TS, 2)},
    )


@router.get("/api/system/memory")
async def system_memory() -> JSONResponse:
    try:
        from core.system_monitor import snapshot
        snap = snapshot()
        return JSONResponse(
            status_code=200,
            content={"success": True, **snap.to_dict()},
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e)[:200]},
        )


__all__ = ["router"]