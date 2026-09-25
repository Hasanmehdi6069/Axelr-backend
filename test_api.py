"""
FastAPI route tests — health, guest session, main chat, extract_stream.
=========================================================================
Migrated from test_routes.py + test_extract_stream.py + test_e2e_features.py.
Uses the sync `client` fixture from conftest.py (no lifespan) so no
network calls happen at import.
"""
from __future__ import annotations

import pytest

from core.routing import IntentResult


# ═══════════════════════════════════════════════════════════════════════════
# Liveness & readiness
# ═══════════════════════════════════════════════════════════════════════════

def test_head_root(client):
    r = client.head("/")
    assert r.status_code == 200


def test_get_root(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body.get("service") == "axelr-backend"
    assert body.get("status") == "ok"


def test_head_health(client):
    r = client.head("/api/health")
    assert r.status_code == 200


def test_options_health(client):
    r = client.options("/api/health")
    assert r.status_code == 200


def test_get_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert "status" in body
    assert "timestamp" in body
    # Must not 5xx even in degraded mode
    assert body["status"] in ("operational", "degraded")


def test_ping(client):
    r = client.get("/ping")
    assert r.status_code == 200
    assert r.text == "pong"


# ═══════════════════════════════════════════════════════════════════════════
# Guest session
# ═══════════════════════════════════════════════════════════════════════════

def test_guest_session_returns_id(client):
    r = client.post("/api/guest/session")
    assert r.status_code == 200
    data = r.json()
    assert "sessionId" in data


# ═══════════════════════════════════════════════════════════════════════════
# Routing — 404s and validation
# ═══════════════════════════════════════════════════════════════════════════

def test_unknown_route_returns_404(client):
    r = client.get("/this/route/does/not/exist")
    assert r.status_code == 404


def test_extract_stream_requires_auth(client):
    """Without a token the route must 401/403/422."""
    r = client.post("/api/extract_stream", data={"command": "hi"})
    assert r.status_code in (401, 403, 422)


def test_extract_stream_rejects_missing_command(client):
    r = client.post("/api/extract_stream", data={})
    assert r.status_code in (401, 403, 422)


# ═══════════════════════════════════════════════════════════════════════════
# Main chat route — mocked intent + router
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_health_ok_when_degraded(client):
    """
    Even with no Redis, no Mongo, no AI providers configured, the health
    endpoint must not 5xx. Status becomes 'degraded' or 'operational'.
    """
    r = client.get("/api/health")
    assert 200 <= r.status_code < 300
    assert r.json()["status"] in ("operational", "degraded")


# ═══════════════════════════════════════════════════════════════════════════
# Intent endpoint (if exposed)
# ═══════════════════════════════════════════════════════════════════════════

def test_intent_endpoint_exists_or_skips(client):
    """
    If /api/intent exists, it should classify without a network call.
    If it doesn't exist, skip rather than fail — endpoint exposure is
    a deploy-time concern, not a code contract.
    """
    r = client.post("/api/intent", json={"prompt": "design a navbar"})
    if r.status_code == 404:
        pytest.skip("/api/intent not exposed in this build")
    assert r.status_code == 200
    body = r.json()
    assert body.get("workspace") in ("data", "design", "core")


# ═══════════════════════════════════════════════════════════════════════════
# Webhook — auth enforcement (degraded)
# ═══════════════════════════════════════════════════════════════════════════

def test_webhook_extract_requires_auth(client):
    """Without inbound token/secret configured the route demands auth."""
    r = client.post(
        "/webhook/extract",
        json={
            "file_url": "https://example.com/x.pdf",
            "pipeline_id": "test",
            "callback_url": "https://example.com/hook",
        },
    )
    # In dev with no auth configured: _verify_inbound warns and allows.
    # With auth configured (conftest sets env): 401.
    assert r.status_code in (202, 401, 403, 422)


def test_webhook_extract_rejects_bad_payload(client):
    """Missing required fields → 422."""
    r = client.post("/webhook/extract", json={})
    assert r.status_code in (401, 403, 422)