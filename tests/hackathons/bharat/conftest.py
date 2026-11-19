"""Shared fixtures and helpers for the Bharat Civic Agent test suite.

Provides:
  - Robust seed-path resolver (works whether cases.json lives in repo
    root or seed_cases/).
  - Fully-wired `engine` fixtures (plain, rate-limited, chaos-injected).
  - Document factories in four scripts (English, Devanagari, Hinglish,
    Tamil) that double as the canonical "expected fields" fixture.
  - `seeded_engine` for tests that need N cases preloaded.
  - `assert_fields` for readable extraction-failure diagnostics.

Everything here is deterministic. No network, no real clock dependency
beyond what individual tests opt into.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from bharat_agentic import CivicAgent, EngineConfig

# ---------------------------------------------------------------------------
# Canonical documents — reused across every test module
# ---------------------------------------------------------------------------

NOTICE = (
    "IN THE COURT OF THE CHIEF JUDICIAL MAGISTRATE, PATNA\n"
    "Case No. 412/2024\n"
    "State of Bihar vs. Ramesh Kumar\n"
    "You are hereby directed to appear in person on 15/11/2026 at 10:30 AM."
)

NOTICE_HINDI = (
    "माननीय न्यायालय, पटना\n"
    "वाद संख्या 412/2024\n"
    "राज्य बनाम रमेश कुमार\n"
    "कृपया 15/11/2026 को व्यक्तिगत रूप से उपस्थित हों।"
)

NOTICE_HINGLISH = (
    "District Court, Delhi\n"
    "Case No. 45/2023\n"
    "State vs. Sunita\n"
    "Aapko 22/12/2026 ko document leke aana hai."
)

NOTICE_TAMIL = (
    "சென்னை உயர் நீதிமன்றம்\n"
    "வழக்கு எண் 890/2024\n"
    "22/12/2026 அன்று ஆஜராக வேண்டும்."
)

NOTICE_FIR_ONLY = (
    "IN THE DISTRICT COURT, DELHI\n"
    "Case No. 45/2023\n"
    "State vs. Sunita\n"
    "FIR dated 01/01/2024. Awaiting further orders."
)

EXPECTED_NOTICE: Dict[str, str] = {
    "case_number": "412/2024",
    "court": "Chief Judicial Magistrate, Patna",
    "next_hearing": "2026-11-15",
    "required_action": "appear in person",
}


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _find_seed_cases(start: Optional[Path] = None) -> Path:
    """Locate cases.json robustly — the test must not fail on layout."""
    root = (start or Path(__file__).resolve().parent)
    candidates = [
        root / "cases.json",
        root / "seed_cases" / "cases.json",
        root.parent / "cases.json",
        root.parent / "seed_cases" / "cases.json",
        root.parent.parent / "cases.json",
        root.parent.parent / "seed_cases" / "cases.json",
    ]
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError(
        "cases.json not found; looked in: "
        + ", ".join(str(c) for c in candidates))


@pytest.fixture(scope="session")
def seed_cases_path() -> Path:
    return _find_seed_cases()


# ---------------------------------------------------------------------------
# Config + engine fixtures
# ---------------------------------------------------------------------------

def make_config(db_path: Path, **overrides: Any) -> EngineConfig:
    base: Dict[str, Any] = dict(
        node_id="test-node",
        db_path=str(db_path),
        max_text_bytes=64 * 1024,
        max_file_bytes=128 * 1024,
        max_text_chars=10_000,
        tool_timeout_seconds=2.0,
        health_timeout_seconds=1.0,
        sync_timeout_seconds=1.0,
        sync_max_retries=3,
        sync_backoff_base=0.001,
        sync_backoff_cap=0.005,
        rate_limit_per_second=10_000.0,
        rate_limit_enabled=False,
        audit_verify_cache_seconds=0.0,
    )
    base.update(overrides)
    cfg = EngineConfig(**base)
    cfg.validate()
    return cfg


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "civic.db"


@pytest.fixture()
async def engine(db_path: Path):
    async with CivicAgent(
        node_id="test-node", config=make_config(db_path),
    ) as e:
        yield e


@pytest.fixture()
async def rate_limited_engine(db_path: Path):
    cfg = make_config(
        db_path, rate_limit_enabled=True, rate_limit_per_second=200.0)
    async with CivicAgent(node_id="test-node", config=cfg) as e:
        yield e


@pytest.fixture()
async def seeded_engine(db_path: Path):
    """Engine pre-seeded with 5 canonical notices."""
    async with CivicAgent(
        node_id="test-node", config=make_config(db_path),
    ) as e:
        for i, text in enumerate(
                [NOTICE, NOTICE_HINDI, NOTICE_HINGLISH,
                 NOTICE_TAMIL, NOTICE_FIR_ONLY]):
            await e.ingest_text(f"seed-{i}.txt", text)
        yield e


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def seed_many(
    engine: CivicAgent, texts: List[str],
) -> List[str]:
    ids: List[str] = []
    for i, text in enumerate(texts):
        rec = await engine.ingest_text(f"seed-{i}.txt", text)
        ids.append(rec.case_id)
    return ids


def assert_fields(record: Any, expected: Dict[str, Optional[str]]) -> None:
    """Assert extracted fields match expectations, with full diff."""
    failures: List[str] = []
    for name, expected_value in expected.items():
        if expected_value is None:
            continue
        actual = getattr(record, name).value
        if actual != expected_value:
            failures.append(
                f"  {name}: expected={expected_value!r} actual={actual!r}")
    assert not failures, "field extraction mismatches:\n" + "\n".join(failures)


def drain_sync_tasks(engine: CivicAgent) -> asyncio.Future:
    """Await all currently queued sync tasks. Safe if the set is empty."""
    async def _drain() -> None:
        while engine._sync_tasks:
            await asyncio.gather(
                *list(engine._sync_tasks), return_exceptions=True)
            await asyncio.sleep(0)
    return asyncio.get_event_loop().create_task(_drain())
