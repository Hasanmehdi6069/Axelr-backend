<<<<<<< HEAD
# core/reflection.py
"""
AXELR — Token-Targeted Linear Reflection Loops (Cognitive Stage 4)
===================================================================
Two-part quality lock:

  1. `scan(text)`        — cheap local detectors. String-only. Never calls
                            the model. Runs on every final output.
  2. `repair(...)`       — ONE targeted micro-critique call per request,
                            ONLY when `scan` flags a genuine defect. The
                            repair prompt names the specific defect, so
                            the model never re-generates correct content.

Contract
--------
    from core.reflection import scan, repair
    r = scan(text)
    if r.needs_repair:
        new_text, meta = await repair(route_fn, ..., text=text)

Design
------
* `scan` costs ~5–30 µs. Safe on the hot path.
* `repair` is bounded: one call, one pass, ~18 s ceiling.
* Never raises — any failure returns the original text unchanged.
* No new providers, no new dependencies, no vector store.

RAM
---
  Constant. No caching. No state.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger("axelr.reflection")

__all__ = ["Reflection", "scan", "repair"]

RouteFn = Callable[..., Awaitable[dict[str, Any]]]


# ── Detector regexes (compiled once) ─────────────────────────────────────
_FENCE_RE = re.compile(r"```")
_REFUSAL_RE = re.compile(
    r"\b(i\s+can(?:'?t|not)|i\s+am\s+unable|as an ai|i'?m just an ai|"
    r"i'?m sorry,? (?:but )?i)\b",
    re.I,
)
_LEAK_RE = re.compile(
    r"</?(?:system_identity|core_directives|reasoning_protocol|"
    r"intent_veto|data_drift_defiance|modern_styling|"
    r"transactional_directive|extraction_directive|styling_directive|"
    r"strategy_directive|diagnostic_directive|"
    r"thinking|workspace_anchor|MASTER_PROMPT)>",
    re.I,
)
_PLACEHOLDER_RE = re.compile(
    r"(\[insert[^\]]*\]|<your[^>]*>|\bTODO\b|\bFIXME\b|"
    r"\bXXX\b|\.\.\.\s*(?:rest of|remaining|etc)|"
    r"\byour\s+\w+\s+here\b)",
    re.I,
)
_SELFCORRECT_RE = re.compile(
    r"(?:\bactually\b|\bwait\b|\blet me (?:re|correct)|\bcorrection:|\bno,?\s+wait\b)",
    re.I,
)
_TERMINAL_RE = re.compile(r"[.!?`)\]}\"'”’>]\s*$")
_OPEN_BRACKETS = (("(", ")"), ("[", "]"), ("{", "}"))

_MAX_REPAIR_INPUT = 8000


@dataclass(frozen=True, slots=True)
class Reflection:
    needs_repair: bool
    issues: tuple[str, ...]
    focus: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "needs_repair": self.needs_repair,
            "issues": list(self.issues),
            "focus": self.focus,
        }


def scan(text: str) -> Reflection:
    """Cheap local defect scan. Pure string analysis, never calls the model."""
    if not text:
        return Reflection(False, ())

    issues: list[str] = []

    # 1. Unclosed code fence.
    if text.count("```") % 2 == 1:
        issues.append("unclosed_fence")

    # 2. Refusal markers.
    if _REFUSAL_RE.search(text):
        issues.append("refusal")

    # 3. Prompt-leak markers.
    if _LEAK_RE.search(text):
        issues.append("prompt_leak")

    # 4. Placeholders.
    if _PLACEHOLDER_RE.search(text):
        issues.append("placeholder")

    # 5. Self-correction markers left in-text.
    if _SELFCORRECT_RE.search(text):
        issues.append("self_correction")

    # 6. Truncation: no terminal char at the tail.
    stripped = text.rstrip()
    if len(stripped) > 200 and not _TERMINAL_RE.search(stripped[-4:]):
        issues.append("possibly_truncated")

    # 7. Bracket imbalance inside the last code fence.
    tail_fence = stripped.rfind("```")
    if tail_fence != -1:
        tail = stripped[tail_fence:]
        for open_ch, close_ch in _OPEN_BRACKETS:
            if tail.count(open_ch) > tail.count(close_ch):
                issues.append(f"unbalanced_{open_ch}{close_ch}")
                break

    # Focus = the last code block, if any defect touched code; else tail.
    focus = ""
    if any(i.startswith(("unclosed_fence", "unbalanced_")) for i in issues):
        idx = stripped.rfind("```")
        if idx > 0:
            prev = stripped.rfind("```", 0, idx)
            if prev != -1:
                focus = stripped[prev:idx + 3][:1200]

    return Reflection(bool(issues), tuple(issues), focus)


def _build_repair_prompt(user_prompt: str, draft: str, r: Reflection) -> str:
    focus_block = f"\n<focus_span>\n{r.focus}\n</focus_span>\n" if r.focus else ""
    return (
        "<reflection_repair>\n"
        f"Detected issues: {', '.join(r.issues)}.\n"
        "Repair ONLY the flagged issues. Preserve every correct token.\n\n"
        "<original_request>\n"
        f"{user_prompt[:1500]}\n"
        "</original_request>\n"
        f"{focus_block}\n"
        "<draft_to_repair>\n"
        f"{draft[:_MAX_REPAIR_INPUT]}\n"
        "</draft_to_repair>\n\n"
        "Return the FULL corrected answer. No preamble. No meta-commentary.\n"
        "</reflection_repair>"
    )


async def repair(
    route_fn: RouteFn,
    *,
    workspace: str,
    task_type: str,
    prompt: str,
    text: str,
    max_tokens: int,
    temp: float,
    tier: str,
    user: dict | None = None,
    context: str = "",
    request: Any = None,
    timeout_s: float = 18.0,
) -> tuple[str, dict[str, Any]]:
    """
    Run one targeted reflection pass. Returns `(final_text, meta)`.
    On any failure returns `(original_text, meta_with_reason)`.
    """
    r = scan(text)
    meta: dict[str, Any] = {"repaired": False, "issues": list(r.issues)}
    if not r.needs_repair:
        return text, meta

    repair_prompt = _build_repair_prompt(prompt, text, r)
    try:
        res = await asyncio.wait_for(
            route_fn(
                workspace, task_type, repair_prompt,
                None, None, max_tokens, temp, tier,
                user, context, request,
            ),
            timeout=max(4.0, float(timeout_s)),
        )
    except asyncio.TimeoutError:
        meta["reason"] = "timeout"
        return text, meta
    except Exception as e:
        logger.debug("reflection_repair_failed error=%s", e)
        meta["reason"] = str(e)[:120]
        return text, meta

    if not isinstance(res, dict) or not res.get("success") or not res.get("text"):
        meta["reason"] = "empty"
        return text, meta

    repaired = str(res["text"])
    # Sanity guard: reject a repair that loses > 60 % of the original.
    if len(repaired) < 0.4 * len(text):
        meta["reason"] = "repair_shrunk"
        return text, meta

    meta["repaired"] = True
    meta["provider"] = res.get("provider")
    meta["model"] = res.get("model_used")
=======
# core/reflection.py
"""
AXELR — Token-Targeted Linear Reflection Loops (Cognitive Stage 4)
===================================================================
Two-part quality lock:

  1. `scan(text)`        — cheap local detectors. String-only. Never calls
                            the model. Runs on every final output.
  2. `repair(...)`       — ONE targeted micro-critique call per request,
                            ONLY when `scan` flags a genuine defect. The
                            repair prompt names the specific defect, so
                            the model never re-generates correct content.

Contract
--------
    from core.reflection import scan, repair
    r = scan(text)
    if r.needs_repair:
        new_text, meta = await repair(route_fn, ..., text=text)

Design
------
* `scan` costs ~5–30 µs. Safe on the hot path.
* `repair` is bounded: one call, one pass, ~18 s ceiling.
* Never raises — any failure returns the original text unchanged.
* No new providers, no new dependencies, no vector store.

RAM
---
  Constant. No caching. No state.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger("axelr.reflection")

__all__ = ["Reflection", "scan", "repair"]

RouteFn = Callable[..., Awaitable[dict[str, Any]]]


# ── Detector regexes (compiled once) ─────────────────────────────────────
_FENCE_RE = re.compile(r"```")
_REFUSAL_RE = re.compile(
    r"\b(i\s+can(?:'?t|not)|i\s+am\s+unable|as an ai|i'?m just an ai|"
    r"i'?m sorry,? (?:but )?i)\b",
    re.I,
)
_LEAK_RE = re.compile(
    r"</?(?:system_identity|core_directives|reasoning_protocol|"
    r"intent_veto|data_drift_defiance|modern_styling|"
    r"transactional_directive|extraction_directive|styling_directive|"
    r"strategy_directive|diagnostic_directive|"
    r"thinking|workspace_anchor|MASTER_PROMPT)>",
    re.I,
)
_PLACEHOLDER_RE = re.compile(
    r"(\[insert[^\]]*\]|<your[^>]*>|\bTODO\b|\bFIXME\b|"
    r"\bXXX\b|\.\.\.\s*(?:rest of|remaining|etc)|"
    r"\byour\s+\w+\s+here\b)",
    re.I,
)
_SELFCORRECT_RE = re.compile(
    r"(?:\bactually\b|\bwait\b|\blet me (?:re|correct)|\bcorrection:|\bno,?\s+wait\b)",
    re.I,
)
_TERMINAL_RE = re.compile(r"[.!?`)\]}\"'”’>]\s*$")
_OPEN_BRACKETS = (("(", ")"), ("[", "]"), ("{", "}"))

_MAX_REPAIR_INPUT = 8000


@dataclass(frozen=True, slots=True)
class Reflection:
    needs_repair: bool
    issues: tuple[str, ...]
    focus: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "needs_repair": self.needs_repair,
            "issues": list(self.issues),
            "focus": self.focus,
        }


def scan(text: str) -> Reflection:
    """Cheap local defect scan. Pure string analysis, never calls the model."""
    if not text:
        return Reflection(False, ())

    issues: list[str] = []

    # 1. Unclosed code fence.
    if text.count("```") % 2 == 1:
        issues.append("unclosed_fence")

    # 2. Refusal markers.
    if _REFUSAL_RE.search(text):
        issues.append("refusal")

    # 3. Prompt-leak markers.
    if _LEAK_RE.search(text):
        issues.append("prompt_leak")

    # 4. Placeholders.
    if _PLACEHOLDER_RE.search(text):
        issues.append("placeholder")

    # 5. Self-correction markers left in-text.
    if _SELFCORRECT_RE.search(text):
        issues.append("self_correction")

    # 6. Truncation: no terminal char at the tail.
    stripped = text.rstrip()
    if len(stripped) > 200 and not _TERMINAL_RE.search(stripped[-4:]):
        issues.append("possibly_truncated")

    # 7. Bracket imbalance inside the last code fence.
    tail_fence = stripped.rfind("```")
    if tail_fence != -1:
        tail = stripped[tail_fence:]
        for open_ch, close_ch in _OPEN_BRACKETS:
            if tail.count(open_ch) > tail.count(close_ch):
                issues.append(f"unbalanced_{open_ch}{close_ch}")
                break

    # Focus = the last code block, if any defect touched code; else tail.
    focus = ""
    if any(i.startswith(("unclosed_fence", "unbalanced_")) for i in issues):
        idx = stripped.rfind("```")
        if idx > 0:
            prev = stripped.rfind("```", 0, idx)
            if prev != -1:
                focus = stripped[prev:idx + 3][:1200]

    return Reflection(bool(issues), tuple(issues), focus)


def _build_repair_prompt(user_prompt: str, draft: str, r: Reflection) -> str:
    focus_block = f"\n<focus_span>\n{r.focus}\n</focus_span>\n" if r.focus else ""
    return (
        "<reflection_repair>\n"
        f"Detected issues: {', '.join(r.issues)}.\n"
        "Repair ONLY the flagged issues. Preserve every correct token.\n\n"
        "<original_request>\n"
        f"{user_prompt[:1500]}\n"
        "</original_request>\n"
        f"{focus_block}\n"
        "<draft_to_repair>\n"
        f"{draft[:_MAX_REPAIR_INPUT]}\n"
        "</draft_to_repair>\n\n"
        "Return the FULL corrected answer. No preamble. No meta-commentary.\n"
        "</reflection_repair>"
    )


async def repair(
    route_fn: RouteFn,
    *,
    workspace: str,
    task_type: str,
    prompt: str,
    text: str,
    max_tokens: int,
    temp: float,
    tier: str,
    user: dict | None = None,
    context: str = "",
    request: Any = None,
    timeout_s: float = 18.0,
) -> tuple[str, dict[str, Any]]:
    """
    Run one targeted reflection pass. Returns `(final_text, meta)`.
    On any failure returns `(original_text, meta_with_reason)`.
    """
    r = scan(text)
    meta: dict[str, Any] = {"repaired": False, "issues": list(r.issues)}
    if not r.needs_repair:
        return text, meta

    repair_prompt = _build_repair_prompt(prompt, text, r)
    try:
        res = await asyncio.wait_for(
            route_fn(
                workspace, task_type, repair_prompt,
                None, None, max_tokens, temp, tier,
                user, context, request,
            ),
            timeout=max(4.0, float(timeout_s)),
        )
    except asyncio.TimeoutError:
        meta["reason"] = "timeout"
        return text, meta
    except Exception as e:
        logger.debug("reflection_repair_failed error=%s", e)
        meta["reason"] = str(e)[:120]
        return text, meta

    if not isinstance(res, dict) or not res.get("success") or not res.get("text"):
        meta["reason"] = "empty"
        return text, meta

    repaired = str(res["text"])
    # Sanity guard: reject a repair that loses > 60 % of the original.
    if len(repaired) < 0.4 * len(text):
        meta["reason"] = "repair_shrunk"
        return text, meta

    meta["repaired"] = True
    meta["provider"] = res.get("provider")
    meta["model"] = res.get("model_used")
>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
    return repaired, meta