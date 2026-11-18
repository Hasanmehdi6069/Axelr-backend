"""Tests for axelr-insights-engine.

Run: `pytest tests.py -v`

Design:
  * Mocks the Appwrite SDK entirely — no network required.
  * Tests every public method on TablesNamespace, InsightsEngine, SDK.
  * Tests every error path.
  * Tests every resilience primitive independently.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from axelr_insights_engine import (
    AppwriteConfig,
    AppwriteSDK,
    CircuitBreakerOpenError,
    ConflictError,
    IdempotencyConflictError,
    InsightsEngine,
    MetricsCollector,
    NotFoundError,
    QueryFilter,
    QueryOptions,
    TokenBucket,
    ValidationFailed,
    VersionConflictError,
    create_engine,
    is_retryable,
    normalize_error,
    to_result,
)
from axelr_insights_engine.errors import APIError
from axelr_insights_engine.resilience import (
    CBState,
    CircuitBreaker,
    LocalCache,
    RequestDeduplicator,
    RetryPolicy,
    Semaphore,
)


# ═══════════════════════════════════════════════════════════════════════
# Mock Appwrite SDK
# ═══════════════════════════════════════════════════════════════════════

class MockTablesDB:
    """In-memory mock of the Appwrite TablesDB service."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}
        self._tables: set[str] = set()
        self._attrs: dict[str, set[str]] = {}
        self._indexes: dict[str, set[str]] = {}
        self.failures_remaining = 0
        self.fail_with: Exception | None = None
        self.call_log: list[tuple[str, dict[str, Any]]] = []

    def _maybe_fail(self) -> None:
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise self.fail_with or APIError("mock failure", status=500)

    def _k(self, t: str, r: str) -> tuple[str, str]:
        return (t, r)

    async def create_row(self, **kw: Any) -> dict[str, Any]:
        self.call_log.append(("create_row", kw))
        self._maybe_fail()
        key = self._k(kw["table_id"], kw["row_id"])
        if key in self._rows:
            raise APIError("conflict", status=409)
        row = {"$id": kw["row_id"], "$permissions": kw.get("permissions") or [], **kw["data"]}
        self._rows[key] = row
        return row

    async def get_row(self, **kw: Any) -> dict[str, Any]:
        self.call_log.append(("get_row", kw))
        self._maybe_fail()
        row = self._rows.get(self._k(kw["table_id"], kw["row_id"]))
        if row is None:
            raise APIError("not found", status=404)
        return row

    async def update_row(self, **kw: Any) -> dict[str, Any]:
        self.call_log.append(("update_row", kw))
        self._maybe_fail()
        key = self._k(kw["table_id"], kw["row_id"])
        if key not in self._rows:
            raise APIError("not found", status=404)
        self._rows[key] = {**self._rows[key], **kw["data"]}
        return self._rows[key]

    async def upsert_row(self, **kw: Any) -> dict[str, Any]:
        self.call_log.append(("upsert_row", kw))
        self._maybe_fail()
        key = self._k(kw["table_id"], kw["row_id"])
        self._rows[key] = {**self._rows.get(key, {"$id": kw["row_id"]}), **kw["data"]}
        return self._rows[key]

    async def delete_row(self, **kw: Any) -> dict[str, Any]:
        self.call_log.append(("delete_row", kw))
        self._maybe_fail()
        key = self._k(kw["table_id"], kw["row_id"])
        if key not in self._rows:
            raise APIError("not found", status=404)
        del self._rows[key]
        return {"ok": True}

    async def list_rows(self, **kw: Any) -> dict[str, Any]:
        self.call_log.append(("list_rows", kw))
        self._maybe_fail()
        table = kw["table_id"]
        rows = [r for (t, _), r in self._rows.items() if t == table]
        if not kw.get("include_deleted"):
            rows = [r for r in rows if not r.get("_deleted")]
        return {"rows": rows, "total": len(rows)}

    async def create_rows(self, **kw: Any) -> dict[str, Any]:
        self.call_log.append(("create_rows", kw))
        self._maybe_fail()
        inserted = []
        for r in kw["rows"]:
            key = self._k(kw["table_id"], r["$id"])
            self._rows[key] = {"$permissions": [], **r}
            inserted.append(r)
        return {"rows": inserted}

    async def upsert_rows(self, **kw: Any) -> dict[str, Any]:
        self.call_log.append(("upsert_rows", kw))
        self._maybe_fail()
        for r in kw["rows"]:
            self._rows[self._k(kw["table_id"], r["$id"])] = r
        return {"rows": kw["rows"]}

    async def delete_rows(self, **kw: Any) -> dict[str, Any]:
        self.call_log.append(("delete_rows", kw))
        self._maybe_fail()
        for rid in kw["rows"]:
            self._rows.pop(self._k(kw["table_id"], rid), None)
        return {}

    async def list_tables(self, **kw: Any) -> dict[str, Any]:
        self.call_log.append(("list_tables", kw))
        self._maybe_fail()
        return {"tables": [{"$id": t} for t in self._tables]}

    async def create_table(self, **kw: Any) -> dict[str, Any]:
        self.call_log.append(("create_table", kw))
        self._maybe_fail()
        self._tables.add(kw["table_id"])
        return {"$id": kw["table_id"]}

    async def list_attributes(self, **kw: Any) -> dict[str, Any]:
        self.call_log.append(("list_attributes", kw))
        attrs = self._attrs.get(kw["table_id"], set())
        return {"attributes": [{"key": k} for k in attrs]}

    async def list_indexes(self, **kw: Any) -> dict[str, Any]:
        self.call_log.append(("list_indexes", kw))
        idxs = self._indexes.get(kw["table_id"], set())
        return {"indexes": [{"key": k} for k in idxs]}

    async def create_string_attribute(self, **kw: Any) -> dict[str, Any]:
        self._attrs.setdefault(kw["table_id"], set()).add(kw["key"])
        return {"key": kw["key"]}

    async def create_integer_attribute(self, **kw: Any) -> dict[str, Any]:
        self._attrs.setdefault(kw["table_id"], set()).add(kw["key"])
        return {"key": kw["key"]}

    async def create_double_attribute(self, **kw: Any) -> dict[str, Any]:
        self._attrs.setdefault(kw["table_id"], set()).add(kw["key"])
        return {"key": kw["key"]}

    async def create_boolean_attribute(self, **kw: Any) -> dict[str, Any]:
        self._attrs.setdefault(kw["table_id"], set()).add(kw["key"])
        return {"key": kw["key"]}

    async def create_datetime_attribute(self, **kw: Any) -> dict[str, Any]:
        self._attrs.setdefault(kw["table_id"], set()).add(kw["key"])
        return {"key": kw["key"]}

    async def create_index(self, **kw: Any) -> dict[str, Any]:
        self._indexes.setdefault(kw["table_id"], set()).add(kw["key"])
        return {"key": kw["key"]}


class MockClient:
    def set_endpoint(self, _: str) -> "MockClient":
        return self

    def set_project(self, _: str) -> "MockClient":
        return self

    def set_key(self, _: str) -> "MockClient":
        return self

    def set_session(self, _: str) -> "MockClient":
        return self


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════

def _cfg(**overrides: Any) -> AppwriteConfig:
    base = {
        "endpoint": "https://test.local/v1",
        "project_id": "test_project",
        "database_id": "test_db",
        "max_retries": 2,
        "retry_base_delay_s": 0.0,
        "retry_jitter": 0.0,
        "circuit_threshold": 3,
        "circuit_cooldown_s": 0.05,
        "rate_limit_per_min": 100000,
        "rate_limit_burst": 1000,
        "log_level": "error",
        "cache_enabled": True,
    }
    base.update(overrides)
    return AppwriteConfig(**base)


@pytest.fixture
def mock_tables() -> MockTablesDB:
    return MockTablesDB()


@pytest.fixture
def sdk(mock_tables: MockTablesDB) -> AppwriteSDK:
    config = _cfg()
    sdk = AppwriteSDK(
        config,
        client_factory=lambda: MockClient(),
        tables_factory=lambda _: mock_tables,
    )
    return sdk


@pytest.fixture
async def engine(sdk: AppwriteSDK) -> InsightsEngine:
    eng = InsightsEngine(_cfg(), sdk_factory=lambda _: sdk)
    await eng.init(bootstrap=False)
    yield eng
    await eng.destroy()


# ═══════════════════════════════════════════════════════════════════════
# Error tests
# ═══════════════════════════════════════════════════════════════════════

class TestErrors:
    def test_base_error(self) -> None:
        from axelr_insights_engine.errors import AppwriteError
        e = AppwriteError("boom")
        assert e.status == 500
        assert e.code == "appwrite_error"
        assert e.retryable is False
        assert "boom" in str(e)

    def test_api_error_retryable_5xx(self) -> None:
        e = APIError("x", status=503)
        assert e.retryable is True
        assert e.status == 503

    def test_api_error_not_retryable_4xx(self) -> None:
        e = APIError("x", status=404)
        assert e.retryable is False

    def test_is_retryable_statuses(self) -> None:
        assert is_retryable(APIError("x", status=500))
        assert is_retryable(APIError("x", status=503))
        assert is_retryable(APIError("x", status=429))
        assert not is_retryable(APIError("x", status=404))

    def test_normalize_error(self) -> None:
        assert isinstance(normalize_error({"status": 404, "message": "x"}), NotFoundError)
        assert isinstance(normalize_error({"status": 409, "message": "x"}), ConflictError)

    def test_to_result_never_raises(self) -> None:
        result = to_result(RuntimeError("boom"))
        assert result["ok"] is False
        assert "boom" in result["error"]


# ═══════════════════════════════════════════════════════════════════════
# Config tests
# ═══════════════════════════════════════════════════════════════════════

class TestConfig:
    def test_required_fields(self) -> None:
        from axelr_insights_engine.errors import ConfigurationError
        with pytest.raises(ConfigurationError):
            AppwriteConfig(endpoint="", project_id="p", database_id="d")

    def test_mode_detection_api_key(self) -> None:
        c = AppwriteConfig(
            endpoint="https://x/v1", project_id="p", database_id="d",
            api_key="k",
        )
        assert c.mode == "server"
        assert c.is_server_mode

    def test_mode_detection_session(self) -> None:
        c = AppwriteConfig(
            endpoint="https://x/v1", project_id="p", database_id="d",
            session_secret="s",
        )
        assert c.mode == "session"
        assert c.is_session_mode

    def test_mode_detection_anonymous(self) -> None:
        c = AppwriteConfig(endpoint="https://x/v1", project_id="p", database_id="d")
        assert c.mode == "anonymous"
        assert c.is_anonymous

    def test_redaction(self) -> None:
        c = AppwriteConfig(
            endpoint="https://x/v1", project_id="p", database_id="d",
            api_key="secret_key",
        )
        safe = c.to_safe_dict()
        assert "secret_key" not in str(safe)
        assert safe["has_api_key"] is True


# ═══════════════════════════════════════════════════════════════════════
# Resilience tests
# ═══════════════════════════════════════════════════════════════════════

class TestTokenBucket:
    @pytest.mark.asyncio
    async def test_allows_burst(self) -> None:
        b = TokenBucket(rate_per_sec=10, capacity=3)
        start = asyncio.get_event_loop().time()
        for _ in range(3):
            await b.acquire()
        elapsed = asyncio.get_event_loop().time() - start
        assert elapsed < 0.1

    @pytest.mark.asyncio
    async def test_waits_when_exhausted(self) -> None:
        b = TokenBucket(rate_per_sec=10, capacity=1)
        await b.acquire()
        start = asyncio.get_event_loop().time()
        await b.acquire()
        elapsed = asyncio.get_event_loop().time() - start
        assert elapsed >= 0.05


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_starts_closed(self) -> None:
        cb = CircuitBreaker(threshold=2)
        assert cb.state == CBState.CLOSED
        assert await cb.allow() is True

    @pytest.mark.asyncio
    async def test_opens_after_threshold(self) -> None:
        cb = CircuitBreaker(threshold=2, cooldown_s=1.0)
        await cb.record_failure()
        assert cb.state == CBState.CLOSED
        await cb.record_failure()
        assert cb.state == CBState.OPEN
        assert await cb.allow() is False

    @pytest.mark.asyncio
    async def test_half_opens_after_cooldown(self) -> None:
        cb = CircuitBreaker(threshold=1, cooldown_s=0.05)
        await cb.record_failure()
        assert cb.state == CBState.OPEN
        await asyncio.sleep(0.06)
        assert cb.state == CBState.HALF_OPEN
        assert await cb.allow() is True

    @pytest.mark.asyncio
    async def test_closes_after_half_open_successes(self) -> None:
        cb = CircuitBreaker(threshold=1, cooldown_s=0.02, successes_needed=2)
        await cb.record_failure()
        await asyncio.sleep(0.03)
        await cb.record_success()
        assert cb.state == CBState.HALF_OPEN
        await cb.record_success()
        assert cb.state == CBState.CLOSED


class TestLocalCache:
    def test_set_get(self) -> None:
        c = LocalCache(max_entries=10)
        c.set(("a", "b"), 42)
        assert c.get(("a", "b")) == 42
        assert c.get(("a", "c")) is None

    def test_lru_eviction(self) -> None:
        c = LocalCache(max_entries=2)
        c.set(("a",), 1)
        c.set(("b",), 2)
        c.set(("c",), 3)
        assert c.get(("a",)) is None
        assert c.get(("c",)) == 3

    @pytest.mark.asyncio
    async def test_ttl_expiry(self) -> None:
        c = LocalCache(ttl_s=0.02, max_entries=10)
        c.set(("k",), 1)
        await asyncio.sleep(0.03)
        assert c.get(("k",)) is None


class TestSemaphore:
    @pytest.mark.asyncio
    async def test_caps_concurrency(self) -> None:
        s = Semaphore(2)
        await s.acquire()
        await s.acquire()
        assert s.active == 2
        s.release()
        assert s.active == 1


class TestRequestDeduplicator:
    @pytest.mark.asyncio
    async def test_coalesces_inflight(self) -> None:
        d = RequestDeduplicator()
        calls = {"n": 0}

        async def slow() -> str:
            calls["n"] += 1
            await asyncio.sleep(0.02)
            return "v"

        results = await asyncio.gather(
            d.run("k", slow),
            d.run("k", slow),
            d.run("k", slow),
        )
        assert calls["n"] == 1
        assert results == ["v", "v", "v"]


class TestMetrics:
    def test_counters_histograms_gauges(self) -> None:
        m = MetricsCollector()
        m.inc("calls")
        m.inc("calls", 2)
        m.observe("latency", 10)
        m.observe("latency", 20)
        m.observe("latency", 30)
        m.gauge("queue", 5)
        snap = m.snapshot()
        assert snap["counters"]["calls"] == 3
        assert snap["histograms"]["latency"]["count"] == 3
        assert snap["histograms"]["latency"]["p50"] == 20
        assert snap["gauges"]["queue"] == 5


class TestRetryPolicy:
    def test_delay_grows(self) -> None:
        p = RetryPolicy(max_attempts=4, base_delay_s=0.1, max_delay_s=10.0, jitter=0.0)
        assert p.delay_for(1) == 0.1
        assert p.delay_for(2) == 0.2
        assert p.delay_for(3) == 0.4


# ═══════════════════════════════════════════════════════════════════════
# SDK tests
# ═══════════════════════════════════════════════════════════════════════

class TestSDK:
    @pytest.mark.asyncio
    async def test_retries_retryable_errors(self, sdk: AppwriteSDK, mock_tables: MockTablesDB) -> None:
        n = {"calls": 0}

        async def flaky() -> str:
            n["calls"] += 1
            if n["calls"] < 3:
                raise APIError("flaky", status=503)
            return "ok"

        result = await sdk.call("test", flaky)
        assert result == "ok"
        assert n["calls"] == 3

    @pytest.mark.asyncio
    async def test_no_retry_on_4xx(self, sdk: AppwriteSDK) -> None:
        n = {"calls": 0}

        async def fail() -> None:
            n["calls"] += 1
            raise APIError("bad", status=400)

        with pytest.raises(Exception):
            await sdk.call("test", fail)
        assert n["calls"] == 1

    @pytest.mark.asyncio
    async def test_circuit_opens(self) -> None:
        config = _cfg(circuit_threshold=2, max_retries=1)
        mock = MockTablesDB()
        sdk = AppwriteSDK(
            config,
            client_factory=lambda: MockClient(),
            tables_factory=lambda _: mock,
        )

        async def fail() -> None:
            raise APIError("x", status=500)

        for _ in range(3):
            try:
                await sdk.call("test", fail)
            except Exception:
                pass
        with pytest.raises(CircuitBreakerOpenError):
            await sdk.call("test", fail)

    @pytest.mark.asyncio
    async def test_cache_hit(self, sdk: AppwriteSDK) -> None:
        n = {"calls": 0}

        async def expensive() -> str:
            n["calls"] += 1
            return "v"

        await sdk.call_read("test", expensive, cache_parts=("k",))
        await sdk.call_read("test", expensive, cache_parts=("k",))
        assert n["calls"] == 1


# ═══════════════════════════════════════════════════════════════════════
# Tables tests
# ═══════════════════════════════════════════════════════════════════════

class TestTables:
    @pytest.mark.asyncio
    async def test_crud_roundtrip(self, engine: InsightsEngine) -> None:
        row = await engine.create("users", {"name": "A"})
        assert row["name"] == "A"
        fetched = await engine.read("users", row["$id"])
        assert fetched["name"] == "A"
        updated = await engine.update("users", row["$id"], {"name": "B"})
        assert updated["name"] == "B"
        assert await engine.delete("users", row["$id"]) is True
        assert await engine.read("users", row["$id"]) is None

    @pytest.mark.asyncio
    async def test_upsert(self, engine: InsightsEngine) -> None:
        r1 = await engine.upsert("t", "row1", {"v": 1})
        r2 = await engine.upsert("t", "row1", {"v": 2})
        assert r1["v"] == 1
        assert r2["v"] == 2

    @pytest.mark.asyncio
    async def test_optimistic_lock_conflict(self, engine: InsightsEngine) -> None:
        await engine.create("t", {"v": 1, "_version": 1}, row_id="r1")
        with pytest.raises(VersionConflictError):
            await engine.update("t", "r1", {"v": 2}, expected_version=99)

    @pytest.mark.asyncio
    async def test_optimistic_lock_success(self, engine: InsightsEngine) -> None:
        await engine.create("t", {"v": 1, "_version": 1}, row_id="r1")
        updated = await engine.update("t", "r1", {"v": 2}, expected_version=1)
        assert updated["v"] == 2
        assert updated["_version"] == 2

    @pytest.mark.asyncio
    async def test_soft_delete(self, engine: InsightsEngine) -> None:
        await engine.create("t", {"v": 1}, row_id="r1")
        assert await engine.delete("t", "r1", soft=True) is True
        assert await engine.read("t", "r1") is None
        raw = await engine.read("t", "r1", include_deleted=True)
        assert raw["_deleted"] is True

    @pytest.mark.asyncio
    async def test_batch_create(self, engine: InsightsEngine) -> None:
        rows = [{"i": i} for i in range(250)]
        n = await engine._tables.batch_create("t", rows)
        assert n == 250

    @pytest.mark.asyncio
    async def test_count(self, engine: InsightsEngine) -> None:
        for i in range(5):
            await engine.create("t", {"i": i})
        assert await engine.count("t") == 5

    @pytest.mark.asyncio
    async def test_stream(self, engine: InsightsEngine) -> None:
        for i in range(5):
            await engine.create("t", {"i": i})
        out = []
        async for row in engine.stream("t"):
            out.append(row)
        assert len(out) == 5

    @pytest.mark.asyncio
    async def test_idempotency_conflict(self, engine: InsightsEngine) -> None:
        await engine.create("t", {"v": 1}, idempotency_key="k1")
        with pytest.raises(IdempotencyConflictError):
            await engine.create("t", {"v": 2}, idempotency_key="k1")

    @pytest.mark.asyncio
    async def test_idempotency_same_payload(self, engine: InsightsEngine) -> None:
        r1 = await engine.create("t", {"v": 1}, idempotency_key="k1")
        r2 = await engine.create("t", {"v": 1}, idempotency_key="k1")
        assert r1["$id"] == r2["$id"]

    @pytest.mark.asyncio
    async def test_permissions_for_user(self) -> None:
        from axelr_insights_engine import TablesNamespace
        perms = TablesNamespace.permissions_for_user("user_123")
        assert len(perms) == 3


# ═══════════════════════════════════════════════════════════════════════
# Engine tests
# ═══════════════════════════════════════════════════════════════════════

class TestEngine:
    @pytest.mark.asyncio
    async def test_init_destroy(self) -> None:
        engine = create_engine(_cfg())
        await engine.init(bootstrap=False)
        assert engine.initialized
        await engine.destroy()
        assert not engine.initialized

    @pytest.mark.asyncio
    async def test_describe(self, engine: InsightsEngine) -> None:
        desc = engine.describe()
        assert "Axelr Insights Engine" in desc
        assert "mode=" in desc

    @pytest.mark.asyncio
    async def test_export_metrics(self, engine: InsightsEngine) -> None:
        metrics = engine.export_metrics()
        assert "engine" in metrics
        assert "sdk" in metrics
        assert "metrics" in metrics

    @pytest.mark.asyncio
    async def test_multi_tenant_helpers(self, engine: InsightsEngine) -> None:
        row = await engine.insert_for_user("axelr_insights", "u1", {"content": "x"})
        assert row["user_id"] == "u1"
        rows = await engine.query_for_user("axelr_insights", "u1")
        assert len(rows) == 1
        count = await engine.count_for_user("axelr_insights", "u1")
        assert count == 1

    @pytest.mark.asyncio
    async def test_run_self_test(self, engine: InsightsEngine) -> None:
        report = await engine.run_self_test(table="audit_events")
        assert report["ok"] is True
        assert report["passed"] == report["total"]

    @pytest.mark.asyncio
    async def test_context_manager(self) -> None:
        async with create_engine(_cfg()) as engine:
            assert engine.initialized
            assert await engine.create("t", {"x": 1}) is not None

    @pytest.mark.asyncio
    async def test_health_check_deep(self, engine: InsightsEngine) -> None:
        report = await engine.health_check(deep=True)
        assert report.healthy is True
        assert report.mode == "anonymous"