# core/healing.py
"""
AXELR Self-Healing Engine + Touch-Fix Engine
============================================
Combined code healing engines:
- SelfHealer: Full code self-healing with retries
- TouchFixEngine: Surgical block-level patching for minimal token usage

Pure stdlib (difflib + re). RAM footprint < 7 MB.
"""
from __future__ import annotations

import difflib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

# Shared route-function type alias (used by SelfHealer, TestLoop).
RouteFunc = Callable[..., Awaitable[dict]]


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

_LANG_TAG: dict[str, str] = {
    "python": "python", "py": "python",
    "javascript": "javascript", "js": "javascript",
    "typescript": "typescript", "ts": "typescript",
    "tsx": "tsx", "jsx": "jsx",
    "html": "html", "css": "css", "json": "json",
    "go": "go", "rust": "rust", "rs": "rust",
    "java": "java", "ruby": "ruby", "rb": "ruby",
    "php": "php", "swift": "swift", "kotlin": "kotlin", "kt": "kotlin",
}

_FENCE_RE = re.compile(r"```[A-Za-z0-9_+\-]*\s*\n(.*?)```", re.DOTALL)
_HUNK_RE  = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(slots=True)
class HealResult:
    success: bool
    final_code: str
    diff: str = ""
    attempts: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "final_code": self.final_code,
            "diff": self.diff,
            "attempts": self.attempts,
            "error": self.error,
        }


class SelfHealer:
    __slots__ = ("_max_retries", "_route")

    def __init__(self, route_func: RouteFunc, max_retries: int = 2) -> None:
        if route_func is None:
            raise ValueError("route_func is required")
        self._route = route_func
        self._max_retries = max(1, int(max_retries))

    async def heal(
        self, code: str, error: str, *,
        language: str = "python", tier: str = "free",
        user: dict | None = None, context: str = "",
    ) -> HealResult:
        if not code or not code.strip():
            return HealResult(success=False, final_code=code, error="empty code")

        original = code
        last_error: str | None = None
        current_error = error

        for attempt in range(1, self._max_retries + 1):
            prompt = self._build_prompt(original, current_error, language)
            try:
                response = await self._route(
                    workspace="design", task_type="touch_fix",
                    prompt=prompt, history=[], files=[],
                    max_tokens=4096, temp=0.15, tier=tier,
                    user=user, context=context,
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
                current_error = f"{current_error}\n\nPrevious attempt did not change the code."
                continue

            diff = self._unified_diff(original, fixed, language)
            return HealResult(success=True, final_code=fixed, diff=diff, attempts=attempt)

        return HealResult(
            success=False, final_code=original,
            attempts=self._max_retries,
            error=last_error or "self-heal failed",
        )

    @staticmethod
    def _build_prompt(code: str, error: str, language: str) -> str:
        lang = language.lower()
        return _BLOCK_FIX_PROMPT.format(
            language=language, lang_tag=_LANG_TAG.get(lang, lang),
            error=(error or "").strip()[:2000], code=code,
        )

    @staticmethod
    def _extract_code(text: str) -> str:
        if not text:
            return ""
        m = _FENCE_RE.search(text)
        return (m.group(1).rstrip() if m else text.strip())

    @staticmethod
    def _unified_diff(original: str, fixed: str, language: str) -> str:
        ext = _LANG_TAG.get(language.lower(), "txt")
        return "".join(difflib.unified_diff(
            original.splitlines(keepends=True),
            fixed.splitlines(keepends=True),
            fromfile=f"original.{ext}", tofile=f"fixed.{ext}", n=3,
        ))


class TouchFixEngine:
    """Surgical block-level patcher. Never raises."""

    __slots__ = ("_route",)

    def __init__(self, route_func: RouteFunc | None = None) -> None:
        self._route = route_func

    def apply_diff(self, code: str, diff_text: str) -> str:
        if not code or not diff_text:
            return code
        try:
            return self._apply_unified_diff(code, diff_text)
        except Exception:
            return code

    async def fix_block(
        self, full_code: str, error_block: str, error_message: str, *,
        language: str = "python", tier: str = "free", user: dict | None = None,
    ) -> str:
        if not full_code or not error_block:
            return full_code
        try:
            return await self._fix_block_inner(
                full_code, error_block, error_message, language, tier, user
            )
        except Exception:
            return full_code

    async def _fix_block_inner(
        self, full_code: str, error_block: str, error_message: str,
        language: str, tier: str, user: dict | None,
    ) -> str:
        start, end = self._locate_block(full_code, error_block)
        fixed_block = await self._ai_fix_block(
            error_block, error_message, language, tier, user
        )
        if not fixed_block or fixed_block == error_block:
            return full_code

        original_lines = full_code.splitlines(keepends=True)
        block_lines = original_lines[start:end]
        diff = "".join(difflib.unified_diff(
            block_lines,
            [l + "\n" for l in fixed_block.splitlines()],
            fromfile="block", tofile="fixed", n=3,
        ))
        if diff:
            return self._apply_unified_diff(full_code, diff)
        return full_code

    @staticmethod
    def _locate_block(full_code: str, error_block: str) -> tuple[int, int]:
        code_lines = full_code.splitlines()
        block_lines = error_block.strip().splitlines()
        if not block_lines:
            return 0, len(code_lines)
        stripped_block = [l.rstrip() for l in block_lines]
        n = len(block_lines)
        for i in range(len(code_lines) - n + 1):
            window = [l.rstrip() for l in code_lines[i : i + n]]
            if window == stripped_block:
                return i, i + n
        anchor = next((l.strip() for l in block_lines if l.strip()), "")
        if not anchor:
            return 0, len(code_lines)
        for i, line in enumerate(code_lines):
            if line.strip() != anchor:
                continue
            indent = len(block_lines[0]) - len(block_lines[0].lstrip())
            end = i + 1
            for j in range(i + 1, len(code_lines)):
                stripped = code_lines[j].strip()
                if not stripped:
                    end = j + 1
                    continue
                current_indent = len(code_lines[j]) - len(code_lines[j].lstrip())
                if current_indent <= indent:
                    break
                end = j + 1
            return i, end
        return 0, len(code_lines)

    async def _ai_fix_block(
        self, error_block: str, error_message: str,
        language: str, tier: str, user: dict | None,
    ) -> str | None:
        if self._route is None:
            return None
        lang_tag = _LANG_TAG.get(language.lower(), "text")
        prompt = _BLOCK_FIX_PROMPT.format(
            language=language, lang_tag=lang_tag,
            error=(error_message or "").strip()[:2000], code=error_block,
        )
        try:
            result = await self._route(
                workspace="design", task_type="touch_fix", prompt=prompt,
                history=[], files=[], max_tokens=2048, temp=0.2,
                tier=tier, user=user, context="",
            )
        except Exception:
            return None
        if not isinstance(result, dict) or not result.get("success"):
            return None
        text = result.get("text") or ""
        if not text:
            return None
        m = _FENCE_RE.search(text)
        return m.group(1).rstrip() if m else text.strip()

    @staticmethod
    def _apply_unified_diff(code: str, diff_text: str) -> str:
        code_lines = code.split("\n")
        diff_lines = diff_text.split("\n")
        result: list[str] = []
        pos = 0
        i = 0
        while i < len(diff_lines):
            m = _HUNK_RE.match(diff_lines[i])
            if not m:
                i += 1
                continue
            old_start = int(m.group(1))
            i += 1
            if old_start > 0:
                result.extend(code_lines[pos : old_start - 1])
            pos = old_start - 1
            while i < len(diff_lines):
                hline = diff_lines[i]
                if _HUNK_RE.match(hline):
                    break
                if not hline:
                    i += 1
                    continue
                first = hline[0]
                if first == "+":
                    result.append(hline[1:])
                elif first == "-":
                    pos += 1
                elif first == " ":
                    result.append(hline[1:])
                    pos += 1
                elif first == "\\":
                    pass
                i += 1
        result.extend(code_lines[pos:])
        return "\n".join(result)


def make_diff(original: str, fixed: str, label: str = "txt") -> str:
    return "".join(difflib.unified_diff(
        original.splitlines(keepends=True),
        fixed.splitlines(keepends=True),
        fromfile=f"original.{label}", tofile=f"fixed.{label}", n=3,
    ))


__all__ = [
    "SelfHealer", "TouchFixEngine", "HealResult", "make_diff", "RouteFunc",
]