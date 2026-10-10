# api/warhead.py
"""
AXELR AI — Canonical FastAPI Application Assembly
==================================================
Factory-style entry point. Includes every router, mounts the webhook
pipeline and adjudicator (both guarded), attaches the boot layer.

    uvicorn api.warhead:app
    uvicorn app:app                # via the shim in root app.py
"""
from __future__ import annotations

import logging
import os
import sys

import structlog

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(message)s",
    stream=sys.stdout,
)
from dotenv import load_dotenv
load_dotenv(override=False)

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    cache_logger_on_first_use=True,
)
logger = structlog.get_logger("axelr")

from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

from api.auth import router as auth_router
from api.config import ORIGIN
from api.enhancer import router as enhancer_router
from api.lifespan import lifespan
from api.middleware import (
    register_exception_handlers,
    register_middleware,
    register_monitor_routes,
)
from api.routes_admin import router as admin_router
from api.routes_agents import router as agents_router
from api.routes_chat import router as chat_router
from api.routes_guest import router as guest_router
from api.routes_tools import router as tools_router


def _allowed_origins() -> list[str]:
    configured = (ORIGIN or "https://axelr.in").strip().rstrip("/")
    return list(dict.fromkeys([
        configured,
        "https://axelr.in",
        "http://localhost:5500",
        "http://127.0.0.1:5500",
        "https://axelr-backend.onrender.com",
    ]))


def create_app() -> FastAPI:
    """Factory — one app per call. Use in tests."""
    app = FastAPI(title="AXELR Unified", version="25.0", lifespan=lifespan)

    register_middleware(app, _allowed_origins())
    register_exception_handlers(app)
    register_monitor_routes(app)

    app.include_router(auth_router)
    app.include_router(enhancer_router)
    app.include_router(chat_router)
    app.include_router(guest_router)
    app.include_router(tools_router)
    app.include_router(agents_router)
    app.include_router(admin_router)

    # Optional routers (adjudicator + webhook) — guarded
    try:
        from app.services.dag_orchestrator import router as adjudicator_router
        app.include_router(adjudicator_router)
    except Exception as e:
        logger.warning("adjudicator_router_unavailable error=%s", e)

    try:
        from api.config import get_http_client
        from api.ai_router import route_ai_request_parallel
        from core.io_pipeline import make_webhook_router
        app.include_router(make_webhook_router(
            route_ai_request_parallel, http_client=get_http_client()))
        logger.info("webhook_router_mounted")
    except Exception as e:
        logger.warning("webhook_router_unavailable error=%s", e)

    @app.get("/metrics", include_in_schema=False)
    async def _metrics_endpoint():
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    # Wire the boot layer (contract + audit + readiness)
    try:
        from api._boot import attach as _axelr_attach
        _axelr_attach(app)
    except Exception as e:
        logger.debug("boot_attach_deferred error=%s", e)

    return app


# Uvicorn discovery contract
app = create_app()
logger.info("axelr_app_initialized")


__all__ = ["app", "create_app"]