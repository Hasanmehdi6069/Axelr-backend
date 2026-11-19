"""Fixtures for Ops Room test suites."""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent.parent / "hackathons" / "ops_room"))

import pytest_asyncio


@pytest_asyncio.fixture
async def ops_room(tmp_path: pathlib.Path, monkeypatch):
    """Fresh Ops Room instance with isolated task queue."""
    import ops_room.engine as eng_mod

    queue_path = tmp_path / "task_queue.db"
    scratch_path = tmp_path / "scratch"
    scratch_path.mkdir()

    monkeypatch.setenv("OPS_QUEUE_PATH", str(queue_path))
    monkeypatch.setenv("OPS_SCRATCH_ROOT", str(scratch_path))
    monkeypatch.setenv("OPS_MAX_TASKS", "1000")
    monkeypatch.setenv("OPS_MAX_MEMORY_MB", "256")
    monkeypatch.setenv("OPS_TASK_TIMEOUT", "30")

    room = eng_mod.OpsRoom()
    await room.initialize()
    yield room
    await room.shutdown()
