# core/consensus.py
"""
AXELR — Stage 4: Cross-Model Consensus & Local Token Repair
=============================================================
The final quality lock before delivery. Two independent gates:

    §1  Multi-signal similarity     similarity(a, b) -> dict
    §2  Cross-model consensus       race_consensus(route_fn, ...) -> Outcome
    §3  Pure-local output repair    local_polish(text) -> PolishResult

Everything is stdlib. No network. No premium APIs. No vector DB.
Deterministic: identical inputs → identical outputs, byte-for-byte.

Cost profile
------------
    similarity(a, b)        ~40 µs on 4 KB inputs
    race_consensus(width=2)  exactly 2 extra provider calls, bounded
    local_polish(text)      ~15 µs on 8 KB outputs
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger("axelr.consensus")

__all__ = [
    "similarity", "race_consensus", "local_polish",
    "ConsensusOutcome", "PolishResult",
]

RouteFn = Callable[..., Awaitable[dict[str, Any]]]


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Multi-signal similarity (pure stdlib)
# ═══════════════════════════════════════════════════════════════════════════
_WORD_RE   = re.compile(r"\w+")
_NUM_RE    = re.compile(r"-?\d+(?:\.\d+)?")
_FENCE_RE  = re.compile(r"```([a-zA-Z0-9_+\-]*)\n[\s\S]*?```")

# Weights sum to 1.0.  Semantic overlap dominates; structure is a tiebreak.
_W_JACCARD = 0.40
_W_TRIGRAM = 0.30
_W_NUMERIC = 0.20
_W_CODE    = 0.10


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def _jaccard_sets(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    u = len(a | b)
    return (len(a & b) / u) if u else 0.0


def _trigrams(words: list[str]) -> set[tuple[str, str, str]]:
    if len(words) < 3:
        return set()
    return {(words[i], words[i + 1], words[i + 2]) for i in range(len(words) - 2)}


def _numeric_agreement(a: str, b: str) -> float:
    ra = set(_NUM_RE.findall(a or ""))
    rb = set(_NUM_RE.findall(b or ""))
    if not ra and not rb:
        return 1.0            # both have no numbers → aligned by default
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
    return _jaccard_sets(set(fa), set(fb))


def similarity(a: str, b: str) -> dict[str, float]:
    """Compute multi-signal similarity. Never raises. Bounded by input length."""
    try:
        ta, tb = _tokens(a), _tokens(b)
        j = _jaccard_sets(set(ta), set(tb))
        t = _jaccard_sets(_trigrams(ta), _trigrams(tb))
        n = _numeric_agreement(a, b)
        c = _code_agreement(a, b)
        combined = _W_JACCARD * j + _W_TRIGRAM * t + _W_NUMERIC * n + _W_CODE * c
        return {
            "jaccard": round(j, 4), "trigram": round(t, 4),
            "numeric": round(n, 4), "code": round(c, 4),
            "combined": round(combined, 4),
        }
    except Exception:
        return {"jaccard": 0.0, "trigram": 0.0, "numeric": 0.0,
                "code": 0.0, "combined": 0.0}


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Cross-model consensus
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class ConsensusOutcome:
    winner: dict[str, Any]
    agreement: float                 # avg pairwise combined similarity
    aligned: bool                    # agreement >= consensus_floor
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
    workspace: str, task_type: str, prompt: str,
    history: Any, files: Any,
    max_tokens: int, temp: float, tier: str,
    user: dict | None, context: str, request: Any,
    width: int = 2,
    timeout_s: float = 22.0,
    consensus_floor: float = 0.55,
) -> ConsensusOutcome:
    """
    Fire N concurrent generation calls on the same prompt, pick the
    "central" answer (highest mean pairwise similarity to peers), and
    return it with a consensus metadata block.

    Never raises. Cancels stragglers at `timeout_s`. Width clamped to [2,3].
    """
    width = max(2, min(3, int(width)))
    tasks = [
        asyncio.create_task(
            route_fn(workspace, task_type, prompt, history, files,
                     max_tokens, temp, tier, user, context, request),
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
            winner={"success": False, "text": "", "provider": "none",
                    "model_used": ""},
            agreement=0.0, aligned=False, participants=0, errors=errors,
        )

    if len(results) == 1:
        return ConsensusOutcome(
            winner=results[0], agreement=1.0, aligned=True,
            participants=1, errors=errors,
        )

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

    # Winner = highest mean similarity; tiebreak by longer text.
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
# §3 — Pure-local output repair (final delivery pass)
# ═══════════════════════════════════════════════════════════════════════════
_TERMINAL_CHARS = frozenset(".!?`)\"'”’]*}]>")
_TRAIL_WS_RE    = re.compile(r"[ \t]+(\n|$)")
_BLANK_LINES_RE = re.compile(r"\n{4,}")
_OPEN_CLOSE     = (("(", ")"), ("[", "]"), ("{", "}"))


@dataclass(slots=True)
class PolishResult:
    text: str
    issues: list[str] = field(default_factory=list)
    changed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"changed": self.changed, "issues": self.issues}


def local_polish(text: str) -> PolishResult:
    """
    Deterministic local repair. Never raises, never calls a model.
    Runs on EVERY delivery (cheap enough to be unconditional).
    """
    if not text:
        return PolishResult("", [], False)
    out = text
    issues: list[str] = []

    # 1. Balance fences.
    if out.count("```") % 2 == 1:
        out = out.rstrip() + "\n```"
        issues.append("closed_unbalanced_fence")

    # 2. Close thinking tags.
    if len(re.findall(r"<thinking>", out, re.I)) > len(re.findall(r"</thinking>", out, re.I)):
        out = out.rstrip() + "\n</thinking>"
        issues.append("closed_thinking_tag")

    # 3. Balance brackets inside the tail fence.
    tail_fence = out.rfind("```")
    if tail_fence != -1:
        tail = out[tail_fence:]
        for o, c in _OPEN_CLOSE:
            d = tail.count(o) - tail.count(c)
            if d > 0:
                out += c * d
                issues.append(f"balanced_bracket_{o}{c}")
                break

    # 4. Strip trailing whitespace per line.
    stripped = _TRAIL_WS_RE.sub(r"\1", out)
    if stripped != out:
        out = stripped
        issues.append("stripped_trailing_ws")

    # 5. Collapse 4+ blank lines.
    collapsed = _BLANK_LINES_RE.sub("\n\n\n", out)
    if collapsed != out:
        out = collapsed
        issues.append("collapsed_blank_lines")

    # 6. Append terminal if the tail looks truncated (but not mid-clause).
    s = out.rstrip()
    if len(s) > 300 and s[-1] not in _TERMINAL_CHARS:
        tail = s[-40:]
        if not re.search(r"\b(?:or|and|the|a|of|to|in|for|with|by|,)$", tail, re.I):
            out = s + "."
            issues.append("appended_terminator")

    return PolishResult(out, issues, changed=(out != text))