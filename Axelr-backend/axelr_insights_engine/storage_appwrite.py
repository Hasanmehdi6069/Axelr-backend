"""Appwrite TablesDB backend.

Honest notes
------------
* Appwrite caps list operations at 100 rows/page — we paginate every
  read. `_all_audit` stops at `cap` (default 5000) to bound memory.
* There is no server-side aggregation. `pipeline_stats` and
  `daily_counts` fetch pages and aggregate in Python. For very high
  volumes run a scheduled external aggregator — do NOT add a cron to a
  serverless function; it will time out.
* Appwrite has no object attribute. We store `payload` as JSON text and
  denormalise `user_id`, `pipeline_id`, `status`, `latency_ms`, `day`
  into indexed columns. Analytics queries filter on those columns; the
  raw payload remains available for drilldown.
* The Python SDK is synchronous. Every call runs via `asyncio.to_thread`
  with jittered retry. Failures trip a `CircuitBreaker` so a degraded
  Appwrite does not consume the whole worker pool.
* Schema is auto-created on `init()`. Indexes are always attempted —
  a table created in Appwrite Console will still get its indexes if you
  redeploy.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from .storage_base import (
    CircuitBreaker,
    CircuitOpenError,
    _denorm,
    _finalise_pipeline_row,
    _py_percentiles,
    serialize,
    to_dt,
    to_iso,
)

logger = logging.getLogger("insights.appwrite")

_PAGE = 100
_RETRIES = 3


# ── Schema ────────────────────────────────────────────────────────────

_APPWRITE_SCHEMA: dict[str, dict[str, Any]] = {
    "audit_events": {
        "attributes": [
            {"key": "kind",       "type": "string",   "size": 64,      "required": True},
            {"key": "payload",    "type": "string",   "size": 65535,   "required": False},
            {"key": "ts",         "type": "datetime",                  "required": True},
            {"key": "user_id",    "type": "string",   "size": 255,     "required": False},
            {"key": "pipeline_id","type": "string",   "size": 255,     "required": False},
            {"key": "status",     "type": "string",   "size": 32,      "required": False},
            {"key": "latency_ms", "type": "double",                    "required": False},
            {"key": "day",        "type": "string",   "size": 10,      "required": False},
        ],
        "indexes": [
            ("kind_ts",     "key", ["kind", "ts"],             ["ASC",  "DESC"]),
            ("ts_desc",     "key", ["ts"],                     ["DESC"]),
            ("pipeline_ts", "key", ["pipeline_id", "ts"],      ["ASC",  "DESC"]),
            ("day_kind",    "key", ["day", "kind"],            ["ASC",  "ASC"]),
        ],
    },
    "indexed_documents": {
        "attributes": [
            {"key": "doc_id",  "type": "string",   "size": 255,     "required": True},
            {"key": "content", "type": "string",   "size": 1048576, "required": True},
            {"key": "meta",    "type": "string",   "size": 65535,   "required": False},
            {"key": "ts",      "type": "datetime",                  "required": True},
        ],
        "indexes": [
            ("doc_id_unique", "unique", ["doc_id"], ["ASC"]),
            ("ts_desc",       "key",    ["ts"],     ["DESC"]),
        ],
    },
}


# ── Appwrite response parsing ─────────────────────────────────────────

def _row_id(row: Any) -> str:
    if isinstance(row, dict):
        return str(row.get("$id") or row.get("id") or "")
    for attr in ("id", "$id"):
        v = getattr(row, attr, None)
        if v:
            return str(v)
    return ""


def _rows(r: Any) -> list[dict[str, Any]]:
    raw = r.get("rows") if isinstance(r, dict) else getattr(r, "rows", [])
    out: list[dict[str, Any]] = []
    for row in raw or []:
        if isinstance(row, dict):
            out.append(row)
        elif hasattr(row, "model_dump"):
            out.append(row.model_dump())
        else:
            out.append(dict(row))
    return out


def _count_rows(r: Any) -> int:
    if isinstance(r, dict):
        return len(r.get("rows") or [])
    return len(getattr(r, "rows", []) or [])


def _hydrate(row: dict[str, Any]) -> dict[str, Any]:
    """Denormalised columns + payload JSON → flat audit event."""
    out: dict[str, Any] = {
        "_id": row.get("$id") or row.get("id"),
        "kind": row.get("kind"),
        "ts": row.get("ts"),
    }
    raw = row.get("payload")
    if isinstance(raw, str) and raw:
        try:
            out.update(json.loads(raw))
        except json.JSONDecodeError:
            logger.warning("bad payload json id=%s", out["_id"])
    return out


def _doc_row(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("meta")
    try:
        meta = json.loads(raw) if isinstance(raw, str) and raw else {}
    except json.JSONDecodeError:
        meta = {}
    return {
        "_id": row.get("$id") or row.get("id"),
        "doc_id": row.get("doc_id"),
        "content": row.get("content"),
        "meta": meta,
        "ts": row.get("ts") or row.get("$createdAt"),
    }


# ── Backend ───────────────────────────────────────────────────────────

class AppwriteBackend:
    """Appwrite TablesDB storage backend.
    
    Honest production limitations
    -----------------------------
    1. **SDK Signature Risk**: create_table/create_index parameters may differ between Appwrite SDK versions.
       If you see "unexpected keyword argument 'attributes'", create tables/indexes manually in the Appwrite Console.
    2. **Bulk Insert Shape**: Some SDK versions require rows to be wrapped in {"$id": ..., "data": ...} instead of raw dicts.
       The code auto-detects this and retries automatically, but if bulk inserts persistently fail, verify your SDK version.
    3. **Datetime Query Compatibility**: If time-range queries return 0 rows, the SDK may require datetime objects instead of ISO strings.
       The code retries automatically, but this adds minor latency to query operations.
    4. **No Server-Side Aggregation**: All analytics are computed client-side. For >10k events, deploy an external aggregator.
    5. **Single-Region Only**: This backend only works with one Appwrite project/region. Multi-region requires a proxy layer.
    6. **Circuit Breaker**: Implements a basic circuit breaker to avoid overwhelming Appwrite Cloud during outages.
    """
    def __init__(self, endpoint: str, project: str, api_key: str,
                 database_id: str) -> None:
        self._endpoint = endpoint
        self._project = project
        self._api_key = api_key
        self._db_id = database_id
        self._client: Any = None
        self._tables: Any = None
        self._breaker = CircuitBreaker(threshold=10, cooldown=60.0)

    # ── SDK bootstrap + choke point ──

    def _ensure(self) -> None:
        if self._client is not None:
            return
        from appwrite.client import Client
        from appwrite.services.tables_db import TablesDB
        self._client = (
            Client()
            .set_endpoint(self._endpoint)
            .set_project(self._project)
            .set_key(self._api_key)
        )
        self._tables = TablesDB(self._client)

    async def _call(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        """Single choke point. Retries + circuit breaker live here."""
        if not await self._breaker.allow():
            raise CircuitOpenError("appwrite circuit breaker is open")
        last: Exception | None = None
        for attempt in range(_RETRIES):
            try:
                result = await asyncio.to_thread(fn, *args, **kwargs)
                await self._breaker.record_success()
                return result
            except Exception as e:
                last = e
                if attempt < _RETRIES - 1:
                    await asyncio.sleep(
                        0.15 * (2 ** attempt) + random.random() * 0.1
                    )
        await self._breaker.record_failure()
        raise last if last else RuntimeError("appwrite retry exhausted")

    async def init(self) -> None:
        self._ensure()
        # Probe — a misconfigured key should fail at startup, not at the
        # first write.
        await self._call(self._tables.list_tables)
        await self._ensure_schema()
        logger.info("appwrite backend ready db=%s", self._db_id)

    async def _ensure_schema(self) -> None:
        """Ensure database schema exists.
        Honest notes about Appwrite SDK compatibility:
        * If table creation fails with "unexpected keyword argument 'attributes'",
          the SDK expects `columns=` instead of `attributes=` — create tables manually in Appwrite Console.
        * If index creation fails with "unexpected keyword argument 'attributes'",
          the SDK expects `columns=` instead of `attributes=` — create indexes manually in Appwrite Console.
        * This method is best-effort; manual schema creation is the most reliable approach for production.
        """
        existing = await self._call(self._tables.list_tables)
        raw = (
            existing.get("tables") if isinstance(existing, dict)
            else getattr(existing, "tables", [])
        )
        have = {
            (t.get("$id") if isinstance(t, dict) else getattr(t, "$id", None))
            for t in (raw or [])
        }
        for table_id, spec in _APPWRITE_SCHEMA.items():
            if table_id not in have:
                try:
                    await self._call(
                        self._tables.create_table,
                        database_id=self._db_id,
                        table_id=table_id,
                        name=table_id,
                        attributes=spec["attributes"],
                        permissions=[],
                    )
                    logger.info("appwrite created table=%s", table_id)
                except Exception as e:
                    # 409 = created concurrently (eventual consistency).
                    msg = str(e).lower()
                    if "unexpected keyword argument" in msg:
                        logger.critical(
                            "appwrite SDK signature mismatch: create_table rejected 'attributes' parameter. "
                            "Create tables manually in Appwrite Console. table=%s err=%s",
                            table_id, e,
                        )
                    elif "409" not in msg and "already exists" not in msg:
                        logger.error(
                            "appwrite table create failed table=%s err=%s",
                            table_id, e,
                        )
                    continue
            # Always attempt indexes — even for existing tables. Appwrite's
            # create_index is idempotent-ish (409 if exists) and we swallow
            # the 409 below.
            for key, kind, attrs, orders in spec["indexes"]:
                try:
                    await self._call(
                        self._tables.create_index,
                        database_id=self._db_id,
                        table_id=table_id,
                        key=key, type=kind,
                        attributes=attrs, orders=orders,
                    )
                except Exception as e:
                    msg = str(e).lower()
                    if "unexpected keyword argument" in msg:
                        logger.critical(
                            "appwrite SDK signature mismatch: create_index rejected 'attributes' parameter. "
                            "Create indexes manually in Appwrite Console. table=%s index=%s err=%s",
                            table_id, key, e,
                        )
                    else:
                        logger.debug(
                            "appwrite index %s.%s skipped: %s",
                            table_id, key, e,
                        )

    async def close(self) -> None:
        self._client = None
        self._tables = None

    async def health(self) -> dict[str, Any]:
        if self._breaker.is_open:
            return {"healthy": False, "backend": "appwrite",
                    "reason": "circuit_open"}
        try:
            self._ensure()
            await self._call(self._tables.list_tables)
            return {"healthy": True, "backend": "appwrite"}
        except Exception as e:
            return {"healthy": False, "backend": "appwrite",
                    "reason": str(e)[:200]}

    # ── Audit writes ──

    async def insert_audit(
        self, kind: str, payload: dict[str, Any],
    ) -> str | None:
        from appwrite.id import ID
        ts = to_dt(payload.get("ts")) or datetime.now(timezone.utc)
        row = {
            "kind": kind,
            "payload": json.dumps(serialize(payload), separators=(",", ":")),
            "ts": to_iso(ts),
            **_denorm(payload),
        }
        try:
            r = await self._call(
                self._tables.create_row,
                database_id=self._db_id, table_id="audit_events",
                row_id=ID.unique(), data=row,
            )
            return _row_id(r)
        except Exception as e:
            logger.warning("appwrite audit insert failed kind=%s err=%s",
                           kind, e)
            return None

    async def insert_audit_batch(
        self, events: list[dict[str, Any]],
    ) -> int:
        if not events:
            return 0
        from appwrite.id import ID
        now = datetime.now(timezone.utc)
        inserted = 0
        for offset in range(0, len(events), _PAGE):
            chunk = events[offset:offset + _PAGE]
            rows: list[dict[str, Any]] = []
            for e in chunk:
                ts = to_dt(e.get("ts")) or now
                rows.append({
                    "$id": ID.unique(),
                    "kind": e.get("kind", "unknown"),
                    "payload": json.dumps(
                        serialize(e), separators=(",", ":"),
                    ),
                    "ts": to_iso(ts),
                    **_denorm({**e, "ts": ts}),
                })
            try:
                r = await self._call(
                    self._tables.create_rows,
                    database_id=self._db_id,
                    table_id="audit_events", rows=rows,
                )
                inserted += _count_rows(r)
            except Exception as e:
                msg = str(e).lower()
                # Appwrite SDK compatibility check: some versions require rows wrapped in "data" key
                if "invalid row shape" in msg or "missing data field" in msg or "unexpected field" in msg:
                    logger.critical(
                        "appwrite SDK bulk insert shape mismatch: raw row format rejected. "
                        "Retrying with wrapped {\"$id\": ..., \"data\": ...} format. err=%s", e
                    )
                    # Retry with wrapped data format
                    wrapped_rows = [{"$id": row["$id"], "data": row} for row in rows]
                    try:
                        r = await self._call(
                            self._tables.create_rows,
                            database_id=self._db_id,
                            table_id="audit_events", rows=wrapped_rows,
                        )
                        inserted += _count_rows(r)
                        logger.info("appwrite bulk insert succeeded with wrapped format n=%d", len(wrapped_rows))
                        continue
                    except Exception as e2:
                        logger.critical(
                            "appwrite bulk insert failed even with wrapped format. "
                            "Create tables manually and check SDK version. err=%s", e2
                        )
                # Appwrite bulk create is not transactional. On failure we
                # cannot know how many rows landed; we deliberately under-
                # count (0) rather than overcount. Retention math is safer
                # conservative.
                logger.warning(
                    "appwrite bulk insert failed n=%d err=%s", len(rows), e,
                )
        return inserted

    # ── Audit reads ──

    async def list_audit(
        self, *, kind: str, limit: int = 50, offset: int = 0,
        user_id: str | None = None, pipeline_id: str | None = None,
        start: datetime | None = None, end: datetime | None = None,
    ) -> list[dict[str, Any]]:
        from appwrite.query import Query
        q = [
            Query.equal("kind", kind),
            Query.order_desc("ts"),
            Query.limit(min(limit, _PAGE)),
            Query.offset(offset),
        ]
        if user_id:
            q.append(Query.equal("user_id", user_id))
        if pipeline_id:
            q.append(Query.equal("pipeline_id", pipeline_id))
        if start:
            q.append(Query.greater_than_equal("ts", to_iso(start)))
        if end:
            q.append(Query.less_than_equal("ts", to_iso(end)))
        try:
            r = await self._call(
                self._tables.list_rows,
                database_id=self._db_id,
                table_id="audit_events", queries=q,
            )
        except Exception as e:
            msg = str(e).lower()
            # Appwrite SDK compatibility: some versions require datetime objects instead of ISO strings
            if ("invalid query value" in msg and "datetime" in msg) or "ts is not a valid datetime" in msg:
                logger.warning(
                    "appwrite SDK rejected ISO string datetime for ts query, retrying with datetime objects. err=%s", e
                )
                # Retry with datetime objects instead of ISO strings
                q = q.copy()
                if start:
                    # Remove the old ISO string query and add datetime object
                    q = [item for item in q if not (hasattr(item, 'key') and getattr(item, 'key') == 'ts' and getattr(item, 'method') == '>=')]
                    q.append(Query.greater_than_equal("ts", start))
                if end:
                    q = [item for item in q if not (hasattr(item, 'key') and getattr(item, 'key') == 'ts' and getattr(item, 'method') == '<=')]
                    q.append(Query.less_than_equal("ts", end))
                r = await self._call(
                    self._tables.list_rows,
                    database_id=self._db_id,
                    table_id="audit_events", queries=q,
                )
            else:
                raise
        return [_hydrate(row) for row in _rows(r)]

    async def _all_audit(
        self, kind: str, *, since: datetime,
        pipeline_id: str | None = None, cap: int = 5000,
    ) -> list[dict[str, Any]]:
        from appwrite.query import Query
        out: list[dict[str, Any]] = []
        offset = 0
        while offset < cap:
            q = [
                Query.equal("kind", kind),
                Query.order_desc("ts"),
                Query.greater_than_equal("ts", to_iso(since)),
                Query.limit(_PAGE),
                Query.offset(offset),
            ]
            if pipeline_id:
                q.append(Query.equal("pipeline_id", pipeline_id))
            try:
                r = await self._call(
                    self._tables.list_rows,
                    database_id=self._db_id,
                    table_id="audit_events", queries=q,
                )
            except Exception as e:
                msg = str(e).lower()
                # Appwrite SDK compatibility: some versions require datetime objects instead of ISO strings
                if ("invalid query value" in msg and "datetime" in msg) or "ts is not a valid datetime" in msg:
                    logger.warning(
                        "appwrite SDK rejected ISO string datetime for ts query in _all_audit, retrying with datetime object. offset=%d err=%s", offset, e
                    )
                    # Retry with datetime object instead of ISO string
                    q_retry = q.copy()
                    q_retry = [item for item in q_retry if not (hasattr(item, 'key') and getattr(item, 'key') == 'ts' and getattr(item, 'method') == '>=')]
                    q_retry.append(Query.greater_than_equal("ts", since))
                    try:
                        r = await self._call(
                            self._tables.list_rows,
                            database_id=self._db_id,
                            table_id="audit_events", queries=q_retry,
                        )
                    except Exception as e2:
                        logger.warning(
                            "appwrite page failed even with datetime object offset=%d err=%s", offset, e2,
                        )
                        break
                else:
                    logger.warning(
                        "appwrite page failed offset=%d err=%s", offset, e,
                    )
                break
            page = _rows(r)
            out.extend(_hydrate(row) for row in page)
            if len(page) < _PAGE:
                break
            offset += _PAGE
        return out

    # ── Analytics (in-Python) ──

    async def latency_stats(
        self, *, pipeline_id: str | None, since: datetime,
    ) -> dict[str, Any]:
        rows = await self._all_audit(
            "node_execution", since=since, pipeline_id=pipeline_id,
        )
        vals = [
            float(r["latency_ms"]) for r in rows
            if isinstance(r.get("latency_ms"), (int, float))
        ]
        return _py_percentiles(vals, backend="appwrite")

    async def pipeline_stats(
        self, *, since: datetime, limit: int = 50,
    ) -> list[dict[str, Any]]:
        rows = await self._all_audit("node_execution", since=since)
        agg: dict[str, dict[str, Any]] = {}
        for r in rows:
            pid = r.get("pipeline_id") or "unknown"
            slot = agg.setdefault(pid, {
                "pipeline_id": pid, "total": 0, "errors": 0, "lat": [],
            })
            slot["total"] += 1
            if r.get("status") == "error":
                slot["errors"] += 1
            v = r.get("latency_ms")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                slot["lat"].append(float(v))
        out = []
        for slot in agg.values():
            lat = slot.pop("lat")
            out.append(_finalise_pipeline_row(slot, lat))
        out.sort(key=lambda s: s["total"], reverse=True)
        return out[:limit]

    async def daily_counts(
        self, *, since: datetime,
    ) -> list[dict[str, Any]]:
        rows = await self._all_audit("transaction", since=since)
        for k in ("node_execution", "pipeline_state", "system_metric"):
            rows.extend(await self._all_audit(k, since=since))
        buckets: dict[tuple[str, str], int] = {}
        for r in rows:
            ts = to_dt(r.get("ts"))
            if ts is None:
                continue
            key = (to_iso(ts)[:10], r.get("kind", "unknown"))
            buckets[key] = buckets.get(key, 0) + 1
        return [
            {"day": d, "kind": k, "count": c}
            for (d, k), c in sorted(buckets.items(), reverse=True)
        ]

    async def latest_pipeline_state(
        self, *, pipeline_id: str,
    ) -> dict[str, Any] | None:
        rows = await self.list_audit(
            kind="pipeline_state", limit=1, pipeline_id=pipeline_id,
        )
        return rows[0] if rows else None

    def health_check(self) -> bool:
        """Appwrite backend health check - returns True if properly configured"""
        return all([
            self._endpoint is not None,
            self._project is not None,
            self._api_key is not None,
            self._db_id is not None,
            len(self._endpoint) > 0,
            len(self._project) > 0,
            len(self._api_key) > 0,
            len(self._db_id) > 0
        ])

    async def purge_audit(self, *, older_than_days: int) -> int:
        from appwrite.query import Query
        cutoff = to_iso(
            datetime.now(timezone.utc) - timedelta(days=older_than_days)
        )
        deleted = 0
        while True:
            r = await self._call(
                self._tables.list_rows,
                database_id=self._db_id,
                table_id="audit_events",
                # Retention is defined on event time (`ts`), which is
                # indexed by `ts_desc`. `$createdAt` is ingestion time —
                # a backfilled event would otherwise be kept forever.
                queries=[
                    Query.less_than("ts", cutoff),
                    Query.limit(_PAGE),
                ],
            )
            rows = _rows(r)
            if not rows:
                break
            for row in rows:
                rid = _row_id(row)
                if not rid:
                    continue
                try:
                    await self._call(
                        self._tables.delete_row,
                        database_id=self._db_id,
                        table_id="audit_events", row_id=rid,
                    )
                    deleted += 1
                except Exception as e:
                    logger.debug("delete failed id=%s err=%s", rid, e)
            if len(rows) < _PAGE:
                break
        return deleted

    # ── Documents ──

    async def upsert_document(
        self, doc_id: str, content: str, meta: dict[str, Any],
    ) -> dict[str, Any]:
        data = {
            "doc_id": doc_id,
            "content": content,
            "meta": json.dumps(serialize(meta), separators=(",", ":")),
            "ts": to_iso(datetime.now(timezone.utc)),
        }
        # Update first; create only on 404. Any other error (500, 429,
        # timeout) re-raises so the caller sees it, rather than silently
        # creating a duplicate row.
        try:
            await self._call(
                self._tables.update_row,
                database_id=self._db_id,
                table_id="indexed_documents",
                row_id=doc_id, data=data,
            )
        except Exception as e:
            msg = str(e).lower()
            if "404" not in msg and "not found" not in msg:
                raise
            # Use doc_id as $id so subsequent update_row calls find this row.
            await self._call(
                self._tables.create_row,
                database_id=self._db_id,
                table_id="indexed_documents",
                row_id=doc_id, data=data,
            )
        return {"doc_id": doc_id, "content": content, "meta": meta}

    async def list_documents(
        self, limit: int = 200,
    ) -> list[dict[str, Any]]:
        from appwrite.query import Query
        r = await self._call(
            self._tables.list_rows,
            database_id=self._db_id,
            table_id="indexed_documents",
            queries=[Query.order_desc("ts"), Query.limit(min(limit, _PAGE))],
        )
        return [_doc_row(row) for row in _rows(r)]

    async def delete_document(self, doc_id: str) -> bool:
        from appwrite.query import Query
        r = await self._call(
            self._tables.list_rows,
            database_id=self._db_id,
            table_id="indexed_documents",
            queries=[Query.equal("doc_id", doc_id), Query.limit(1)],
        )
        rows = _rows(r)
        if not rows:
            return False
        rid = _row_id(rows[0])
        if not rid:
            return False
        try:
            await self._call(
                self._tables.delete_row,
                database_id=self._db_id,
                table_id="indexed_documents", row_id=rid,
            )
            return True
        except Exception as e:
            logger.debug("delete doc failed id=%s err=%s", doc_id, e)
            return False