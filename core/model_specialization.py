# core/model_specialization.py
"""
AXELR — Model Specialization Router
=====================================
Routes every task to a *specialist* model, not a generalist.

    §1  Capability taxonomy      TASK_SPECIALISTS  (task → {primary, elite})
    §2  Tier policy              paid tiers get elite variant
    §3  Loaded-model selection   pick the highest-quality loaded model
    §4  Quality tracking         EWMA of pass/fail per (model, task)
    §5  Dispatch + emergency     route() → model_id | None

Routing policy (strict order):
    1. If (task, workspace) has a specialist AND paid tier AND concurrency=1
       AND the elite variant is loaded → return it.
    2. If (task, workspace) has a specialist AND the primary variant is
       loaded → return it.
    3. If any *capability-compatible* model is loaded (e.g., a "reasoner"
       for a math task) → return the highest quality-ranked one.
    4. If NO task-compatible model is loaded BUT thermal budget allows →
       return any loaded model as last-resort (marked emergency).
    5. Otherwise → return None. Caller uses cloud provider.

Never raises. Deterministic. Bounded state.
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger("axelr.model_specialization")

try:
    from prometheus_client import REGISTRY, Counter

    def _metric(name: str, factory):
        try:
            return factory()
        except ValueError:
            return getattr(REGISTRY, "_names_to_collectors", {}).get(name)

    # GAP4 required metrics (BATCH 1)
    _MODEL_QUALITY = _metric("axelr_model_quality_ewma", lambda: Counter(
        "axelr_model_quality_ewma", "EWMA model quality score", ["task", "model"]))
    _SPECIALIZATION_ROUTES = _metric("axelr_specialization_route_total", lambda: Counter(
        "axelr_specialization_route_total", "Specialization route hits/misses", ["task", "tier"]))
    _VERIFIER_PASS_RATE = _metric("axelr_verifier_pass_rate", lambda: Counter(
        "axelr_verifier_pass_rate", "AHE verifier pass rate", ["task", "kind"]))
    _HEDGE_LOCAL_WINS = _metric("axelr_hedge_local_wins_total", lambda: Counter(
        "axelr_hedge_local_wins_total", "Local hedge wins count"))
    _CONDUCTOR_PATHS = _metric("axelr_conductor_path_total", lambda: Counter(
        "axelr_conductor_path_total", "Conductor path usage count", ["path"]))
except Exception as e:
    logger.warning("prometheus_unavailable error=%s", e)
    _MODEL_QUALITY = _SPECIALIZATION_ROUTES = _VERIFIER_PASS_RATE = _HEDGE_LOCAL_WINS = _CONDUCTOR_PATHS = None

__all__ = [
    "Capability",
    "TaskSpecialist",
    "TASK_SPECIALISTS",
    "SPECIALIZATION_VERSION",
    "route",
    "best_for",
    "record_quality",
    "quality_score",
    "snapshot",
    "audit",
]

SPECIALIZATION_VERSION = "1.0"


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Capability taxonomy
# ═══════════════════════════════════════════════════════════════════════════
Capability = str   # "code" | "extraction" | "math" | "reasoning" | "classification" |
                   # "summarization" | "ui" | "general"


@dataclass(frozen=True, slots=True)
class TaskSpecialist:
    """What model should serve this task, at each tier."""
    task: str
    primary: str                    # fast-tier model_id (always preferred for free)
    elite: str | None = None        # elite-tier model_id (preferred for paid)
    capability: Capability = "general"  # what kind of task this is
    fallback_capabilities: tuple[Capability, ...] = ()


# Mapping assumes `twin_roster.ROSTER_BY_ID` IDs. No fabrication — every ID
# below exists in the canonical roster.
TASK_SPECIALISTS: dict[tuple[str, str], TaskSpecialist] = {
    # ── Code ───────────────────────────────────────────────────────────
    ("code_generation", "design"): TaskSpecialist(
        "code_generation", "qwen25-coder", "qwen25-coder-tuned",
        "code", ("reasoning", "general")),
    ("code_generation", "core"): TaskSpecialist(
        "code_generation", "qwen25-coder", "qwen25-coder-tuned",
        "code", ("reasoning", "general")),
    ("code_fix", "design"): TaskSpecialist(
        "code_fix", "qwen25-coder", "qwen25-coder-tuned",
        "code", ("reasoning",)),
    ("code_fix", "core"): TaskSpecialist(
        "code_fix", "qwen25-coder", "qwen25-coder-tuned",
        "code", ("reasoning",)),
    ("touch_fix", "design"): TaskSpecialist(
        "touch_fix", "qwen25-coder", "qwen25-coder-tuned", "code"),
    ("touch_fix", "core"): TaskSpecialist(
        "touch_fix", "qwen25-coder", "qwen25-coder-tuned", "code"),

    # ── Data extraction ────────────────────────────────────────────────
    ("extraction", "data"): TaskSpecialist(
        "extraction", "granite4-350m", "granite4-350m-q5",
        "extraction", ("general",)),
    ("structuring", "data"): TaskSpecialist(
        "structuring", "granite4-350m", "granite4-350m-q5",
        "extraction", ("general",)),
    ("extraction", "core"): TaskSpecialist(
        "extraction", "granite4-350m", "granite4-350m-q5", "extraction"),
    ("structuring", "core"): TaskSpecialist(
        "structuring", "granite4-350m", "granite4-350m-q5", "extraction"),

    # ── Math ───────────────────────────────────────────────────────────
    ("math", "core"): TaskSpecialist(
        "math", "qwen25-math", "qwen25-math-full",
        "math", ("reasoning",)),

    # ── Reasoning ──────────────────────────────────────────────────────
    ("reasoning", "core"): TaskSpecialist(
        "reasoning", "qwen25-05b", "llama32-1b-cap", "reasoning"),
    ("strategy", "core"): TaskSpecialist(
        "strategy", "qwen25-05b", "llama32-1b-cap", "reasoning"),

    # ── Classification / routing ──────────────────────────────────────
    ("classification", "core"): TaskSpecialist(
        "classification", "minimind-100m", None, "classification"),
    ("intent", "core"): TaskSpecialist(
        "intent", "minimind-100m", None, "classification"),

    # ── Summarization ─────────────────────────────────────────────────
    ("summarization", "core"): TaskSpecialist(
        "summarization", "minicpm4-05b", "openelm-450m",
        "summarization", ("general",)),

    # ── UI / frontend ─────────────────────────────────────────────────
    ("frontend", "design"): TaskSpecialist(
        "frontend", "qwen25-coder", "qwen25-coder-tuned",
        "ui", ("code", "reasoning")),

    # ── General fallback ──────────────────────────────────────────────
    ("general", "core"): TaskSpecialist(
        "general", "smollm2-360m", None, "general"),
    ("general", "design"): TaskSpecialist(
        "general", "smollm2-360m", None, "general"),
    ("general", "data"): TaskSpecialist(
        "general", "smollm2-360m", None, "general"),
}

# Every capability that has a "reasoner" role family in the roster.
_REASONER_CAPABLE: frozenset[str] = frozenset({
    "qwen25-05b", "qwen25-math", "qwen25-coder", "qwen25-coder-tuned",
    "llama32-1b-cap", "openelm-450m", "granite4-350m-q5", "qwen25-05b-elite",
    "smollm2-360m", "granite4-350m", "gemma3-270m", "danube2-200m",
})


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Loaded-model probe (queries twin_engine, cached 5 s)
# ═══════════════════════════════════════════════════════════════════════════
_LOADED_CACHE: dict[str, tuple[float, bool]] = {}
_LOADED_TTL_S = 5.0
_LOADED_LOCK = threading.Lock()


def _is_loaded(model_id: str) -> bool:
    """True iff the twin engine has this model resident. Never raises."""
    now = time.monotonic()
    with _LOADED_LOCK:
        cached = _LOADED_CACHE.get(model_id)
        if cached is not None:
            ts, val = cached
            if now - ts < _LOADED_TTL_S:
                return val
    try:
        from .twin_engine import get_hot_swap_engine
        engine = get_hot_swap_engine()
        if engine is None:
            loaded = False
        else:
            snap = engine.snapshot()
            fast = snap.get("fast", {}) or {}
            elite = snap.get("elite", {}) or {}
            loaded = (
                (fast.get("spec") == model_id and fast.get("loaded"))
                or (elite.get("spec") == model_id and elite.get("loaded"))
            )
    except Exception:
        loaded = False
    with _LOADED_LOCK:
        if len(_LOADED_CACHE) >= 128:
            _LOADED_CACHE.pop(next(iter(_LOADED_CACHE)), None)
        _LOADED_CACHE[model_id] = (now, loaded)
    return loaded


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Quality tracking (per-model, per-task EWMA)
# ═══════════════════════════════════════════════════════════════════════════
_PAID_TIERS = frozenset({"pro", "business", "enterprise", "paid", "elite"})
_QUALITY_ALPHA = 0.2
_QUALITY_TABLE: dict[tuple[str, str], float] = {}
_QUALITY_SAMPLES: dict[tuple[str, str], int] = {}
_QUALITY_LOCK = threading.Lock()


def record_quality(model_id: str, task: str, passed: bool) -> None:
    """Feed a verifier verdict into the model's EWMA quality score.
    Elite gate: only call this from a *verifier* (AST/JSON/numeric), never
    from an unverified local heuristic."""
    if not model_id or not task:
        return
    key = (model_id, task)
    with _QUALITY_LOCK:
        prev = _QUALITY_TABLE.get(key, 0.5)
        sample = 1.0 if passed else 0.0
        new_ewma = _QUALITY_ALPHA * sample + (1 - _QUALITY_ALPHA) * prev
        _QUALITY_TABLE[key] = new_ewma
        _QUALITY_SAMPLES[key] = _QUALITY_SAMPLES.get(key, 0) + 1
        # Update Prometheus metric (GAP4)
        if _MODEL_QUALITY is not None:
            with contextlib.suppress(Exception):
                _MODEL_QUALITY.labels(task=task, model=model_id).inc(new_ewma - prev)


def quality_score(model_id: str, task: str) -> float:
    """Return the EWMA for (model, task). Default 0.5 (unknown)."""
    with _QUALITY_LOCK:
        return _QUALITY_TABLE.get((model_id, task), 0.5)


# ═══════════════════════════════════════════════════════════════════════════
# §4 — Routing primitives
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class _RouteDecision:
    model_id: str | None
    reason: str
    emergency: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"model_id": self.model_id, "reason": self.reason,
                "emergency": self.emergency}


def _is_paid(tier: str) -> bool:
    return (tier or "free").lower() in _PAID_TIERS


def _concurrency_allows_elite(concurrency: int) -> bool:
    return concurrency <= 1


def _pick_highest_quality_loaded(
    candidates: list[str], task: str,
) -> str | None:
    loaded = [m for m in candidates if _is_loaded(m)]
    if not loaded:
        return None
    return max(loaded, key=lambda m: (quality_score(m, task), m))


def _fallback_loaded(task: str, capabilities: tuple[Capability, ...]) -> str | None:
    """Find any loaded model whose roster role is capability-compatible."""
    try:
        from .twin_roster import ROSTER_BY_ID
    except Exception:
        return None
    candidates: list[tuple[str, str]] = []  # (model_id, role)
    for spec in ROSTER_BY_ID.values():
        if spec.rejected:
            continue
        role = (spec.role or "").lower()
        # role → capability mapping (loose; roster only has ~6 role strings)
        if "code" in task or "design" in task:
            if role in ("reasoner", "cap") and spec.id in _REASONER_CAPABLE:
                candidates.append((spec.id, role))
        elif task in ("math", "reasoning", "strategy"):
            if role in ("reasoner", "cap") and spec.id in _REASONER_CAPABLE:
                candidates.append((spec.id, role))
        elif task in ("classification", "intent"):
            if role == "router":
                candidates.append((spec.id, role))
        elif task in ("extraction", "structuring"):
            if role in ("parser", "reasoner"):
                candidates.append((spec.id, role))
        else:
            if role in ("reasoner", "anchor", "parser"):
                candidates.append((spec.id, role))
    if not candidates:
        return None
    return max(
        (m for m, _ in candidates),
        key=lambda m: (quality_score(m, task), m),
        default=None,
    )


# ═══════════════════════════════════════════════════════════════════════════
# §5 — Public API
# ═══════════════════════════════════════════════════════════════════════════
def route(
    task: str,
    *, workspace: str = "core", tier: str = "free",
    concurrency: int = 1, thermal_budget: int | None = None,
) -> str | None:
    """
    Return a model_id or ``None`` (caller should fall back to cloud).

    Strict routing order:
        1. Elite specialist (paid + solo + loaded)
        2. Primary specialist (loaded)
        3. Capability-compatible loaded model
        4. Any loaded model if thermal budget > 0 (emergency)
        5. None
    """
    # Update Prometheus route counter (GAP4)
    if _SPECIALIZATION_ROUTES is not None:
        with contextlib.suppress(Exception):
            _SPECIALIZATION_ROUTES.labels(task=task, tier=tier).inc()
    spec = TASK_SPECIALISTS.get((task, workspace))
    if spec is None:
        spec = TASK_SPECIALISTS.get((task, "core"))
    if spec is None:
        # Unknown task → try the general specialist
        spec = TASK_SPECIALISTS.get(("general", "core"))

    # 1. Elite specialist
    if spec is not None and spec.elite and _is_paid(tier) \
       and _concurrency_allows_elite(concurrency) and _is_loaded(spec.elite):
        return spec.elite

    # 2. Primary specialist
    if spec is not None and _is_loaded(spec.primary):
        return spec.primary

    # 3. Capability-compatible loaded model
    if spec is not None:
        fallback = _fallback_loaded(spec.task, spec.fallback_capabilities)
        if fallback is not None:
            return fallback

    # 4. Emergency — any loaded model if thermal budget allows
    if thermal_budget is None:
        try:
            from .guards import budget as _tb
            thermal_budget = _tb()
        except Exception:
            thermal_budget = 4
    if thermal_budget > 0:
        try:
            from .twin_engine import get_hot_swap_engine
            engine = get_hot_swap_engine()
            snap = engine.snapshot() if engine else {}
            for slot in ("fast", "elite"):
                entry = snap.get(slot, {}) or {}
                if entry.get("loaded"):
                    mid = entry.get("spec")
                    if mid:
                        logger.info(
                            "specialization_emergency model=%s task=%s", mid, task)
                        return mid
        except Exception:
            pass

    return None


def best_for(task: str, *, workspace: str = "core") -> str | None:
    """Return the recommended specialist's *primary* model, regardless of load."""
    spec = TASK_SPECIALISTS.get((task, workspace))
    if spec is None:
        spec = TASK_SPECIALISTS.get((task, "core"))
    return spec.primary if spec is not None else None


def snapshot() -> dict[str, Any]:
    """JSON-serialisable view for /health."""
    with _QUALITY_LOCK:
        quality = {
            f"{m}::{t}": round(v, 4)
            for (m, t), v in _QUALITY_TABLE.items() if v != 0.5
        }
        samples = {
            f"{m}::{t}": n for (m, t), n in _QUALITY_SAMPLES.items()
        }
    return {
        "version": SPECIALIZATION_VERSION,
        "task_specialists": {
            f"{t}::{w}": {
                "primary": s.primary,
                "elite": s.elite,
                "capability": s.capability,
            }
            for (t, w), s in TASK_SPECIALISTS.items()
        },
        "quality_scores": quality,
        "quality_samples": samples,
    }


def audit() -> dict[str, Any]:
    return snapshot()


# ═══════════════════════════════════════════════════════════════════════════
# Integration hook for pipeline / twin_engine
# ═══════════════════════════════════════════════════════════════════════════
def resolve_specialized_model(
    task: str, workspace: str, tier: str, concurrency: int = 1,
) -> str | None:
    """
    Convenience wrapper for twin_engine.pick_local_model paths.

    Prefer this over calling `route()` directly when you're inside the twin
    engine — it returns a model_id, matching `ModelSpec.id`.
    """
    return route(task, workspace=workspace, tier=tier,
                 concurrency=concurrency)