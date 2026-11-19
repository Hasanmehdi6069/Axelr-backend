"""Apex Router — telemetry aggregator.

Append-only JSONL at /telemetry/routes.jsonl. Non-blocking async write queue
so the router never stalls on disk I/O. Aggregation computes tier breakdown,
total cost, p50/p95 latency, escalation rate, and the recent N entries the
dashboard renders.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_DEFAULT_LOG = Path.cwd() / "telemetry" / "routes.jsonl"

_write_queue: Optional[asyncio.Queue[dict]] = None
_writer_task: Optional[asyncio.Task] = None


def log_path() -> Path:
    return Path(os.environ.get("APEX_TELEMETRY", str(_DEFAULT_LOG)))


async def log_route(entry: dict) -> None:
    """
    Queue a routing entry for append. Adds timestamp if missing.
    Drops on queue full (bounded backpressure) rather than blocking the router.
    """
    global _write_queue, _writer_task
    if _write_queue is None:
        _write_queue = asyncio.Queue(maxsize=4096)
        _writer_task = asyncio.create_task(_writer_loop())
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **entry,
    }
    try:
        _write_queue.put_nowait(record)
    except asyncio.QueueFull:
        pass


async def _writer_loop() -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        while True:
            try:
                record = await _write_queue.get()
            except asyncio.CancelledError:
                fh.flush()
                return
            try:
                fh.write(json.dumps(record, separators=(",", ":")) + "\n")
                fh.flush()
            except Exception:
                pass


async def flush() -> None:
    """Drain pending writes (used by tests and shutdown)."""
    if _write_queue is not None:
        await _write_queue.join()


async def stop() -> None:
    global _writer_task, _write_queue
    if _writer_task is not None:
        _writer_task.cancel()
        try:
            await _writer_task
        except asyncio.CancelledError:
            pass
    _writer_task = None
    _write_queue = None


def read_all_sync() -> list[dict]:
    path = log_path()
    if not path.exists():
        return []
    entries: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


async def read_all() -> list[dict]:
    return await asyncio.to_thread(read_all_sync)


# ─── Aggregate ────────────────────────────────────────────────────────────
@dataclass(slots=True)
class Aggregate:
    total: int
    byTier: dict[str, int]
    byTaskType: dict[str, int]
    totalCostUsd: float
    p50LatencyMs: int
    p95LatencyMs: int
    escalationRate: float
    simulatedRate: float
    recent: list[dict]

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "byTier": self.byTier,
            "byTaskType": self.byTaskType,
            "totalCostUsd": round(self.totalCostUsd, 6),
            "p50LatencyMs": self.p50LatencyMs,
            "p95LatencyMs": self.p95LatencyMs,
            "escalationRate": round(self.escalationRate, 3),
            "simulatedRate": round(self.simulatedRate, 3),
            "recent": self.recent,
        }


async def aggregate(recent_n: int = 25) -> Aggregate:
    entries = await read_all()

    by_tier: dict[str, int] = {}
    by_task: dict[str, int] = {}
    total_cost = 0.0
    escalations = 0
    simulated = 0
    latencies: list[int] = []

    for e in entries:
        tier = e.get("tier", "unknown")
        task = e.get("taskType", "general")
        by_tier[tier] = by_tier.get(tier, 0) + 1
        by_task[task] = by_task.get(task, 0) + 1
        total_cost += float(e.get("costUsd", 0) or 0)
        latencies.append(int(e.get("latencyMs", 0) or 0))
        if e.get("escalated"):
            escalations += 1
        if e.get("simulated"):
            simulated += 1

    latencies.sort()
    n = len(latencies)

    return Aggregate(
        total=n,
        byTier=by_tier,
        byTaskType=by_task,
        totalCostUsd=total_cost,
        p50LatencyMs=_percentile(latencies, 0.50),
        p95LatencyMs=_percentile(latencies, 0.95),
        escalationRate=(escalations / n) if n else 0.0,
        simulatedRate=(simulated / n) if n else 0.0,
        recent=list(reversed(entries[-recent_n:])),
    )


def _percentile(sorted_values: list[int], p: float) -> int:
    if not sorted_values:
        return 0
    idx = min(len(sorted_values) - 1, int(len(sorted_values) * p))
    return sorted_values[idx]


def reset() -> None:
    """Truncate the log — used by bench and demo reset."""
    path = log_path()
    if path.exists():
        path.write_text("", encoding="utf-8")
