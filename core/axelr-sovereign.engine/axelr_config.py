# axelr_config.py
"""axelr_config.py — Tier enum · profiles · domains · quantization · validation.

Systems: data feed for #1–#25. Adds env-override + JSON round-trip +
profile validation on top of the previous static config.

Memory budget: ~215 KB (frozen dataclasses + keyword tables).
Dependencies: axelr_utils.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, FrozenSet, Optional, Tuple

from axelr_utils import get_logger

logger = get_logger(__name__)

MAX_MODEL_SIZE_MB: int = 40
CONFIDENCE_THRESHOLD: float = 0.75
CONTEXTUAL_WINDOW_SIZE: int = 100


class Tier(Enum):
    LOW = "low_ram"
    STANDARD = "standard"
    HIGH = "high_end"


class QuantizationFormat(Enum):
    INT4 = "int4"
    INT8 = "int8"


@dataclass(frozen=True, slots=True)
class QuantSpec:
    fmt: QuantizationFormat
    bits: int
    block_size: int
    memory_reduction: float
    accuracy_loss: str


QUANT_SPEC: Dict[QuantizationFormat, QuantSpec] = {
    QuantizationFormat.INT4: QuantSpec(QuantizationFormat.INT4, 4, 32, 0.25, "low"),
    QuantizationFormat.INT8: QuantSpec(QuantizationFormat.INT8, 8, 32, 0.50, "minimal"),
}


@dataclass(frozen=True, slots=True)
class FeatureFlags:
    phonetic_ensemble: bool = True
    streaming_rerank: bool = True
    structured_repair: bool = True
    warm_up: bool = True
    vad_state_machine: bool = True
    cloud_fallback: bool = True
    calibration: bool = True

    def to_dict(self) -> Dict[str, bool]:
        return {
            "phonetic_ensemble": self.phonetic_ensemble,
            "streaming_rerank": self.streaming_rerank,
            "structured_repair": self.structured_repair,
            "warm_up": self.warm_up,
            "vad_state_machine": self.vad_state_machine,
            "cloud_fallback": self.cloud_fallback,
            "calibration": self.calibration,
        }


@dataclass(frozen=True, slots=True)
class SamplingConfig:
    seed: int = 0
    temperature: float = 0.0
    top_k: int = 1


@dataclass(frozen=True, slots=True)
class VadConfig:
    init_noise_floor: float = 1e-3
    noise_alpha: float = 0.05
    speech_multiplier: float = 3.0
    enter_frames: int = 3
    exit_frames: int = 15
    endpoint_ms: int = 700


@dataclass(frozen=True, slots=True)
class CalibrationConfig:
    initial_temperature: float = 1.4
    min_temperature: float = 0.5
    max_temperature: float = 5.0


@dataclass(frozen=True, slots=True)
class HardwareProfile:
    tier: Tier
    max_memory_mb: int
    quantization: QuantizationFormat
    model_size: str
    sample_rate: int
    buffer_frames: int
    kv_cache_max_mb: float
    ring_capacity_samples: int
    flags: FeatureFlags
    vad: VadConfig = field(default_factory=VadConfig)
    calibration: CalibrationConfig = field(default_factory=CalibrationConfig)
    sampling: SamplingConfig = field(default_factory=SamplingConfig)


HARDWARE_PROFILES: Dict[Tier, HardwareProfile] = {
    Tier.LOW: HardwareProfile(
        Tier.LOW, 85, QuantizationFormat.INT4, "tiny", 16000, 8192,
        8.0, 32768,
        FeatureFlags(cloud_fallback=False, warm_up=True),
    ),
    Tier.STANDARD: HardwareProfile(
        Tier.STANDARD, 115, QuantizationFormat.INT8, "base", 16000, 16384,
        20.0, 65536, FeatureFlags(),
    ),
    Tier.HIGH: HardwareProfile(
        Tier.HIGH, 150, QuantizationFormat.INT8, "small", 16000, 32768,
        30.0, 131072, FeatureFlags(),
    ),
}


@dataclass(frozen=True, slots=True)
class MemoryBudget:
    budget_mb: float
    headroom_mb: float
    asr_model_size: str


MEMORY_BUDGETS: Dict[Tier, MemoryBudget] = {
    Tier.LOW: MemoryBudget(50.0, 5.0, "tiny"),
    Tier.STANDARD: MemoryBudget(115.0, 15.0, "base"),
    Tier.HIGH: MemoryBudget(150.0, 10.0, "small"),
}


def budget_for(tier: Tier) -> MemoryBudget:
    return MEMORY_BUDGETS[tier]


def map_js_tier_to_python(js_tier: str) -> Tier:
    """Map JS tier names ("low", "standard", "high") to Python Tier enum."""
    mapping = {
        "low": Tier.LOW,
        "low_ram": Tier.LOW,
        "standard": Tier.STANDARD,
        "high": Tier.HIGH,
        "high_end": Tier.HIGH
    }
    if js_tier not in mapping:
        raise ValueError(f"unknown JS tier: {js_tier}. Valid tiers: low, standard, high")
    return mapping[js_tier]

def map_python_tier_to_js(py_tier: Tier) -> str:
    """Map Python Tier enum to JS tier names ("low", "standard", "high")."""
    mapping = {
        Tier.LOW: "low",
        Tier.STANDARD: "standard",
        Tier.HIGH: "high"
    }
    return mapping[py_tier]

# Moved to final __all__ assignment at end of file

# ═══════════════════════════════════════════════════════════════════════
# Profile selection + validation
# ═══════════════════════════════════════════════════════════════════════
def select_profile(tier: Optional[Tier] = None) -> HardwareProfile:
    """Return a profile; None triggers psutil autodetect."""
    if tier is not None:
        return HARDWARE_PROFILES[tier]
    try:
        import psutil
    except ImportError as exc:
        raise RuntimeError("psutil unavailable; pass explicit Tier") from exc
    total_mb = psutil.virtual_memory().total / (1024 * 1024)
    if total_mb < 4096:
        return HARDWARE_PROFILES[Tier.LOW]
    if total_mb < 16384:
        return HARDWARE_PROFILES[Tier.STANDARD]
    return HARDWARE_PROFILES[Tier.HIGH]


def validate_profile(profile: HardwareProfile) -> Tuple[bool, list]:
    """Return (ok, issues). Raises no exceptions."""
    issues = []
    if profile.max_memory_mb < 32:
        issues.append(f"max_memory_mb={profile.max_memory_mb} < 32 minimum")
    if profile.sample_rate not in (16000, 22050, 44100, 48000):
        issues.append(f"sample_rate={profile.sample_rate} not in standard set")
    if profile.buffer_frames & (profile.buffer_frames - 1):
        issues.append(f"buffer_frames={profile.buffer_frames} must be power of 2")
    if profile.ring_capacity_samples & (profile.ring_capacity_samples - 1):
        issues.append(f"ring_capacity_samples={profile.ring_capacity_samples} must be power of 2")
    if profile.kv_cache_max_mb > profile.max_memory_mb:
        issues.append(f"kv_cache_max_mb={profile.kv_cache_max_mb} > max_memory_mb={profile.max_memory_mb}")
    budget = MEMORY_BUDGETS.get(profile.tier)
    if budget is not None and budget.budget_mb > profile.max_memory_mb:
        issues.append(f"tier {profile.tier.value}: budget={budget.budget_mb} > profile={profile.max_memory_mb}")
    return len(issues) == 0, issues


def validate_all_profiles() -> Tuple[bool, list]:
    all_issues = []
    for tier, profile in HARDWARE_PROFILES.items():
        ok, issues = validate_profile(profile)
        if not ok:
            all_issues.append((tier.value, issues))
    return len(all_issues) == 0, all_issues


# ═══════════════════════════════════════════════════════════════════════
# Environment overrides
# ═══════════════════════════════════════════════════════════════════════
def apply_env_overrides(profile: HardwareProfile) -> HardwareProfile:
    """Read AXELR_TIER, AXELR_QUANT, AXELR_SAMPLE_RATE from env."""
    tier_env = os.environ.get("AXELR_TIER", "").lower()
    if tier_env and tier_env != profile.tier.value:
        try:
            new_tier = Tier(tier_env)
            logger.info("env_override tier=%s", new_tier.value)
            return HARDWARE_PROFILES[new_tier]
        except ValueError:
            logger.warning("invalid AXELR_TIER=%s", tier_env)

    updates: Dict[str, Any] = {}
    quant_env = os.environ.get("AXELR_QUANT", "").lower()
    if quant_env in ("int4", "int8"):
        updates["quantization"] = QuantizationFormat(quant_env)
    sr_env = os.environ.get("AXELR_SAMPLE_RATE")
    if sr_env:
        try:
            sr = int(sr_env)
            if sr in (16000, 22050, 44100, 48000):
                updates["sample_rate"] = sr
        except ValueError:
            pass

    if updates:
        logger.info("env_overrides=%s", list(updates.keys()))
        return replace(profile, **updates)
    return profile


# ═══════════════════════════════════════════════════════════════════════
# JSON round-trip + diff
# ═══════════════════════════════════════════════════════════════════════
def profile_to_dict(profile: HardwareProfile) -> Dict[str, Any]:
    return {
        "tier": profile.tier.value,
        "max_memory_mb": profile.max_memory_mb,
        "quantization": profile.quantization.value,
        "model_size": profile.model_size,
        "sample_rate": profile.sample_rate,
        "buffer_frames": profile.buffer_frames,
        "kv_cache_max_mb": profile.kv_cache_max_mb,
        "ring_capacity_samples": profile.ring_capacity_samples,
        "flags": profile.flags.to_dict(),
    }


def profile_diff(a: HardwareProfile, b: HardwareProfile) -> Dict[str, Tuple[Any, Any]]:
    """Return {(field): (a_value, b_value)} for every difference."""
    da, db = profile_to_dict(a), profile_to_dict(b)
    diff = {}
    for key in da:
        if da[key] != db[key]:
            diff[key] = (da[key], db[key])
    return diff


def profile_summary(profile: HardwareProfile) -> str:
    return (
        f"{profile.tier.value} | {profile.max_memory_mb}MB | "
        f"{profile.quantization.value} | {profile.model_size} | "
        f"{profile.sample_rate}Hz | ring={profile.ring_capacity_samples}"
    )


# ═══════════════════════════════════════════════════════════════════════
# Domain configs + patterns
# ═══════════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class TokenBiasing:
    keywords: Tuple[str, ...]
    boost: float


@dataclass(frozen=True, slots=True)
class DomainConfig:
    name: str
    biasing: TokenBiasing


DOMAIN_CONFIGS: Dict[str, DomainConfig] = {
    "medical": DomainConfig("medical", TokenBiasing(
        ("diagnosis", "treatment", "patient", "symptom", "prescription"), 2.5)),
    "legal": DomainConfig("legal", TokenBiasing(
        ("contract", "agreement", "plaintiff", "defendant", "clause"), 2.5)),
    "financial": DomainConfig("financial", TokenBiasing(
        ("transaction", "payment", "invoice", "ledger", "reconciliation", "compliance"), 2.5)),
    "scientific": DomainConfig("scientific", TokenBiasing(
        ("hypothesis", "experiment", "analysis", "methodology", "empirical"), 2.5)),
    "programming": DomainConfig("programming", TokenBiasing(
        ("function", "class", "return", "async", "await", "import", "export",
         "const", "let", "var", "if", "else", "for", "while", "try", "catch",
         "throw", "new", "this", "null", "undefined"), 2.5)),
    "devops": DomainConfig("devops", TokenBiasing(
        ("kubernetes", "docker", "deployment", "pipeline", "container",
         "namespace", "helm", "terraform"), 2.5)),
    "data_science": DomainConfig("data_science", TokenBiasing(
        ("dataframe", "numpy", "pandas", "regression", "classification",
         "feature", "model", "training"), 2.5)),
    "education": DomainConfig("education", TokenBiasing(
        ("assignment", "lecture", "curriculum", "grade", "student", "syllabus"), 2.5)),
}

DOMAIN_PATTERNS: Dict[str, Tuple[str, ...]] = {
    "python": (r"\.py$", r"def\s+\w+", r"class\s+\w+"),
    "javascript": (r"\.js$", r"const\s+\w+", r"function\s+\w+"),
    "typescript": (r"\.ts$", r"interface\s+\w+"),
    "markdown": (r"\.md$", r"^#\s+.+"),
    "browser": (r"https?://", r"\.com$", r"\.org$"),
    "document": (r"\.docx?$", r"\.pdf$"),
    "notebook": (r"\.ipynb$",),
    "sql": (r"SELECT\s+", r"INSERT\s+", r"UPDATE\s+"),
}

GLOBAL_COMMON_WORDS: FrozenSet[str] = frozenset({
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "must", "can", "i", "you", "he", "she", "it", "we",
    "they", "me", "him", "her", "us", "them", "my", "your", "his", "its", "our",
    "their", "this", "that", "these", "those", "to", "of", "in", "on", "at", "for",
    "with", "by", "from", "as", "into", "about", "like", "not", "no", "yes", "so",
    "if", "then", "than", "there", "here", "they're", "you're", "we're", "it's",
})


__all__ = [
    "Tier", "QuantizationFormat", "QuantSpec", "QUANT_SPEC",
    "FeatureFlags", "SamplingConfig", "VadConfig", "CalibrationConfig",
    "HardwareProfile", "HARDWARE_PROFILES", "MemoryBudget", "MEMORY_BUDGETS",
    "budget_for", "select_profile", "validate_profile", "validate_all_profiles",
    "apply_env_overrides", "profile_to_dict", "profile_diff", "profile_summary",
    "TokenBiasing", "DomainConfig", "DOMAIN_CONFIGS", "DOMAIN_PATTERNS",
    "GLOBAL_COMMON_WORDS",
    "MAX_MODEL_SIZE_MB", "CONFIDENCE_THRESHOLD", "CONTEXTUAL_WINDOW_SIZE",
]