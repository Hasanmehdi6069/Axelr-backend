"""
AXELR Test Fixtures — single source of truth.
=============================================
Forces the app into degraded mode *before* import so no test ever touches
Redis, Mongo, or an LLM provider. All fixtures are opt-in.

Design goals:
  - Zero network. Ever.
  - Zero Mongo. Ever.
  - Deterministic. Same inputs → same outputs, always.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Force degraded mode BEFORE any project import
# ---------------------------------------------------------------------------
os.environ.setdefault("ENV", "dev")
os.environ.setdefault("JWT_SECRET", "test-secret-key-xxxxxxxxxxxxxxxxxxxxxxxxxx")
os.environ.setdefault("ENABLE_INTENT_CLASSIFIER", "false")
os.environ.setdefault("ENABLE_CONTEXT_REGISTRY", "false")
os.environ.setdefault("ENABLE_CRITIC", "false")
os.environ.setdefault("ENABLE_SELF_HEAL", "false")
os.environ.setdefault("ENABLE_BLAST_RADIUS", "false")
os.environ.setdefault("ENABLE_PR_DEFENSE", "false")
os.environ.setdefault("MONGO_URI", "")
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("WORKER_URL", "")
os.environ.setdefault("DATA_WORKER_URL", "")
os.environ.setdefault("WEBHOOK_INBOUND_TOKEN", "test-token")
os.environ.setdefault("WEBHOOK_INBOUND_SECRET", "test-secret")
os.environ.setdefault("WEBHOOK_CALLBACK_SECRET", "test-callback-secret")

# Ensure `import app` / `import core` resolve from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# FastAPI client fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def client():
    """
    TestClient WITHOUT lifespan — liveness endpoints don't need it,
    and lifespan would try to open Redis/Mongo connections.
    """
    from fastapi.testclient import TestClient
    from app import app
    return TestClient(app)


@pytest.fixture(scope="module")
def anyio_backend():
    """Force asyncio backend for httpx AsyncClient tests."""
    return "asyncio"


# ---------------------------------------------------------------------------
# Orchestrator route-fn stub
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_route_fn():
    """
    A route_fn stub returning canned responses keyed on the prompt.
    Callers register expected responses via ``.set_response(marker, reply)``.
    """
    calls: list[dict] = []

    async def _route(
        workspace: str,
        task_type: str,
        prompt: str,
        history: Any,
        files: Any,
        max_tokens: int,
        temp: float,
        tier: str,
        user: Any,
        context: str = "",
    ) -> dict[str, Any]:
        calls.append({
            "workspace": workspace,
            "task_type": task_type,
            "prompt": prompt,
            "max_tokens": max_tokens,
            "temp": temp,
        })
        for marker, reply in _route.responses.items():
            if marker in prompt:
                return {
                    "success": True,
                    "text": reply,
                    "provider": "stub",
                    "model_used": "stub",
                    "tokens_used": len(reply.split()),
                    "latency_ms": 1.0,
                }
        return {
            "success": True,
            "text": _route.default,
            "provider": "stub",
            "model_used": "stub",
            "tokens_used": 1,
            "latency_ms": 1.0,
        }

    _route.responses: dict[str, str] = {}
    _route.default = "DEFAULT_STUB_REPLY"
    _route.calls = calls
    _route.set_response = lambda marker, reply: _route.responses.__setitem__(marker, reply)
    _route.reset = lambda: (calls.clear(), _route.responses.clear())

    return _route


# ---------------------------------------------------------------------------
# User stub
# ---------------------------------------------------------------------------
@pytest.fixture
def stub_user():
    return {
        "_id": "u_test_0001",
        "email": "test@example.com",
        "tier": "free",
        "enhancerQuota": {"date": "1970-01-01", "count": 0, "limit": 3},
    }


# ---------------------------------------------------------------------------
# Cache-clearing fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def clear_caches():
    """
    Clear in-process caches between tests.

    Only `ai_cache` is guaranteed to exist in the current app.py. The
    provider/model failure trackers were removed when the circuit breaker
    moved to core.infra.CircuitBreaker, so we clear whatever we can find
    and never assume a symbol exists.
    """
    try:
        import app
    except Exception:
        yield
        return

    caches = []
    for name in ("ai_cache", "provider_failures", "provider_last_fail",
                 "model_failures", "model_last_fail"):
        obj = getattr(app, name, None)
        if obj is not None and hasattr(obj, "clear"):
            caches.append(obj)

    for c in caches:
        try:
            c.clear()
        except Exception:
            pass
    yield
    for c in caches:
        try:
            c.clear()
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Worker-client cleanup (httpx global client in core.code)
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_worker_client():
    """
    NOTE: intentionally SYNC. `close_worker_client()` is async, but we only
    need to null the global reference — the actual socket close happens on
    the next event loop's teardown. Making this sync avoids the
    `_finalizers` clash between pytest-asyncio and anyio plugins.
    """
    yield
    try:
        import core.code as code_mod
        code_mod._HTTP = None  # drop the lazy client; GC handles the socket
    except Exception:
        pass

    # conftest.py (append)
def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", default=False,
                     help="Run live-network tests")

def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="Live test — pass --live to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)