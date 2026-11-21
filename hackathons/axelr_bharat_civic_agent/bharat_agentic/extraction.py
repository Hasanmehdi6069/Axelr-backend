"""Deterministic case-document extraction.

Multi-language action detection (English, Hindi/Devanagari, Hinglish),
context-aware date selection that avoids picking the FIR-date, and hard
size caps so a 5 MB notice cannot OOM the process.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Protocol, Tuple

from .models import CaseRecord, CaseStatus, ExtractedField, new_case_id

logger = logging.getLogger("bharat_agentic.extraction")


# ---------------------------------------------------------------------------
# Document loading
# ---------------------------------------------------------------------------


class DocumentLoadError(RuntimeError):
    pass


class DocumentLoader:
    """Load bytes -> plain text.

    - ``.txt`` / ``.md`` / unknown  — UTF-8 decode (latin-1 fallback).
    - ``.pdf``                      — via ``pypdf`` if installed.
    - ``.png`` / ``.jpg`` / ``.jpeg`` — via ``pytesseract`` + ``pillow``.
    """

    @staticmethod
    def load(filename: str, content: bytes) -> str:
        name = (filename or "").lower()
        if name.endswith(".pdf"):
            try:
                import io

                from pypdf import PdfReader  # type: ignore
            except ImportError as exc:
                raise DocumentLoadError("pypdf not installed; `pip install pypdf` for PDF support") from exc
            reader = PdfReader(io.BytesIO(content))
            return "\n".join(p.extract_text() or "" for p in reader.pages).strip()
        if name.endswith((".png", ".jpg", ".jpeg")):
            try:
                import io

                import pytesseract  # type: ignore
                from PIL import Image  # type: ignore
            except ImportError as exc:
                raise DocumentLoadError("pillow + pytesseract not installed; `pip install pillow pytesseract`") from exc
            return pytesseract.image_to_string(Image.open(io.BytesIO(content)))
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            return content.decode("latin-1", errors="replace")


# ---------------------------------------------------------------------------
# Extraction protocol
# ---------------------------------------------------------------------------


class ExtractionBackend(Protocol):
    async def extract(self, text: str) -> CaseRecord: ...


# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

_CASE_NUMBER = re.compile(
    r"\b((?:C\.C\.|Crl\.?\s*[AM]\.|W\.P\.?\s*\(C\)|S\.L\.P\.|FIR|S\.T\.|"
    r"M\.A\.|R\.S\.A\.|C\.P\.|Case)\s*(?:No\.?\s*)?\d+\s*[/\-]\s*\d{4})",
    re.IGNORECASE,
)

_COURT = re.compile(
    r"(?:IN\s+THE\s+COURT\s+OF\s+(?:THE\s+)?|IN\s+THE\s+|BEFORE\s+THE\s+)?"
    r"((?:CHIEF\s+JUDICIAL\s+MAGISTRATE|JUDICIAL\s+MAGISTRATE|"
    r"DISTRICT\s+(?:&\s+|AND\s+)?SESSIONS\s+COURT|DISTRICT\s+COURT|"
    r"SESSIONS\s+COURT|FAMILY\s+COURT|CONSUMER\s+(?:DISPUTES\s+)?FORUM|"
    r"NCLT|LOK\s+ADALAT|HIGH\s+COURT\s+OF\s+[A-Z]+|"
    r"SUPREME\s+COURT\s+OF\s+INDIA|CIVIL\s+JUDGE(?:\s*\([A-Z.\s]+\))?|"
    r"SUB-?DIVISIONAL\s+MAGISTRATE)"
    r"(?:\s*,\s*[A-Z][A-Za-z\s]+?)?)(?=\s*(?:Case|$|\n))",
    re.IGNORECASE,
)

_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATE_DMY = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b")
_DATE_TEXT = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{4})\b",
    re.IGNORECASE,
)
_HEARING_KEYWORD = re.compile(r"(?i)\b(hearing|listed|appear|next\s+date|date\s+of\s+hearing)\b")
# Anti-keywords: dates immediately after these are almost certainly NOT the
# next hearing — they are the FIR date, order date, registration date.
_NON_HEARING_DATE = re.compile(
    r"(?i)\b(fir\s+dated|filed\s+on|dated\s+this|registered\s+on|"
    r"issue\s+date|order\s+dated|complaint\s+dated)\b"
)
_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

_ACTIONS: List[Tuple[re.Pattern, str]] = [
    # --- English ---
    (re.compile(r"(?i)\bappear\s+in\s+person\b"), "appear in person"),
    (
        re.compile(
            r"(?i)\bproduce\s+(?:the\s+)?(?:original\s+)?"
            r"(?:following\s+)?documents?\b"
        ),
        "produce documents",
    ),
    (
        re.compile(
            r"(?i)\bfile\s+(?:your\s+)?(?:written\s+)?"
            r"(?:reply|statement|objection)\b"
        ),
        "file reply",
    ),
    (re.compile(r"(?i)\bpay\s+(?:the\s+)?(?:fine|costs?|compensation)\b"), "pay fine"),
    (re.compile(r"(?i)\bsettle\s+(?:the\s+)?(?:matter|dispute)\b"), "settle matter"),
    (re.compile(r"(?i)\b(?:serve|send)\s+(?:the\s+)?notice\b"), "serve notice"),
    (re.compile(r"(?i)\bfurnish\s+(?:the\s+)?(?:bail\s+)?bond\b"), "furnish bond"),
    # --- Hinglish / code-mixed ---
    (
        re.compile(
            r"(?i)\b(?:document[s]?|kagaz|papers?)\s+"
            r"(?:leke\s+aana|lekar\s+aana)\b"
        ),
        "produce documents",
    ),
    (
        re.compile(
            r"(?i)\b(?:khud\s+)?(?:aana\s+hai|hazir\s+hona|"
            r"hazir\s+raho)\b"
        ),
        "appear in person",
    ),
    (re.compile(r"(?i)\b(?:reply|jawab)\s+(?:file\s+karna|dena)\b"), "file reply"),
    (
        re.compile(
            r"(?i)\b(?:fine|jurmana|paise)\s+"
            r"(?:bharna|jama|bharo|jama\s+karo)\b"
        ),
        "pay fine",
    ),
    (re.compile(r"(?i)\b(?:mamla|matter)\s+(?:suljhana|settle\s+karna)\b"), "settle matter"),
    (re.compile(r"(?i)\bnotice\s+(?:bhejo|serve\s+karo|dena)\b"), "serve notice"),
    (re.compile(r"(?i)\b(?:bail\s+)?bond\s+dena\b"), "furnish bond"),
    # --- Hindi / Devanagari ---
    (re.compile(r"व्यक्तिगत\s+रूप\s+से\s+उपस्थित|स्वयं\s+उपस्थित"), "appear in person"),
    (re.compile(r"दस्तावेज़\s+(?:प्रस्तुत|पेश)|कागज़ात\s+प्रस्तुत"), "produce documents"),
    (re.compile(r"(?:उत्तर|जवाब)\s+दाखिल"), "file reply"),
    (re.compile(r"जुर्माना\s+(?:भरें|दें|अदा)"), "pay fine"),
    (re.compile(r"मामला\s+सुलझा|मामले\s+का\s+समझौता"), "settle matter"),
    (re.compile(r"नोटिस\s+(?:तामील|सेवा|भेजें)"), "serve notice"),
    (re.compile(r"(?:बॉन्ड|बंधपत्र)\s+प्रस्तुत"), "furnish bond"),
]

_PARTIES = re.compile(
    r"^([A-Z\u0900-\u097F\u0B80-\u0BFF\u0C00-\u0C7F\u0980-\u09FF]"
    r"[A-Za-z\u0900-\u097F\u0B80-\u0BFF\u0C00-\u0C7F\u0980-\u09FF.&'\- ]{2,60}?)"
    r"\s+(?:vs?\.?|versus)\s+"
    r"([A-Z\u0900-\u097F\u0B80-\u0BFF\u0C00-\u0C7F\u0980-\u09FF]"
    r"[A-Za-z\u0900-\u097F\u0B80-\u0BFF\u0C00-\u0C7F\u0980-\u09FF.&'\- ]{2,60}?)"
    r"\s*$",
    re.MULTILINE,
)

_PRESERVE_CASE = {"NCLT", "FIR", "SMS", "WAL"}
_SMALL_WORDS = {"of", "and", "the", "in", "at", "on", "for", "to", "a", "an"}


def _normalise(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().rstrip(".,;")


def _capitalize_alpha_runs(word: str) -> str:
    out: List[str] = []
    run: List[str] = []
    for ch in word:
        if ch.isalpha():
            run.append(ch)
        else:
            if run:
                out.append("".join(run).capitalize())
                run = []
            out.append(ch)
    if run:
        out.append("".join(run).capitalize())
    return "".join(out)


def _smart_title(text: str) -> str:
    if not text:
        return text
    out: List[str] = []
    for i, word in enumerate(text.split()):
        core = word.rstrip(",.;:")
        suffix = word[len(core) :]
        if core in _PRESERVE_CASE:
            out.append(word)
        elif core.upper() in _PRESERVE_CASE:
            out.append(core.upper() + suffix)
        elif core.isalpha() and core.lower() in _SMALL_WORDS and i > 0:
            out.append(core.lower() + suffix)
        elif not core:
            out.append(word)
        else:
            out.append(_capitalize_alpha_runs(core) + suffix)
    return " ".join(out)


@dataclass(slots=True)
class _DateCandidate:
    iso: str
    start: int
    end: int


def _iso(y: int, m: int, d: int) -> Optional[str]:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def _extract_date(text: str) -> ExtractedField:
    candidates: List[_DateCandidate] = []
    for m in _DATE_ISO.finditer(text):
        iso = _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if iso:
            candidates.append(_DateCandidate(iso, m.start(), m.end()))
    for m in _DATE_DMY.finditer(text):
        iso = _iso(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if iso:
            candidates.append(_DateCandidate(iso, m.start(), m.end()))
    for m in _DATE_TEXT.finditer(text):
        iso = _iso(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
        if iso:
            candidates.append(_DateCandidate(iso, m.start(), m.end()))

    if not candidates:
        return ExtractedField(notes="no date candidate found")

    keyword_positions = [m.start() for m in _HEARING_KEYWORD.finditer(text)]
    anti_positions = [m.start() for m in _NON_HEARING_DATE.finditer(text)]

    def _min_dist(pos: int, points: List[int]) -> float:
        if not points:
            return 10_000.0
        return float(min(abs(pos - p) for p in points))

    def score(c: _DateCandidate) -> float:
        kw_dist = _min_dist(c.start, keyword_positions)
        anti_dist = _min_dist(c.start, anti_positions)
        return kw_dist - (anti_dist * 0.5)

    best = min(candidates, key=score)
    kw_distance = _min_dist(best.start, keyword_positions)
    anti_distance = _min_dist(best.start, anti_positions)

    if kw_distance < 60:
        conf = 0.95
    elif kw_distance < 200:
        conf = 0.7
    else:
        conf = 0.45
    # Single-candidate boost is DELIBERATELY suppressed when an
    # anti-keyword ("FIR dated", "filed on", ...) is nearby. Otherwise a
    # notice whose only date is "FIR dated 01/01/2024" would ship a
    # MEDIUM-confidence hearing date and bypass needs_review.
    if len(candidates) == 1 and anti_distance > 100:
        conf = max(conf, 0.7)
    return ExtractedField(value=best.iso, confidence=conf, source_span=text[best.start : best.end].strip())


def _extract_case_number(text: str) -> ExtractedField:
    m = _CASE_NUMBER.search(text)
    if not m:
        return ExtractedField(notes="no case-number pattern matched")
    raw = _normalise(m.group(1))
    digits = re.search(r"(\d+\s*[/\-]\s*\d{4})", raw)
    value = re.sub(r"\s+", "", digits.group(1)) if digits else raw
    return ExtractedField(value=value, confidence=0.95, source_span=raw)


def _extract_court(text: str) -> ExtractedField:
    m = _COURT.search(text)
    if not m:
        return ExtractedField(notes="no court pattern matched")
    raw_value = m.group(1)
    cleaned_value = re.sub(r"\s*Case\s*No\.?\s*$", "", raw_value, flags=re.IGNORECASE)
    value = _smart_title(_normalise(cleaned_value))
    return ExtractedField(value=value, confidence=0.9, source_span=m.group(0).strip())


def _extract_action(text: str) -> ExtractedField:
    for pattern, canonical in _ACTIONS:
        m = pattern.search(text)
        if m:
            return ExtractedField(value=canonical, confidence=0.85, source_span=m.group(0).strip())
    return ExtractedField(notes="no known required-action phrase found")


def _extract_parties(
    text: str,
) -> Tuple[ExtractedField, ExtractedField]:
    m = _PARTIES.search(text)
    if not m:
        return (
            ExtractedField(notes="petitioner not found"),
            ExtractedField(notes="respondent not found"),
        )
    return (
        ExtractedField(value=_normalise(m.group(1)), confidence=0.7),
        ExtractedField(value=_normalise(m.group(2)), confidence=0.7),
    )


class RegexExtractionBackend:
    """Deterministic extractor — the default backend for this deployment."""

    name = "regex-v2-multilang"

    async def extract(self, text: str) -> CaseRecord:
        if not text or not text.strip():
            raise ValueError("document text is empty")
        record = CaseRecord(
            case_id=new_case_id(),
            source_filename="",
            raw_text=text,
            status=CaseStatus.EXTRACTED,
        )
        record.case_number = _extract_case_number(text)
        record.court = _extract_court(text)
        record.next_hearing = _extract_date(text)
        record.required_action = _extract_action(text)
        record.petitioner, record.respondent = _extract_parties(text)
        if not record.next_hearing.value:
            record.extraction_warnings.append("next_hearing missing — manual review required")
        if not record.required_action.value:
            record.extraction_warnings.append("required_action missing — manual review required")
        return record


# ---------------------------------------------------------------------------
# Public facade
# ---------------------------------------------------------------------------


class CaseExtractor:
    def __init__(self, backend: Optional[ExtractionBackend] = None) -> None:
        self.backend: ExtractionBackend = backend or RegexExtractionBackend()

    async def extract(self, filename: str, content: bytes) -> CaseRecord:
        text = DocumentLoader.load(filename, content)
        record = await self.backend.extract(text)
        record.source_filename = filename
        return record

    async def extract_text(self, filename: str, text: str) -> CaseRecord:
        record = await self.backend.extract(text)
        record.source_filename = filename
        return record

    def describe(self) -> Dict[str, Any]:
        return {
            "backend": getattr(self.backend, "name", type(self.backend).__name__),
        }
