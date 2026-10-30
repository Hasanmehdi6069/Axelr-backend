"""Test suite for the Insights Engine.

Run: `pytest tests.py -v`

Coverage strategy:
  * Pure functions: always run, no deps.
  * Async primitives (breaker, limiter): always run, no deps.
  * Search index: uses an in-memory fake backend — always runs.
  * Backend integration: marked `@pytest.mark.integration`, skipped
    unless the relevant env var is set. This is the honest way to
    handle "requires real Appwrite/Mongo".

No mocks for the backends — we use real implementations when creds are
present and skip otherwise. Mocking a database tests the mock.
"""
from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone

import pytest

from . import config as config_mod
from . import search as search_mod
from . import storage_base as sb


# ── Pure-function tests ───────────────────────────────────────────────

class TestSerialization(unittest.TestCase):
    def test_to_iso_naive_datetime_assumed_utc(self):
        dt = datetime(2024, 1, 1, 12, 0, 0)
        assert sb.to_iso(dt) == "2024-01-01T12:00:00Z"

    def test_to_iso_aware_datetime_converted_to_utc(self):
        from datetime import timezone as tz
        dt = datetime(2024, 1, 1, 12, 0, 0, tzinfo=tz(timedelta(hours=5)))
        assert sb.to_iso(dt) == "2024-01-01T07:00:00Z"

    def test_to_dt_parses_z_suffix(self):
        dt = sb.to_dt("2024-01-01T12:00:00Z")
        assert dt is not None
        assert dt.tzinfo is not None
        assert dt.hour == 12

    def test_to_dt_rejects_garbage(self):
        assert sb.to_dt("not-a-date") is None
        assert sb.to_dt(None) is None
        assert sb.to_dt("") is None

    def test_to_dt_accepts_unix_epoch(self):
        dt = sb.to_dt(1700000000)
        assert dt is not None
        assert dt.year == 2023

    def test_serialize_recursive(self):
        dt = datetime(2024, 1, 1, tzinfo=timezone.utc)
        src = {"a": dt, "b": [dt, dt], "c": {"d": dt}, "e": 1}
        out = sb.serialize(src)
        assert out["a"] == "2024-01-01T00:00:00Z"
        assert out["b"] == ["2024-01-01T00:00:00Z"] * 2
        assert out["c"]["d"] == "2024-01-01T00:00:00Z"
        assert out["e"] == 1


class TestDenorm(unittest.TestCase):
    def test_promotes_hot_fields(self):
        out = sb._denorm({
            "user_id": "u1", "pipeline_id": "p1",
            "status": "ok", "latency_ms": 42.5,
            "ts": datetime(2024, 3, 15, tzinfo=timezone.utc),
        })
        assert out["user_id"] == "u1"
        assert out["pipeline_id"] == "p1"
        assert out["status"] == "ok"
        assert out["latency_ms"] == 42.5
        assert out["day"] == "2024-03-15"

    def test_rejects_bool_latency(self):
        out = sb._denorm({"latency_ms": True})
        assert "latency_ms" not in out

    def test_truncates_long_strings(self):
        out = sb._denorm({"user_id": "x" * 1000})
        assert len(out["user_id"]) == 255


class TestPercentiles(unittest.TestCase):
    def test_empty_returns_nones(self):
        r = sb._py_percentiles([], backend="test")
        assert r["count"] == 0
        assert r["p50"] is None

    def test_single_value(self):
        r = sb._py_percentiles([5.0], backend="test")
        assert r["count"] == 1
        assert r["p50"] == 5.0
        assert r["p95"] == 5.0
        assert r["p99"] == 5.0

    def test_monotonic_percentiles(self):
        vals = list(range(1, 101))
        r = sb._py_percentiles([float(v) for v in vals], backend="test")
        assert r["p50"] <= r["p95"] <= r["p99"]
        assert r["min"] == 1.0
        assert r["max"] == 100.0


class TestPipelineRow(unittest.TestCase):
    def test_healthy_when_error_rate_low(self):
        r = sb._finalise_pipeline_row(
            {"pipeline_id": "p1", "total": 100, "errors": 2}, [10.0, 20.0],
        )
        assert r["health_status"] == "healthy"
        assert r["error_rate"] == 0.02
        assert r["avg_latency_ms"] == 15.0

    def test_degraded_when_error_rate_high(self):
        r = sb._finalise_pipeline_row(
            {"pipeline_id": "p1", "total": 100, "errors": 20}, [],
        )
        assert r["health_status"] == "degraded"

    def test_zero_total_is_safe(self):
        r = sb._finalise_pipeline_row(
            {"pipeline_id": "p1", "total": 0, "errors": 0}, [],
        )
        assert r["error_rate"] == 0.0
        assert r["health_status"] == "healthy"


# ── Async primitives ──────────────────────────────────────────────────

class TestCircuitBreaker(unittest.IsolatedAsyncioTestCase):
    async def test_closed_state_allows(self):
        br = sb.CircuitBreaker(threshold=2, cooldown=0.1)
        assert await br.allow() is True

    async def test_opens_after_threshold(self):
        br = sb.CircuitBreaker(threshold=2, cooldown=0.5)
        await br.record_failure()
        assert await br.allow() is True
        await br.record_failure()
        assert await br.allow() is False
        assert br.is_open is True

    async def test_half_open_after_cooldown(self):
        br = sb.CircuitBreaker(threshold=1, cooldown=0.05)
        await br.record_failure()
        assert await br.allow() is False
        await asyncio.sleep(0.06)
        assert await br.allow() is True

    async def test_success_resets(self):
        br = sb.CircuitBreaker(threshold=1, cooldown=0.05)
        await br.record_failure()
        await br.record_success()
        assert br.is_open is False


class TestRateLimiter(unittest.IsolatedAsyncioTestCase):
    async def test_allows_under_limit(self):
        rl = sb.RateLimiter(max_requests=3, window_seconds=1.0)
        for _ in range(3):
            assert await rl.check("k") is True

    async def test_rejects_over_limit(self):
        rl = sb.RateLimiter(max_requests=2, window_seconds=1.0)
        assert await rl.check("k") is True
        assert await rl.check("k") is True
        assert await rl.check("k") is False

    async def test_window_expires(self):
        rl = sb.RateLimiter(max_requests=1, window_seconds=0.05)
        assert await rl.check("k") is True
        assert await rl.check("k") is False
        await asyncio.sleep(0.06)
        assert await rl.check("k") is True

    async def test_keys_independent(self):
        rl = sb.RateLimiter(max_requests=1, window_seconds=1.0)
        assert await rl.check("a") is True
        assert await rl.check("a") is False
        assert await rl.check("b") is True


# ── Config ────────────────────────────────────────────────────────────

class TestConfig(unittest.TestCase):
    def test_parsers_clamp_ranges(self):
        os.environ["_T_INT"] = "9999"
        try:
            assert config_mod._int("_T_INT", 5, lo=0, hi=100) == 100
        finally:
            del os.environ["_T_INT"]

    def test_parser_rejects_invalid(self):
        os.environ["_T_BAD"] = "not-a-number"
        try:
            assert config_mod._int("_T_BAD", 42) == 42
            assert config_mod._float("_T_BAD", 1.5) == 1.5
        finally:
            del os.environ["_T_BAD"]

    def test_str_allowed_values(self):
        os.environ["_T_STR"] = "bad"
        try:
            assert config_mod._str(
                "_T_STR", "good", allowed=("good", "best"),
            ) == "good"
        finally:
            del os.environ["_T_STR"]


# ── Search index with fake backend ────────────────────────────────────

class _FakeBackend:
    """In-memory backend implementing only what search.py uses."""

    def __init__(self) -> None:
        self._docs: dict[str, dict] = {}

    async def list_documents(self, limit: int = 200) -> list[dict]:
        return list(self._docs.values())[:limit]

    async def upsert_document(self, doc_id: str, content: str,
                              meta: dict) -> dict:
        self._docs[doc_id] = {
            "doc_id": doc_id, "content": content, "meta": meta,
        }
        return self._docs[doc_id]

    async def delete_document(self, doc_id: str) -> bool:
        return self._docs.pop(doc_id, None) is not None


class TestSearchIndex(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._saved = sb._backend
        sb._backend = _FakeBackend()  # type: ignore[assignment]
        search_mod._INDEX = None
        self.idx = search_mod.get_index()
        await self.idx.bootstrap()

    async def asyncTearDown(self):
        sb._backend = self._saved
        search_mod._INDEX = None

    async def test_empty_index_returns_no_hits(self):
        assert self.idx.search("anything") == []

    async def test_upsert_then_search(self):
        await self.idx.upsert(
            "d1", "vector databases are fast", {"src": "test"},
        )
        await self.idx.upsert("d2", "relational databases are slower", {})
        hits = self.idx.search("vector database", top_k=2)
        assert len(hits) >= 1
        assert hits[0]["doc_id"] == "d1"

    async def test_remove_drops_from_index(self):
        await self.idx.upsert("d1", "some content here", {})
        assert await self.idx.remove("d1") is True
        assert self.idx.search("content") == []

    async def test_min_score_filters_low_matches(self):
        await self.idx.upsert("d1", "absolutely unrelated text", {})
        hits = self.idx.search("zzz never mentioned", min_score=0.9)
        assert hits == []


# ── Integration tests (skipped without creds) ─────────────────────────

def _has_mongo() -> bool:
    return bool(os.getenv("MONGODB_URI"))


def _has_appwrite() -> bool:
    return all(os.getenv(k) for k in (
        "APPWRITE_ENDPOINT", "APPWRITE_PROJECT_ID", "APPWRITE_API_KEY",
    ))


@pytest.mark.integration
@pytest.mark.skipif(not _has_mongo(), reason="MONGODB_URI not set")
class TestMongoIntegration:
    """Runs against real MongoDB. Skipped unless MONGODB_URI is set."""

    @pytest.mark.asyncio
    async def test_roundtrip(self):
        import uuid
        from .storage_mongo import MongoBackend
        db_name = f"axelr_test_{uuid.uuid4().hex[:8]}"
        b = MongoBackend(uri=os.environ["MONGODB_URI"], db_name=db_name)
        await b.init()
        try:
            rid = await b.insert_audit("node_execution", {
                "pipeline_id": "test_p", "node_id": "test_n",
                "latency_ms": 12.5, "status": "ok",
                "ts": datetime.now(timezone.utc),
            })
            assert rid is not None
            rows = await b.list_audit(kind="node_execution", limit=10)
            assert any(r.get("pipeline_id") == "test_p" for r in rows)
        finally:
            # Drop the isolated DB so reruns never collide.
            try:
                await b._client.drop_database(db_name)
            except Exception:
                pass
            await b.close()


@pytest.mark.integration
@pytest.mark.skipif(not _has_appwrite(), reason="APPWRITE_* not set")
class TestAppwriteIntegration:
    """Runs against real Appwrite. Skipped unless APPWRITE_* set."""

    @pytest.mark.asyncio
    async def test_roundtrip(self):
        from .storage_appwrite import AppwriteBackend
        b = AppwriteBackend(
            endpoint=os.environ["APPWRITE_ENDPOINT"],
            project=os.environ["APPWRITE_PROJECT_ID"],
            api_key=os.environ["APPWRITE_API_KEY"],
            database_id=os.getenv("APPWRITE_DATABASE_ID", "insights"),
        )
        await b.init()
        try:
            rid = await b.insert_audit("node_execution", {
                "pipeline_id": "test_p", "node_id": "test_n",
                "latency_ms": 12.5, "status": "ok",
                "ts": datetime.now(timezone.utc),
            })
            assert rid is not None
            rows = await b.list_audit(kind="node_execution", limit=10)
            assert any(r.get("pipeline_id") == "test_p" for r in rows)
        finally:
            await b.close()