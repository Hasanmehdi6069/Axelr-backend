"""
AXELR Test Fixtures — single source of truth.
=============================================
Forces the app into degraded mode *before* any project import so no test
ever touches Redis, Mongo, or a real LLM provider. Every fixture is opt-in.

Design goals:
    - Zero network. Ever.
    - Zero Mongo. Ever.
    - Deterministic. Same inputs → same outputs, always.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

# ─── Force degraded mode BEFORE any project import ──────────────────────────
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
os.environ.setdefault("AXELR_LOCAL_ROUTER", "on")
os.environ.setdefault("AXELR_LOCAL_VERIFY", "on")
os.environ.setdefault("AXELR_CHAOS_ENABLED", "false")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ─── Markers ─────────────────────────────────────────────────────────────────
def pytest_configure(config):
    for line in (
        "unit: pure logic, no I/O",
        "contract: wire/schema shapes",
        "security: adversarial inputs",
        "chaos: fault injection",
        "quality: conformance, gates, evals",
        "e2e: end-to-end journeys",
        "slow: > 2s wall-clock",
        "requires_network: hits a real external service",
        "live: real providers (opt-in via --live)",
    ):
        config.addinivalue_line("markers", line)


# ─── --live flag ─────────────────────────────────────────────────────────────
def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", default=False,
                     help="Run live-network tests")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="live test — pass --live to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


# ─── FastAPI client (no lifespan → no Redis/Mongo connection) ────────────────
@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient
    from api.warhead import create_app
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="session")
def app():
    from api.warhead import create_app
    return create_app()


# ─── anyio backend ───────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


# ─── Deterministic clock ─────────────────────────────────────────────────────
class _FakeClock:
    def __init__(self, start: float = 1_700_000_000.0):
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


@pytest.fixture
def fake_clock(monkeypatch):
    clk = _FakeClock()
    monkeypatch.setattr(time, "time", clk.now)
    return clk


# ─── Route-function stub ─────────────────────────────────────────────────────
@pytest.fixture
def fake_route_fn():
    """Canned route_fn keyed on prompt substring."""
    calls: list[dict] = []

    async def _route(
        workspace: str = "core", task_type: str = "structuring",
        prompt: str = "", history: Any = None, files: Any = None,
        max_tokens: int = 2048, temp: float = 0.4, tier: str = "free",
        user: Any = None, context: str = "", request: Any = None,
        **kw: Any,
    ) -> dict[str, Any]:
        calls.append({"workspace": workspace, "prompt": prompt,
                      "task_type": task_type})
        for marker, reply in _route.responses.items():
            if marker in prompt:
                return {"success": True, "text": reply, "provider": "stub",
                        "model_used": "stub", "tokens_used": len(reply.split()),
                        "latency_ms": 1.0}
        return {"success": True, "text": _route.default, "provider": "stub",
                "model_used": "stub", "tokens_used": 1, "latency_ms": 1.0}

    _route.responses: dict[str, str] = {}
    _route.default = "DEFAULT_STUB_REPLY"
    _route.calls = calls
    _route.set_response = lambda m, r: _route.responses.__setitem__(m, r)
    _route.reset = lambda: (calls.clear(), _route.responses.clear())
    return _route


# ─── Stub user ───────────────────────────────────────────────────────────────
@pytest.fixture
def stub_user():
    return {
        "_id": "u_test_0001",
        "email": "test@example.com",
        "tier": "free",
        "enhancerQuota": {"date": "1970-01-01", "count": 0, "limit": 3},
    }


# ─── Clear in-process caches between tests ───────────────────────────────────
@pytest.fixture
def clear_caches():
    try:
        import api.state as state_mod
    except Exception:
        yield
        return
    objs = []
    for name in ("ai_cache",):
        obj = getattr(state_mod.state, name, None)
        if obj is not None and hasattr(obj, "clear"):
            objs.append(obj)
    for o in objs:
        try:
            o.clear()
        except Exception:
            pass
    yield
    for o in objs:
        try:
            o.clear()
        except Exception:
            pass


# ─── Fake Redis ──────────────────────────────────────────────────────────────
@pytest.fixture
def fake_redis():
    fakeredis = pytest.importorskip("fakeredis")
    return fakeredis.aioredis.FakeRedis()


# ─── HTTP mock (respx) ───────────────────────────────────────────────────────
@pytest.fixture
def http_mock():
    respx = pytest.importorskip("respx")
    with respx.mock(assert_all_called=False) as mock:
        yield mock


# ─── Chaos probe control ─────────────────────────────────────────────────────
@pytest.fixture
def chaos(monkeypatch):
    def _enable(rate: float = 1.0, providers: str = "", delay_s: float = 0.05):
        monkeypatch.setenv("AXELR_CHAOS_ENABLED", "true")
        monkeypatch.setenv("AXELR_CHAOS_RATE", str(rate))
        monkeypatch.setenv("AXELR_CHAOS_DELAY_S", str(delay_s))
        monkeypatch.setenv("AXELR_CHAOS_PROVIDERS", providers)
        monkeypatch.setenv("AXELR_CHAOS_SEED", "42")
        import chaos_probe
        importlib.reload(chaos_probe)
        return chaos_probe

    def _disable():
        monkeypatch.setenv("AXELR_CHAOS_ENABLED", "false")
        import chaos_probe
        importlib.reload(chaos_probe)

    yield _enable, _disable
    os.environ["AXELR_CHAOS_ENABLED"] = "false"
    try:
        import chaos_probe
        importlib.reload(chaos_probe)
    except Exception:
        pass


# ─── Repo-index tmp dir ──────────────────────────────────────────────────────
@pytest.fixture
def repo_index_tmp(tmp_path):
    pkg = tmp_path / "repo"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    return pkg


# ─── SSE parser (single source of truth) ────────────────────────────────────
@pytest.fixture
def sse_parse():
    def _parse(frame: str) -> tuple[str, dict]:
        import json
        event, data_lines = "message", []
        for line in frame.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        raw = "\n".join(data_lines)
        try:
            return event, json.loads(raw) if raw else {}
        except Exception:
            return event, {"_raw": raw}
    return _parse