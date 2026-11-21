# core/orchestration.py
"""
AXELR — Orchestration Singletons + Task Scheduler
====================================================
Thin module. Every other symbol previously in this file now lives in its
canonical home; this file keeps only:

    §1 TaskScheduler · Priority · Task  (scheduler for pipeline work)
    §2 Lazy singletons                  (get_pipeline / run_pipeline / ...)
    §3 Re-exports from canonical homes  (for backward-compat call sites)
    §4 Audit

Canonical homes:
    pipeline.py   — ResilientPipeline, ProviderCascade, tier constants
    resilience.py — Tier, TIERS, ladder_run, HotSwapDetector
    adaptive.py   — hedge_wrap, canonical_system, adapt_max_tokens
    guards.py     — thermal budget / pressure
    utils/misc_utils.py — signal_warmup, pacemaker_loop, prewarm
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable

from .adaptive import (
    adapt_max_tokens,
    canonical_system,
    hedge_wrap,
    record_token_ratio,
)
from .pipeline import (
    AXELR_COGNITION_LEVEL,
    FAST_TIER_DEADLINE_S,
    MAX_ROUNDS,
    PIPELINE_DEADLINE_S,
    PRO_TIER_DEADLINE_S,
    RACE_WIDTH,
    ProviderCascade,
    ResilientPipeline,
)
from .resilience import (
    TIERS,
    HotSwapDetector,
    HotSwapResult,
    ProviderFunc,
    Tier,
    get_hot_swap_detector,
    ladder_run,
)

logger = logging.getLogger("axelr.orchestration")


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Task scheduler
# ═══════════════════════════════════════════════════════════════════════════
class Priority:
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


@dataclass(slots=True)
class Task:
    id: str
    coro: Awaitable[Any]
    priority: int
    created_at: float = field(default_factory=time.time)
    executed_at: float | None = None
    completed_at: float | None = None
    success: bool | None = None
    error: str | None = None


class TaskScheduler:
    __slots__ = ("_queue", "_running", "_max_concurrency", "_worker", "_closed")

    def __init__(self, max_concurrency: int = 10) -> None:
        self._max_concurrency = max(1, int(max_concurrency))
        self._queue: list[Task] = []
        self._running: set[asyncio.Task[None]] = set()
        self._closed = False
        self._worker = asyncio.create_task(self._worker_loop(), name="task_scheduler_worker")

    async def _worker_loop(self) -> None:
        while not self._closed or self._queue:
            for done in {t for t in self._running if t.done()}:
                self._running.discard(done)
                with contextlib.suppress(Exception):
                    await done
            while len(self._running) < self._max_concurrency and self._queue:
                task = self._queue.pop(0)
                at = asyncio.create_task(self._execute_task(task), name=f"task_{task.id}")
                self._running.add(at)
            await asyncio.sleep(0.01)

    async def _execute_task(self, task: Task) -> None:
        task.executed_at = time.time()
        try:
            await task.coro
            task.success = True
        except Exception as e:
            task.success = False
            task.error = str(e)[:200]
            logger.error("task_failure id=%s error=%s", task.id, task.error)
        finally:
            task.completed_at = time.time()

    def schedule(self, coro: Awaitable[Any], priority: int = Priority.NORMAL) -> str:
        task_id = uuid.uuid4().hex[:8]
        task = Task(id=task_id, coro=coro, priority=int(priority))
        for i, existing in enumerate(self._queue):
            if existing.priority < task.priority:
                self._queue.insert(i, task)
                break
        else:
            self._queue.append(task)
        return task_id

    async def close(self, *, wait: bool = True) -> None:
        self._closed = True
        if wait and not self._worker.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._worker
        for t in self._running:
            if not t.done():
                t.cancel()
        self._running.clear()


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Lazy singletons
# ═══════════════════════════════════════════════════════════════════════════
_PIPELINE_LOCK = threading.Lock()
_PIPELINE: ResilientPipeline | None = None

_SCHEDULER_LOCK = threading.Lock()
_SCHEDULER: TaskScheduler | None = None


def get_pipeline() -> ResilientPipeline:
    global _PIPELINE
    if _PIPELINE is not None:
        return _PIPELINE
    with _PIPELINE_LOCK:
        if _PIPELINE is None:
            _PIPELINE = ResilientPipeline()
        return _PIPELINE


async def run_pipeline(**kwargs: Any) -> dict[str, Any]:
    return await get_pipeline().run(**kwargs)


async def stream_pipeline(**kwargs: Any):
    async for item in get_pipeline().stream(**kwargs):
        yield item


def get_task_scheduler() -> TaskScheduler:
    global _SCHEDULER
    if _SCHEDULER is not None:
        return _SCHEDULER
    with _SCHEDULER_LOCK:
        if _SCHEDULER is None:
            _SCHEDULER = TaskScheduler()
        return _SCHEDULER


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Re-exports from misc_utils (warmup + prewarm)
# ═══════════════════════════════════════════════════════════════════════════
try:
    from .utils.misc_utils import (
        cancel as cancel_prewarm,
    )
    from .utils.misc_utils import (
        pacemaker_loop,
        prewarm_snapshot,
        signal_warmup,
    )
    from .utils.misc_utils import (
        signal as signal_prewarm,
    )
except ImportError:

    async def signal_warmup(redis_client):
        return False

    async def pacemaker_loop(state, *, interval_s=0.5):
        return

    def signal_prewarm(*a, **kw):
        return False

    def cancel_prewarm(*a, **kw):
        return False

    def prewarm_snapshot():
        return {}


# ═══════════════════════════════════════════════════════════════════════════
# §4 — Audit
# ═══════════════════════════════════════════════════════════════════════════
def pipeline_stats() -> dict[str, int]:
    return dict(get_pipeline().stats)


def audit() -> dict[str, Any]:
    return {
        "pipeline_stats": pipeline_stats(),
        "tiers": list(TIERS.keys()),
        "cognition_level": AXELR_COGNITION_LEVEL,
        "max_rounds": MAX_ROUNDS,
        "race_width": RACE_WIDTH,
        "deadline_s": PIPELINE_DEADLINE_S,
        "fast_deadline_s": FAST_TIER_DEADLINE_S,
        "pro_deadline_s": PRO_TIER_DEADLINE_S,
        "scheduler_active": _SCHEDULER is not None,
    }


__all__ = [
    "TaskScheduler",
    "Priority",
    "Task",
    "get_task_scheduler",
    "get_pipeline",
    "run_pipeline",
    "stream_pipeline",
    "get_hot_swap_detector",
    "ResilientPipeline",
    "ProviderCascade",
    "AXELR_COGNITION_LEVEL",
    "MAX_ROUNDS",
    "RACE_WIDTH",
    "PIPELINE_DEADLINE_S",
    "FAST_TIER_DEADLINE_S",
    "PRO_TIER_DEADLINE_S",
    "Tier",
    "TIERS",
    "ladder_run",
    "HotSwapResult",
    "HotSwapDetector",
    "ProviderFunc",
    "hedge_wrap",
    "canonical_system",
    "adapt_max_tokens",
    "record_token_ratio",
    "signal_warmup",
    "pacemaker_loop",
    "signal_prewarm",
    "cancel_prewarm",
    "prewarm_snapshot",
    "pipeline_stats",
    "audit",
]
