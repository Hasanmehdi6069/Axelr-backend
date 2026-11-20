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
_SOVEREIGN_ENGINE = _ROOT / "core" / "axelr-sovereign.engine"
_HACKATHONS_DIR = _ROOT / "hackathons"
_BHARAT_AGENT_DIR = _HACKATHONS_DIR / "axelr_bharat_civic_agent"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_SOVEREIGN_ENGINE) not in sys.path:
    sys.path.insert(0, str(_SOVEREIGN_ENGINE))
if str(_HACKATHONS_DIR) not in sys.path:
    sys.path.insert(0, str(_HACKATHONS_DIR))
if str(_BHARAT_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_BHARAT_AGENT_DIR))


# ─── Markers ─────────────────────────────────────────────────────────────────
def pytest_configure(config):
    markers = [
        "unit: pure logic, no I/O",
        "contract: wire/schema shapes",
        "security: adversarial inputs",
        "chaos: fault injection",
        "quality: conformance, gates, evals",
        "e2e: end-to-end journeys",
        "slow: > 2s wall-clock",
        "requires_network: hits a real external service",
        "live: real providers (opt-in via --live)",
        "perf: slow wall-clock performance tests (skipped by default)",
        "stress: sustained load tests (skipped by default)",
    ]
    for line in markers:
        config.addinivalue_line("markers", line)


# ─── --live flag ─────────────────────────────────────────────────────────────
def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", default=False,
                     help="Run live-network tests")


def pytest_collection_modifyitems(config, items):
    # Handle --live flag
    if not config.getoption("--live"):
        skip = pytest.mark.skip(reason="live test — pass --live to run")
        for item in items:
            if "live" in item.keywords:
                item.add_marker(skip)
    
    # Handle perf and stress markers
    skip_perf = not config.getoption("-m") or "perf" not in config.getoption("-m")
    skip_stress = not config.getoption("-m") or "stress" not in config.getoption("-m")
    for item in items:
        if skip_perf and "perf" in item.keywords:
            item.add_marker(pytest.mark.skip(reason="perf test — run with -m perf"))
        if skip_stress and "stress" in item.keywords:
            item.add_marker(pytest.mark.skip(reason="stress test — run with -m stress"))


# ─── FastAPI client (no lifespan → no Redis/Mongo connection) ────────────────
@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from api.main import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="session")
def app():
    from api.main import app
    return app


@pytest.fixture
def authed_headers():
    """Fixture for authenticated request headers."""
    return {"Authorization": "Bearer test-token"}


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
    """Reset all singletons/LRUs that can leak state across tests."""
    # Clear API state caches
    try:
        import api.state as state_mod
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
    except Exception:
        pass
    
    # Clear core cache module
    try:
        from core import cache
        for name in ("SemanticCache", "BM25Cache"):
            cls = getattr(cache, name, None)
            if cls and hasattr(cls, "_reset_for_tests"):
                cls._reset_for_tests()
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


@pytest.fixture
def tmp_consent_dir(tmp_path):
    """Isolated consent store dir per test."""
    return tmp_path / "consent"


# ══════════════════════════════════════════════════════════════════════════
# HACKATHON MODULE FIXTURES - SINGLE SOURCE OF TRUTH
# ══════════════════════════════════════════════════════════════════════════

# ─── Sentinel Engine Fixture ───────────────────────────────────────────────
@pytest.fixture
async def engine(tmp_path: Path, monkeypatch):
    """Fresh Sentinel Engine, isolated scratch/WAL/audit. Fast wall-clock for tests."""
    import sentinel_agentic.engine as eng_mod

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    audit = tmp_path / "audit.jsonl"
    wal_db = tmp_path / "wal.db"

    monkeypatch.setattr(eng_mod, "SANDBOX_ROOT", scratch.resolve())
    monkeypatch.setattr(eng_mod, "AUDIT_PATH", audit)
    monkeypatch.setattr(eng_mod, "WALL_CLOCK_S", 5)
    monkeypatch.setattr(eng_mod, "MAX_MEMORY_MB", 64)
    monkeypatch.setattr(eng_mod, "MAX_CPU_S", 10)
    monkeypatch.setenv("SENTINEL_WAL_BACKEND", "sqlite")
    monkeypatch.setenv("SENTINEL_WAL_PATH", str(wal_db))
    monkeypatch.setenv("SENTINEL_DEMO", "1")
    monkeypatch.setenv("SENTINEL_ISOLATION", os.getenv("PYTEST_ISOLATION", "none"))

    yield eng_mod.Engine()


# ─── Vultr Fixture ─────────────────────────────────────────────────────────
@pytest.fixture
async def vultr_client(tmp_path: Path, monkeypatch):
    """Fresh Vultr client with isolated state."""
    import vultr_integration.client as eng_mod

    state_path = tmp_path / "vultr_state.json"
    monkeypatch.setenv("VULTR_STATE_PATH", str(state_path))
    monkeypatch.setenv("VULTR_MAX_INSTANCES", "100")
    monkeypatch.setenv("VULTR_RATE_LIMIT", "100")

    client = eng_mod.VultrClient()
    await client.initialize()
    yield client
    await client.shutdown()


# ─── Apex Router Fixture ────────────────────────────────────────────────────
@pytest.fixture
async def apex_router(tmp_path: Path, monkeypatch):
    """Fresh Apex Router instance with isolated routing table."""
    import apex_router.engine as eng_mod

    table_path = tmp_path / "routing_table.json"
    monkeypatch.setenv("APEX_ROUTER_TABLE_PATH", str(table_path))
    monkeypatch.setenv("APEX_MAX_REQUEST_SIZE", "1048576")
    monkeypatch.setenv("APEX_RATE_LIMIT", "1000")
    monkeypatch.setenv("APEX_TIMEOUT", "30")

    router = eng_mod.ApexRouter()
    await router.initialize()
    await router.add_rule("test-rule", {"path": "/api/v1/test", "target": "test-service"})
    yield router
    await router.shutdown()


# ─── Insights Engine Fixture ───────────────────────────────────────────────
@pytest.fixture
async def insights_engine(tmp_path: Path, monkeypatch):
    """Fresh Insights Engine instance with isolated database."""
    import sys
    insights_dir = Path(__file__).parent.parent / "hackathons" / "axelr_insights_engine"
    if str(insights_dir) not in sys.path:
        sys.path.insert(0, str(insights_dir))
    import engine.engine as eng_mod

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("INSIGHTS_DB_PATH", str(db_path))
    monkeypatch.setenv("INSIGHTS_MAX_MEMORY_MB", "128")
    monkeypatch.setenv("INSIGHTS_QUERY_TIMEOUT", "5")

    engine = eng_mod.InsightsEngine()
    await engine.init()
    yield engine


# ─── Ops Room Fixture ──────────────────────────────────────────────────────
@pytest.fixture
async def ops_room(tmp_path: Path, monkeypatch):
    """Fresh Ops Room instance with isolated task queue."""
    import sys
    ops_dir = Path(__file__).parent.parent / "hackathons" / "axelr_ops_room"
    if str(ops_dir) not in sys.path:
        sys.path.insert(0, str(ops_dir))
    import agent.engine as eng_mod

    queue_path = tmp_path / "task_queue.db"
    scratch_path = tmp_path / "scratch"
    scratch_path.mkdir()

    monkeypatch.setenv("OPS_QUEUE_PATH", str(queue_path))
    monkeypatch.setenv("OPS_SCRATCH_ROOT", str(scratch_path))
    monkeypatch.setenv("OPS_MAX_TASKS", "1000")
    monkeypatch.setenv("OPS_MAX_MEMORY_MB", "256")
    monkeypatch.setenv("OPS_TASK_TIMEOUT", "30")

    room = eng_mod.OpsEngine()
    await room.init()
    yield room


# ─── Bharat Agent Fixture ──────────────────────────────────────────────────
@pytest.fixture
async def bharat_agent(tmp_path: Path, monkeypatch):
    """Fresh Bharat Agent instance with isolated state."""
    import bharat_agentic.engine as eng_mod

    state_path = tmp_path / "bharat_state.json"
    monkeypatch.setenv("BHARAT_STATE_PATH", str(state_path))
    monkeypatch.setenv("BHARAT_MAX_REQUESTS", "1000")
    monkeypatch.setenv("BHARAT_TIMEOUT", "60")

    agent = eng_mod.CivicAgent(node_id="test")
    await agent.__aenter__()
    yield agent
    await agent.__aexit__(None, None, None)


# ─── Buildx Fixture ────────────────────────────────────────────────────────
@pytest.fixture
async def buildx_engine(tmp_path: Path, monkeypatch):
    """Fresh Buildx engine instance with isolated build state."""
    import buildx.engine as eng_mod

    build_path = tmp_path / "builds"
    build_path.mkdir()
    monkeypatch.setenv("BUILDX_BUILD_ROOT", str(build_path))
    monkeypatch.setenv("BUILDX_MAX_PARALLEL", "50")
    monkeypatch.setenv("BUILDX_TIMEOUT", "300")

    engine = eng_mod.BuildxEngine()
    await engine.initialize()
    yield engine
    await engine.shutdown()


# ─── Inbox Engine Fixture ──────────────────────────────────────────────────
@pytest.fixture
async def inbox_engine(tmp_path: Path, monkeypatch):
    """Fresh Inbox Engine instance with isolated storage."""
    import inbox.engine as eng_mod

    storage_path = tmp_path / "inbox_storage.db"
    monkeypatch.setenv("INBOX_STORAGE_PATH", str(storage_path))
    monkeypatch.setenv("INBOX_MAX_MESSAGES", "10000")
    monkeypatch.setenv("INBOX_RETENTION_DAYS", "30")

    engine = eng_mod.InboxEngine()
    await engine.initialize()
    yield engine
    await engine.shutdown()


# ─── PayPal Fixture ────────────────────────────────────────────────────────
@pytest.fixture
async def paypal_client(tmp_path: Path, monkeypatch):
    """Fresh PayPal client with isolated transaction state."""
    import paypal_integration.client as eng_mod

    state_path = tmp_path / "paypal_state.json"
    monkeypatch.setenv("PAYPAL_STATE_PATH", str(state_path))
    monkeypatch.setenv("PAYPAL_MAX_TRANSACTIONS", "1000")

    client = eng_mod.PayPalClient()
    await client.initialize()
    yield client
    await client.shutdown()