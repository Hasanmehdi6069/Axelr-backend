"""Entry points — FastAPI app and Appwrite Function handler."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from typing import Any

from fastapi import FastAPI

from .config import settings
from .routes import router
from .search import ensure_bootstrapped, search_cached
from .storage_base import close_backend, init_backend

logger = logging.getLogger("insights.main")
__version__ = "3.2.0"


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    # Import here (not at module top) so a missing dep fails at startup,
    # not at import. `check_deps` is local to this module.
    from .check_deps import run_all_checks
    if not run_all_checks():
        logger.critical("dep_checks_failed")
        raise RuntimeError("Dependency checks failed, cannot start server")

    if settings.storage_backend == "appwrite" and not settings.appwrite_configured():
        logger.warning("storage=appwrite but APPWRITE_* env missing")
    elif settings.storage_backend == "mongo" and not settings.mongo_configured():
        logger.warning("storage=mongo but MONGODB_URI missing")
    else:
        await init_backend()
        await ensure_bootstrapped()
    try:
        yield
    finally:
        await close_backend()
        # Free the search index (sparse matrix can be tens of MB).
        try:
            from . import search as _search_mod
            _search_mod._INDEX = None
            logger.info("search_index_reset")
        except Exception as e:
            logger.debug("search_index_reset_failed err=%s", e)


app = FastAPI(title="Axelr Insights", version=__version__, lifespan=lifespan)
app.include_router(router, prefix="/insights")


class _Ctx:
    def __init__(self, body: dict | None = None, headers: dict | None = None):
        class Req:
            bodyJson = body or {}
            headers_ = headers or {}
        class Res:
            def json(self, data: dict, status_code: int = 200) -> dict:
                return {"body": json.dumps(data), "statusCode": status_code}
        self.req = Req()
        self.res = Res()
        self.log = lambda m: logger.info("[fn] %s", m)
        self.error = lambda m: logger.error("[fn] %s", m)


def _validate_search(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    q = body.get("query")
    if not isinstance(q, str) or not q.strip():
        raise ValueError("query must be a non-empty string")
    q = q.strip()[:2048]
    try:
        top_k = int(body.get("top_k", settings.search_top_k_default))
    except (TypeError, ValueError):
        raise ValueError("top_k must be an integer")
    if not (1 <= top_k <= settings.search_top_k_max):
        raise ValueError(f"top_k must be 1..{settings.search_top_k_max}")
    ms = body.get("min_score")
    if ms is not None:
        try:
            ms = float(ms)
        except (TypeError, ValueError):
            raise ValueError("min_score must be a number")
        if not (0.0 <= ms <= 1.0):
            raise ValueError("min_score must be 0..1")
    return {"query": q, "top_k": top_k, "min_score": ms}


async def main(context: Any) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        params = _validate_search(context.req.bodyJson)
    except ValueError as e:
        return context.res.json({"error": str(e)}, 400)
    try:
        if settings.appwrite_configured() or settings.mongo_configured():
            await init_backend()
        hits = await search_cached(
            params["query"], top_k=params["top_k"],
            min_score=params["min_score"],
        )
    except Exception as e:
        logger.exception("search failed")
        return context.res.json(
            {"error": "internal_error", "detail": str(e)[:200]}, 500)
    return context.res.json({
        "meta": {
            "query": params["query"],
            "search_backend": settings.search_backend,
            "storage_backend": settings.storage_backend,
            "processing_time_ms": round((time.perf_counter() - t0) * 1000, 2),
        },
        "results": {"count": len(hits), "hits": hits},
    }, 200)


async def _local_demo() -> None:
    logging.basicConfig(level=logging.INFO)
    ctx = _Ctx(body={"query": "provider failover", "top_k": 3})
    print(json.dumps(json.loads((await main(ctx))["body"]), indent=2))


if __name__ == "__main__":
    asyncio.run(_local_demo())