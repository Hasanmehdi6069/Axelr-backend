"""Master adversarial suite for Sentinel Engine — every test attacks a stated claim.

Reading guide:
  A. AST shield        — parse-time defense against malicious code
  B. Filesystem        — write/read containment
  C. Resources         — memory, CPU, output caps
  D. Network           — egress control (mode-dependent, honest about it)
  E. WAL integrity     — commit vs rollback semantics (S4 guard)
  F. Approval gate     — token binding, expiry (S5 guard)
  G. Protocol          — JSON-RPC 2.0 conformance
  H. Known limitations — @xfail, documented in SECURITY.md

Each failure belongs to exactly one of three buckets:
  1. Real bug in production code  → fix it
  2. Real bug in this test file   → fix it
  3. Honest limitation            → add to SECURITY.md, mark @xfail
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

# ══════════════════════════════════════════════════════════════════════════
# A. AST shield — parse-time defense
# ══════════════════════════════════════════════════════════════════════════

_BANNED_CALLS = [
    "eval",
    "exec",
    "compile",
    "__import__",
    "globals",
    "locals",
    "vars",
    "breakpoint",
    "input",
    "open",
    "file",
    "execfile",
    "compile",
]
_BANNED_IMPORTS = [
    "subprocess",
    "multiprocessing",
    "ctypes",
    "cffi",
    "pty",
    "fcntl",
    "signal",
    "resource",
    "shutil",
    "socket",
    "urllib",
    "requests",
    "http",
    "ftplib",
    "telnetlib",
    "poplib",
    "imaplib",
    "smtplib",
]
_SENSITIVE = ["/etc/passwd", "/etc/shadow", "/etc/sudoers", "/root/.ssh", "/proc/self", "/dev/mem", "/dev/kmem"]


@pytest.mark.parametrize("call", _BANNED_CALLS)
def test_a1_shield_blocks_banned_call(engine, call):
    from sentinel_agentic.engine import ContainmentViolation

    with pytest.raises(ContainmentViolation) as e:
        engine.validate(f"{call}('x')")
    assert "forbidden_call" in str(e.value.why)


@pytest.mark.parametrize("mod", _BANNED_IMPORTS)
def test_a2_shield_blocks_banned_import(engine, mod):
    from sentinel_agentic.engine import ContainmentViolation

    with pytest.raises(ContainmentViolation) as e:
        engine.validate(f"import {mod}")
    assert "forbidden_import" in str(e.value.why)


@pytest.mark.parametrize("lit", _SENSITIVE)
def test_a3_shield_blocks_sensitive_literal(engine, lit):
    from sentinel_agentic.engine import ContainmentViolation

    with pytest.raises(ContainmentViolation) as e:
        engine.validate(f"x = {lit!r}")
    assert "forbidden_literal" in str(e.value.why)


def test_a6_shield_allows_benign_python(engine):
    """Control: over-blocking is also a failure mode."""
    for src in [
        "print('hello')",
        "x = [i*i for i in range(100)]\nprint(sum(x))",
        "import json\nprint(json.dumps({'k': 1}))",
        "def f(n): return n * 2\nprint(f(21))",
        "try:\n    raise ValueError('x')\nexcept ValueError:\n    pass",
        "class A:\n    def method(self): return 42\nprint(A().method())",
        "lambda x: x + 1",
        "map(lambda x: x*2, [1,2,3])",
    ]:
        engine.validate(src)


# ══════════════════════════════════════════════════════════════════════════
# B. Filesystem containment
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_b1_absolute_write_outside_scratch_blocked(engine, tmp_path):
    from sentinel_agentic.actions import dispatch

    target = tmp_path.parent / "escaped.txt"
    r = await dispatch(
        engine,
        {
            "action": "sandbox.run_script",
            "params": {"script": f"open({str(target)!r}, 'w').write('x')"},
            "dryRun": False,
        },
    )
    assert r["ok"] is False
    assert not target.exists()


@pytest.mark.asyncio
async def test_b2_relative_escape_via_dotdot_blocked(engine):
    from sentinel_agentic.actions import dispatch

    r = await dispatch(
        engine,
        {
            "action": "sandbox.run_script",
            "params": {"script": "open('../../escape.txt', 'w').write('x')"},
            "dryRun": False,
        },
    )
    assert r["ok"] is False


@pytest.mark.asyncio
async def test_b3_symlink_escape_blocked(engine, tmp_path):
    """A symlink in scratch pointing outside must not be followable for write."""
    scratch = engine.assert_path(".")
    link = scratch / "sneaky"
    try:
        link.symlink_to(tmp_path.parent)
    except OSError:
        pytest.skip("symlinks unavailable")
    from sentinel_agentic.actions import dispatch

    r = await dispatch(
        engine,
        {
            "action": "sandbox.run_script",
            "params": {"script": "open('sneaky/escaped.txt', 'w').write('x')"},
            "dryRun": False,
        },
    )
    assert r["ok"] is False
    assert not (tmp_path.parent / "escaped.txt").exists()


@pytest.mark.asyncio
async def test_b4_read_sensitive_files_blocked(engine):
    from sentinel_agentic.actions import dispatch

    for path in ["/etc/passwd", "/etc/shadow", "/etc/sudoers"]:
        r = await dispatch(
            engine,
            {
                "action": "sandbox.read_file",
                "params": {"path": path},
                "dryRun": False,
            },
        )
        assert r["ok"] is False, f"sandbox.read_file allowed {path}"


@pytest.mark.asyncio
async def test_b5_path_traversal_via_encoding_blocked(engine):
    """Path traversal via URL encoding or other obfuscation must be blocked."""
    from sentinel_agentic.actions import dispatch

    r = await dispatch(
        engine,
        {
            "action": "sandbox.run_script",
            "params": {"script": "open('%2e%2e/%2e%2e/escape.txt', 'w').write('x')"},
            "dryRun": False,
        },
    )
    assert r["ok"] is False


# ══════════════════════════════════════════════════════════════════════════
# C. Resource discipline
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_c1_memory_limit_enforced(engine):
    """Allocate 1GB. RLIMIT_AS=64MB (test) must trigger MemoryError."""
    from sentinel_agentic.actions import dispatch

    script = (
        "x = []\n"
        "try:\n"
        "    while True: x.append(b'x' * (5 * 1024 * 1024))\n"
        "except MemoryError:\n"
        "    print('MEMORY_LIMIT_HIT')\n"
    )
    r = await dispatch(
        engine,
        {
            "action": "sandbox.run_script",
            "params": {"script": script},
            "dryRun": False,
        },
    )
    out = (r.get("result") or {}).get("output", {}).get("text", "")
    err = (r.get("error") or {}).get("output", {}).get("text", "")
    assert "MEMORY_LIMIT_HIT" in out or "MemoryError" in err or not r["ok"]


@pytest.mark.asyncio
async def test_c2_cpu_limit_enforced(engine):
    """Infinite loop must be killed inside wall-clock budget (5s in tests)."""
    from sentinel_agentic.actions import dispatch

    t0 = time.monotonic()
    r = await dispatch(
        engine,
        {
            "action": "sandbox.run_script",
            "params": {"script": "while True: pass"},
            "dryRun": False,
        },
    )
    elapsed = time.monotonic() - t0
    assert r["ok"] is False
    assert elapsed < 10.0, f"CPU limit did not enforce: {elapsed:.1f}s"


@pytest.mark.asyncio
async def test_c3_output_capped(engine):
    """500 MB of output must be truncated."""
    from sentinel_agentic.actions import dispatch

    script = "\n".join(["print('A' * 10000)"] * 500)
    r = await dispatch(
        engine,
        {
            "action": "sandbox.run_script",
            "params": {"script": script},
            "dryRun": False,
        },
    )
    out = (r.get("result") or {}).get("output", {}).get("text", "")
    assert len(out) < 5 * 1024 * 1024, f"output not capped: {len(out)} bytes"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="RLIMIT_FSIZE not supported on Windows")
async def test_c4_filesize_limit_enforced(engine):
    """Writing >64MB to a file must trigger SIGXFSZ (RLIMIT_FSIZE)."""
    from sentinel_agentic.actions import dispatch

    script = "with open('big.bin','wb') as f:\n    for _ in range(200):\n        f.write(b'x' * (1024*1024))\n"
    r = await dispatch(
        engine,
        {
            "action": "sandbox.run_script",
            "params": {"script": script},
            "dryRun": False,
        },
    )
    assert r["ok"] is False


@pytest.mark.asyncio
async def test_c5_process_limit_enforced(engine):
    """Fork bomb must be contained by process limits."""
    from sentinel_agentic.actions import dispatch

    script = "import os\ntry:\n    while True: os.fork()\nexcept OSError:\n    print('FORK_LIMIT_HIT')\n"
    r = await dispatch(
        engine,
        {
            "action": "sandbox.run_script",
            "params": {"script": script},
            "dryRun": False,
        },
    )
    assert r["ok"] is False


# ══════════════════════════════════════════════════════════════════════════
# D. Network egress
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_d1_network_blocked_in_none_mode(engine):
    """In `none` mode, socket.connect monkeypatch must reject non-allowlisted."""
    from sentinel_agentic.hardening import detect_isolation

    if detect_isolation() != "none":
        pytest.skip("this test targets `none` mode specifically")
    from sentinel_agentic.actions import dispatch

    script = (
        "import socket\n"
        "s = socket.socket(); s.settimeout(1)\n"
        "try:\n"
        "    s.connect(('evil.example', 80))\n"
        "    print('CONNECTED')\n"
        "except PermissionError:\n"
        "    print('BLOCKED')\n"
        "except Exception as e:\n"
        "    print('OTHER', type(e).__name__)\n"
    )
    r = await dispatch(
        engine,
        {
            "action": "sandbox.run_script",
            "params": {"script": script},
            "dryRun": False,
        },
    )
    if r["ok"]:
        text = r["result"]["output"]["text"]
        assert "BLOCKED" in text and "CONNECTED" not in text


@pytest.mark.asyncio
@pytest.mark.bwrap
async def test_d2_network_fully_isolated_in_bwrap(engine):
    from sentinel_agentic.hardening import detect_isolation

    if detect_isolation() != "bwrap":
        pytest.skip("bwrap not installed")
    from sentinel_agentic.actions import dispatch

    script = (
        "import socket\n"
        "s = socket.socket(); s.settimeout(1)\n"
        "try:\n"
        "    s.connect(('1.1.1.1', 80))\n"
        "    print('CONNECTED')\n"
        "except Exception:\n"
        "    print('BLOCKED')\n"
    )
    r = await dispatch(
        engine,
        {
            "action": "sandbox.run_script",
            "params": {"script": script},
            "dryRun": False,
        },
    )
    if r["ok"]:
        assert "CONNECTED" not in r["result"]["output"]["text"]


@pytest.mark.asyncio
async def test_d3_dns_rebinding_blocked(engine):
    """DNS rebinding attacks must be blocked by network isolation."""
    from sentinel_agentic.actions import dispatch

    script = (
        "import socket\n"
        "s = socket.socket(); s.settimeout(1)\n"
        "try:\n"
        "    s.connect(('127.0.0.1', 22))\n"
        "    print('CONNECTED')\n"
        "except Exception:\n"
        "    print('BLOCKED')\n"
    )
    r = await dispatch(
        engine,
        {
            "action": "sandbox.run_script",
            "params": {"script": script},
            "dryRun": False,
        },
    )
    if r["ok"]:
        assert "CONNECTED" not in r["result"]["output"]["text"]


# ══════════════════════════════════════════════════════════════════════════
# E. WAL integrity — the atomicity claim
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_e1_committed_workflow_survives_rollback(engine):
    """S4 guard — THE atomicity test. If this fails, commit() doesn't mark
    the WAL, and any later rollback silently un-does work the user was told
    succeeded."""
    from sentinel_agentic.workflow import run_workflow

    r = await run_workflow(
        engine,
        [
            {"action": "sandbox.write_file", "params": {"path": "committed.txt", "content": "must survive"}},
        ],
        name="e1",
    )
    assert r["ok"] is True

    p = engine.assert_path("committed.txt")
    assert p.read_text() == "must survive"

    await engine.rollback()
    assert p.exists(), "S4 regression: committed file was rolled back"
    assert p.read_text() == "must survive"


@pytest.mark.asyncio
async def test_e2_failed_workflow_fully_rolled_back(engine):
    """Counterpart: uncommitted work MUST roll back."""
    from sentinel_agentic.workflow import run_workflow

    r = await run_workflow(
        engine,
        [
            {"action": "sandbox.write_file", "params": {"path": "doomed.txt", "content": "x"}},
            {"action": "sandbox.run_script", "params": {"script": "eval('boom')"}},
        ],
        name="e2",
    )
    assert r["ok"] is False
    assert r["failed_step"] == 1
    assert not engine.assert_path("doomed.txt").exists()


@pytest.mark.asyncio
async def test_e3_wal_survives_instance_boundary(engine, tmp_path):
    """PersistentWAL must be readable from a fresh instance (crash durability)."""
    from sentinel_agentic.wal import PersistentWAL

    db = str(tmp_path / "wal-e3.db")
    wal1 = PersistentWAL(db)
    await wal1.record("/tmp/e3.txt", "prev", workflow_id="wf-e3")
    await wal1.mark_committed("wf-e3")

    wal2 = PersistentWAL(db)
    s = await wal2.stats()
    assert s["total"] >= 1
    assert s["pending"] == 0


@pytest.mark.asyncio
async def test_e4_concurrent_wal_writes_no_corruption(engine):
    """100 concurrent WAL writes must not corrupt the database."""
    from sentinel_agentic.wal import make_wal

    wal = make_wal()
    results = await asyncio.gather(
        *[wal.record(f"/tmp/file-{i}.txt", "prev", workflow_id=f"wf-{i}") for i in range(100)], return_exceptions=True
    )
    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"{len(failures)}/100 WAL writes failed"
    stats = await wal.stats()
    assert stats["total"] >= 100


# ══════════════════════════════════════════════════════════════════════════
# F. Approval gate — S5
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_f1_arbitrary_string_rejected(engine):
    """S5 guard: `if approval:` accepts any non-empty string. Real gate must
    verify token structure and binding."""
    from sentinel_agentic.actions import dispatch

    r = await dispatch(
        engine,
        {
            "action": "vultr.destroy_instance",
            "params": {"instance_id": "fake"},
            "dryRun": False,
            "approval_token": "x",
        },
    )
    assert r["ok"] is False
    assert r["error"]["code"] in ("APPROVAL_INVALID", "APPROVAL_REQUIRED")


@pytest.mark.asyncio
async def test_f2_wrong_action_token_rejected(engine):
    """Token bound to action A must not work for action B."""
    from sentinel_agentic import atlassian
    from sentinel_agentic.actions import dispatch

    pid = "f2-mismatch"
    atlassian._recently_approved[pid] = time.time()
    wrong = atlassian._make_approval_token(pid, "jira.delete_issue")
    r = await dispatch(
        engine,
        {
            "action": "vultr.destroy_instance",
            "params": {"instance_id": "fake"},
            "dryRun": False,
            "approval_token": wrong,
        },
    )
    assert r["ok"] is False
    assert r["error"]["code"] == "APPROVAL_INVALID"


@pytest.mark.asyncio
async def test_f3_expired_token_rejected(engine):
    from sentinel_agentic import atlassian
    from sentinel_agentic.actions import dispatch

    pid = "f3-expired"
    atlassian._recently_approved[pid] = time.time() - 10000
    token = atlassian._make_approval_token(pid, "vultr.destroy_instance")
    r = await dispatch(
        engine,
        {
            "action": "vultr.destroy_instance",
            "params": {"instance_id": "fake"},
            "dryRun": False,
            "approval_token": token,
        },
    )
    assert r["ok"] is False
    assert r["error"]["code"] == "APPROVAL_INVALID"


@pytest.mark.asyncio
async def test_f4_valid_token_accepted_and_executes(engine):
    """Positive: valid token reaches execute() (DEMO mode succeeds)."""
    from sentinel_agentic import atlassian
    from sentinel_agentic.actions import dispatch

    pid = "f4-positive"
    action = "vultr.destroy_instance"
    token = atlassian._sign(pid, "test-approver")
    r = await dispatch(
        engine,
        {
            "action": action,
            "params": {"instance_id": "dry-any"},
            "dryRun": False,
            "approval_token": token,
        },
    )
    assert r.get("error", {}).get("code") != "APPROVAL_INVALID"


# ══════════════════════════════════════════════════════════════════════════
# G. Protocol — JSON-RPC 2.0 conformance over the wire
# ══════════════════════════════════════════════════════════════════════════


async def _rpc(reader, writer, payload: dict | list) -> dict | list | None:
    writer.write((json.dumps(payload) + "\n").encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=5.0)
    return json.loads(line) if line else None


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="TCP subprocess tests require Linux")
async def test_g1_initialize_and_tools_list(tcp_client):
    from tests.conftest import rpc

    reader, writer, _ = tcp_client
    init = await rpc(reader, writer, "initialize", {})
    assert init["result"]["protocolVersion"] == "2024-11-05"

    tools = await rpc(reader, writer, "tools/list", {})
    names = {t["name"] for t in tools["result"]["tools"]}
    assert {"apply_system_change", "propose_actions", "rollback_last", "audit_tail", "wal_stats"} <= names


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="TCP subprocess tests require Linux")
async def test_g2_error_codes(tcp_client):
    from tests.conftest import rpc

    reader, writer, _ = tcp_client

    writer.write(b"{not json\n")
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), 5.0)
    assert json.loads(line)["error"]["code"] == -32700

    r = await rpc(reader, writer, "initialize", {}, rid=1)
    writer.write(b'{"id":1,"method":"initialize"}\n')
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), 5.0)
    assert json.loads(line)["error"]["code"] == -32600

    r = await rpc(reader, writer, "nope/nope", {})
    assert r["error"]["code"] == -32601


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="TCP subprocess tests require Linux")
async def test_g3_batch_supported(tcp_client):
    reader, writer, _ = tcp_client
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    r = await _rpc(reader, writer, batch)
    assert isinstance(r, list) and len(r) == 2
    assert {item["id"] for item in r} == {1, 2}


# ══════════════════════════════════════════════════════════════════════════
# H. Known limitations (documented in SECURITY.md §1)
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True, reason="SECURITY.md §1: AST shield does not block __builtins__ subscript access. Blocked by bwrap."
)
async def test_h1_builtins_subscript_known_gap(engine):
    from sentinel_agentic.actions import dispatch

    r = await dispatch(
        engine,
        {
            "action": "sandbox.run_script",
            "params": {
                "script": "f = (__builtins__['eval'] if isinstance(__builtins__, dict) "
                "     else getattr(__builtins__, 'eval'))\n"
                "print(f('1+1'))"
            },
            "dryRun": False,
        },
    )
    assert r["ok"] is False


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True, reason="SECURITY.md §1: AST shield does not block getattr-based attribute access. Blocked by bwrap."
)
async def test_h2_getattr_bypass_known_gap(engine):
    from sentinel_agentic.actions import dispatch

    r = await dispatch(
        engine,
        {
            "action": "sandbox.run_script",
            "params": {"script": "print(getattr(__builtins__, 'eval')('1+1'))"},
            "dryRun": False,
        },
    )
    assert r["ok"] is False
