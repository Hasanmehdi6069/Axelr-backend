# core/bilingual.py
"""
AXELR — Bilingual Context-Interlocking Multi-Bridge (F40)
============================================================
Detects the target language of a user query, then converts the surrounding
prose to English before the core model call — while leaving any embedded
code fences untouched. Reverses the translation on the way back so the
user sees their primary language.

Zero cost: pure regex + a small dictionary of developer-domain keywords.
No model, no network. RAM: ~40 KB.

Public:
    detect_language(prompt) -> str
    normalize_for_model(prompt, *, target="en") -> str
    restore_for_user(text, *, original_lang) -> str
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("axelr.bilingual")

# Language fingerprints — Devanagari, Arabic, CJK, Cyrillic, Greek,
# Hebrew, Thai, Hangul. One regex each; O(1) check.
_LANG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("hi",   re.compile(r"[\u0900-\u097F]")),   # Devanagari (Hindi)
    ("ar",   re.compile(r"[\u0600-\u06FF]")),   # Arabic
    ("zh",   re.compile(r"[\u4E00-\u9FFF]")),   # CJK Unified
    ("ja",   re.compile(r"[\u3040-\u30FF]")),   # Hiragana + Katakana
    ("ko",   re.compile(r"[\uAC00-\uD7AF]")),   # Hangul
    ("ru",   re.compile(r"[\u0400-\u04FF]")),   # Cyrillic
    ("el",   re.compile(r"[\u0370-\u03FF]")),   # Greek
    ("he",   re.compile(r"[\u0590-\u05FF]")),   # Hebrew
    ("th",   re.compile(r"[\u0E00-\u0E7F]")),   # Thai
    ("ur",   re.compile(r"[\u0600-\u06FF]")),   # Urdu (shares Arabic block)
)

_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")


def detect_language(prompt: str) -> str:
    """Return ISO-639-1-ish code, or 'en' if Latin / empty."""
    if not prompt:
        return "en"
    for code, rx in _LANG_PATTERNS:
        if rx.search(prompt):
            return code
    return "en"


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


# Minimal alignment table — enough for the top-5 dev languages. Any
# language not covered is passed through unchanged (still useful: the
# model sees the raw prompt and the code fence is protected).
_ALIGNMENT: dict[str, dict[str, str]] = {
    "hi": {"बनाएं": "build", "बनाओ": "create", "ठीक": "fix", "सुधार": "refactor",
           "डिबग": "debug", "व्याख्या": "explain", "लिखें": "write",
           "तुलना": "compare", "सारांश": "summarize"},
    "ar": {"أنشئ": "create", "بناء": "build", "إصلاح": "fix",
           "اشرح": "explain", "اكتب": "write", "قارن": "compare"},
    "zh": {"创建": "create", "构建": "build", "修复": "fix",
           "解释": "explain", "写": "write", "比较": "compare"},
    "ru": {"создать": "create", "построить": "build", "исправить": "fix",
           "объяснить": "explain", "написать": "write", "сравнить": "compare"},
    "es": {"crear": "create", "construir": "build", "arreglar": "fix",
           "explicar": "explain", "escribir": "write", "comparar": "compare"},
}


def normalize_for_model(prompt: str, *, target: str = "en") -> str:
    """
    Convert recognized prose tokens to English, keep code fences intact.
    Detected language is stashed in a sentinel that must be restored.
    """
    if not prompt:
        return ""
    lang = detect_language(prompt)
    if lang == "en" or lang not in _ALIGNMENT:
        return prompt
    protected, stashed = _protect_code(prompt)
    table = _ALIGNMENT[lang]
    for src, dst in table.items():
        protected = protected.replace(src, dst)
    # Tag the language for the restore path
    tagged = f"\x00LANG:{lang}\x00{protected}"
    return _restore_code(tagged, stashed)


def restore_for_user(text: str, *, original_lang: str = "en") -> str:
    """Reverse the alignment so the user sees their language."""
    if not text or original_lang == "en":
        return text
    table = _ALIGNMENT.get(original_lang)
    if not table:
        return text
    reverse = {v: k for k, v in table.items()}
    protected, stashed = _protect_code(text)
    for src, dst in reverse.items():
        protected = re.sub(rf"\b{re.escape(src)}\b", dst, protected)
    return _restore_code(protected, stashed)


def extract_lang_tag(text: str) -> tuple[str, str]:
    """Split a normalized prompt into (lang, prompt_without_tag)."""
    m = re.match(r"\x00LANG:([a-z]{2})\x00(.*)", text, re.DOTALL)
    if not m:
        return ("en", text)
    return (m.group(1), m.group(2))


__all__ = [
    "detect_language", "normalize_for_model",
    "restore_for_user", "extract_lang_tag",
]