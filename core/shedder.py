# core/shedder.py
"""
AXELR — Task Shedder (complexity · tier gate · TTC width)
==========================================================
Pure-stdlib complexity classification. Zero I/O, zero allocation spikes.

    §1  Complexity       assess(prompt, ...) → Complexity
    §2  Budget gate      adaptive_budget(c) — thermal-aware shed decision
    §3  TTC width        decide_width(prompt, ...) — [1, 3] race width

Tiers (ascending):
    trivial   — single pass, no history compression, no reflection
    standard  — single pass + compression + reflection
    deep      — multi-agent cognitive pipeline + reflection
    critical  — multi-agent cognitive pipeline (all 3 stages) + reflection

v2.1 fixes over the pre-consolidation file:
    * ``adaptive_budget`` no longer has ``return True`` followed by dead
      code — the tier-capacity and pressure-based logic is properly wired.
    * Deterministic (same prompt → same tier), byte-identical across runs.
"""
from __future__ import annotations

import logging
import math
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

logger = logging.getLogger("axelr.shedder")

# ── Compile once ──────────────────────────────────────────────────────
_WS_RE = re.compile(r"\s+")
_CODE_HINT_RE = re.compile(
    r"\b(def|class|async|await|import|from|function|const|let|var|return|"
    r"SELECT|INSERT|UPDATE|DELETE|JOIN|WHERE|docker|kubectl|nginx|"
    r"systemd|npm|pip|yarn|poetry|traceback|exception|stack trace|"
    r"segfault|deadlock|race condition)\b", re.I)
_ERROR_RE = re.compile(
    r"\b(error|exception|traceback|fail(?:ed|ure)?|crash|broken|"
    r"500|502|503|504|timeout|refused|panic|assert(?:ion)?)\b", re.I)
_MULTI_STEP_RE = re.compile(
    r"(\bthen\b|\bafter that\b|\bnext\b|\bfinally\b|\band then\b|"
    r"\bstep \d|\bphase \d|\bfirst\b[^.]{0,60}\bthen\b)", re.I)
_DEEP_VERB_RE = re.compile(
    r"\b(architect|design|refactor|optimi[sz]e|migrat|benchmark|"
    r"analy[sz]|evaluate|compare|review|audit|threat[- ]?model|"
    r"root[- ]?cause|distributed|scalab|throughput|latency|"
    r"concurrency|consistency|sharding|partition|replicat)\w*\b", re.I)
_IMP_VERB_RE = re.compile(
    r"^\s*(build|create|write|make|generate|implement|fix|debug|"
    r"refactor|explain|analy[sz]e|compare|design|draft|review|audit|"
    r"optimi[sz]e|migrate|convert|extract|summari[sz]e)\b",
    re.I | re.MULTILINE)
_QUESTION_DEPTH_RE = re.compile(
    r"\b(why|how (?:does|do|would|can|might|should)|"
    r"what (?:are the )?(?:trade[- ]?offs|implications|alternatives|"
    r"advantages|disadvantages|risks|edge cases))\b", re.I)
_QUANT_RE = re.compile(
    r"\b(\d+(?:\.\d+)?\s*(?:ms|s|m|h|kb|mb|gb|tb|%|x|k|million|billion|"
    r"tokens?|requests?|users?|rps|qps))\b", re.I)
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)
_NEGATION_FANOUT_RE = re.compile(
    r"\b(?:without|avoid|don't|do not|never|must not|should not|"
    r"refrain|exclude|except|unless|forbid)\b", re.I)


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Complexity assessment
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class Complexity:
    score: float
    tier: str
    reasons: tuple[str, ...]
    words: int
    has_code: bool
    has_error: bool
    multi_step: bool
    depth_questions: int

    @property
    def is_deep(self) -> bool:
        return self.tier in ("deep", "critical")

    def to_goat_tier(self) -> str:
        """Map shedder tier → goat_layer tier. Consumed by quality_conductor."""
        return {"trivial": "shed", "standard": "lite",
                "deep": "standard", "critical": "deep"}.get(self.tier, "shed")

    def budget_hint(self) -> int:
        """Recommended extra-provider-call budget for the conductor."""
        return {"trivial": 0, "standard": 1,
                "deep": 2, "critical": 3}.get(self.tier, 1)

    def to_dict(self) -> dict[str, Any]:
        return {"score": round(self.score, 3), "tier": self.tier,
                "reasons": list(self.reasons), "words": self.words,
                "has_code": self.has_code, "has_error": self.has_error,
                "multi_step": self.multi_step,
                "depth_questions": self.depth_questions}


def _normalize(text: str, cap: int = 8000) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text[:cap])
    return _WS_RE.sub(" ", t).strip()


def _length_signal(words: int) -> float:
    if words <= 0:
        return 0.0
    return min(1.0, math.log1p(words) / math.log1p(500))


def assess(
    prompt: str, *,
    history: Iterable[dict] | None = None,
    files: Iterable[dict] | None = None,
) -> Complexity:
    raw = prompt or ""
    text = _normalize(raw)
    if not text:
        return Complexity(0.0, "trivial", ("empty",), 0,
                          False, False, False, 0)

    words = len(text.split())
    reasons: list[str] = []

    has_code = bool(_CODE_HINT_RE.search(text))
    has_error = bool(_ERROR_RE.search(text))
    multi_step = bool(_MULTI_STEP_RE.search(text))
    depth_q = len(_QUESTION_DEPTH_RE.findall(text))
    deep_v = len(_DEEP_VERB_RE.findall(text))
    imp_v = len(_IMP_VERB_RE.findall(raw))
    quant = len(_QUANT_RE.findall(text))
    bullets = len(_BULLET_RE.findall(raw))
    negations = len(_NEGATION_FANOUT_RE.findall(text))

    n_files = 0
    if files:
        try:
            n_files = sum(1 for _ in files)
        except Exception:
            n_files = 0

    hist_turns = 0
    if history:
        try:
            hist_turns = sum(1 for m in history if isinstance(m, dict))
        except Exception:
            hist_turns = 0

    s = 0.0
    s += 0.22 * _length_signal(words)
    if has_code:
        s += 0.10; reasons.append("code")
    if has_error:
        s += 0.15; reasons.append("error_signal")
    if multi_step:
        s += 0.12; reasons.append("multi_step")
    if depth_q >= 1:
        s += min(0.15, 0.08 * depth_q); reasons.append(f"depth_q×{depth_q}")
    if deep_v >= 1:
        s += min(0.18, 0.07 * deep_v); reasons.append(f"deep_verb×{deep_v}")
    if imp_v >= 2:
        s += min(0.10, 0.04 * imp_v); reasons.append(f"imperative×{imp_v}")
    if quant >= 2:
        s += min(0.08, 0.03 * quant); reasons.append(f"quant×{quant}")
    if bullets >= 3:
        s += 0.07; reasons.append(f"bullets×{bullets}")
    if negations >= 2:
        s += 0.06; reasons.append(f"negations×{negations}")
    if n_files >= 1:
        s += min(0.12, 0.06 * n_files); reasons.append(f"files×{n_files}")
    if hist_turns >= 8:
        s += 0.05; reasons.append("long_history")

    s = max(0.0, min(1.0, s))

    if s >= 0.85:
        tier = "critical"
    elif s >= 0.55:
        tier = "deep"
    elif s >= 0.25:
        tier = "standard"
    else:
        tier = "trivial"

    return Complexity(score=s, tier=tier, reasons=tuple(reasons),
                      words=words, has_code=has_code, has_error=has_error,
                      multi_step=multi_step, depth_questions=depth_q)


def should_escalate(prompt: str, **kw: Any) -> bool:
    return assess(prompt, **kw).is_deep


def should_shed(prompt: str, **kw: Any) -> bool:
    return assess(prompt, **kw).tier == "trivial"


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Budget gate (thermal-aware)
# ═══════════════════════════════════════════════════════════════════════════
# Per-tier capacity limits prevent low-priority starvation at high load.
_TIER_CAPACITY_LIMITS: dict[str, float] = {
    "critical": 1.0, "deep": 0.7, "standard": 0.4, "trivial": 0.2,
}

# Observability counters — constant small (~50 bytes total).
_req_counters = {"trivial": 0, "standard": 0, "deep": 0, "critical": 0,
                 "total": 0}
_shed_counters = {"trivial": 0, "standard": 0, "deep": 0, "critical": 0,
                  "total": 0}
_last_metrics_ts = time.time()
_METRICS_INTERVAL_S = 60.0


def _thermal_budget() -> int:
    try:
        from .guards import budget as _budget
        return _budget()
    except Exception:
        return 4


def _thermal_pressure() -> str:
    try:
        from .guards import pressure as _pressure
        return _pressure()
    except Exception:
        return "ok"


def adaptive_budget(complexity: Complexity) -> bool:
    """
    Decide if a request has capacity to execute based on:
      1. Current thermal-guard budget (system health)
      2. Request complexity tier (priority)
      3. Per-tier capacity limits (starvation prevention)
      4. Hard circuit-break when thermal state has been critical too long

    Returns True if the request can be processed, False if it should be shed.
    """
    global _last_metrics_ts
    _req_counters[complexity.tier] += 1
    _req_counters["total"] += 1

    current_budget = _thermal_budget()
    current_pressure = _thermal_pressure()

    # Periodic metric log
    now = time.time()
    if now - _last_metrics_ts >= _METRICS_INTERVAL_S:
        shed_rate = (_shed_counters["total"] /
                     max(1, _req_counters["total"])) * 100
        logger.info(
            "shedder_metrics reqs=%d shed=%d rate=%.1f%% pressure=%s budget=%d",
            _req_counters["total"], _shed_counters["total"], shed_rate,
            current_pressure, current_budget)
        for k in _req_counters:
            _req_counters[k] = 0
            _shed_counters[k] = 0
        _last_metrics_ts = now

    # Circuit-break: budget==0 means thermal guard tripped. Only critical
    # requests survive.
    if current_budget == 0:
        if complexity.tier != "critical":
            _shed_counters[complexity.tier] += 1
            _shed_counters["total"] += 1
            return False
        return True

    # Per-tier capacity enforcement.
    tier_max_slots = int(current_budget * _TIER_CAPACITY_LIMITS[complexity.tier])
    current_usage = _req_counters[complexity.tier] - _shed_counters[complexity.tier]
    if current_usage >= tier_max_slots and complexity.tier != "critical":
        _shed_counters[complexity.tier] += 1
        _shed_counters["total"] += 1
        logger.debug(
            "shed_request tier=%s usage=%d tier_max=%d total_budget=%d",
            complexity.tier, current_usage, tier_max_slots, current_budget)
        return False

    # Pressure-based modulation: at "critical" only 1-in-5 standard,
    # 1-in-2 deep get through. "hot" and "warm" progressively relax.
    if current_pressure == "critical":
        if complexity.tier == "trivial":
            return True
        if complexity.tier == "standard":
            allow = (_req_counters["standard"] % 5 == 0)
        elif complexity.tier == "deep":
            allow = (_req_counters["deep"] % 2 == 0)
        else:  # critical
            allow = True
        if not allow:
            _shed_counters[complexity.tier] += 1
            _shed_counters["total"] += 1
        return allow

    if current_pressure == "hot":
        if complexity.tier == "trivial":
            return True
        if complexity.tier == "standard":
            allow = (_req_counters["standard"] % 2 == 0)
        elif complexity.tier == "deep":
            allow = (_req_counters["deep"] % 4 != 3)
        else:
            allow = True
        if not allow:
            _shed_counters[complexity.tier] += 1
            _shed_counters["total"] += 1
        return allow

    if current_pressure == "warm":
        if complexity.tier == "standard":
            allow = (_req_counters["standard"] % 5 != 4)
        else:
            allow = True
        if not allow:
            _shed_counters[complexity.tier] += 1
            _shed_counters["total"] += 1
        return allow

    # "ok" — allow everything
    return True


def shed_stats() -> dict[str, Any]:
    tier_stats: dict[str, dict[str, Any]] = {}
    for tier in ("trivial", "standard", "deep", "critical"):
        reqs = _req_counters[tier]
        sheds = _shed_counters[tier]
        tier_stats[tier] = {
            "requests": reqs, "sheds": sheds,
            "shed_rate": (sheds / max(1, reqs)) * 100 if reqs > 0 else 0.0,
        }
    return {
        "system": {"pressure": _thermal_pressure(),
                   "budget": _thermal_budget(),
                   "timestamp": time.time()},
        "requests": dict(_req_counters),
        "sheds": dict(_shed_counters),
        "tiers": tier_stats,
        "global_shed_rate": ((_shed_counters["total"] /
                              max(1, _req_counters["total"])) * 100
                             if _req_counters["total"] > 0 else 0.0),
    }


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Test-time compute width
# ═══════════════════════════════════════════════════════════════════════════
_TIER_WIDTH = {"trivial": 1, "standard": 1, "deep": 2, "critical": 3}
_MAX_WIDTH = 3
_MIN_WIDTH = 1


def _ttc_enabled() -> bool:
    try:
        from .foundation import load as _load
        return _load().ttc_enabled
    except Exception:
        return False


def decide_width(
    prompt: str, *,
    history: Any = None, files: Any = None, default: int = 1,
) -> int:
    """
    Return the TTC width for this prompt. Bounded [1, 3]. Never raises.
    Width=1 → single call. Width≥2 → race via consensus.race_consensus.
    """
    if not _ttc_enabled():
        return max(_MIN_WIDTH, min(_MAX_WIDTH, int(default)))
    try:
        c = assess(prompt, history=history, files=files)
        w = _TIER_WIDTH.get(c.tier, 1)
        return max(_MIN_WIDTH, min(_MAX_WIDTH, w))
    except Exception:
        return max(_MIN_WIDTH, min(_MAX_WIDTH, int(default)))


# ═══════════════════════════════════════════════════════════════════════════
# Audit
# ═══════════════════════════════════════════════════════════════════════════
def audit() -> dict[str, Any]:
    return {
        "version": "2.1",
        "tier_width_map": dict(_TIER_WIDTH),
        "tier_capacity_limits": dict(_TIER_CAPACITY_LIMITS),
        "ttc_enabled": _ttc_enabled(),
        "shed_stats": shed_stats(),
    }


__all__ = [
    # Complexity
    "Complexity", "assess", "should_escalate", "should_shed",
    # Budget gate
    "adaptive_budget", "shed_stats",
    # TTC
    "decide_width",
    # Audit
    "audit",
]