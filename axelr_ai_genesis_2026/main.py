"""aiohttp module entrypoint."""
from __future__ import annotations
import logging
from aiohttp import web
from .core import health, init as init_core, shutdown as shutdown_core

logger = logging.getLogger("axelr.ai_genesis.main")

async def init_module(app: web.Application) -> None:
    await init_core()
    app.on_cleanup.append(_on_cleanup)
    app.router.add_get("/api/ai_genesis/health", _health)

async def _on_cleanup(_app): await shutdown_core()

async def _health(_req):
    body = health()
    return web.json_response(body, status=200 if body["ok"] else 503)

__all__ = ["init_module"]