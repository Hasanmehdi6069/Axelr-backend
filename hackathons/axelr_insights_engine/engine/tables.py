"""axelr-insights-engine — Tables namespace.

Every TablesDB operation. Exposes:
  * CRUD: create, read, update, upsert, delete, exists, restore
  * Query: query, query_all, stream, count
  * Batch: batch_create, batch_upsert, batch_delete
  * Elite: soft delete, optimistic locking, idempotency, ACLs

All calls go through the SDK choke point, so resilience is inherited.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from appwrite.id import ID
from appwrite.permission import Permission
from appwrite.query import Query
from appwrite.role import Role

from .errors import (
    APIError,
    IdempotencyConflictError,
    NotFoundError,
    ValidationFailed,
    VersionConflictError,
)
from .errors import ensure_valid_row_id, sanitize_row_id, validate_data, make_row_id
from .resilience import IdempotencyStore, stable_hash
from .models import QueryFilter, QueryOptions
from .sdk import AppwriteSDK

_PAGE_SIZE = 100
_MAX_PAGE = 5000
_IDEM_PREFIX = "__idem_"


class TablesNamespace:
    """Full-featured wrapper for a single Appwrite TablesDB database."""

    def __init__(self, sdk: AppwriteSDK, *, idem: IdempotencyStore | None = None) -> None:
        self._sdk = sdk
        self._config = sdk.config
        self._log = sdk.log.with_(component="tables")
        self._idem = idem or IdempotencyStore(max_entries=10_000, ttl_s=86_400.0)

    # ── Identity & permissions ───────────────────────────────────────

    @staticmethod
    def new_row_id() -> str:
        """Generate a new Appwrite-compatible row ID."""
        return ID.unique()

    @staticmethod
    def permissions_for_user(user_id: str, *, read_only: bool = False) -> list[str]:
        """Build per-row ACLs so only `user_id` can access the row."""
        if not user_id:
            raise ValidationFailed("user_id required for permissions")
        role = Role.user(user_id)
        perms = [Permission.read(role)]
        if not read_only:
            perms.extend([Permission.update(role), Permission.delete(role)])
        return perms

    @staticmethod
    def permissions_public_read() -> list[str]:
        return [Permission.read(Role.any())]

    @staticmethod
    def permissions_public_full() -> list[str]:
        role = Role.any()
        return [
            Permission.read(role), Permission.update(role), Permission.delete(role),
        ]

    # ── Create ───────────────────────────────────────────────────────

    async def create(
        self, table: str, data: dict[str, Any], *,
        row_id: str | None = None,
        permissions: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a row. Idempotent when `idempotency_key` is provided."""
        if not table:
            raise ValidationFailed("table required")
        data = validate_data(data)
        rid = sanitize_row_id(row_id) if row_id else self.new_row_id()

        if idempotency_key:
            prior = self._idem.lookup(table, idempotency_key, data)
            if prior is not None:
                return prior

        payload: dict[str, Any] = {
            "database_id": self._config.database_id,
            "table_id": table, "row_id": rid, "data": data,
        }
        if permissions:
            payload["permissions"] = permissions

        row = await self._sdk.call(
            "tables.create", lambda: self._sdk.tables.create_row(**payload),
        )
        if idempotency_key:
            self._idem.store(table, idempotency_key, data, row)
        self._sdk.cache.delete(("read", table, rid))
        self._log.debug("row_created", table=table, row_id=rid)
        return row

    # ── Read ─────────────────────────────────────────────────────────

    async def read(
        self, table: str, row_id: str, *,
        include_deleted: bool = False, cacheable: bool = True,
    ) -> dict[str, Any] | None:
        """Read a row. Returns `None` on 404 (not an error)."""
        if not table or not row_id:
            raise ValidationFailed("table and row_id required")
        row_id = ensure_valid_row_id(row_id)
        try:
            row = await self._sdk.call_read(
                "tables.read",
                lambda: self._sdk.tables.get_row(
                    database_id=self._config.database_id,
                    table_id=table, row_id=row_id,
                ),
                cache_parts=("read", table, row_id),
            )
        except NotFoundError:
            return None
        if not include_deleted and row.get("_deleted"):
            return None
        return row

    async def exists(self, table: str, row_id: str) -> bool:
        """Cheap existence check — bypasses the cache."""
        try:
            row = await self._sdk.call(
                "tables.exists",
                lambda: self._sdk.tables.get_row(
                    database_id=self._config.database_id,
                    table_id=table,
                    row_id=row_id,
                ),
            )
            return not row.get("_deleted")
        except NotFoundError:
            return False

    # ── Update ───────────────────────────────────────────────────────

    async def update(
        self, table: str, row_id: str, data: dict[str, Any], *,
        expected_version: int | None = None,
        increment_version: bool = False,
        permissions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update a row. Optional optimistic lock via `expected_version`."""
        if not table or not row_id:
            raise ValidationFailed("table and row_id required")
        row_id = ensure_valid_row_id(row_id)
        data = validate_data(data)

        payload_data = dict(data)

        if expected_version is not None or increment_version:
            current = await self.read(table, row_id, cacheable=False)
            if current is None:
                raise NotFoundError(f"row '{row_id}' not found in '{table}'")
            actual = int(current.get("_version", 1) or 1)
            if expected_version is not None and actual != expected_version:
                raise VersionConflictError(
                    f"expected version {expected_version}, got {actual} on "
                    f"{table}/{row_id}"
                )
            payload_data["_version"] = actual + 1

        payload: dict[str, Any] = {
            "database_id": self._config.database_id,
            "table_id": table,
            "row_id": row_id,
            "data": payload_data,
        }
        if permissions:
            payload["permissions"] = permissions

        row = await self._sdk.call(
            "tables.update",
            lambda: self._sdk.tables.update_row(**payload),
        )
        self._sdk.cache.delete(("read", table, row_id))
        return row

    async def soft_delete(
        self, table: str, row_id: str,
    ) -> dict[str, Any]:
        """Mark a row deleted without physically removing it."""
        return await self.update(
            table, row_id, {"_deleted": True}, increment_version=True,
        )

    async def restore(self, table: str, row_id: str) -> dict[str, Any]:
        """Un-mark a soft-deleted row."""
        return await self.update(
            table, row_id, {"_deleted": False}, increment_version=True,
        )

    # ── Upsert ───────────────────────────────────────────────────────

    async def upsert(
        self, table: str, row_id: str, data: dict[str, Any], *,
        permissions: list[str] | None = None,
        increment_version: bool = True,
    ) -> dict[str, Any]:
        """Insert-or-update. Requires the caller to supply a stable row_id."""
        if not table or not row_id:
            raise ValidationFailed("table and row_id required")
        row_id = ensure_valid_row_id(row_id)
        data = validate_data(data)
        payload_data = dict(data)
        if increment_version:
            current = await self.read(table, row_id, cacheable=False)
            payload_data["_version"] = int(current.get("_version", 0) or 0) + 1 if current else 1

        payload: dict[str, Any] = {
            "database_id": self._config.database_id,
            "table_id": table,
            "row_id": row_id,
            "data": payload_data,
        }
        if permissions:
            payload["permissions"] = permissions

        row = await self._sdk.call(
            "tables.upsert",
            lambda: self._sdk.tables.upsert_row(**payload),
        )
        self._sdk.cache.delete(("read", table, row_id))
        return row

    # ── Delete ───────────────────────────────────────────────────────

    async def delete(self, table: str, row_id: str, *, soft: bool = False) -> bool:
        """Delete a row. `soft=True` flips the `_deleted` flag instead."""
        if not table or not row_id:
            raise ValidationFailed("table and row_id required")
        row_id = ensure_valid_row_id(row_id)
        if soft:
            await self.soft_delete(table, row_id)
            return True
        try:
            await self._sdk.call(
                "tables.delete",
                lambda: self._sdk.tables.delete_row(
                    database_id=self._config.database_id,
                    table_id=table,
                    row_id=row_id,
                ),
            )
        except NotFoundError:
            return False
        self._sdk.cache.delete(("read", table, row_id))
        return True

    # ── Query ────────────────────────────────────────────────────────

    async def query(
        self,
        table: str,
        options: QueryOptions | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Filtered + ordered query, single page."""
        opts = _to_query_options(options, kwargs)
        queries = self._build_queries(opts)
        queries.append(Query.limit(min(opts.limit or _PAGE_SIZE, _PAGE_SIZE)))
        if opts.cursor:
            queries.append(Query.cursor_after(opts.cursor))
        elif opts.offset:
            queries.append(Query.offset(opts.offset))

        res = await self._sdk.call(
            "tables.query",
            lambda: self._sdk.tables.list_rows(
                database_id=self._config.database_id,
                table_id=table, queries=queries,
            ),
            dedup_key=f"query:{table}:{stable_hash(opts.model_dump())}",
        )
        rows = _rows(res)
        if not opts.include_deleted:
            rows = [r for r in rows if not r.get("_deleted")]
        return rows

    async def query_all(
        self,
        table: str,
        options: QueryOptions | dict[str, Any] | None = None,
        *,
        cap: int = _MAX_PAGE,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Paginate up to `cap` rows using cursor-based pagination."""
        opts = _to_query_options(options, kwargs)
        cap = min(cap, _MAX_PAGE)
        out: list[dict[str, Any]] = []
        cursor = opts.cursor
        while len(out) < cap:
            page = await self.query(
                table, opts, cursor=cursor, limit=_PAGE_SIZE,
            )
            if not page:
                break
            out.extend(page)
            if len(page) < _PAGE_SIZE:
                break
            cursor = page[-1].get("$id")
        return out[:cap]

    async def stream(
        self,
        table: str,
        options: QueryOptions | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream rows as an async iterator — bounded memory."""
        opts = _to_query_options(options, kwargs)
        cursor = opts.cursor
        while True:
            page = await self.query(
                table, opts, cursor=cursor, limit=_PAGE_SIZE,
            )
            if not page:
                return
            for row in page:
                yield row
            if len(page) < _PAGE_SIZE:
                return
            cursor = page[-1].get("$id")

    async def count(
        self,
        table: str,
        options: QueryOptions | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> int:
        """Total rows matching the filter (uses Appwrite's `total`)."""
        opts = _to_query_options(options, kwargs)
        queries = self._build_queries(opts)
        queries.append(Query.limit(1))
        queries.append(Query.select(["$id"]))
        res = await self._sdk.call(
            "tables.count",
            lambda: self._sdk.tables.list_rows(
                database_id=self._config.database_id,
                table_id=table,
                queries=queries,
                total=True,
            ),
        )
        if isinstance(res, dict):
            return int(res.get("total", 0) or 0)
        return 0

    # ── Batch ────────────────────────────────────────────────────────

    async def batch_create(
        self,
        table: str,
        rows: list[dict[str, Any]],
        *,
        chunk_size: int = _PAGE_SIZE,
    ) -> int:
        """Bulk insert. Best effort — returns count of inserted rows."""
        if not rows:
            return 0
        inserted = 0
        for i in range(0, len(rows), chunk_size):
            chunk = rows[i : i + chunk_size]
            payload = [{"$id": r.get("$id") or self.new_row_id(), **r} for r in chunk]
            try:
                res = await self._sdk.call(
                    "tables.batch_create",
                    lambda p=payload: self._sdk.tables.create_rows(
                        database_id=self._config.database_id,
                        table_id=table,
                        rows=p,
                    ),
                )
                inserted += len(_rows(res)) or len(payload)
            except Exception as e:
                self._log.warn(
                    "batch_create_chunk_failed",
                    table=table, chunk_len=len(chunk), error=str(e)[:120],
                )
        return inserted

    async def batch_upsert(
        self,
        table: str,
        rows: list[dict[str, Any]],
        *,
        chunk_size: int = _PAGE_SIZE,
    ) -> int:
        """Bulk upsert. Requires each row to include `$id`."""
        if not rows:
            return 0
        upserted = 0
        for i in range(0, len(rows), chunk_size):
            chunk = rows[i : i + chunk_size]
            payload = [{"$id": r.get("$id") or self.new_row_id(), **r} for r in chunk]
            try:
                res = await self._sdk.call(
                    "tables.batch_upsert",
                    lambda p=payload: self._sdk.tables.upsert_rows(
                        database_id=self._config.database_id,
                        table_id=table,
                        rows=p,
                    ),
                )
                upserted += len(_rows(res)) or len(payload)
            except Exception as e:
                self._log.warn(
                    "batch_upsert_chunk_failed",
                    table=table, chunk_len=len(chunk), error=str(e)[:120],
                )
        return upserted

    async def batch_delete(
        self,
        table: str,
        row_ids: list[str],
        *,
        chunk_size: int = _PAGE_SIZE,
    ) -> int:
        """Bulk delete by row ID."""
        if not row_ids:
            return 0
        deleted = 0
        for i in range(0, len(row_ids), chunk_size):
            chunk = row_ids[i : i + chunk_size]
            try:
                await self._sdk.call(
                    "tables.batch_delete",
                    lambda c=chunk: self._sdk.tables.delete_rows(
                        database_id=self._config.database_id,
                        table_id=table,
                        rows=c,
                    ),
                )
                deleted += len(chunk)
                for rid in chunk:
                    self._sdk.cache.delete(("read", table, rid))
            except Exception as e:
                self._log.warn(
                    "batch_delete_chunk_failed",
                    table=table, chunk_len=len(chunk), error=str(e)[:120],
                )
        return deleted

    # ── Bulk helpers ────────────────────────────────────────────────

    async def iter_batches(self, table: str, batch_size: int = 100,
                           options: QueryOptions | None = None):
        """Yield batches of rows as lists, for memory-bounded processing."""
        opts = options or QueryOptions()
        cursor = opts.cursor
        while True:
            page = await self.query(table, opts, cursor=cursor, limit=batch_size)
            if not page: return
            yield page
            if len(page) < batch_size: return
            cursor = page[-1].get("$id")

    async def bulk_read(self, table: str, row_ids: list[str],
                        *, concurrency: int = 8) -> list[dict[str, Any] | None]:
        """Fetch many rows by ID, bounded concurrency."""
        results: list[dict[str, Any] | None] = [None] * len(row_ids)
        sem = asyncio.Semaphore(concurrency)

        async def _one(idx: int, rid: str) -> None:
            async with sem:
                try: results[idx] = await self.read(table, rid)
                except Exception: results[idx] = None

        await asyncio.gather(*[_one(i, r) for i, r in enumerate(row_ids)])
        return results

    async def delete_by_filter(self, table: str, filters: list[QueryFilter] | None = None,
                               *, batch_size: int = 100, max_deletes: int = 10_000) -> int:
        """Delete all rows matching the filter. Returns count deleted."""
        opts = QueryOptions(filters=filters or [], limit=batch_size)
        deleted = 0
        while deleted < max_deletes:
            page = await self.query(table, opts, limit=batch_size)
            if not page: break
            ids = [r["$id"] for r in page if "$id" in r]
            if not ids: break
            deleted += await self.batch_delete(table, ids)
            if len(page) < batch_size: break
        return deleted

    async def latest(self, table: str, *,
                     order_field: str = "ts",
                     filters: list[QueryFilter] | None = None) -> dict[str, Any] | None:
        """Return the most recent row matching filters (one call)."""
        opts = QueryOptions(filters=filters or [],
                            order_by=order_field, order_desc=True, limit=1)
        rows = await self.query(table, opts)
        return rows[0] if rows else None

    async def deduplicate_existing(self, table: str, *,
                                    key_field: str,
                                    keep: str = "newest") -> int:
        """Find and delete duplicate rows sharing the same key_field value.

        Keeps either the newest or oldest by $createdAt. Returns count deleted.
        """
        all_rows = await self.query_all(table, order_by="$createdAt", order_desc=(keep == "newest"))
        seen: dict[Any, str] = {}
        to_delete: list[str] = []
        for r in all_rows:
            k = r.get(key_field)
            if k is None: continue
            if k in seen: to_delete.append(r["$id"])
            else: seen[k] = r["$id"]
        if to_delete: return await self.batch_delete(table, to_delete)
        return 0

    async def bulk_upsert_from_iterable(self, table: str, rows: Any,
                                        *, batch_size: int = 100) -> int:
        """Upsert rows from any iterable (list, generator, async iterator)."""
        total = 0
        buffer: list[dict[str, Any]] = []
        if hasattr(rows, "__aiter__"):
            async for r in rows:
                buffer.append(r)
                if len(buffer) >= batch_size:
                    total += await self.batch_upsert(table, buffer)
                    buffer = []
        else:
            for r in rows:
                buffer.append(r)
                if len(buffer) >= batch_size:
                    total += await self.batch_upsert(table, buffer)
                    buffer = []
        if buffer: total += await self.batch_upsert(table, buffer)
        return total
    # ── Query building ───────────────────────────────────────────────

    def _build_queries(self, opts: QueryOptions) -> list[str]:
        queries: list[str] = []
        for f in opts.filters:
            queries.append(_filter_to_query(f))
        if opts.order_by:
            fn = Query.order_desc if opts.order_desc else Query.order_asc
            queries.append(fn(opts.order_by))
        if opts.select:
            queries.append(Query.select(list(opts.select)))
        return queries


# ── Helpers ───────────────────────────────────────────────────────────

def _to_query_options(
    options: QueryOptions | dict[str, Any] | None,
    extra: dict[str, Any],
) -> QueryOptions:
    if isinstance(options, QueryOptions):
        if extra:
            return options.model_copy(update=extra)
        return options
    data = dict(options or {})
    data.update({k: v for k, v in extra.items() if k not in data or data[k] is None})
    return QueryOptions(**data)


def _filter_to_query(f: QueryFilter) -> str:
    op = f.op
    field, value = f.field, f.value
    if op == "eq":
        return Query.equal(field, value)
    if op == "ne":
        return Query.not_equal(field, value)
    if op == "gt":
        return Query.greater_than(field, value)
    if op == "gte":
        return Query.greater_than_equal(field, value)
    if op == "lt":
        return Query.less_than(field, value)
    if op == "lte":
        return Query.less_than_equal(field, value)
    if op == "in":
        return Query.equal(field, value)
    if op == "not_in":
        return Query.not_equal(field, value)
    if op == "starts_with":
        return Query.starts_with(field, value)
    if op == "ends_with":
        return Query.ends_with(field, value)
    if op == "contains":
        return Query.contains(field, value)
    if op == "search":
        return Query.search(field, value)
    if op == "is_null":
        return Query.is_null(field)
    if op == "is_not_null":
        return Query.is_not_null(field)
    if op == "between":
        lo, hi = value
        return Query.between(field, lo, hi)
    raise ValidationFailed(f"unsupported filter op '{op}'")


def _rows(res: Any) -> list[dict[str, Any]]:
    if isinstance(res, dict):
        raw = res.get("rows") or []
    else:
        raw = getattr(res, "rows", []) or []
    return [r if isinstance(r, dict) else dict(r) for r in raw]