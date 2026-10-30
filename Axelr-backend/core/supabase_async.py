<<<<<<< HEAD
# core/supabase_async.py
"""
AXELR — Async Supabase Transaction Tracker (fire-and-forget)
=============================================================
Preserves every Supabase write path. The driver is loaded lazily inside
each task and released when the task completes, so the API container's
idle RSS does not hold a Supabase socket.

Pattern:
    from core.supabase_async import track
    track("extraction_events", {"user_id": uid, "status": "ok"})
    # → returns immediately; write happens on the event loop

Contract:
    * track() never raises.
    * Tracked tasks are bounded (max 32 concurrent).
    * Every task has a 5 s internal deadline.
    * On shutdown, `drain()` waits ≤2 s for in-flight writes.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any

logger = logging.getLogger("axelr.supabase_async")

MAX_INFLIGHT = 32
TASK_TIMEOUT_S = 5.0
_INFLIGHT: set[asyncio.Task] = set()
_LOCK = asyncio.Lock()


async def _write(
    table: str, payload: dict[str, Any], conflict_key: str | None,
) -> None:
    """One lazy-loaded write. Isolated from the caller's request lifecycle."""
    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    key = (os.getenv("SUPABASE_SERVICE_KEY") or "").strip()
    if not url or not key:
        return
    try:
        from supabase import create_async_client  # type: ignore
        client = await create_async_client(url, key)
        try:
            q = client.table(table)
            if conflict_key:
                await q.upsert(payload, on_conflict=conflict_key).execute()
            else:
                await q.insert(payload).execute()
        finally:
            with contextlib.suppress(Exception):
                await client.aclose()
    except Exception as e:
        logger.debug("supabase_track_failed table=%s error=%s", table, e)


def track(
    table: str, payload: dict[str, Any], *, conflict_key: str | None = None,
) -> asyncio.Task | None:
    """
    Fire-and-forget tracker. Returns the created task (or None if the
    loop is closed or the inflight cap is reached). Never raises.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    if len(_INFLIGHT) >= MAX_INFLIGHT:
        return None

    async def _guarded():
        try:
            await asyncio.wait_for(
                _write(table, payload, conflict_key), timeout=TASK_TIMEOUT_S,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception as e:
            logger.debug("supabase_task_failed error=%s", e)

    task = loop.create_task(_guarded(), name=f"supabase::{table}")
    _INFLIGHT.add(task)
    task.add_done_callback(_INFLIGHT.discard)
    return task


async def drain(timeout_s: float = 2.0) -> int:
    """Wait briefly for in-flight writes. Returns the count that drained."""
    if not _INFLIGHT:
        return 0
    pending = list(_INFLIGHT)
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True), timeout=timeout_s,
        )
    return len(pending)


def inflight() -> int:
    return len(_INFLIGHT)


=======
# core/supabase_async.py
"""
AXELR — Async Supabase Transaction Tracker (fire-and-forget)
=============================================================
Preserves every Supabase write path. The driver is loaded lazily inside
each task and released when the task completes, so the API container's
idle RSS does not hold a Supabase socket.

Pattern:
    from core.supabase_async import track
    track("extraction_events", {"user_id": uid, "status": "ok"})
    # → returns immediately; write happens on the event loop

Contract:
    * track() never raises.
    * Tracked tasks are bounded (max 32 concurrent).
    * Every task has a 5 s internal deadline.
    * On shutdown, `drain()` waits ≤2 s for in-flight writes.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any

logger = logging.getLogger("axelr.supabase_async")

MAX_INFLIGHT = 32
TASK_TIMEOUT_S = 5.0
_INFLIGHT: set[asyncio.Task] = set()
_LOCK = asyncio.Lock()


async def _write(
    table: str, payload: dict[str, Any], conflict_key: str | None,
) -> None:
    """One lazy-loaded write. Isolated from the caller's request lifecycle."""
    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    key = (os.getenv("SUPABASE_SERVICE_KEY") or "").strip()
    if not url or not key:
        return
    try:
        from supabase import create_async_client  # type: ignore
        client = await create_async_client(url, key)
        try:
            q = client.table(table)
            if conflict_key:
                await q.upsert(payload, on_conflict=conflict_key).execute()
            else:
                await q.insert(payload).execute()
        finally:
            with contextlib.suppress(Exception):
                await client.aclose()
    except Exception as e:
        logger.debug("supabase_track_failed table=%s error=%s", table, e)


def track(
    table: str, payload: dict[str, Any], *, conflict_key: str | None = None,
) -> asyncio.Task | None:
    """
    Fire-and-forget tracker. Returns the created task (or None if the
    loop is closed or the inflight cap is reached). Never raises.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    if len(_INFLIGHT) >= MAX_INFLIGHT:
        return None

    async def _guarded():
        try:
            await asyncio.wait_for(
                _write(table, payload, conflict_key), timeout=TASK_TIMEOUT_S,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception as e:
            logger.debug("supabase_task_failed error=%s", e)

    task = loop.create_task(_guarded(), name=f"supabase::{table}")
    _INFLIGHT.add(task)
    task.add_done_callback(_INFLIGHT.discard)
    return task


async def drain(timeout_s: float = 2.0) -> int:
    """Wait briefly for in-flight writes. Returns the count that drained."""
    if not _INFLIGHT:
        return 0
    pending = list(_INFLIGHT)
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True), timeout=timeout_s,
        )
    return len(pending)


def inflight() -> int:
    return len(_INFLIGHT)


>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
__all__ = ["track", "drain", "inflight"]