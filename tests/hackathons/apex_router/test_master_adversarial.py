"""Master adversarial suite for Apex Router — security, containment, protocol.

Rules for this file:
  * every test is marked `slow` — excluded from fast PR runs
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.adversarial]


# ══════════════════════════════════════════════════════════════════════════
# A. Routing Shield — must block dangerous routes
# ══════════════════════════════════════════════════════════════════════════

DANGEROUS_METHODS = ["TRACE", "CONNECT", "DEBUG", "ADMIN", "INTERNAL"]

@pytest.mark.asyncio
async def test_a1_dangerous_method_blocked(apex_router):
    """Requests with dangerous methods must be blocked."""
    for method in DANGEROUS_METHODS:
        with pytest.raises(Exception):
            await apex_router.route_request(method, "/test", {})


# ══════════════════════════════════════════════════════════════════════════
# B. Routing Containment — must not allow path traversal
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_b1_path_traversal_blocked(apex_router):
    """Path traversal attempts must be blocked."""
    traversal_paths = [
        "../../etc/passwd",
        "..%2f..%2fetc/passwd",
        "/../admin",
        "/test/../admin",
    ]
    for path in traversal_paths:
        with pytest.raises(Exception):
            await apex_router.route_request("GET", path, {})


@pytest.mark.asyncio
async def test_b2_internal_route_blocked(apex_router):
    """Internal routes must not be accessible externally."""
    with pytest.raises(Exception):
        await apex_router.route_request("GET", "/internal/config", {})


# ══════════════════════════════════════════════════════════════════════════
# C. Rate Limiting — must enforce request rate caps
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_c1_rate_limit_enforced(apex_router):
    """Excessive requests must be rate-limited."""
    from apex_agentic.actions import dispatch
    for i in range(100):
        r = await dispatch(apex_router, {"action": "route", "params": {"method": "GET", "path": "/test", "body": {}}})
        if r.get("status") == 429:
            break
    assert r.get("status") == 429 or r.get("rate_limited", False)


@pytest.mark.asyncio
async def test_c2_payload_size_capped(apex_router):
    """Request payloads must not exceed maximum size limit."""
    large_payload = {"data": "x" * (10 * 1024 * 1024)}
    from apex_agentic.actions import dispatch
    r = await dispatch(apex_router, {"action": "route", "params": {"method": "POST", "path": "/test", "body": large_payload}})
    assert r["ok"] is False or r.get("payload_too_large", False)


# ══════════════════════════════════════════════════════════════════════════
# D. Authorization — must enforce authentication and access control
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_d1_unauthenticated_blocked(apex_router):
    """Protected routes must require authentication."""
    from apex_agentic.actions import dispatch
    r = await dispatch(apex_router, {"action": "route", "params": {"method": "GET", "path": "/protected", "body": {}}})
    assert r.get("status") == 401 or r["ok"] is False


@pytest.mark.asyncio
async def test_d2_insufficient_permissions_blocked(apex_router):
    """Users must not be able to access routes outside their permissions."""
    from apex_agentic.actions import dispatch
    r = await dispatch(apex_router, {"action": "route", "params": {"method": "GET", "path": "/admin", "body": {}, "headers": {"Authorization": "user-token"}}})
    assert r.get("status") == 403 or r["ok"] is False


# ══════════════════════════════════════════════════════════════════════════
# E. Protocol Conformance — HTTP must be strictly enforced
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_e1_invalid_http_rejected(apex_router):
    """Invalid HTTP requests must be rejected."""
    from apex_agentic.actions import dispatch
    r = await dispatch(apex_router, {"invalid": "request"})
    assert r["ok"] is False


@pytest.mark.asyncio
async def test_e2_missing_required_headers_rejected(apex_router):
    """Requests missing required headers must be rejected."""
    from apex_agentic.actions import dispatch
    r = await dispatch(apex_router, {"action": "route", "params": {"method": "POST", "path": "/test", "body": {}, "headers": {}}})
    assert r["ok"] is False or r.get("missing_header", False)
