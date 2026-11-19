# axelr_resource.py
"""
axelr_resource.py — Unified resource management: memory pressure monitoring +
graceful feature degradation ladder + resource health tracking.

Merged from axelr_pressure.py and axelr_degradation.py per E10 requirement.
When memory pressure is HIGH, shed features from the top of the ladder
(cheapest to lose first). When pressure recovers for `sustain_s`, reinstate
from bottom (most valuable first). Each rung has a shed() and reinstate().

Memory: ~0.
"""
from __future__ import annotations

import threading
import time
import tracemalloc
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Protocol

from axelr_utils import get_logger

logger = get_logger(__name__)

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    psutil = None
    _HAS_PSUTIL = False

logger = get_logger(__name__)


class Rung(Protocol):
    name: str
    def shed(self) -> None: ...
    def reinstate(self) -> None: ...


@dataclass
class _Rung:
    name: str
    shed_fn: Callable[[], None]
    reinstate_fn: Callable[[], None]
    active: bool = True

    def shed(self) -> None:
        if self.active:
            try:
                self.shed_fn()
                self.active = False
                logger.info("degradation_shed rung=%s", self.name)
            except Exception:
                logger.exception("degradation_shed_failed rung=%s", self.name)

    def reinstate(self) -> None:
        if not self.active:
            try:
                self.reinstate_fn()
                self.active = True
                logger.info("degradation_reinstate rung=%s", self.name)
            except Exception:
                logger.exception("degradation_reinstate_failed rung=%s", self.name)


class GracefulFeatureLadder:
    """
    Ordered feature degradation. Default rung order (index 0 = shed FIRST):

        0. streaming_rerank      (cheapest to lose, biggest CPU saver)
        1. structured_repair     (nice-to-have text polish)
        2. phonetic_ensemble     (drop ensemble → soundex-only)
        3. cloud_fallback        (close aiohttp session)
        4. warmup                (skip warmup on next restart)

    Rung 3 (cloud) sheds last because losing cloud fallback is a real
    quality regression; it's better to lose reranking.
    """
    __slots__ = ("_rungs", "_shed_count")

    DEFAULT_ORDER = [
        "streaming_rerank",
        "structured_repair",
        "phonetic_ensemble",
        "cloud_fallback",
        "warmup",
    ]

    def __init__(self) -> None:
        self._rungs: List[_Rung] = []
        self._shed_count = 0

    def add(self, name: str,
            shed_fn: Callable[[], None],
            reinstate_fn: Callable[[], None]) -> None:
        self._rungs.append(_Rung(name, shed_fn, reinstate_fn))

    def shed_one(self) -> bool:
        """Shed the first active rung. Returns True if something was shed."""
        for rung in self._rungs:
            if rung.active:
                rung.shed()
                self._shed_count += 1
                return True
        return False

    def reinstate_one(self) -> bool:
        """Reinstate the last shed rung. Returns True if something was restored."""
        for rung in reversed(self._rungs):
            if not rung.active:
                rung.reinstate()
                self._shed_count = max(0, self._shed_count - 1)
                return True
        return False

    def on_high(self, ratio: float) -> None:
        """Called by MemoryPressureMonitor. Sheds up to 2 rungs per event."""
        for _ in range(2):
            if not self.shed_one():
                break

    def on_low(self, ratio: float) -> None:
        """Called by MemoryPressureMonitor. Reinstates 1 rung per event."""
        self.reinstate_one()

    def state(self) -> dict:
        return {
            "shed_count": self._shed_count,
            "rungs": [{"name": r.name, "active": r.active} for r in self._rungs],
        }


@dataclass(frozen=True, slots=True)
class PressureConfig:
    budget_mb: float
    high_watermark: float = 0.85
    low_watermark: float = 0.65
    sustain_s: float = 30.0
    sample_ms: int = 500


class MemoryPressureMonitor:
    """
    Polls RSS + Python heap every `sample_ms`. Emits callbacks on
    crossing thresholds (transition only, not on every poll).
    """
    __slots__ = ("config", "_proc", "_running", "_thread", "_lock",
                 "_state", "_low_since", "_on_high", "_on_low", "_last_ratio")

    def __init__(self, config: PressureConfig,
                 on_high: Optional[Callable[[float], None]] = None,
                 on_low: Optional[Callable[[float], None]] = None) -> None:
        self.config = config
        self._proc = psutil.Process() if _HAS_PSUTIL else None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._state = "normal"     # normal | high
        self._low_since = 0.0
        self._on_high = on_high
        self._on_low = on_low
        self._last_ratio = 0.0
        if not tracemalloc.is_tracing():
            tracemalloc.start()

    def _sample_ratio(self) -> float:
        rss_bytes = 0
        if self._proc is not None:
            try:
                rss_bytes = self._proc.memory_info().rss
            except Exception:
                rss_bytes = 0
        py_bytes, _ = tracemalloc.get_traced_memory()
        total = max(rss_bytes, py_bytes)
        return total / max(1.0, self.config.budget_mb * 1024 * 1024)

    def sample_once(self) -> float:
        ratio = self._sample_ratio()
        self._last_ratio = ratio
        now = time.monotonic()
        with self._lock:
            if self._state == "normal" and ratio >= self.config.high_watermark:
                self._state = "high"
                self._low_since = 0.0
                if self._on_high:
                    try: self._on_high(ratio)
                    except Exception: logger.exception("pressure_on_high")
                logger.warning("memory_pressure_high ratio=%.2f budget=%.0fmb",
                               ratio, self.config.budget_mb)
            elif self._state == "high":
                if ratio <= self.config.low_watermark:
                    if self._low_since == 0.0:
                        self._low_since = now
                    elif now - self._low_since >= self.config.sustain_s:
                        self._state = "normal"
                        if self._on_low:
                            try: self._on_low(ratio)
                            except Exception: logger.exception("pressure_on_low")
                        logger.info("memory_pressure_recovered ratio=%.2f", ratio)
                else:
                    self._low_since = 0.0
        return ratio

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        interval = self.config.sample_ms / 1000.0
        while self._running:
            self.sample_once()
            time.sleep(interval)

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def last_ratio(self) -> float:
        return self._last_ratio


class TierConsistencyChecker:
    """Cross-check tier budgets across config / engine / IPC.
    All three must agree or the engine refuses to start."""
    def __init__(self) -> None:
        self.violations: List[str] = []
    def check(self, config_budgets, engine_budgets, ipc_budgets):
        issues = []
        for tier_name, cfg in config_budgets.items():
            eng = engine_budgets.get(tier_name)
            ipc = ipc_budgets.get(tier_name)
            if eng is None: issues.append(f"{tier_name}: missing in engine"); continue
            if ipc is None: issues.append(f"{tier_name}: missing in IPC"); continue
            for key in ("max_claims", "max_chunk_chars"):
                if key in cfg:
                    cfg_val = cfg[key]
                    eng_val = getattr(eng, key, None)
                    ipc_val = ipc.get(key) if isinstance(ipc, dict) else getattr(ipc, key, None)
                    if eng_val is not None and eng_val != cfg_val:
                        issues.append(f"{tier_name}.{key}: config={cfg_val} engine={eng_val}")
                    if ipc_val is not None and ipc_val != cfg_val:
                        issues.append(f"{tier_name}.{key}: config={cfg_val} ipc={ipc_val}")
        self.violations.extend(issues)
        return (len(issues) == 0, issues)


class MemoryGradientMonitor:
    """Alarm on RSS growth > threshold MB/min over a window."""
    def __init__(self, threshold_mb_per_min=1.0, sample_count=20):
        self.threshold = threshold_mb_per_min
        self.sample_count = sample_count
        self._samples: List[tuple] = []
        self.alarms = 0
    def sample(self, rss_mb, ts=None):
        t = ts if ts is not None else time.monotonic()
        self._samples.append((t, rss_mb))
        if len(self._samples) > self.sample_count:
            self._samples = self._samples[-self.sample_count:]
        if len(self._samples) < self.sample_count: return False
        first_t, first_mb = self._samples[0]
        last_t, last_mb = self._samples[-1]
        dt_min = (last_t - first_t) / 60.0
        if dt_min <= 0: return False
        gradient = (last_mb - first_mb) / dt_min
        if gradient > self.threshold:
            self.alarms += 1; return True
        return False
    def stats(self):
        if len(self._samples) < 2:
            return {"samples": len(self._samples), "alarms": self.alarms}
        first_t, first_mb = self._samples[0]
        last_t, last_mb = self._samples[-1]
        dt_min = max(1e-6, (last_t - first_t) / 60.0)
        return {"samples": len(self._samples), "alarms": self.alarms,
                "gradient_mb_per_min": (last_mb - first_mb) / dt_min}


class ColdStartProfiler:
    """Record warmup durations per subsystem. Alarm if > expected × 1.5."""
    def __init__(self, expected_ms):
        self.expected = dict(expected_ms)
        self._start: Dict[str, float] = {}
        self._durations: Dict[str, List[float]] = {}
        self.alarms = 0
    def start(self, subsystem):
        self._start[subsystem] = time.perf_counter()
    def stop(self, subsystem):
        if subsystem not in self._start:
            return
        dur = (time.perf_counter() - self._start.pop(subsystem)) * 1000
        self._durations.setdefault(subsystem, []).append(dur)
        if len(self._durations[subsystem]) > 64:
            self._durations[subsystem] = self._durations[subsystem][-32:]
        expected = self.expected.get(subsystem, 100.0)
        if dur > expected * 1.5:
            self.alarms += 1
            logger.warning("cold_start_slow subsystem=%s dur=%.1fms expected=%.1fms",
                           subsystem, dur, expected)
    def stats(self):
        return {k: sorted(v)[-5:] for k, v in self._durations.items()}
