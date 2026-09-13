# self_healer.py
"""
AXELR Self-Healing Engine
==========================
Accepts broken code and an error message, routes to the AI backend with a
surgical block-fixing prompt, and returns the fixed code plus a unified diff.

No local linter execution. All diff computation uses ``difflib`` — stdlib only.
RAM footprint: < 5 MB.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional

__all__ = ["SelfHealer", "HealResult", "RouteFunc", "make_diff"]

RouteFunc = Callable[..., Awaitable[dict]]


# ---------------------------------------------------------------------------
# Prompt template — module-level so it can be tuned without code edits.
# ---------------------------------------------------------------------------

_BLOCK_FIX_PROMPT = """You are a senior {language} engineer.
The following {language} code has a bug or runtime error.

Error / symptom:
{error}

Fix ONLY the minimum necessary to resolve the issue. Preserve:
- Public API names
- Comments and formatting style
- Existing imports (unless they are the bug)

Return ONLY the corrected code inside a single fenced ```{lang_tag} block.
Do NOT add commentary, warnings, or explanations.

--- ORIGINAL CODE ---
{code}
--- END ORIGINAL CODE ---
"""


# ---------------------------------------------------------------------------
# Language → fence tag
# ---------------------------------------------------------------------------

_LANG_TAG: dict[str, str] = {
    "python": "python", "py": "python",
    "javascript": "javascript", "js": "javascript",
    "typescript": "typescript", "ts": "typescript",
    "tsx": "tsx", "jsx": "jsx",
    "html": "html", "css": "css",
    "go": "go", "rust": "rust", "rs": "rust",
    "java": "java", "ruby": "ruby", "rb": "ruby",
    "php": "php", "swift": "swift", "kotlin": "kotlin", "kt": "kotlin",
}


# Fenced-code extractor — captures the first ```lang ... ``` block.
_FENCE_RE = re.compile(
    r"```(?:[A-Za-z0-9_+\-]*)?\s*\n(?P<code>.*?)```",
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class HealResult:
    """Outcome of a self-heal attempt."""

    success: bool
    final_code: str
    diff: str = ""
    attempts: int = 0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        """Return a JSON-serialisable representation."""
        return {
            "success": self.success,
            "final_code": self.final_code,
            "diff": self.diff,
            "attempts": self.attempts,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Healer
# ---------------------------------------------------------------------------

class SelfHealer:
    """
    AI-driven code healer.

    Parameters
    ----------
    route_func : Callable
        An async function with the same signature as AXELR's
        ``route_ai_request``. Must accept keyword args:
        ``workspace, task_type, prompt, history, files, max_tokens, temp,
        tier, user, context`` and return a dict with at least ``success``
        and ``text``.
    max_retries : int
        Number of full-code retries if the block fix fails.
    """

    __slots__ = ("_route", "_max_retries")

    def __init__(self, route_func: RouteFunc, max_retries: int = 2) -> None:
        """Initialise the healer with a route function and retry budget."""
        if route_func is None:
            raise ValueError("route_func is required")
        self._route = route_func
        self._max_retries = max(1, int(max_retries))

    # -- public API ---------------------------------------------------------

    async def heal(
        self,
        code: str,
        error: str,
        *,
        language: str = "python",
        tier: str = "free",
        user: Optional[dict] = None,
        context: str = "",
    ) -> HealResult:
        """
        Attempt to heal the given code.

        Never raises — AI-side or network failures are returned as a
        ``HealResult`` with ``success=False``.
        """
        if not code or not code.strip():
            return HealResult(success=False, final_code=code, error="empty code")

        original = code
        last_error: Optional[str] = None
        current_error = error

        for attempt in range(1, self._max_retries + 1):
            prompt = self._build_prompt(original, current_error, language)

            try:
                response = await self._route(
                    workspace="design",
                    task_type="touch_fix",
                    prompt=prompt,
                    history=[],
                    files=[],
                    max_tokens=4096,
                    temp=0.15,
                    tier=tier,
                    user=user,
                    context=context,
                )
            except Exception as exc:
                last_error = f"route error: {exc}"
                continue

            if not response or not response.get("success"):
                last_error = str(response.get("text", "AI route returned no text"))
                continue

            fixed = self._extract_code(response.get("text", ""))
            if not fixed or fixed.strip() == original.strip():
                last_error = "AI returned unchanged code"
                # Nudge the model with the previous failure on the next attempt
                current_error = f"{current_error}\n\nPrevious attempt did not change the code."
                continue

            diff = self._unified_diff(original, fixed, language)
            return HealResult(
                success=True,
                final_code=fixed,
                diff=diff,
                attempts=attempt,
            )

        return HealResult(
            success=False,
            final_code=original,
            attempts=self._max_retries,
            error=last_error or "self-heal failed",
        )

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _build_prompt(code: str, error: str, language: str) -> str:
        """Render the surgical fix prompt."""
        lang = language.lower()
        return _BLOCK_FIX_PROMPT.format(
            language=language,
            lang_tag=_LANG_TAG.get(lang, lang),
            error=(error or "").strip()[:2000],
            code=code,
        )

    @staticmethod
    def _extract_code(text: str) -> str:
        """Extract the first fenced code block, or return the text as-is."""
        if not text:
            return ""
        match = _FENCE_RE.search(text)
        if match:
            return match.group("code").rstrip()
        # If the model returned bare code (no fences), use it directly.
        return text.strip()

    @staticmethod
    def _unified_diff(original: str, fixed: str, language: str) -> str:
        """Generate a unified diff — cheap, pure stdlib."""
        ext = _LANG_TAG.get(language.lower(), "txt")
        diff_lines = difflib.unified_diff(
            original.splitlines(keepends=True),
            fixed.splitlines(keepends=True),
            fromfile=f"original.{ext}",
            tofile=f"fixed.{ext}",
            n=3,
        )
        return "".join(diff_lines)


# ---------------------------------------------------------------------------
# Standalone diff helper (for callers that only need the diff)
# ---------------------------------------------------------------------------

def make_diff(original: str, fixed: str, label: str = "txt") -> str:
    """Return a unified diff between two code strings."""
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            fixed.splitlines(keepends=True),
            fromfile=f"original.{label}",
            tofile=f"fixed.{label}",
            n=3,
        )
    )