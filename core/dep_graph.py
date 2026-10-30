<<<<<<< HEAD
# core/dep_graph.py
"""
AXELR — SQLite-backed Dependency Graph
========================================
Disk-resident replacement for the in-memory DependencyTracker graph.
Every node and edge lives in a SQLite file at
``WORKSPACE_ROOT/.axelr-deps.db``. No Python dict holds the graph, so a
3000-file repository adds zero marginal heap.

Public surface (drop-in for DependencyTracker):
    graph = SQLiteDependencyGraph(root)
    await graph.build(max_files=3000)
    await graph.update_file(path)
    graph.get_dependencies(path) -> list[str]
    graph.get_dependents(path) -> list[str]
    graph.assess_impact(path) -> BlastRadius

BlastRadius is defined in core.code_intel and re-exported here for
import stability.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Iterable, Iterator

from .code_intel import BlastRadius, _EXT_LANG, _SKIP_DIRS

logger = logging.getLogger("axelr.dep_graph")

_RESOLVE_EXTS: tuple[str, ...] = (
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx",
    ".html", ".htm", ".css", ".scss", ".json", ".vue", ".svelte",
)
_INDEX_FILES: tuple[str, ...] = (
    "index.js", "index.ts", "index.jsx", "index.tsx", "__init__.py",
)

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


_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    path TEXT PRIMARY KEY,
    mtime REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS edges (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    PRIMARY KEY (src, dst)
);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
"""


class SQLiteDependencyGraph:
    """Disk-resident dependency graph. Thread-safe by SQLite locking."""

    __slots__ = ("_root", "_db_path")

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._db_path = self._root / ".axelr-deps.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as c:
            c.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self._db_path), timeout=5.0, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA temp_store=MEMORY")
        return c

    # ── build / update ────────────────────────────────────────────────
    async def build(self, *, max_files: int = 5000) -> None:
        def _sync():
            with self._connect() as c:
                c.execute("DELETE FROM edges")
                c.execute("DELETE FROM nodes")
                for fp in self._iter_source_files(max_files=max_files):
                    key = str(fp)
                    try:
                        mtime = fp.stat().st_mtime
                    except OSError:
                        mtime = 0.0
                    c.execute(
                        "INSERT OR REPLACE INTO nodes (path, mtime) VALUES (?, ?)",
                        (key, mtime),
                    )
                    try:
                        deps = self._extract_dependencies(fp)
                    except Exception:
                        deps = set()
                    if deps:
                        c.executemany(
                            "INSERT OR IGNORE INTO edges (src, dst) VALUES (?, ?)",
                            [(key, d) for d in deps],
                        )
        await asyncio.to_thread(_sync)

    async def update_file(self, file_path: str | Path) -> None:
        path = Path(file_path).resolve()
        key = str(path)

        def _sync():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            try:
                deps = self._extract_dependencies(path)
            except Exception:
                deps = set()
            with self._connect() as c:
                c.execute("DELETE FROM edges WHERE src=?", (key,))
                c.execute(
                    "INSERT OR REPLACE INTO nodes (path, mtime) VALUES (?, ?)",
                    (key, mtime),
                )
                if deps:
                    c.executemany(
                        "INSERT OR IGNORE INTO edges (src, dst) VALUES (?, ?)",
                        [(key, d) for d in deps],
                    )
        await asyncio.to_thread(_sync)

    # ── queries ────────────────────────────────────────────────────────
    def get_dependencies(self, file_path: str | Path) -> list[str]:
        key = str(Path(file_path).resolve())
        with self._connect() as c:
            rows = c.execute(
                "SELECT dst FROM edges WHERE src=? ORDER BY dst", (key,),
            ).fetchall()
        return [r[0] for r in rows]

    def get_dependents(self, file_path: str | Path) -> list[str]:
        key = str(Path(file_path).resolve())
        with self._connect() as c:
            rows = c.execute(
                "SELECT src FROM edges WHERE dst=? ORDER BY src", (key,),
            ).fetchall()
        return [r[0] for r in rows]

    def assess_impact(self, file_path: str | Path) -> BlastRadius:
        key = str(Path(file_path).resolve())
        # Recursive CTE walks the reverse graph in one query. SQLite
        # batches this in C with no Python object graph materialized.
        sql = """
        WITH RECURSIVE rd(p) AS (
            SELECT ? 
            UNION
            SELECT e.src FROM edges e JOIN rd ON e.dst = rd.p
        )
        SELECT DISTINCT p FROM rd WHERE p <> ? ORDER BY p
        """
        with self._connect() as c:
            rows = c.execute(sql, (key, key)).fetchall()
        dependents = [r[0] for r in rows]
        n = len(dependents)
        severity = "high" if n >= 10 else "medium" if n >= 3 else "low"
        return BlastRadius(
            file=key, dependents=dependents, severity=severity, count=n,
        )

    def stats(self) -> dict[str, int]:
        with self._connect() as c:
            nodes = c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            edges = c.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        try:
            size = self._db_path.stat().st_size
        except OSError:
            size = 0
        return {"nodes": nodes, "edges": edges, "db_bytes": size}

    # ── internal ───────────────────────────────────────────────────────
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
        resolved: set[str] = set()
        source_dir = source.parent
        for imp in imports:
            if not imp or imp.startswith(("http://", "https://", "//", "data:")):
                continue
            for cand in ((source_dir / imp).resolve(),
                         (self._root / imp).resolve()):
                m = self._match_file(cand)
                if m:
                    resolved.add(str(m))
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


=======
# core/dep_graph.py
"""
AXELR — SQLite-backed Dependency Graph
========================================
Disk-resident replacement for the in-memory DependencyTracker graph.
Every node and edge lives in a SQLite file at
``WORKSPACE_ROOT/.axelr-deps.db``. No Python dict holds the graph, so a
3000-file repository adds zero marginal heap.

Public surface (drop-in for DependencyTracker):
    graph = SQLiteDependencyGraph(root)
    await graph.build(max_files=3000)
    await graph.update_file(path)
    graph.get_dependencies(path) -> list[str]
    graph.get_dependents(path) -> list[str]
    graph.assess_impact(path) -> BlastRadius

BlastRadius is defined in core.code_intel and re-exported here for
import stability.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Iterable, Iterator

from .code_intel import BlastRadius, _EXT_LANG, _SKIP_DIRS

logger = logging.getLogger("axelr.dep_graph")

_RESOLVE_EXTS: tuple[str, ...] = (
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx",
    ".html", ".htm", ".css", ".scss", ".json", ".vue", ".svelte",
)
_INDEX_FILES: tuple[str, ...] = (
    "index.js", "index.ts", "index.jsx", "index.tsx", "__init__.py",
)

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


_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    path TEXT PRIMARY KEY,
    mtime REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS edges (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    PRIMARY KEY (src, dst)
);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
"""


class SQLiteDependencyGraph:
    """Disk-resident dependency graph. Thread-safe by SQLite locking."""

    __slots__ = ("_root", "_db_path")

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._db_path = self._root / ".axelr-deps.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as c:
            c.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self._db_path), timeout=5.0, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA temp_store=MEMORY")
        return c

    # ── build / update ────────────────────────────────────────────────
    async def build(self, *, max_files: int = 5000) -> None:
        def _sync():
            with self._connect() as c:
                c.execute("DELETE FROM edges")
                c.execute("DELETE FROM nodes")
                for fp in self._iter_source_files(max_files=max_files):
                    key = str(fp)
                    try:
                        mtime = fp.stat().st_mtime
                    except OSError:
                        mtime = 0.0
                    c.execute(
                        "INSERT OR REPLACE INTO nodes (path, mtime) VALUES (?, ?)",
                        (key, mtime),
                    )
                    try:
                        deps = self._extract_dependencies(fp)
                    except Exception:
                        deps = set()
                    if deps:
                        c.executemany(
                            "INSERT OR IGNORE INTO edges (src, dst) VALUES (?, ?)",
                            [(key, d) for d in deps],
                        )
        await asyncio.to_thread(_sync)

    async def update_file(self, file_path: str | Path) -> None:
        path = Path(file_path).resolve()
        key = str(path)

        def _sync():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            try:
                deps = self._extract_dependencies(path)
            except Exception:
                deps = set()
            with self._connect() as c:
                c.execute("DELETE FROM edges WHERE src=?", (key,))
                c.execute(
                    "INSERT OR REPLACE INTO nodes (path, mtime) VALUES (?, ?)",
                    (key, mtime),
                )
                if deps:
                    c.executemany(
                        "INSERT OR IGNORE INTO edges (src, dst) VALUES (?, ?)",
                        [(key, d) for d in deps],
                    )
        await asyncio.to_thread(_sync)

    # ── queries ────────────────────────────────────────────────────────
    def get_dependencies(self, file_path: str | Path) -> list[str]:
        key = str(Path(file_path).resolve())
        with self._connect() as c:
            rows = c.execute(
                "SELECT dst FROM edges WHERE src=? ORDER BY dst", (key,),
            ).fetchall()
        return [r[0] for r in rows]

    def get_dependents(self, file_path: str | Path) -> list[str]:
        key = str(Path(file_path).resolve())
        with self._connect() as c:
            rows = c.execute(
                "SELECT src FROM edges WHERE dst=? ORDER BY src", (key,),
            ).fetchall()
        return [r[0] for r in rows]

    def assess_impact(self, file_path: str | Path) -> BlastRadius:
        key = str(Path(file_path).resolve())
        # Recursive CTE walks the reverse graph in one query. SQLite
        # batches this in C with no Python object graph materialized.
        sql = """
        WITH RECURSIVE rd(p) AS (
            SELECT ? 
            UNION
            SELECT e.src FROM edges e JOIN rd ON e.dst = rd.p
        )
        SELECT DISTINCT p FROM rd WHERE p <> ? ORDER BY p
        """
        with self._connect() as c:
            rows = c.execute(sql, (key, key)).fetchall()
        dependents = [r[0] for r in rows]
        n = len(dependents)
        severity = "high" if n >= 10 else "medium" if n >= 3 else "low"
        return BlastRadius(
            file=key, dependents=dependents, severity=severity, count=n,
        )

    def stats(self) -> dict[str, int]:
        with self._connect() as c:
            nodes = c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            edges = c.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        try:
            size = self._db_path.stat().st_size
        except OSError:
            size = 0
        return {"nodes": nodes, "edges": edges, "db_bytes": size}

    # ── internal ───────────────────────────────────────────────────────
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
        resolved: set[str] = set()
        source_dir = source.parent
        for imp in imports:
            if not imp or imp.startswith(("http://", "https://", "//", "data:")):
                continue
            for cand in ((source_dir / imp).resolve(),
                         (self._root / imp).resolve()):
                m = self._match_file(cand)
                if m:
                    resolved.add(str(m))
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


>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
__all__ = ["SQLiteDependencyGraph", "BlastRadius"]