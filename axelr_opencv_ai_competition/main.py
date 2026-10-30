"""aiohttp module entrypoint."""
from __future__ import annotations

import logging

from aiohttp import web

from .core import health, init as init_core, shutdown as shutdown_core

logger = logging.getLogger("axelr.opencv_ai.main")


async def init_module(app: web.Application) -> None:
    await init_core()
    app.on_cleanup.append(_on_cleanup)
    app.router.add_get("/api/opencv/health", _health_handler)


async def _on_cleanup(_app: web.Application) -> None:
    await shutdown_core()


async def _health_handler(_request: web.Request) -> web.Response:
    body = health()
    return web.json_response(body, status=200 if body["ok"] else 503)


def init_opencv_module(app):
    return init_module(app)

__all__ = ["init_module", "init_opencv_module"]