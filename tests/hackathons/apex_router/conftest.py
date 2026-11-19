"""Fixtures for Apex Router test suites."""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent.parent / "hackathons" / "apex_router"))

import pytest_asyncio


@pytest_asyncio.fixture
async def apex_router(tmp_path: pathlib.Path, monkeypatch):
    """Fresh Apex Router instance with isolated routing table."""
    import apex_router.engine as eng_mod

    table_path = tmp_path / "routing_table.json"
    monkeypatch.setenv("APEX_ROUTER_TABLE_PATH", str(table_path))
    monkeypatch.setenv("APEX_MAX_REQUEST_SIZE", "1048576")
    monkeypatch.setenv("APEX_RATE_LIMIT", "1000")
    monkeypatch.setenv("APEX_TIMEOUT", "30")

    router = eng_mod.ApexRouter()
    await router.initialize()
    await router.add_rule("test-rule", {"path": "/api/v1/test", "target": "test-service"})
    yield router
    await router.shutdown()
