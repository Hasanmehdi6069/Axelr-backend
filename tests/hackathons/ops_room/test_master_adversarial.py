"""Master adversarial suite for Ops Room — security, containment, protocol.

Rules for this file:
  * every test is marked `slow` — excluded from fast PR runs
"""
from __future__ import annotations

import json
import os
import pathlib
import socket

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.adversarial]


# ══════════════════════════════════════════════════════════════════════════
# A. Task Shield — must block dangerous tasks
# ══════════════════════════════════════════════════════════════════════════

DANGEROUS_ACTIONS = ["execute_shell", "run_command", "delete_file", "write_system", "access_network", "privilege_escalation"]

@pytest.mark.asyncio
async def test_a1_dangerous_task_blocked(ops_room):
    """Tasks with dangerous actions must be blocked."""
    for action in DANGEROUS_ACTIONS:
        task = {"id": "dangerous-task", "action": action, "params": {}}
        with pytest.raises(Exception):
            await ops_room.submit_task(task)


# ══════════════════════════════════════════════════════════════════════════
# B. Execution Containment — tasks must not escape sandbox
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_b1_absolute_write_outside_scratch_blocked(ops_room, tmp_path):
    from ops_agentic.actions import dispatch
    target = tmp_path.parent / "escaped.txt"
    r = await dispatch(ops_room, {
        "action": "task.run",
        "params": {"task": {"action": "write_file", "params": {"path": str(target), "content": "x"}}},
        "dryRun": False,
    })
    assert r["ok"] is False
    assert not target.exists()


@pytest.mark.asyncio
async def test_b2_relative_escape_via_dotdot_blocked(ops_room):
    from ops_agentic.actions import dispatch
    r = await dispatch(ops_room, {
        "action": "task.run",
        "params": {"task": {"action": "write_file", "params": {"path": "../../escape.txt", "content": "x"}}},
        "dryRun": False,
    })
    assert r["ok"] is False


@pytest.mark.asyncio
async def test_b3_network_egress_blocked(ops_room):
    """Tasks must not be able to make outbound network connections."""
    from ops_agentic.actions import dispatch
    r = await dispatch(ops_room, {
        "action": "task.run",
        "params": {"task": {"action": "run_script", "params": {"script": "import socket; s=socket.socket(); s.connect(('8.8.8.8', 53))"}}},
        "dryRun": False,
    })
    assert r["ok"] is False


# ══════════════════════════════════════════════════════════════════════════
# C. Resource Limits — must enforce task timeouts and resource caps
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_c1_task_timeout_enforced(ops_room):
    """Long-running tasks must be killed within timeout."""
    from ops_agentic.actions import dispatch
    r = await dispatch(ops_room, {
        "action": "task.run",
        "params": {"task": {"action": "run_script", "params": {"script": "while True: pass"}}},
        "dryRun": False,
    })
    assert r["ok"] is False or r.get("timed_out", False)


@pytest.mark.asyncio
async def test_c2_memory_limit_enforced(ops_room):
    """Tasks must not exceed memory limit."""
    from ops_agentic.actions import dispatch
    script = "x = []\nwhile True: x.append(b'x' * 1024*1024)"
    r = await dispatch(ops_room, {
        "action": "task.run",
        "params": {"task": {"action": "run_script", "params": {"script": script}}},
        "dryRun": False,
    })
    assert r["ok"] is False or r.get("memory_exceeded", False)


# ══════════════════════════════════════════════════════════════════════════
# D. Authorization — must enforce role-based access control
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_d1_unauthorized_action_blocked(ops_room):
    """Regular users must not be able to perform admin actions."""
    from ops_agentic.actions import dispatch
    r = await dispatch(ops_room, {
        "action": "admin.restart_service",
        "params": {},
        "dryRun": False,
    })
    assert r["ok"] is False


@pytest.mark.asyncio
async def test_d2_task_approval_required(ops_room):
    """High-risk tasks must require approval before execution."""
    from ops_agentic.actions import dispatch
    r = await dispatch(ops_room, {
        "action": "task.run",
        "params": {"task": {"action": "delete_file", "params": {"path": "/important/file.txt"}}},
        "dryRun": False,
    })
    assert r["ok"] is False or r.get("requires_approval", False)


# ══════════════════════════════════════════════════════════════════════════
# E. Protocol Conformance — JSON-RPC 2.0 must be strictly enforced
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_e1_rpc_invalid_json_rejected(ops_room):
    """Non-JRPC requests must be rejected."""
    from ops_agentic.actions import dispatch
    r = await dispatch(ops_room, {"invalid": "request"})
    assert r["ok"] is False


@pytest.mark.asyncio
async def test_e2_rpc_missing_method_rejected(ops_room):
    """JRPC requests without method must be rejected."""
    from ops_agentic.actions import dispatch
    r = await dispatch(ops_room, {"jsonrpc": "2.0", "id": 1})
    assert r["ok"] is False


@pytest.mark.asyncio
async def test_e3_rpc_version_validation(ops_room):
    """JRPC must require jsonrpc version 2.0."""
    from ops_agentic.actions import dispatch
    r = await dispatch(ops_room, {"jsonrpc": "1.0", "method": "task.run", "params": {}, "id": 1})
    assert r["ok"] is False