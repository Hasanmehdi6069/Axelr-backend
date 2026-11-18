"""Fixtures for the two master test suites.

Design rules:
  * every test gets its own scratch/, audit, and WAL — no shared /tmp state
  * SANDBOX_ROOT, AUDIT_PATH, etc. are monkeypatched on the *module* because
    engine.py resolves them at import time
  * the TCP server fixture spawns a real subprocess and kills it hard on teardown
  * HYPOTHESIS_PROFILE=ci disables deadline; local runs are allowed to be slow
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import socket
import sys
from typing import AsyncIterator

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent.parent / "hackathons" / "sentinel_agent"))

import pytest
import pytest_asyncio


# ── engine fixture ──────────────────────────────────────────────────────
@pytest_asyncio.fixture
async def engine(tmp_path: pathlib.Path, monkeypatch) -> AsyncIterator:
    """Fresh Engine, isolated scratch/WAL/audit. Fast wall-clock for tests."""
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
    monkeypatch.setenv("SENTINEL_ISOLATION",
                       os.getenv("PYTEST_ISOLATION", "none"))

    yield eng_mod.Engine()


# ── live TCP fixture ────────────────────────────────────────────────────
@pytest.fixture
def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest_asyncio.fixture
async def tcp_client(free_port: int, tmp_path: pathlib.Path
                     ) -> AsyncIterator[tuple[asyncio.StreamReader,
                                               asyncio.StreamWriter, int]]:
    """Real subprocess, real TCP connection, killed on teardown."""
    scratch = tmp_path / "tcp-scratch"
    scratch.mkdir()
    env = {
        **os.environ,
        "SENTINEL_SCRATCH": str(scratch),
        "SENTINEL_AUDIT": str(tmp_path / "tcp-audit.jsonl"),
        "SENTINEL_WAL_BACKEND": "sqlite",
        "SENTINEL_WAL_PATH": str(tmp_path / "tcp-wal.db"),
        "SENTINEL_ISOLATION": "none",
        "SENTINEL_TCP_PORT": str(free_port),
        "SENTINEL_DEMO": "1",
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "sentinel_agentic.server", "--tcp",
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    for _ in range(50):
        try:
            _, w = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", free_port), timeout=0.1)
            w.close()
            await w.wait_closed()
            break
        except (ConnectionRefusedError, asyncio.TimeoutError):
            await asyncio.sleep(0.1)
    else:
        proc.kill()
        err = await proc.stderr.read()
        raise RuntimeError(f"tcp server failed to bind: {err.decode()[:400]}")

    reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
    try:
        yield reader, writer, free_port
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        if proc.returncode is None:
            proc.send_signal(2)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()


async def rpc(reader, writer, method: str, params=None,
              rid: int | str = 1, timeout: float = 5.0) -> dict:
    """One JSON-RPC request/response round-trip over a TCP socket."""
    req = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        req["params"] = params
    writer.write((json.dumps(req) + "\n").encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout)
    return json.loads(line) if line else {"error": "empty_response"}