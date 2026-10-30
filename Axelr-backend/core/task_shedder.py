<<<<<<< HEAD
# core/task_shedder.py
"""
AXELR — Dynamic Algorithmic Task-Shedding (v1.1)
=================================================
A zero-IO gatekeeper.  Measures prompt complexity from pure string
signal, classifies the request into one of four tiers, and decides
whether the request earns the multi-agent cognitive track.

Contract
--------
    from core.task_shedder import assess, should_escalate, should_shed
    c = assess(prompt, history=history, files=files)
    if c.is_deep:
        ...                    # unlock multi-agent cognitive pipeline

v1.1 adds two convenience methods on Complexity:
    .to_goat_tier()   → 'shed' | 'lite' | 'standard' | 'deep'
    .budget_hint()    → recommended extra-call budget for the conductor

Design
------
* Pure analysis. No model calls, no Redis, no allocation spikes.
* Deterministic — same prompt always maps to the same tier.
* Cost: O(len(prompt)). ~3–10 µs on a 2 KB prompt.

Tiers (ascending)
-----------------
    trivial   → single-pass, no history compression, no reflection
    standard  → single-pass + compression + reflection
    deep      → multi-agent cognitive pipeline + reflection
    critical  → multi-agent cognitive pipeline (all 3 stages) + reflection

RAM
---
  Constant. No cache. No registry. No module state.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

__all__ = ["Complexity", "assess", "should_escalate", "should_shed"]


_WS_RE = re.compile(r"\s+")

_CODE_HINT_RE = re.compile(
    r"\b(def|class|async|await|import|from|function|const|let|var|return|"
    r"SELECT|INSERT|UPDATE|DELETE|JOIN|WHERE|"
    r"docker|kubectl|nginx|systemd|npm|pip|yarn|poetry|"
    r"traceback|exception|stack trace|segfault|deadlock|race condition)\b",
    re.I,
)
_ERROR_RE = re.compile(
    r"\b(error|exception|traceback|fail(?:ed|ure)?|crash|broken|"
    r"500|502|503|504|timeout|refused|panic|assert(?:ion)?)\b",
    re.I,
)
_MULTI_STEP_RE = re.compile(
    r"(\bthen\b|\bafter that\b|\bnext\b|\bfinally\b|\band then\b|"
    r"\bstep \d|\bphase \d|\bfirst\b[^.]{0,60}\bthen\b)",
    re.I,
)
_DEEP_VERB_RE = re.compile(
    r"\b(architect|design|refactor|optimi[sz]e|migrat|benchmark|"
    r"analy[sz]|evaluate|compare|review|audit|threat[- ]?model|"
    r"root[- ]?cause|distributed|scalab|throughput|latency|"
    r"concurrency|consistency|sharding|partition|replicat)\w*\b",
    re.I,
)
_IMP_VERB_RE = re.compile(
    r"^\s*(build|create|write|make|generate|implement|fix|debug|"
    r"refactor|explain|analy[sz]e|compare|design|draft|review|audit|"
    r"optimi[sz]e|migrate|convert|extract|summari[sz]e)\b",
    re.I | re.MULTILINE,
)
_QUESTION_DEPTH_RE = re.compile(
    r"\b(why|how (?:does|do|would|can|might|should)|"
    r"what (?:are the )?(?:trade[- ]?offs|implications|alternatives|"
    r"advantages|disadvantages|risks|edge cases))\b",
    re.I,
)
_QUANT_RE = re.compile(
    r"\b(\d+(?:\.\d+)?\s*(?:ms|s|m|h|kb|mb|gb|tb|%|x|k|million|billion|"
    r"tokens?|requests?|users?|rps|qps))\b",
    re.I,
)
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)
_NEGATION_FANOUT_RE = re.compile(
    r"\b(?:without|avoid|don't|do not|never|must not|should not|"
    r"refrain|exclude|except|unless|forbid)\b",
    re.I,
)


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
        """Map this shedder tier → goat_layer tier.
        Used by core.quality_conductor."""
        return {"trivial": "shed", "standard": "lite",
                "deep": "standard", "critical": "deep"}.get(self.tier, "shed")

    def budget_hint(self) -> int:
        """Recommended extra-provider-call budget for the conductor."""
        return {"trivial": 0, "standard": 1,
                "deep": 2, "critical": 3}.get(self.tier, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 3),
            "tier": self.tier,
            "reasons": list(self.reasons),
            "words": self.words,
            "has_code": self.has_code,
            "has_error": self.has_error,
            "multi_step": self.multi_step,
            "depth_questions": self.depth_questions,
        }


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
    prompt: str,
    *,
    history: Iterable[dict] | None = None,
    files: Iterable[dict] | None = None,
) -> Complexity:
    raw = prompt or ""
    text = _normalize(raw)
    if not text:
        return Complexity(0.0, "trivial", ("empty",), 0, False, False, False, 0)

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

    return Complexity(
        score=s, tier=tier, reasons=tuple(reasons), words=words,
        has_code=has_code, has_error=has_error,
        multi_step=multi_step, depth_questions=depth_q,
    )


def should_escalate(prompt: str, **kw: Any) -> bool:
    return assess(prompt, **kw).is_deep


def should_shed(prompt: str, **kw: Any) -> bool:
=======
# core/task_shedder.py
"""
AXELR — Dynamic Algorithmic Task-Shedding (v1.1)
=================================================
A zero-IO gatekeeper.  Measures prompt complexity from pure string
signal, classifies the request into one of four tiers, and decides
whether the request earns the multi-agent cognitive track.

Contract
--------
    from core.task_shedder import assess, should_escalate, should_shed
    c = assess(prompt, history=history, files=files)
    if c.is_deep:
        ...                    # unlock multi-agent cognitive pipeline

v1.1 adds two convenience methods on Complexity:
    .to_goat_tier()   → 'shed' | 'lite' | 'standard' | 'deep'
    .budget_hint()    → recommended extra-call budget for the conductor

Design
------
* Pure analysis. No model calls, no Redis, no allocation spikes.
* Deterministic — same prompt always maps to the same tier.
* Cost: O(len(prompt)). ~3–10 µs on a 2 KB prompt.

Tiers (ascending)
-----------------
    trivial   → single-pass, no history compression, no reflection
    standard  → single-pass + compression + reflection
    deep      → multi-agent cognitive pipeline + reflection
    critical  → multi-agent cognitive pipeline (all 3 stages) + reflection

RAM
---
  Constant. No cache. No registry. No module state.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

__all__ = ["Complexity", "assess", "should_escalate", "should_shed"]


_WS_RE = re.compile(r"\s+")

_CODE_HINT_RE = re.compile(
    r"\b(def|class|async|await|import|from|function|const|let|var|return|"
    r"SELECT|INSERT|UPDATE|DELETE|JOIN|WHERE|"
    r"docker|kubectl|nginx|systemd|npm|pip|yarn|poetry|"
    r"traceback|exception|stack trace|segfault|deadlock|race condition)\b",
    re.I,
)
_ERROR_RE = re.compile(
    r"\b(error|exception|traceback|fail(?:ed|ure)?|crash|broken|"
    r"500|502|503|504|timeout|refused|panic|assert(?:ion)?)\b",
    re.I,
)
_MULTI_STEP_RE = re.compile(
    r"(\bthen\b|\bafter that\b|\bnext\b|\bfinally\b|\band then\b|"
    r"\bstep \d|\bphase \d|\bfirst\b[^.]{0,60}\bthen\b)",
    re.I,
)
_DEEP_VERB_RE = re.compile(
    r"\b(architect|design|refactor|optimi[sz]e|migrat|benchmark|"
    r"analy[sz]|evaluate|compare|review|audit|threat[- ]?model|"
    r"root[- ]?cause|distributed|scalab|throughput|latency|"
    r"concurrency|consistency|sharding|partition|replicat)\w*\b",
    re.I,
)
_IMP_VERB_RE = re.compile(
    r"^\s*(build|create|write|make|generate|implement|fix|debug|"
    r"refactor|explain|analy[sz]e|compare|design|draft|review|audit|"
    r"optimi[sz]e|migrate|convert|extract|summari[sz]e)\b",
    re.I | re.MULTILINE,
)
_QUESTION_DEPTH_RE = re.compile(
    r"\b(why|how (?:does|do|would|can|might|should)|"
    r"what (?:are the )?(?:trade[- ]?offs|implications|alternatives|"
    r"advantages|disadvantages|risks|edge cases))\b",
    re.I,
)
_QUANT_RE = re.compile(
    r"\b(\d+(?:\.\d+)?\s*(?:ms|s|m|h|kb|mb|gb|tb|%|x|k|million|billion|"
    r"tokens?|requests?|users?|rps|qps))\b",
    re.I,
)
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)
_NEGATION_FANOUT_RE = re.compile(
    r"\b(?:without|avoid|don't|do not|never|must not|should not|"
    r"refrain|exclude|except|unless|forbid)\b",
    re.I,
)


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
        """Map this shedder tier → goat_layer tier.
        Used by core.quality_conductor."""
        return {"trivial": "shed", "standard": "lite",
                "deep": "standard", "critical": "deep"}.get(self.tier, "shed")

    def budget_hint(self) -> int:
        """Recommended extra-provider-call budget for the conductor."""
        return {"trivial": 0, "standard": 1,
                "deep": 2, "critical": 3}.get(self.tier, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 3),
            "tier": self.tier,
            "reasons": list(self.reasons),
            "words": self.words,
            "has_code": self.has_code,
            "has_error": self.has_error,
            "multi_step": self.multi_step,
            "depth_questions": self.depth_questions,
        }


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
    prompt: str,
    *,
    history: Iterable[dict] | None = None,
    files: Iterable[dict] | None = None,
) -> Complexity:
    raw = prompt or ""
    text = _normalize(raw)
    if not text:
        return Complexity(0.0, "trivial", ("empty",), 0, False, False, False, 0)

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

    return Complexity(
        score=s, tier=tier, reasons=tuple(reasons), words=words,
        has_code=has_code, has_error=has_error,
        multi_step=multi_step, depth_questions=depth_q,
    )


def should_escalate(prompt: str, **kw: Any) -> bool:
    return assess(prompt, **kw).is_deep


def should_shed(prompt: str, **kw: Any) -> bool:
>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
    return assess(prompt, **kw).tier == "trivial"