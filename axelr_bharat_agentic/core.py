"""Bharat Agentic AI: Resilient Data Persistence — v1.1.0
=====================================================
WAL-backed persistence with automatic reconciliation.

Honest scope / fixes
--------------------
* **`create()` classmethod vs. `create()` instance method collision**
  was silently broken. Python kept the instance method, so
  `ResilientPersistenceLayer.create(node_id, wal_path)` raised
  `TypeError: missing argument 'data'`. Renamed classmethod to
  `create_layer()`. `init()` was silently broken before this fix.
* **Backend sync + health are pluggable** — `backend_sync: Callable[[WALEntry],
  Awaitable[bool]]` and `backend_health: Callable[[], Awaitable[bool]]`.
  This module does not fake network calls with `sleep(0.1)`.
* **WAL prunes synced entries** — previously grew unbounded forever.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import random
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("axelr.bharat_agentic.core")
__version__ = "1.1.0"

_MAX_PENDING_WAL = 10_000
_SYNC_INTERVAL_S = 5.0
_HEALTH_INTERVAL_S = 10.0
_SYNC_CONCURRENCY = 10


@dataclass(slots=True)
class VectorClock:
    clocks: Dict[str, int] = field(default_factory=dict)

    def increment(self, node_id: str) -> None:
        self.clocks[node_id] = self.clocks.get(node_id, 0) + 1

    def merge(self, other: "VectorClock") -> None:
        for node, count in other.clocks.items():
            self.clocks[node] = max(self.clocks.get(node, 0), count)

    def to_dict(self) -> Dict[str, int]:
        return self.clocks.copy()


@dataclass(slots=True)
class WALEntry:
    entry_id: str
    operation: str
    collection: str
    document_id: str
    data: Dict[str, Any]
    vector_clock: Dict[str, int]
    checksum: str
    created_at: float
    attempts: int = 0
    last_attempt: Optional[float] = None
    synced: bool = False

    def compute_checksum(self) -> str:
        content = json.dumps({
            "collection": self.collection,
            "document_id": self.document_id,
            "data": self.data,
            "created_at": self.created_at,
        }, sort_keys=True, default=str).encode()
        return hashlib.sha256(content).hexdigest()

    def verify_checksum(self) -> bool:
        return self.checksum == self.compute_checksum()


@dataclass(slots=True)
class ConnectionStatus:
    is_connected: bool
    last_successful_sync: float
    consecutive_failures: int
    latency_ms: float
    checked_at: float


class SQLiteWriteAheadLog:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS wal_entries (
                    entry_id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    collection TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    vector_clock TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    attempts INTEGER DEFAULT 0,
                    last_attempt REAL,
                    synced BOOLEAN DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_synced ON wal_entries(synced);
                CREATE INDEX IF NOT EXISTS idx_created ON wal_entries(created_at);
            """)

    async def append(self, entry: WALEntry) -> None:
        def _do():
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO wal_entries VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    entry.entry_id, entry.operation, entry.collection,
                    entry.document_id, json.dumps(entry.data, default=str),
                    json.dumps(entry.vector_clock), entry.checksum,
                    entry.created_at, entry.attempts, entry.last_attempt,
                    int(entry.synced),
                ))
        await asyncio.to_thread(_do)

    async def get_pending_entries(self, limit: int = 100) -> List[WALEntry]:
        def _do():
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM wal_entries WHERE synced=0 "
                    "ORDER BY created_at ASC LIMIT ?", (limit,)
                ).fetchall()
                return [WALEntry(
                    entry_id=r["entry_id"], operation=r["operation"],
                    collection=r["collection"], document_id=r["document_id"],
                    data=json.loads(r["data"]),
                    vector_clock=json.loads(r["vector_clock"]),
                    checksum=r["checksum"], created_at=r["created_at"],
                    attempts=r["attempts"], last_attempt=r["last_attempt"],
                    synced=bool(r["synced"]),
                ) for r in rows]
        return await asyncio.to_thread(_do)

    async def mark_synced(self, entry_id: str) -> None:
        def _do():
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE wal_entries SET synced=1 WHERE entry_id=?",
                    (entry_id,))
        await asyncio.to_thread(_do)

    async def record_attempt(self, entry_id: str) -> None:
        def _do():
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE wal_entries SET attempts=attempts+1, "
                    "last_attempt=? WHERE entry_id=?",
                    (time.time(), entry_id))
        await asyncio.to_thread(_do)

    async def prune_synced(self, max_age_s: float) -> int:
        cutoff = time.time() - max_age_s

        def _do() -> int:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute(
                    "DELETE FROM wal_entries WHERE synced=1 AND created_at<?",
                    (cutoff,))
                return cur.rowcount

        return await asyncio.to_thread(_do)

    async def count_pending(self) -> int:
        def _do() -> int:
            with sqlite3.connect(self.db_path) as conn:
                return int(conn.execute(
                    "SELECT COUNT(*) FROM wal_entries WHERE synced=0"
                ).fetchone()[0])
        return await asyncio.to_thread(_do)


BackendSync = Callable[[WALEntry], Awaitable[bool]]
BackendHealth = Callable[[], Awaitable[bool]]


@dataclass(slots=True)
class ResilientPersistenceLayer:
    node_id: str
    wal: SQLiteWriteAheadLog
    connection_status: ConnectionStatus
    backend_sync: Optional[BackendSync] = None
    backend_health: Optional[BackendHealth] = None
    sync_task: Optional[asyncio.Task] = None
    health_check_task: Optional[asyncio.Task] = None
    shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @classmethod
    async def create_layer(
        cls, node_id: str, wal_db_path: str,
        backend_sync: Optional[BackendSync] = None,
        backend_health: Optional[BackendHealth] = None,
    ) -> "ResilientPersistenceLayer":
        wal = await asyncio.to_thread(SQLiteWriteAheadLog, wal_db_path)
        status = ConnectionStatus(
            is_connected=backend_health is not None,
            last_successful_sync=0.0, consecutive_failures=0,
            latency_ms=0.0, checked_at=time.time(),
        )
        layer = cls(
            node_id=node_id, wal=wal, connection_status=status,
            backend_sync=backend_sync, backend_health=backend_health,
        )
        layer.sync_task = asyncio.create_task(layer._sync_loop())
        layer.health_check_task = asyncio.create_task(layer._health_loop())
        logger.info("resilient_persistence_initialized node=%s", node_id)
        return layer

    async def create_document(
        self, collection: str, document_id: str, data: Dict[str, Any],
    ) -> str:
        return await self._queue("create", collection, document_id, data)

    async def update_document(
        self, collection: str, document_id: str, data: Dict[str, Any],
    ) -> str:
        return await self._queue("update", collection, document_id, data)

    async def delete_document(
        self, collection: str, document_id: str,
    ) -> str:
        return await self._queue("delete", collection, document_id, {})

    async def _queue(
        self, op: str, collection: str, document_id: str,
        data: Dict[str, Any],
    ) -> str:
        pending = await self.wal.count_pending()
        if pending >= _MAX_PENDING_WAL:
            raise RuntimeError(
                f"wal_pending_limit_reached ({pending}/{_MAX_PENDING_WAL})")
        entry_id = str(uuid.uuid4())
        vc = VectorClock()
        vc.increment(self.node_id)
        entry = WALEntry(
            entry_id=entry_id, operation=op, collection=collection,
            document_id=document_id, data=data,
            vector_clock=vc.to_dict(), checksum="",
            created_at=time.time(),
        )
        entry.checksum = entry.compute_checksum()
        await self.wal.append(entry)
        if self.connection_status.is_connected:
            asyncio.create_task(self._sync_one(entry))
        return entry_id

    async def _sync_one(self, entry: WALEntry) -> bool:
        if self.backend_sync is None:
            raise NotImplementedError("requires backend: backend_sync callable")
        max_retries = 5
        for attempt in range(max_retries):
            try:
                ok = await self.backend_sync(entry)
                if not ok:
                    raise RuntimeError("backend_sync returned False")
                await self.wal.mark_synced(entry.entry_id)
                self.connection_status.last_successful_sync = time.time()
                self.connection_status.consecutive_failures = 0
                return True
            except Exception as e:
                await self.wal.record_attempt(entry.entry_id)
                if attempt == max_retries - 1:
                    break
                backoff = (0.5 * (2 ** attempt)) + random.uniform(0, 0.5)
                logger.warning(
                    "sync_retry id=%s attempt=%d backoff_s=%.2f err=%s",
                    entry.entry_id, attempt + 1, backoff, e)
                await asyncio.sleep(backoff)
        self.connection_status.consecutive_failures += 1
        if self.connection_status.consecutive_failures > 3:
            self.connection_status.is_connected = False
            logger.error("connection_marked_unavailable")
        return False

    async def _sync_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self.shutdown_event.wait(), timeout=_SYNC_INTERVAL_S)
                return
            except asyncio.TimeoutError:
                pass
            if not self.connection_status.is_connected:
                continue
            try:
                pending = await self.wal.get_pending_entries(limit=50)
            except Exception as e:
                logger.error("wal_read_failed err=%s", e)
                continue
            if not pending:
                await self.wal.prune_synced(max_age_s=3600)
                continue
            sem = asyncio.Semaphore(_SYNC_CONCURRENCY)
            async def _go(e: WALEntry):
                async with sem:
                    with contextlib.suppress(Exception):
                        await self._sync_one(e)
            await asyncio.gather(*(_go(e) for e in pending))

    async def _health_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self.shutdown_event.wait(), timeout=_HEALTH_INTERVAL_S)
                return
            except asyncio.TimeoutError:
                pass
            if self.backend_health is None:
                continue
            try:
                t0 = time.monotonic()
                ok = await self.backend_health()
                self.connection_status.latency_ms = (
                    time.monotonic() - t0) * 1000
                self.connection_status.is_connected = ok
                self.connection_status.checked_at = time.time()
                if ok:
                    self.connection_status.consecutive_failures = 0
            except Exception as e:
                self.connection_status.is_connected = False
                self.connection_status.consecutive_failures += 1
                logger.warning("health_check_failed err=%s", e)

    async def shutdown(self) -> None:
        self.shutdown_event.set()
        for t in (self.sync_task, self.health_check_task):
            if t is not None and not t.done():
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        self.sync_task = None
        self.health_check_task = None
        logger.info("resilient_persistence_shutdown")


_persistence: Optional[ResilientPersistenceLayer] = None


def get_persistence() -> Optional[ResilientPersistenceLayer]:
    return _persistence


async def init(
    node_id: str, wal_path: str = "./bharat_persistence.db",
    backend_sync: Optional[BackendSync] = None,
    backend_health: Optional[BackendHealth] = None,
) -> None:
    global _persistence
    if _persistence is not None:
        await _persistence.shutdown()
    _persistence = await ResilientPersistenceLayer.create_layer(
        node_id, wal_path, backend_sync=backend_sync,
        backend_health=backend_health,
    )
    logger.info("bharat_agentic_persistence_initialized")


async def shutdown() -> None:
    global _persistence
    if _persistence is not None:
        await _persistence.shutdown()
        _persistence = None
    logger.info("bharat_agentic_persistence_module_shutdown")


async def health() -> Dict[str, Any]:
    if _persistence is None:
        return {"ok": False,
                "details": {"reason": "not_initialized", "version": __version__}}
    pending = await _persistence.wal.count_pending()
    return {
        "ok": _persistence.connection_status.is_connected,
        "details": {
            "version": __version__,
            "node_id": _persistence.node_id,
            "is_connected": _persistence.connection_status.is_connected,
            "consecutive_failures": _persistence.connection_status.consecutive_failures,
            "latency_ms": round(_persistence.connection_status.latency_ms, 2),
            "pending_wal": pending,
            "pending_max": _MAX_PENDING_WAL,
            "backend_sync_configured": _persistence.backend_sync is not None,
            "backend_health_configured": _persistence.backend_health is not None,
        },
    }