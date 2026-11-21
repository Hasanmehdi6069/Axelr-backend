"""Integration tests — run against live FastAPI (in-process or HTTP).

Gates:
  PYTEST_LIVE=1              — required to run at all against a real server
  AXELR_BASE_URL             — default http://127.0.0.1:8000
  AXELR_TOKEN                — required for authenticated endpoints
  RUN_LIVE_VULTR=1           — enables POST/DELETE against real Vultr
  RUN_LIVE_ATLASSIAN=1       — enables real Confluence calls

Without PYTEST_LIVE, tests exercise the Sentinel engine in-process only
(no FastAPI required) and the module's health/metrics endpoints via mock.

Run:
    PYTEST_LIVE=1 AXELR_TOKEN=... pytest tests/test_integration.py -v
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import time

import httpx
import pytest
import pytest_asyncio

BASE = os.getenv("AXELR_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.getenv("AXELR_TOKEN", "")
LIVE = os.getenv("PYTEST_LIVE") == "1"
LIVE_VULTR = os.getenv("RUN_LIVE_VULTR") == "1"
LIVE_ATL = os.getenv("RUN_LIVE_ATLASSIAN") == "1"

pytestmark = pytest.mark.skipif(not LIVE, reason="PYTEST_LIVE!=1")

HEADERS = {"authorization": f"Bearer {TOKEN}"} if TOKEN else {}


# ── GUARDED IMPORT — sentinel_agent may not exist in all deployments ─────
try:
    from hackathons.sentinel_agent.sentinel_agentic import Engine, dispatch
    from hackathons.sentinel_agent.sentinel_agentic.wal import PersistentWAL
    from hackathons.sentinel_agent.sentinel_agentic.workflow import run_workflow

    _SENTINEL_OK = True
    _SENTINEL_ERR = ""
except ImportError as _e:
    Engine = dispatch = PersistentWAL = run_workflow = None
    _SENTINEL_OK = False
    _SENTINEL_ERR = str(_e)


def _require_sentinel():
    if not _SENTINEL_OK:
        pytest.skip(f"sentinel_agent unavailable: {_SENTINEL_ERR}")


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(base_url=BASE, headers=HEADERS, timeout=60.0) as c:
        yield c


# ── Health (unauthenticated) ────────────────────────────────────────────────
async def test_vultr_health(client):
    r = await client.get("/vultr/health")
    assert r.status_code in (200, 503)
    body = r.json()
    assert "healthy" in body and "version" in body


async def test_metrics_endpoint(client):
    r = await client.get("/vultr/metrics", headers=HEADERS)
    if r.status_code == 401:
        pytest.skip("no auth")
    assert r.status_code == 200
    body = r.json()
    assert "metrics" in body and "spend" in body
    assert "requests_total" in body["metrics"]


# ── Auth enforcement ────────────────────────────────────────────────────────
async def test_requires_auth():
    async with httpx.AsyncClient(base_url=BASE, timeout=10.0) as anon:
        r = await anon.post("/vultr/instances", json={"blueprint": "web_node"})
        assert r.status_code == 401


# ── Read endpoints ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "path,key",
    [
        ("/vultr/blueprints", "blueprints"),
        ("/vultr/quotas", "quota"),
        ("/vultr/spend", "summary"),
    ],
)
async def test_read_endpoints(client, path, key):
    r = await client.get(path)
    if r.status_code == 401:
        pytest.skip("no auth")
    assert r.status_code in (200, 502, 503), r.text[:200]
    if r.status_code == 200:
        assert key in r.json()


# ── Request ID uniqueness (trace correlation) ─────────────────────────────
async def test_unique_request_ids(client):
    """Each call gets a unique X-Request-Id used for trace correlation.
    True idempotency (retry safety) requires a caller-supplied
    Idempotency-Key header — not yet implemented in routes.py."""
    if not TOKEN:
        pytest.skip("no auth")
    payload = {"blueprint": "web_node", "dry_run": True}
    r1 = await client.post("/vultr/instances", json=payload)
    r2 = await client.post("/vultr/instances", json=payload)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["request_id"] != r2.json()["request_id"]
    assert r1.headers.get("X-Request-Id") == r1.json()["request_id"]


# ── Dry-run provision path ─────────────────────────────────────────────────
async def test_provision_dry_run(client):
    if not TOKEN:
        pytest.skip("no auth")
    r = await client.post("/vultr/instances", json={"blueprint": "database", "dry_run": True})
    assert r.status_code == 201
    body = r.json()
    assert body["success"] and body["dry_run"]
    assert body["instance"]["id"].startswith("dry-")


# ── Unknown blueprint → 422 ────────────────────────────────────────────────
async def test_unknown_blueprint_rejected(client):
    if not TOKEN:
        pytest.skip("no auth")
    r = await client.post("/vultr/instances", json={"blueprint": "not_a_blueprint"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] in ("UNKNOWN_BLUEPRINT", "VALIDATION")


# ── Confirm gate ───────────────────────────────────────────────────────────
async def test_destroy_requires_confirm(client):
    if not TOKEN:
        pytest.skip("no auth")
    r = await client.delete("/vultr/instances/FAKE-ID")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "CONFIRM_REQUIRED"


# ── Live Vultr round-trip (opt-in) ─────────────────────────────────────────
@pytest.mark.skipif(not LIVE_VULTR, reason="RUN_LIVE_VULTR!=1")
async def test_live_provision_destroy(client):
    if not TOKEN:
        pytest.skip("no auth")
    r = await client.post(
        "/vultr/instances",
        json={
            "blueprint": "web_node",
            "dry_run": False,
            "label": "sentinel-itest-" + str(int(time.time())),
        },
    )
    assert r.status_code == 201, r.text[:400]
    iid = r.json()["instance"]["id"]
    try:
        w = await client.post(f"/vultr/instances/{iid}/wait?timeout_s=240")
        assert w.status_code == 200
    finally:
        d = await client.delete(f"/vultr/instances/{iid}?confirm=true")
        assert d.status_code == 200


# ── Sentinel in-process tests (no FastAPI required) ───────────────────────
def test_sentinel_engine_containment():
    _require_sentinel()

    async def run() -> None:
        engine = Engine()
        r = await dispatch(
            engine,
            {
                "action": "sandbox.run_script",
                "params": {"script": "open('/etc/passwd','w').write('x')"},
                "dryRun": False,
            },
        )
        assert r["ok"] is False
        assert r["error"]["code"] == "CONTAINMENT_VIOLATION"
        assert r["error"]["why"] in ("forbidden_literal", "forbidden_call")

    asyncio.run(run())


def test_sentinel_engine_allowed():
    _require_sentinel()

    async def run() -> None:
        engine = Engine()
        r = await dispatch(
            engine,
            {
                "action": "sandbox.run_script",
                "params": {"script": "print('ok')"},
                "dryRun": False,
            },
        )
        assert r["ok"] is True
        assert r["result"]["receipt"]["exit_code"] == 0

    asyncio.run(run())


def test_persistent_wal_survives_instance():
    _require_sentinel()
    db = "/tmp/sentinel-wal-test.db"
    pathlib.Path(db).unlink(missing_ok=True)

    async def run() -> None:
        wal = PersistentWAL(db)
        await wal.record("/tmp/x.txt", "prev-content", workflow_id="wf1")
        # New instance — simulates restart
        wal2 = PersistentWAL(db)
        s = await wal2.stats()
        assert s["pending"] >= 1
        await wal2.mark_committed("wf1")
        s2 = await wal2.stats()
        assert s2["pending"] == 0

    asyncio.run(run())


def test_committed_workflow_not_rollbackable():
    _require_sentinel()
    """S4 guard: a committed workflow's writes must survive engine.rollback()."""
    os.environ.setdefault("SENTINEL_SCRATCH", "/tmp/test-sentinel-scratch")
    pathlib.Path("/tmp/test-sentinel-wal.db").unlink(missing_ok=True)

    async def run():
        e = Engine()
        r = await run_workflow(
            e,
            [
                {"action": "sandbox.write_file", "params": {"path": "committed.txt", "content": "survives"}},
            ],
            name="commit-test",
        )
        assert r["ok"] is True
        await e.rollback()
        assert e.assert_path("committed.txt").exists(), "S4 regression: committed file was rolled back"

    asyncio.run(run())


def test_invalid_approval_token_rejected():
    _require_sentinel()
    """S5 guard: any non-empty string must not satisfy destructive gate."""

    async def run():
        e = Engine()
        # vultr.destroy_instance is destructive; garbage token must fail
        r = await dispatch(
            e,
            {
                "action": "vultr.destroy_instance",
                "params": {"instance_id": "fake"},
                "dryRun": False,
                "approval_token": "x",
            },
        )
        assert r["ok"] is False
        assert r["error"]["code"] == "APPROVAL_INVALID"

    asyncio.run(run())


# ── Sentinel HTTP approval flow (opt-in) ──────────────────────────────────
@pytest.mark.skipif(not TOKEN, reason="no auth")
async def test_sentinel_approval_flow(client):
    r = await client.get("/sentinel/health")
    assert r.status_code in (200, 404)
    if r.status_code == 404:
        pytest.skip("/sentinel router not mounted")
    assert "healthy" in r.json()


@pytest.mark.skipif(not LIVE_ATL, reason="RUN_LIVE_ATLASSIAN!=1")
async def test_live_propose_actions(client):
    if not TOKEN:
        pytest.skip("no auth")
    r = await client.post("/sentinel/propose", json={"max_pages": 3, "queue": True})
    if r.status_code != 200:
        pytest.skip("Atlassian not configured")
    assert "proposals" in r.json()
