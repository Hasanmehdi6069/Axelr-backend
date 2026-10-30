"""Standalone entrypoint — `python -m axelr_nebius_nvidia.main`."""
from __future__ import annotations

import logging
import os

from aiohttp import web

from .core import init as init_bridge
from .routes import on_shutdown, on_startup, register_routes

logger = logging.getLogger("axelr.nebius_nvidia.main")


def create_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_shutdown)
    register_routes(app)
    return app


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    app = create_app()
    web.run_app(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
    )