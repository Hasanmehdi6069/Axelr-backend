"""Fixtures for Insights Engine test suites."""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent.parent / "hackathons" / "insights_engine"))

import pytest_asyncio


@pytest_asyncio.fixture
async def insights_engine(tmp_path: pathlib.Path, monkeypatch):
    """Fresh Insights Engine instance with isolated database."""
    import insights_engine.engine as eng_mod

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("INSIGHTS_DB_PATH", str(db_path))
    monkeypatch.setenv("INSIGHTS_MAX_MEMORY_MB", "128")
    monkeypatch.setenv("INSIGHTS_QUERY_TIMEOUT", "5")

    engine = eng_mod.InsightsEngine()
    await engine.initialize()
    await engine.execute_query("CREATE TABLE IF NOT EXISTS test_table (id INT, value TEXT)")
    await engine.execute_query("CREATE TABLE IF NOT EXISTS allowed_table (id INT, data TEXT)")
    yield engine
    await engine.shutdown()
