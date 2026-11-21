# core/consensus.py
"""
AXELR — Multi-Pass Generation (parallel consensus · sequential cognition)
==========================================================================
Two orthogonal multi-pass strategies:

    §1  Signal similarity          similarity(a, b) → dict
    §2  Parallel consensus         race_consensus(route_fn, ...) → Outcome
    §3  Local delivery polish      local_polish(text) → PolishResult
    §4  Sequential cognition       run_cognitive(route_fn, ...) → dict

All stdlib. No network beyond route_fn. Deterministic.

Dispatch:
    tier=critical + short prompt + non-streaming  → consensus (parallel)
    tier=critical + longer prompt                 → cognitive (sequential)
    tier=deep                                     → cognitive (logic+edit)
    tier=≤standard                                → single call
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

logger = logging.getLogger("axelr.consensus")

RouteFn = Callable[..., Awaitable[dict[str, Any]]]


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Multi-signal similarity
# ═══════════════════════════════════════════════════════════════════════════
_WORD_RE = re.compile(r"\w+")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+\-]*)\n[\s\S]*?```")

_W_JACCARD, _W_TRIGRAM = 0.40, 0.30
_W_NUMERIC, _W_CODE = 0.20, 0.10


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _trigrams(words: list[str]) -> set[tuple[str, str, str]]:
    if len(words) < 3:
        return set()
    return {(words[i], words[i + 1], words[i + 2]) for i in range(len(words) - 2)}


def _numeric_agreement(a: str, b: str) -> float:
    ra, rb = set(_NUM_RE.findall(a or "")), set(_NUM_RE.findall(b or ""))
    if not ra and not rb:
        return 1.0
    if not ra or not rb:
        return 0.0
    return len(ra & rb) / len(ra | rb)


def _code_agreement(a: str, b: str) -> float:
    fa = [x.lower() for x in _FENCE_RE.findall(a or "")]
    fb = [x.lower() for x in _FENCE_RE.findall(b or "")]
    if not fa and not fb:
        return 1.0
    if not fa or not fb:
        return 0.0
    return _jaccard(set(fa), set(fb))


def similarity(a: str, b: str) -> dict[str, float]:
    """Compute multi-signal similarity. Never raises."""
    try:
        if a == b:
            return {"jaccard": 1.0, "trigram": 1.0, "numeric": 1.0, "code": 1.0, "combined": 1.0}
        ta, tb = _tokens(a), _tokens(b)
        j = _jaccard(set(ta), set(tb))
        t = _jaccard(_trigrams(ta), _trigrams(tb))
        n = _numeric_agreement(a, b)
        c = _code_agreement(a, b)
        combined = _W_JACCARD * j + _W_TRIGRAM * t + _W_NUMERIC * n + _W_CODE * c
        return {
            "jaccard": round(j, 4),
            "trigram": round(t, 4),
            "numeric": round(n, 4),
            "code": round(c, 4),
            "combined": round(combined, 4),
        }
    except Exception:
        return {"jaccard": 0.0, "trigram": 0.0, "numeric": 0.0, "code": 0.0, "combined": 0.0}


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Parallel consensus
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class ConsensusOutcome:
    winner: dict[str, Any]
    agreement: float
    aligned: bool
    participants: int
    pairwise: list[dict[str, Any]] = field(default_factory=list)
    errors: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": "consensus",
            "agreement": round(self.agreement, 3),
            "aligned": self.aligned,
            "participants": self.participants,
            "errors": self.errors,
            "pairwise": self.pairwise,
        }


async def race_consensus(
    route_fn: RouteFn,
    *,
    workspace: str,
    task_type: str,
    prompt: str,
    history: Any,
    files: Any,
    max_tokens: int,
    temp: float,
    tier: str,
    user: dict | None,
    context: str,
    request: Any,
    width: int = 2,
    timeout_s: float = 22.0,
    consensus_floor: float = 0.55,
    prefer_non_stream: bool = True,
) -> ConsensusOutcome:
    """Fire N concurrent calls, pick the central answer. Never raises."""
    width = max(2, min(3, int(width)))
    tasks = [
        asyncio.create_task(
            route_fn(workspace, task_type, prompt, history, files, max_tokens, temp, tier, user, context, request),
            name=f"consensus::{i}",
        )
        for i in range(width)
    ]
    done, pending = await asyncio.wait(tasks, timeout=max(4.0, float(timeout_s)))
    for t in pending:
        if not t.done():
            t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    results: list[dict[str, Any]] = []
    errors = 0
    for t in done:
        try:
            r = t.result()
        except Exception:
            errors += 1
            continue
        if isinstance(r, dict) and r.get("success") and (r.get("text") or "").strip():
            results.append(r)
        else:
            errors += 1

    if not results:
        return ConsensusOutcome(
            winner={"success": False, "text": "", "provider": "none", "model_used": ""},
            agreement=0.0,
            aligned=False,
            participants=0,
            errors=errors,
        )
    if len(results) == 1:
        return ConsensusOutcome(winner=results[0], agreement=1.0, aligned=True, participants=1, errors=errors)

    n = len(results)
    avg = [0.0] * n
    counts = [0] * n
    pairwise: list[dict[str, Any]] = []
    for i in range(n):
        for j in range(i + 1, n):
            s = similarity(results[i].get("text", ""), results[j].get("text", ""))
            pairwise.append({"i": i, "j": j, **s})
            avg[i] += s["combined"]
            avg[j] += s["combined"]
            counts[i] += 1
            counts[j] += 1
    for i in range(n):
        if counts[i]:
            avg[i] /= counts[i]

    winner_idx = max(range(n), key=lambda i: (avg[i], len(results[i].get("text", ""))))
    agreement = (sum(avg) / n) if n else 1.0

    return ConsensusOutcome(
        winner=results[winner_idx],
        agreement=agreement,
        aligned=agreement >= consensus_floor,
        participants=n,
        pairwise=pairwise,
        errors=errors,
    )


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Local delivery polish
# ═══════════════════════════════════════════════════════════════════════════
_TERMINAL_CHARS = frozenset(".!?`)\"'”’]*}]>")
_TRAIL_WS_RE = re.compile(r"[ \t]+(\n|$)")
_BLANK_LINES_RE = re.compile(r"\n{4,}")
_OPEN_CLOSE = (("(", ")"), ("[", "]"), ("{", "}"))


@dataclass(slots=True)
class PolishResult:
    text: str
    issues: list[str] = field(default_factory=list)
    changed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"changed": self.changed, "issues": self.issues}


def local_polish(text: str) -> PolishResult:
    """Deterministic local repair. Never calls a model."""
    if not text:
        return PolishResult("", [], False)
    out = text
    issues: list[str] = []

    if out.count("```") % 2 == 1:
        out = out.rstrip() + "\n```"
        issues.append("closed_unbalanced_fence")

    if len(re.findall(r"<thinking>", out, re.I)) > len(re.findall(r"</thinking>", out, re.I)):
        out = out.rstrip() + "\n</thinking>"
        issues.append("closed_thinking_tag")

    tail_fence = out.rfind("```")
    if tail_fence != -1:
        tail = out[tail_fence:]
        for o, c in _OPEN_CLOSE:
            d = tail.count(o) - tail.count(c)
            if d > 0:
                out += c * d
                issues.append(f"balanced_{o}{c}")
                break

    stripped = _TRAIL_WS_RE.sub(r"\1", out)
    if stripped != out:
        out = stripped
        issues.append("stripped_trailing_ws")

    collapsed = _BLANK_LINES_RE.sub("\n\n\n", out)
    if collapsed != out:
        out = collapsed
        issues.append("collapsed_blank_lines")

    s = out.rstrip()
    if len(s) > 300 and s[-1] not in _TERMINAL_CHARS:
        tail = s[-40:]
        if not re.search(r"\b(?:or|and|the|a|of|to|in|for|with|by|,)$", tail, re.I):
            out = s + "."
            issues.append("appended_terminator")

    return PolishResult(out, issues, changed=(out != text))


# ═══════════════════════════════════════════════════════════════════════════
# §4 — Sequential cognition (from cognitive_pipeline.py)
# ═══════════════════════════════════════════════════════════════════════════
_LOGIC_DIRECTIVE = (
    '<cognitive_stage name="logic">\n'
    "Produce ONLY the factual/logical core of the answer.\n"
    "• Bullet list of correct, verifiable claims.\n"
    "• Include every identifier, code block, and numeric fact.\n"
    "• No preamble. No prose polish. No sign-off.\n"
    "</cognitive_stage>"
)

_EDITORIAL_DIRECTIVE = (
    '<cognitive_stage name="editorial">\n'
    "Rewrite the provided draft as the final user-facing answer.\n"
    "• Preserve EVERY identifier, number, and code block.\n"
    "• Remove hedging, filler, and meta-commentary.\n"
    "• Match the user's request shape (code / table / prose).\n"
    "• Add zero new claims — do not invent.\n"
    "</cognitive_stage>"
)

_VERIFY_DIRECTIVE = (
    '<cognitive_stage name="verify">\n'
    "Audit the draft. Fix ONLY these defect classes:\n"
    "• Unclosed code fences, broken brackets, truncation.\n"
    "• Contradictions or self-corrections left in text.\n"
    '• Refusal markers ("I cannot", "As an AI").\n'
    "• Prompt-leak markers (<system_identity>, MASTER_PROMPT).\n"
    "Return the FULL corrected answer.\n"
    "</cognitive_stage>"
)


@dataclass(frozen=True, slots=True)
class Stage:
    name: str
    directive: str
    tokens_frac: float
    min_prior_words: int = 0


_STAGES_STANDARD: tuple[Stage, ...] = (
    Stage("logic", _LOGIC_DIRECTIVE, 0.60),
    Stage("editorial", _EDITORIAL_DIRECTIVE, 0.40, min_prior_words=40),
)
_STAGES_CRITICAL: tuple[Stage, ...] = (
    Stage("logic", _LOGIC_DIRECTIVE, 0.50),
    Stage("editorial", _EDITORIAL_DIRECTIVE, 0.30, min_prior_words=40),
    Stage("verify", _VERIFY_DIRECTIVE, 0.20, min_prior_words=60),
)


def _select_stages(tier: str) -> tuple[Stage, ...]:
    if tier == "critical":
        return _STAGES_CRITICAL
    if tier == "deep":
        return _STAGES_STANDARD
    return ()


def _stage_prompt(stage: Stage, user_prompt: str, prior_text: str) -> str:
    if not prior_text:
        return f"{stage.directive}\n\n<original_request>\n{user_prompt[:2000]}\n</original_request>"
    return (
        f"{stage.directive}\n\n"
        f"<original_request>\n{user_prompt[:2000]}\n</original_request>\n\n"
        f"<prior_draft>\n{prior_text[:6000]}\n</prior_draft>"
    )


async def run_cognitive(
    route_fn: RouteFn,
    *,
    workspace: str,
    task_type: str,
    prompt: str,
    history: Iterable[dict] | None,
    files: Iterable[dict] | None,
    max_tokens: int,
    temp: float,
    tier: str,
    user: dict | None = None,
    context: str = "",
    request: Any = None,
    budget_s: float = 22.0,
    complexity_tier: str | None = None,
) -> dict[str, Any]:
    """Run 2-3 sequential specialist passes. Never raises."""
    stage_tier = complexity_tier or tier
    stages = _select_stages(stage_tier)
    if not stages:
        return await route_fn(
            workspace, task_type, prompt, history, files, max_tokens, temp, tier, user, context, request
        )

    deadline = time.monotonic() + max(4.0, float(budget_s))
    prior_text = ""
    last_result: dict[str, Any] = {}
    stages_run = stages_attempted = stages_skipped = 0

    for stage in stages:
        if time.monotonic() >= deadline - 1.0:
            stages_skipped += 1
            continue
        if stage.min_prior_words and len(prior_text.split()) < stage.min_prior_words:
            stages_skipped += 1
            continue
        stages_attempted += 1
        stage_budget = max(128, int(max_tokens * stage.tokens_frac))
        stage_prompt = _stage_prompt(stage, prompt, prior_text)
        remaining = max(2.0, deadline - time.monotonic())
        try:
            r = await asyncio.wait_for(
                route_fn(
                    workspace, task_type, stage_prompt, None, files, stage_budget, temp, tier, user, context, request
                ),
                timeout=remaining,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.debug("cognitive_stage_timeout stage=%s", stage.name)
            break
        except Exception as e:
            logger.debug("cognitive_stage_failed stage=%s error=%s", stage.name, e)
            continue
        if not isinstance(r, dict) or not r.get("success") or not r.get("text"):
            continue
        prior_text = str(r["text"])
        last_result = r
        stages_run += 1

    if not last_result:
        return await route_fn(
            workspace, task_type, prompt, history, files, max_tokens, temp, tier, user, context, request
        )

    last_result["cognitive"] = {
        "tier": stage_tier,
        "stages_run": stages_run,
        "stages_attempted": stages_attempted,
        "stages_skipped": stages_skipped,
        "stages_total": len(stages),
    }
    return last_result


__all__ = [
    "similarity",
    "ConsensusOutcome",
    "race_consensus",
    "PolishResult",
    "local_polish",
    "Stage",
    "run_cognitive",
]
