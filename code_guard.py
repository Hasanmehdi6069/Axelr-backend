# code_guard.py
"""
AXELR Code Security Scanner (CodeGuard)
========================================
Pure-Python + regex security scanner for source code.

Detects:
    - SQL injection patterns
    - XSS sinks (innerHTML, dangerouslySetInnerHTML, document.write, eval)
    - Command injection (os.system, subprocess with f-strings)
    - Hardcoded secrets (AWS, GCP, GitHub, Stripe, private keys, generic Bearer)
    - Unsafe pickle.loads / yaml.load (missing Loader=)
    - Path traversal (../ in open/join)
    - Python syntax validation via `ast`

Zero heavy dependencies. All regex patterns are pre-compiled at import.
RAM footprint: < 10 MB.
"""

from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass, field
from typing import List, Literal, Optional, Tuple

__all__ = ["CodeGuard", "Finding", "ScanResult", "Severity", "default_guard"]

Severity = Literal["low", "medium", "high", "critical"]


# ---------------------------------------------------------------------------
# Pre-compiled patterns (compiled once at import — cheap, ~1 MB total)
# ---------------------------------------------------------------------------

_SQL_INJECTION = re.compile(
    r"""(?ix)
    (?: \b(?: select|insert|update|delete|drop|union|alter) \b .{0,120}? )
    (?: \$\{ | %s | \+\s*[a-z_] | f["'].*\{ )
    """,
    re.DOTALL,
)

_CMD_INJECTION = re.compile(
    r"""(?ix)
    (?: os\.system|os\.popen|subprocess\.(?:call|run|Popen|check_output)|
        commands\.getoutput )
    \s*\(
    (?:
        f["'][^"']*\{[^}]+\}
        | [^)]*\+[^)]*
        | [^)]*%\s*[a-z_]
    )
    """,
    re.DOTALL,
)

_XSS_SINK = re.compile(
    r"""(?ix)
    (?:
        \.innerHTML \s*=
        | \.outerHTML \s*=
        | document\.write \s*\(
        | dangerouslySetInnerHTML
        | \.insertAdjacentHTML \s*\(
        | eval \s*\(
    )
    """,
)

_EVAL_USAGE = re.compile(r"(?<![A-Za-z0-9_])eval\s*\(")
_EXEC_USAGE = re.compile(r"(?<![A-Za-z0-9_])exec\s*\(")
_PICKLE_LOAD = re.compile(r"pickle\.loads?\s*\(")
_YAML_LOAD = re.compile(r"yaml\.load\s*\((?![^)]*Loader\s*=)")

_PATH_TRAVERSAL = re.compile(
    r"""(?ix)
    (?:
        open \s*\( [^)]* \.\./
        | os\.path\.join \s*\( [^)]* (?:request|input|user|param)
        | \.\./\.\./
    )
    """,
)

_SECRET_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("AWS Access Key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Google API Key", re.compile(r"AIza[0-9A-Za-z\-_]{35}")),
    ("Slack Token", re.compile(r"xox[baprs]-[0-9A-Za-z\-]{10,48}")),
    ("GitHub Token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("Stripe Live Key", re.compile(r"sk_live_[0-9a-zA-Z]{24,}")),
    ("Private Key Block", re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
    )),
    ("Generic Bearer", re.compile(r"(?i)bearer\s+[A-Za-z0-9\-_\.=]{20,}")),
    ("Generic API key assignment", re.compile(
        r"""(?ix)
        (?: api[_-]?key | secret[_-]?key | access[_-]?token | auth[_-]?token )
        \s*[:=]\s*
        ["'][A-Za-z0-9\-_/+=]{16,}["']
        """
    )),
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Finding:
    """A single security finding."""

    type: str
    severity: Severity
    message: str
    line: int = 0
    snippet: str = ""

    def to_dict(self) -> dict:
        """Return a JSON-serialisable representation."""
        return asdict(self)


@dataclass(slots=True)
class ScanResult:
    """Aggregated scan output."""

    findings: List[Finding] = field(default_factory=list)
    syntax_ok: bool = True
    syntax_error: Optional[str] = None
    score: int = 100  # 0–100; lower = more dangerous

    def to_dict(self) -> dict:
        """Return a JSON-serialisable representation."""
        return {
            "findings": [f.to_dict() for f in self.findings],
            "syntax_ok": self.syntax_ok,
            "syntax_error": self.syntax_error,
            "score": self.score,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEVERITY_WEIGHT: dict[str, int] = {
    "low": 2,
    "medium": 5,
    "high": 12,
    "critical": 25,
}


def _line_number(text: str, index: int) -> int:
    """Return the 1-based line number for a byte offset."""
    return text.count("\n", 0, index) + 1


def _snippet(text: str, index: int, window: int = 60) -> str:
    """Return a small around-the-match snippet, newlines collapsed."""
    start = max(0, index - window // 2)
    end = min(len(text), index + window)
    return text[start:end].replace("\n", " ").strip()


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class CodeGuard:
    """
    Stateless code security scanner.

    Usage::

        guard = CodeGuard()
        result = guard.scan(code, language="python")
    """

    __slots__ = ("_patterns", "_secret_patterns")

    def __init__(self) -> None:
        """Instantiate the scanner with pre-compiled patterns."""
        self._patterns: Tuple[Tuple[str, str, re.Pattern[str]], ...] = (
            ("sql_injection", "critical", _SQL_INJECTION),
            ("command_injection", "critical", _CMD_INJECTION),
            ("xss_sink", "high", _XSS_SINK),
            ("eval_usage", "high", _EVAL_USAGE),
            ("exec_usage", "high", _EXEC_USAGE),
            ("pickle_load", "high", _PICKLE_LOAD),
            ("yaml_unsafe_load", "medium", _YAML_LOAD),
            ("path_traversal", "high", _PATH_TRAVERSAL),
        )
        self._secret_patterns = _SECRET_PATTERNS

    # -- public API ---------------------------------------------------------

    def scan(
        self,
        code: str,
        language: str = "python",
        *,
        check_syntax: bool = True,
    ) -> ScanResult:
        """
        Run a full scan over ``code``.

        Never raises — any internal failure results in a conservative
        ``ScanResult`` with ``syntax_ok=True`` and no findings.
        """
        result = ScanResult()
        if not code:
            return result

        # 1. Python syntax validation (ast is CPU-bound, sync)
        if check_syntax and language.lower() in ("python", "py"):
            try:
                self._check_python_syntax(code, result)
            except Exception:
                # Defensive: syntax check should never break the scan
                pass

        # 2. Regex-based pattern scanning
        for name, severity, pattern in self._patterns:
            try:
                for match in pattern.finditer(code):
                    idx = match.start()
                    result.findings.append(
                        Finding(
                            type=name,
                            severity=severity,  # type: ignore[arg-type]
                            message=self._describe(name),
                            line=_line_number(code, idx),
                            snippet=_snippet(code, idx),
                        )
                    )
            except Exception:
                continue

        # 3. Secret scanning
        for label, pattern in self._secret_patterns:
            try:
                for match in pattern.finditer(code):
                    idx = match.start()
                    result.findings.append(
                        Finding(
                            type="hardcoded_secret",
                            severity="critical",
                            message=f"Potential {label} committed in source",
                            line=_line_number(code, idx),
                            snippet=_snippet(code, idx),
                        )
                    )
            except Exception:
                continue

        # 4. Compute security score
        penalty = sum(_SEVERITY_WEIGHT.get(f.severity, 0) for f in result.findings)
        result.score = max(0, 100 - min(penalty, 100))
        return result

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _check_python_syntax(code: str, result: ScanResult) -> None:
        """Parse Python code with ``ast``; append a finding on syntax error."""
        try:
            ast.parse(code)
        except SyntaxError as exc:
            result.syntax_ok = False
            result.syntax_error = f"Line {exc.lineno}: {exc.msg}"
            result.findings.append(
                Finding(
                    type="syntax_error",
                    severity="high",
                    message=f"Python syntax error: {exc.msg}",
                    line=exc.lineno or 0,
                    snippet=(exc.text or "").strip(),
                )
            )

    @staticmethod
    def _describe(name: str) -> str:
        """Human-readable description for a pattern name."""
        return {
            "sql_injection": "Potential SQL injection — user input interpolated into SQL string.",
            "command_injection": "Potential command injection — interpolated argument passed to shell.",
            "xss_sink": "XSS sink detected — untrusted input may be rendered as HTML/JS.",
            "eval_usage": "eval() usage — arbitrary code execution risk.",
            "exec_usage": "exec() usage — arbitrary code execution risk.",
            "pickle_load": "pickle.loads() — deserialisation of untrusted data is unsafe.",
            "yaml_unsafe_load": "yaml.load() without a Loader — use yaml.safe_load().",
            "path_traversal": "Possible path traversal via user-controlled input.",
        }.get(name, name)


# Module-level stateless singleton for convenience.
default_guard = CodeGuard()