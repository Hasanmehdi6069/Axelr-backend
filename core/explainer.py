# core/explainer.py
"""
AXELR — AI-Orchestrated Instant Technical Explainer (F46)
============================================================
Detects stack traces and error strings in model output, then fires one
bounded provider call to generate a short structured explanation.
Output is capped at 512 tokens — never a long-form generation.

Public:
    detect_error(text) -> ErrorSignature | None
    explain(error, *, route_fn, language) -> dict
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger("axelr.explainer")

RouteFn = Callable[..., Awaitable[dict[str, Any]]]

_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\):[\s\S]{0,3000}")
_JS_ERROR_RE = re.compile(r"^\s*(?:Uncaught\s+)?([A-Z][A-Za-z]+Error):\s*(.+)$",
                           re.MULTILINE)
_HTTP_5XX_RE = re.compile(r"\b(50[0-9])\b[^\n]{0,120}")
_PY_EXC_RE = re.compile(
    r"^([A-Z][A-Za-z]+(?:Error|Exception|Warning)):\s*(.+)$", re.MULTILINE,
)

_MAX_SIGNATURE_CHARS = 1200

_EXPLAIN_PROMPT = """\
<instant_explainer>
You are a senior engineer. Explain this error in ≤ 80 words.

Error:
{signature}

Return ONLY a compact JSON object with these exact keys:
{{"cause": "<one sentence>", "fix": "<one sentence>", "guard": "<one line code or check>"}}
No prose before or after. No markdown fence.
</instant_explainer>"""


@dataclass(frozen=True, slots=True)
class ErrorSignature:
    kind: str              # traceback | js_error | py_exception | http_5xx
    text: str
    line: int = 0


def detect_error(text: str) -> ErrorSignature | None:
    """Find the first error in `text`. Never raises."""
    if not text:
        return None
    m = _TRACEBACK_RE.search(text)
    if m:
        return ErrorSignature("traceback", m.group(0)[:_MAX_SIGNATURE_CHARS])
    m = _JS_ERROR_RE.search(text)
    if m:
        return ErrorSignature("js_error", m.group(0)[:_MAX_SIGNATURE_CHARS])
    m = _PY_EXC_RE.search(text)
    if m:
        return ErrorSignature("py_exception", m.group(0)[:_MAX_SIGNATURE_CHARS])
    m = _HTTP_5XX_RE.search(text)
    if m:
        return ErrorSignature("http_5xx", m.group(0)[:_MAX_SIGNATURE_CHARS])
    return None


async def explain(
    signature: ErrorSignature,
    *,
    route_fn: RouteFn,
    language: str = "en",
    tier: str = "free",
    user: dict | None = None,
) -> dict[str, Any]:
    """One bounded provider call. Falls back to a static explanation."""
    prompt = _EXPLAIN_PROMPT.format(signature=signature.text)
    try:
        result = await route_fn(
            workspace="core", task_type="touch_fix", prompt=prompt,
            history=[], files=[], max_tokens=512, temp=0.1,
            tier=tier, user=user, context="",
        )
    except Exception as e:
        logger.debug("explainer_route_failed error=%s", e)
        return _static(signature)

    if not isinstance(result, dict) or not result.get("success"):
        return _static(signature)

    text = (result.get("text") or "").strip()
    import json
    try:
        # Allow the model to have wrapped it in a fence anyway
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return _static(signature)
        data = json.loads(m.group(0))
        if not isinstance(data, dict):
            return _static(signature)
        return {
            "success": True,
            "kind": signature.kind,
            "cause": str(data.get("cause", ""))[:300],
            "fix": str(data.get("fix", ""))[:300],
            "guard": str(data.get("guard", ""))[:200],
            "source": "llm",
        }
    except Exception:
        return _static(signature)


def _static(signature: ErrorSignature) -> dict[str, Any]:
    return {
        "success": True,
        "kind": signature.kind,
        "cause": "Error detected; no live explanation available.",
        "fix": "Inspect the traceback and check the referenced line.",
        "guard": "Add an assertion or explicit try/except around the call site.",
        "source": "static",
    }


__all__ = ["ErrorSignature", "detect_error", "explain"]