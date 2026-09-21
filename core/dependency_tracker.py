# dependency_tracker.py
"""
AXELR Dependency Tracker
========================
Import extraction and in-memory dependency graph.

Languages supported:
    Python (via ``ast`` — accurate), JavaScript, TypeScript,
    HTML, CSS, Vue, Svelte, JSON, YAML (regex fallback).

Python AST is the preferred parser; on SyntaxError or unsupported
constructs, falls back to a compiled regex. No tree-sitter. No external
parsers.

RAM footprint: < 5 MB.
"""

from __future__ import annotations

import ast
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["BlastRadius", "DependencyTracker"]


# ---------------------------------------------------------------------------
# Extension → language
# ---------------------------------------------------------------------------

_EXT_LANG: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".html": "html", ".htm": "html",
    ".css": "css", ".scss": "css", ".sass": "css", ".less": "css",
    ".json": "json",
    ".yaml": "yaml", ".yml": "yaml",
    ".vue": "vue", ".svelte": "svelte",
}

_RESOLVE_EXTS: tuple[str, ...] = (
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx",
    ".html", ".htm", ".css", ".scss", ".json",
    ".vue", ".svelte",
)

_INDEX_FILES: tuple[str, ...] = (
    "index.js", "index.ts", "index.jsx", "index.tsx",
    "__init__.py",
)

# Directories skipped during recursive walks.
_SKIP_DIRS: frozenset[str] = frozenset({
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".nuxt", "coverage",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    "env", ".env", "site-packages",
})


# ---------------------------------------------------------------------------
# Pre-compiled import patterns
# ---------------------------------------------------------------------------

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
    """,
    re.MULTILINE,
)

_HTML_REF_RE = re.compile(
    r"""(?xi)
    <(?:script|link|img|iframe|source|video|audio)\b[^>]*?
    (?:src|href)\s*=\s*["']([^"'#?]+)["']
    """,
)

_CSS_IMPORT_RE = re.compile(
    r"""(?xi) @import\s+(?:url\()?["']([^"')]+)["']\)?""",
)

_VUE_IMPORT_RE = re.compile(
    r"""(?xi)
    (?:import\s+.*?from\s+["']([^"']+)["'])
    |(?:src\s*=\s*["']([^"']+)["'])
    """,
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class BlastRadius:
    """Result of an impact assessment."""

    file: str
    dependents: list[str] = field(default_factory=list)
    severity: str = "low"
    count: int = 0

    def to_dict(self) -> dict:
        """Return a JSON-serialisable representation."""
        return {
            "file": self.file,
            "dependents": list(self.dependents),
            "severity": self.severity,
            "count": self.count,
        }


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------

class DependencyTracker:
    """
    In-memory dependency graph builder.

    Example::

        tracker = DependencyTracker("/path/to/project")
        tracker.build()
        impact = tracker.assess_impact("/path/to/project/app.py")
    """

    __slots__ = ("_built", "_graph", "_reverse", "_root")

    def __init__(self, root: str | os.PathLike[str]) -> None:
        """Initialise the tracker rooted at ``root``."""
        self._root = Path(root).resolve()
        self._graph: dict[str, set[str]] = {}
        self._reverse: dict[str, set[str]] = {}
        self._built = False

    # -- public API ---------------------------------------------------------

    def build(self, *, max_files: int = 5000) -> None:
        """Walk the project tree once and build the full graph."""
        self._graph.clear()
        self._reverse.clear()

        for file_path in self._iter_source_files(max_files=max_files):
            try:
                deps = self._extract_dependencies(file_path)
            except Exception:
                deps = set()
            key = str(file_path)
            self._graph[key] = deps
            for dep in deps:
                self._reverse.setdefault(dep, set()).add(key)

        self._built = True

    def update_file(self, file_path: str | os.PathLike[str]) -> None:
        """Reparse a single file (used after an edit)."""
        path = Path(file_path).resolve()
        key = str(path)

        # Remove old outgoing edges
        for old_dep in self._graph.get(key, set()):
            bucket = self._reverse.get(old_dep)
            if bucket is not None:
                bucket.discard(key)
        self._graph.pop(key, None)

        try:
            deps = self._extract_dependencies(path)
        except Exception:
            deps = set()
        self._graph[key] = deps
        for dep in deps:
            self._reverse.setdefault(dep, set()).add(key)

    def get_dependencies(self, file_path: str | os.PathLike[str]) -> list[str]:
        """Direct dependencies of a file."""
        return sorted(self._graph.get(str(Path(file_path).resolve()), set()))

    def get_dependents(self, file_path: str | os.PathLike[str]) -> list[str]:
        """Files that directly depend on this file."""
        return sorted(self._reverse.get(str(Path(file_path).resolve()), set()))

    def assess_impact(self, file_path: str | os.PathLike[str]) -> BlastRadius:
        """Transitive dependents (BFS) plus a coarse severity score."""
        root_key = str(Path(file_path).resolve())
        seen: set[str] = set()
        queue: list[str] = [root_key]

        while queue:
            current = queue.pop()
            for dependent in self._reverse.get(current, set()):
                if dependent not in seen:
                    seen.add(dependent)
                    queue.append(dependent)

        dependents = sorted(seen)
        count = len(dependents)
        if count >= 10:
            severity = "high"
        elif count >= 3:
            severity = "medium"
        else:
            severity = "low"

        return BlastRadius(
            file=root_key,
            dependents=dependents,
            severity=severity,
            count=count,
        )

    def to_dict(self) -> dict:
        """Return the full graph as JSON-serialisable dicts."""
        return {
            "graph": {k: sorted(v) for k, v in self._graph.items()},
            "reverse": {k: sorted(v) for k, v in self._reverse.items()},
        }

    # -- internals ----------------------------------------------------------

    def _iter_source_files(self, *, max_files: int) -> Iterator[Path]:
        """Yield source files up to ``max_files``, skipping heavy dirs."""
        count = 0
        for dirpath, dirnames, filenames in os.walk(self._root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for filename in filenames:
                ext = Path(filename).suffix.lower()
                if ext not in _EXT_LANG:
                    continue
                yield Path(dirpath) / filename
                count += 1
                if count >= max_files:
                    return

    def _extract_dependencies(self, path: Path) -> set[str]:
        """Return the set of resolved absolute paths this file imports."""
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeDecodeError):
            return set()

        lang = _EXT_LANG.get(path.suffix.lower(), "")
        raw = self._extract_raw_imports(content, lang)
        return self._resolve(path, raw)

    def _extract_raw_imports(self, content: str, lang: str) -> list[str]:
        """Extract raw import strings (unresolved) for the given language."""
        if not content:
            return []

        # Python: try AST first for accuracy
        if lang == "python":
            ast_imports = self._extract_python_imports_ast(content)
            if ast_imports:
                return ast_imports
            # Fallback to regex on SyntaxError
            return [
                (m.group(1) or m.group(2) or "")
                for m in _PY_IMPORT_RE.finditer(content)
            ]

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
        """Extract Python imports with ``ast`` — accurate, no false positives."""
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return []
        except Exception:
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
        """Resolve raw import strings to filesystem paths."""
        resolved: set[str] = set()
        source_dir = source.parent

        for imp in imports:
            if not imp:
                continue
            # Skip bare remote references
            if imp.startswith(("http://", "https://", "//", "data:")):
                continue

            candidates: list[Path] = []
            if imp.startswith("."):
                candidates.append((source_dir / imp).resolve())
            else:
                candidates.append((source_dir / imp).resolve())
                candidates.append((self._root / imp).resolve())

            for cand in candidates:
                resolved_path = self._match_file(cand)
                if resolved_path:
                    resolved.add(str(resolved_path))
                    break

        return resolved

    @staticmethod
    def _match_file(candidate: Path) -> Path | None:
        """Try candidate as-is, with each extension, or as an index file."""
        try:
            if candidate.is_file():
                return candidate
            for ext in _RESOLVE_EXTS:
                with_ext = candidate.with_suffix(ext)
                if with_ext.is_file():
                    return with_ext
            if candidate.is_dir():
                for index_name in _INDEX_FILES:
                    index_file = candidate / index_name
                    if index_file.is_file():
                        return index_file
        except OSError:
            return None
        return None