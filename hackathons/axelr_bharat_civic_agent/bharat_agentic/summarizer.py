"""Template-only bilingual summaries — no free-text generation.

Never calls a language model. Composes exactly three sentences per language
from the structured fields returned by :mod:`bharat_agentic.extraction`. If a
critical field is uncertain, the summary says so explicitly.
"""
from __future__ import annotations

from datetime import date
from typing import Dict

from .models import CaseRecord, CaseSummary, Confidence

_EN_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_HI_MONTHS = [
    "जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून",
    "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर",
]

_EN_ACTIONS: Dict[str, str] = {
    "appear in person": "Please appear in person on that date.",
    "produce documents": "Please bring the required original documents.",
    "file reply": "Please file your reply before the hearing date.",
    "pay fine": "Please pay the outstanding fine before the hearing.",
    "settle matter": "Please appear for settlement of the matter.",
    "serve notice": "Please keep the served notice safe for reference.",
    "furnish bond": "Please furnish the required bond before the hearing.",
}
_HI_ACTIONS: Dict[str, str] = {
    "appear in person": "कृपया उस दिन व्यक्तिगत रूप से उपस्थित हों।",
    "produce documents": "कृपया आवश्यक मूल दस्तावेज़ साथ लाएँ।",
    "file reply": "कृपया सुनवाई से पहले अपना उत्तर दाखिल करें।",
    "pay fine": "कृपया सुनवाई से पहले निर्धारित जुर्माना भरें।",
    "settle matter": "कृपया मामला सुलझाने के लिए उपस्थित हों।",
    "serve notice": "कृपया तामील की गई नोटिस सुरक्षित रखें।",
    "furnish bond": "कृपया सुनवाई से पहले आवश्यक बॉन्ड प्रस्तुत करें।",
}

_EN_REVIEW = ("Some fields are uncertain — please verify them against the "
              "original document.")
_HI_REVIEW = "कुछ विवरण अनिश्चित हैं — कृपया मूल दस्तावेज़ से सत्यापित करें।"
_EN_CLOSING = ("Keep this summary and the original notice together for "
               "your records.")
_HI_CLOSING = "कृपया यह सारांश और मूल नोटिस साथ में सुरक्षित रखें।"


def _format_en(d: date) -> str:
    return f"{d.day} {_EN_MONTHS[d.month - 1]} {d.year}"


def _format_hi(d: date) -> str:
    return f"{d.day} {_HI_MONTHS[d.month - 1]} {d.year}"


def _safe_date(value: str):
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


class CaseSummarizer:
    """Composes exactly three sentences per language."""

    def summarise(self, record: CaseRecord) -> CaseSummary:
        english = self._compose_en(record)
        hindi = self._compose_hi(record)
        record.summary_en = english
        record.summary_hi = hindi
        return CaseSummary(
            case_id=record.case_id,
            english=english,
            hindi=hindi,
            template_only=True,
        )

    # ----- English -----

    def _compose_en(self, record: CaseRecord) -> str:
        subject = self._subject_en(record)
        hearing_date = (
            _format_en(_safe_date(record.next_hearing.value))
            if record.next_hearing.value and _safe_date(record.next_hearing.value) and record.next_hearing.level is not Confidence.LOW
            else None)
        if hearing_date:
            s1 = f"{subject} is listed for its next hearing on {hearing_date}."
        else:
            s1 = f"{subject} is pending; the next hearing date could not be read."

        action = record.required_action.value
        s2 = _EN_ACTIONS.get(action, "Please check the original notice for required actions.")

        if record.needs_review or not record.next_hearing.value or record.next_hearing.level is Confidence.LOW:
            s3 = _EN_REVIEW
        else:
            s3 = _EN_CLOSING
        return f"{s1} {s2} {s3}"

    def _subject_en(self, record: CaseRecord) -> str:
        cn = record.case_number
        court = record.court
        if cn.value and cn.level is not Confidence.LOW:
            subject = f"Your case {cn.value}"
        else:
            subject = "Your case"
        if court.value and court.level is not Confidence.LOW:
            subject += f" at {court.value}"
        return subject

    # ----- Hindi -----

    def _compose_hi(self, record: CaseRecord) -> str:
        subject = self._subject_hi(record)
        hearing_date = (
            _format_hi(_safe_date(record.next_hearing.value))
            if record.next_hearing.value and _safe_date(record.next_hearing.value) and record.next_hearing.level is not Confidence.LOW
            else None)
        if hearing_date:
            s1 = f"{subject} विचाराधीन है और अगली सुनवाई {hearing_date} को निर्धारित है।"
        else:
            s1 = f"{subject} विचाराधीन है; अगली सुनवाई की तारीख पढ़ी नहीं जा सकी।"

        action = record.required_action.value
        s2 = _HI_ACTIONS.get(action, "कृपया आवश्यक कार्रवाई के लिए मूल नोटिस देखें।")

        if record.needs_review or not record.next_hearing.value or record.next_hearing.level is Confidence.LOW:
            s3 = _HI_REVIEW
        else:
            s3 = _HI_CLOSING
        return f"{s1} {s2} {s3}"

    def _subject_hi(self, record: CaseRecord) -> str:
        cn = record.case_number
        court = record.court
        if cn.value and cn.level is not Confidence.LOW:
            subject = f"आपका मुकदमा {cn.value}"
        else:
            subject = "आपका मुकदमा"
        if court.value and court.level is not Confidence.LOW:
            subject += f" {court.value} में"
        else:
            subject += " न्यायालय में"
        return subject
