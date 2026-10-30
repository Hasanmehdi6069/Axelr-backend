# core/ghost_telemetry.py
"""
AXELR — Ghost Telemetry v2.0
=============================
Server-side device-capability negotiation. The client posts a fingerprint
(once per session, in under 2 ms on the JS side); this module returns a
routing decision the client uses to pick a model, size its resident
envelope, and schedule a purge.

Design contract:
    * Deterministic. Same (fingerprint, tier, task_type) -> same decision.
    * Headroom-preserving. The envelope never takes the device below
      ``max(80 MB, 35% of free RAM)`` for OS / browser use.
    * Tier-gated. Free users cap at blitz. Pro at fast. Business at elite.
    * Task-aware. The specialist model is chosen by ``pick_local_model``
      for the actual (workspace, task_type, prompt).
    * Override-aware. Below 70 MB free, the decision requests the user's
      acknowledgement before falling back to a 15 MB micro-volatile mode.
    * Never raises. All coercion is total.

Public:
    DeviceProfile · RoutingDecision
    fingerprint_to_profile(fp) -> DeviceProfile
    match_category(profile, *, tier, task_type, workspace, prompt) -> RoutingDecision
    accept_telemetry(fp, *, session_id, tier, task_type, workspace, prompt) -> RoutingDecision
    snapshot() · audit()
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .twin_roster import (
    BLITZ_MODELS, ELITE_MODELS, FAST_MODELS,
    ROSTER_BY_ID, ModelSpec, pick_local_model,
)

logger = logging.getLogger("axelr.ghost_telemetry")

_VERSION = "2.0"

# ═══════════════════════════════════════════════════════════════════════════
# Tunables — every threshold is an env var, never a magic number
# ═══════════════════════════════════════════════════════════════════════════
_REFUSE_BELOW_MB      = int(os.getenv("AXELR_TELEMETRY_REFUSE_BELOW_MB", "70"))
_BLITZ_BELOW_MB       = int(os.getenv("AXELR_TELEMETRY_BLITZ_BELOW_MB",  "150"))
_FAST_BELOW_MB        = int(os.getenv("AXELR_TELEMETRY_FAST_BELOW_MB",   "300"))
_ELITE_BELOW_MB       = int(os.getenv("AXELR_TELEMETRY_ELITE_BELOW_MB",   "500"))
_MIN_CORES            = int(os.getenv("AXELR_TELEMETRY_MIN_CORES",       "2"))
_LOW_BATTERY_PCT      = int(os.getenv("AXELR_TELEMETRY_LOW_BATTERY_PCT", "15"))
_CRITICAL_THERMALS    = frozenset({"serious", "critical"})

# Envelope headroom: how much RAM stays free for the OS and browser.
_HEADROOM_MIN_MB      = int(os.getenv("AXELR_TELEMETRY_HEADROOM_MIN_MB", "80"))
_HEADROOM_FRACTION    = float(os.getenv("AXELR_TELEMETRY_HEADROOM_FRACTION", "0.35"))

# Category ceilings (envelope size the client may allocate per category).
_CATEGORY_ENVELOPE_MB: dict[str, int] = {
    "elite":  240,
    "fast":   150,
    "blitz":  100,
    "micro":   15,
    "refuse":   0,
}

# Purge policy per category. Everything is immediate except refuse.
_CATEGORY_PURGE_POLICY: dict[str, str] = {
    "elite":  "immediate",
    "fast":   "immediate",
    "blitz":  "immediate",
    "micro":  "immediate",
    "refuse": "no_purge",
}

# Category ordering for min() comparisons.
_CATEGORY_RANK: dict[str, int] = {
    "refuse": 0, "micro": 1, "blitz": 2, "fast": 3, "elite": 4,
}

# Task RAM multiplier — smaller envelopes mean less GC pressure means
# smoother UX. Easy tasks get smaller envelopes than hard ones.
_TASK_RAM_MULTIPLIER: dict[str, float] = {
    "extraction":  0.70,
    "touch_fix":   0.60,
    "structuring": 0.85,
    "frontend":    1.00,
}

# Adjacent task types — used for prewarm hints.
_ADJACENT_TASKS: dict[str, tuple[str, ...]] = {
    "extraction":  ("structuring", "touch_fix"),
    "structuring": ("extraction",  "frontend"),
    "touch_fix":   ("frontend",    "structuring"),
    "frontend":    ("touch_fix",   "structuring"),
}

# Decision cache — small, bounded, TTL'd.
_DECISION_CACHE_MAX   = int(os.getenv("AXELR_TELEMETRY_CACHE_MAX", "512"))
_DECISION_CACHE_TTL_S = float(os.getenv("AXELR_TELEMETRY_CACHE_TTL_S", "60.0"))


# ═══════════════════════════════════════════════════════════════════════════
# Tier policy — one row per tier, no scattering
# ═══════════════════════════════════════════════════════════════════════════
_TIER_POLICY: dict[str, dict[str, Any]] = {
    "guest": {
        "max_category":      "blitz",
        "preferred_category": "blitz",
        "envelope_cap_mb":   110,
        "context_window":    512,
        "deep_think":        False,
        "provider_limit":    5,
    },
    "free": {
        "max_category":      "blitz",
        "preferred_category": "blitz",
        "envelope_cap_mb":   110,
        "context_window":    1024,
        "deep_think":        False,
        "provider_limit":    8,
    },
    "pro": {
        "max_category":      "fast",
        "preferred_category": "fast",
        "envelope_cap_mb":   180,
        "context_window":    2048,
        "deep_think":        False,
        "provider_limit":    15,
    },
    "business": {
        "max_category":      "elite",
        "preferred_category": "fast",
        "envelope_cap_mb":   240,
        "context_window":    4096,
        "deep_think":        True,
        "provider_limit":    40,
    },
    "enterprise": {
        "max_category":      "elite",
        "preferred_category": "elite",
        "envelope_cap_mb":   300,
        "context_window":    8192,
        "deep_think":        True,
        "provider_limit":    40,
    },
}


def _tier_policy(tier: str) -> dict[str, Any]:
    return _TIER_POLICY.get((tier or "free").lower(), _TIER_POLICY["free"])


# ═══════════════════════════════════════════════════════════════════════════
# Device profile
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class DeviceProfile:
    ram_free_mb: int
    ram_total_mb: int
    cores: int
    has_simd: bool
    has_wasm: bool
    has_webgpu: bool
    battery_pct: int | None
    thermal_state: str | None

    @property
    def is_legacy(self) -> bool:
        return self.cores < _MIN_CORES or not self.has_simd

    @property
    def is_constrained(self) -> bool:
        if self.thermal_state in _CRITICAL_THERMALS:
            return True
        if (self.battery_pct is not None
                and self.battery_pct <= _LOW_BATTERY_PCT):
            return True
        return False

    def cache_key(self) -> str:
        ram_bucket = self.ram_free_mb // 25
        payload = (
            f"{ram_bucket}|{self.cores}|{int(self.has_simd)}|"
            f"{int(self.has_wasm)}|{int(self.has_webgpu)}|"
            f"{self.thermal_state or 'na'}|"
            f"{self.battery_pct if self.battery_pct is not None else 'na'}"
        )
        return hashlib.blake2b(payload.encode(), digest_size=8).hexdigest()


def _coerce_int(v: Any, default: int) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return default


def _coerce_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return False


def fingerprint_to_profile(fp: dict[str, Any] | None) -> DeviceProfile:
    fp = fp or {}
    return DeviceProfile(
        ram_free_mb=_coerce_int(fp.get("ram_free_mb"), 0),
        ram_total_mb=_coerce_int(fp.get("ram_total_mb"), 0),
        cores=_coerce_int(fp.get("cores"), 1),
        has_simd=_coerce_bool(fp.get("has_simd")),
        has_wasm=_coerce_bool(fp.get("has_wasm")),
        has_webgpu=_coerce_bool(fp.get("has_webgpu")),
        battery_pct=(
            _coerce_int(fp.get("battery_pct"), -1)
            if fp.get("battery_pct") is not None else None
        ),
        thermal_state=(
            str(fp["thermal_state"]).strip().lower()
            if fp.get("thermal_state") else None
        ),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Category resolution
# ═══════════════════════════════════════════════════════════════════════════
def _device_category(free_ram_mb: int) -> str:
    if free_ram_mb < _REFUSE_BELOW_MB:
        return "refuse"
    if free_ram_mb < _BLITZ_BELOW_MB:
        return "blitz"
    if free_ram_mb < _FAST_BELOW_MB:
        return "fast"
    return "elite"


def _lower(cat_a: str, cat_b: str) -> str:
    """Return whichever category is lower-quality of the two."""
    return cat_a if _CATEGORY_RANK[cat_a] <= _CATEGORY_RANK[cat_b] else cat_b


def _downgrade_one(cat: str) -> str:
    order = ["refuse", "micro", "blitz", "fast", "elite"]
    i = order.index(cat) if cat in order else 0
    return order[max(0, i - 1)]


# ═══════════════════════════════════════════════════════════════════════════
# Envelope computation
# ═══════════════════════════════════════════════════════════════════════════
def _headroom_for(free_ram_mb: int) -> int:
    return max(_HEADROOM_MIN_MB, int(free_ram_mb * _HEADROOM_FRACTION))


def _compute_envelope(free_ram_mb: int, category: str,
                      tier_cap_mb: int,
                      task_type: str) -> int:
    headroom = _headroom_for(free_ram_mb)
    available = max(0, free_ram_mb - headroom)
    category_cap = _CATEGORY_ENVELOPE_MB.get(category, 0)
    task_factor = _TASK_RAM_MULTIPLIER.get(
        (task_type or "").lower(), 1.0)
    base = min(available, category_cap, tier_cap_mb)
    scaled = int(base * task_factor)
    # Floor: micro-volatile's 15 MB; below that the client cannot run.
    if category == "micro":
        return _CATEGORY_ENVELOPE_MB["micro"]
    if category == "refuse":
        return 0
    return max(40, scaled)


# ═══════════════════════════════════════════════════════════════════════════
# Specialist model selection
# ═══════════════════════════════════════════════════════════════════════════
def _pick_models_in_category(category: str, limit: int = 6) -> tuple[str, ...]:
    if category == "elite":
        pool = ELITE_MODELS
    elif category == "fast":
        pool = FAST_MODELS
    elif category == "blitz":
        pool = BLITZ_MODELS
    else:
        return ()
    ordered = [
        m.id for m in ROSTER_BY_ID.values()
        if m.id in pool and not m.rejected
    ]
    return tuple(ordered[:limit])


def _pick_specialist(task_type: str, workspace: str, prompt: str,
                     category: str) -> ModelSpec | None:
    """
    Use the roster's own router for the specialist pick. Only honoured
    when the specialist's category is at or below the effective category
    (otherwise we'd hand the client a model it cannot load).
    """
    try:
        spec = pick_local_model(
            workspace or "core", task_type or "structuring",
            prompt or "", concurrency=1,
        )
    except Exception:
        return None
    if spec is None or spec.rejected:
        return None
    if _CATEGORY_RANK.get(spec.category, 0) > _CATEGORY_RANK.get(category, 0):
        return None
    return spec


# ═══════════════════════════════════════════════════════════════════════════
# Decision
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class RoutingDecision:
    category: str
    memory_envelope_mb: int
    headroom_mb: int
    purge_policy: str
    context_window_hint: int
    cache_prune_aggressive: bool
    suggested_models: tuple[str, ...]
    specialist_model: str | None
    specialist_role: str | None
    deep_think_enabled: bool
    prewarm_hints: tuple[tuple[str, str], ...]
    advisory: str | None
    override_required: bool
    override_seconds: int
    accept_id: str
    device_class: str
    tier_honored: str
    micro_volatile: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "memory_envelope_mb": self.memory_envelope_mb,
            "headroom_mb": self.headroom_mb,
            "purge_policy": self.purge_policy,
            "context_window_hint": self.context_window_hint,
            "cache_prune_aggressive": self.cache_prune_aggressive,
            "suggested_models": list(self.suggested_models),
            "specialist_model": self.specialist_model,
            "specialist_role": self.specialist_role,
            "deep_think_enabled": self.deep_think_enabled,
            "prewarm_hints": [list(t) for t in self.prewarm_hints],
            "advisory": self.advisory,
            "override_required": self.override_required,
            "override_seconds": self.override_seconds,
            "accept_id": self.accept_id,
            "device_class": self.device_class,
            "tier_honored": self.tier_honored,
            "micro_volatile": self.micro_volatile,
        }


def _device_class(category: str, profile: DeviceProfile) -> str:
    if category == "refuse":
        return "critical"
    if category == "micro":
        return "constrained"
    if category == "blitz":
        return "mobile" if profile.ram_total_mb <= 4096 else "standard"
    if category == "fast":
        return "standard"
    return "flagship"


def _prewarm_hints(task_type: str, workspace: str) -> tuple[tuple[str, str], ...]:
    adj = _ADJACENT_TASKS.get((task_type or "").lower(), ())
    return tuple((workspace or "core", t) for t in adj[:2])


def _advisory_for(
    profile: DeviceProfile,
    decision_category: str,
    effective_category: str,
    downgraded: bool,
    tier_name: str,
    envelope_mb: int,
    specialist: ModelSpec | None,
) -> str | None:
    """Calm, informative advisory. Never alarming unless we must refuse."""
    if decision_category == "refuse":
        return (
            f"Device reports {profile.ram_free_mb} MB free — below the "
            f"{_REFUSE_BELOW_MB} MB safety floor. Local inference is paused. "
            f"Tap override to run in a 15 MB compressed envelope, or continue "
            f"with the remote provider pool."
        )
    if decision_category == "micro":
        return (
            f"Running in micro-volatile mode — {_CATEGORY_ENVELOPE_MB['micro']} MB "
            f"envelope. Output quality is reduced to protect your device. "
            f"Free up memory to restore full capability."
        )
    parts: list[str] = []
    if effective_category == "blitz":
        parts.append("Fast mode")
    elif effective_category == "fast":
        parts.append("Balanced mode")
    else:
        parts.append("Elite mode")
    parts.append(f"{envelope_mb} MB envelope")
    if specialist is not None:
        parts.append(f"{specialist.role} specialist")
    if downgraded:
        if profile.is_legacy:
            parts.append("legacy hardware safeguard")
        if profile.is_constrained:
            reasons: list[str] = []
            if profile.thermal_state in _CRITICAL_THERMALS:
                reasons.append("thermal")
            if (profile.battery_pct is not None
                    and profile.battery_pct <= _LOW_BATTERY_PCT):
                reasons.append("battery")
            if reasons:
                parts.append("/".join(reasons) + " safeguard")
    if tier_name in ("free", "guest"):
        parts.append("upgrade for extended capabilities")
    return " · ".join(parts) + "."


# ═══════════════════════════════════════════════════════════════════════════
# Main resolution
# ═══════════════════════════════════════════════════════════════════════════
def match_category(
    profile: DeviceProfile,
    *,
    tier: str = "free",
    task_type: str = "",
    workspace: str = "",
    prompt: str = "",
) -> RoutingDecision:
    tier_name = (tier or "free").lower()
    policy = _tier_policy(tier_name)

    # Device capability from RAM alone.
    device_cat = _device_category(profile.ram_free_mb)

    # Device downgrades from legacy hardware / thermal / battery.
    downgraded = False
    if device_cat != "refuse":
        if profile.is_legacy:
            device_cat = _downgrade_one(device_cat)
            downgraded = True
        if profile.is_constrained and device_cat != "micro":
            device_cat = _downgrade_one(device_cat)
            downgraded = True

    # Tier ceiling.
    tier_max = policy["max_category"]
    effective = _lower(device_cat, tier_max) if device_cat != "refuse" else "refuse"

    # Below the refuse floor: offer micro-volatile, require user override.
    override_required = False
    override_seconds = 0
    micro_volatile = False
    if effective == "refuse":
        # Check whether the device could still run micro-volatile.
        if profile.ram_free_mb >= 30:
            override_required = True
            override_seconds = int(os.getenv("AXELR_OVERRIDE_SECONDS", "5"))
            micro_volatile = True
        # If the device is truly dead (<30 MB), no override is offered.
        else:
            micro_volatile = False
    elif effective == "micro":
        # Rare — only if a tier capped us at micro (never currently).
        micro_volatile = True
        override_required = True
        override_seconds = int(os.getenv("AXELR_OVERRIDE_SECONDS", "5"))

    # Envelope.
    category_for_envelope = effective if effective != "refuse" else (
        "micro" if micro_volatile else "refuse"
    )
    envelope = _compute_envelope(
        profile.ram_free_mb, category_for_envelope,
        policy["envelope_cap_mb"], task_type,
    )
    headroom = _headroom_for(profile.ram_free_mb)

    # Specialist.
    specialist = (
        _pick_specialist(task_type, workspace, prompt, effective)
        if effective not in ("refuse", "micro") else None
    )

    # Suggested models (fallback list for the client).
    suggested = _pick_models_in_category(category_for_envelope)

    # Context window — scale by envelope.
    ctx = int(policy["context_window"])
    if envelope and category_for_envelope in ("blitz", "micro"):
        ctx = min(ctx, 1024)
    if category_for_envelope == "micro":
        ctx = min(ctx, 512)

    # Deep Think — business/enterprise only, and only when the device is
    # good enough to actually benefit (elite category + SIMD).
    deep_think = (
        bool(policy["deep_think"])
        and effective == "elite"
        and profile.has_simd
    )

    # Aggressive KV pruning when the envelope is tight.
    aggressive = category_for_envelope in ("blitz", "micro") or envelope < 80

    device_class = _device_class(category_for_envelope, profile)

    return RoutingDecision(
        category=category_for_envelope,
        memory_envelope_mb=envelope,
        headroom_mb=headroom,
        purge_policy=_CATEGORY_PURGE_POLICY.get(
            category_for_envelope, "immediate"),
        context_window_hint=ctx,
        cache_prune_aggressive=aggressive,
        suggested_models=suggested,
        specialist_model=specialist.id if specialist else None,
        specialist_role=specialist.role if specialist else None,
        deep_think_enabled=deep_think,
        prewarm_hints=_prewarm_hints(task_type, workspace),
        advisory=_advisory_for(
            profile, category_for_envelope, effective,
            downgraded, tier_name, envelope, specialist,
        ),
        override_required=override_required,
        override_seconds=override_seconds,
        accept_id=profile.cache_key(),
        device_class=device_class,
        tier_honored=tier_name,
        micro_volatile=micro_volatile,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Cached entry point
# ═══════════════════════════════════════════════════════════════════════════
_CACHE: "OrderedDict[str, tuple[float, RoutingDecision]]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


def accept_telemetry(
    fp: dict[str, Any] | None,
    *,
    session_id: str = "",
    tier: str = "free",
    task_type: str = "",
    workspace: str = "",
    prompt: str = "",
) -> RoutingDecision:
    """
    Cached, deterministic decision. Cache key includes tier, task_type,
    and workspace so switching scope produces a fresh decision while
    the same fingerprint+scope stays cached.
    """
    profile = fingerprint_to_profile(fp)
    key = (
        f"{session_id}:{tier}:{task_type}:{workspace}:"
        f"{profile.cache_key()}"
    )

    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            ts, decision = hit
            if (time.monotonic() - ts) < _DECISION_CACHE_TTL_S:
                _CACHE.move_to_end(key)
                return decision
            _CACHE.pop(key, None)

    decision = match_category(
        profile, tier=tier, task_type=task_type,
        workspace=workspace, prompt=prompt,
    )

    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), decision)
        _CACHE.move_to_end(key)
        while len(_CACHE) > _DECISION_CACHE_MAX:
            _CACHE.popitem(last=False)

    return decision


# ═══════════════════════════════════════════════════════════════════════════
# Introspection
# ═══════════════════════════════════════════════════════════════════════════
def snapshot() -> dict[str, Any]:
    with _CACHE_LOCK:
        n = len(_CACHE)
    return {
        "version": _VERSION,
        "cache_entries": n,
        "cache_max": _DECISION_CACHE_MAX,
        "cache_ttl_s": _DECISION_CACHE_TTL_S,
        "thresholds": {
            "refuse_below_mb": _REFUSE_BELOW_MB,
            "blitz_below_mb": _BLITZ_BELOW_MB,
            "fast_below_mb": _FAST_BELOW_MB,
            "min_cores": _MIN_CORES,
            "low_battery_pct": _LOW_BATTERY_PCT,
            "headroom_min_mb": _HEADROOM_MIN_MB,
            "headroom_fraction": _HEADROOM_FRACTION,
        },
        "category_envelope_mb": dict(_CATEGORY_ENVELOPE_MB),
        "tiers": sorted(_TIER_POLICY.keys()),
    }


def audit() -> dict[str, Any]:
    return snapshot()


__all__ = [
    "DeviceProfile", "RoutingDecision",
    "fingerprint_to_profile", "match_category", "accept_telemetry",
    "snapshot", "audit",
]