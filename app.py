# api/warhead.py — canonical app factory
"""AXELR AI — Canonical FastAPI Application Assembly."""

from __future__ import annotations

import datetime as _dt
import logging
import os
import sys

import structlog
from dotenv import load_dotenv

load_dotenv(override=False)  # UTF-8 default

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(message)s",
    stream=sys.stdout,
)
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
from api.lifecycle import attach_boot, lifespan
from api.middleware import (
    register_exception_handlers,
    register_middleware,
    register_monitor_routes,
)
from api.routes_admin import router as admin_router
from api.routes_chat import router as chat_router
from api.routes_ops import router as ops_router
from api.routes_tools import router as tools_router
from api.state import state


def _allowed_origins() -> list[str]:
    configured = (ORIGIN or "https://axelr.in").strip().rstrip("/")
    return list(
        dict.fromkeys(
            [
                configured,
                "https://axelr.in",
                "http://localhost:5500",
                "http://127.0.0.1:5500",
                "https://axelr-backend.onrender.com",
            ]
        )
    )


def create_app() -> FastAPI:
    app = FastAPI(title="AXELR Unified", version="26.0", lifespan=lifespan)

    register_middleware(app, _allowed_origins())
    register_exception_handlers(app)
    register_monitor_routes(app)

    app.include_router(auth_router)
    app.include_router(enhancer_router)
    app.include_router(chat_router)
    app.include_router(tools_router)
    app.include_router(admin_router)
    app.include_router(ops_router)

    # Optional routers (degrade gracefully)
    try:
        from core.dag_orchestrator import router as adjudicator_router

        app.include_router(adjudicator_router)
    except Exception as e:
        logger.warning("adjudicator_router_unavailable error=%s", e)

    try:
        from api.qstash_webhook import init_instrumentation
        from api.qstash_webhook import router as qstash_router

        app.include_router(qstash_router)
        init_instrumentation(app)
        logger.info("qstash_webhook_router_mounted")
    except Exception as e:
        logger.warning("qstash_router_unavailable error=%s", e)

    try:
        from api.ai_router import route_ai_request_parallel
        from api.config import get_http_client
        from core.io_pipeline import make_webhook_router

        app.include_router(make_webhook_router(route_ai_request_parallel, http_client=get_http_client()))
        logger.info("webhook_router_mounted")
    except Exception as e:
        logger.warning("webhook_router_unavailable error=%s", e)

    # Prometheus
    @app.get("/metrics", include_in_schema=False)
    async def _metrics_endpoint():
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    # Unified health check — uses `state`, not `app_state`
    @app.get("/health", summary="Overall system health check")
    async def health_check():
        module_statuses = {}
        overall_healthy = True

        module_statuses["axelr_core"] = {
            "status": "healthy",
            "message": "Core infrastructure operational",
        }

        db_healthy = state.db_available and state.client is not None
        redis_healthy = state.redis_client is not None
        module_statuses["database_layer"] = {
            "status": "healthy" if (db_healthy and redis_healthy) else "degraded",
            "mongo_available": db_healthy,
            "redis_available": redis_healthy,
        }
        if not db_healthy or not redis_healthy:
            overall_healthy = False

        modules_to_check = [
            ("axelr_nebius_nvidia", "Nebius × NVIDIA bridge"),
            ("axelr_paypal", "PayPal billing module"),
            ("axelr_vultr", "Vultr cloud infrastructure"),
            ("axelr_insights_engine", "Insights analytics engine"),
            ("axelr_bharat_agentic", "Bharat agentic framework"),
            ("axelr_ugreen_ai_nas", "UGREEN NAS storage"),
        ]

        for module_name, description in modules_to_check:
            try:
                __import__(module_name)
                module_statuses[module_name] = {
                    "status": "healthy",
                    "message": f"{description} imported successfully",
                }
            except Exception as e:
                # Non-fatal — modules are optional
                module_statuses[module_name] = {
                    "status": "unavailable",
                    "message": f"{description} not installed",
                    "error": str(e)[:120],
                }

        return {
            "status": "healthy" if overall_healthy else "degraded",
            "version": "26.0",
            "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "modules": module_statuses,
        }

    @app.get("/status/{module_name}", summary="Module health detail")
    async def module_health(module_name: str):
        try:
            __import__(module_name)
            mod = sys.modules[module_name]
            # Try to call the module's health() if available
            health_attr = getattr(mod, "health", None)
            if health_attr is not None:
                import asyncio as _a

                if _a.iscoroutinefunction(health_attr):
                    return {"module": module_name, **(await health_attr())}
                return {"module": module_name, **health_attr()}
            return {"module": module_name, "status": "available"}
        except Exception as e:
            return {"module": module_name, "status": "unavailable", "error": str(e)[:200]}

    # Boot layer (contract + audit)
    with _contextlib_suppress():
        attach_boot(app)

    return app


def _contextlib_suppress():
    import contextlib

    return contextlib.suppress(Exception)


app = create_app()
logger.info("axelr_app_initialized")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)


__all__ = ["app", "create_app"]
