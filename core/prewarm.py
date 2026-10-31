# core/prewarm.py
"""
AXELR — Pre-Warm Preemption
============================
Task-specific warm-up for the twin engine.

The twin engine runs a generic "hi" warm-up after model load. That
primes the KV cache for the base prompt but not for the specific task's
system prefix or grammar constraint. When the first real user request
arrives, the server still has to prefetch that prefix.

This module composes a synthetic prompt that matches the task's system
bytes and grammar, fires it against the twin engine, and discards the
output. The downstream llama-server KV cache then already holds the
common prefix, so the first real request pays only the delta.

Cost control:
    * Rate-limited per (workspace, task_type) — default once per 90 s.
    * Bounded queue — max 4 concurrent prewarms.
    * Cancelled if a real request for the same key arrives.
    * Zero-cost if the twin engine is unavailable.
    * Never raises.

Public:
    signal(workspace, task_type, prompt_hint="") -> bool
    cancel(workspace, task_type) -> bool
    snapshot() -> dict
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("axelr.prewarm")

_VERSION = "1.0"

_COOLDOWN_S = float(os.getenv("AXELR_PREWARM_COOLDOWN_S", "90.0"))
_MAX_CONCURRENT = int(os.getenv("AXELR_PREWARM_MAX_CONCURRENT", "4"))
_PREWARM_TIMEOUT_S = float(os.getenv("AXELR_PREWARM_TIMEOUT_S", "6.0"))
_ENABLED = os.getenv("AXELR_PREWARM", "true").lower() in ("1", "true", "yes")


_SYNTHETIC_PROMPTS: dict[str, str] = {
    "extraction": (
        "Extract the requested fields from the following record. "
        "Return a compact JSON array inside ```json fences.\n\n"
        "<record>\nfield1: value1\nfield2: value2\n</record>\n"
    ),
    "structuring": (
        "Return a single JSON object with the requested keys.\n\n"
        "<request>summarize the following in a structured form</request>\n"
    ),
    "frontend": (
        "Produce a single self-contained HTML document with inline CSS "
        "and JS. Modern 2025 styling.\n\n"
        "<request>build a small landing page</request>\n"
    ),
    "touch_fix": (
        "Fix the following code. Return the full corrected file inside "
        "a single fenced block.\n\n"
        "<code>def f(): pass</code>\n"
    ),
}


@dataclass(slots=True)
class _PrewarmEntry:
    workspace: str
    task_type: str
    started_at: float
    task: asyncio.Task | None = None
    result: str = ""


_state: dict[str, Any] = {
    "in_flight": {},
    "last_ts": {},
    "stats": {
        "signals": 0, "fires": 0, "coalesced": 0,
        "cancelled": 0, "skipped_cooldown": 0, "skipped_cap": 0,
        "skipped_disabled": 0, "skipped_unavailable": 0,
    },
}


def _key(workspace: str, task_type: str) -> tuple[str, str]:
    return (workspace or "core", task_type or "structuring")


def _synthetic(task_type: str) -> str:
    return _SYNTHETIC_PROMPTS.get(
        task_type, _SYNTHETIC_PROMPTS["structuring"])


async def _run_prewarm(workspace: str, task_type: str,
                       prompt_hint: str) -> None:
    key = _key(workspace, task_type)
    entry: _PrewarmEntry | None = _state["in_flight"].get(key)
    try:
        try:
            from .twin_engine import execute_local_inference
        except Exception as e:
            logger.debug("prewarm_twin_import_failed error=%s", e)
            _state["stats"]["skipped_unavailable"] += 1
            if entry:
                entry.result = "skipped:no_engine"
            return

        synthetic = _synthetic(task_type)
        if prompt_hint:
            synthetic = prompt_hint[:200] + "\n\n" + synthetic

        t0 = time.monotonic()
        try:
            await asyncio.wait_for(
                execute_local_inference(workspace, task_type, synthetic),
                timeout=_PREWARM_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            if entry:
                entry.result = "error:timeout"
            return
        except asyncio.CancelledError:
            if entry:
                entry.result = "cancelled"
            raise
        except Exception as e:
            if entry:
                entry.result = f"error:{type(e).__name__}"
            logger.debug("prewarm_inference_failed error=%s", e)
            return
        if entry:
            entry.result = "ok"
        _state["stats"]["fires"] += 1
        logger.debug(
            "prewarm_done workspace=%s task=%s elapsed_ms=%.0f",
            workspace, task_type, (time.monotonic() - t0) * 1000,
        )
    finally:
        _state["in_flight"].pop(key, None)


def signal(workspace: str, task_type: str, prompt_hint: str = "") -> bool:
    if not _ENABLED:
        _state["stats"]["skipped_disabled"] += 1
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False

    _state["stats"]["signals"] += 1
    key = _key(workspace, task_type)
    now = time.monotonic()

    if key in _state["in_flight"]:
        _state["stats"]["coalesced"] += 1
        return True

    last = _state["last_ts"].get(key, 0.0)
    if (now - last) < _COOLDOWN_S:
        _state["stats"]["skipped_cooldown"] += 1
        return False

    if len(_state["in_flight"]) >= _MAX_CONCURRENT:
        _state["stats"]["skipped_cap"] += 1
        return False

    _state["last_ts"][key] = now
    entry = _PrewarmEntry(
        workspace=workspace, task_type=task_type, started_at=now,
    )
    _state["in_flight"][key] = entry
    task = loop.create_task(
        _run_prewarm(workspace, task_type, prompt_hint),
        name=f"prewarm::{key[0]}::{key[1]}",
    )
    entry.task = task
    return True


def cancel(workspace: str, task_type: str) -> bool:
    key = _key(workspace, task_type)
    entry = _state["in_flight"].get(key)
    if entry is None or entry.task is None or entry.task.done():
        return False
    entry.task.cancel()
    _state["stats"]["cancelled"] += 1
    return True


def snapshot() -> dict[str, Any]:
    now = time.monotonic()
    in_flight = [
        {
            "workspace": e.workspace,
            "task_type": e.task_type,
            "age_ms": round((now - e.started_at) * 1000, 1),
        }
        for e in _state["in_flight"].values()
    ]
    cooldowns = {
        f"{k[0]}:{k[1]}": round(max(0.0, _COOLDOWN_S - (now - ts)), 1)
        for k, ts in _state["last_ts"].items()
    }
    return {
        "version": _VERSION,
        "enabled": _ENABLED,
        "in_flight": in_flight,
        "cooldowns_remaining_s": cooldowns,
        "stats": dict(_state["stats"]),
        "tunables": {
            "cooldown_s": _COOLDOWN_S,
            "max_concurrent": _MAX_CONCURRENT,
            "timeout_s": _PREWARM_TIMEOUT_S,
        },
    }


def audit() -> dict[str, Any]:
    return snapshot()


__all__ = ["signal", "cancel", "snapshot", "audit"]