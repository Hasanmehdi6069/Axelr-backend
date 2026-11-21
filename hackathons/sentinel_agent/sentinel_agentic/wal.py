"""WAL backends — memory (default) and SQLite (production).

The in-memory WAL is fast but loses rollback capability on process crash.
The SQLite WAL survives restarts, so a workflow failure after a deploy or
container restart can still be rolled back on the next boot.

Interface (both backends):
    await wal.record(path, prev_content, action, workflow_id)  -> None
    await wal.rollback_all(workflow_id=None)                    -> dict
    await wal.mark_committed(workflow_id)                       -> None
    await wal.stats()                                           -> dict

Backend selection:
    SENTINEL_WAL_BACKEND=memory (default)  — dict, zero I/O
    SENTINEL_WAL_BACKEND=sqlite           — persistent, WAL mode
    SENTINEL_WAL_PATH=/tmp/sentinel-wal.db (default when sqlite)
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Snapshot:
    path: str
    prev: str | None
    action: str
    workflow_id: str | None
    ts: float
    id: int = 0


class MemoryWAL:
    """In-process WAL. Fast, not durable. Same API as PersistentWAL."""

    name = "memory"

    def __init__(self) -> None:
        self._store: dict[str, Snapshot] = {}
        self._lock = threading.Lock()

    async def record(self, path: str, prev: str | None, *, action: str = "", workflow_id: str | None = None) -> None:
        def _do() -> None:
            with self._lock:
                self._store[path] = Snapshot(path, prev, action, workflow_id, time.time())

        await asyncio.to_thread(_do)

    async def rollback_all(self, workflow_id: str | None = None) -> dict[str, Any]:
        def _do() -> dict[str, Any]:
            with self._lock:
                items = [(p, s) for p, s in self._store.items() if workflow_id is None or s.workflow_id == workflow_id]
                restored = 0
                for _, snap in reversed(items):
                    try:
                        p = Path(snap.path)
                        if snap.prev is None:
                            p.unlink(missing_ok=True)
                        else:
                            p.write_text(snap.prev, encoding="utf-8")
                        restored += 1
                    except Exception:
                        pass
                for p, _ in items:
                    self._store.pop(p, None)
            return {"restored": restored, "backend": self.name}

        return await asyncio.to_thread(_do)

    async def mark_committed(self, workflow_id: str) -> None:
        def _do() -> None:
            with self._lock:
                for p in [p for p, s in self._store.items() if s.workflow_id == workflow_id]:
                    self._store.pop(p, None)

        await asyncio.to_thread(_do)

    async def stats(self) -> dict[str, Any]:
        def _do() -> dict[str, Any]:
            with self._lock:
                return {"backend": self.name, "entries": len(self._store)}

        return await asyncio.to_thread(_do)


class PersistentWAL:
    """SQLite-backed WAL, survives process restart."""

    name = "sqlite"

    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.getenv("SENTINEL_WAL_PATH", "/tmp/sentinel-wal.db")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init()

    def _init(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL,
                    prev_content TEXT,
                    action TEXT NOT NULL DEFAULT '',
                    workflow_id TEXT,
                    created_at REAL NOT NULL,
                    applied INTEGER NOT NULL DEFAULT 0
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_wf ON snapshots(workflow_id)")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    async def record(self, path: str, prev: str | None, *, action: str = "", workflow_id: str | None = None) -> None:
        def _do() -> None:
            with self._lock:
                with self._connect() as db:
                    db.execute(
                        "INSERT INTO snapshots(path,prev_content,action,workflow_id,created_at) VALUES(?,?,?,?,?)",
                        (path, prev, action, workflow_id, time.time()),
                    )

        await asyncio.to_thread(_do)

    async def rollback_all(self, workflow_id: str | None = None) -> dict[str, Any]:
        def _do() -> dict[str, Any]:
            with self._lock:
                with self._connect() as db:
                    if workflow_id:
                        rows = db.execute(
                            "SELECT * FROM snapshots WHERE workflow_id=? AND applied=0 ORDER BY id DESC",
                            (workflow_id,),
                        ).fetchall()
                    else:
                        rows = db.execute("SELECT * FROM snapshots WHERE applied=0 ORDER BY id DESC").fetchall()
                    restored = 0
                    for r in rows:
                        try:
                            p = Path(r["path"])
                            if r["prev_content"] is None:
                                p.unlink(missing_ok=True)
                            else:
                                p.write_text(r["prev_content"], encoding="utf-8")
                            db.execute("UPDATE snapshots SET applied=1 WHERE id=?", (r["id"],))
                            restored += 1
                        except Exception:
                            pass
                return {"restored": restored, "backend": self.name}

        return await asyncio.to_thread(_do)

    async def mark_committed(self, workflow_id: str) -> None:
        def _do() -> None:
            with self._lock:
                with self._connect() as db:
                    db.execute("UPDATE snapshots SET applied=1 WHERE workflow_id=? AND applied=0", (workflow_id,))

        await asyncio.to_thread(_do)

    async def stats(self) -> dict[str, Any]:
        def _do() -> dict[str, Any]:
            with self._lock:
                with self._connect() as db:
                    total = db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
                    pend = db.execute("SELECT COUNT(*) FROM snapshots WHERE applied=0").fetchone()[0]
                    size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
                    return {
                        "backend": self.name,
                        "total": total,
                        "pending": pend,
                        "file_bytes": size,
                        "path": self.path,
                    }

        return await asyncio.to_thread(_do)


def make_wal() -> MemoryWAL | PersistentWAL:
    backend = (os.getenv("SENTINEL_WAL_BACKEND") or "memory").lower()
    return PersistentWAL() if backend == "sqlite" else MemoryWAL()
