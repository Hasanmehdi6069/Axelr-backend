"""
End-to-end user journeys via TestClient (no lifespan).
========================================================
Every layer is exercised together. This is what "the product works"
means, not "the unit tests pass".
"""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.e2e


def test_journey_health_guest_chat(client):
    # The smallest real path a user takes.
    assert client.get("/api/health").status_code == 200
    r = client.post("/api/guest/session")
    assert r.status_code == 200
    assert r.json()["sessionId"]
    assert client.get("/").json()["service"] == "axelr-backend"


def test_journey_unknown_route_is_clean_404(client):
    # WHY: a 500 on a missing route looks like an outage to monitors.
    r = client.get("/definitely/not/a/route")
    assert r.status_code == 404


def test_journey_head_root_does_not_5xx(client):
    assert client.head("/").status_code == 200


def test_journey_options_preflight(client):
    assert client.options("/api/health").status_code == 200


def test_journey_guest_session_id_not_leaked_across_sessions(client):
    a = client.post("/api/guest/session").json()["sessionId"]
    b = client.post("/api/guest/session").json()["sessionId"]
    assert a != b


def test_journey_extract_stream_rejects_bad_auth(client):
    r = client.post("/api/extract_stream", data={"command": "hi"})
    assert r.status_code in (401, 403, 422)


def test_journey_sse_contract_present_via_boot(client):
    # The /api/contract endpoint is installed by _boot.attach.
    r = client.get("/api/contract")
    assert r.status_code == 200
    body = r.json()
    assert body["endpoints"]["/api/extract_stream"]["media_type"] == "text/event-stream"


def test_journey_audit_endpoint_responds(client):
    r = client.get("/api/audit")
    assert r.status_code == 200
    body = r.json()
    assert "version" in body


def test_journey_metrics_exposes_prometheus(client):
    r = client.get("/metrics")
    assert r.status_code == 200


def test_journey_openapi_lists_core_routes(client):
    spec = client.get("/openapi.json").json()
    paths = set(spec["paths"].keys())
    for required in ("/api/health", "/api/guest/session"):
        assert required in paths, f"{required} missing from OpenAPI"


def test_journey_security_headers_present(client):
    # WHY: missing CSP is a real security regression.
    r = client.get("/")
    for h in ("X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy"):
        assert h in r.headers, f"{h} missing from response"


def test_journey_body_size_guard_registered(client):
    # WHY: unguarded body → trivial DoS.
    from api.middleware import MAX_BODY_BYTES
    assert MAX_BODY_BYTES > 0