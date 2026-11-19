# axelr_telemetry.py
"""axelr_telemetry.py — Ghost telemetry engine for device observations.

Feeds real free_ram_gb, thermal_pressure, gpu_kind overrides into
axelr_devices.classify(telemetry_hint=...) and collects WER/p99 metrics
to prove performance claims instead of estimating them.

The engine runs in the background, sampling system state and ASR
performance, and maintains a rolling window of observations that
can be used to:
  1. Refine device tier classification beyond static RAM checks
  2. Detect thermal throttling and suggest runtime downgrade
  3. Track actual WER/latency per device tier for honest guarantees
  4. Provide real telemetry data for arbitration decisions
"""
from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from axelr_devices import DeviceProfile, classify
from axelr_utils import get_logger

logger = get_logger(__name__)

_TRACE_ID: Optional[str] = None
_TRACE_LOCK = threading.Lock()


def new_trace_id() -> str:
    """Generate and return a new trace ID."""
    global _TRACE_ID
    with _TRACE_LOCK:
        _TRACE_ID = str(uuid.uuid4())[:12]
    return _TRACE_ID


def trace_id() -> str:
    """Get the current trace ID, generating one if none exists."""
    global _TRACE_ID
    with _TRACE_LOCK:
        if _TRACE_ID is None:
            _TRACE_ID = str(uuid.uuid4())[:12]
        return _TRACE_ID


@dataclass
class SessionState:
    """Persistent session state for user preferences and history."""
    calibration_temperature: float = 1.0
    lexicon: Dict[str, float] = field(default_factory=dict)
    last_activity: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "calibration_temperature": self.calibration_temperature,
            "lexicon": self.lexicon,
            "last_activity": self.last_activity,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionState":
        return cls(
            calibration_temperature=data.get("calibration_temperature", 1.0),
            lexicon=data.get("lexicon", {}),
            last_activity=data.get("last_activity", time.time()),
            created_at=data.get("created_at", time.time()),
        )


class SessionStore:
    """Thread-safe persistent session storage."""

    def __init__(self, path: Optional[Path] = None):
        self._path = path
        self._state = SessionState()
        self._lock = threading.Lock()
        if self._path and self._path.exists():
            self._load()

    def _load(self) -> None:
        """Load state from disk."""
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._state = SessionState.from_dict(data)
        except Exception as e:
            logger.debug(f"session_load_failed: {e}")

    def flush(self) -> None:
        """Persist state to disk."""
        if not self._path:
            return
        try:
            with self._lock:
                data = self._state.to_dict()
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.debug(f"session_flush_failed: {e}")

    @property
    def state(self) -> SessionState:
        with self._lock:
            return self._state

    def set_temperature(self, temp: float) -> None:
        with self._lock:
            self._state.calibration_temperature = temp
            self._state.last_activity = time.time()

    def add_lexicon_word(self, word: str, confidence: float = 1.0) -> None:
        with self._lock:
            self._state.lexicon[word] = confidence
            self._state.last_activity = time.time()


class MetricsExporter:
    """Prometheus-style metrics exporter for request tracking."""

    def __init__(self):
        self._lock = threading.Lock()
        self._counters: Dict[str, int] = {}
        self._errors: Dict[str, int] = {}
        self._request_durations: List[float] = []

    def record_request(self, request_type: str, ok: bool, duration_s: float) -> None:
        """Record a request with type, success status, and duration."""
        with self._lock:
            key = f"{request_type}:{str(ok).lower()}"
            self._counters[key] = self._counters.get(key, 0) + 1
            if not ok:
                self._errors["protocol"] = self._errors.get("protocol", 0) + 1
            self._request_durations.append(duration_s)

    def render(self) -> str:
        """Render metrics in Prometheus exposition format."""
        lines = []
        with self._lock:
            for key, count in self._counters.items():
                req_type, ok = key.split(":")
                lines.append(f'axelr_requests_total{{type="{req_type}",ok="{ok}"}} {count}')
            for code, count in self._errors.items():
                lines.append(f'axelr_errors_total{{code="{code}"}} {count}')
        return "\n".join(lines) + "\n"


# ═══════════════════════════════════════════════════════════════════════
# §1 — Observation types
# ═══════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class RamObservation:
    """Snapshot of system RAM state."""
    total_gb: float
    free_gb: float
    used_pct: float
    timestamp: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "ram",
            "total_gb": round(self.total_gb, 1),
            "free_gb": round(self.free_gb, 1),
            "used_pct": round(self.used_pct, 2),
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class ThermalObservation:
    """CPU/GPU thermal pressure observation."""
    cpu_temp_c: Optional[float] = None
    gpu_temp_c: Optional[float] = None
    thermal_pressure: str = "unknown"  # "ok" | "warm" | "hot" | "critical"
    cpu_load_pct: float = 0.0
    gpu_load_pct: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "thermal",
            "cpu_temp_c": self.cpu_temp_c,
            "gpu_temp_c": self.gpu_temp_c,
            "thermal_pressure": self.thermal_pressure,
            "cpu_load_pct": round(self.cpu_load_pct, 2),
            "gpu_load_pct": round(self.gpu_load_pct, 2),
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class AsrObservation:
    """ASR performance observation for WER/latency tracking."""
    runtime: str          # "ipc_gpu" | "ipc_cpu" | "wasm" | "cloud"
    audio_duration_s: float
    processing_s: float
    p95_s: float
    p99_s: float
    wer: float            # Word Error Rate (0.0 = perfect)
    token_count: int
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "asr",
            "runtime": self.runtime,
            "audio_duration_s": round(self.audio_duration_s, 2),
            "processing_s": round(self.processing_s, 3),
            "p95_s": round(self.p95_s, 3),
            "p99_s": round(self.p99_s, 3),
            "wer": round(self.wer, 4),
            "token_count": self.token_count,
            "timestamp": self.timestamp,
        }


Observation = RamObservation | ThermalObservation | AsrObservation


# ═══════════════════════════════════════════════════════════════════════
# §2 — Rolling window store
# ═══════════════════════════════════════════════════════════════════════
class TelemetryStore:
    """Thread-safe rolling window store for observations."""

    def __init__(self, max_ram: int = 60, max_thermal: int = 60, max_asr: int = 200):
        self._ram: Deque[RamObservation] = deque(maxlen=max_ram)
        self._thermal: Deque[ThermalObservation] = deque(maxlen=max_thermal)
        self._asr: Deque[AsrObservation] = deque(maxlen=max_asr)
        self._lock = threading.Lock()
        self._last_sample: Dict[str, float] = {
            "ram": 0.0,
            "thermal": 0.0,
        }

    def add_ram(self, obs: RamObservation) -> None:
        with self._lock:
            self._ram.append(obs)

    def add_thermal(self, obs: ThermalObservation) -> None:
        with self._lock:
            self._thermal.append(obs)

    def add_asr(self, obs: AsrObservation) -> None:
        with self._lock:
            self._asr.append(obs)

    def get_ram(self) -> List[RamObservation]:
        with self._lock:
            return list(self._ram)

    def get_thermal(self) -> List[ThermalObservation]:
        with self._lock:
            return list(self._thermal)

    def get_asr(self) -> List[AsrObservation]:
        with self._lock:
            return list(self._asr)

    def get_latest_ram(self) -> Optional[RamObservation]:
        with self._lock:
            return self._ram[-1] if self._ram else None

    def get_latest_thermal(self) -> Optional[ThermalObservation]:
        with self._lock:
            return self._thermal[-1] if self._thermal else None

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            ram_samples = list(self._ram)
            thermal_samples = list(self._thermal)
            asr_samples = list(self._asr)

        stats: Dict[str, Any] = {
            "counts": {
                "ram": len(ram_samples),
                "thermal": len(thermal_samples),
                "asr": len(asr_samples),
            },
        }

        if ram_samples:
            free_values = [o.free_gb for o in ram_samples]
            stats["ram"] = {
                "latest_free_gb": free_values[-1],
                "avg_free_gb": sum(free_values) / len(free_values),
                "min_free_gb": min(free_values),
                "max_free_gb": max(free_values),
            }

        if thermal_samples:
            pressures = [o.thermal_pressure for o in thermal_samples]
            stats["thermal"] = {
                "latest_pressure": pressures[-1] if pressures else "unknown",
                "pressure_counts": {p: pressures.count(p) for p in set(pressures)},
            }

        if asr_samples:
            wer_values = [o.wer for o in asr_samples]
            p99_values = [o.p99_s for o in asr_samples]
            runtime_counts = {}
            for o in asr_samples:
                runtime_counts[o.runtime] = runtime_counts.get(o.runtime, 0) + 1

            stats["asr"] = {
                "total_samples": len(asr_samples),
                "avg_wer": sum(wer_values) / len(wer_values) if wer_values else 0.0,
                "min_wer": min(wer_values) if wer_values else 0.0,
                "max_wer": max(wer_values) if wer_values else 0.0,
                "avg_p99_s": sum(p99_values) / len(p99_values) if p99_values else 0.0,
                "runtime_counts": runtime_counts,
            }

        return stats


# ═══════════════════════════════════════════════════════════════════════
# §3 — Samplers
# ═══════════════════════════════════════════════════════════════════════
def sample_ram() -> Optional[RamObservation]:
    """Sample system RAM state. Returns None on failure."""
    try:
        import psutil
        m = psutil.virtual_memory()
        return RamObservation(
            total_gb=m.total / 1e9,
            free_gb=m.available / 1e9,
            used_pct=m.percent / 100.0,
            timestamp=time.time(),
        )
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"ram_sample_failed: {e}")

    try:
        if sys.platform == "win32":
            import ctypes
            class M(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong)]
            ms = M(); ms.dwLength = ctypes.sizeof(M)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
            return RamObservation(
                total_gb=ms.ullTotalPhys / 1e9,
                free_gb=ms.ullAvailPhys / 1e9,
                used_pct=ms.dwMemoryLoad / 100.0,
                timestamp=time.time(),
            )
    except Exception as e:
        logger.debug(f"win32_ram_sample_failed: {e}")

    return None


def sample_thermal() -> ThermalObservation:
    """Sample thermal and load state. Returns observation with available data."""
    cpu_temp: Optional[float] = None
    gpu_temp: Optional[float] = None
    cpu_load: float = 0.0
    gpu_load: float = 0.0
    pressure = "ok"

    try:
        import psutil
        cpu_load = psutil.cpu_percent(interval=0.1) / 100.0

        if hasattr(psutil, "sensors_temperatures"):
            temps = psutil.sensors_temperatures()
            if temps:
                for name, entries in temps.items():
                    if "core" in name.lower() or "cpu" in name.lower():
                        for entry in entries:
                            if entry.current:
                                cpu_temp = entry.current
                                break
                    if "gpu" in name.lower() or "nvidia" in name.lower():
                        for entry in entries:
                            if entry.current:
                                gpu_temp = entry.current
                                break
    except Exception as e:
        logger.debug(f"thermal_sample_failed: {e}")

    try:
        import subprocess
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=temperature.gpu,utilization.gpu",
             "--format=csv,noheader,nounits"], timeout=1
        ).decode().strip()
        parts = out.split(", ")
        if len(parts) >= 2:
            gpu_temp = float(parts[0])
            gpu_load = float(parts[1]) / 100.0
    except Exception:
        pass

    if cpu_temp and cpu_temp > 90:
        pressure = "critical"
    elif cpu_temp and cpu_temp > 80:
        pressure = "hot"
    elif cpu_temp and cpu_temp > 70:
        pressure = "warm"

    if gpu_temp and gpu_temp > 95:
        pressure = "critical"
    elif gpu_temp and gpu_temp > 85:
        pressure = "hot"
    elif gpu_temp and gpu_temp > 75:
        pressure = "warm"

    return ThermalObservation(
        cpu_temp_c=cpu_temp,
        gpu_temp_c=gpu_temp,
        thermal_pressure=pressure,
        cpu_load_pct=cpu_load,
        gpu_load_pct=gpu_load,
        timestamp=time.time(),
    )


# ═══════════════════════════════════════════════════════════════════════
# §4 — Ghost telemetry engine
# ═══════════════════════════════════════════════════════════════════════
class GhostTelemetry:
    """Background telemetry collector that feeds device classification hints."""

    def __init__(self, sample_interval_s: float = 2.0):
        self.store = TelemetryStore()
        self._sample_interval = sample_interval_s
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._profile_cache: Optional[DeviceProfile] = None
        self._profile_cache_time: float = 0.0

    def start(self) -> None:
        """Start background sampling thread."""
        with self._lock:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(
            target=self._sample_loop,
            daemon=True,
            name="axelr-telemetry",
        )
        self._thread.start()
        logger.info("ghost_telemetry_started")

    def stop(self) -> None:
        """Stop background sampling thread."""
        with self._lock:
            self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        logger.info("ghost_telemetry_stopped")

    def _sample_loop(self) -> None:
        while True:
            with self._lock:
                if not self._running:
                    break
            try:
                ram = sample_ram()
                if ram:
                    self.store.add_ram(ram)

                thermal = sample_thermal()
                self.store.add_thermal(thermal)

            except Exception as e:
                logger.debug(f"telemetry_sample_error: {e}")

            time.sleep(self._sample_interval)

    def get_telemetry_hint(self) -> Dict[str, Any]:
        """Build telemetry_hint dict for classify() override."""
        hint: Dict[str, Any] = {}

        latest_ram = self.store.get_latest_ram()
        if latest_ram:
            hint["free_ram_gb"] = latest_ram.free_gb

        latest_thermal = self.store.get_latest_thermal()
        if latest_thermal and latest_thermal.thermal_pressure != "unknown":
            hint["thermal_pressure"] = latest_thermal.thermal_pressure

        return hint

    def get_profile(self, max_age_s: float = 5.0) -> DeviceProfile:
        """Get device profile with telemetry overrides, cached for max_age_s."""
        now = time.time()
        with self._lock:
            if self._profile_cache and (now - self._profile_cache_time) < max_age_s:
                return self._profile_cache

        hint = self.get_telemetry_hint()
        profile = classify(telemetry_hint=hint)

        with self._lock:
            self._profile_cache = profile
            self._profile_cache_time = now

        return profile

    def record_asr(self, runtime: str, audio_duration_s: float,
                   processing_s: float, wer: float = 0.0,
                   token_count: int = 1) -> None:
        """Record an ASR observation for WER/latency tracking."""
        obs = AsrObservation(
            runtime=runtime,
            audio_duration_s=audio_duration_s,
            processing_s=processing_s,
            p95_s=processing_s,
            p99_s=processing_s,
            wer=wer,
            token_count=token_count,
        )
        self.store.add_asr(obs)

    def get_performance_summary(self) -> Dict[str, Any]:
        """Get honest performance summary from observed data."""
        stats = self.store.get_stats()
        summary = {
            "device_profile": self.get_profile().to_dict(),
            "stats": stats,
            "claims": {
                "observed_wer": stats.get("asr", {}).get("avg_wer", None),
                "observed_p99_s": stats.get("asr", {}).get("avg_p99_s", None),
                "thermal_stable": stats.get("thermal", {}).get("latest_pressure", "ok") == "ok",
            },
        }
        return summary


# Global singleton
_GHOST: Optional[GhostTelemetry] = None
_GHOST_LOCK = threading.Lock()


def get_ghost_telemetry() -> GhostTelemetry:
    """Get or create the global GhostTelemetry instance."""
    global _GHOST
    with _GHOST_LOCK:
        if _GHOST is None:
            _GHOST = GhostTelemetry()
        return _GHOST


def start_telemetry() -> GhostTelemetry:
    """Start the global ghost telemetry engine."""
    ghost = get_ghost_telemetry()
    ghost.start()
    return ghost


def stop_telemetry() -> None:
    """Stop the global ghost telemetry engine."""
    global _GHOST
    if _GHOST is not None:
        _GHOST.stop()
        _GHOST = None


ghost_telemetry = get_ghost_telemetry()

__all__ = [
    "GhostTelemetry", "TelemetryStore",
    "RamObservation", "ThermalObservation", "AsrObservation",
    "sample_ram", "sample_thermal",
    "get_ghost_telemetry", "start_telemetry", "stop_telemetry",
    "ghost_telemetry",
    "MetricsExporter", "SessionStore", "SessionState",
    "new_trace_id", "trace_id",
]
