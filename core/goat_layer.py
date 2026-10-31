<<<<<<< HEAD
# core/goat_layer.py
"""
AXELR — Absolute GOAT Layer (Cognitive Orchestration)
======================================================
Four-stage stack that forces small/cheap models to emit frontier-tier
content at $0 marginal cost and ~0 RAM overhead. Pure stdlib — no
provider imports, no vector DB, no ML libraries.

    §1  Workload classification   classify_workload(...) -> WorkloadProfile
    §2  Cognitive pipeline        run_cognitive_pipeline(...) -> dict
    §3  Semantic anchor compressor compress_anchors(history) -> list[dict]
    §4  Token-targeted reflection local_repair(...) · llm_reflect(...)

Every stage is deterministic, bounded, and independently testable.
Import has zero side effects. No provider call happens in this module —
the caller supplies a `route_fn`.

Why this file exists
--------------------
Frontier-model quality on a resource-constrained runtime is a *scheduling*
problem, not a *parameter* problem. Spend tokens only where complexity
demands them; strip everything else to the bones. That is the entire
philosophy of this layer.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

logger = logging.getLogger("axelr.goat")

_VERSION = "1.0"

_ENABLE_PIPELINE = os.getenv("AXELR_GOAT_PIPELINE", "true").lower() in ("1", "true", "yes")
_ENABLE_REFLECTION_LLM = os.getenv("AXELR_GOAT_REFLECTION_LLM", "false").lower() in ("1", "true", "yes")
_MAX_PIPELINE_CALLS = max(1, int(os.getenv("AXELR_GOAT_MAX_CALLS", "4")))
_REFLECT_SEVERITY_GATE = float(os.getenv("AXELR_GOAT_REFLECT_GATE", "0.5"))


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Workload classification
# ═══════════════════════════════════════════════════════════════════════════
_COMPLEXITY_SIGNALS: tuple[tuple[re.Pattern[str], int, str], ...] = (
    (re.compile(r"\b(?:compare|versus|vs\.?|trade[- ]?offs?)\b", re.I), 18, "comparison"),
    (re.compile(r"\b(?:architect(?:ure)?|design\s+pattern|blueprint)\b", re.I), 20, "architecture"),
    (re.compile(r"\b(?:multi[- ]?step|pipeline|workflow|orchestrat)\w*\b", re.I), 16, "multistep"),
    (re.compile(r"\b(?:because|therefore|hence|consequently|moreover)\b", re.I), 8, "causal_chain"),
    (re.compile(r"\bwhy\b|\bhow\s+(?:would|should|might|can|do)\b", re.I), 12, "reasoning"),
    (re.compile(r"\b(?:algorithm|complexity|big[- ]?o|optimi[sz]ation)\b", re.I), 18, "algorithmic"),
    (re.compile(r"```"), 14, "code"),
    (re.compile(r"\b(?:debug|traceback|exception|stack\s*trace|regression)\b", re.I), 16, "debug"),
    (re.compile(r"\b(?:analy[sz]e|evaluate|assess|audit|review)\b", re.I), 14, "analysis"),
    (re.compile(r"\b(?:explain|describe|summari[sz]e)\b", re.I), 6, "explain"),
    (re.compile(r"\b(?:extract|parse|scrape|transform)\b", re.I), 8, "extract"),
    (re.compile(r"\b(?:strategy|roadmap|methodolog|framework)\b", re.I), 16, "strategy"),
)

_MULTI_PART_RE = re.compile(r"(?:^|\n)\s*(?:\d+[.)]|[-*•])\s+", re.M)
_CONJ_RE = re.compile(r"\b(?:and|also|plus|then|next|additionally|as\s+well\s+as)\b", re.I)


@dataclass(frozen=True, slots=True)
class WorkloadProfile:
    """Immutable verdict from the complexity gatekeeper."""
    tier: str                       # shed | lite | standard | deep
    score: float                    # 0–100
    signals: tuple[str, ...]
    use_pipeline: bool
    use_reflection: bool
    recommended_rounds: int
    recommended_max_tokens: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier, "score": self.score,
            "signals": list(self.signals),
            "pipeline": self.use_pipeline, "reflection": self.use_reflection,
            "rounds": self.recommended_rounds,
        }


_SHED_PROFILE = WorkloadProfile("shed", 0.0, (), False, False, 1, 1024)


def classify_workload(prompt: str, task_type: str = "", workspace: str = "") -> WorkloadProfile:
    """Deterministic. Same input → same route. Bounded by 12 regex scans."""
    text = (prompt or "").strip()
    if not text:
        return _SHED_PROFILE

    n_words = len(text.split())
    score = 0.0
    signals: list[str] = []

    if n_words >= 150:   score += 35
    elif n_words >= 60:  score += 25
    elif n_words >= 25:  score += 15
    elif n_words >= 8:   score += 6

    parts = len(_MULTI_PART_RE.findall(text))
    if parts >= 2:
        score += min(15, parts * 4)
        signals.append(f"multi_part({parts})")

    qs = text.count("?")
    if qs >= 2:
        score += min(10, qs * 3)
        signals.append(f"questions({qs})")

    conj = len(_CONJ_RE.findall(text))
    if conj >= 2:
        score += min(10, conj * 2)
        signals.append(f"conj({conj})")

    for pat, weight, tag in _COMPLEXITY_SIGNALS:
        if pat.search(text):
            score += weight
            signals.append(tag)

    if task_type in ("frontend", "extraction", "touch_fix"):
        score += 5
    elif task_type == "structuring":
        score += 8

    score = max(0.0, min(100.0, score))

    if score < 20:      tier = "shed"
    elif score < 45:    tier = "lite"
    elif score < 75:    tier = "standard"
    else:               tier = "deep"

    pipeline = _ENABLE_PIPELINE and tier in ("standard", "deep")
    reflection = tier in ("standard", "deep")
    rounds = {"shed": 1, "lite": 1, "standard": 2, "deep": 3}[tier]
    rec_tokens = {"shed": 1024, "lite": 1536, "standard": 2048, "deep": 3072}[tier]

    return WorkloadProfile(
        tier=tier, score=round(score, 1), signals=tuple(signals),
        use_pipeline=pipeline, use_reflection=reflection,
        recommended_rounds=rounds, recommended_max_tokens=rec_tokens,
    )


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Semantic anchor compression (declared before §2 because §2 may use it)
# ═══════════════════════════════════════════════════════════════════════════
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n{2,}")
_FENCE_RE = re.compile(r"```[\s\S]*?```", re.DOTALL)
_ASSIGN_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{0,63}\s*(?:=|:)\s*[^\n,;]{1,120}")
_INSTR_RE = re.compile(
    r"\b(?:must|should|need(?:s|ed)?|require(?:s|d)?|ensure|always|never|"
    r"do\s+not|don'?t|only|exactly|specifically)\b", re.I,
)
_ERROR_RE = re.compile(r"\b(?:error|exception|traceback|failed|warning|timeout|refused)\b", re.I)
_PATH_RE = re.compile(r"\b(?:/[\w./-]+\.\w{1,6}|[A-Za-z]:\\[\w\\.-]+)\b")
_ENTITY_RE = re.compile(r"\b[A-Z][a-zA-Z0-9]{2,}\b")

_ANCHOR_SIGNALS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"```"), 6),
    (_ERROR_RE, 6),
    (_PATH_RE, 5),
    (_INSTR_RE, 4),
    (_ASSIGN_RE, 4),
    (re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|kb|mb|gb|%|px|em|rem)\b", re.I), 3),
    (re.compile(r"\b(?:api|endpoint|schema|route|table|field|column)\b", re.I), 3),
    (_ENTITY_RE, 2),
)

_FILLER_RE = re.compile(
    r"^(?:ok(?:ay)?|sure|cool|thanks?|thank you|hi|hello|hey|"
    r"got it|sounds good|perfect|great|nice|yep|yeah|nope)[\s!.,]*$", re.I,
)


def _normalize_turn(msg: dict) -> dict:
    role = str(msg.get("role") or "user")
    raw = msg.get("text") or msg.get("content") or ""
    if isinstance(raw, list):
        raw = "\n".join(p.get("text", "") for p in raw if isinstance(p, dict))
    return {"role": role, "text": str(raw)}


def _score_sentence(sent: str) -> int:
    s = sent.strip()
    if len(s) < 8 or _FILLER_RE.match(s):
        return 0
    score = 0
    for pat, weight in _ANCHOR_SIGNALS:
        if pat.search(s):
            score += weight
    n = len(s.split())
    if 8 <= n <= 60:
        score += 1
    return score


def _compress_text(text: str, *, max_chars: int = 800) -> str:
    """Anchored reduction: keep code fences verbatim + top-scoring sentences."""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text

    # 1. Code fences preserved first (they carry the most signal density).
    kept_fences: list[str] = []
    fence_budget = max_chars // 2
    used = 0
    for m in _FENCE_RE.finditer(text):
        block = m.group(0)
        if len(block) > 400:
            block = block[:400] + "\n…[truncated]\n```"
        if used + len(block) > fence_budget:
            break
        kept_fences.append(block)
        used += len(block)

    # 2. Score remaining sentences, keep top-scoring in original order.
    remainder = _FENCE_RE.sub(" ", text)
    sentences = [s.strip() for s in _SENT_SPLIT_RE.split(remainder) if s.strip()]
    scored = sorted(
        ((_score_sentence(s), i, s) for i, s in enumerate(sentences)),
        key=lambda x: (-x[0], x[1]),
    )
    text_budget = max_chars - used
    picked: list[tuple[int, str]] = []
    acc = 0
    for sc, idx, s in scored:
        if sc <= 0:
            continue
        if acc + len(s) + 2 > text_budget:
            break
        picked.append((idx, s))
        acc += len(s) + 2
    picked.sort(key=lambda x: x[0])
    body = " ".join(s for _, s in picked)

    parts: list[str] = []
    if body:
        parts.append(body)
    if kept_fences:
        parts.append("\n".join(kept_fences))
    out = "\n\n".join(parts) if parts else text[:max_chars].rstrip() + " …"
    return out[: max_chars + 100]


def compress_anchors(
    history: Iterable[dict] | None, *,
    max_anchors: int = 10, keep_recent: int = 4, max_chars_per_anchor: int = 800,
) -> list[dict]:
    """
    Walk the history. Keep the last `keep_recent` turns verbatim. Compress
    everything before that into high-density anchor summaries.
    Never calls an LLM — this is pure regex scoring.
    """
    if not history:
        return []
    turns = [_normalize_turn(m) for m in history if isinstance(m, dict)]
    if not turns:
        return []

    keep = min(keep_recent, len(turns))
    recent = turns[-keep:]
    older = turns[:-keep] if len(turns) > keep else []

    anchors: list[dict] = []
    for t in older:
        compressed = _compress_text(t["text"], max_chars=max_chars_per_anchor)
        if not compressed:
            continue
        anchors.append({"role": t["role"], "text": compressed, "compressed": True})

    if len(anchors) > max_anchors:
        anchors = anchors[-max_anchors:]

    return anchors + recent


# ═══════════════════════════════════════════════════════════════════════════
# §4 — Token-targeted linear reflection
# ═══════════════════════════════════════════════════════════════════════════
_TERMINAL = frozenset(".!?`)\"'”’]*}]>")
_LONG_DUP_THRESHOLD = 40


@dataclass(slots=True)
class ReflectionIssue:
    kind: str
    detail: str
    severity: float

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail, "severity": self.severity}


@dataclass(slots=True)
class ReflectionResult:
    text: str
    issues: list[ReflectionIssue] = field(default_factory=list)
    local_repaired: bool = False
    llm_repaired: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "local": self.local_repaired,
            "llm": self.llm_repaired,
            "issues": [i.to_dict() for i in self.issues],
        }


def _dup_sentences(text: str) -> int:
    seen: set[str] = set()
    dups = 0
    for s in _SENT_SPLIT_RE.split(text):
        norm = " ".join(s.lower().split())
        if len(norm) < _LONG_DUP_THRESHOLD:
            continue
        if norm in seen:
            dups += 1
        else:
            seen.add(norm)
    return dups


def _dedup_sentences(text: str) -> str:
    seen: set[str] = set()
    kept: list[str] = []
    for s in _SENT_SPLIT_RE.split(text):
        norm = " ".join(s.lower().split())
        if len(norm) >= _LONG_DUP_THRESHOLD and norm in seen:
            continue
        if len(norm) >= _LONG_DUP_THRESHOLD:
            seen.add(norm)
        kept.append(s)
    return " ".join(kept)


def local_repair(text: str) -> ReflectionResult:
    """
    Free, deterministic repairs. Safe to run on any text. Never raises,
    never calls a provider. Bounded by string length only.
    """
    issues: list[ReflectionIssue] = []
    if not text or not text.strip():
        return ReflectionResult(text, issues)

    out = text
    repaired = False

    if out.count("```") % 2 == 1:
        out = out.rstrip() + "\n```"
        issues.append(ReflectionIssue("unclosed_fence", "auto-closed", 0.6))
        repaired = True

    opens = len(re.findall(r"<thinking>", out, re.I))
    closes = len(re.findall(r"</thinking>", out, re.I))
    if opens > closes:
        out = out.rstrip() + "\n</thinking>"
        issues.append(ReflectionIssue("unclosed_thinking", "auto-closed", 0.5))
        repaired = True

    tail_idx = out.rfind("```")
    if tail_idx != -1:
        tail = out[tail_idx:]
        for o, c in (("[", "]"), ("{", "}"), ("(", ")")):
            delta = tail.count(o) - tail.count(c)
            if delta > 0:
                out += c * delta
                issues.append(ReflectionIssue(
                    "unbalanced_bracket", f"appended {delta}×{c}", 0.4))
                repaired = True

    dups = _dup_sentences(out)
    if dups:
        out = _dedup_sentences(out)
        issues.append(ReflectionIssue("duplicate_sentences", f"removed {dups}", 0.35))
        repaired = True

    stripped = out.rstrip()
    if len(stripped) > 220 and stripped[-1] not in _TERMINAL:
        tail = stripped[-40:]
        if not re.search(r"\b(?:or|and|the|a|of|to|in|for|with|by)$", tail, re.I):
            issues.append(ReflectionIssue("possibly_truncated", "no terminator", 0.45))

    return ReflectionResult(out, issues, local_repaired=repaired)


_LLM_REFLECT_PROMPT = """\
<goat_reflect>
The following answer has concrete issues:

{issues}

User request:
{prompt}

Original answer:
\"\"\"
{text}
\"\"\"

Rewrite ONLY to fix the issues above. Preserve all code, markdown, and
factual content. Return only the corrected answer — no preamble, no
explanation, no apology.
</goat_reflect>"""


async def llm_reflect(
    text: str, *,
    issues: list[ReflectionIssue],
    route_fn: Callable[..., Awaitable[dict[str, Any]]],
    workspace: str, task_type: str, prompt: str,
    tier: str, user: dict | None, max_tokens: int = 1024,
) -> tuple[str, bool]:
    """One bounded LLM correction round. Opt-in via AXELR_GOAT_REFLECTION_LLM."""
    if not _ENABLE_REFLECTION_LLM or not text or not issues:
        return text, False
    actionable = [i for i in issues if i.severity >= _REFLECT_SEVERITY_GATE]
    if not actionable:
        return text, False
    issue_lines = "\n".join(f"- {i.kind}: {i.detail}" for i in actionable)
    reflect_prompt = _LLM_REFLECT_PROMPT.format(
        issues=issue_lines, prompt=prompt[:600], text=text[:4000],
    )
    try:
        result = await route_fn(
            workspace=workspace, task_type=task_type,
            prompt=reflect_prompt, history=[], files=[],
            max_tokens=max_tokens, temp=0.2, tier=tier,
            user=user, context="", request=None,
        )
    except Exception as e:
        logger.debug("goat_llm_reflect_failed error=%s", e)
        return text, False
    if not isinstance(result, dict) or not result.get("success"):
        return text, False
    new_text = (result.get("text") or "").strip()
    if not new_text or len(new_text) < len(text) * 0.4:
        return text, False
    return new_text, True


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Multi-agent cognitive pipeline
# ═══════════════════════════════════════════════════════════════════════════
_PLAN_PROMPT = """\
<goat_plan>
Workspace: {workspace}
Task type: {task_type}

User request:
{prompt}

Decompose this into a tight execution plan. Output ONLY:
1. Deliverable shape (bullet list, code file, JSON, markdown).
2. The 3-6 critical steps.
3. Hard constraints the answer must respect.

Numbered list. No prose preamble. No code blocks.
</goat_plan>"""

_GENERATE_PROMPT = """\
<goat_generate>
Execution plan:
{plan}

User request:
{prompt}

Produce the final deliverable following the plan exactly.
</goat_generate>"""

_CRITIC_PROMPT = """\
<goat_critic>
User request:
{prompt}

Answer under review:
{answer}

List ONLY concrete flaws: missing requirements, wrong facts, broken code,
incomplete structure. If the answer is clean, output exactly: CLEAN.
No praise. No summary. Only flaws.
</goat_critic>"""

_POLISH_PROMPT = """\
<goat_polish>
Original answer:
{answer}

Identified flaws:
{critique}

Rewrite the answer to fix each flaw. Preserve everything else. Output
only the corrected answer.
</goat_polish>"""


def _empty_result() -> dict[str, Any]:
    return {"success": False, "text": "", "provider": "none", "model_used": ""}


async def _call(
    route_fn, *, workspace: str, task_type: str, prompt: str,
    history, files, max_tokens: int, temp: float, tier: str,
    user: dict | None, context: str, request: Any,
) -> dict[str, Any]:
    """Call the router. Any failure → empty result, never raises."""
    try:
        r = await route_fn(
            workspace=workspace, task_type=task_type, prompt=prompt,
            history=history, files=files, max_tokens=max_tokens, temp=temp,
            tier=tier, user=user, context=context, request=request,
        )
    except Exception as e:
        logger.warning("goat_call_failed error=%s", e)
        return _empty_result()
    if not isinstance(r, dict):
        return _empty_result()
    return r


def _clean(text: str, cap: int = 6000) -> str:
    return (text or "").strip()[:cap]


async def run_cognitive_pipeline(
    *,
    route_fn: Callable[..., Awaitable[dict[str, Any]]],
    workspace: str, task_type: str, prompt: str,
    history: list[dict] | None, files: list[dict] | None,
    max_tokens: int, temp: float, tier: str,
    user: dict | None, context: str, request: Any,
    profile: WorkloadProfile,
) -> dict[str, Any]:
    """
    Multi-scope pipeline. Bounded by _MAX_PIPELINE_CALLS.
    Falls back to single-shot on ANY failure. Never raises.
    """
    calls = 0
    trace: list[dict[str, Any]] = []
    t0 = time.monotonic()

    # ── Scope 1 — plan (standard + deep) ────────────────────────────────
    plan_text = ""
    plan_tokens = min(512, max(128, max_tokens // 4))
    if profile.tier in ("standard", "deep") and calls < _MAX_PIPELINE_CALLS:
        plan_prompt = _PLAN_PROMPT.format(
            workspace=workspace, task_type=task_type, prompt=prompt[:1500],
        )
        plan_result = await _call(
            route_fn, workspace=workspace, task_type=task_type,
            prompt=plan_prompt, history=None, files=None,
            max_tokens=plan_tokens, temp=0.3, tier=tier, user=None,
            context=context, request=request,
        )
        calls += 1
        if plan_result.get("success"):
            plan_text = _clean(plan_result.get("text", ""), cap=1200)
        trace.append({"scope": "plan", "ok": bool(plan_text)})

    # ── Scope 2 — generate (always) ─────────────────────────────────────
    gen_prompt = (
        _GENERATE_PROMPT.format(plan=plan_text, prompt=prompt)
        if plan_text else prompt
    )
    gen_result = await _call(
        route_fn, workspace=workspace, task_type=task_type,
        prompt=gen_prompt, history=history, files=files,
        max_tokens=max_tokens, temp=temp, tier=tier, user=user,
        context=context, request=request,
    )
    calls += 1
    trace.append({"scope": "generate", "ok": bool(gen_result.get("success"))})
    if not gen_result.get("success") or not gen_result.get("text"):
        return gen_result

    # ── Scope 3 — critique (deep only) ──────────────────────────────────
    critique = ""
    if profile.tier == "deep" and calls < _MAX_PIPELINE_CALLS:
        critic_prompt = _CRITIC_PROMPT.format(
            prompt=prompt[:800],
            answer=_clean(gen_result.get("text", ""), cap=3000),
        )
        critic_result = await _call(
            route_fn, workspace=workspace, task_type=task_type,
            prompt=critic_prompt, history=None, files=None,
            max_tokens=min(384, plan_tokens), temp=0.1, tier=tier,
            user=None, context="", request=None,
        )
        calls += 1
        if critic_result.get("success"):
            critique = _clean(critic_result.get("text", ""), cap=800)
        trace.append({
            "scope": "critique", "ok": bool(critique),
            "clean": critique.strip().upper().startswith("CLEAN"),
        })

    # ── Scope 4 — polish (only when critique is real) ───────────────────
    if (critique and not critique.strip().upper().startswith("CLEAN")
            and calls < _MAX_PIPELINE_CALLS):
        polish_prompt = _POLISH_PROMPT.format(
            answer=_clean(gen_result.get("text", ""), cap=4000),
            critique=critique,
        )
        polish_result = await _call(
            route_fn, workspace=workspace, task_type=task_type,
            prompt=polish_prompt, history=None, files=None,
            max_tokens=max_tokens, temp=0.3, tier=tier, user=user,
            context=context, request=None,
        )
        calls += 1
        trace.append({"scope": "polish", "ok": bool(polish_result.get("success"))})
        if polish_result.get("success") and polish_result.get("text"):
            gen_result = {**gen_result, **polish_result,
                          "text": polish_result["text"]}

    gen_result["goat"] = {
        "tier": profile.tier, "score": profile.score,
        "signals": list(profile.signals),
        "calls": calls, "trace": trace,
        "elapsed_ms": round((time.monotonic() - t0) * 1000, 2),
    }
    return gen_result


# ═══════════════════════════════════════════════════════════════════════════
# Audit
# ═══════════════════════════════════════════════════════════════════════════
def audit() -> dict[str, Any]:
    return {
        "version": _VERSION,
        "pipeline_enabled": _ENABLE_PIPELINE,
        "reflection_llm": _ENABLE_REFLECTION_LLM,
        "max_calls": _MAX_PIPELINE_CALLS,
        "reflect_gate": _REFLECT_SEVERITY_GATE,
    }


__all__ = [
    "WorkloadProfile", "classify_workload",
    "compress_anchors",
    "ReflectionIssue", "ReflectionResult", "local_repair", "llm_reflect",
    "run_cognitive_pipeline",
    "audit",
=======
# core/goat_layer.py
"""
AXELR — Absolute GOAT Layer (Cognitive Orchestration)
======================================================
Four-stage stack that forces small/cheap models to emit frontier-tier
content at $0 marginal cost and ~0 RAM overhead. Pure stdlib — no
provider imports, no vector DB, no ML libraries.

    §1  Workload classification   classify_workload(...) -> WorkloadProfile
    §2  Cognitive pipeline        run_cognitive_pipeline(...) -> dict
    §3  Semantic anchor compressor compress_anchors(history) -> list[dict]
    §4  Token-targeted reflection local_repair(...) · llm_reflect(...)

Every stage is deterministic, bounded, and independently testable.
Import has zero side effects. No provider call happens in this module —
the caller supplies a `route_fn`.

Why this file exists
--------------------
Frontier-model quality on a resource-constrained runtime is a *scheduling*
problem, not a *parameter* problem. Spend tokens only where complexity
demands them; strip everything else to the bones. That is the entire
philosophy of this layer.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

logger = logging.getLogger("axelr.goat")

_VERSION = "1.0"

_ENABLE_PIPELINE = os.getenv("AXELR_GOAT_PIPELINE", "true").lower() in ("1", "true", "yes")
_ENABLE_REFLECTION_LLM = os.getenv("AXELR_GOAT_REFLECTION_LLM", "false").lower() in ("1", "true", "yes")
_MAX_PIPELINE_CALLS = max(1, int(os.getenv("AXELR_GOAT_MAX_CALLS", "4")))
_REFLECT_SEVERITY_GATE = float(os.getenv("AXELR_GOAT_REFLECT_GATE", "0.5"))


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Workload classification
# ═══════════════════════════════════════════════════════════════════════════
_COMPLEXITY_SIGNALS: tuple[tuple[re.Pattern[str], int, str], ...] = (
    (re.compile(r"\b(?:compare|versus|vs\.?|trade[- ]?offs?)\b", re.I), 18, "comparison"),
    (re.compile(r"\b(?:architect(?:ure)?|design\s+pattern|blueprint)\b", re.I), 20, "architecture"),
    (re.compile(r"\b(?:multi[- ]?step|pipeline|workflow|orchestrat)\w*\b", re.I), 16, "multistep"),
    (re.compile(r"\b(?:because|therefore|hence|consequently|moreover)\b", re.I), 8, "causal_chain"),
    (re.compile(r"\bwhy\b|\bhow\s+(?:would|should|might|can|do)\b", re.I), 12, "reasoning"),
    (re.compile(r"\b(?:algorithm|complexity|big[- ]?o|optimi[sz]ation)\b", re.I), 18, "algorithmic"),
    (re.compile(r"```"), 14, "code"),
    (re.compile(r"\b(?:debug|traceback|exception|stack\s*trace|regression)\b", re.I), 16, "debug"),
    (re.compile(r"\b(?:analy[sz]e|evaluate|assess|audit|review)\b", re.I), 14, "analysis"),
    (re.compile(r"\b(?:explain|describe|summari[sz]e)\b", re.I), 6, "explain"),
    (re.compile(r"\b(?:extract|parse|scrape|transform)\b", re.I), 8, "extract"),
    (re.compile(r"\b(?:strategy|roadmap|methodolog|framework)\b", re.I), 16, "strategy"),
)

_MULTI_PART_RE = re.compile(r"(?:^|\n)\s*(?:\d+[.)]|[-*•])\s+", re.M)
_CONJ_RE = re.compile(r"\b(?:and|also|plus|then|next|additionally|as\s+well\s+as)\b", re.I)


@dataclass(frozen=True, slots=True)
class WorkloadProfile:
    """Immutable verdict from the complexity gatekeeper."""
    tier: str                       # shed | lite | standard | deep
    score: float                    # 0–100
    signals: tuple[str, ...]
    use_pipeline: bool
    use_reflection: bool
    recommended_rounds: int
    recommended_max_tokens: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier, "score": self.score,
            "signals": list(self.signals),
            "pipeline": self.use_pipeline, "reflection": self.use_reflection,
            "rounds": self.recommended_rounds,
        }


_SHED_PROFILE = WorkloadProfile("shed", 0.0, (), False, False, 1, 1024)


def classify_workload(prompt: str, task_type: str = "", workspace: str = "") -> WorkloadProfile:
    """Deterministic. Same input → same route. Bounded by 12 regex scans."""
    text = (prompt or "").strip()
    if not text:
        return _SHED_PROFILE

    n_words = len(text.split())
    score = 0.0
    signals: list[str] = []

    if n_words >= 150:   score += 35
    elif n_words >= 60:  score += 25
    elif n_words >= 25:  score += 15
    elif n_words >= 8:   score += 6

    parts = len(_MULTI_PART_RE.findall(text))
    if parts >= 2:
        score += min(15, parts * 4)
        signals.append(f"multi_part({parts})")

    qs = text.count("?")
    if qs >= 2:
        score += min(10, qs * 3)
        signals.append(f"questions({qs})")

    conj = len(_CONJ_RE.findall(text))
    if conj >= 2:
        score += min(10, conj * 2)
        signals.append(f"conj({conj})")

    for pat, weight, tag in _COMPLEXITY_SIGNALS:
        if pat.search(text):
            score += weight
            signals.append(tag)

    if task_type in ("frontend", "extraction", "touch_fix"):
        score += 5
    elif task_type == "structuring":
        score += 8

    score = max(0.0, min(100.0, score))

    if score < 20:      tier = "shed"
    elif score < 45:    tier = "lite"
    elif score < 75:    tier = "standard"
    else:               tier = "deep"

    pipeline = _ENABLE_PIPELINE and tier in ("standard", "deep")
    reflection = tier in ("standard", "deep")
    rounds = {"shed": 1, "lite": 1, "standard": 2, "deep": 3}[tier]
    rec_tokens = {"shed": 1024, "lite": 1536, "standard": 2048, "deep": 3072}[tier]

    return WorkloadProfile(
        tier=tier, score=round(score, 1), signals=tuple(signals),
        use_pipeline=pipeline, use_reflection=reflection,
        recommended_rounds=rounds, recommended_max_tokens=rec_tokens,
    )


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Semantic anchor compression (declared before §2 because §2 may use it)
# ═══════════════════════════════════════════════════════════════════════════
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n{2,}")
_FENCE_RE = re.compile(r"```[\s\S]*?```", re.DOTALL)
_ASSIGN_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{0,63}\s*(?:=|:)\s*[^\n,;]{1,120}")
_INSTR_RE = re.compile(
    r"\b(?:must|should|need(?:s|ed)?|require(?:s|d)?|ensure|always|never|"
    r"do\s+not|don'?t|only|exactly|specifically)\b", re.I,
)
_ERROR_RE = re.compile(r"\b(?:error|exception|traceback|failed|warning|timeout|refused)\b", re.I)
_PATH_RE = re.compile(r"\b(?:/[\w./-]+\.\w{1,6}|[A-Za-z]:\\[\w\\.-]+)\b")
_ENTITY_RE = re.compile(r"\b[A-Z][a-zA-Z0-9]{2,}\b")

_ANCHOR_SIGNALS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"```"), 6),
    (_ERROR_RE, 6),
    (_PATH_RE, 5),
    (_INSTR_RE, 4),
    (_ASSIGN_RE, 4),
    (re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|kb|mb|gb|%|px|em|rem)\b", re.I), 3),
    (re.compile(r"\b(?:api|endpoint|schema|route|table|field|column)\b", re.I), 3),
    (_ENTITY_RE, 2),
)

_FILLER_RE = re.compile(
    r"^(?:ok(?:ay)?|sure|cool|thanks?|thank you|hi|hello|hey|"
    r"got it|sounds good|perfect|great|nice|yep|yeah|nope)[\s!.,]*$", re.I,
)


def _normalize_turn(msg: dict) -> dict:
    role = str(msg.get("role") or "user")
    raw = msg.get("text") or msg.get("content") or ""
    if isinstance(raw, list):
        raw = "\n".join(p.get("text", "") for p in raw if isinstance(p, dict))
    return {"role": role, "text": str(raw)}


def _score_sentence(sent: str) -> int:
    s = sent.strip()
    if len(s) < 8 or _FILLER_RE.match(s):
        return 0
    score = 0
    for pat, weight in _ANCHOR_SIGNALS:
        if pat.search(s):
            score += weight
    n = len(s.split())
    if 8 <= n <= 60:
        score += 1
    return score


def _compress_text(text: str, *, max_chars: int = 800) -> str:
    """Anchored reduction: keep code fences verbatim + top-scoring sentences."""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text

    # 1. Code fences preserved first (they carry the most signal density).
    kept_fences: list[str] = []
    fence_budget = max_chars // 2
    used = 0
    for m in _FENCE_RE.finditer(text):
        block = m.group(0)
        if len(block) > 400:
            block = block[:400] + "\n…[truncated]\n```"
        if used + len(block) > fence_budget:
            break
        kept_fences.append(block)
        used += len(block)

    # 2. Score remaining sentences, keep top-scoring in original order.
    remainder = _FENCE_RE.sub(" ", text)
    sentences = [s.strip() for s in _SENT_SPLIT_RE.split(remainder) if s.strip()]
    scored = sorted(
        ((_score_sentence(s), i, s) for i, s in enumerate(sentences)),
        key=lambda x: (-x[0], x[1]),
    )
    text_budget = max_chars - used
    picked: list[tuple[int, str]] = []
    acc = 0
    for sc, idx, s in scored:
        if sc <= 0:
            continue
        if acc + len(s) + 2 > text_budget:
            break
        picked.append((idx, s))
        acc += len(s) + 2
    picked.sort(key=lambda x: x[0])
    body = " ".join(s for _, s in picked)

    parts: list[str] = []
    if body:
        parts.append(body)
    if kept_fences:
        parts.append("\n".join(kept_fences))
    out = "\n\n".join(parts) if parts else text[:max_chars].rstrip() + " …"
    return out[: max_chars + 100]


def compress_anchors(
    history: Iterable[dict] | None, *,
    max_anchors: int = 10, keep_recent: int = 4, max_chars_per_anchor: int = 800,
) -> list[dict]:
    """
    Walk the history. Keep the last `keep_recent` turns verbatim. Compress
    everything before that into high-density anchor summaries.
    Never calls an LLM — this is pure regex scoring.
    """
    if not history:
        return []
    turns = [_normalize_turn(m) for m in history if isinstance(m, dict)]
    if not turns:
        return []

    keep = min(keep_recent, len(turns))
    recent = turns[-keep:]
    older = turns[:-keep] if len(turns) > keep else []

    anchors: list[dict] = []
    for t in older:
        compressed = _compress_text(t["text"], max_chars=max_chars_per_anchor)
        if not compressed:
            continue
        anchors.append({"role": t["role"], "text": compressed, "compressed": True})

    if len(anchors) > max_anchors:
        anchors = anchors[-max_anchors:]

    return anchors + recent


# ═══════════════════════════════════════════════════════════════════════════
# §4 — Token-targeted linear reflection
# ═══════════════════════════════════════════════════════════════════════════
_TERMINAL = frozenset(".!?`)\"'”’]*}]>")
_LONG_DUP_THRESHOLD = 40


@dataclass(slots=True)
class ReflectionIssue:
    kind: str
    detail: str
    severity: float

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail, "severity": self.severity}


@dataclass(slots=True)
class ReflectionResult:
    text: str
    issues: list[ReflectionIssue] = field(default_factory=list)
    local_repaired: bool = False
    llm_repaired: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "local": self.local_repaired,
            "llm": self.llm_repaired,
            "issues": [i.to_dict() for i in self.issues],
        }


def _dup_sentences(text: str) -> int:
    seen: set[str] = set()
    dups = 0
    for s in _SENT_SPLIT_RE.split(text):
        norm = " ".join(s.lower().split())
        if len(norm) < _LONG_DUP_THRESHOLD:
            continue
        if norm in seen:
            dups += 1
        else:
            seen.add(norm)
    return dups


def _dedup_sentences(text: str) -> str:
    seen: set[str] = set()
    kept: list[str] = []
    for s in _SENT_SPLIT_RE.split(text):
        norm = " ".join(s.lower().split())
        if len(norm) >= _LONG_DUP_THRESHOLD and norm in seen:
            continue
        if len(norm) >= _LONG_DUP_THRESHOLD:
            seen.add(norm)
        kept.append(s)
    return " ".join(kept)


def local_repair(text: str) -> ReflectionResult:
    """
    Free, deterministic repairs. Safe to run on any text. Never raises,
    never calls a provider. Bounded by string length only.
    """
    issues: list[ReflectionIssue] = []
    if not text or not text.strip():
        return ReflectionResult(text, issues)

    out = text
    repaired = False

    if out.count("```") % 2 == 1:
        out = out.rstrip() + "\n```"
        issues.append(ReflectionIssue("unclosed_fence", "auto-closed", 0.6))
        repaired = True

    opens = len(re.findall(r"<thinking>", out, re.I))
    closes = len(re.findall(r"</thinking>", out, re.I))
    if opens > closes:
        out = out.rstrip() + "\n</thinking>"
        issues.append(ReflectionIssue("unclosed_thinking", "auto-closed", 0.5))
        repaired = True

    tail_idx = out.rfind("```")
    if tail_idx != -1:
        tail = out[tail_idx:]
        for o, c in (("[", "]"), ("{", "}"), ("(", ")")):
            delta = tail.count(o) - tail.count(c)
            if delta > 0:
                out += c * delta
                issues.append(ReflectionIssue(
                    "unbalanced_bracket", f"appended {delta}×{c}", 0.4))
                repaired = True

    dups = _dup_sentences(out)
    if dups:
        out = _dedup_sentences(out)
        issues.append(ReflectionIssue("duplicate_sentences", f"removed {dups}", 0.35))
        repaired = True

    stripped = out.rstrip()
    if len(stripped) > 220 and stripped[-1] not in _TERMINAL:
        tail = stripped[-40:]
        if not re.search(r"\b(?:or|and|the|a|of|to|in|for|with|by)$", tail, re.I):
            issues.append(ReflectionIssue("possibly_truncated", "no terminator", 0.45))

    return ReflectionResult(out, issues, local_repaired=repaired)


_LLM_REFLECT_PROMPT = """\
<goat_reflect>
The following answer has concrete issues:

{issues}

User request:
{prompt}

Original answer:
\"\"\"
{text}
\"\"\"

Rewrite ONLY to fix the issues above. Preserve all code, markdown, and
factual content. Return only the corrected answer — no preamble, no
explanation, no apology.
</goat_reflect>"""


async def llm_reflect(
    text: str, *,
    issues: list[ReflectionIssue],
    route_fn: Callable[..., Awaitable[dict[str, Any]]],
    workspace: str, task_type: str, prompt: str,
    tier: str, user: dict | None, max_tokens: int = 1024,
) -> tuple[str, bool]:
    """One bounded LLM correction round. Opt-in via AXELR_GOAT_REFLECTION_LLM."""
    if not _ENABLE_REFLECTION_LLM or not text or not issues:
        return text, False
    actionable = [i for i in issues if i.severity >= _REFLECT_SEVERITY_GATE]
    if not actionable:
        return text, False
    issue_lines = "\n".join(f"- {i.kind}: {i.detail}" for i in actionable)
    reflect_prompt = _LLM_REFLECT_PROMPT.format(
        issues=issue_lines, prompt=prompt[:600], text=text[:4000],
    )
    try:
        result = await route_fn(
            workspace=workspace, task_type=task_type,
            prompt=reflect_prompt, history=[], files=[],
            max_tokens=max_tokens, temp=0.2, tier=tier,
            user=user, context="", request=None,
        )
    except Exception as e:
        logger.debug("goat_llm_reflect_failed error=%s", e)
        return text, False
    if not isinstance(result, dict) or not result.get("success"):
        return text, False
    new_text = (result.get("text") or "").strip()
    if not new_text or len(new_text) < len(text) * 0.4:
        return text, False
    return new_text, True


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Multi-agent cognitive pipeline
# ═══════════════════════════════════════════════════════════════════════════
_PLAN_PROMPT = """\
<goat_plan>
Workspace: {workspace}
Task type: {task_type}

User request:
{prompt}

Decompose this into a tight execution plan. Output ONLY:
1. Deliverable shape (bullet list, code file, JSON, markdown).
2. The 3-6 critical steps.
3. Hard constraints the answer must respect.

Numbered list. No prose preamble. No code blocks.
</goat_plan>"""

_GENERATE_PROMPT = """\
<goat_generate>
Execution plan:
{plan}

User request:
{prompt}

Produce the final deliverable following the plan exactly.
</goat_generate>"""

_CRITIC_PROMPT = """\
<goat_critic>
User request:
{prompt}

Answer under review:
{answer}

List ONLY concrete flaws: missing requirements, wrong facts, broken code,
incomplete structure. If the answer is clean, output exactly: CLEAN.
No praise. No summary. Only flaws.
</goat_critic>"""

_POLISH_PROMPT = """\
<goat_polish>
Original answer:
{answer}

Identified flaws:
{critique}

Rewrite the answer to fix each flaw. Preserve everything else. Output
only the corrected answer.
</goat_polish>"""


def _empty_result() -> dict[str, Any]:
    return {"success": False, "text": "", "provider": "none", "model_used": ""}


async def _call(
    route_fn, *, workspace: str, task_type: str, prompt: str,
    history, files, max_tokens: int, temp: float, tier: str,
    user: dict | None, context: str, request: Any,
) -> dict[str, Any]:
    """Call the router. Any failure → empty result, never raises."""
    try:
        r = await route_fn(
            workspace=workspace, task_type=task_type, prompt=prompt,
            history=history, files=files, max_tokens=max_tokens, temp=temp,
            tier=tier, user=user, context=context, request=request,
        )
    except Exception as e:
        logger.warning("goat_call_failed error=%s", e)
        return _empty_result()
    if not isinstance(r, dict):
        return _empty_result()
    return r


def _clean(text: str, cap: int = 6000) -> str:
    return (text or "").strip()[:cap]


async def run_cognitive_pipeline(
    *,
    route_fn: Callable[..., Awaitable[dict[str, Any]]],
    workspace: str, task_type: str, prompt: str,
    history: list[dict] | None, files: list[dict] | None,
    max_tokens: int, temp: float, tier: str,
    user: dict | None, context: str, request: Any,
    profile: WorkloadProfile,
) -> dict[str, Any]:
    """
    Multi-scope pipeline. Bounded by _MAX_PIPELINE_CALLS.
    Falls back to single-shot on ANY failure. Never raises.
    """
    calls = 0
    trace: list[dict[str, Any]] = []
    t0 = time.monotonic()

    # ── Scope 1 — plan (standard + deep) ────────────────────────────────
    plan_text = ""
    plan_tokens = min(512, max(128, max_tokens // 4))
    if profile.tier in ("standard", "deep") and calls < _MAX_PIPELINE_CALLS:
        plan_prompt = _PLAN_PROMPT.format(
            workspace=workspace, task_type=task_type, prompt=prompt[:1500],
        )
        plan_result = await _call(
            route_fn, workspace=workspace, task_type=task_type,
            prompt=plan_prompt, history=None, files=None,
            max_tokens=plan_tokens, temp=0.3, tier=tier, user=None,
            context=context, request=request,
        )
        calls += 1
        if plan_result.get("success"):
            plan_text = _clean(plan_result.get("text", ""), cap=1200)
        trace.append({"scope": "plan", "ok": bool(plan_text)})

    # ── Scope 2 — generate (always) ─────────────────────────────────────
    gen_prompt = (
        _GENERATE_PROMPT.format(plan=plan_text, prompt=prompt)
        if plan_text else prompt
    )
    gen_result = await _call(
        route_fn, workspace=workspace, task_type=task_type,
        prompt=gen_prompt, history=history, files=files,
        max_tokens=max_tokens, temp=temp, tier=tier, user=user,
        context=context, request=request,
    )
    calls += 1
    trace.append({"scope": "generate", "ok": bool(gen_result.get("success"))})
    if not gen_result.get("success") or not gen_result.get("text"):
        return gen_result

    # ── Scope 3 — critique (deep only) ──────────────────────────────────
    critique = ""
    if profile.tier == "deep" and calls < _MAX_PIPELINE_CALLS:
        critic_prompt = _CRITIC_PROMPT.format(
            prompt=prompt[:800],
            answer=_clean(gen_result.get("text", ""), cap=3000),
        )
        critic_result = await _call(
            route_fn, workspace=workspace, task_type=task_type,
            prompt=critic_prompt, history=None, files=None,
            max_tokens=min(384, plan_tokens), temp=0.1, tier=tier,
            user=None, context="", request=None,
        )
        calls += 1
        if critic_result.get("success"):
            critique = _clean(critic_result.get("text", ""), cap=800)
        trace.append({
            "scope": "critique", "ok": bool(critique),
            "clean": critique.strip().upper().startswith("CLEAN"),
        })

    # ── Scope 4 — polish (only when critique is real) ───────────────────
    if (critique and not critique.strip().upper().startswith("CLEAN")
            and calls < _MAX_PIPELINE_CALLS):
        polish_prompt = _POLISH_PROMPT.format(
            answer=_clean(gen_result.get("text", ""), cap=4000),
            critique=critique,
        )
        polish_result = await _call(
            route_fn, workspace=workspace, task_type=task_type,
            prompt=polish_prompt, history=None, files=None,
            max_tokens=max_tokens, temp=0.3, tier=tier, user=user,
            context=context, request=None,
        )
        calls += 1
        trace.append({"scope": "polish", "ok": bool(polish_result.get("success"))})
        if polish_result.get("success") and polish_result.get("text"):
            gen_result = {**gen_result, **polish_result,
                          "text": polish_result["text"]}

    gen_result["goat"] = {
        "tier": profile.tier, "score": profile.score,
        "signals": list(profile.signals),
        "calls": calls, "trace": trace,
        "elapsed_ms": round((time.monotonic() - t0) * 1000, 2),
    }
    return gen_result


# ═══════════════════════════════════════════════════════════════════════════
# Audit
# ═══════════════════════════════════════════════════════════════════════════
def audit() -> dict[str, Any]:
    return {
        "version": _VERSION,
        "pipeline_enabled": _ENABLE_PIPELINE,
        "reflection_llm": _ENABLE_REFLECTION_LLM,
        "max_calls": _MAX_PIPELINE_CALLS,
        "reflect_gate": _REFLECT_SEVERITY_GATE,
    }


__all__ = [
    "WorkloadProfile", "classify_workload",
    "compress_anchors",
    "ReflectionIssue", "ReflectionResult", "local_repair", "llm_reflect",
    "run_cognitive_pipeline",
    "audit",
>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
]