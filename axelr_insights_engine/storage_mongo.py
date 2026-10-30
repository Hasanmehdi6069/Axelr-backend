"""MongoDB backend.

Uses `pymongo.AsyncMongoClient` (available since 4.9). Motor is
deprecated and its eventual removal will break downstream code — this
implementation sidesteps that entirely.

Index strategy:
  * Every audit query filters on `kind` + a second dimension + `ts`.
    We index that tuple, plus a TTL on `ts` so retention is enforced by
    the server, not a cron.
  * The TTL index also serves as the `ts` index for read queries — Mongo
    rejects a plain `ts` index alongside a TTL on the same key.

Aggregation:
  * `latency_stats` uses native `$percentile` (Mongo 7.0+). If the server
    is older, we fall back to Python-side percentile math. The fallback
    is slower but exact.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import settings
from .storage_base import (
    _finalise_pipeline_row,
    _py_percentiles,
    serialize,
)

logger = logging.getLogger("insights.mongo")

_AUDIT_KINDS = frozenset(
    ("transaction", "node_execution", "pipeline_state", "system_metric")
)


def _pct(v: Any) -> float | None:
    """Mongo `$percentile` returns `[v]` for a single-p quantile."""
    if isinstance(v, list) and v:
        return float(v[0])
    return None


def _pipeline_row(r: dict[str, Any]) -> dict[str, Any]:
    pid = r.pop("_id", "unknown")
    return _finalise_pipeline_row({"pipeline_id": pid, **r}, [])


class MongoBackend:
    def __init__(self, uri: str, db_name: str) -> None:
        self._uri = uri
        self._db_name = db_name
        self._client: Any = None
        self._db: Any = None

    # ── Lifecycle ──

    async def init(self) -> None:
        from pymongo import AsyncMongoClient
        self._client = AsyncMongoClient(
            self._uri, maxPoolSize=10, serverSelectionTimeoutMS=5000,
        )
        self._db = self._client[self._db_name]
        await self._client.admin.command("ping")
        await self._ensure_indexes()
        logger.info("mongo backend ready db=%s", self._db_name)

    async def _ensure_indexes(self) -> None:
        audit = self._db["audit_events"]
        docs = self._db["indexed_documents"]

        for keys in (
            [("kind", 1), ("ts", -1)],
            [("kind", 1), ("pipeline_id", 1), ("ts", -1)],
            [("kind", 1), ("user_id", 1), ("ts", -1)],
            [("kind", 1), ("status", 1), ("ts", -1)],
        ):
            try:
                await audit.create_index(keys)
            except Exception as e:
                logger.debug("audit index %s skipped: %s", keys, e)

        if settings.audit_retention_days > 0:
            try:
                await audit.create_index(
                    "ts",
                    expireAfterSeconds=settings.audit_retention_days * 86400,
                )
            except Exception as e:
                logger.debug("ttl index skipped: %s", e)

        try:
            await docs.create_index("doc_id", unique=True)
            await docs.create_index([("ts", -1)])
        except Exception as e:
            logger.debug("doc indexes skipped: %s", e)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
            self._db = None

    async def health(self) -> dict[str, Any]:
        try:
            await self._client.admin.command("ping")
            return {"healthy": True, "backend": "mongo"}
        except Exception as e:
            return {"healthy": False, "backend": "mongo",
                    "reason": str(e)[:200]}

    # ── Audit writes ──

    async def insert_audit(self, kind: str,
                           payload: dict[str, Any]) -> str | None:
        if kind not in _AUDIT_KINDS:
            logger.warning("unknown audit kind=%r", kind)
        doc = {"kind": kind, **serialize(payload)}
        doc.setdefault("ts", datetime.now(timezone.utc))
        try:
            r = await self._db["audit_events"].insert_one(doc)
            return str(r.inserted_id)
        except Exception as e:
            logger.warning("mongo audit insert failed kind=%s err=%s", kind, e)
            return None

    async def insert_audit_batch(self, events: list[dict[str, Any]]) -> int:
        if not events:
            return 0
        now = datetime.now(timezone.utc)
        docs = [{"kind": e.get("kind", "unknown"), **serialize(e)}
                for e in events]
        for d in docs:
            d.setdefault("ts", now)
        try:
            r = await self._db["audit_events"].insert_many(docs, ordered=False)
            return len(r.inserted_ids)
        except Exception as e:
            # PyMongo raises BulkWriteError on partial success; the details
            # dict exposes nInserted. We return that — never overcount.
            details = getattr(e, "details", None) or {}
            n = int(details.get("nInserted", 0))
            logger.warning("mongo bulk insert partial n=%d err=%s", n, e)
            return n

    # ── Audit reads ──

    async def list_audit(
        self, *, kind: str, limit: int = 50, offset: int = 0,
        user_id: str | None = None, pipeline_id: str | None = None,
        start: datetime | None = None, end: datetime | None = None,
    ) -> list[dict[str, Any]]:
        q: dict[str, Any] = {"kind": kind}
        if user_id:
            q["user_id"] = user_id
        if pipeline_id:
            q["pipeline_id"] = pipeline_id
        if start or end:
            q["ts"] = {k: v for k, v in (("$gte", start), ("$lte", end)) if v}
        out: list[dict[str, Any]] = []
        cursor = (
            self._db["audit_events"].find(q)
            .sort("ts", -1).skip(offset).limit(min(limit, 1000))
        )
        async for row in cursor:
            row["_id"] = str(row["_id"])
            out.append(row)
        return out

    # ── Analytics ──

    async def latency_stats(
        self, *, pipeline_id: str | None, since: datetime,
    ) -> dict[str, Any]:
        match: dict[str, Any] = {
            "kind": "node_execution", "ts": {"$gte": since},
        }
        if pipeline_id:
            match["pipeline_id"] = pipeline_id

        pipeline = [
            {"$match": match},
            {"$group": {
                "_id": None,
                "count": {"$sum": 1},
                "p50": {"$percentile": {"input": "$latency_ms",
                                        "p": [0.5], "method": "approximate"}},
                "p95": {"$percentile": {"input": "$latency_ms",
                                        "p": [0.95], "method": "approximate"}},
                "p99": {"$percentile": {"input": "$latency_ms",
                                        "p": [0.99], "method": "approximate"}},
                "mean": {"$avg": "$latency_ms"},
                "min": {"$min": "$latency_ms"},
                "max": {"$max": "$latency_ms"},
            }},
        ]
        try:
            rows = await self._db["audit_events"].aggregate(pipeline).to_list(length=1)
        except Exception:
            return await self._latency_fallback(match)

        if not rows:
            return {"count": 0, "p50": None, "p95": None, "p99": None,
                    "mean": None, "min": None, "max": None, "backend": "mongo"}
        r = rows[0]
        return {
            "count": r["count"],
            "p50": _pct(r.get("p50")), "p95": _pct(r.get("p95")),
            "p99": _pct(r.get("p99")),
            "mean": round(r["mean"], 2) if r.get("mean") is not None else None,
            "min": r.get("min"), "max": r.get("max"),
            "backend": "mongo",
        }

    async def _latency_fallback(self, match: dict[str, Any]) -> dict[str, Any]:
        vals: list[float] = []
        async for d in self._db["audit_events"].find(
            match, {"latency_ms": 1},
        ):
            v = d.get("latency_ms")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                vals.append(float(v))
        return _py_percentiles(vals, backend="mongo-fallback")

    async def pipeline_stats(
        self, *, since: datetime, limit: int = 50,
    ) -> list[dict[str, Any]]:
        pipeline = [
            {"$match": {"kind": "node_execution", "ts": {"$gte": since}}},
            {"$group": {
                "_id": "$pipeline_id",
                "total": {"$sum": 1},
                "errors": {"$sum": {"$cond": [
                    {"$eq": ["$status", "error"]}, 1, 0,
                ]}},
                "avg_latency_ms": {"$avg": "$latency_ms"},
                "max_latency_ms": {"$max": "$latency_ms"},
            }},
            {"$sort": {"total": -1}},
            {"$limit": limit},
        ]
        rows = await self._db["audit_events"].aggregate(pipeline).to_list(limit)
        return [_pipeline_row(r) for r in rows]

    async def daily_counts(self, *, since: datetime) -> list[dict[str, Any]]:
        pipeline = [
            {"$match": {"ts": {"$gte": since}}},
            {"$group": {
                "_id": {
                    "day": {"$dateToString": {"format": "%Y-%m-%d",
                                              "date": "$ts"}},
                    "kind": "$kind",
                },
                "count": {"$sum": 1},
            }},
            {"$sort": {"_id.day": -1}},
        ]
        rows = await self._db["audit_events"].aggregate(pipeline).to_list(1000)
        return [
            {"day": r["_id"]["day"], "kind": r["_id"]["kind"],
             "count": r["count"]}
            for r in rows
        ]

    async def latest_pipeline_state(
        self, *, pipeline_id: str,
    ) -> dict[str, Any] | None:
        doc = await self._db["audit_events"].find_one(
            {"kind": "pipeline_state", "pipeline_id": pipeline_id},
            sort=[("ts", -1)],
        )
        if not doc:
            return None
        doc["_id"] = str(doc["_id"])
        return doc

    async def purge_audit(self, *, older_than_days: int) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        r = await self._db["audit_events"].delete_many({"ts": {"$lt": cutoff}})
        return int(r.deleted_count)

    # ── Documents ──

    async def upsert_document(
        self, doc_id: str, content: str, meta: dict[str, Any],
    ) -> dict[str, Any]:
        await self._db["indexed_documents"].update_one(
            {"doc_id": doc_id},
            {"$set": {
                "doc_id": doc_id, "content": content,
                "meta": serialize(meta),
                "ts": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
        return {"doc_id": doc_id, "content": content, "meta": meta}

    async def list_documents(self, limit: int = 200) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = (
            self._db["indexed_documents"]
            .find({}).sort("ts", -1).limit(min(limit, 1000))
        )
        async for row in cursor:
            row["_id"] = str(row["_id"])
            out.append(row)
        return out

    async def delete_document(self, doc_id: str) -> bool:
        r = await self._db["indexed_documents"].delete_one({"doc_id": doc_id})
        return int(r.deleted_count) > 0