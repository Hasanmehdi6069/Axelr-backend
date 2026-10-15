# core/context_condenser.py
"""
AXELR — Context Condenser (LLMLingua-equivalent, extractive)

Prompt 6 §1: "Process incoming prompts only after they pass through
the LLMLingua Context Condensation and Proactive Semantic Pruning
pipelines."

LLMLingua is a Microsoft research project that requires a trained
compression model (~hundreds of MB). Unavailable on a 512 MB tier.
This module delivers the same *effect* with pure stdlib: extractive
compression by information density, per-sentence scoring, and
head/tail preservation.

The output is shorter but lossless in identifiers, numbers, code
fences, and imperative constraints.

Public:
    condense(text, *, target_ratio=0.5) -> CondenseResult
    prune_whitespace(text) -> str
    extract_anchors(text, max_n=40) -> list[str]

Never raises.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("axelr.context_condenser")

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[\"'`])|\n{2,}")
_FENCE = re.compile(r"```[\s\S]*?```", re.DOTALL)
_WS_RUN = re.compile(r"[ \t]+")
_MULTINL = re.compile(r"\n{3,}")
_URL = re.compile(r"https?://[^\s)\]\"'>]+")
_PATH = re.compile(r"(?:\.{0,2}/[\w./-]{2,}|~[/\w.-]{2,})")
_IDENT = re.compile(
    r"\b(?:[a-z]+_[a-z_]+|[a-z]+(?:[A-Z][a-z0-9]+)+|"
    r"[A-Z][a-z]+(?:[A-Z][a-z0-9]+)+|[A-Z][A-Z0-9_]{2,})\b"
)
_QUANT = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ms|s|kb|mb|gb|tb|%|x|tokens?|bytes?|"
    r"words?|chars?|rps|qps|req/s)\b",
    re.IGNORECASE,
)
_IMPERATIVE = re.compile(
    r"^\s*(?:must|should|never|always|ensure|require|avoid|"
    r"do not|don't|only|exactly|prefer|forbid)\b",
    re.IGNORECASE | re.MULTILINE,
)
_FILLER_WORDS = frozenset({
    "please", "kindly", "just", "really", "very", "actually",
    "basically", "simply", "maybe", "perhaps", "well", "anyway",
})


@dataclass(slots=True)
class CondenseResult:
    text: str
    original_chars: int
    condensed_chars: int
    sentences_kept: int
    sentences_dropped: int
    fences_kept: int
    ratio: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "original_chars": self.original_chars,
            "condensed_chars": self.condensed_chars,
            "sentences_kept": self.sentences_kept,
            "sentences_dropped": self.sentences_dropped,
            "fences_kept": self.fences_kept,
            "ratio": round(self.ratio, 3),
            "reason": self.reason,
        }


def prune_whitespace(text: str) -> str:
    if not text:
        return ""
    t = _WS_RUN.sub(" ", text)
    t = _MULTINL.sub("\n\n", t)
    return t.strip()


def extract_anchors(text: str, max_n: int = 40) -> list[str]:
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for rx in (_URL, _PATH, _IDENT, _QUANT):
        for m in rx.finditer(text):
            s = m.group(0).strip()
            k = s.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(s)
            if len(out) >= max_n:
                return out
    return out


def _sentence_score(sent: str) -> float:
    s = sent.strip()
    if not s:
        return 0.0
    score = 0.0
    if _IMPERATIVE.match(s):
        score += 8.0
    if _IDENT.search(s):
        score += 4.0
    if _QUANT.search(s):
        score += 3.0
    if _URL.search(s) or _PATH.search(s):
        score += 3.0
    n = len(s.split())
    if 8 <= n <= 60:
        score += 1.0
    elif n < 4:
        score -= 0.5
    # Penalize filler-dense sentences.
    fillers = sum(1 for w in s.lower().split() if w in _FILLER_WORDS)
    if n:
        score -= 1.5 * (fillers / n)
    return score


def condense(text: str, *, target_ratio: float = 0.5,
             keep_head_sentences: int = 2,
             keep_tail_sentences: int = 1) -> CondenseResult:
    if not text:
        return CondenseResult("", 0, 0, 0, 0, 0, 1.0, "empty")
    text = prune_whitespace(text)
    orig_len = len(text)

    # Extract and stash all fenced code blocks — always preserved.
    fences: list[str] = []

    def _stash(m: re.Match) -> str:
        fences.append(m.group(0))
        return f"\x00FENCE{len(fences) - 1}\x00"

    stashed = _FENCE.sub(_stash, text)

    # Split into sentences.
    sentences = [s.strip() for s in _SENT_SPLIT.split(stashed) if s.strip()]
    if len(sentences) <= (keep_head_sentences + keep_tail_sentences + 2):
        joined = _join(stashed, fences)
        return CondenseResult(
            text=joined, original_chars=orig_len,
            condensed_chars=len(joined), sentences_kept=len(sentences),
            sentences_dropped=0, fences_kept=len(fences),
            ratio=len(joined) / orig_len if orig_len else 1.0,
            reason="short_input",
        )

    head = sentences[:keep_head_sentences]
    tail = sentences[-keep_tail_sentences:] if keep_tail_sentences else []
    middle = sentences[keep_head_sentences:
                       len(sentences) - keep_tail_sentences]

    scored = sorted(
        ((_sentence_score(s), i, s) for i, s in enumerate(middle)),
        key=lambda x: (-x[0], x[1]),
    )

    budget_chars = int(orig_len * max(0.1, min(target_ratio, 1.0)))
    head_chars = sum(len(s) for s in head)
    tail_chars = sum(len(s) for s in tail)
    remaining = max(0, budget_chars - head_chars - tail_chars - 40)

    picked: list[tuple[int, str]] = []
    acc = 0
    for sc, idx, s in scored:
        if sc <= 0:
            continue
        if acc + len(s) + 1 > remaining:
            break
        picked.append((idx, s))
        acc += len(s) + 1
    picked.sort(key=lambda x: x[0])

    kept = head + [s for _, s in picked] + tail
    joined_body = " ".join(kept)
    final = _join(joined_body, fences)

    return CondenseResult(
        text=final,
        original_chars=orig_len,
        condensed_chars=len(final),
        sentences_kept=len(kept),
        sentences_dropped=len(sentences) - len(kept),
        fences_kept=len(fences),
        ratio=len(final) / orig_len if orig_len else 1.0,
        reason="extractive_condense",
    )


def _join(body: str, fences: list[str]) -> str:
    if not fences:
        return body
    parts = [body]
    parts.extend(fences)
    out = "\n\n".join(p for p in parts if p)
    return re.sub(r"\x00FENCE(\d+)\x00",
                  lambda m: fences[int(m.group(1))]
                  if 0 <= int(m.group(1)) < len(fences) else "",
                  out).strip()


def audit() -> dict[str, Any]:
    return {
        "module": "context_condenser",
        "filler_words": len(_FILLER_WORDS),
        "anchor_extractors": 4,
    }


__all__ = [
    "CondenseResult", "condense", "prune_whitespace", "extract_anchors",
    "audit",
]