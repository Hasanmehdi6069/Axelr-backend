# axelr_devices.py
"""axelr_devices.py — Device tier classification + feature availability.

Cross-platform, cross-runtime. JS and Python use the same tier names and
boundaries so arbitration logic is identical everywhere.

Tiers (IntEnum — comparable with <, <=, >=):
  MOBILE       — < 4 GB system RAM. Phones, budget tablets.
  LIGHT        — 4–8 GB. Budget laptops, mid tablets.
  STANDARD     — 8–16 GB. Most laptops, mid desktops.
  PERFORMANCE  — 16–32 GB. High-end laptops, mid workstations.
  WORKSTATION  — 32+ GB. Workstations, servers.

Features declare min_tier, min_free_ram_mb, requires_gpu. Any feature
that fails any check is refused with an honest user-facing message —
never silently degraded, never "try anyway and crash."

Ready for ghost telemetry: classify(telemetry_hint={...}) overrides
local detection when the telemetry engine has accurate data.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Dict, List, Optional, Tuple

from axelr_utils import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# §1 — Tier enum
# ═══════════════════════════════════════════════════════════════════════
class DeviceTier(IntEnum):
    MOBILE = 0
    LIGHT = 1
    STANDARD = 2
    PERFORMANCE = 3
    WORKSTATION = 4

    @property
    def label(self) -> str:
        return {
            DeviceTier.MOBILE: "Mobile",
            DeviceTier.LIGHT: "Light",
            DeviceTier.STANDARD: "Standard",
            DeviceTier.PERFORMANCE: "Performance",
            DeviceTier.WORKSTATION: "Workstation",
        }[self]

    @property
    def ram_min_gb(self) -> float:
        return {
            DeviceTier.MOBILE: 0.0,
            DeviceTier.LIGHT: 4.0,
            DeviceTier.STANDARD: 8.0,
            DeviceTier.PERFORMANCE: 16.0,
            DeviceTier.WORKSTATION: 32.0,
        }[self]


# ═══════════════════════════════════════════════════════════════════════
# §2 — Profile + FeatureRequirement
# ═══════════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class DeviceProfile:
    tier: DeviceTier
    total_ram_gb: float
    free_ram_gb: float
    is_mobile: bool
    is_battery: bool
    gpu_kind: str  # "cuda" | "metal" | "webgl2" | "none"
    gpu_name: str
    cpu_cores: int
    platform: str
    detected_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier": self.tier.name.lower(),
            "tier_label": self.tier.label,
            "total_ram_gb": round(self.total_ram_gb, 1),
            "free_ram_gb": round(self.free_ram_gb, 1),
            "is_mobile": self.is_mobile,
            "is_battery": self.is_battery,
            "gpu_kind": self.gpu_kind,
            "gpu_name": self.gpu_name,
            "cpu_cores": self.cpu_cores,
            "platform": self.platform,
        }


@dataclass(frozen=True, slots=True)
class FeatureRequirement:
    feature_id: str
    min_tier: DeviceTier
    min_free_ram_mb: int = 0
    requires_gpu: bool = False
    description: str = ""


# ═══════════════════════════════════════════════════════════════════════
# §3 — Feature matrix (single source of truth)
# ═══════════════════════════════════════════════════════════════════════
FEATURES: Dict[str, FeatureRequirement] = {
    "web_speech": FeatureRequirement(
        "web_speech",
        DeviceTier.MOBILE,
        description="Cloud ASR via browser (Web Speech API)",
    ),
    "wasm_whisper_tiny": FeatureRequirement(
        "wasm_whisper_tiny",
        DeviceTier.LIGHT,
        200,
        description="Offline WASM whisper-tiny (browser)",
    ),
    "python_cpu_tiny": FeatureRequirement(
        "python_cpu_tiny",
        DeviceTier.LIGHT,
        400,
        description="Python whisper-tiny (CPU)",
    ),
    "python_cpu_base": FeatureRequirement(
        "python_cpu_base",
        DeviceTier.STANDARD,
        800,
        description="Python whisper-base (CPU)",
    ),
    "python_gpu_tiny": FeatureRequirement(
        "python_gpu_tiny",
        DeviceTier.STANDARD,
        300,
        requires_gpu=True,
        description="GPU whisper-tiny (fast)",
    ),
    "python_gpu_base": FeatureRequirement(
        "python_gpu_base",
        DeviceTier.PERFORMANCE,
        500,
        requires_gpu=True,
        description="GPU whisper-base (high accuracy)",
    ),
    "python_gpu_small": FeatureRequirement(
        "python_gpu_small",
        DeviceTier.PERFORMANCE,
        1200,
        requires_gpu=True,
        description="GPU whisper-small (best accuracy)",
    ),
    "streaming_partials": FeatureRequirement(
        "streaming_partials",
        DeviceTier.LIGHT,
        50,
        description="Live partial transcripts as you speak",
    ),
    "session_persistence": FeatureRequirement(
        "session_persistence",
        DeviceTier.LIGHT,
        description="Vocabulary and calibration persist across sessions",
    ),
    "cross_encoder": FeatureRequirement(
        "cross_encoder",
        DeviceTier.STANDARD,
        10,
        description="Bigram context disambiguation (their/there)",
    ),
    "elite_telemetry": FeatureRequirement(
        "elite_telemetry",
        DeviceTier.LIGHT,
        description="Prometheus metrics endpoint",
    ),
}


# ═══════════════════════════════════════════════════════════════════════
# §4 — RAM detection (psutil → platform-native → conservative default)
# ═══════════════════════════════════════════════════════════════════════
def _read_ram() -> Tuple[float, float]:
    """Return (total_gb, free_gb). Falls back through 3 detection layers."""
    try:
        import psutil

        m = psutil.virtual_memory()
        return (m.total / 1e9, m.available / 1e9)
    except ImportError:
        pass

    if sys.platform == "linux":
        try:
            total_kb = free_kb = 0
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        total_kb = int(line.split()[1])
                    elif line.startswith("MemAvailable:"):
                        free_kb = int(line.split()[1])
            if total_kb:
                return (total_kb / 1e6, free_kb / 1e6)
        except Exception:
            pass

    if sys.platform == "win32":
        try:
            import ctypes

            class M(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            ms = M()
            ms.dwLength = ctypes.sizeof(M)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
            return (ms.ullTotalPhys / 1e9, ms.ullAvailPhys / 1e9)
        except Exception:
            pass

    if sys.platform == "darwin":
        try:
            total = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], timeout=2).strip()) / 1e9
            return (total, total * 0.35)
        except Exception:
            pass

    logger.warning("ram_detection_failed; assuming 8GB total / 4GB free")
    return (8.0, 4.0)


def _detect_mobile() -> bool:
    if os.environ.get("AXELR_MOBILE", "") == "1":
        return True
    if sys.platform in ("ios", "android"):
        return True
    return False


def _detect_battery() -> bool:
    try:
        import psutil

        b = psutil.sensors_battery()
        return b is not None and not b.power_plugged
    except (ImportError, AttributeError):
        return False


def _detect_gpu() -> Tuple[str, str]:
    """Return (kind, name). kind ∈ {cuda, metal, none}."""
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            name = "NVIDIA (CUDA)"
            try:
                out = subprocess.check_output(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], timeout=2)
                name = out.decode().strip().split("\n")[0]
            except Exception:
                pass
            return ("cuda", name)
    except Exception:
        pass

    if sys.platform == "darwin":
        try:
            out = subprocess.check_output(["system_profiler", "SPDisplaysDataType"], timeout=2).decode()
            if "Apple M" in out or "Apple GPU" in out:
                return ("metal", "Apple Silicon")
        except Exception:
            pass

    return ("none", "")


# ═══════════════════════════════════════════════════════════════════════
# §5 — Classification
# ═══════════════════════════════════════════════════════════════════════
def _assign_tier(total_gb: float, is_mobile: bool) -> DeviceTier:
    """Tier from total RAM (not free — free fluctuates). Mobile always ≤ LIGHT."""
    if is_mobile:
        return DeviceTier.MOBILE
    if total_gb < 4:
        return DeviceTier.MOBILE
    if total_gb < 8:
        return DeviceTier.LIGHT
    if total_gb < 16:
        return DeviceTier.STANDARD
    if total_gb < 32:
        return DeviceTier.PERFORMANCE
    return DeviceTier.WORKSTATION


def classify(telemetry_hint: Optional[Dict[str, Any]] = None) -> DeviceProfile:
    """Classify current device. ~5 ms. Accepts ghost-telemetry overrides.

    telemetry_hint keys that override local detection:
      free_ram_gb : float     — more accurate than psutil
      thermal_pressure : str  — "ok"|"warm"|"hot"|"critical"
      battery_pct : float
      gpu_kind : str          — "cuda"|"metal"|"none" (if telemetry knows)
    """
    hint = telemetry_hint or {}
    total_gb, free_gb = _read_ram()
    if "free_ram_gb" in hint and isinstance(hint["free_ram_gb"], (int, float)):
        free_gb = max(0.0, float(hint["free_ram_gb"]))
    is_mobile = _detect_mobile()
    is_battery = _detect_battery()
    gpu_kind, gpu_name = _detect_gpu()
    if hint.get("gpu_kind"):
        gpu_kind = str(hint["gpu_kind"])
    tier = _assign_tier(total_gb, is_mobile)
    return DeviceProfile(
        tier=tier,
        total_ram_gb=total_gb,
        free_ram_gb=free_gb,
        is_mobile=is_mobile,
        is_battery=is_battery,
        gpu_kind=gpu_kind,
        gpu_name=gpu_name,
        cpu_cores=os.cpu_count() or 2,
        platform=("mobile" if is_mobile else sys.platform),
        detected_at=time.time(),
    )


# ═══════════════════════════════════════════════════════════════════════
# §6 — Feature availability + user-facing message
# ═══════════════════════════════════════════════════════════════════════
def check_feature(feature_id: str, profile: Optional[DeviceProfile] = None) -> Tuple[bool, str]:
    """Returns (available, honest_user_message). Empty message if available."""
    req = FEATURES.get(feature_id)
    if req is None:
        return (False, f"Unknown feature: {feature_id}")
    if profile is None:
        profile = classify()

    reasons: List[str] = []
    if profile.tier < req.min_tier:
        reasons.append(f"needs {req.min_tier.label} tier or higher (your device: {profile.tier.label})")
    if req.min_free_ram_mb > 0:
        free_mb = int(profile.free_ram_gb * 1024)
        if free_mb < req.min_free_ram_mb:
            reasons.append(f"needs {req.min_free_ram_mb} MB free RAM (your device: {free_mb} MB)")
    if req.requires_gpu and profile.gpu_kind == "none":
        reasons.append("requires GPU (no CUDA/Metal detected)")

    if not reasons:
        return (True, "")

    msg = (
        f"{req.description or feature_id} — unavailable: "
        + "; ".join(reasons)
        + ". Choose a lighter alternative or use a device with more RAM/GPU."
    )
    return (False, msg)


def list_available_features(profile: Optional[DeviceProfile] = None) -> List[Dict[str, Any]]:
    if profile is None:
        profile = classify()
    out = []
    for fid, req in FEATURES.items():
        ok, msg = check_feature(fid, profile)
        out.append(
            {
                "feature_id": fid,
                "description": req.description,
                "min_tier": req.min_tier.label,
                "requires_gpu": req.requires_gpu,
                "available": ok,
                "reason": msg,
            }
        )
    return out


# ═══════════════════════════════════════════════════════════════════════
# §7 — Runtime selector (mirrors JS arbitration)
# ═══════════════════════════════════════════════════════════════════════
def select_runtime(profile: Optional[DeviceProfile] = None, preference: str = "auto") -> str:
    """Mirror of axelr-asr.js arbitration. Same tiers, same order."""
    if profile is None:
        profile = classify()

    def ok(fid: str) -> bool:
        return check_feature(fid, profile)[0]

    if preference == "cloud":
        return "cloud"
    if preference == "text":
        return "text"
    if preference == "wasm":
        return "wasm" if ok("wasm_whisper_tiny") else "cloud"
    if preference == "ipc_gpu":
        return "ipc_gpu" if ok("python_gpu_tiny") else "ipc_cpu"
    if preference == "ipc_cpu":
        return "ipc_cpu" if ok("python_cpu_tiny") else "cloud"

    if ok("python_gpu_tiny"):
        return "ipc_gpu"
    if ok("python_cpu_tiny"):
        return "ipc_cpu"
    if ok("wasm_whisper_tiny"):
        return "wasm"
    if ok("web_speech"):
        return "cloud"
    return "text"


# ═══════════════════════════════════════════════════════════════════════
# §8 — User-facing summary + enforcement
# ═══════════════════════════════════════════════════════════════════════
class FeatureUnavailableError(RuntimeError):
    def __init__(self, feature_id: str, user_message: str):
        self.feature_id = feature_id
        self.user_message = user_message
        super().__init__(user_message)


def require_feature(feature_id: str, profile: Optional[DeviceProfile] = None) -> None:
    """Raise FeatureUnavailableError if the device can't run this feature."""
    ok, msg = check_feature(feature_id, profile)
    if not ok:
        raise FeatureUnavailableError(feature_id, msg)


def user_facing_summary(profile: Optional[DeviceProfile] = None) -> str:
    if profile is None:
        profile = classify()
    lines = [
        f"Your device: {profile.tier.label} tier",
        f"  RAM: {profile.total_ram_gb:.1f} GB total, {profile.free_ram_gb:.1f} GB free",
        f"  GPU: {profile.gpu_name or 'None detected'} ({profile.gpu_kind})",
        f"  CPU cores: {profile.cpu_cores}   Platform: {profile.platform}",
        "",
        "Feature availability:",
    ]
    for feat in list_available_features(profile):
        mark = "[YES]" if feat["available"] else "[NO ]"
        lines.append(f"  {mark} {feat['description']}")
        if not feat["available"]:
            lines.append(f"        -> {feat['reason']}")
    return "\n".join(lines)


__all__ = [
    "DeviceTier",
    "DeviceProfile",
    "FeatureRequirement",
    "FEATURES",
    "classify",
    "check_feature",
    "list_available_features",
    "select_runtime",
    "FeatureUnavailableError",
    "require_feature",
    "user_facing_summary",
]
