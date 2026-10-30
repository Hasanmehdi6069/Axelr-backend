"""UGREEN AI NAS Module Entrypoint (aiohttp)."""
from __future__ import annotations

import logging
import os

from aiohttp import web

from .core import init as init_core
from .core import shutdown as shutdown_core

logger = logging.getLogger("axelr.ugreen_ai_nas.main")


async def init_module(app: web.Application, nas_root: str | None = None) -> None:
    """Initialize the AI NAS indexing system and register cleanup.

    `nas_root` comes from either the explicit argument (preferred — aiohttp
    hosts usually know the path) or the `NAS_ROOT` env var. Fails loudly if
    neither is set so we never silently index the process CWD.
    """
    root = nas_root or os.getenv("NAS_ROOT", "")
    if not root:
        raise RuntimeError("NAS_ROOT env or nas_root param required")
    await init_core(root, cloud_endpoint=os.getenv("NAS_CLOUD_ENDPOINT"))
    app.on_cleanup.append(_on_cleanup)
    logger.info("ugreen_ai_nas_module_initialized root=%s", root)


async def _on_cleanup(_app: web.Application) -> None:
    await shutdown_core()


__all__ = ["init_module"]