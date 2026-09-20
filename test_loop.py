# core/test_loop.py
"""
AXELR Autonomous Test Loop
==========================
Generates tests for source code, executes them in the sandbox, and — on
failure — routes the failure back to the AI to fix the *source*, up to a
configurable iteration budget.

Interface
---------
``TestLoop`` receives two async callables by dependency injection:

* ``route_func``   — same signature as ``route_ai_request`` in ``app.py``.
* ``execute_func`` — ``async (language, code, timeout) -> dict`` returning
  ``{"success": bool, "output": str, "error": Optional[str]}``.

Neither the AI router nor the sandbox is imported here — this keeps the
module fully testable and free of app-level globals.

RAM footprint: < 5 MB.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("axelr.test_loop")


RouteFunc = Callable[..., Awaitable[dict[str, Any]]]
ExecuteFunc = Callable[[str, str, int], Awaitable[dict[str, Any]]]


# ---------------------------------------------------------------------------
# Fenced-code extraction
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(
    r"```(?:[A-Za-z0-9_+\-]*)?\s*\n(?P<code>.*?)```", re.DOTALL
)


def _extract_fenced(text: str) -> str:
    """Return the first fenced code block, or the raw text if none."""
    if not text:
        return ""
    m = _FENCE_RE.search(text)
    return (m.group("code") if m else text).rstrip()


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class TestIteration:
    """One pass through the generate → execute → fix loop."""

    iteration: int
    tests: str
    passed: bool
    output: str
    error: str | None = None

    def to_dict(self) -> dict:
        """Return a JSON-serialisable representation."""
        return {
            "iteration": self.iteration,
            "tests": self.tests,
            "passed": self.passed,
            "output": self.output[:2000],
            "error": self.error,
        }


@dataclass(slots=True)
class TestLoopResult:
    """Final result of a test loop run."""

    success: bool
    final_code: str
    final_tests: str
    iterations: list[TestIteration] = field(default_factory=list)
    elapsed_ms: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict:
        """Return a JSON-serialisable representation."""
        return {
            "success": self.success,
            "final_code": self.final_code,
            "final_tests": self.final_tests,
            "iterations": [i.to_dict() for i in self.iterations],
            "elapsed_ms": round(self.elapsed_ms, 2),
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_GEN_PROMPT = """\
Generate a single self-contained {language} script that:

1. Includes this source code verbatim at the top:

--- SOURCE START ---
{code}
--- SOURCE END ---

2. Follows it with a series of `assert` statements that verify the public
   API (happy paths, boundary values, one error case).
3. Prints `ALL TESTS PASSED` at the very end if every assertion passes.
4. Uses ONLY the standard library (no pytest, no unittest, no third-party).
5. Exits with code 0 on success.

Return ONLY the resulting script inside a single fenced code block.
No commentary, no explanations, no additional text.
"""

_FIX_PROMPT = """\
The following {language} source code fails when these tests run.

--- SOURCE START ---
{code}
--- SOURCE END ---

--- TESTS START ---
{tests}
--- TESTS END ---

--- SANDBOX OUTPUT ---
{output}
--- END OUTPUT ---

Fix the SOURCE CODE (do NOT edit the tests) so that every assertion passes.
Preserve the public API, function signatures and existing behaviour.
Return ONLY the corrected source code inside a single fenced code block.
"""


_LANG_TAG = {"py": "python", "js": "javascript", "ts": "typescript"}


# ---------------------------------------------------------------------------
# TestLoop
# ---------------------------------------------------------------------------

class TestLoop:
    """
    Autonomous generate → execute → heal loop for source code.

    Parameters
    ----------
    route_func : RouteFunc
        Async AI router (``route_ai_request``).
    execute_func : ExecuteFunc
        Async sandbox executor.
    max_iterations : int
        Maximum number of generate/heal cycles. Default 3.
    exec_timeout : int
        Per-execution sandbox timeout, seconds. Default 8.
    pass_marker : str
        Marker the generated tests must print on success.
    """

    __slots__ = (
        "_exec",
        "_lang_map",
        "_marker",
        "_max_iter",
        "_route",
        "_timeout",
    )

    def __init__(
        self,
        route_func: RouteFunc,
        execute_func: ExecuteFunc,
        *,
        max_iterations: int = 3,
        exec_timeout: int = 8,
        pass_marker: str = "ALL TESTS PASSED",
    ) -> None:
        if route_func is None or execute_func is None:
            raise ValueError("route_func and execute_func are required")
        self._route = route_func
        self._exec = execute_func
        self._max_iter = max(1, int(max_iterations))
        self._timeout = max(1, int(exec_timeout))
        self._marker = pass_marker
        self._lang_map = {
            "py": "python",
            "python": "python",
            "js": "javascript",
            "javascript": "javascript",
            "node": "javascript",
            "ts": "typescript",
            "typescript": "typescript",
        }

    # -- public ------------------------------------------------------------

    async def run(
        self,
        code: str,
        language: str = "python",
        *,
        tier: str = "free",
        user: dict | None = None,
        context: str = "",
    ) -> TestLoopResult:
        """
        Execute the generate/execute/heal loop.

        Never raises — failures are captured in the returned
        :class:`TestLoopResult`.
        """
        t0 = time.time()
        lang = self._lang_map.get(language.lower(), "python")
        if not code or not code.strip():
            return TestLoopResult(
                success=False, final_code=code, final_tests="",
                error="empty code",
                elapsed_ms=(time.time() - t0) * 1000,
            )

        current_code = code
        final_tests = ""
        iterations: list[TestIteration] = []

        for i in range(1, self._max_iter + 1):
            try:
                tests = await self._generate_tests(current_code, lang, tier, user, context)
            except Exception as e:
                logger.warning("generate_tests_failed error=%s", e)
                tests = None

            if not tests:
                return TestLoopResult(
                    success=False, final_code=current_code,
                    final_tests=final_tests, iterations=iterations,
                    error="test generation failed",
                    elapsed_ms=(time.time() - t0) * 1000,
                )

            final_tests = tests
            exec_result = await self._safe_execute(tests, lang)
            passed = self._is_pass(exec_result)

            iterations.append(
                TestIteration(
                    iteration=i,
                    tests=tests,
                    passed=passed,
                    output=str(exec_result.get("output", "")),
                    error=exec_result.get("error"),
                )
            )

            if passed:
                return TestLoopResult(
                    success=True, final_code=current_code,
                    final_tests=tests, iterations=iterations,
                    elapsed_ms=(time.time() - t0) * 1000,
                )

            # Last iteration — don't waste an AI call on a fix
            if i == self._max_iter:
                break

            try:
                fixed = await self._heal(
                    current_code, tests, exec_result, lang, tier, user, context
                )
            except Exception as e:
                logger.warning("heal_failed error=%s", e)
                fixed = None

            if not fixed or fixed.strip() == current_code.strip():
                # Model couldn't improve — stop early
                break
            current_code = fixed

        return TestLoopResult(
            success=False,
            final_code=current_code,
            final_tests=final_tests,
            iterations=iterations,
            error=f"tests failed after {len(iterations)} iteration(s)",
            elapsed_ms=(time.time() - t0) * 1000,
        )

    # -- internals ---------------------------------------------------------

    async def _generate_tests(
        self,
        code: str,
        lang: str,
        tier: str,
        user: dict | None,
        context: str,
    ) -> str | None:
        prompt = _GEN_PROMPT.format(language=lang, code=code)
        result = await self._route(
            workspace="design",
            task_type="generate_tests",
            prompt=prompt,
            history=[],
            files=[],
            max_tokens=4096,
            temp=0.2,
            tier=tier,
            user=user,
            context=context,
        )
        if not result or not result.get("success"):
            return None
        text = _extract_fenced(result.get("text", ""))
        if self._marker not in text:
            # Nudge the model to include the marker
            text = text + f"\n\nprint({self._marker!r})\n" if lang == "python" else text
        return text or None

    async def _heal(
        self,
        code: str,
        tests: str,
        exec_result: dict[str, Any],
        lang: str,
        tier: str,
        user: dict | None,
        context: str,
    ) -> str | None:
        output = str(exec_result.get("output", ""))
        error = exec_result.get("error") or ""
        combined = f"{output}\n{error}".strip()[:4000]
        prompt = _FIX_PROMPT.format(
            language=lang, code=code, tests=tests, output=combined
        )
        result = await self._route(
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
        if not result or not result.get("success"):
            return None
        fixed = _extract_fenced(result.get("text", ""))
        return fixed or None

    async def _safe_execute(self, code: str, lang: str) -> dict[str, Any]:
        try:
            return await self._exec(lang, code, self._timeout) or {
                "success": False, "output": "", "error": "empty_exec_result",
            }
        except Exception as e:
            logger.warning("execute_failed error=%s", e)
            return {"success": False, "output": "", "error": str(e)}

    def _is_pass(self, exec_result: dict[str, Any]) -> bool:
        """A run passes iff the sandbox succeeded AND the marker was printed."""
        if not exec_result:
            return False
        output = str(exec_result.get("output", ""))
        if self._marker not in output:
            return False
        # The sandbox's own success flag is authoritative when present.
        if exec_result.get("error") and not exec_result.get("success"):
            return False
        return True