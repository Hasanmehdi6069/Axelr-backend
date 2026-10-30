"""aiohttp module entrypoint."""
from __future__ import annotations
import logging
import os
from aiohttp import web
from .core import health, init as init_core, shutdown as shutdown_core

logger = logging.getLogger("axelr.bharat_agentic.main")

async def init_module(app: web.Application) -> None:
    node_id = os.getenv("BHARAT_NODE_ID", "bharat-node-1")
    wal_path = os.getenv("BHARAT_WAL_PATH", "./bharat_persistence.db")
    await init_core(node_id, wal_path)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_get("/api/bharat/health", _health)

async def _on_cleanup(_app): await shutdown_core()

async def _health(_req):
    body = await health()
    return web.json_response(body, status=200 if body["ok"] else 503)

__all__ = ["init_module"]