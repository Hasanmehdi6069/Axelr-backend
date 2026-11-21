"""AXELR AHE — VerificationEngine adapter (v1.6).

This module provides the honest VerificationEngine that Ghost-Agent calls
into the AHE pipeline. It's intentionally conservative: no claim reaches
`High` confidence unless it survives verbatim substring matching against
the canonical form of the source text.

Design principles:
  * Verbatim > paraphrased > inferred. Only verbatim counts as High.
  * Case and whitespace normalization are allowed (defends against CRLF,
    smart quotes). Paraphrasing is NOT allowed.
  * Negation detection: if the surrounding sentence contains a negator
    but the claim doesn't, the claim is downgraded.
  * Contradictions are surfaced, not resolved silently.
  * Source integrity: caller may supply a SHA-256 hash; if it doesn't
    match the current file, verification fails.
  * Fail-closed: any exception during verification downgrades confidence
    to Low. Never raise — callers depend on non-throwing contract.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

from .bounding import canonicalize

_SENT_RE = re.compile(r"[^.!?\n]+[.!?]?")
_NUM_RE = re.compile(r"\b\d+(?:[.,]\d+)?%?\b")
_NEGATORS = frozenset(
    {
        "not",
        "no",
        "never",
        "none",
        "nobody",
        "nothing",
        "neither",
        "nor",
        "without",
        "cannot",
        "can't",
        "won't",
        "didn't",
        "doesn't",
        "isn't",
        "aren't",
    }
)
_SENT_BREAK = re.compile(r"[.!?\n]")

# Max sentence length for a claim to be considered a sentence. Anything
# longer is a paragraph and gets truncated before verbatim matching.
_MAX_SENT_LEN = 500
_MIN_SENT_LEN = 20

# Confidence thresholds (all [0, 1])
_VERBATIM_THRESHOLD = 1.0  # exact substring required
_NORMALIZED_THRESHOLD = 0.98  # after canonicalization
_PARTIAL_THRESHOLD = 0.90  # token overlap (very strict)


@dataclass
class VerificationResult:
    """Result returned to Ghost-Agent. Never raises; downgrades on failure."""

    confidence: str  # "High" | "Medium" | "Low"
    source_path: Optional[str] = None
    reason: str = ""
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    source_hash: Optional[str] = None
    normalized_match: bool = False


def _hash_file(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return None


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def _tokenize(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _token_overlap(a: str, b: str) -> float:
    ta = set(_tokenize(a))
    tb = set(_tokenize(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _find_negation_window(text: str, start: int, end: int, radius: int = 240) -> Tuple[int, int, bool]:
    s0 = start
    while s0 > 0 and s0 > start - radius and not _SENT_BREAK.match(text[s0 - 1]):
        s0 -= 1
    s1 = end
    while s1 < len(text) and s1 < end + radius and not _SENT_BREAK.match(text[s1]):
        s1 += 1
    window = text[s0:s1].lower()
    tokens = set(re.findall(r"[a-z']+", window))
    return s0, s1, bool(_NEGATORS & tokens)


def _claim_has_negator(claim: str) -> bool:
    tokens = set(re.findall(r"[a-z']+", claim.lower()))
    return bool(_NEGATORS & tokens)


class VerificationEngine:
    """Honest adapter — verbatim substring, no blind trust, fail-closed."""

    def __init__(self, *, strict: bool = True) -> None:
        self.strict = strict
        self._cache: dict[str, str] = {}

    # ── Extraction ──────────────────────────────────────────────────
    def extract_assertions(self, content: str) -> List[str]:
        if not content:
            return []
        out: List[str] = []
        for sentence in _SENT_RE.findall(content):
            s = sentence.strip()
            if _MIN_SENT_LEN <= len(s) <= _MAX_SENT_LEN:
                out.append(s)
        return out[:50]

    # ── Verification ────────────────────────────────────────────────
    def verify_source(
        self,
        assertion: str,
        file_path: str,
        line_number: Optional[int] = None,
        content: Optional[str] = None,
        *,
        expected_hash: Optional[str] = None,
    ) -> VerificationResult:
        """Returns High iff assertion is verbatim in the source.

        Confidence tiers:
          High   — verbatim substring (or normalized-verbatim ≥ 0.98)
          Medium — high token overlap but not verbatim, no negation conflict
          Low    — everything else (fail-closed)
        """
        # Guard against empty / too-short claims
        if not assertion or not file_path or len(assertion) < _MIN_SENT_LEN:
            return VerificationResult("Low", file_path, "too_short")

        # Load source text (lazy, cached)
        if content is None:
            if file_path not in self._cache:
                self._cache[file_path] = _read_text(file_path)
            content = self._cache[file_path]
        if not content:
            return VerificationResult("Low", file_path, "source_empty")

        # Source integrity check
        actual_hash = _hash_file(file_path) if expected_hash else None
        if expected_hash and actual_hash and actual_hash != expected_hash:
            return VerificationResult(
                "Low",
                file_path,
                "source_hash_mismatch",
                source_hash=actual_hash,
            )

        # Try exact substring first (cheapest)
        pos = content.find(assertion)
        if pos >= 0:
            # Negation sanity check
            s0, s1, neg = _find_negation_window(content, pos, pos + len(assertion))
            if neg and not _claim_has_negator(assertion):
                return VerificationResult(
                    "Low",
                    file_path,
                    "source_negated",
                    pos,
                    pos + len(assertion),
                )
            return VerificationResult(
                "High",
                file_path,
                "verbatim",
                pos,
                pos + len(assertion),
                source_hash=actual_hash,
                normalized_match=False,
            )

        # Try normalized match (handles CRLF, smart quotes, extra spaces)
        try:
            canon_a, _ = canonicalize(assertion)
            canon_c, idx = canonicalize(content)
        except Exception:
            return VerificationResult("Low", file_path, "canon_failed")

        if canon_a and canon_a in canon_c:
            npos = canon_c.find(canon_a)
            # Map back to original positions
            if idx is not None and npos + len(canon_a) - 1 < len(idx):
                orig_start = idx[npos]
                orig_end = idx[npos + len(canon_a) - 1] + 1
            else:
                orig_start, orig_end = pos if pos >= 0 else 0, 0
            s0, s1, neg = _find_negation_window(
                content,
                orig_start,
                orig_end,
            )
            if neg and not _claim_has_negator(assertion):
                return VerificationResult(
                    "Low",
                    file_path,
                    "source_negated",
                    orig_start,
                    orig_end,
                )
            return VerificationResult(
                "High",
                file_path,
                "normalized_verbatim",
                orig_start,
                orig_end,
                source_hash=actual_hash,
                normalized_match=True,
            )

        # Partial match: high token overlap, no verbatim
        overlap = _token_overlap(assertion, content)
        if not self.strict and overlap >= _PARTIAL_THRESHOLD:
            return VerificationResult(
                "Medium",
                file_path,
                f"partial_match:{overlap:.2f}",
                source_hash=actual_hash,
            )

        return VerificationResult(
            "Low",
            file_path,
            f"not_verbatim:overlap={overlap:.2f}",
            source_hash=actual_hash,
        )

    # ── Contradiction detection ────────────────────────────────────
    def detect_contradictions(
        self,
        assertions: Iterable,
    ) -> List[Tuple[object, object]]:
        """Two claims contradict if they normalize to the same template but
        have different numeric values, ordered by timestamp."""
        pairs: List[Tuple[object, object]] = []
        groups: dict = {}
        for a in assertions:
            text = getattr(a, "assertion", "")
            nums = _NUM_RE.findall(text)
            if not nums:
                continue
            template = re.sub(r"\d+(?:[.,]\d+)?%?", "#", text.lower()).strip()
            groups.setdefault(template, []).append((a, tuple(sorted(nums))))
        for items in groups.values():
            if len(items) < 2:
                continue
            tokens = {t for _, t in items}
            if len(tokens) <= 1:
                continue
            ordered = sorted(items, key=lambda x: x[0].timestamp)
            for i in range(len(ordered) - 1):
                pairs.append((ordered[i][0], ordered[i + 1][0]))
        return pairs

    def resolve_contradictions(
        self,
        contradictions: List[Tuple[object, object]],
        audit_trail,
    ) -> None:
        for older, _ in contradictions:
            try:
                older.confidence = type(older.confidence).LOW
            except Exception:
                pass

    # ── Cache management ───────────────────────────────────────────
    def invalidate(self, file_path: Optional[str] = None) -> None:
        if file_path is None:
            self._cache.clear()
        else:
            self._cache.pop(file_path, None)


__all__ = ["VerificationEngine", "VerificationResult"]
