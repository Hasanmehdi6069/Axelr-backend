<<<<<<< HEAD
# core/thermal_guard.py
"""
AXELR — Thermal Guard

Prompt 1 §2: "Fused kernels must never trigger high constant CPU
utilization on 1-2 core consumer devices. Avoid thermal throttling,
memory leakage, and battery drain."

Background loop that reads live CPU utilisation and thread count,
and adjusts a global "concurrency budget" that downstream modules
(twin_engine parallel slot, provider race width) consult before
scheduling work.

Public:
    start() / stop() / running()
    budget() -> int          # currently allowed parallel slots
    pressure() -> str        # "ok" | "warm" | "hot" | "critical"
    snapshot() -> dict

Never raises.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time
from collections import deque
from typing import Any

logger = logging.getLogger("axelr.thermal_guard")

try:
    import psutil  # type: ignore
    _HAS_PSUTIL = True
except ImportError:
    psutil = None  # type: ignore
    _HAS_PSUTIL = False

_SAMPLE_INTERVAL_S = float(os.getenv("AXELR_THERMAL_INTERVAL_S", "5.0"))
_WARM_PCT = float(os.getenv("AXELR_THERMAL_WARM_PCT", "70"))
_HOT_PCT = float(os.getenv("AXELR_THERMAL_HOT_PCT", "85"))
_CRITICAL_PCT = float(os.getenv("AXELR_THERMAL_CRITICAL_PCT", "95"))
_RING = int(os.getenv("AXELR_THERMAL_RING", "6"))


from dataclasses import dataclass


@dataclass(slots=True)
class _Snapshot:
    ts: float
    cpu_pct: float
    mem_pct: float
    load1: float
    threads: int
    pressure: str
    budget: int


class _ThermalGuard:
    __slots__ = ("_cpu_ring", "_task", "_lock", "_running", "_snap")

    def __init__(self) -> None:
        self._cpu_ring: deque[float] = deque(maxlen=max(2, _RING))
        self._task: asyncio.Task | None = None
        self._lock = threading.Lock()
        self._running = False
        self._snap = _Snapshot(
            ts=time.time(), cpu_pct=0.0, mem_pct=0.0, load1=0.0,
            threads=threading.active_count(), pressure="ok", budget=4,
        )

    def _read_cpu(self) -> float:
        if not _HAS_PSUTIL:
            return 0.0
        try:
            return float(psutil.cpu_percent(interval=None))
        except Exception:
            return 0.0

    def _read_mem(self) -> float:
        if not _HAS_PSUTIL:
            return 0.0
        try:
            return float(psutil.virtual_memory().percent)
        except Exception:
            return 0.0

    def _read_load(self) -> float:
        try:
            return float(os.getloadavg()[0])
        except (OSError, AttributeError):
            return 0.0

    def _compute(self) -> _Snapshot:
        cpu = self._read_cpu()
        self._cpu_ring.append(cpu)
        # Use the worst of the ring to avoid single-spike decisions.
        worst = max(self._cpu_ring) if self._cpu_ring else cpu
        mem_pct = self._read_mem()
        load1 = self._read_load()
        n_threads = threading.active_count()

        cores = os.cpu_count() or 2
        if worst >= _CRITICAL_PCT or load1 >= cores * 2.5:
            pressure = "critical"
            budget = 1
        elif worst >= _HOT_PCT or load1 >= cores * 1.5:
            pressure = "hot"
            budget = max(1, min(2, cores - 1))
        elif worst >= _WARM_PCT or load1 >= cores * 0.9:
            pressure = "warm"
            budget = max(2, min(3, cores))
        else:
            pressure = "ok"
            budget = max(2, cores)

        return _Snapshot(
            ts=time.time(), cpu_pct=round(worst, 1),
            mem_pct=round(mem_pct, 1), load1=round(load1, 2),
            threads=n_threads, pressure=pressure, budget=budget,
        )

    async def _loop(self) -> None:
        # Warm up cpu_percent measurement.
        if _HAS_PSUTIL:
            with contextlib.suppress(Exception):
                psutil.cpu_percent(interval=None)
        while True:
            try:
                await asyncio.sleep(_SAMPLE_INTERVAL_S)
                snap = self._compute()
                with self._lock:
                    prev = self._snap.pressure
                    self._snap = snap
                if snap.pressure != prev:
                    logger.info(
                        "thermal_pressure_change %s->%s cpu_pct=%.1f "
                        "load1=%.2f threads=%d budget=%d",
                        prev, snap.pressure, snap.cpu_pct,
                        snap.load1, snap.threads, snap.budget,
                    )
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug("thermal_guard_error error=%s", e)

    def start(self) -> bool:
        if self._running:
            return True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._task = loop.create_task(self._loop(),
                                      name="thermal_guard")
        self._running = True
        logger.info("thermal_guard_started interval_s=%.1f",
                    _SAMPLE_INTERVAL_S)
        return True

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        self._running = False
        logger.info("thermal_guard_stopped")

    def running(self) -> bool:
        return self._running

    def budget(self) -> int:
        with self._lock:
            return self._snap.budget

    def pressure(self) -> str:
        with self._lock:
            return self._snap.pressure

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            s = self._snap
        return {
            "ts": s.ts,
            "cpu_pct": s.cpu_pct,
            "mem_pct": s.mem_pct,
            "load1": s.load1,
            "threads": s.threads,
            "pressure": s.pressure,
            "budget": s.budget,
            "running": self._running,
            "psutil": _HAS_PSUTIL,
        }


GUARD = _ThermalGuard()


def start() -> bool:
    return GUARD.start()


def stop() -> None:
    GUARD.stop()


def running() -> bool:
    return GUARD.running()


def budget() -> int:
    return GUARD.budget()


def pressure() -> str:
    return GUARD.pressure()


def snapshot() -> dict[str, Any]:
    return GUARD.snapshot()


def audit() -> dict[str, Any]:
    return snapshot()


__all__ = [
    "start", "stop", "running", "budget", "pressure", "snapshot", "audit",
=======
# core/thermal_guard.py
"""
AXELR — Thermal Guard

Prompt 1 §2: "Fused kernels must never trigger high constant CPU
utilization on 1-2 core consumer devices. Avoid thermal throttling,
memory leakage, and battery drain."

Background loop that reads live CPU utilisation and thread count,
and adjusts a global "concurrency budget" that downstream modules
(twin_engine parallel slot, provider race width) consult before
scheduling work.

Public:
    start() / stop() / running()
    budget() -> int          # currently allowed parallel slots
    pressure() -> str        # "ok" | "warm" | "hot" | "critical"
    snapshot() -> dict

Never raises.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time
from collections import deque
from typing import Any

logger = logging.getLogger("axelr.thermal_guard")

try:
    import psutil  # type: ignore
    _HAS_PSUTIL = True
except ImportError:
    psutil = None  # type: ignore
    _HAS_PSUTIL = False

_SAMPLE_INTERVAL_S = float(os.getenv("AXELR_THERMAL_INTERVAL_S", "5.0"))
_WARM_PCT = float(os.getenv("AXELR_THERMAL_WARM_PCT", "70"))
_HOT_PCT = float(os.getenv("AXELR_THERMAL_HOT_PCT", "85"))
_CRITICAL_PCT = float(os.getenv("AXELR_THERMAL_CRITICAL_PCT", "95"))
_RING = int(os.getenv("AXELR_THERMAL_RING", "6"))


from dataclasses import dataclass


@dataclass(slots=True)
class _Snapshot:
    ts: float
    cpu_pct: float
    mem_pct: float
    load1: float
    threads: int
    pressure: str
    budget: int


class _ThermalGuard:
    __slots__ = ("_cpu_ring", "_task", "_lock", "_running", "_snap")

    def __init__(self) -> None:
        self._cpu_ring: deque[float] = deque(maxlen=max(2, _RING))
        self._task: asyncio.Task | None = None
        self._lock = threading.Lock()
        self._running = False
        self._snap = _Snapshot(
            ts=time.time(), cpu_pct=0.0, mem_pct=0.0, load1=0.0,
            threads=threading.active_count(), pressure="ok", budget=4,
        )

    def _read_cpu(self) -> float:
        if not _HAS_PSUTIL:
            return 0.0
        try:
            return float(psutil.cpu_percent(interval=None))
        except Exception:
            return 0.0

    def _read_mem(self) -> float:
        if not _HAS_PSUTIL:
            return 0.0
        try:
            return float(psutil.virtual_memory().percent)
        except Exception:
            return 0.0

    def _read_load(self) -> float:
        try:
            return float(os.getloadavg()[0])
        except (OSError, AttributeError):
            return 0.0

    def _compute(self) -> _Snapshot:
        cpu = self._read_cpu()
        self._cpu_ring.append(cpu)
        # Use the worst of the ring to avoid single-spike decisions.
        worst = max(self._cpu_ring) if self._cpu_ring else cpu
        mem_pct = self._read_mem()
        load1 = self._read_load()
        n_threads = threading.active_count()

        cores = os.cpu_count() or 2
        if worst >= _CRITICAL_PCT or load1 >= cores * 2.5:
            pressure = "critical"
            budget = 1
        elif worst >= _HOT_PCT or load1 >= cores * 1.5:
            pressure = "hot"
            budget = max(1, min(2, cores - 1))
        elif worst >= _WARM_PCT or load1 >= cores * 0.9:
            pressure = "warm"
            budget = max(2, min(3, cores))
        else:
            pressure = "ok"
            budget = max(2, cores)

        return _Snapshot(
            ts=time.time(), cpu_pct=round(worst, 1),
            mem_pct=round(mem_pct, 1), load1=round(load1, 2),
            threads=n_threads, pressure=pressure, budget=budget,
        )

    async def _loop(self) -> None:
        # Warm up cpu_percent measurement.
        if _HAS_PSUTIL:
            with contextlib.suppress(Exception):
                psutil.cpu_percent(interval=None)
        while True:
            try:
                await asyncio.sleep(_SAMPLE_INTERVAL_S)
                snap = self._compute()
                with self._lock:
                    prev = self._snap.pressure
                    self._snap = snap
                if snap.pressure != prev:
                    logger.info(
                        "thermal_pressure_change %s->%s cpu_pct=%.1f "
                        "load1=%.2f threads=%d budget=%d",
                        prev, snap.pressure, snap.cpu_pct,
                        snap.load1, snap.threads, snap.budget,
                    )
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug("thermal_guard_error error=%s", e)

    def start(self) -> bool:
        if self._running:
            return True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._task = loop.create_task(self._loop(),
                                      name="thermal_guard")
        self._running = True
        logger.info("thermal_guard_started interval_s=%.1f",
                    _SAMPLE_INTERVAL_S)
        return True

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        self._running = False
        logger.info("thermal_guard_stopped")

    def running(self) -> bool:
        return self._running

    def budget(self) -> int:
        with self._lock:
            return self._snap.budget

    def pressure(self) -> str:
        with self._lock:
            return self._snap.pressure

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            s = self._snap
        return {
            "ts": s.ts,
            "cpu_pct": s.cpu_pct,
            "mem_pct": s.mem_pct,
            "load1": s.load1,
            "threads": s.threads,
            "pressure": s.pressure,
            "budget": s.budget,
            "running": self._running,
            "psutil": _HAS_PSUTIL,
        }


GUARD = _ThermalGuard()


def start() -> bool:
    return GUARD.start()


def stop() -> None:
    GUARD.stop()


def running() -> bool:
    return GUARD.running()


def budget() -> int:
    return GUARD.budget()


def pressure() -> str:
    return GUARD.pressure()


def snapshot() -> dict[str, Any]:
    return GUARD.snapshot()


def audit() -> dict[str, Any]:
    return snapshot()


__all__ = [
    "start", "stop", "running", "budget", "pressure", "snapshot", "audit",
>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
]