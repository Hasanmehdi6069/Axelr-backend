# axelr_utils.py
"""axelr_utils.py — Logging · MetricsCollector · RollingMetrics · Clipboard ·
TokenBucket · CircuitBreakerV2 · shared exceptions.

Split from the original 900-line utils to keep files 250-650 lines.
IPC client lives in `axelr_ipc.py`; Prometheus/session in `axelr_telemetry.py`.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

_LOGGING_CONFIGURED = False
_FMT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def configure_logging(level: int = logging.INFO) -> None:
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return
    logging.basicConfig(level=level, format=_FMT, stream=sys.stderr)
    _LOGGING_CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    if not _LOGGING_CONFIGURED:
        configure_logging()
    return logging.getLogger(name)


logger = get_logger(__name__)

NUM_WORDS: Dict[str, str] = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
    "10": "ten",
}
MONTHS = ("january", "february", "march", "april", "may", "june",
          "july", "august", "september", "october", "november", "december")


def normalize_text(text: str) -> str:
    return " ".join(text.split()).strip()


class ProtocolError(RuntimeError):
    """Raised when the peer violates the IPC protocol."""


class IPCNotConnectedError(RuntimeError):
    """Raised when IPC is used before connect() or after close()."""


# ═══════════════════════════════════════════════════════════════════════
# MetricsCollector — ring buffer of percentiles
# ═══════════════════════════════════════════════════════════════════════
class MetricsCollector:
    __slots__ = ("_buffers", "_cursors", "_counts", "_cap")
    _CAP = 256

    def __init__(self) -> None:
        self._buffers: Dict[str, List[float]] = {}
        self._cursors: Dict[str, int] = {}
        self._counts: Dict[str, int] = {}
        self._cap = self._CAP

    @staticmethod
    def now() -> float:
        return time.monotonic()

    @staticmethod
    def elapsed_ms(t0: float) -> float:
        return (time.monotonic() - t0) * 1000.0

    def observe(self, name: str, value: float) -> None:
        if name not in self._buffers:
            self._buffers[name] = [0.0] * self._cap
            self._cursors[name] = 0
            self._counts[name] = 0
        cur = self._cursors[name]
        self._buffers[name][cur] = value
        self._cursors[name] = (cur + 1) % self._cap
        self._counts[name] = min(self._cap, self._counts[name] + 1)

    def percentile(self, name: str, p: float) -> float:
        buf = self._buffers.get(name)
        count = self._counts.get(name, 0)
        if buf is None or count == 0:
            return 0.0
        samples = buf if count >= self._cap else buf[:count]
        sv = sorted(samples)
        idx = max(0, min(len(sv) - 1, int(p * (len(sv) - 1))))
        return sv[idx]

    def dump(self, log: Optional[logging.Logger] = None) -> None:
        log = log or logger
        for name in self._buffers:
            log.info("metric %s p50=%.2f p95=%.2f p99=%.2f", name,
                     self.percentile(name, 0.50),
                     self.percentile(name, 0.95),
                     self.percentile(name, 0.99))


# ═══════════════════════════════════════════════════════════════════════
# RollingMetrics — thresholded rolling windows with EMA
# ═══════════════════════════════════════════════════════════════════════
@dataclass(slots=True, frozen=True)
class MetricThreshold:
    warning_p95: float
    critical_p95: float
    warning_p99: float
    critical_p99: float


@dataclass(slots=True)
class RollingMetric:
    buffer: List[float]
    cursor: int
    window_size: int
    count: int = 0
    thresholds: Optional[MetricThreshold] = None
    alert_count: int = 0


class RollingMetrics:
    __slots__ = ("_metrics", "_lock")

    def __init__(self) -> None:
        self._metrics: Dict[str, RollingMetric] = {}
        self._lock = threading.Lock()

    def register_metric(self, name: str, window_size: int = 1000,
                        thresholds: Optional[MetricThreshold] = None) -> None:
        with self._lock:
            if name not in self._metrics:
                self._metrics[name] = RollingMetric(
                    buffer=[0.0] * window_size, cursor=0,
                    window_size=window_size, thresholds=thresholds)

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            m = self._metrics.get(name)
            if not m:
                return
            m.buffer[m.cursor] = value
            m.cursor = (m.cursor + 1) % m.window_size
            m.count = min(m.window_size, m.count + 1)
            if m.thresholds and m.cursor % 100 == 0:
                self._check_locked(m)

    def _check_locked(self, m: RollingMetric) -> None:
        if not m.thresholds:
            return
        samples = (m.buffer if m.count >= m.window_size
                   else m.buffer[:m.count])
        if not samples:
            return
        p95 = self._percentile(samples, 0.95)
        p99 = self._percentile(samples, 0.99)
        if p95 > m.thresholds.critical_p95 or p99 > m.thresholds.critical_p99:
            m.alert_count += 1
            logger.critical("threshold_critical p95=%.2f p99=%.2f", p95, p99)
        elif p95 > m.thresholds.warning_p95 or p99 > m.thresholds.warning_p99:
            m.alert_count += 1
            logger.warning("threshold_warning p95=%.2f p99=%.2f", p95, p99)

    @staticmethod
    def _percentile(samples: List[float], p: float) -> float:
        if not samples:
            return 0.0
        sv = sorted(samples)
        idx = max(0, min(len(sv) - 1, int(p * (len(sv) - 1))))
        return sv[idx]

    def get_ema(self, name: str, alpha: float = 0.1) -> Optional[float]:
        with self._lock:
            m = self._metrics.get(name)
            if not m:
                return None
            samples = (m.buffer if m.count >= m.window_size
                       else m.buffer[:m.count])
            if not samples:
                return 0.0
            ema = samples[0]
            for v in samples[1:]:
                ema = alpha * v + (1 - alpha) * ema
            return ema


# ═══════════════════════════════════════════════════════════════════════
# Clipboard — platform-native, non-blocking
# ═══════════════════════════════════════════════════════════════════════
class Clipboard:
    """Platform-native clipboard. Non-blocking with timeout. No tkinter."""
    __slots__ = ()

    @staticmethod
    def set(text: str, timeout_s: float = 0.2) -> bool:
        import subprocess
        import platform
        system = platform.system()
        cmd = None
        if system == "Darwin":
            cmd = ["pbcopy"]
        elif system == "Windows":
            cmd = ["clip"]
        elif system == "Linux":
            for tool in (["xclip", "-selection", "clipboard"],
                         ["xsel", "--clipboard", "--input"]):
                try:
                    subprocess.run(["which", tool[0]], check=True,
                                   capture_output=True, timeout=0.1)
                    cmd = tool
                    break
                except Exception:
                    continue
        if cmd is None:
            logger.warning("clipboard_unsupported platform=%s", system)
            return False
        try:
            proc = subprocess.run(cmd, input=text.encode("utf-8"),
                                  timeout=timeout_s, check=True)
            return proc.returncode == 0
        except subprocess.TimeoutExpired:
            logger.error("clipboard_timeout")
            return False
        except Exception:
            logger.exception("clipboard_set_failed")
            return False


# ═══════════════════════════════════════════════════════════════════════
# TokenBucket — per-session rate limiter
# ═══════════════════════════════════════════════════════════════════════
class TokenBucket:
    __slots__ = ("_capacity", "_rate", "_tokens", "_last_refill", "_lock")

    def __init__(self, capacity: int = 100,
                 refill_per_sec: float = 100.0) -> None:
        self._capacity = float(capacity)
        self._rate = float(refill_per_sec)
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, cost: float = 1.0):
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._capacity,
                               self._tokens + elapsed * self._rate)
            self._last_refill = now
            if self._tokens >= cost:
                self._tokens -= cost
                return True, 0.0
            deficit = cost - self._tokens
            return False, max(0.001, deficit / self._rate)


# ═══════════════════════════════════════════════════════════════════════
# CircuitBreaker — single owner. Prevents cascade failures.
# ═══════════════════════════════════════════════════════════════════════
class CircuitState:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Canonical circuit breaker. No other module may define this class.

    States: CLOSED → (threshold failures) → OPEN → (recovery_s) → HALF_OPEN
            → (success) → CLOSED  |  (failure) → OPEN
    """
    __slots__ = ("_threshold", "_recovery_s", "_half_open_timeout",
                 "_failures", "_state", "_opened_at", "_last_failure_ts",
                 "_half_open_started", "_lock", "_metrics")

    def __init__(self, failure_threshold: int = 5,
                 recovery_s: float = 30.0,
                 half_open_timeout: float = 5.0) -> None:
        import threading
        self._threshold = failure_threshold
        self._recovery_s = recovery_s
        self._half_open_timeout = half_open_timeout
        self._failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at = 0.0
        self._last_failure_ts = 0.0
        self._half_open_started = 0.0
        self._lock = threading.Lock()
        self._metrics = {"trips": 0, "recoveries": 0, "failed_requests": 0}

    def __enter__(self) -> "CircuitBreaker":
        if not self.allow():
            raise RuntimeError("circuit breaker is open")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is not None:
            self.record_failure()
        else:
            self.record_success()

    @property
    def state(self) -> str:
        return self._state

    def allow(self) -> bool:
        with self._lock:
            now = time.monotonic()
            if self._state == CircuitState.OPEN:
                if now - self._opened_at > self._recovery_s:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_started = now
                    logger.info("circuit_breaker_half_open")
                    return True
                return False
            if self._state == CircuitState.HALF_OPEN:
                if now - self._half_open_started > self._half_open_timeout:
                    self._state = CircuitState.OPEN
                    self._opened_at = now
                    logger.warning("circuit_breaker_half_open_timeout")
                    return False
                return True
            return True

    def record(self, success: bool) -> None:
        self.record_success() if success else self.record_failure()

    def record_success(self) -> None:
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED
                self._failures = 0
                self._metrics["recoveries"] += 1
                logger.info("circuit_breaker_recovered")
            elif self._state == CircuitState.CLOSED:
                self._failures = max(0, self._failures - 1)

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._metrics["failed_requests"] += 1
            if (self._failures >= self._threshold
                    or self._state == CircuitState.HALF_OPEN):
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                self._last_failure_ts = self._opened_at
                self._metrics["trips"] += 1
                logger.error("circuit_breaker_open failures=%d", self._failures)

    def metrics(self) -> Dict[str, int]:
        return dict(self._metrics)


__all__ = [
    "configure_logging", "get_logger", "normalize_text",
    "NUM_WORDS", "MONTHS", "ProtocolError", "IPCNotConnectedError",
    "MetricsCollector", "MetricThreshold", "RollingMetrics", "RollingMetric",
    "Clipboard", "TokenBucket", "CircuitBreaker", "CircuitState",
]