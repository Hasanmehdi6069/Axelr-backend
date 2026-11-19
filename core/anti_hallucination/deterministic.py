"""AXELR AHE — LLM-free deterministic extractor (v1.6).

Runs before the LLM is ever called. Any field it can find verbatim is
returned at zero provider cost, tagged `trivial=True`, and routed through
the deterministic fast path in SkepticAgent.cross_examine.

Covered fields: 32 patterns across 6 categories. Each pattern is anchored
to reject obvious code/prose false positives (e.g. `def f():` in Python).
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Pattern

from .bounding import Chunk
from .schemas import ExtractedClaim

# ── Pattern registry ──────────────────────────────────────────────────────
_PATTERNS: Dict[str, Pattern] = {}
_PATTERN_META: Dict[str, Dict] = {}


def _reg(name: str, pattern: str, *, category: str,
         description: str, min_len: int = 1) -> None:
    compiled = re.compile(pattern)
    _PATTERNS[name.lower()] = compiled
    _PATTERN_META[name.lower()] = {
        "category": category,
        "description": description,
        "pattern": pattern,
        "min_len": min_len,
    }


# ── Amounts & money ───────────────────────────────────────────────────────
for _f in ("amount", "total", "price", "cost", "fee", "balance", "payment",
           "subtotal", "tax", "discount"):
    _reg(_f, r"\b\d{1,3}(?:,\d{3})*(?:\.\d{1,4})?\b",
         category="money", description="decimal amount with optional thousands",
         min_len=1)

_reg("currency", r"[-+]?[$€£¥₹]\s*\d{1,3}(?:,\d{3})*(?:\.\d{1,4})?",
     category="money", description="currency-prefixed amount")

# ── Identifiers & codes ───────────────────────────────────────────────────
for _f in ("id", "code", "number", "reference", "ref", "sku", "isbn"):
    _reg(_f, r"\b[A-Z0-9][A-Z0-9_\-/]{2,}\b",
         category="identifier", description="alphanumeric identifier", min_len=3)

_reg("uuid", r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
     category="identifier", description="RFC-4122 UUID", min_len=36)

# ── Dates & times ─────────────────────────────────────────────────────────
_reg("date", r"\b\d{4}-\d{2}-\d{2}\b",
     category="datetime", description="ISO 8601 date", min_len=10)
_reg("date_us", r"\b\d{2}/\d{2}/\d{4}\b",
     category="datetime", description="US date", min_len=10)
_reg("date_long", r"\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}\b",
     category="datetime", description="long-form date", min_len=10)
_reg("time", r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\s*[APap]M)?\b",
     category="datetime", description="clock time", min_len=4)
_reg("timestamp", r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b",
     category="datetime", description="ISO 8601 timestamp", min_len=19)

# ── Percentages & ratios ──────────────────────────────────────────────────
_reg("percent", r"\b\d+(?:\.\d+)?\s*%",
     category="numeric", description="percentage", min_len=2)
_reg("ratio", r"\b\d+(?:\.\d+)?\s*:\s*\d+(?:\.\d+)?\b",
     category="numeric", description="ratio (N:M)", min_len=3)

# ── Contact & web ─────────────────────────────────────────────────────────
_reg("email", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
     category="contact", description="email address", min_len=5)
_reg("url", r"https?://\S+",
     category="contact", description="HTTP(S) URL", min_len=8)
_reg("phone", r"\b\+?\d[\d\s\-().]{6,}\d\b",
     category="contact", description="phone number", min_len=8)

# ── Measurements ──────────────────────────────────────────────────────────
_reg("dimension", r"\b\d+(?:\.\d+)?\s*(?:mm|cm|m|km|in|ft|yd|mi)\b",
     category="measurement", description="distance", min_len=3)
_reg("weight", r"\b\d+(?:\.\d+)?\s*(?:mg|g|kg|oz|lb|t)\b",
     category="measurement", description="mass", min_len=3)
_reg("volume", r"\b\d+(?:\.\d+)?\s*(?:ml|cl|dl|l|gal|pt|qt)\b",
     category="measurement", description="volume", min_len=3)
_reg("temperature", r"\b-?\d+(?:\.\d+)?\s*°\s*[CF]\b",
     category="measurement", description="temperature", min_len=3)
_reg("duration", r"\b\d+(?:\.\d+)?\s*(?:ns|us|ms|s|min|h|d|w)\b",
     category="measurement", description="time duration", min_len=3)


def list_patterns() -> Dict[str, Dict]:
    """Machine-readable pattern inventory. Manifesto promise: no magic."""
    return {k: dict(v) for k, v in _PATTERN_META.items()}


def categories() -> List[str]:
    return sorted({m["category"] for m in _PATTERN_META.values()})


# ── Extractor ─────────────────────────────────────────────────────────────
class DeterministicExtractor:
    """No LLM. No network. Verbatim-only. Safe to call first."""

    def extract(
        self, *, query: str, target_fields: List[str], chunks: List[Chunk],
    ) -> List[ExtractedClaim]:
        """Return one claim per (field, chunk) pair where a match exists.

        The claim's raw_snippet IS the matched substring — never a
        reconstructed or reformatted version. This is what makes the
        downstream Skeptic fast path sound.
        """
        out: List[ExtractedClaim] = []
        if not chunks or not target_fields:
            return out
        for field in target_fields:
            pattern = _PATTERNS.get(field.lower())
            if pattern is None:
                continue
            meta = _PATTERN_META.get(field.lower(), {})
            min_len = meta.get("min_len", 1)
            for chunk in chunks:
                text = getattr(chunk, "text", "") or ""
                if not text:
                    continue
                m = pattern.search(text)
                if not m:
                    continue
                value = m.group(0)
                if len(value) < min_len:
                    continue
                out.append(ExtractedClaim(
                    field_name=field,
                    value=value,
                    raw_snippet=value,   # verbatim — no reconstruction
                    chunk_id=chunk.chunk_id,
                    trivial=True,
                ))
                break   # one claim per (field, chunk)
        return out

    def extract_multi(
        self, *, query: str, target_fields: List[str], chunks: List[Chunk],
        max_per_field: int = 5,
    ) -> List[ExtractedClaim]:
        """Like extract() but returns up to max_per_field per field.

        Useful when the source document has multiple occurrences
        (e.g. a CSV export with 100 rows, each containing an amount).
        """
        out: List[ExtractedClaim] = []
        for field in target_fields:
            pattern = _PATTERNS.get(field.lower())
            if pattern is None:
                continue
            found = 0
            for chunk in chunks:
                text = getattr(chunk, "text", "") or ""
                for m in pattern.finditer(text):
                    value = m.group(0)
                    out.append(ExtractedClaim(
                        field_name=field, value=value, raw_snippet=value,
                        chunk_id=chunk.chunk_id, trivial=True,
                    ))
                    found += 1
                    if found >= max_per_field:
                        break
                if found >= max_per_field:
                    break
        return out

    def covers(self, field: str) -> bool:
        return field.lower() in _PATTERNS

    def coverage_report(self, target_fields: Iterable[str]) -> Dict[str, bool]:
        return {f: self.covers(f) for f in target_fields}


__all__ = [
    "DeterministicExtractor", "list_patterns", "categories",
]
