# core/condenser.py
"""
AXELR — Context Condensation (extractive · anchor-based)
=========================================================
Two extractive compressors sharing the same regex extraction primitives:

    §1  Sentence scoring      condense(text, target_ratio=0.5)
    §2  Anchor extraction     anchors_of(text) → ordered unique anchors
    §3  History compression   compress_history(history, max_tokens=1200)

All pure-stdlib. No LLM. Deterministic. Bounded by input length.
Preserves code fences verbatim.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger("axelr.condenser")

CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Shared regexes
# ═══════════════════════════════════════════════════════════════════════════
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[\"'`])|\n{2,}")
_FENCE = re.compile(r"```[\s\S]*?```", re.DOTALL)
_WS_RUN = re.compile(r"[ \t]+")
_MULTINL = re.compile(r"\n{3,}")
_URL = re.compile(r"https?://[^\s)\]\"'>]+")
_PATH = re.compile(r"(?:\.{0,2}/[\w./-]{2,}|~[/\w.-]{2,})")
_IDENT = re.compile(
    r"\b(?:[a-z]+_[a-z_]+|[a-z]+(?:[A-Z][a-z0-9]+)+|"
    r"[A-Z][a-z]+(?:[A-Z][a-z0-9]+)+|[A-Z][A-Z0-9_]{2,})\b")
_QUANT = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ms|s|m|h|kb|mb|gb|tb|%|x|k|m|b|"
    r"tokens?|bytes?|words?|chars?|rps|qps|req/s|users?)\b", re.I)
_IMPERATIVE = re.compile(
    r"^\s*(?:must|should|never|always|ensure|require|avoid|"
    r"do not|don't|only|exactly|prefer|forbid|ensure that|"
    r"the rule is|constraint:|requirement:)\b[^\n]{0,200}$",
    re.I | re.MULTILINE)
_FILLER = frozenset({
    "please", "kindly", "just", "really", "very", "actually",
    "basically", "simply", "maybe", "perhaps", "sure", "okay",
    "ok", "well", "anyway", "so", "thus", "hence", "therefore",
    "hello", "hi", "hey", "thanks", "thank", "cheers", "great",
    "cool", "nice", "awesome", "got", "it", "you", "i",
})


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Sentence scoring / condense
# ═══════════════════════════════════════════════════════════════════════════
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
        return {"text": self.text, "original_chars": self.original_chars,
                "condensed_chars": self.condensed_chars,
                "sentences_kept": self.sentences_kept,
                "sentences_dropped": self.sentences_dropped,
                "fences_kept": self.fences_kept,
                "ratio": round(self.ratio, 3), "reason": self.reason}


def prune_whitespace(text: str) -> str:
    if not text:
        return ""
    t = _WS_RUN.sub(" ", text)
    t = _MULTINL.sub("\n\n", t)
    return t.strip()


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
    fillers = sum(1 for w in s.lower().split() if w in _FILLER)
    if n:
        score -= 1.5 * (fillers / n)
    return score


def condense(
    text: str, *, target_ratio: float = 0.5,
    keep_head_sentences: int = 2, keep_tail_sentences: int = 1,
) -> CondenseResult:
    """Extractive compression preserving code fences + imperative lines."""
    if not text:
        return CondenseResult("", 0, 0, 0, 0, 0, 1.0, "empty")
    text = prune_whitespace(text)
    orig_len = len(text)

    fences: list[str] = []
    def _stash(m: re.Match) -> str:
        fences.append(m.group(0))
        return f"\x00FENCE{len(fences) - 1}\x00"
    stashed = _FENCE.sub(_stash, text)

    sentences = [s.strip() for s in _SENT_SPLIT.split(stashed) if s.strip()]
    if len(sentences) <= (keep_head_sentences + keep_tail_sentences + 2):
        joined = _join(stashed, fences)
        return CondenseResult(joined, orig_len, len(joined), len(sentences),
                              0, len(fences),
                              len(joined) / orig_len if orig_len else 1.0,
                              "short_input")

    head = sentences[:keep_head_sentences]
    tail = sentences[-keep_tail_sentences:] if keep_tail_sentences else []
    middle = sentences[keep_head_sentences:len(sentences) - keep_tail_sentences]

    scored = sorted(
        ((_sentence_score(s), i, s) for i, s in enumerate(middle)),
        key=lambda x: (-x[0], x[1]))

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
        final, orig_len, len(final), len(kept),
        len(sentences) - len(kept), len(fences),
        len(final) / orig_len if orig_len else 1.0, "extractive_condense")


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


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Anchor extraction (from semantic_anchor.py)
# ═══════════════════════════════════════════════════════════════════════════
def anchors_of(text: str, *, max_anchors: int = 60) -> list[str]:
    """Return ordered, deduplicated anchors from a text blob."""
    if not text:
        return []
    out: list[str] = []
    for m in _IMPERATIVE.finditer(text):
        line = m.group(0).strip()
        if 6 <= len(line) <= 200:
            out.append(line)
        if len(out) >= max_anchors:
            break
    for m in _FENCE.finditer(text):
        block = m.group(0)
        if 10 <= len(block) <= 600:
            out.append(block)
        if len(out) >= max_anchors:
            break
    for rx in (_URL, _PATH, _IDENT):
        for m in rx.finditer(text):
            tok = m.group(0).strip()
            if 2 <= len(tok) <= 80 and tok.lower() not in _FILLER:
                out.append(tok)
        if len(out) >= max_anchors * 2:
            break
    for m in _QUANT.finditer(text):
        out.append(m.group(0))

    seen: set[str] = set()
    uniq: list[str] = []
    for a in out:
        k = a.lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(a)
        if len(uniq) >= max_anchors:
            break
    return uniq


def _densify(text: str, *, cap: int = 280) -> str:
    if not text:
        return ""
    t = _WS_RUN.sub(" ", text)
    t = _MULTINL.sub("\n\n", t)
    words = t.split()
    kept: list[str] = []
    for w in words:
        wl = w.lower().strip(".,;:!?")
        if wl in _FILLER and len(wl) <= 6:
            continue
        kept.append(w)
    t = " ".join(kept) if kept else t
    if len(t) > cap:
        t = t[:cap].rstrip() + "…"
    return t


def _norm_turn(msg: dict) -> dict[str, Any]:
    role = str(msg.get("role") or "user")
    raw = msg.get("text") or msg.get("content") or ""
    if isinstance(raw, list):
        raw = "\n".join(p.get("text", "") for p in raw if isinstance(p, dict))
    return {"role": role, "text": str(raw)}


# ═══════════════════════════════════════════════════════════════════════════
# §4 — History compression (drop-in for context.clip_history)
# ═══════════════════════════════════════════════════════════════════════════
def compress_history(
    history: Iterable[dict] | None, *, max_tokens: int = 1200,
    keep_recent: int = 3, per_turn_cap: int = 280,
    max_anchors: int = 40,
) -> list[dict[str, Any]]:
    """Anchor-based history compression. Same output shape as clip_history."""
    if not history:
        return []
    turns = [_norm_turn(m) for m in history if isinstance(m, dict)]
    if not turns:
        return []

    keep_recent = max(0, int(keep_recent))
    n = len(turns)
    recent_start = max(0, n - keep_recent)
    cold = turns[:recent_start]
    warm = turns[recent_start:]

    anchors: list[str] = []
    seen: set[str] = set()
    for t in reversed(cold):
        for a in anchors_of(t["text"], max_anchors=max_anchors):
            k = a.lower()
            if k in seen:
                continue
            seen.add(k)
            anchors.append(a)
            if len(anchors) >= max_anchors:
                break
        if len(anchors) >= max_anchors:
            break
    anchors.reverse()

    out: list[dict[str, Any]] = []
    if anchors:
        header = (f"[Compressed context — anchors from {len(cold)} earlier turn(s)]")
        out.append({"role": "system",
                    "text": header + "\n" + "\n".join(f"• {a}" for a in anchors)})

    header_cost = estimate_tokens(out[0]["text"]) if out else 0
    budget = max(80, int(max_tokens) - header_cost)

    kept: list[dict[str, Any]] = []
    used = 0
    for t in reversed(warm):
        body = _densify(t["text"], cap=per_turn_cap)
        cost = estimate_tokens(body)
        if kept and used + cost > budget:
            break
        kept.append({"role": t["role"], "text": body})
        used += cost
    kept.reverse()
    out.extend(kept)
    return out


__all__ = [
    "CondenseResult", "condense", "prune_whitespace",
    "anchors_of", "compress_history", "estimate_tokens",
]