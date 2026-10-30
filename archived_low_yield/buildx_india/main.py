"""aiohttp module entrypoint."""
from __future__ import annotations
import logging
from aiohttp import web
from .core import health, init as init_core, shutdown as shutdown_core
from .routes import setup_routes

logger = logging.getLogger("axelr.buildx_india.main")

async def init_module(app: web.Application) -> None:
    await init_core()
    setup_routes(app)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_get("/api/buildx/health", _health)

async def _on_cleanup(_app): await shutdown_core()

async def _health(_req):
    body = health()
    return web.json_response(body, status=200 if body["ok"] else 503)

__all__ = ["init_module"]