# core/code_intel.py
"""
AXELR — Code Intelligence (dependency · security · healing · PR · test loop)
=============================================================================
Five concerns, one file — a senior engineer reviewing a PR needs all five
at once:

    §1 Dependency tracker    who imports what; blast radius for a change
    §2 Security scanner      SQL injection, XSS sinks, hardcoded secrets, …
    §3 Self-healing          fix code from an error + emit a unified diff
    §4 PR shield             Markdown PR defense report builder
    §5 Test loop             autonomous generate → execute → heal

All five are deterministic (no ML), async-safe, never raise to their
caller, and RAM-bounded (< 20 MB aggregate).
"""
from __future__ import annotations

import ast
import asyncio
import difflib
import logging   # noqa: F401 — used by §5 TestLoop
import os        # noqa: F401 — used by §1 DependencyTracker
import re
import time      # noqa: F401 — used by §5 TestLoop.run
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger("axelr.code")

# ═══════════════════════════════════════════════════════════════════════════
# §1 — Dependency tracker (was core/dependency.py)
# ═══════════════════════════════════════════════════════════════════════════
_EXT_LANG: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".html": "html", ".htm": "html",
    ".css": "css", ".scss": "css", ".sass": "css", ".less": "css",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".vue": "vue", ".svelte": "svelte",
}
_RESOLVE_EXTS: tuple[str, ...] = (
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx",
    ".html", ".htm", ".css", ".scss", ".json", ".vue", ".svelte",
)
_INDEX_FILES: tuple[str, ...] = (
    "index.js", "index.ts", "index.jsx", "index.tsx", "__init__.py",
)
_SKIP_DIRS: frozenset[str] = frozenset({
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".nuxt", "coverage",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    "env", ".env", "site-packages",
})
_PY_IMPORT_RE = re.compile(
    r"""^[ \t]*(?:from\s+([\w\.]+)\s+import|import\s+([\w\.]+))""",
    re.MULTILINE,
)
_JS_IMPORT_RE = re.compile(
    r"""(?x)
    (?:
        import \s+ (?:[^'"]*?from\s+)? ['"]([^'"]+)['"]
      | require \s* \( \s* ['"]([^'"]+)['"] \s* \)
      | import \s* \( \s* ['"]([^'"]+)['"] \s* \)
    )
    """, re.MULTILINE)
_HTML_REF_RE = re.compile(
    r"""(?xi)
    <(?:script|link|img|iframe|source|video|audio)\b[^>]*?
    (?:src|href)\s*=\s*["']([^"'#?]+)["']
    """)
_CSS_IMPORT_RE = re.compile(r"""(?xi) @import\s+(?:url\()?["']([^"')]+)["']\)?""")
_VUE_IMPORT_RE = re.compile(
    r"""(?xi)
    (?:import\s+.*?from\s+["']([^"']+)["'])
    |(?:src\s*=\s*["']([^"']+)["'])
    """)


@dataclass(slots=True)
class BlastRadius:
    file: str
    dependents: list[str] = field(default_factory=list)
    severity: str = "low"
    count: int = 0

    def to_dict(self) -> dict:
        return {"file": self.file, "dependents": list(self.dependents),
                "severity": self.severity, "count": self.count}


class DependencyTracker:
    """In-memory dependency graph builder. Python AST + regex fallback."""
    __slots__ = ("_built", "_graph", "_reverse", "_root")

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._graph: dict[str, set[str]] = {}
        self._reverse: dict[str, set[str]] = {}
        self._built = False

    async def build(self, *, max_files: int = 5000) -> None:
        self._graph.clear()
        self._reverse.clear()

        def _sync():
            lg: dict[str, set[str]] = {}
            lr: dict[str, set[str]] = {}
            for fp in self._iter_source_files(max_files=max_files):
                try:
                    deps = self._extract_dependencies(fp)
                except Exception:
                    deps = set()
                key = str(fp)
                lg[key] = deps
                for d in deps:
                    lr.setdefault(d, set()).add(key)
            return lg, lr

        self._graph, self._reverse = await asyncio.to_thread(_sync)
        self._built = True

    async def update_file(self, file_path: str | Path) -> None:
        path = Path(file_path).resolve()
        key = str(path)
        for old_dep in self._graph.get(key, set()):
            bucket = self._reverse.get(old_dep)
            if bucket is not None:
                bucket.discard(key)
        self._graph.pop(key, None)

        def _sync():
            try:
                return self._extract_dependencies(path)
            except Exception:
                return set()

        deps = await asyncio.to_thread(_sync)
        self._graph[key] = deps
        for d in deps:
            self._reverse.setdefault(d, set()).add(key)

    def get_dependencies(self, file_path: str | Path) -> list[str]:
        return sorted(self._graph.get(str(Path(file_path).resolve()), set()))

    def get_dependents(self, file_path: str | Path) -> list[str]:
        return sorted(self._reverse.get(str(Path(file_path).resolve()), set()))

    def assess_impact(self, file_path: str | Path) -> BlastRadius:
        root_key = str(Path(file_path).resolve())
        seen: set[str] = set()
        queue: list[str] = [root_key]
        while queue:
            cur = queue.pop()
            for dep in self._reverse.get(cur, set()):
                if dep not in seen:
                    seen.add(dep)
                    queue.append(dep)
        dependents = sorted(seen)
        n = len(dependents)
        severity = "high" if n >= 10 else "medium" if n >= 3 else "low"
        return BlastRadius(file=root_key, dependents=dependents,
                           severity=severity, count=n)

    def to_dict(self) -> dict:
        return {"graph": {k: sorted(v) for k, v in self._graph.items()},
                "reverse": {k: sorted(v) for k, v in self._reverse.items()}}

    def _iter_source_files(self, *, max_files: int) -> Iterator[Path]:
        count = 0
        for dirpath, dirnames, filenames in os.walk(self._root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for filename in filenames:
                if Path(filename).suffix.lower() not in _EXT_LANG:
                    continue
                yield Path(dirpath) / filename
                count += 1
                if count >= max_files:
                    return

    def _extract_dependencies(self, path: Path) -> set[str]:
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeDecodeError):
            return set()
        lang = _EXT_LANG.get(path.suffix.lower(), "")
        raw = self._extract_raw_imports(content, lang)
        return self._resolve(path, raw)

    def _extract_raw_imports(self, content: str, lang: str) -> list[str]:
        if not content:
            return []
        if lang == "python":
            ast_imports = self._extract_python_imports_ast(content)
            if ast_imports:
                return ast_imports
            return [(m.group(1) or m.group(2) or "")
                    for m in _PY_IMPORT_RE.finditer(content)]
        results: list[str] = []
        if lang in ("javascript", "typescript"):
            for m in _JS_IMPORT_RE.finditer(content):
                results.append(m.group(1) or m.group(2) or m.group(3) or "")
        elif lang == "html":
            results.extend(m.group(1) for m in _HTML_REF_RE.finditer(content))
        elif lang == "css":
            results.extend(m.group(1) for m in _CSS_IMPORT_RE.finditer(content))
        elif lang in ("vue", "svelte"):
            for m in _VUE_IMPORT_RE.finditer(content):
                results.append(m.group(1) or m.group(2) or "")
        return [r.strip() for r in results if r and r.strip()]

    @staticmethod
    def _extract_python_imports_ast(content: str) -> list[str]:
        try:
            tree = ast.parse(content)
        except (SyntaxError, Exception):
            return []
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name:
                        imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)
        return imports

    def _resolve(self, source: Path, imports: Iterable[str]) -> set[str]:
        resolved: set[str] = set()
        source_dir = source.parent
        for imp in imports:
            if not imp:
                continue
            if imp.startswith(("http://", "https://", "//", "data:")):
                continue
            candidates = [(source_dir / imp).resolve(),
                          (self._root / imp).resolve()]
            for cand in candidates:
                match = self._match_file(cand)
                if match:
                    resolved.add(str(match))
                    break
        return resolved

    @staticmethod
    def _match_file(candidate: Path) -> Path | None:
        try:
            if candidate.is_file():
                return candidate
            for ext in _RESOLVE_EXTS:
                with_ext = candidate.with_suffix(ext)
                if with_ext.is_file():
                    return with_ext
            if candidate.is_dir():
                for index_name in _INDEX_FILES:
                    f = candidate / index_name
                    if f.is_file():
                        return f
        except OSError:
            return None
        return None




# ═══════════════════════════════════════════════════════════════════════════
# §2 — Security scanner (was core/security.py)
# ═══════════════════════════════════════════════════════════════════════════
Severity = Literal["low", "medium", "high", "critical"]

_SQL_INJECTION = re.compile(
    r"""(?ix)
    (?: \b(?: select|insert|update|delete|drop|union|alter) \b .{0,120}? )
    (?: \$\{ | %s | \+\s*[a-z_] | f["'].*\{ )
    """, re.DOTALL)
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
    """, re.DOTALL)
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
    """)
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
    """)
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AWS Access Key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Google API Key", re.compile(r"AIza[0-9A-Za-z\-_]{35}")),
    ("Slack Token", re.compile(r"xox[baprs]-[0-9A-Za-z\-]{10,48}")),
    ("GitHub Token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("Stripe Live Key", re.compile(r"sk_live_[0-9a-zA-Z]{24,}")),
    ("Private Key Block", re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("Generic Bearer", re.compile(r"(?i)bearer\s+[A-Za-z0-9\-_\.=]{20,}")),
    ("Generic API key assignment", re.compile(
        r"""(?ix)
        (?: api[_-]?key | secret[_-]?key | access[_-]?token | auth[_-]?token )
        \s*[:=]\s*
        ["'][A-Za-z0-9\-_/+=]{16,}["']
        """)),
)
_SEVERITY_WEIGHT: dict[str, int] = {
    "low": 2, "medium": 5, "high": 12, "critical": 25,
}


@dataclass(slots=True)
class Finding:
    type: str
    severity: Severity
    message: str
    line: int = 0
    snippet: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class ScanResult:
    findings: list[Finding] = field(default_factory=list)
    syntax_ok: bool = True
    syntax_error: str | None = None
    score: int = 100

    def to_dict(self) -> dict:
        return {"findings": [f.to_dict() for f in self.findings],
                "syntax_ok": self.syntax_ok,
                "syntax_error": self.syntax_error,
                "score": self.score}


def _line_number(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _snippet(text: str, index: int, window: int = 60) -> str:
    start = max(0, index - window // 2)
    end = min(len(text), index + window)
    return text[start:end].replace("\n", " ").strip()


class CodeGuard:
    """Stateless code security scanner. Deterministic. Never raises."""
    __slots__ = ("_patterns", "_secret_patterns")

    def __init__(self) -> None:
        self._patterns = (
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

    async def scan(self, code: str, language: str = "python",
                   *, check_syntax: bool = True) -> ScanResult:
        if not code:
            return ScanResult()

        def _sync() -> ScanResult:
            r = ScanResult()
            if check_syntax and language.lower() in ("python", "py"):
                try:
                    self._check_python_syntax(code, r)
                except Exception:
                    pass
            for name, severity, pattern in self._patterns:
                try:
                    for m in pattern.finditer(code):
                        idx = m.start()
                        r.findings.append(Finding(
                            type=name, severity=severity,
                            message=self._describe(name),
                            line=_line_number(code, idx),
                            snippet=_snippet(code, idx),
                        ))
                except Exception:
                    continue
            for label, pattern in self._secret_patterns:
                try:
                    for m in pattern.finditer(code):
                        idx = m.start()
                        r.findings.append(Finding(
                            type="hardcoded_secret", severity="critical",
                            message=f"Potential {label} committed in source",
                            line=_line_number(code, idx),
                            snippet=_snippet(code, idx),
                        ))
                except Exception:
                    continue
            penalty = sum(_SEVERITY_WEIGHT.get(f.severity, 0) for f in r.findings)
            r.score = max(0, 100 - min(penalty, 100))
            return r

        return await asyncio.to_thread(_sync)

    @staticmethod
    def _check_python_syntax(code: str, result: ScanResult) -> None:
        try:
            ast.parse(code)
        except SyntaxError as exc:
            result.syntax_ok = False
            result.syntax_error = f"Line {exc.lineno}: {exc.msg}"
            result.findings.append(Finding(
                type="syntax_error", severity="high",
                message=f"Python syntax error: {exc.msg}",
                line=exc.lineno or 0, snippet=(exc.text or "").strip(),
            ))

    @staticmethod
    def _describe(name: str) -> str:
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


default_guard = CodeGuard()


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Self-healing (was core/healing.py)
# ═══════════════════════════════════════════════════════════════════════════
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
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(slots=True)
class HealResult:
    success: bool
    final_code: str
    diff: str = ""
    attempts: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        return {"success": self.success, "final_code": self.final_code,
                "diff": self.diff, "attempts": self.attempts, "error": self.error}


class SelfHealer:
    __slots__ = ("_max_retries", "_route")

    def __init__(self, route_func: RouteFunc, max_retries: int = 2) -> None:
        if route_func is None:
            raise ValueError("route_func is required")
        self._route = route_func
        self._max_retries = max(1, int(max_retries))

    async def heal(self, code: str, error: str, *, language: str = "python",
                   tier: str = "free", user: dict | None = None,
                   context: str = "") -> HealResult:
        if not code or not code.strip():
            return HealResult(success=False, final_code=code, error="empty code")
        original = code
        last_error: str | None = None
        current_error = error
        for attempt in range(1, self._max_retries + 1):
            prompt = self._build_prompt(original, current_error, language)
            try:
                response = await self._route(
                    workspace="design", task_type="touch_fix", prompt=prompt,
                    history=[], files=[], max_tokens=4096, temp=0.15,
                    tier=tier, user=user, context=context,
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
            return HealResult(success=True, final_code=fixed,
                              diff=diff, attempts=attempt)
        return HealResult(success=False, final_code=original,
                          attempts=self._max_retries,
                          error=last_error or "self-heal failed")

    @staticmethod
    def _build_prompt(code: str, error: str, language: str) -> str:
        lang = language.lower()
        return _BLOCK_FIX_PROMPT.format(
            language=language, lang_tag=_LANG_TAG.get(lang, lang),
            error=(error or "").strip()[:2000], code=code)

    @staticmethod
    def _extract_code(text: str) -> str:
        if not text:
            return ""
        m = _FENCE_RE.search(text)
        return m.group(1).rstrip() if m else text.strip()

    @staticmethod
    def _unified_diff(original: str, fixed: str, language: str) -> str:
        ext = _LANG_TAG.get(language.lower(), "txt")
        return "".join(difflib.unified_diff(
            original.splitlines(keepends=True),
            fixed.splitlines(keepends=True),
            fromfile=f"original.{ext}", tofile=f"fixed.{ext}", n=3))


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

    async def fix_block(self, full_code: str, error_block: str,
                        error_message: str, *, language: str = "python",
                        tier: str = "free",
                        user: dict | None = None) -> str:
        if not full_code or not error_block:
            return full_code
        try:
            return await self._fix_block_inner(
                full_code, error_block, error_message, language, tier, user)
        except Exception:
            return full_code

    async def _fix_block_inner(self, full_code: str, error_block: str,
                               error_message: str, language: str, tier: str,
                               user: dict | None) -> str:
        start, end = self._locate_block(full_code, error_block)
        fixed_block = await self._ai_fix_block(
            error_block, error_message, language, tier, user)
        if not fixed_block or fixed_block == error_block:
            return full_code
        original_lines = full_code.splitlines(keepends=True)
        block_lines = original_lines[start:end]
        diff = "".join(difflib.unified_diff(
            block_lines, [l + "\n" for l in fixed_block.splitlines()],
            fromfile="block", tofile="fixed", n=3))
        if diff:
            return self._apply_unified_diff(full_code, diff)
        return full_code

    @staticmethod
    def _locate_block(full_code: str, error_block: str) -> tuple[int, int]:
        code_lines = full_code.splitlines()
        block_lines = error_block.strip().splitlines()
        if not block_lines:
            return 0, len(code_lines)
        stripped = [l.rstrip() for l in block_lines]
        n = len(block_lines)
        for i in range(len(code_lines) - n + 1):
            window = [l.rstrip() for l in code_lines[i:i + n]]
            if window == stripped:
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
                s = code_lines[j].strip()
                if not s:
                    end = j + 1; continue
                cur_indent = len(code_lines[j]) - len(code_lines[j].lstrip())
                if cur_indent <= indent:
                    break
                end = j + 1
            return i, end
        return 0, len(code_lines)

    async def _ai_fix_block(self, error_block: str, error_message: str,
                            language: str, tier: str,
                            user: dict | None) -> str | None:
        if self._route is None:
            return None
        lang_tag = _LANG_TAG.get(language.lower(), "text")
        prompt = _BLOCK_FIX_PROMPT.format(
            language=language, lang_tag=lang_tag,
            error=(error_message or "").strip()[:2000], code=error_block)
        try:
            result = await self._route(
                workspace="design", task_type="touch_fix", prompt=prompt,
                history=[], files=[], max_tokens=2048, temp=0.2,
                tier=tier, user=user, context="")
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
                i += 1; continue
            old_start = int(m.group(1))
            i += 1
            if old_start > 0:
                result.extend(code_lines[pos:old_start - 1])
            pos = old_start - 1
            while i < len(diff_lines):
                hline = diff_lines[i]
                if _HUNK_RE.match(hline):
                    break
                if not hline:
                    i += 1; continue
                first = hline[0]
                if first == "+":
                    result.append(hline[1:])
                elif first == "-":
                    pos += 1
                elif first == " ":
                    result.append(hline[1:])
                    pos += 1
                i += 1
        result.extend(code_lines[pos:])
        return "\n".join(result)


def make_diff(original: str, fixed: str, label: str = "txt") -> str:
    return "".join(difflib.unified_diff(
        original.splitlines(keepends=True),
        fixed.splitlines(keepends=True),
        fromfile=f"original.{label}", tofile=f"fixed.{label}", n=3))


# ═══════════════════════════════════════════════════════════════════════════
# §4 — PR shield (was core/pr_shield.py)
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class PRShieldInput:
    title: str = "Untitled Change"
    author: str | None = None
    files_changed: list[str] = field(default_factory=list)
    blast_radius: Mapping[str, Any] | None = None
    security_findings: list[Mapping[str, Any]] = field(default_factory=list)
    self_heal: Mapping[str, Any] | None = None
    test_results: Mapping[str, Any] | None = None
    recommendations: list[str] = field(default_factory=list)
    extra_notes: str | None = None

    def to_dict(self) -> dict:
        return {"title": self.title, "author": self.author,
                "files_changed": list(self.files_changed),
                "blast_radius": dict(self.blast_radius) if self.blast_radius else None,
                "security_findings": [dict(f) for f in self.security_findings],
                "self_heal": dict(self.self_heal) if self.self_heal else None,
                "test_results": dict(self.test_results) if self.test_results else None,
                "recommendations": list(self.recommendations),
                "extra_notes": self.extra_notes}


class PRShield:
    """Stateless Markdown report builder. Never raises."""
    __slots__ = ()
    _SEVERITY_ORDER = ("critical", "high", "medium", "low")

    def render(self, data: PRShieldInput) -> str:
        if data is None:
            return "# PR Defense Report\n\n_No data supplied._\n"
        parts = [f"# PR Defense Report — {data.title}",
                 f"**Generated:** {datetime.now(timezone.utc).isoformat(timespec='seconds')}"]
        if data.author:
            parts.append(f"**Author:** {data.author}")
        parts += ["", "---", ""]
        try:
            parts.append(self._section_data_flow(data))
            parts.append(self._section_validation(data))
            parts.append(self._section_security(data))
            parts.append(self._section_tests(data))
            parts.append(self._section_recommendations(data))
        except Exception as exc:
            parts.append(f"\n> ⚠️ Report generation warning: {exc}\n")
        if data.extra_notes:
            parts += ["## Notes\n", data.extra_notes.strip(), ""]
        parts += ["---", "*Report generated by AXELR PR Shield.*"]
        return "\n".join(parts)

    def _section_data_flow(self, data: PRShieldInput) -> str:
        out = ["## 1. Data Flow & Blast Radius", ""]
        if data.files_changed:
            out.append("**Files touched:**")
            for f in data.files_changed:
                out.append(f"- `{f}`")
            out.append("")
        br = data.blast_radius
        if not br:
            out += ["_Blast-radius analysis not available._", ""]
            return "\n".join(out)
        severity = str(br.get("severity", "unknown")).upper()
        count = int(br.get("count", 0) or 0)
        out += [f"**Severity:** `{severity}`  ", f"**Dependents affected:** {count}"]
        dependents = list(br.get("dependents", []) or [])
        if dependents:
            out += ["", "<details><summary>Show dependent files</summary>", ""]
            for dep in dependents[:50]:
                out.append(f"- `{dep}`")
            if len(dependents) > 50:
                out.append(f"- …and {len(dependents) - 50} more")
            out += ["", "</details>"]
        out.append("")
        return "\n".join(out)

    def _section_validation(self, data: PRShieldInput) -> str:
        out = ["## 2. Validation & Self-Heal", ""]
        heal = data.self_heal
        if not heal:
            out += ["_Self-heal was not attempted._", ""]
            return "\n".join(out)
        success = bool(heal.get("success", False))
        attempts = int(heal.get("attempts", 0) or 0)
        out += [f"**Self-heal outcome:** {'✅ success' if success else '❌ failed'}",
                f"**Attempts:** {attempts}"]
        err = heal.get("error")
        if err:
            out.append(f"**Error:** `{err}`")
        diff = heal.get("diff")
        if diff:
            out += ["", "<details><summary>Show unified diff</summary>", "",
                    "```diff", str(diff).rstrip(), "```", "", "</details>"]
        out.append("")
        return "\n".join(out)

    def _section_security(self, data: PRShieldInput) -> str:
        out = ["## 3. Security Summary", ""]
        findings = list(data.security_findings or [])
        if not findings:
            out += ["✅ No security findings.", ""]
            return "\n".join(out)
        by_sev: dict[str, list[Mapping[str, Any]]] = {s: [] for s in self._SEVERITY_ORDER}
        for f in findings:
            by_sev.setdefault(str(f.get("severity", "low")).lower(), []).append(f)
        for sev in self._SEVERITY_ORDER:
            bucket = by_sev.get(sev) or []
            if not bucket:
                continue
            out += [f"### {sev.title()} ({len(bucket)})", "",
                    "| Type | Line | Message |", "|------|------|---------|"]
            for f in bucket:
                out.append(f"| {self._escape_cell(str(f.get('type', '')))} "
                           f"| {f.get('line', 0)} "
                           f"| {self._escape_cell(str(f.get('message', '')))} |")
            out.append("")
        out += [f"**Total findings:** {len(findings)}", ""]
        return "\n".join(out)

    def _section_tests(self, data: PRShieldInput) -> str:
        out = ["## 4. Test Results", ""]
        tr = data.test_results
        if not tr:
            out += ["_No test results were supplied._", ""]
            return "\n".join(out)
        passed = bool(tr.get("passed", False))
        out.append(f"**Status:** {'✅ passed' if passed else '❌ failed'}")
        errors = list(tr.get("errors", []) or [])
        if errors:
            out += ["", f"**Failures ({len(errors)}):**"]
            for e in errors[:20]:
                msg = str(e.get("message", e)) if isinstance(e, Mapping) else str(e)
                out.append(f"- {msg}")
            if len(errors) > 20:
                out.append(f"- …and {len(errors) - 20} more")
        out.append("")
        return "\n".join(out)

    def _section_recommendations(self, data: PRShieldInput) -> str:
        out = ["## 5. Recommendations", ""]
        recs = list(data.recommendations or []) or self._auto_recommendations(data)
        if not recs:
            out.append("_No recommendations._")
        else:
            for r in recs:
                out.append(f"- {r}")
        out.append("")
        return "\n".join(out)

    @staticmethod
    def _auto_recommendations(data: PRShieldInput) -> list[str]:
        recs: list[str] = []
        br = data.blast_radius or {}
        if str(br.get("severity", "")).lower() == "high":
            recs.append("Blast radius is **high** — request reviews from owners "
                        "of the listed dependent modules before merging.")
        critical = [f for f in (data.security_findings or [])
                    if str(f.get("severity", "")).lower() == "critical"]
        if critical:
            recs.append(f"{len(critical)} critical security finding(s) must be "
                        "resolved before merge.")
        heal = data.self_heal or {}
        if heal and not heal.get("success", False):
            recs.append("Self-heal failed — inspect the diff manually.")
        tests = data.test_results or {}
        if tests and not tests.get("passed", False):
            recs.append("Fix failing tests before merging.")
        if not recs:
            recs.append("No blocking issues detected. Proceed with normal review.")
        return recs

    @staticmethod
    def _escape_cell(text: str) -> str:
        return text.replace("|", "\\|").replace("\n", " ")


# ═══════════════════════════════════════════════════════════════════════════
# §5 — Test loop (was core/testloop.py)
# ═══════════════════════════════════════════════════════════════════════════
ExecuteFunc = Callable[[str, str, int], Awaitable[dict[str, Any]]]

_TEST_FENCE_RE = re.compile(r"```(?:[A-Za-z0-9_+\-]*)?\s*\n(?P<code>.*?)```",
                            re.DOTALL)


def _extract_fenced(text: str) -> str:
    if not text:
        return ""
    m = _TEST_FENCE_RE.search(text)
    return (m.group("code") if m else text).rstrip()


@dataclass(slots=True)
class TestIteration:
    iteration: int
    tests: str
    passed: bool
    output: str
    error: str | None = None

    def to_dict(self) -> dict:
        return {"iteration": self.iteration, "tests": self.tests,
                "passed": self.passed, "output": self.output[:2000],
                "error": self.error}


@dataclass(slots=True)
class TestLoopResult:
    success: bool
    final_code: str
    final_tests: str
    iterations: list[TestIteration] = field(default_factory=list)
    elapsed_ms: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict:
        return {"success": self.success, "final_code": self.final_code,
                "final_tests": self.final_tests,
                "iterations": [i.to_dict() for i in self.iterations],
                "elapsed_ms": round(self.elapsed_ms, 2), "error": self.error}


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

_TEST_LANG_TAG = {"py": "python", "js": "javascript", "ts": "typescript"}


class TestLoop:
    __slots__ = ("_exec", "_lang_map", "_marker", "_max_iter", "_route", "_timeout")

    def __init__(self, route_func: RouteFunc, execute_func: ExecuteFunc, *,
                 max_iterations: int = 3, exec_timeout: int = 8,
                 pass_marker: str = "ALL TESTS PASSED") -> None:
        if route_func is None or execute_func is None:
            raise ValueError("route_func and execute_func are required")
        self._route = route_func
        self._exec = execute_func
        self._max_iter = max(1, int(max_iterations))
        self._timeout = max(1, int(exec_timeout))
        self._marker = pass_marker
        self._lang_map = {"py": "python", "python": "python", "js": "javascript",
                          "javascript": "javascript", "node": "javascript",
                          "ts": "typescript", "typescript": "typescript"}

    async def run(self, code: str, language: str = "python", *,
                  tier: str = "free", user: dict | None = None,
                  context: str = "") -> TestLoopResult:
        t0 = time.time()
        lang = self._lang_map.get(language.lower(), "python")
        if not code or not code.strip():
            return TestLoopResult(success=False, final_code=code, final_tests="",
                                  error="empty code",
                                  elapsed_ms=(time.time() - t0) * 1000)
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
                return TestLoopResult(success=False, final_code=current_code,
                                      final_tests=final_tests, iterations=iterations,
                                      error="test generation failed",
                                      elapsed_ms=(time.time() - t0) * 1000)
            final_tests = tests
            exec_result = await self._safe_execute(tests, lang)
            passed = self._is_pass(exec_result)
            iterations.append(TestIteration(iteration=i, tests=tests, passed=passed,
                                            output=str(exec_result.get("output", "")),
                                            error=exec_result.get("error")))
            if passed:
                return TestLoopResult(success=True, final_code=current_code,
                                      final_tests=tests, iterations=iterations,
                                      elapsed_ms=(time.time() - t0) * 1000)
            if i == self._max_iter:
                break
            try:
                fixed = await self._heal(current_code, tests, exec_result,
                                         lang, tier, user, context)
            except Exception as e:
                logger.warning("heal_failed error=%s", e)
                fixed = None
            if not fixed or fixed.strip() == current_code.strip():
                break
            current_code = fixed
        return TestLoopResult(success=False, final_code=current_code,
                              final_tests=final_tests, iterations=iterations,
                              error=f"tests failed after {len(iterations)} iteration(s)",
                              elapsed_ms=(time.time() - t0) * 1000)

    async def _generate_tests(self, code: str, lang: str, tier: str,
                              user: dict | None, context: str) -> str | None:
        prompt = _GEN_PROMPT.format(language=lang, code=code)
        result = await self._route(workspace="design", task_type="generate_tests",
                                   prompt=prompt, history=[], files=[],
                                   max_tokens=4096, temp=0.2, tier=tier,
                                   user=user, context=context)
        if not result or not result.get("success"):
            return None
        text = _extract_fenced(result.get("text", ""))
        if self._marker not in text and lang == "python":
            text = text + f"\n\nprint({self._marker!r})\n"
        return text or None

    async def _heal(self, code: str, tests: str, exec_result: dict[str, Any],
                    lang: str, tier: str, user: dict | None,
                    context: str) -> str | None:
        output = str(exec_result.get("output", ""))
        error = exec_result.get("error") or ""
        combined = f"{output}\n{error}".strip()[:4000]
        prompt = _FIX_PROMPT.format(language=lang, code=code,
                                    tests=tests, output=combined)
        result = await self._route(workspace="design", task_type="touch_fix",
                                   prompt=prompt, history=[], files=[],
                                   max_tokens=4096, temp=0.15, tier=tier,
                                   user=user, context=context)
        if not result or not result.get("success"):
            return None
        return _extract_fenced(result.get("text", "")) or None

    async def _safe_execute(self, code: str, lang: str) -> dict[str, Any]:
        try:
            return await self._exec(lang, code, self._timeout) or {
                "success": False, "output": "", "error": "empty_exec_result"}
        except Exception as e:
            logger.warning("execute_failed error=%s", e)
            return {"success": False, "output": "", "error": str(e)}

    def _is_pass(self, exec_result: dict[str, Any]) -> bool:
        if not exec_result:
            return False
        output = str(exec_result.get("output", ""))
        if self._marker not in output:
            return False
        if exec_result.get("error") and not exec_result.get("success"):
            return False
        return True


__all__ = [
    # §1
    "DependencyTracker", "BlastRadius",
    # §2
    "CodeGuard", "Finding", "ScanResult", "Severity", "default_guard",
    # §3
    "SelfHealer", "TouchFixEngine", "HealResult", "make_diff", "RouteFunc",
    # §4
    "PRShield", "PRShieldInput",
    # §5
    "TestLoop", "TestIteration", "TestLoopResult", "ExecuteFunc",
]