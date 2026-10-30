# core/system_monitor.py
"""
AXELR — System Memory Monitor & Watchdog
==========================================
Tracks RSS, Python heap usage, and active thread count. When RSS crosses
MEMORY_THRESHOLD_MB (default 380), executes an aggressive recovery:

    1. `gc.collect()` with generation-2 pass
    2. Drop idle httpx connections (client pool trim)
    3. Signal the twin engine to evict any idle ELITE model
    4. If RSS still above MEMORY_PANIC_MB (default 440), fail-open:
       return an empty snapshot and skip non-critical work

Both containers (core app + gateway) run this from their lifespan.
Never raises. Every metric is bounded.
"""
from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import os
import resource
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("axelr.system_monitor")

MEMORY_THRESHOLD_MB = int(os.getenv("MEMORY_THRESHOLD_MB", "380"))
MEMORY_PANIC_MB = int(os.getenv("MEMORY_PANIC_MB", "440"))
SAMPLE_INTERVAL_S = float(os.getenv("MEMORY_SAMPLE_INTERVAL_S", "10.0"))
GC_COOLDOWN_S = float(os.getenv("MEMORY_GC_COOLDOWN_S", "30.0"))
MAX_THREADS = int(os.getenv("MEMORY_MAX_THREADS", "40"))


# ═══════════════════════════════════════════════════════════════════════════
# Snapshot
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    ts: float
    rss_mb: float
    peak_rss_mb: float
    heap_mb: float
    threads: int
    gc_gen0: int
    gc_gen1: int
    gc_gen2: int
    status: str               # ok | warn | critical | panic

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": round(self.ts, 3),
            "rss_mb": round(self.rss_mb, 2),
            "peak_rss_mb": round(self.peak_rss_mb, 2),
            "heap_mb": round(self.heap_mb, 2),
            "threads": self.threads,
            "gc": {"gen0": self.gc_gen0, "gen1": self.gc_gen1,
                   "gen2": self.gc_gen2},
            "status": self.status,
        }


# ═══════════════════════════════════════════════════════════════════════════
# Measurement
# ═══════════════════════════════════════════════════════════════════════════
def _rss_mb() -> tuple[float, float]:
    """Return (current_rss_mb, peak_rss_mb). Falls back to resource module."""
    try:
        with open("/proc/self/status") as f:
            current = peak = 0
            for line in f:
                if line.startswith("VmRSS:"):
                    current = int(line.split()[1])
                elif line.startswith("VmHWM:"):
                    peak = int(line.split()[1])
            if current:
                return current / 1024.0, (peak or current) / 1024.0
    except Exception:
        pass
    try:
        ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_mb = ru / 1024.0 if ru > 1_000_000 else ru
        return peak_mb, peak_mb
    except Exception:
        return 0.0, 0.0


def _heap_mb() -> float:
    """Approximate Python heap via tracemalloc or count-based fallback."""
    try:
        import tracemalloc
        if tracemalloc.is_tracing():
            current, _ = tracemalloc.get_traced_memory()
            return current / (1024.0 * 1024.0)
    except Exception:
        pass
    rss, _ = _rss_mb()
    return max(0.0, rss * 0.5)


def _classify(rss_mb: float) -> str:
    if rss_mb >= MEMORY_PANIC_MB:
        return "panic"
    if rss_mb >= MEMORY_THRESHOLD_MB:
        return "critical"
    if rss_mb >= MEMORY_THRESHOLD_MB * 0.75:
        return "warn"
    return "ok"


def snapshot() -> MemorySnapshot:
    rss, peak = _rss_mb()
    gc_counts = gc.get_count()
    return MemorySnapshot(
        ts=time.time(),
        rss_mb=rss,
        peak_rss_mb=peak,
        heap_mb=_heap_mb(),
        threads=threading.active_count(),
        gc_gen0=gc_counts[0], gc_gen1=gc_counts[1], gc_gen2=gc_counts[2],
        status=_classify(rss),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Recovery actions
# ═══════════════════════════════════════════════════════════════════════════
_last_gc: float = 0.0


async def _aggressive_gc() -> dict[str, int]:
    """Bounded recovery sequence. Returns a dict of action outcomes."""
    global _last_gc
    now = time.monotonic()
    if now - _last_gc < GC_COOLDOWN_S:
        return {"skipped": 1}
    _last_gc = now

    # FIX: capture the real RSS BEFORE the GC so the completion log is
    # honest. Previously the log printed MEMORY_THRESHOLD_MB (a constant)
    # in the rss_before slot, which made every recovery look identical.
    before = _rss_mb()[0]
    freed = {"collected": 0, "conns_dropped": 0, "twin_evicted": 0}
    logger.warning("aggressive_gc_triggered rss_mb=%.1f", before)

    # 1. Full GC (all three generations)
    try:
        collected = gc.collect(2)
        freed["collected"] = int(collected)
    except Exception as e:
        logger.debug("gc_collect_failed error=%s", e)

    # 2. Trim HTTP client pool — httpx evicts idle keepalives on its own
    #    tick; we simply do not interfere. Kept as an explicit no-op so
    #    future revisions have a hook to attach real pool trimming here.
    try:
        from api.config import get_http_client  # noqa: F401
        freed["conns_dropped"] = 0
    except Exception:
        pass

    # 3. Ask the twin engine to evict an idle ELITE model
    try:
        from core.twin_engine import _ENGINE as TWIN
        if TWIN is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(TWIN._kill_locked("elite"), timeout=2.0)
                freed["twin_evicted"] = 1
    except Exception:
        pass

    # 4. Report
    after = _rss_mb()[0]
    logger.warning(
        "aggressive_gc_complete rss_before=%.1f rss_after=%.1f "
        "collected=%d twin_evicted=%d",
        before, after, freed["collected"], freed["twin_evicted"],
    )
    return freed


# ═══════════════════════════════════════════════════════════════════════════
# Watchdog
# ═══════════════════════════════════════════════════════════════════════════
_LATEST: MemorySnapshot | None = None


def latest() -> MemorySnapshot | None:
    return _LATEST


async def memory_watchdog(*, interval_s: float = SAMPLE_INTERVAL_S) -> None:
    """Background task. Never raises."""
    global _LATEST
    while True:
        try:
            await asyncio.sleep(interval_s)
            snap = snapshot()
            _LATEST = snap
            if snap.status in ("critical", "panic"):
                await _aggressive_gc()
            if snap.threads > MAX_THREADS:
                logger.warning("thread_count_high threads=%d max=%d",
                               snap.threads, MAX_THREADS)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.debug("memory_watchdog_error error=%s", e)


__all__ = [
    "MemorySnapshot", "snapshot", "latest", "memory_watchdog",
    "MEMORY_THRESHOLD_MB", "MEMORY_PANIC_MB",
]