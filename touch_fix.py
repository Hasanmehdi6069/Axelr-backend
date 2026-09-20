# core/touch_fix.py
"""
AXELR Touch-Fix Engine
======================
Surgical code patching: only the user-selected block is sent to the AI,
cutting token usage by 80%+ on large files.

Public API:
    * ``apply_diff(code, diff_text)``    — apply a unified diff.
    * ``fix_block(full_code, error_block, error_message)`` — AI-assisted
      block-level repair with unified-diff output.

The AI route function is injected via the constructor (dependency
injection). Every method is defensive: on any failure, the original
code is returned unchanged. Never raises.

RAM footprint: < 2 MB.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Awaitable, Callable

__all__ = ["TouchFixEngine"]

RouteFunc = Callable[..., Awaitable[dict]]

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FENCE_RE = re.compile(r"```[A-Za-z0-9_+\-]*\s*\n(.*?)```", re.DOTALL)

_LANG_TAG = {
    "python": "python", "py": "python",
    "javascript": "javascript", "js": "javascript",
    "typescript": "typescript", "ts": "typescript",
    "html": "html", "css": "css", "json": "json",
    "go": "go", "rust": "rust", "rs": "rust",
    "java": "java", "ruby": "ruby", "rb": "ruby",
    "php": "php", "kotlin": "kotlin", "kt": "kotlin",
}

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

--- ORIGINAL BLOCK ---
{code}
--- END ORIGINAL BLOCK ---
"""


class TouchFixEngine:
    """Surgical patching engine. Never raises."""

    __slots__ = ("_route",)

    def __init__(self, route_func: RouteFunc | None = None) -> None:
        """Inject the AI route function. May be ``None`` for diff-only use."""
        self._route = route_func

    # -- public API ---------------------------------------------------------

    def apply_diff(self, code: str, diff_text: str) -> str:
        """Apply a unified diff to ``code``. Returns original on failure."""
        if not code or not diff_text:
            return code
        try:
            return self._apply_unified_diff(code, diff_text)
        except Exception:
            return code

    async def fix_block(
        self,
        full_code: str,
        error_block: str,
        error_message: str,
        *,
        language: str = "python",
        tier: str = "free",
        user: dict | None = None,
    ) -> str:
        """Fix only ``error_block`` inside ``full_code``. Never raises."""
        if not full_code or not error_block:
            return full_code
        try:
            return await self._fix_block_inner(
                full_code, error_block, error_message, language, tier, user
            )
        except Exception:
            return full_code

    # -- internals ----------------------------------------------------------

    async def _fix_block_inner(
        self,
        full_code: str,
        error_block: str,
        error_message: str,
        language: str,
        tier: str,
        user: dict | None,
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
            fromfile="block",
            tofile="fixed",
            n=3,
        ))
        if diff:
            return self._apply_unified_diff(full_code, diff)
        return full_code

    @staticmethod
    def _locate_block(full_code: str, error_block: str) -> tuple[int, int]:
        """Locate ``error_block`` inside ``full_code``. Returns (start, end)."""
        code_lines = full_code.splitlines()
        block_lines = error_block.strip().splitlines()
        if not block_lines:
            return 0, len(code_lines)

        # 1. Exact match (whitespace-tolerant)
        stripped_block = [l.rstrip() for l in block_lines]
        n = len(block_lines)
        for i in range(len(code_lines) - n + 1):
            window = [l.rstrip() for l in code_lines[i : i + n]]
            if window == stripped_block:
                return i, i + n

        # 2. Anchor on first meaningful line, walk by indentation
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

        # 3. Fallback: whole file
        return 0, len(code_lines)

    async def _ai_fix_block(
        self,
        error_block: str,
        error_message: str,
        language: str,
        tier: str,
        user: dict | None,
    ) -> str | None:
        if self._route is None:
            return None

        lang_tag = _LANG_TAG.get(language.lower(), "text")
        prompt = _BLOCK_FIX_PROMPT.format(
            language=language,
            lang_tag=lang_tag,
            error=(error_message or "").strip()[:2000],
            code=error_block,
        )

        try:
            result = await self._route(
                workspace="design",
                task_type="touch_fix",
                prompt=prompt,
                history=[],
                files=[],
                max_tokens=2048,
                temp=0.2,
                tier=tier,
                user=user,
                context="",
            )
        except Exception:
            return None

        if not isinstance(result, dict) or not result.get("success"):
            return None
        text = result.get("text") or ""
        if not text:
            return None

        match = _FENCE_RE.search(text)
        if match:
            return match.group(1).rstrip()
        return text.strip()

    @staticmethod
    def _apply_unified_diff(code: str, diff_text: str) -> str:
        """Parse and apply a unified diff. Raises on malformed input."""
        code_lines: list[str] = code.split("\n")
        diff_lines: list[str] = diff_text.split("\n")

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
                    pass                       # "\ No newline at end of file"
                i += 1

        result.extend(code_lines[pos:])
        return "\n".join(result)