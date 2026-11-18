"""axelr-insights-engine — Main engine class.

The engine is the singleton wrapper around `sdk` + `tables` + lifecycle.

Usage:
    from axelr_insights_engine import init, get_engine
    await init()
    db = get_engine()
    await db.insert_for_user("axelr_insights", "user_1", {...})
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator

from .config import AppwriteConfig, resolve_config
from .errors import (
    AppwriteError,
    ConfigurationError,
    NotFoundError,
    ValidationFailed,
)
from .models import HealthReport, QueryOptions
from .resilience import MetricsCollector, StructuredLogger
from .sdk import AppwriteSDK
from .setup import ALL_SCHEMAS, ensure_all_tables, verify_all_tables
from .tables import TablesNamespace

__version__ = "1.0.0"


class InsightsEngine:
    """The unified Axelr Insights Engine — client-side, TablesDB only."""

    def __init__(
        self,
        config: AppwriteConfig | dict[str, Any] | None = None,
        *,
        sdk_factory: Any = None,
        auto_bootstrap: bool = True,
    ) -> None:
        self._config = resolve_config(config)
        self._log = StructuredLogger(
            level=self._config.log_level,
            context={"component": "insights_engine", "version": __version__},
        )
        self._sdk = (
            sdk_factory(self._config)
            if sdk_factory
            else AppwriteSDK(self._config)
        )
        self._tables = TablesNamespace(self._sdk)
        self._metrics = self._sdk.metrics
        self._initialized = False
        self._schema_report: dict[str, Any] | None = None
        self._auto_bootstrap = auto_bootstrap
        self._started_at = time.time()

    # ── Accessors ────────────────────────────────────────────────────

    @property
    def config(self) -> AppwriteConfig:
        return self._config

    @property
    def sdk(self) -> AppwriteSDK:
        return self._sdk

    @property
    def tables(self) -> TablesNamespace:
        return self._tables

    @property
    def metrics(self) -> MetricsCollector:
        return self._metrics

    @property
    def initialized(self) -> bool:
        return self._initialized

    # ── Lifecycle ────────────────────────────────────────────────────

    async def init(self, *, bootstrap: bool | None = None) -> "InsightsEngine":
        if self._initialized:
            self._log.info("engine_already_initialized")
            return self
        self._log.info("engine_init_start", config=self._config.to_safe_dict())

        do_bootstrap = self._auto_bootstrap if bootstrap is None else bootstrap
        if do_bootstrap:
            try:
                self._schema_report = await ensure_all_tables(self._sdk)
                self._log.info(
                    "schema_bootstrap_complete",
                    tables=list(self._schema_report.keys()),
                )
            except Exception as e:
                self._log.warn("schema_bootstrap_failed", error=str(e)[:200])

        self._initialized = True
        self._log.info("engine_init_complete")
        return self

    async def health_check(self, *, deep: bool = False) -> HealthReport:
        """Probe the engine. Safe to call before init."""
        base = await self._sdk.health(deep=deep)
        report = HealthReport(
            healthy=base.get("healthy", False),
            backend="appwrite",
            mode=self._config.mode,
            database_id=self._config.database_id,
            circuit_state=base.get("circuit_state", "unknown"),
            cache_size=base.get("cache_size", 0),
            initialized=self._initialized,
            timestamp=base.get("timestamp", time.time()),
            reason=base.get("reason"),
        )
        if deep and report.healthy:
            try:
                report.schema = await verify_all_tables(self._sdk)
                report.schema_ok = all(
                    r["table_exists"]
                    and not r["missing_attributes"]
                    and not r["missing_indexes"]
                    for r in report.schema.values()
                )
            except Exception as e:
                report.metrics = {"error": str(e)[:200]}
            report.metrics = self._metrics.snapshot()
        return report

    async def destroy(self) -> None:
        await self._sdk.close()
        self._initialized = False
        self._log.info("engine_destroyed")

    async def __aenter__(self) -> "InsightsEngine":
        await self.init()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.destroy()

    # ── Convenience shortcuts ────────────────────────────────────────

    async def create(self, table: str, data: dict[str, Any], **kw: Any) -> dict[str, Any]:
        return await self._tables.create(table, data, **kw)

    async def read(self, table: str, row_id: str, **kw: Any) -> dict[str, Any] | None:
        return await self._tables.read(table, row_id, **kw)

    async def update(self, table: str, row_id: str, data: dict[str, Any], **kw: Any) -> dict[str, Any]:
        return await self._tables.update(table, row_id, data, **kw)

    async def upsert(self, table: str, row_id: str, data: dict[str, Any], **kw: Any) -> dict[str, Any]:
        return await self._tables.upsert(table, row_id, data, **kw)

    async def delete(self, table: str, row_id: str, **kw: Any) -> bool:
        return await self._tables.delete(table, row_id, **kw)

    async def query(self, table: str, options: Any = None, **kw: Any) -> list[dict[str, Any]]:
        return await self._tables.query(table, options, **kw)

    async def query_all(self, table: str, options: Any = None, **kw: Any) -> list[dict[str, Any]]:
        return await self._tables.query_all(table, options, **kw)

    async def count(self, table: str, options: Any = None, **kw: Any) -> int:
        return await self._tables.count(table, options, **kw)

    def stream(self, table: str, options: Any = None, **kw: Any) -> AsyncIterator[dict[str, Any]]:
        return self._tables.stream(table, options, **kw)

    # ── Multi-tenant helpers ────────────────────────────────────────

    async def insert_for_user(
        self,
        table: str,
        user_id: str,
        data: dict[str, Any],
        *,
        row_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Insert a row scoped to a user — with per-row ACLs."""
        if not user_id:
            raise ValidationFailed("user_id required")
        permissions = self._tables.permissions_for_user(user_id)
        enriched = {**data, "user_id": user_id}
        return await self._tables.create(
            table, enriched,
            row_id=row_id,
            permissions=permissions,
            idempotency_key=idempotency_key,
        )

    async def query_for_user(
        self,
        table: str,
        user_id: str,
        options: QueryOptions | dict[str, Any] | None = None,
        **kw: Any,
    ) -> list[dict[str, Any]]:
        """Query rows scoped to a user."""
        from .models import QueryFilter
        opts = _merge_user_filter(options, kw, user_id)
        return await self._tables.query(table, opts)

    async def count_for_user(
        self,
        table: str,
        user_id: str,
        options: QueryOptions | dict[str, Any] | None = None,
        **kw: Any,
    ) -> int:
        from .models import QueryFilter
        opts = _merge_user_filter(options, kw, user_id)
        return await self._tables.count(table, opts)

    async def delete_for_user(
        self, table: str, user_id: str, row_id: str,
    ) -> bool:
        """Delete a row only if it belongs to `user_id`."""
        row = await self._tables.read(table, row_id, cacheable=False)
        if row is None:
            return False
        if row.get("user_id") != user_id:
            raise AppwriteError(
                f"row {row_id} does not belong to user {user_id}",
                status=403, code="forbidden",
            )
        return await self._tables.delete(table, row_id)

    # ── Auth helpers ────────────────────────────────────────────────

    async def sign_in_email(self, email: str, password: str) -> dict[str, Any]:
        """Sign in a user via email/password (session mode)."""
        from appwrite.services.account import Account
        account = Account(self._sdk.client)
        session = await self._sdk.call(
            "auth.sign_in_email",
            lambda: account.create_email_password_session(email, password),
        )
        if isinstance(session, dict) and session.get("secret"):
            self._sdk.set_session(session["secret"])
        return session

    async def sign_in_anonymous(self) -> dict[str, Any]:
        """Create an anonymous session."""
        from appwrite.services.account import Account
        account = Account(self._sdk.client)
        session = await self._sdk.call(
            "auth.sign_in_anonymous",
            lambda: account.create_anonymous_session(),
        )
        if isinstance(session, dict) and session.get("secret"):
            self._sdk.set_session(session["secret"])
        return session

    async def sign_out(self) -> bool:
        from appwrite.services.account import Account
        account = Account(self._sdk.client)
        try:
            await self._sdk.call(
                "auth.sign_out",
                lambda: account.delete_session("current"),
            )
            return True
        except NotFoundError:
            return False

    async def get_current_user(self) -> dict[str, Any] | None:
        from appwrite.services.account import Account
        account = Account(self._sdk.client)
        try:
            user = await self._sdk.call(
                "auth.get_current_user",
                lambda: account.get(),
            )
            return user if isinstance(user, dict) else dict(user)
        except AppwriteError:
            return None

    # ── Introspection & observability ──────────────────────────────

    def describe(self) -> str:
        """Human-readable engine summary."""
        uptime = int(time.time() - self._started_at)
        return (
            f"Axelr Insights Engine v{__version__} | "
            f"mode={self._config.mode} | "
            f"db={self._config.database_id} | "
            f"circuit={self._sdk.breaker.state} | "
            f"cache={self._sdk.cache.size} | "
            f"uptime={uptime}s"
        )

    def export_metrics(self) -> dict[str, Any]:
        """Full metrics snapshot as a dict."""
        return {
            "engine": {
                "version": __version__,
                "mode": self._config.mode,
                "initialized": self._initialized,
                "uptime_s": time.time() - self._started_at,
            },
            "sdk": {
                "circuit": self._sdk.breaker.snapshot(),
                "cache_size": self._sdk.cache.size,
                "config": self._config.to_safe_dict(),
            },
            "metrics": self._metrics.snapshot(),
        }

    def get_schema_report(self) -> dict[str, Any] | None:
        return self._schema_report

    def list_schemas(self) -> list[Any]:
        return list(ALL_SCHEMAS)

    async def flush_cache(self) -> None:
        self._sdk.cache.clear()
        self._log.info("cache_flushed")

    async def reset_breaker(self) -> None:
        await self._sdk.breaker.reset()
        self._log.info("breaker_reset")

    # ── Self-test (demo + health) ───────────────────────────────────

    async def run_self_test(self, table: str = "audit_events") -> dict[str, Any]:
        """Run a full insert → read → update → delete cycle.

        Returns a step report with pass/fail. Used by demos and by
        deep health checks against the live backend.
        """
        steps: list[dict[str, Any]] = []
        row_id = self._tables.new_row_id()

        async def _step(name: str, coro: Any) -> Any:
            started = time.monotonic()
            try:
                result = await coro
                steps.append({
                    "step": name,
                    "ok": True,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                })
                return result
            except Exception as e:
                steps.append({
                    "step": name,
                    "ok": False,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                    "error": str(e)[:200],
                })
                return None

        await _step("create", self._tables.create(
            table,
            {
                "kind": "self_test",
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "status": "ok",
            },
            row_id=row_id,
        ))
        await _step("read", self._tables.read(table, row_id, cacheable=False))
        await _step("update", self._tables.update(
            table, row_id, {"status": "updated"},
        ))
        await _step("delete", self._tables.delete(table, row_id))

        passed = sum(1 for s in steps if s["ok"])
        return {
            "ok": passed == len(steps),
            "passed": passed,
            "total": len(steps),
            "steps": steps,
            "table": table,
        }


# ── Helpers ───────────────────────────────────────────────────────────

def _merge_user_filter(
    options: Any,
    extra_kwargs: dict[str, Any],
    user_id: str,
) -> QueryOptions:
    from .models import QueryFilter
    if isinstance(options, QueryOptions):
        opts = options.model_copy()
    else:
        opts = QueryOptions(**(options or {}))
    opts.filters = [QueryFilter.eq("user_id", user_id), *opts.filters]
    if extra_kwargs:
        update = {k: v for k, v in extra_kwargs.items() if k in QueryOptions.model_fields}
        if update:
            opts = opts.model_copy(update=update)
    return opts


# ── Module-level factory ──────────────────────────────────────────────

def create_engine(
    config: AppwriteConfig | dict[str, Any] | None = None,
    **options: Any,
) -> InsightsEngine:
    """Build a non-singleton engine (useful in tests and multi-tenant apps)."""
    return InsightsEngine(config, **options)


def create_axelr_module(
    config: AppwriteConfig | dict[str, Any] | None = None,
    **options: Any,
) -> dict[str, Any]:
    """Axelr-compatible module factory (drop-in for `createHackathonModule`)."""
    engine = InsightsEngine(config, **options)

    async def _init(opts: dict[str, Any] | None = None) -> dict[str, Any]:
        await engine.init(**(opts or {}))
        return module

    async def _health(opts: dict[str, Any] | None = None) -> dict[str, Any]:
        report = await engine.health_check(**(opts or {}))
        return report.model_dump()

    module: dict[str, Any] = {
        "MODULE_NAME": "insights_engine",
        "VERSION": __version__,
        "engine": engine,
        "init": _init,
        "destroy": engine.destroy,
        "health_check": _health,
        "tables": {
            "create": engine.create,
            "read": engine.read,
            "update": engine.update,
            "upsert": engine.upsert,
            "delete": engine.delete,
            "query": engine.query,
            "query_all": engine.query_all,
            "count": engine.count,
        },
        "insert_for_user": engine.insert_for_user,
        "query_for_user": engine.query_for_user,
        "estimate_custom_memory": lambda: 8 + (engine._sdk.cache.size // 1000) + 2,
    }
    return module