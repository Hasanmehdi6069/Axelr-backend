# core/burst_scheduler.py
"""
AXELR — Ephemeral Burst Scheduler

Prompt 1 §1: "Limit all background model optimization, weight
compilation, or compression loops to short-duration blocks strictly
under a 10-to-20 minute execution horizon per unique session."

This module enforces that contract. Background maintenance tasks
register with the scheduler, receive a session token with a hard
deadline, and are cancelled when the deadline passes.

Tunables:
    AXELR_BURST_DEFAULT_MIN  = 10  (default window, minutes)
    AXELR_BURST_HARD_MAX_MIN = 20  (ceiling, minutes)
    AXELR_BURST_MAX_ACTIVE   = 4   (concurrent bursts)

Public:
    acquire(name, *, minutes=None, on_expire=None) -> BurstToken | None
    release(token)
    active() -> list[dict]
    stats() -> dict
    shutdown() -> None

Never raises.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("axelr.burst")

_DEFAULT_MIN = float(os.getenv("AXELR_BURST_DEFAULT_MIN", "10"))
_HARD_MAX_MIN = float(os.getenv("AXELR_BURST_HARD_MAX_MIN", "20"))
_MAX_ACTIVE = int(os.getenv("AXELR_BURST_MAX_ACTIVE", "4"))


@dataclass(slots=True)
class BurstToken:
    name: str
    started_at: float
    deadline_at: float
    token_id: str
    _cancelled: bool = False
    _task: asyncio.Task | None = None
    _on_expire: Callable[[str], None] | None = None

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline_at

    def remaining_s(self) -> float:
        return max(0.0, self.deadline_at - time.monotonic())

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "token_id": self.token_id,
            "age_s": round(time.monotonic() - self.started_at, 1),
            "remaining_s": round(self.remaining_s(), 1),
            "deadline_in_s": round(self.deadline_at - self.started_at, 1),
            "cancelled": self._cancelled,
        }


class _BurstScheduler:
    __slots__ = ("_lock", "_active", "_counter", "_stats")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, BurstToken] = {}
        self._counter = 0
        self._stats = {
            "acquired": 0, "released": 0, "expired": 0, "rejected_cap": 0,
        }

    def acquire(self, name: str, *, minutes: float | None = None,
                on_expire: Callable[[str], None] | None = None
                ) -> BurstToken | None:
        requested = minutes if minutes is not None else _DEFAULT_MIN
        requested = max(0.5, min(float(requested), _HARD_MAX_MIN))
        with self._lock:
            if len(self._active) >= _MAX_ACTIVE:
                self._stats["rejected_cap"] += 1
                logger.warning("burst_rejected name=%s cap=%d",
                               name, _MAX_ACTIVE)
                return None
            self._counter += 1
            now = time.monotonic()
            tok = BurstToken(
                name=name,
                started_at=now,
                deadline_at=now + requested * 60.0,
                token_id=f"{name}::{self._counter}",
                _on_expire=on_expire,
            )
            self._active[tok.token_id] = tok
            self._stats["acquired"] += 1

        try:
            loop = asyncio.get_running_loop()
            tok._task = loop.create_task(
                self._expiry_watch(tok), name=f"burst::{tok.token_id}",
            )
        except RuntimeError:
            pass
        logger.info("burst_acquired name=%s minutes=%.1f token=%s",
                    name, requested, tok.token_id)
        return tok

    async def _expiry_watch(self, tok: BurstToken) -> None:
        try:
            while True:
                remaining = tok.remaining_s()
                if remaining <= 0:
                    break
                if tok._cancelled:
                    return
                await asyncio.sleep(min(1.0, remaining))
            if tok._cancelled:
                return
            logger.warning("burst_expired name=%s token=%s",
                           tok.name, tok.token_id)
            with self._lock:
                self._stats["expired"] += 1
            if tok._on_expire is not None:
                with contextlib.suppress(Exception):
                    tok._on_expire(tok.token_id)
            self.release(tok)
        except asyncio.CancelledError:
            return

    def release(self, tok: BurstToken | None) -> None:
        if tok is None:
            return
        with self._lock:
            removed = self._active.pop(tok.token_id, None)
            if removed is not None:
                self._stats["released"] += 1
        if tok._task is not None and not tok._task.done():
            tok._task.cancel()
            tok._task = None

    def active(self) -> list[dict[str, Any]]:
        with self._lock:
            return [t.to_dict() for t in self._active.values()]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": len(self._active),
                "max_active": _MAX_ACTIVE,
                "default_min": _DEFAULT_MIN,
                "hard_max_min": _HARD_MAX_MIN,
                **dict(self._stats),
            }

    def shutdown(self) -> None:
        with self._lock:
            tokens = list(self._active.values())
            self._active.clear()
        for t in tokens:
            if t._task is not None and not t._task.done():
                t._task.cancel()


SCHEDULER = _BurstScheduler()


def acquire(name: str, *, minutes: float | None = None,
            on_expire: Callable[[str], None] | None = None
            ) -> BurstToken | None:
    return SCHEDULER.acquire(name, minutes=minutes, on_expire=on_expire)


def release(tok: BurstToken | None) -> None:
    SCHEDULER.release(tok)


def active() -> list[dict[str, Any]]:
    return SCHEDULER.active()


def stats() -> dict[str, Any]:
    return SCHEDULER.stats()


def shutdown() -> None:
    SCHEDULER.shutdown()


def audit() -> dict[str, Any]:
    return stats()


__all__ = [
    "BurstToken", "acquire", "release", "active", "stats", "shutdown",
    "audit",
]