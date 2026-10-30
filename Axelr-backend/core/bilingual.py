# core/bilingual.py
"""
AXELR — Language Detection & Directive Composition (v2.0)
==========================================================
Two jobs, and only two:

    1. Identify the user's language from their prompt (script-based
       detection, deterministic, O(n) in prompt length).
    2. Compose a <language_directive> block that tells the model to
       reply in kind, without translating anything.

The model's own multilingual ability handles the content.  What the
model cannot guess is that we want code, identifiers, and file paths
preserved verbatim.  That is the entire reason this module exists.

v2.0 removes the previous alignment table.  That table had 5 languages
× 6 words — it munged prompts worse than the model handled natively,
and it was a source of silent wrong-token substitutions.  Native
multilingual capability does the actual work now.

Public:
    detect_language(text)                  -> str
    detect_language_with_confidence(text)  -> (str, float)
    is_rtl(lang)                           -> bool
    compose_language_directive(lang)       -> str
    extract_lang_tag(text)                 -> (lang, clean_text)
    _protect_code(text) / _restore_code    — code fence protection
    normalize_for_model / restore_for_user — DEPRECATED no-ops
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("axelr.bilingual")

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


# ═══════════════════════════════════════════════════════════════════════════
# Detection
# ═══════════════════════════════════════════════════════════════════════════
def detect_language(text: str) -> str:
    """Return ISO-639-1-ish code, or 'en' if Latin / empty."""
    if not text:
        return "en"
    for code, rx in _LANG_PATTERNS:
        if rx.search(text):
            return code
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
        return ("en", 1.0)
    for code, rx in _LANG_PATTERNS:
        matches = rx.findall(text)
        if matches:
            conf = len(matches) / non_ascii
            return (code, round(min(conf, 1.0), 3))
    return ("en", 1.0)


def is_rtl(lang: str) -> bool:
    return lang in _RTL_LANGS


# ═══════════════════════════════════════════════════════════════════════════
# Directive composition — the only thing the conductor consumes
# ═══════════════════════════════════════════════════════════════════════════
def compose_language_directive(lang: str) -> str:
    """
    Return a prompt block telling the model to reply in the user's
    language while preserving code and identifiers verbatim.
    Returns '' for English (no directive needed).
    """
    if not lang or lang == "en":
        return ""
    rtl_note = " The user's language is right-to-left." if is_rtl(lang) else ""
    return (
        "<language_directive>\n"
        f"The user's message is in language code '{lang}'.{rtl_note}\n"
        f"Reply in the same language unless the user explicitly asks for English.\n"
        f"Preserve all code, identifiers, file paths, and quoted strings\n"
        f"exactly as written — do not translate them.\n"
        "</language_directive>"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Code-fence protection (used by any downstream text rewriter)
# ═══════════════════════════════════════════════════════════════════════════
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
# Deprecated — kept for import compatibility; no longer used
# ═══════════════════════════════════════════════════════════════════════════
def normalize_for_model(prompt: str, *, target: str = "en") -> str:
    """
    DEPRECATED.  The previous alignment table produced worse outputs than
    the model's native multilingual handling.  Now a passthrough so that
    any code still importing this function keeps working.  Use
    detect_language() + compose_language_directive() instead.
    """
    return prompt or ""


def restore_for_user(text: str, *, original_lang: str = "en") -> str:
    """DEPRECATED.  See normalize_for_model."""
    return text or ""


__all__ = [
    "detect_language",
    "detect_language_with_confidence",
    "is_rtl",
    "compose_language_directive",
    "extract_lang_tag",
    "normalize_for_model",
    "restore_for_user",
    "_protect_code",
    "_restore_code",
]