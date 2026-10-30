# core/compliance.py
"""
AXELR — PII Detection & Sanitization + Language Utilities
==========================================================
Unified module combining PII handling and language processing
to eliminate scattered micro-files while preserving 100% backward
compatibility and adding elite cross-cutting enhancements.
"""
from __future__ import annotations
import re
import logging
from typing import Any, tuple, list, set, frozenset

logger = logging.getLogger("axelr.compliance")
__version__ = "1.0.0"

# Global lock for all shared state (elite unified locking model)
_GLOBAL_LOCK: Any = None
# Global stats for observability (cross-module correlation)
_GLOBAL_STATS: dict[str, int] = {
    "pii_sanitizations": 0,
    "language_detections": 0,
    "directives_composed": 0,
    "cross_cutting_triggers": 0
}

# Initialize lock on module load
import threading
_GLOBAL_LOCK = threading.Lock()

# ═══════════════════════════════════════════════════════════════════════════
# PII DETECTION & SANITIZATION (merged from pii.py)
# ═══════════════════════════════════════════════════════════════════════════
PIIType = str

_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    # (compiled, pii_type, replacement_token)
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
    (re.compile(
        r"\b(?:password|passwd|pwd|secret|token|auth)[^\s]*\s*[=:]\s*.{8,}\b",
        re.IGNORECASE), "credential", "[REDACTED_CREDENTIAL]"),
    (re.compile(
        r"\b(?:usd|eur|gbp|cad|aud|jpy|cny)\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?\b",
        re.IGNORECASE), "financial", "[REDACTED_FINANCIAL]"),
    (re.compile(r"\$\d{1,3}(?:,\d{3})*(?:\.\d{2})?\b"), "financial",
     "[REDACTED_FINANCIAL]"),
]


def sanitize(text: str) -> tuple[str, list[str]]:
    """Return (sanitized_text, sorted_list_of_types_found). Never raises."""
    if not text:
        return "", []
    found: set[str] = set()
    out = text
    for rx, pii_type, repl in _PATTERNS:
        try:
            if rx.search(out):
                out = rx.sub(repl, out)
                found.add(pii_type)
        except Exception:
            continue
    with _GLOBAL_LOCK:
        _GLOBAL_STATS["pii_sanitizations"] += 1
        if found:
            _GLOBAL_STATS["cross_cutting_triggers"] += 1
    return out, sorted(found)


def found_types(text: str) -> list[str]:
    """Cheap detection without mutation. Returns sorted type names."""
    if not text:
        return []
    found: set[str] = set()
    for rx, pii_type, _ in _PATTERNS:
        try:
            if rx.search(text):
                found.add(pii_type)
        except Exception:
            continue
    return sorted(found)


# ═══════════════════════════════════════════════════════════════════════════
# LANGUAGE DETECTION & DIRECTIVE COMPOSITION (merged from bilingual.py)
# ═══════════════════════════════════════════════════════════════════════════
# Script fingerprints.  One regex each; O(n) match, zero network cost.
_LANG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("hi", re.compile(r"[\u0900-\u097F]")),   # Devanagari (Hindi, Marathi)
    ("bn", re.compile(r"[\u0980-\u09FF]")),   # Bengali
    ("ta", re.compile(r"[\u0B80-\u0BFF]")),   # Tamil
    ("te", re.compile(r"[\u0C00-\u0C7F]")),   # Telugu
    ("ar", re.compile(r"[\u0600-\u06FF]")),   # Arabic
    ("zh", re.compile(r"[\u4E00-\u9FFF]")),   # CJK Unified
    ("ja", re.compile(r"[\u3040-\u30FF]")),   # Hiragana + Katakana
    ("ko", re.compile(r"[\uAC00-\uD7AF]")),   # Hangul
    ("ru", re.compile(r"[\u0400-\u04FF]")),   # Cyrillic
    ("el", re.compile(r"[\u0370-\u03FF]")),   # Greek
    ("he", re.compile(r"[\u0590-\u05FF]")),   # Hebrew
    ("th", re.compile(r"[\u0E00-\u0E7F]")),   # Thai
)

_RTL_LANGS: frozenset[str] = frozenset({"ar", "he", "fa", "ur"})

_CODE_FENCE_RE  = re.compile(r"```[\s\S]*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")


def detect_language(text: str) -> str:
    """Return ISO-639-1-ish code, or 'en' if Latin / empty."""
    if not text:
        return "en"
    for code, rx in _LANG_PATTERNS:
        if rx.search(text):
            with _GLOBAL_LOCK:
                _GLOBAL_STATS["language_detections"] += 1
            return code
    with _GLOBAL_LOCK:
        _GLOBAL_STATS["language_detections"] += 1
    return "en"


def detect_language_with_confidence(text: str) -> tuple[str, float]:
    """
    Return (lang, confidence) where confidence is the fraction of
    non-ASCII characters that match the detected script.  A pure-English
    prompt returns ('en', 1.0) by convention.
    """
    if not text:
        return ("en", 1.0)
    non_ascii = sum(1 for c in text if ord(c) > 127)
    if non_ascii == 0:
        with _GLOBAL_LOCK:
            _GLOBAL_STATS["language_detections"] += 1
        return ("en", 1.0)
    for code, rx in _LANG_PATTERNS:
        matches = rx.findall(text)
        if matches:
            conf = len(matches) / non_ascii
            with _GLOBAL_LOCK:
                _GLOBAL_STATS["language_detections"] += 1
            return (code, round(min(conf, 1.0), 3))
    with _GLOBAL_LOCK:
        _GLOBAL_STATS["language_detections"] += 1
    return ("en", 1.0)


def is_rtl(lang: str) -> bool:
    return lang in _RTL_LANGS


def compose_language_directive(lang: str) -> str:
    """
    Return a prompt block telling the model to reply in the user's
    language while preserving code and identifiers verbatim.
    Returns '' for English (no directive needed).
    """
    if not lang or lang == "en":
        return ""
    rtl_note = " The user's language is right-to-left." if is_rtl(lang) else ""
    directive = (
        "<language_directive>\n"
        f"The user's message is in language code '{lang}'.{rtl_note}\n"
        f"Reply in the same language unless the user explicitly asks for English.\n"
        f"Preserve all code, identifiers, file paths, and quoted strings\n"
        f"exactly as written — do not translate them.\n"
        "</language_directive>"
    )
    with _GLOBAL_LOCK:
        _GLOBAL_STATS["directives_composed"] += 1
        if is_rtl(lang):
            _GLOBAL_STATS["cross_cutting_triggers"] += 1
    return directive


def _protect_code(text: str) -> tuple[str, list[str]]:
    """Replace code fences and inline code with sentinels."""
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
    """Split a previously-tagged prompt into (lang, prompt_without_tag)."""
    m = re.match(r"\x00LANG:([a-z]{2})\x00(.*)", text, re.DOTALL)
    if not m:
        return ("en", text)
    return (m.group(1), m.group(2))


# ═══════════════════════════════════════════════════════════════════════════
# DEPRECATED (kept for import compatibility from bilingual.py)
# ═══════════════════════════════════════════════════════════════════════════
def normalize_for_model(prompt: str, *, target: str = "en") -> str:
    """DEPRECATED. Passthrough to maintain backward compatibility."""
    return prompt or ""


def restore_for_user(text: str, *, original_lang: str = "en") -> str:
    """DEPRECATED. Passthrough to maintain backward compatibility."""
    return text or ""


# ═══════════════════════════════════════════════════════════════════════════
# ELITE ENHANCEMENT: Unified audit API (cross-module observability)
# ═══════════════════════════════════════════════════════════════════════════
def unified_audit() -> dict[str, Any]:
    """Get complete audit of all compliance systems in a single response."""
    with _GLOBAL_LOCK:
        return {
            "version": __version__,
            "global_stats": _GLOBAL_STATS.copy(),
            "pii": {
                "patterns_tracked": len(_PATTERNS),
                "pii_types_tracked": list({t for _, t, _ in _PATTERNS})
            },
            "language": {
                "scripts_tracked": len(_LANG_PATTERNS),
                "rtl_languages_tracked": len(_RTL_LANGS),
                "rtl_codes": sorted(_RTL_LANGS)
            }
        }


# ═══════════════════════════════════════════════════════════════════════════
# BACKWARD COMPATIBILITY SHIMS — ALL ORIGINAL EXPORTS PRESERVED
# ═══════════════════════════════════════════════════════════════════════════
# Original pii.py exports
sanitize = sanitize
found_types = found_types
PIIType = PIIType

# Original bilingual.py exports
detect_language = detect_language
detect_language_with_confidence = detect_language_with_confidence
is_rtl = is_rtl
compose_language_directive = compose_language_directive
extract_lang_tag = extract_lang_tag
normalize_for_model = normalize_for_model
restore_for_user = restore_for_user
_protect_code = _protect_code
_restore_code = _restore_code

__all__ = [
    # PII exports
    "sanitize", "found_types", "PIIType",
    # Language exports
    "detect_language", "detect_language_with_confidence", "is_rtl",
    "compose_language_directive", "extract_lang_tag", "normalize_for_model",
    "restore_for_user", "_protect_code", "_restore_code",
    # New unified exports
    "unified_audit"
]