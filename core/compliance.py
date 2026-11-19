# core/compliance.py
"""
AXELR — Compliance: PII Sanitization + Language Detection
===========================================================
Two isolated concerns, one file:

    §1 PII        — Deterministic regex redaction. Never calls an LLM.
    §2 Language   — Script fingerprint detection. O(n), no network.

Both are exposed through a single `audit()` for observability.
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Any

logger = logging.getLogger("axelr.compliance")
__version__ = "2.0.0"

__all__ = [
    # PII
    "sanitize", "found_types", "PIIType",
    # Language
    "detect_language", "detect_language_with_confidence",
    "is_rtl", "compose_language_directive", "extract_lang_tag",
    # Deprecated (kept for backward compat)
    "normalize_for_model", "restore_for_user",
    # Audit
    "audit",
]

# ═══════════════════════════════════════════════════════════════════════════
# §1 — PII Sanitization
# ═══════════════════════════════════════════════════════════════════════════
PIIType = str

_PII_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
                re.IGNORECASE), "email", "[REDACTED_EMAIL]"),
    (re.compile(r"\b(\+\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
     "phone", "[REDACTED_PHONE]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "ip_address", "[REDACTED_IP]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "ssn", "[REDACTED_SSN]"),
    (re.compile(r"\b(?:\d[ -]*?){13,16}\b"), "credit_card", "[REDACTED_CC]"),
    (re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"), "api_key", "[REDACTED_API_KEY]"),
    (re.compile(r"\bpk_[A-Za-z0-9]{32,}\b"), "api_key", "[REDACTED_API_KEY]"),
    (re.compile(r"\b[A-Za-z0-9/+]{32,}\b"), "api_key", "[REDACTED_API_KEY]"),
    (re.compile(r"\b(?:password|passwd|pwd|secret|token|auth)[^\s]*\s*[=:]\s*.{8,}\b",
                re.IGNORECASE), "credential", "[REDACTED_CREDENTIAL]"),
    (re.compile(r"\b(?:usd|eur|gbp|cad|aud|jpy|cny)\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?\b",
                re.IGNORECASE), "financial", "[REDACTED_FINANCIAL]"),
    (re.compile(r"\$\d{1,3}(?:,\d{3})*(?:\.\d{2})?\b"),
     "financial", "[REDACTED_FINANCIAL]"),
]

_STATS_LOCK = threading.Lock()
_STATS = {"redacted": 0, "sanitized_calls": 0, "language_calls": 0}


def sanitize(text: str) -> tuple[str, list[str]]:
    """Return (sanitized_text, sorted_list_of_types_found). Never raises."""
    if not text:
        return "", []
    found: set[str] = set()
    out = text
    for rx, pii_type, repl in _PII_PATTERNS:
        try:
            if rx.search(out):
                out = rx.sub(repl, out)
                found.add(pii_type)
        except Exception:
            continue
    with _STATS_LOCK:
        _STATS["sanitized_calls"] += 1
        _STATS["redacted"] += len(found)
    return out, sorted(found)


def found_types(text: str) -> list[str]:
    """Cheap detection without mutation. Returns sorted type names."""
    if not text:
        return []
    found: set[str] = set()
    for rx, pii_type, _ in _PII_PATTERNS:
        try:
            if rx.search(text):
                found.add(pii_type)
        except Exception:
            continue
    return sorted(found)


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Language Detection
# ═══════════════════════════════════════════════════════════════════════════
_LANG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("hi", re.compile(r"[\u0900-\u097F]")),
    ("bn", re.compile(r"[\u0980-\u09FF]")),
    ("ta", re.compile(r"[\u0B80-\u0BFF]")),
    ("te", re.compile(r"[\u0C00-\u0C7F]")),
    ("ar", re.compile(r"[\u0600-\u06FF]")),
    ("zh", re.compile(r"[\u4E00-\u9FFF]")),
    ("ja", re.compile(r"[\u3040-\u30FF]")),
    ("ko", re.compile(r"[\uAC00-\uD7AF]")),
    ("ru", re.compile(r"[\u0400-\u04FF]")),
    ("el", re.compile(r"[\u0370-\u03FF]")),
    ("he", re.compile(r"[\u0590-\u05FF]")),
    ("th", re.compile(r"[\u0E00-\u0E7F]")),
)
_RTL_LANGS: frozenset[str] = frozenset({"ar", "he", "fa", "ur"})
_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")


def detect_language(text: str) -> str:
    """Return ISO-639-1-ish code, or 'en' if Latin / empty."""
    if not text:
        return "en"
    with _STATS_LOCK:
        _STATS["language_calls"] += 1
    for code, rx in _LANG_PATTERNS:
        if rx.search(text):
            return code
    return "en"


def detect_language_with_confidence(text: str) -> tuple[str, float]:
    if not text:
        return ("en", 1.0)
    non_ascii = sum(1 for c in text if ord(c) > 127)
    if non_ascii == 0:
        return ("en", 1.0)
    for code, rx in _LANG_PATTERNS:
        matches = rx.findall(text)
        if matches:
            conf = len(matches) / non_ascii
            return (code, round(min(conf, 1.0), 3))
    return ("en", 1.0)


def is_rtl(lang: str) -> bool:
    return lang in _RTL_LANGS


def compose_language_directive(lang: str) -> str:
    if not lang or lang == "en":
        return ""
    rtl_note = " The user's language is right-to-left." if is_rtl(lang) else ""
    return (
        "<language_directive>\n"
        f"The user's message is in language code '{lang}'.{rtl_note}\n"
        "Reply in the same language unless the user explicitly asks for English.\n"
        "Preserve all code, identifiers, file paths, and quoted strings\n"
        "exactly as written — do not translate them.\n"
        "</language_directive>"
    )


def _protect_code(text: str) -> tuple[str, list[str]]:
    stashed: list[str] = []
    def _stash(m: re.Match) -> str:
        stashed.append(m.group(0))
        return f"\x00CODE{len(stashed) - 1}\x00"
    protected = _CODE_FENCE_RE.sub(_stash, text)
    protected = _INLINE_CODE_RE.sub(_stash, protected)
    return protected, stashed


def _restore_code(text: str, stashed: list[str]) -> str:
    def _restore(m: re.Match) -> str:
        i = int(m.group(1))
        return stashed[i] if 0 <= i < len(stashed) else m.group(0)
    return re.sub(r"\x00CODE(\d+)\x00", _restore, text)


def extract_lang_tag(text: str) -> tuple[str, str]:
    m = re.match(r"\x00LANG:([a-z]{2})\x00(.*)", text, re.DOTALL)
    if not m:
        return ("en", text)
    return (m.group(1), m.group(2))


# ── Deprecated (kept for backward compat) ──────────────────────────────
def normalize_for_model(prompt: str, *, target: str = "en") -> str:
    return prompt or ""


def restore_for_user(text: str, *, original_lang: str = "en") -> str:
    return text or ""


# ═══════════════════════════════════════════════════════════════════════════
# Audit
# ═══════════════════════════════════════════════════════════════════════════
def audit() -> dict[str, Any]:
    with _STATS_LOCK:
        stats = dict(_STATS)
    return {
        "version": __version__,
        "stats": stats,
        "pii_types_tracked": sorted({t for _, t, _ in _PII_PATTERNS}),
        "scripts_tracked": len(_LANG_PATTERNS),
        "rtl_languages_tracked": sorted(_RTL_LANGS),
    }
