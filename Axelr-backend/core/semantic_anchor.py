<<<<<<< HEAD
# core/semantic_anchor.py
"""
AXELR — Semantic Anchor Compression (Cognitive Stage 3)
========================================================
Strips conversational history down to what the model actually needs:
identifiers, code, explicit constraints, numeric facts, named entities.
Filler prose is discarded. The output is a compact, high-density token
summary that cannot cause context-window confusion.

Contract
--------
    from core.semantic_anchor import compress, anchors_of
    compressed = compress(history, max_tokens=1200)     # drop-in for clip_history
    anchors    = anchors_of("some text")                # low-level extraction

Design
------
* Pure regex + token walk. No model. No embeddings. No vector DB.
* Preserves: snake_case, camelCase, PascalCase, SCREAMING_CASE,
  file paths, URLs, numeric facts with units, imperative constraints
  ("must", "never", "exactly"...), and fenced code (verbatim).
* Output shape matches `core.context.clip_history` → callers swap freely.
* Deterministic. Same history → same summary. Byte-identical.

RAM
---
  Constant. Work buffers are per-call only. Nothing cached.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

__all__ = ["compress", "anchors_of", "estimate_tokens"]


# ── Anchor regexes ───────────────────────────────────────────────────────
_URL_RE = re.compile(r"https?://[^\s)\]\"'>]+")
_PATH_RE = re.compile(r"(?:\.{0,2}/[\w./-]{2,}|~[/\w.-]{2,})")
_IDENT_RE = re.compile(
    r"\b(?:"
    r"[a-z]+_[a-z_]+"              # snake_case
    r"|[a-z]+(?:[A-Z][a-z0-9]+)+"  # camelCase
    r"|[A-Z][a-z]+(?:[A-Z][a-z0-9]+)+"  # PascalCase
    r"|[A-Z][A-Z0-9_]{2,}"         # SCREAMING_CASE
    r")\b"
)
_QUANT_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ms|s|m|h|d|kb|mb|gb|tb|%|x|k|m|b|"
    r"tokens?|bytes?|words?|chars?|rps|qps|req/s|users?)\b",
    re.I,
)
_INSTR_RE = re.compile(
    r"^\s*(?:must|should|never|always|ensure|require|avoid|"
    r"do not|don't|only|exactly|prefer|forbid|ensure that|"
    r"the rule is|constraint:|requirement:)\b[^\n]{0,200}$",
    re.I | re.MULTILINE,
)
_FENCE_RE = re.compile(r"```[\w+-]*\n[\s\S]*?```")
_FENCE_PLACEHOLDER_RE = re.compile(
    r"```[\w+-]*\n[\s\S]{0,400}?```"
)
_WS_RE = re.compile(r"[ \t]+")
_MULTINL_RE = re.compile(r"\n{3,}")

_FILLER = frozenset({
    "please", "kindly", "just", "really", "very", "actually",
    "basically", "simply", "maybe", "perhaps", "sure", "okay",
    "ok", "well", "anyway", "so", "thus", "hence", "therefore",
    "hello", "hi", "hey", "thanks", "thank", "cheers", "great",
    "cool", "nice", "awesome", "got", "it", "you", "i",
})

CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


# ── Low-level extraction ─────────────────────────────────────────────────
def anchors_of(text: str, *, max_anchors: int = 60) -> list[str]:
    """Return ordered, deduplicated anchors from a text blob."""
    if not text:
        return []
    out: list[str] = []

    # 1. Explicit constraints first — highest signal.
    for m in _INSTR_RE.finditer(text):
        line = m.group(0).strip()
        if 6 <= len(line) <= 200:
            out.append(line)
        if len(out) >= max_anchors:
            break

    # 2. Code blocks — preserve verbatim (already dense).
    for m in _FENCE_RE.finditer(text):
        block = m.group(0)
        if 10 <= len(block) <= 600:
            out.append(block)
        if len(out) >= max_anchors:
            break

    # 3. URLs, paths, identifiers.
    for rx in (_URL_RE, _PATH_RE, _IDENT_RE):
        for m in rx.finditer(text):
            tok = m.group(0).strip()
            if 2 <= len(tok) <= 80 and tok.lower() not in _FILLER:
                out.append(tok)
        if len(out) >= max_anchors * 2:
            break

    # 4. Quantities.
    for m in _QUANT_RE.finditer(text):
        out.append(m.group(0))

    # Deduplicate, preserve order.
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


# ── Turn densifier ───────────────────────────────────────────────────────
def _densify(text: str, *, cap: int = 280) -> str:
    if not text:
        return ""
    t = _WS_RE.sub(" ", text)
    t = _MULTINL_RE.sub("\n\n", t)
    # Keep instruction lines + code fences; drop fillers from prose.
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
        raw = "\n".join(
            p.get("text", "") for p in raw if isinstance(p, dict)
        )
    return {"role": role, "text": str(raw)}


# ── Public: compress ─────────────────────────────────────────────────────
def compress(
    history: Iterable[dict] | None,
    *,
    max_tokens: int = 1200,
    keep_recent: int = 3,
    per_turn_cap: int = 280,
    max_anchors: int = 40,
) -> list[dict[str, Any]]:
    """
    Anchor-based history compression. Drop-in replacement for
    `core.context.clip_history` — same output shape, dense content.
    """
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

    # Extract anchors from the cold tail.
    anchors: list[str] = []
    seen: set[str] = set()
    for t in reversed(cold):                       # newest cold first
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
    anchors.reverse()                              # chronological order

    out: list[dict[str, Any]] = []
    if anchors:
        header = (
            f"[Compressed context — anchors from {len(cold)} earlier turn(s)]"
        )
        out.append({
            "role": "system",
            "text": header + "\n" + "\n".join(f"• {a}" for a in anchors),
        })

    # Fit warm turns newest-first.
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
=======
# core/semantic_anchor.py
"""
AXELR — Semantic Anchor Compression (Cognitive Stage 3)
========================================================
Strips conversational history down to what the model actually needs:
identifiers, code, explicit constraints, numeric facts, named entities.
Filler prose is discarded. The output is a compact, high-density token
summary that cannot cause context-window confusion.

Contract
--------
    from core.semantic_anchor import compress, anchors_of
    compressed = compress(history, max_tokens=1200)     # drop-in for clip_history
    anchors    = anchors_of("some text")                # low-level extraction

Design
------
* Pure regex + token walk. No model. No embeddings. No vector DB.
* Preserves: snake_case, camelCase, PascalCase, SCREAMING_CASE,
  file paths, URLs, numeric facts with units, imperative constraints
  ("must", "never", "exactly"...), and fenced code (verbatim).
* Output shape matches `core.context.clip_history` → callers swap freely.
* Deterministic. Same history → same summary. Byte-identical.

RAM
---
  Constant. Work buffers are per-call only. Nothing cached.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

__all__ = ["compress", "anchors_of", "estimate_tokens"]


# ── Anchor regexes ───────────────────────────────────────────────────────
_URL_RE = re.compile(r"https?://[^\s)\]\"'>]+")
_PATH_RE = re.compile(r"(?:\.{0,2}/[\w./-]{2,}|~[/\w.-]{2,})")
_IDENT_RE = re.compile(
    r"\b(?:"
    r"[a-z]+_[a-z_]+"              # snake_case
    r"|[a-z]+(?:[A-Z][a-z0-9]+)+"  # camelCase
    r"|[A-Z][a-z]+(?:[A-Z][a-z0-9]+)+"  # PascalCase
    r"|[A-Z][A-Z0-9_]{2,}"         # SCREAMING_CASE
    r")\b"
)
_QUANT_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ms|s|m|h|d|kb|mb|gb|tb|%|x|k|m|b|"
    r"tokens?|bytes?|words?|chars?|rps|qps|req/s|users?)\b",
    re.I,
)
_INSTR_RE = re.compile(
    r"^\s*(?:must|should|never|always|ensure|require|avoid|"
    r"do not|don't|only|exactly|prefer|forbid|ensure that|"
    r"the rule is|constraint:|requirement:)\b[^\n]{0,200}$",
    re.I | re.MULTILINE,
)
_FENCE_RE = re.compile(r"```[\w+-]*\n[\s\S]*?```")
_FENCE_PLACEHOLDER_RE = re.compile(
    r"```[\w+-]*\n[\s\S]{0,400}?```"
)
_WS_RE = re.compile(r"[ \t]+")
_MULTINL_RE = re.compile(r"\n{3,}")

_FILLER = frozenset({
    "please", "kindly", "just", "really", "very", "actually",
    "basically", "simply", "maybe", "perhaps", "sure", "okay",
    "ok", "well", "anyway", "so", "thus", "hence", "therefore",
    "hello", "hi", "hey", "thanks", "thank", "cheers", "great",
    "cool", "nice", "awesome", "got", "it", "you", "i",
})

CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


# ── Low-level extraction ─────────────────────────────────────────────────
def anchors_of(text: str, *, max_anchors: int = 60) -> list[str]:
    """Return ordered, deduplicated anchors from a text blob."""
    if not text:
        return []
    out: list[str] = []

    # 1. Explicit constraints first — highest signal.
    for m in _INSTR_RE.finditer(text):
        line = m.group(0).strip()
        if 6 <= len(line) <= 200:
            out.append(line)
        if len(out) >= max_anchors:
            break

    # 2. Code blocks — preserve verbatim (already dense).
    for m in _FENCE_RE.finditer(text):
        block = m.group(0)
        if 10 <= len(block) <= 600:
            out.append(block)
        if len(out) >= max_anchors:
            break

    # 3. URLs, paths, identifiers.
    for rx in (_URL_RE, _PATH_RE, _IDENT_RE):
        for m in rx.finditer(text):
            tok = m.group(0).strip()
            if 2 <= len(tok) <= 80 and tok.lower() not in _FILLER:
                out.append(tok)
        if len(out) >= max_anchors * 2:
            break

    # 4. Quantities.
    for m in _QUANT_RE.finditer(text):
        out.append(m.group(0))

    # Deduplicate, preserve order.
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


# ── Turn densifier ───────────────────────────────────────────────────────
def _densify(text: str, *, cap: int = 280) -> str:
    if not text:
        return ""
    t = _WS_RE.sub(" ", text)
    t = _MULTINL_RE.sub("\n\n", t)
    # Keep instruction lines + code fences; drop fillers from prose.
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
        raw = "\n".join(
            p.get("text", "") for p in raw if isinstance(p, dict)
        )
    return {"role": role, "text": str(raw)}


# ── Public: compress ─────────────────────────────────────────────────────
def compress(
    history: Iterable[dict] | None,
    *,
    max_tokens: int = 1200,
    keep_recent: int = 3,
    per_turn_cap: int = 280,
    max_anchors: int = 40,
) -> list[dict[str, Any]]:
    """
    Anchor-based history compression. Drop-in replacement for
    `core.context.clip_history` — same output shape, dense content.
    """
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

    # Extract anchors from the cold tail.
    anchors: list[str] = []
    seen: set[str] = set()
    for t in reversed(cold):                       # newest cold first
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
    anchors.reverse()                              # chronological order

    out: list[dict[str, Any]] = []
    if anchors:
        header = (
            f"[Compressed context — anchors from {len(cold)} earlier turn(s)]"
        )
        out.append({
            "role": "system",
            "text": header + "\n" + "\n".join(f"• {a}" for a in anchors),
        })

    # Fit warm turns newest-first.
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
>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
    return out