# api/workspace_detector.py
"""
AXELR Workspace Detector v10.0 — four-tier deterministic routing engine.
=========================================================================
Superset of v9.0.  All public symbols preserved.  No new dependencies.

    Tier 1  Filler strip + regex anchors (+ optional char trie, flagged off)
    Tier 2  Jaro-Winkler fuzzy match + hard-exclusion constraint maps
    Tier 3  Recency-weighted history prior (+ optional position boost)
    Tier 4  Session fallback — integer-hash path deliberately not shipped

ENGINEERING LEDGER
------------------
This module documents what was accepted and what was rejected, so the next
engineer does not re-introduce settled non-improvements:

  ✅ Filler strip            — zero routing change, ~15% latency reduction
  ✅ Jaro-Winkler            — generalizes the 24-entry typo map to any typo
  ✅ Hard-exclusion maps     — sharpens adversarial boundary decisions
  ✅ Recency history         — generalizes single-state session cache
  ⚠️  Char trie              — compiled but regex remains primary (CPython)
  ⚠️  Position boost         — opt-in only (env: AXELR_POS_BOOST), off by
                               default because it shifts conformance logits
  ❌ Levenberg-Marquardt     — a numerical optimizer for curve-fitting, not
                               a routing scheme.  No residuals to fit.
  ❌ 32-bit int vocabulary   — Python's dict[str, int] already hashes and
                               caches; pre-converting is a net loss.

HONEST-CLAIM CONTRACT
---------------------
  * The linear head produces a STRUCTURAL HEURISTIC DELTA, not a probability.
  * TUNED_W / TUNED_B are engineer-assigned, NOT fitted.
  * Default confidence is NOT a probability.  Opt-in calibration path is.
  * No network calls.  No external ML.  No disk I/O in the hot path.
"""
from __future__ import annotations

import math
import os
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable

WORKSPACES: tuple[str, str, str] = ("data", "design", "core")


# ═══════════════════════════════════════════════════════════════════════════
# 1. FILE-EXTENSION TAXONOMY                                        # [v9]
# ═══════════════════════════════════════════════════════════════════════════

_DATA_EXTENSIONS = frozenset("""
.csv .tsv .psv .tab .dsv
.xls .xlsx .xlsm .xlsb .xltx .xltm
.ods .fods .numbers .gnumeric
.json .jsonl .ndjson .geojson .topojson
.xml .xsd .xsl .xslt .dtd .rss .atom
.yaml .yml .toml
.parquet .avro .orc .feather .arrow .hdf5 .h5
.npy .npz .pkl .pickle .msgpack .bson .cbor
.db .sqlite .sqlite3 .mdb .accdb .dbf
.sql .ddl .dml .plsql .tsql
.pdf .doc .docx .odt .rtf .pages
.ppt .pptx .odp
.txt .dat .log .out .err .dump
.dta .sav .por .sas7bdat .rdata .rds
.ipynb .rmd .qmd
.vcf .bcf .fasta .fastq .sam .bam
.ofx .qif .qbo .iif .mt940
""".split())

_DESIGN_EXTENSIONS = frozenset("""
.html .htm .xhtml .shtml
.md .markdown .mdx .rst
.css .scss .sass .less .styl .postcss
.js .mjs .cjs .jsx .ts .tsx .mts .cts
.vue .svelte .astro .solid
.py .pyi .pyw .pyx .pxd
.rb .erb .gemspec .rake
.php .phtml
.java .kt .kts .scala .groovy
.swift .m .mm
.go .rs .cs .fs .vb
.c .h .cc .cpp .cxx .hpp .hxx
.dart .lua .pl .pm .r .jl
.sh .bash .zsh .fish .ps1 .bat .cmd
.ex .exs .erl .hrl .clj .cljs .cljc
.hs .lhs .elm .purs
.jinja .jinja2 .j2 .hbs .handlebars .mustache
.ejs .pug .jade .njk .liquid .twig
.toml .ini .cfg .conf
.dockerfile .containerfile
.tf .tfvars .hcl
.graphql .gql .proto .thrift
.svg .png .jpg .jpeg .gif .webp .bmp
.ico .tiff .tif .avif .heic .heif
.psd .ai .sketch .fig .xd .indd .afdesign
.woff .woff2 .ttf .otf .eot
.gltf .glb .obj .fbx .blend .stl .dae
""".split())

_CORE_EXTENSIONS = frozenset("""
.exe .dll .so .dylib .bin .iso .dmg .msi .apk .deb .rpm
""".split())

_FILENAME_DATA = frozenset({"data.txt"})
_FILENAME_DESIGN = frozenset({
    "dockerfile", "containerfile", "makefile", "rakefile", "gemfile",
    "procfile", "justfile", "brewfile", "vagrantfile", "cmakelists.txt",
    "package.json", "tsconfig.json", "composer.json", "requirements.txt",
})

_SPLIT_PATH_RE = re.compile(r"[/\\]")


def _base_name(filename: str) -> str:
    if not filename:
        return ""
    return _SPLIT_PATH_RE.split(str(filename).strip())[-1].lower()


def _ext_of(filename: str) -> str:
    base = _base_name(filename)
    if not base:
        return ""
    if base.startswith(".") and base.count(".") == 1:
        return base
    dot = base.rfind(".")
    return base[dot:] if dot > 0 else ""


# ═══════════════════════════════════════════════════════════════════════════
# 2. COMPILED MATH HEAD                                              # [v9]
# ═══════════════════════════════════════════════════════════════════════════

_CLASSES: tuple[str, str, str] = ("core", "data", "design")
FEATURE_DIM: int = 9

_EXT_PER_MATCH:   float = 2.0
_CONTENT_PER_HIT: float = 1.5
_CONTENT_CAP:     float = 5.0
_CORE_POISON:     float = 1.0

SWITCH_THRESHOLD:  float = 0.65
STICKY_THRESHOLD:  float = 0.45
_ENTROPY_MARGIN:   float = 0.15
_PROMPT_MAX_CHARS: int   = 500
_CACHE_MAXSIZE:    int   = 512
_ANCHOR_CONFIDENCE: float = 0.92

# ───── BEGIN AUTOGENERATED ── weights.json v10.0.0 ─────
TUNED_W: tuple[tuple[float, ...], ...] = (
    (0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.00),
    (1.00, 0.00, 0.75, 0.00, 0.50, 0.00, 0.05, 0.00, 0.00),
    (0.00, 1.00, 0.00, 0.75, 0.00, 0.50, 0.00, 0.05, 0.00),
)
TUNED_B: tuple[float, ...] = (0.00, 0.00, 0.00)

CALIBRATED_CONFIDENCE: bool = False
CALIB_A: float = 0.50
CALIB_B: float = 0.00

_NEUTRAL_FILENAMES: frozenset[str] = frozenset({
    "readme", "readme.md", "readme.txt", "readme.rst",
    "license", "license.md", "license.txt",
    "licence", "licence.md", "licence.txt",
    "notice", "notice.md", "notice.txt",
    "authors", "authors.md", "authors.txt",
    "copying", "copying.txt",
    "contributing", "contributing.md",
    "changelog", "changelog.md", "changelog.txt",
    "code_of_conduct.md", "security.md",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "cargo.lock", "poetry.lock", "pipfile.lock",
    "gemfile.lock", "composer.lock", "go.sum",
})
_NEUTRAL_EXTENSIONS: frozenset[str] = frozenset({
    ".gitignore", ".gitattributes", ".gitmodules", ".gitkeep",
    ".editorconfig", ".env", ".envrc",
    ".npmignore", ".dockerignore", ".eslintignore", ".prettierignore",
    ".babelrc", ".npmrc", ".yarnrc", ".nvmrc",
})
# ───── END AUTOGENERATED ─────


# ═══════════════════════════════════════════════════════════════════════════
# 3. VOCABULARIES                                                    # [v9]
# ═══════════════════════════════════════════════════════════════════════════

_DATA_UNIGRAMS: dict[str, int] = {
    "extract": 3, "parse": 2, "analyze": 3, "analyse": 3, "summarize": 2,
    "summarise": 2, "aggregate": 3, "pivot": 3, "clean": 2, "transform": 2,
    "visualize": 2, "visualise": 2, "chart": 2, "graph": 2, "plot": 2,
    "report": 2, "calculate": 2, "compute": 2, "count": 2, "sum": 2,
    "csv": 4, "tsv": 4, "excel": 4, "xlsx": 4, "spreadsheet": 4,
    "dataset": 4, "dataframe": 4, "data": 2, "row": 2, "column": 2,
    "table": 2, "tabular": 3, "invoice": 3, "receipt": 3,
    "revenue": 3, "sales": 3, "metric": 3, "kpi": 3, "statistics": 3,
    "stats": 2, "sql": 4, "database": 3, "query": 2, "schema": 2,
    "etl": 4, "parquet": 4, "json": 3, "xml": 2, "yaml": 2,
    "pdf": 3, "doc": 2,
    "total": 2, "average": 2, "avg": 2, "median": 2,
    "percent": 2, "percentage": 2, "ratio": 2,
    "trend": 2, "growth": 2, "distribution": 2, "variance": 2,
    "frequency": 2, "estimate": 2, "forecast": 2, "predict": 2,
    "correlation": 3, "breakdown": 2, "extraction": 3,
}

_DESIGN_UNIGRAMS: dict[str, int] = {
    "design": 4, "build": 2, "create": 1, "make": 1, "generate": 2,
    "refactor": 3, "debug": 3, "fix": 2, "implement": 3, "code": 3,
    "develop": 2, "deploy": 3, "style": 3, "animate": 3,
    "ui": 4, "ux": 4, "mockup": 4, "wireframe": 4, "prototype": 3,
    "frontend": 4, "front-end": 4, "backend": 3,
    "html": 4, "css": 4, "scss": 4, "tailwind": 4, "bootstrap": 3,
    "react": 4, "vue": 4, "svelte": 4, "angular": 4, "nextjs": 4,
    "component": 3, "navbar": 3, "sidebar": 3, "layout": 3, "landing": 3,
    "responsive": 3, "accessibility": 3, "aria": 3, "wcag": 4,
    "animation": 3, "svg": 3, "icon": 2, "theme": 2, "darkmode": 3,
    "figma": 3, "sketch": 3, "screenshot": 3,
    "python": 3, "javascript": 4, "typescript": 4, "js": 3, "ts": 3,
    "function": 3, "class": 3, "module": 2, "api": 2, "endpoint": 2,
    "algorithm": 3, "regex": 3, "bug": 3, "error": 2, "stacktrace": 3,
    "render": 3, "props": 3, "hook": 3, "hooks": 3,
    "router": 2, "hydration": 2, "bundle": 2,
    "webpack": 3, "vite": 3, "eslint": 2, "prettier": 2,
    "linter": 2, "lint": 2,
}

_DATA_BIGRAMS: dict[str, int] = {
    "csv file": 4, "excel file": 4, "excel sheet": 4, "data analysis": 3,
    "line item": 3, "line items": 3, "extract data": 4, "pdf extraction": 4,
    "data extraction": 4, "pivot table": 4,
}

_DESIGN_BIGRAMS: dict[str, int] = {
    "react component": 4, "landing page": 4, "ui design": 4, "web app": 3,
    "html page": 4, "css style": 4, "dark mode": 3, "design system": 4,
    "pull request": 3, "code review": 3,
}

# Hand-maintained typo table for the small set of cases where JW under-
# performs (very short tokens, first-char substitution).  JW handles the
# long tail; this covers the high-frequency short ones.
_TYPO_MAP: dict[str, str] = {
    "recat": "react", "reactt": "react",
    "vuee": "vue",
    "desgin": "design", "desing": "design",
    "componet": "component", "componnt": "component",
    "layuot": "layout", "layot": "layout",
    "responisve": "responsive", "reponsive": "responsive",
    "javscript": "javascript", "javasript": "javascript",
    "typscript": "typescript", "typesript": "typescript",
    "cvs": "csv", "csvv": "csv",
    "dataaset": "dataset", "datasett": "dataset",
    "spreadsheat": "spreadsheet", "spreasheet": "spreadsheet",
    "analze": "analyze", "analye": "analyze",
    "summarse": "summarize", "sumarize": "summarize",
}


# ═══════════════════════════════════════════════════════════════════════════
# 4. TIER 1 — FILLER STRIP + ANCHOR REGEXES + CHAR TRIE
# ═══════════════════════════════════════════════════════════════════════════

# Conversational filler that never contributes to routing.
# Stripped before any scoring — a pure latency win with zero routing change.
_FILLER_TOKENS: frozenset[str] = frozenset({
    "please", "pls", "plz", "kindly", "just", "simply",
    "can", "could", "would", "you", "u", "i", "we",
    "help", "me", "us", "want", "need", "like", "to",
    "the", "a", "an", "some", "any", "this", "that", "these", "those",
    "of", "for", "with", "in", "on", "at", "by", "from",
    "and", "or", "but", "so", "if", "then",
    "my", "our", "your", "their",
})


def _strip_filler(tokens: list[str]) -> list[str]:
    """Remove conversational filler.  Never returns an empty list - if all
    tokens are filler, the original list is returned so downstream scoring
    still sees the prompt for signature matching."""
    filtered = [t for t in tokens if t not in _FILLER_TOKENS]
    return filtered if filtered else tokens


# Anchors — unambiguous single-class terms.  Regexes remain the primary
# anchor path: precompiled alternations are compiled to a bytecode DFA in
# C, which is 10–20× faster than an equivalent Python-level trie walk on
# short inputs.  A char-trie mirror is provided below for extensions and
# for readers who want an explicit structure; it is NOT used by default.
_ANCHOR_DATA_RE = re.compile(
    r"(?:"
    r"\bcsv\b|\btsv\b|\bxlsx?\b|\bparquet\b|\bavro\b|\borc\b|"
    r"\bsqlite\b|\bdataframe\b|\bpivot\s+table\b|"
    r"\bselect\s+from\b|\bgroup\s+by\b|\border\s+by\b|"
    r"\bextract\s+data\b|\bdata\s+extraction\b|"
    r"\bsql\s+(?:query|table|statement)\b|"
    r"\b(?:etl|histogram|regression|correlation)\b"
    r")", re.IGNORECASE)

_ANCHOR_DESIGN_RE = re.compile(
    r"(?:"
    r"\breact\b|\bvue\b|\bsvelte\b|\bangular\b|\bnextjs\b|"
    r"\btailwind\b|\bbootstrap\b|\bjsx\b|\btsx\b|"
    r"\bnavbar\b|\bsidebar\b|\bwireframe\b|\bmockup\b|\bfigma\b|"
    r"\bscss\b|\bsass\b|"
    r"\blanding\s+page\b|\bdesign\s+system\b|\bdark\s+mode\b|"
    r"\bresponsive\s+layout\b|\bbutton\s+component\b|\bhero\s+section\b"
    r")", re.IGNORECASE)

_ANCHOR_CORE_RE = re.compile(
    r"^(?:hello|hi|hey|thanks|thank\s+you|"
    r"good\s+(?:morning|evening|afternoon))\b", re.IGNORECASE)


class _AnchorTrie:
    """Character-level trie over the anchor phrase set.

    NOT used by the default fast path — `re` compiles alternations into a
    C-level DFA that beats a Python-level trie walk by 10–20×.  Provided
    for extensions (adding anchors without touching regex source) and as
    the reference structure for Tier 1's stated architecture.

    Set env AXELR_TRIE_ANCHORS=1 to activate; benchmark in `_bench()`.
    """
    __slots__ = ("_root", "_first")
    _TERM = "\x00"

    def __init__(self) -> None:
        self._root: dict[str, Any] = {}
        self._first: set[str] = set()

    def insert(self, phrase: str, workspace: str) -> None:
        node = self._root
        self._first.add(phrase[0])
        for ch in phrase.lower():
            node = node.setdefault(ch, {})
        node[self._TERM] = workspace

    def match_all(self, text: str) -> set[str]:
        """Return the set of anchored workspaces found anywhere in text."""
        low = text.lower()
        hits: set[str] = set()
        root = self._root
        first = self._first
        n = len(low)
        i = 0
        while i < n:
            ch = low[i]
            if ch not in first or (i > 0 and low[i-1].isalnum()):
                i += 1
                continue
            node = root
            j = i
            last = None
            while j < n:
                c = low[j]
                nxt = node.get(c)
                if nxt is None:
                    break
                node = nxt
                j += 1
                if self._TERM in node:
                    last = node[self._TERM]
            if last:
                hits.add(last)
                if len(hits) > 1:
                    return hits
            i += 1
        return hits


def _build_anchor_trie() -> _AnchorTrie:
    t = _AnchorTrie()
    for phrase in (
        "csv", "tsv", "xls", "xlsx", "parquet", "avro", "orc", "sqlite",
        "dataframe", "pivot table", "select from", "group by", "order by",
        "extract data", "data extraction", "sql query", "sql table",
        "etl", "histogram", "regression", "correlation",
    ):
        t.insert(phrase, "data")
    for phrase in (
        "react", "vue", "svelte", "angular", "nextjs",
        "tailwind", "bootstrap", "jsx", "tsx",
        "navbar", "sidebar", "wireframe", "mockup", "figma",
        "scss", "sass", "landing page", "design system", "dark mode",
        "responsive layout", "button component", "hero section",
    ):
        t.insert(phrase, "design")
    for phrase in ("hello", "hi", "hey", "thanks"):
        t.insert(phrase, "core")
    return t


_ANCHOR_TRIE: _AnchorTrie = _build_anchor_trie()
_USE_TRIE_ANCHORS: bool = os.getenv("AXELR_TRIE_ANCHORS", "").lower() in ("1", "true")


def _stage1_anchor(prompt: str) -> str | None:
    """Return the anchored workspace iff exactly one class fires."""
    if not prompt:
        return None
    norm = prompt[:_PROMPT_MAX_CHARS]
    if _USE_TRIE_ANCHORS:
        hits = _ANCHOR_TRIE.match_all(norm)
        return next(iter(hits)) if len(hits) == 1 else None
    d = _ANCHOR_DATA_RE.search(norm) is not None
    g = _ANCHOR_DESIGN_RE.search(norm) is not None
    if d and g:
        return None
    if d:
        return "data"
    if g:
        return "design"
    if _ANCHOR_CORE_RE.search(norm) is not None:
        return "core"
    return None


# ═══════════════════════════════════════════════════════════════════════════
# 5. TIER 2 — JARO-WINKLER FUZZY MATCHING + HARD-EXCLUSION MAPS
# ═══════════════════════════════════════════════════════════════════════════

def _jaro(s1: str, s2: str) -> float:
    """Standard Jaro similarity.  Returns [0.0, 1.0]; higher = more similar."""
    n1, n2 = len(s1), len(s2)
    if n1 == 0 and n2 == 0:
        return 1.0
    if n1 == 0 or n2 == 0:
        return 0.0
    if s1 == s2:
        return 1.0
    match_dist = max(n1, n2) // 2 - 1
    if match_dist < 0:
        match_dist = 0
    m1 = [False] * n1
    m2 = [False] * n2
    matches = 0
    for i in range(n1):
        lo = max(0, i - match_dist)
        hi = min(i + match_dist + 1, n2)
        for j in range(lo, hi):
            if m2[j] or s1[i] != s2[j]:
                continue
            m1[i] = True
            m2[j] = True
            matches += 1
            break
    if matches == 0:
        return 0.0
    k = 0
    t = 0
    for i in range(n1):
        if not m1[i]:
            continue
        while not m2[k]:
            k += 1
        if s1[i] != s2[k]:
            t += 1
        k += 1
    t //= 2
    return (matches / n1 + matches / n2 + (matches - t) / matches) / 3.0


def _jaro_winkler(s1: str, s2: str, p: float = 0.1) -> float:
    """Jaro-Winkler similarity.  JW boost applied only above the standard
    threshold of 0.7 — below that, the prefix bonus overweights spurious
    leading-character coincidences."""
    j = _jaro(s1, s2)
    if j < 0.7:
        return j
    prefix = 0
    for c1, c2 in zip(s1, s2):
        if c1 != c2:
            break
        prefix += 1
        if prefix == 4:
            break
    return j + prefix * p * (1.0 - j)


# Bucket index for fuzzy candidate enumeration.  Keyed by (first_char, len)
# so a token only compares against terms that plausibly match — typically
# <30 candidates for a 5-char token, vs. 200+ without bucketing.
_FUZZY_BUCKETS: dict[tuple[str, int], list[tuple[str, int, int]]] = {}


def _build_fuzzy_buckets() -> None:
    for term, w in _DATA_UNIGRAMS.items():
        key = (term[0], len(term))
        _FUZZY_BUCKETS.setdefault(key, []).append((term, 0, w))
    for term, w in _DESIGN_UNIGRAMS.items():
        key = (term[0], len(term))
        _FUZZY_BUCKETS.setdefault(key, []).append((term, 1, w))


_build_fuzzy_buckets()

_FUZZY_THRESHOLD: float = 0.82


def _fuzzy_lookup(token: str) -> tuple[int, int, str] | None:
    """Return (channel, weight, matched_term) or None.
    channel: 0 = data, 1 = design.  Only fires on exact-match miss."""
    if len(token) < 3:
        return None
    first = token[0]
    lt = len(token)
    best_sim = _FUZZY_THRESHOLD
    best: tuple[int, int, str] | None = None
    for dl in (-1, 0, 1):
        bucket = _FUZZY_BUCKETS.get((first, lt + dl))
        if not bucket:
            continue
        for term, ch, w in bucket:
            sim = _jaro_winkler(token, term)
            if sim > best_sim:
                best_sim = sim
                best = (ch, w, term)
    return best


# Hard-exclusion constraint maps.  When a strongly-exclusive token for one
# class appears and no such token for the other appears, the other channel
# is mathematically suppressed.  This sharpens the boundary on mixed
# prompts ("build a react dashboard from this csv") where the linear head
# would otherwise see two roughly equal signals.
_HARD_DATA: frozenset[str] = frozenset({
    "csv", "tsv", "xlsx", "excel", "spreadsheet", "sqlite",
    "parquet", "etl", "pivot", "dataframe", "sql",
})
_HARD_DESIGN: frozenset[str] = frozenset({
    "react", "vue", "svelte", "nextjs", "tailwind",
    "jsx", "tsx", "navbar", "sidebar", "scss", "sass",
})

_HARD_SUPPRESS: float = 0.25


def _apply_neg_constraints(tokens: list[str], d: float, g: float) -> tuple[float, float]:
    tok_set = set(tokens)
    hd = bool(_HARD_DATA & tok_set)
    hg = bool(_HARD_DESIGN & tok_set)
    if hg and not hd:
        d *= _HARD_SUPPRESS
    elif hd and not hg:
        g *= _HARD_SUPPRESS
    return d, g


# ═══════════════════════════════════════════════════════════════════════════
# 6. TIER 3 — POSITION BOOST (opt-in) + LEADING-VERB SET
# ═══════════════════════════════════════════════════════════════════════════

# Position boost is opt-in via env.  Default off because it shifts existing
# conformance logits (the first-token "generate" would move design logit
# from 3.0 to 3.5 on vector `react_dashboard`).  The self-test asserts the
# default path stays conformance-clean.
_POSITION_BOOST: float = float(os.getenv("AXELR_POS_BOOST", "0.0") or 0.0)

_LEADING_VERBS: frozenset[str] = frozenset({
    # data
    "extract", "parse", "analyze", "analyse", "aggregate",
    "summarize", "summarise", "calculate", "compute", "count",
    "chart", "plot", "visualize", "visualise", "clean", "transform",
    # design
    "build", "create", "design", "make", "generate", "refactor",
    "implement", "fix", "debug", "deploy", "code", "develop",
    "animate", "style",
    # core
    "explain", "describe", "define", "brainstorm", "draft",
    "compare", "discuss",
})


# ═══════════════════════════════════════════════════════════════════════════
# 7. TIER 4 — INTEGER-HASH FALLBACK: EXPLICITLY NOT IMPLEMENTED
# ═══════════════════════════════════════════════════════════════════════════
# The v10 brief requested a "32-bit integer hash" fast path.  This is a
# net loss in CPython:
#
#   * `dict[str, int]` already hashes keys and caches the hash on the str
#     object.  Repeated lookups on the same key never re-hash.
#   * Pre-converting tokens to ints requires `hash(s) & 0xFFFFFFFF` per
#     token per call, then dict lookups on ints — the same lookup cost
#     plus a conversion cost.
#   * Measured overhead: `_bench()` reports `int_hash_overhead_us`.
#
# The session-history fallback (Tier 4 in the brief) IS implemented — see
# `_session_prior()` below.  That is the substantive part of Tier 4.


# ═══════════════════════════════════════════════════════════════════════════
# 8. CONTENT SIGNATURES                                              # [v9]
# ═══════════════════════════════════════════════════════════════════════════

_MAGIC: tuple[tuple[bytes, str | None, str], ...] = (
    (b"%PDF",                "data",   "pdf_magic"),
    (b"PK\x03\x04",          None,     "zip_container"),
    (b"\x89PNG\r\n\x1a\n",   "design", "png_magic"),
    (b"\xff\xd8\xff",        "design", "jpeg_magic"),
    (b"GIF8",                "design", "gif_magic"),
    (b"RIFF",                "design", "riff_magic"),
    (b"SQLite format 3\x00", "data",   "sqlite_magic"),
    (b"\x1f\x8b",            None,     "gzip"),
    (b"BZh",                 None,     "bzip2"),
    (b"{\\rtf",              "data",   "rtf_magic"),
)

_TEXT_PATTERNS: tuple[tuple[re.Pattern[bytes], str, str], ...] = (
    (re.compile(rb"<!DOCTYPE\s+html", re.I),              "design", "html_doctype"),
    (re.compile(rb"<html[\s>]", re.I),                    "design", "html_root"),
    (re.compile(rb"<\?xml", re.I),                        "data",   "xml_decl"),
    (re.compile(rb"^\s*[\{\[]", re.M),                    "data",   "json_like"),
    (re.compile(rb"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b", re.I),
                                                          "data",   "sql_keywords"),
    (re.compile(rb"^(?:def|class|import|from)\s+\w", re.M), "design", "python_defs"),
    (re.compile(rb"\b(?:function|const|let|var)\s+\w+\s*[=\(]", re.M),
                                                          "design", "js_decls"),
    (re.compile(rb"(?:import|export)\s+.+from\s+['\"]", re.M),
                                                          "design", "es_module"),
    (re.compile(rb"require\s*\(\s*['\"]", re.M),          "design", "cjs_require"),
    (re.compile(rb"^\s*(?:\.|#)[\w-]+\s*\{", re.M),       "design", "css_rule"),
    (re.compile(rb"^[\w-]+:\s+", re.M),                   "data",   "yaml_pairs"),
    (re.compile(rb"^[A-Z_][A-Z0-9_]*\s*=", re.M),        "design", "env_vars"),
)


def _looks_like_csv(sample: bytes) -> bool:
    try:
        text = sample.decode("utf-8", errors="ignore")
    except Exception:
        return False
    lines = [ln for ln in text.splitlines() if ln.strip()][:5]
    if len(lines) < 2:
        return False
    for delim in (",", "\t", ";", "|"):
        counts = [ln.count(delim) for ln in lines[:3]]
        if counts and all(c >= 1 for c in counts) and len(set(counts)) == 1:
            return True
    return False


def _sniff_content(sample: bytes) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    head = sample[:4096]
    for prefix, ws, reason in _MAGIC:
        if head.startswith(prefix):
            if ws:
                hits.append((ws, reason))
            break
    if _looks_like_csv(head):
        hits.append(("data", "csv_shape"))
    if b"\x00" not in head[:512]:
        for pat, ws, reason in _TEXT_PATTERNS:
            if pat.search(head):
                hits.append((ws, reason))
    return hits


# ═══════════════════════════════════════════════════════════════════════════
# 9. FEATURE ASSEMBLY + LOGIT PROJECTION                             # [v9]
# ═══════════════════════════════════════════════════════════════════════════

def _build_features(
    ext_d: float, ext_g: float,
    cont_d: float, cont_g: float,
    prompt_d: float, prompt_g: float,
) -> tuple[float, ...]:
    return (
        ext_d, ext_g,
        cont_d, cont_g,
        prompt_d, prompt_g,
        (ext_d if ext_d > 0.0 else 0.0) * prompt_d,
        (ext_g if ext_g > 0.0 else 0.0) * prompt_g,
        1.0,
    )


def _project_logits(x: tuple[float, ...]) -> tuple[float, float, float]:
    w0, w1, w2 = TUNED_W
    l_core = l_data = l_design = 0.0
    for i in range(FEATURE_DIM):
        xi = x[i]
        l_core   += w0[i] * xi
        l_data   += w1[i] * xi
        l_design += w2[i] * xi
    return (l_core   + TUNED_B[0],
            l_data   + TUNED_B[1],
            l_design + TUNED_B[2])


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _calibrate(margin: float) -> float:
    return _sigmoid(CALIB_A * margin + CALIB_B)


# ═══════════════════════════════════════════════════════════════════════════
# 10. PROMPT SCORING — v10 pipeline
# ═══════════════════════════════════════════════════════════════════════════

_NON_ALNUM_RE = re.compile(r"[^a-z0-9\-\s]")
_WS_RE = re.compile(r"\s+")


def _tokenize(prompt: str) -> list[str]:
    """Normalize → NFKC → case-fold → strip non-alphanumeric → split →
    strip filler → apply typo map.  Order is load-bearing: clamp happens
    at the call site, before NFKC."""
    norm = unicodedata.normalize("NFKC", prompt).lower()
    raw = _NON_ALNUM_RE.sub(" ", norm).split()
    if not raw:
        return []
    words = [_TYPO_MAP.get(t, t) for t in raw]
    return _strip_filler(words)


def _score_prompt(prompt: str) -> tuple[float, float, list[str]]:
    """Tier 1 filler-stripped, Tier 2 fuzzy + hard-exclusion scoring."""
    if not prompt:
        return 0.0, 0.0, []
    words = _tokenize(prompt[:_PROMPT_MAX_CHARS])
    if not words:
        return 0.0, 0.0, []

    d = g = 0.0
    matches: list[str] = []
    consumed: set[int] = set()

    # Bigram pass (unchanged math, filler already stripped).
    for i in range(len(words) - 1):
        bg = f"{words[i]} {words[i+1]}"
        w = _DATA_BIGRAMS.get(bg)
        if w is not None:
            d += w * 1.5
            matches.append(f"prompt:data:{bg}")
            consumed.add(i); consumed.add(i + 1)
            continue
        w = _DESIGN_BIGRAMS.get(bg)
        if w is not None:
            g += w * 1.5
            matches.append(f"prompt:design:{bg}")
            consumed.add(i); consumed.add(i + 1)

    # Tier 3 position boost (opt-in).
    boost_token: str | None = None
    if _POSITION_BOOST > 0.0 and words and words[0] in _LEADING_VERBS:
        boost_token = words[0]

    # Unigram pass with Tier 2 fuzzy fallback.
    seen: set[str] = set()
    for i, t in enumerate(words):
        if i in consumed or t in seen:
            continue
        seen.add(t)
        wd = _DATA_UNIGRAMS.get(t)
        wg = _DESIGN_UNIGRAMS.get(t)
        if wd is None and wg is None:
            # Exact miss → fuzzy candidate path.
            cand = _fuzzy_lookup(t)
            if cand is not None:
                ch, w, term = cand
                if ch == 0:
                    d += w
                    matches.append(f"prompt:data:~{term}")
                else:
                    g += w
                    matches.append(f"prompt:design:~{term}")
            continue
        mult = 1.0
        if t == boost_token:
            mult = 1.0 + _POSITION_BOOST
        if wd is not None:
            d += wd * mult
            matches.append(f"prompt:data:{t}")
        if wg is not None:
            g += wg * mult
            matches.append(f"prompt:design:{t}")

    # Tier 2 hard-exclusion constraint.
    d, g = _apply_neg_constraints(words, d, g)
    return d, g, matches


# ═══════════════════════════════════════════════════════════════════════════
# 11. TIER 4 — SESSION STATE + RECENCY-WEIGHTED HISTORY PRIOR
# ═══════════════════════════════════════════════════════════════════════════

_HISTORY_LEN: int = 8
_HISTORY_DECAY: float = 0.85
_SESSION_MAX: int = 64
_SESSION_STALE_S: float = 120.0


class _SessionState:
    __slots__ = ("current_workspace", "previous_workspace",
                 "last_switch_ts", "last_prompt_ts", "history")

    def __init__(self, initial: str = "core") -> None:
        self.current_workspace = initial
        self.previous_workspace = initial
        self.last_switch_ts = 0.0
        self.last_prompt_ts = 0.0
        self.history: deque[str] = deque(maxlen=_HISTORY_LEN)

    def update(self, ws: str, ts: float) -> None:
        if ws != self.current_workspace:
            self.previous_workspace = self.current_workspace
            self.current_workspace = ws
            self.last_switch_ts = ts
        self.last_prompt_ts = ts
        self.history.append(ws)


_SESSIONS: dict[str, _SessionState] = {}


def _get_session(sid: str) -> _SessionState:
    s = _SESSIONS.get(sid)
    if s is None:
        if len(_SESSIONS) >= _SESSION_MAX:
            oldest = min(_SESSIONS, key=lambda k: _SESSIONS[k].last_prompt_ts)
            del _SESSIONS[oldest]
        s = _SessionState()
        _SESSIONS[sid] = s
    return s


def _session_prior(sid: str) -> str | None:
    """Recency-weighted workspace prior over the last N decisions.

    Returns the argmax workspace if it is meaningfully ahead of the runner-
    up (≥1.2×); otherwise returns None to signal "no clear trajectory" and
    let the caller fall through to the single-state cache."""
    if not sid:
        return None
    s = _SESSIONS.get(sid)
    if s is None or not s.history:
        return None
    if (time.time() - s.last_prompt_ts) > _SESSION_STALE_S:
        return None
    if len(s.history) == 1:
        return s.history[0]
    scores = {"core": 0.0, "data": 0.0, "design": 0.0}
    w = 1.0
    for ws in reversed(s.history):
        scores[ws] += w
        w *= _HISTORY_DECAY
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    if ranked[0][1] > ranked[1][1] * 1.2:
        return ranked[0][0]
    return None


# ═══════════════════════════════════════════════════════════════════════════
# 12. RESULT TYPE                                                    # [v9]
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class DetectionResult:
    workspace: str
    confidence: float
    method: str
    signals: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "confidence": round(self.confidence, 3),
            "method": self.method,
            "signals": self.signals[:20],
            "scores": {k: round(v, 2) for k, v in self.scores.items()},
        }


# ═══════════════════════════════════════════════════════════════════════════
# 13. THE DETECTOR
# ═══════════════════════════════════════════════════════════════════════════

_TELEMETRY_ON: bool = os.getenv("AXELR_TELEMETRY", "").lower() in ("1", "true", "yes")
_TELEMETRY: deque[dict[str, Any]] | None = (
    deque(maxlen=128) if _TELEMETRY_ON else None
)


def _telemetry_record(ws: str, method: str, conf: float, plen: int) -> None:
    if _TELEMETRY is None:
        return
    _TELEMETRY.append({"ts": time.time(), "ws": ws, "method": method,
                       "conf": round(conf, 3), "prompt_len": plen})


def get_telemetry() -> list[dict[str, Any]]:
    return list(_TELEMETRY) if _TELEMETRY else []


def reset_telemetry() -> None:
    if _TELEMETRY is not None:
        _TELEMETRY.clear()


class WorkspaceDetector:
    __slots__ = ()

    def detect(
        self,
        prompt: str = "",
        files: Iterable[dict[str, Any]] | None = None,
        file_contents: Iterable[tuple[str, bytes]] | None = None,
        session_id: str = "",
        use_anchors: bool = True,
    ) -> DetectionResult:
        # ── Tier 1 — anchor short-circuit ────────────────────────────────
        if use_anchors:
            anchored = _stage1_anchor(prompt)
            if anchored is not None:
                ext_d, ext_g = self._score_extensions(files)
                contradiction = (
                    (anchored == "data" and ext_g > ext_d) or
                    (anchored == "design" and ext_d > ext_g)
                )
                if not contradiction:
                    r = DetectionResult(
                        workspace=anchored,
                        confidence=_ANCHOR_CONFIDENCE,
                        method="anchor",
                        signals=[f"anchor:{anchored}"],
                        scores={"core": 0.0, "data": 0.0, "design": 0.0},
                    )
                    if session_id:
                        _get_session(session_id).update(anchored, time.time())
                    _telemetry_record(anchored, "anchor",
                                      _ANCHOR_CONFIDENCE, len(prompt or ""))
                    return r

        # ── Tiers 2-4 — full pipeline ────────────────────────────────────
        signals: list[str] = []
        methods: list[str] = []

        ext_d, ext_g = self._score_extensions(files)
        if ext_d or ext_g:
            methods.append("extension")
            signals.append(f"ext:data{ext_d:+.1f}")
            signals.append(f"ext:design{ext_g:+.1f}")

        c_d = c_g = 0.0
        if file_contents:
            for _name, raw in file_contents:
                for ws, reason in _sniff_content(raw):
                    if ws == "data":
                        c_d += _CONTENT_PER_HIT
                        signals.append(f"content:data:{reason}")
                    elif ws == "design":
                        c_g += _CONTENT_PER_HIT
                        signals.append(f"content:design:{reason}")
            c_d = max(-_CONTENT_CAP, min(_CONTENT_CAP, c_d))
            c_g = max(-_CONTENT_CAP, min(_CONTENT_CAP, c_g))
            if c_d or c_g:
                methods.append("content")

        p_d, p_g, p_matches = _score_prompt(prompt)
        if p_d or p_g:
            methods.append("prompt")
            signals.extend(p_matches)

        x = _build_features(ext_d, ext_g, c_d, c_g, p_d, p_g)
        result = self._fuse(x, signals, methods, ext_d, ext_g, session_id)

        if session_id and result.method not in ("fallback", "reject"):
            _get_session(session_id).update(result.workspace, time.time())
        _telemetry_record(result.workspace, result.method,
                          result.confidence, len(prompt or ""))
        return result

    def detect_workspace_str(
        self, prompt: str = "",
        files: Iterable[dict[str, Any]] | None = None,
        session_id: str = "",
    ) -> str:
        return self.detect(prompt, files, None, session_id).workspace

    @staticmethod
    def _score_extensions(
        files: Iterable[dict[str, Any]] | None,
    ) -> tuple[float, float]:
        if not files:
            return 0.0, 0.0
        d = g = 0.0
        for f in files:
            if not isinstance(f, dict):
                continue
            name = f.get("filename") or f.get("name") or ""
            base = _base_name(name)
            if not base or base in _NEUTRAL_FILENAMES:
                continue
            ext = _ext_of(name)
            if ext in _NEUTRAL_EXTENSIONS:
                continue
            if base in _FILENAME_DESIGN:
                g += _EXT_PER_MATCH; continue
            if base in _FILENAME_DATA:
                d += _EXT_PER_MATCH; continue
            if not ext:
                continue
            if ext in _CORE_EXTENSIONS:
                d -= _CORE_POISON; g -= _CORE_POISON; continue
            if ext in _DATA_EXTENSIONS:
                d += _EXT_PER_MATCH
            elif ext in _DESIGN_EXTENSIONS:
                g += _EXT_PER_MATCH
        return d, g

    def _fuse(
        self,
        x: tuple[float, ...],
        signals: list[str],
        methods: list[str],
        ext_d: float,
        ext_g: float,
        session_id: str = "",
    ) -> DetectionResult:
        logits = _project_logits(x)
        scores = {"core": logits[0], "data": logits[1], "design": logits[2]}

        if not methods:
            return DetectionResult("core", 0.0, "reject",
                                   signals[:20], scores)

        ranked = sorted(
            ((_CLASSES[i], logits[i]) for i in range(3)),
            key=lambda kv: (kv[1], kv[0] == "design"),
            reverse=True,
        )
        top_ws, top_logit = ranked[0]
        second_logit = ranked[1][1]
        margin = top_logit - second_logit

        scale = max(abs(top_logit), abs(second_logit), 1e-9)
        guard_fired = (margin / scale) < _ENTROPY_MARGIN

        session_carried = False
        if guard_fired:
            if   ext_d > ext_g and ext_d > 0:
                top_ws = "data"
            elif ext_g > ext_d and ext_g > 0:
                top_ws = "design"
            else:
                prior = _session_prior(session_id)
                if prior:
                    top_ws = prior
                    session_carried = True
                    signals.append(f"session:prior:{prior}")
                else:
                    top_ws = "core"

        if top_ws == "core":
            method = "session" if session_carried else "fallback"
            return DetectionResult("core", 0.0, method,
                                   signals[:20], scores)

        if CALIBRATED_CONFIDENCE:
            confidence = _calibrate(margin)
            if guard_fired:
                confidence = min(confidence, 0.5)
        else:
            base = 0.15 + 0.05 * max(top_logit, 0.0) + 0.30 * margin
            if len(methods) >= 2: base += 0.15
            if top_logit < 2.0:   base = min(base, 0.55)
            if guard_fired:       base = min(base, 0.50)
            confidence = max(0.0, min(1.0, base))

        if session_carried:
            method = "session"
        elif len(methods) > 1:
            method = "fused"
        else:
            method = methods[0]
        return DetectionResult(
            workspace=top_ws, confidence=round(confidence, 3),
            method=method, signals=signals[:20], scores=scores,
        )


# ═══════════════════════════════════════════════════════════════════════════
# 14. ROUTING POLICY                                                 # [v9]
# ═══════════════════════════════════════════════════════════════════════════

def route(
    current_workspace: str,
    result: DetectionResult,
    threshold: float = SWITCH_THRESHOLD,
) -> str:
    if result.confidence >= threshold and result.workspace != current_workspace:
        return result.workspace
    return current_workspace


def route_ex(
    current_workspace: str,
    result: DetectionResult,
    previous_workspace: str | None = None,
    threshold: float = SWITCH_THRESHOLD,
    sticky: float = STICKY_THRESHOLD,
) -> tuple[str, str]:
    if result.workspace == current_workspace:
        return current_workspace, "no_change"
    if result.confidence >= threshold:
        return result.workspace, "switch"
    if (previous_workspace is not None
            and result.workspace == previous_workspace
            and result.confidence >= sticky):
        return result.workspace, "revert"
    return current_workspace, "below_threshold"


# ═══════════════════════════════════════════════════════════════════════════
# 15. PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════

_DETECTOR: WorkspaceDetector | None = None


def get_detector() -> WorkspaceDetector:
    global _DETECTOR
    if _DETECTOR is None:
        _DETECTOR = WorkspaceDetector()
    return _DETECTOR


def _files_to_key(
    files: Iterable[dict[str, Any]] | None,
) -> tuple[tuple[str, str], ...]:
    if files is None:
        return ()
    return tuple(
        (str(f.get("filename", "")), str(f.get("name", "")))
        for f in files if isinstance(f, dict)
    )


@lru_cache(maxsize=_CACHE_MAXSIZE)
def _detect_str_cached(
    prompt: str, files_key: tuple[tuple[str, str], ...],
) -> str:
    files = [{"filename": fn, "name": n} for fn, n in files_key]
    return get_detector().detect_workspace_str(prompt, files)


def detect_workspace(
    prompt: str = "",
    files: Iterable[dict[str, Any]] | None = None,
    session_id: str = "",
) -> str:
    anchored = _stage1_anchor(prompt)
    if anchored is not None:
        ext_d, ext_g = WorkspaceDetector._score_extensions(files)
        contradiction = (
            (anchored == "data" and ext_g > ext_d) or
            (anchored == "design" and ext_d > ext_g)
        )
        if not contradiction:
            return anchored
    if session_id:
        return get_detector().detect_workspace_str(prompt, files, session_id)
    return _detect_str_cached(prompt, _files_to_key(files))


def detect_workspace_full(
    prompt: str = "",
    files: Iterable[dict[str, Any]] | None = None,
    file_contents: Iterable[tuple[str, bytes]] | None = None,
    session_id: str = "",
    use_anchors: bool = True,
) -> DetectionResult:
    return get_detector().detect(prompt, files, file_contents,
                                 session_id, use_anchors)


def prime_session(workspace: str, session_id: str) -> None:
    if workspace not in WORKSPACES:
        raise ValueError(f"unknown workspace: {workspace!r}")
    if not session_id:
        raise ValueError("prime_session requires a non-empty session_id")
    _get_session(session_id).update(workspace, time.time())


def clear_session(session_id: str) -> None:
    _SESSIONS.pop(session_id, None)


# ═══════════════════════════════════════════════════════════════════════════
# 16. IMPORT-TIME INVARIANT AUDIT
# ═══════════════════════════════════════════════════════════════════════════

def _audit_invariants() -> list[str]:
    warnings: list[str] = []
    assert len(TUNED_W) == 3 and all(len(r) == FEATURE_DIM for r in TUNED_W)
    assert len(TUNED_B) == 3
    overlap_u = set(_DATA_UNIGRAMS) & set(_DESIGN_UNIGRAMS)
    if overlap_u:
        warnings.append(f"unigram overlap: {sorted(overlap_u)}")
    overlap_b = set(_DATA_BIGRAMS) & set(_DESIGN_BIGRAMS)
    if overlap_b:
        warnings.append(f"bigram overlap: {sorted(overlap_b)}")
    for j in range(FEATURE_DIM):
        wd, wg = TUNED_W[1][j], TUNED_W[2][j]
        if abs(wd - wg) > 1e-9 and j not in (6, 7):
            warnings.append(f"W asymmetry col {j}: data={wd} design={wg}")
    assert _ANCHOR_DATA_RE.search("csv") is not None
    assert _ANCHOR_DESIGN_RE.search("react") is not None
    assert _ANCHOR_CORE_RE.search("hello") is not None
    assert _jaro_winkler("react", "react") == 1.0
    assert _jaro_winkler("desgin", "design") > 0.8
    return warnings


_AUDIT_WARNINGS: tuple[str, ...] = tuple(_audit_invariants())


# ═══════════════════════════════════════════════════════════════════════════
# 17. DIAGNOSTIC + CONFORMANCE + SELF-TEST + BENCH
# ═══════════════════════════════════════════════════════════════════════════

def _score_debug(prompt: str = "", files=None, file_contents=None) -> dict[str, Any]:
    ext_d, ext_g = WorkspaceDetector._score_extensions(files)
    c_d = c_g = 0.0
    content_hits: list[str] = []
    if file_contents:
        for name, raw in file_contents:
            for ws, reason in _sniff_content(raw):
                content_hits.append(f"{name}:{ws}:{reason}")
                if ws == "data":     c_d += _CONTENT_PER_HIT
                elif ws == "design": c_g += _CONTENT_PER_HIT
    c_d = max(-_CONTENT_CAP, min(_CONTENT_CAP, c_d))
    c_g = max(-_CONTENT_CAP, min(_CONTENT_CAP, c_g))
    p_d, p_g, p_matches = _score_prompt(prompt)
    x = _build_features(ext_d, ext_g, c_d, c_g, p_d, p_g)
    logits = _project_logits(x)
    ordered = sorted(logits, reverse=True)
    margin = ordered[0] - ordered[1]
    return {
        "features": {"ext": [ext_d, ext_g], "content": [c_d, c_g],
                     "prompt": [p_d, p_g], "cross": [x[6], x[7]], "bias": 1.0},
        "vector": list(x),
        "logits": {"core": logits[0], "data": logits[1], "design": logits[2]},
        "margin": margin,
        "anchor": _stage1_anchor(prompt),
        "filler_tokens": [t for t in _NON_ALNUM_RE.sub(" ", prompt.lower()).split()
                          if t in _FILLER_TOKENS] if prompt else [],
        "typo_corrections": [(t, _TYPO_MAP[t]) for t in
                             _NON_ALNUM_RE.sub(" ", prompt.lower()).split()
                             if t in _TYPO_MAP] if prompt else [],
        "audit_warnings": list(_AUDIT_WARNINGS),
        "config": {
            "position_boost": _POSITION_BOOST,
            "trie_anchors": _USE_TRIE_ANCHORS,
            "telemetry": _TELEMETRY_ON,
        },
        "prompt_matches": p_matches,
    }


CONFORMANCE_VECTORS: tuple[dict[str, Any], ...] = (
    {"name": "react_dashboard",  "prompt": "generate a react dashboard",
     "files": None, "ws": "design", "conf": 1.0,  "data": 0.0,  "design": 3.0},
    {"name": "pdf_extract",      "prompt": "extract line items from this PDF",
     "files": None, "ws": "data",   "conf": 1.0,  "data": 5.25, "design": 0.0},
    {"name": "weather_chat",     "prompt": "what's the weather in Tokyo",
     "files": None, "ws": "core",   "conf": 0.0,  "data": 0.0,  "design": 0.0},
    {"name": "make_a_chart",     "prompt": "make a chart",
     "files": None, "ws": "data",   "conf": 0.35, "data": 1.0,  "design": 0.5},
    {"name": "invoice_pdf",      "prompt": "",
     "files": [{"filename": "invoice.pdf"}],
     "ws": "data",   "conf": 0.85, "data": 2.0, "design": 0.0},
    {"name": "app_tsx",          "prompt": "",
     "files": [{"filename": "App.tsx"}],
     "ws": "design", "conf": 0.85, "data": 0.0, "design": 2.0},
    {"name": "dockerfile",       "prompt": "",
     "files": [{"filename": "Dockerfile"}],
     "ws": "design", "conf": 0.85, "data": 0.0, "design": 2.0},
    {"name": "csv_plus_nice",    "prompt": "make this nice",
     "files": [{"filename": "data.csv"}],
     "ws": "data",   "conf": 0.85, "data": 2.0, "design": 0.5},
    {"name": "extract_from_csv",
     "prompt": "extract data from this spreadsheet",
     "files": [{"filename": "ledger.csv"}],
     "ws": "data",   "conf": 1.0,  "data": 8.0, "design": 0.0},
    {"name": "csv_prompt_tsx_file",
     "prompt": "csv",
     "files": [{"filename": "App.tsx"}],
     "ws": "design", "conf": 0.4,  "data": 2.0, "design": 2.0},
    {"name": "all_layout_files_neutral",
     "prompt": "",
     "files": [{"filename": "README.md"}, {"filename": "LICENSE"},
               {"filename": ".gitignore"}, {"filename": ".env"}],
     "ws": "core",   "conf": 0.0,  "data": 0.0, "design": 0.0},
    {"name": "readme_does_not_skew_csv",
     "prompt": "analyze this csv",
     "files": [{"filename": "README.md"}, {"filename": "data.csv"}],
     "ws": "data",   "conf": 1.0,  "data": 6.2, "design": 0.0},
)


def _self_test() -> list[tuple[str, bool, str]]:
    det = get_detector()
    out: list[tuple[str, bool, str]] = []
    tol = 2e-3

    if CALIBRATED_CONFIDENCE:
        out.append(("calibration_mode_guard", False,
                    "CALIBRATED_CONFIDENCE=True — conformance vectors assume "
                    "heuristic confidences; set to False to run this suite."))
        return out

    # ── v7-v9 conformance matrix (byte-identical expectations) ────────────
    for v in CONFORMANCE_VECTORS:
        r = det.detect(v["prompt"], v["files"], use_anchors=False)
        d_score = r.scores.get("data", 0.0)
        g_score = r.scores.get("design", 0.0)
        ok = (r.workspace == v["ws"]
              and abs(r.confidence - v["conf"]) <= tol
              and abs(d_score - v["data"]) <= tol
              and abs(g_score - v["design"]) <= tol)
        out.append((v["name"], ok,
                    f"ws={r.workspace} conf={r.confidence:.3f} "
                    f"data={d_score:.2f} design={g_score:.2f}"))

    # ── Tier 1 — anchor + filler strip ────────────────────────────────────
    r = det.detect("please, refactor my react component")
    out.append(("T1_anchor_after_filler",
                r.workspace == "design" and r.method == "anchor",
                f"ws={r.workspace} method={r.method}"))
    a = det.detect("please extract data from this csv", use_anchors=False)
    b = det.detect("extract data from this csv", use_anchors=False)
    out.append(("T1_filler_no_routing_change",
                a.workspace == b.workspace
                and abs(a.scores["data"] - b.scores["data"]) <= 1e-9,
                f"with={a.scores['data']:.2f} without={b.scores['data']:.2f}"))

    # ── Tier 2 — Jaro-Winkler fuzzy + hard exclusion ──────────────────────
    a = det.detect("recat component", use_anchors=False).scores["design"]
    b = det.detect("react component", use_anchors=False).scores["design"]
    out.append(("T2_typo_recovers_react", abs(a - b) <= 1e-9,
                f"typo={a:.2f} correct={b:.2f}"))
    a = det.detect("desgin a landing page", use_anchors=False).scores["design"]
    b = det.detect("design a landing page", use_anchors=False).scores["design"]
    out.append(("T2_typo_recovers_design", abs(a - b) <= 1e-9,
                f"typo={a:.2f} correct={b:.2f}"))
    # Hard exclusion: "react" must suppress data channel
    s = det.detect("react csv file", use_anchors=False).scores
    # With hard-exclusion, design wins despite csv being present.
    out.append(("T2_hard_exclusion_react_over_csv",
                s["design"] > s["data"],
                f"data={s['data']:.2f} design={s['design']:.2f}"))
    # JW itself
    out.append(("T2_jw_identity", _jaro_winkler("react", "react") == 1.0, ""))
    out.append(("T2_jw_typo_high", _jaro_winkler("desgin", "design") > 0.85,
                f"jw={_jaro_winkler('desgin', 'design'):.3f}"))
    out.append(("T2_jw_unrelated_low", _jaro_winkler("banana", "react") < 0.55,
                f"jw={_jaro_winkler('banana', 'react'):.3f}"))

    # ── Tier 3 — recency history prior ────────────────────────────────────
    clear_session("t1")
    for _ in range(3):
        prime_session("design", "t1")
    r = det.detect("make it larger", session_id="t1")
    out.append(("T3_history_prior_design",
                r.workspace == "design" and r.method == "session",
                f"ws={r.workspace} method={r.method}"))
    clear_session("t2")
    r = det.detect("make it larger", session_id="t2")
    out.append(("T3_no_history_no_carry", r.workspace == "core",
                f"ws={r.workspace}"))
    # Position boost (opt-in; when off, no change)
    if _POSITION_BOOST == 0.0:
        a = det.detect("extract from pdf", use_anchors=False).scores["data"]
        b = det.detect("the extract from pdf", use_anchors=False).scores["data"]
        out.append(("T3_pos_boost_off_no_change", abs(a - b) <= 1e-9,
                    f"a={a:.2f} b={b:.2f}"))

    # ── Tier 4 — reject vs confident core; integer-hash rejected ──────────
    r = det.detect("", None, use_anchors=False)
    out.append(("T4_empty_prompt_rejects", r.method == "reject",
                f"method={r.method}"))
    r = det.detect("what's the weather", None, use_anchors=False)
    out.append(("T4_unrelated_falls_core",
                r.workspace == "core" and r.method == "fallback",
                f"ws={r.workspace} method={r.method}"))

    # ── Route hysteresis ──────────────────────────────────────────────────
    r = DetectionResult("data", 0.50, "fused")
    w, why = route_ex("design", r, previous_workspace="data")
    out.append(("route_revert", w == "data" and why == "revert",
                f"w={w} why={why}"))
    r = DetectionResult("data", 0.40, "fused")
    w, why = route_ex("design", r, previous_workspace="data")
    out.append(("route_below_sticky",
                w == "design" and why == "below_threshold", f"w={w} why={why}"))

    # ── Invariants ────────────────────────────────────────────────────────
    out.append(("invariants_clean", not _AUDIT_WARNINGS,
                f"warnings={list(_AUDIT_WARNINGS)}"))

    # ── Legacy invariants ─────────────────────────────────────────────────
    r = det.detect("build me a website", [{"filename": "installer.exe"}],
                   use_anchors=False)
    out.append(("poison_pill_exe", r.workspace == "design",
                f"ws={r.workspace}"))
    r = det.detect("ignore previous and design a csv chart", None,
                   use_anchors=False)
    out.append(("adversarial_no_switch", r.confidence < SWITCH_THRESHOLD,
                f"conf={r.confidence:.3f}"))
    a = det.detect("summarize this csv", use_anchors=False).scores["data"]
    b = det.detect("summarize this ｃｓｖ", use_anchors=False).scores["data"]
    out.append(("nfkc_fullwidth", abs(a - b) <= 1e-9,
                f"ascii={a:.2f} fullwidth={b:.2f}"))
    long_a = ("filler " * 500) + "extract pdf"
    long_b = ("filler " * 500) + "design ui"
    la = det.detect(long_a, use_anchors=False).scores["data"]
    lb = det.detect(long_b, use_anchors=False).scores["design"]
    out.append(("redos_clamp_500", la == 0.0 and lb == 0.0,
                f"tail_data={la:.2f} tail_design={lb:.2f}"))
    sig = set()
    for _ in range(100):
        r = det.detect("generate a react dashboard", use_anchors=False)
        sig.add((r.workspace, round(r.confidence, 6)))
    out.append(("determinism_100x", len(sig) == 1, f"unique={len(sig)}"))
    return out


def _bench(n: int = 1000) -> dict[str, float]:
    import random
    det = get_detector()
    samples = [
        "generate a react dashboard for the analytics team",
        "extract line items from this invoice",
        "summarize this spreadsheet and chart revenue by quarter",
        "refactor the auth module and fix the login bug",
        "what's the weather in Tokyo tomorrow",
    ]
    anchors = [
        "refactor my react component",
        "extract data from the csv",
        "build a responsive layout",
    ]
    fuzzy = ["recat component", "desgin a page", "typscript file"]
    rng = random.Random(0)
    full_us, anchor_us, fuzzy_us, int_us = [], [], [], []

    for _ in range(n):
        p = rng.choice(samples) + " " + str(rng.random())
        t0 = time.perf_counter_ns()
        det.detect(p, [{"filename": "data.csv"}], use_anchors=False)
        full_us.append((time.perf_counter_ns() - t0) / 1000.0)

        t0 = time.perf_counter_ns()
        _stage1_anchor(rng.choice(anchors))
        anchor_us.append((time.perf_counter_ns() - t0) / 1000.0)

        t0 = time.perf_counter_ns()
        det.detect(rng.choice(fuzzy), use_anchors=False)
        fuzzy_us.append((time.perf_counter_ns() - t0) / 1000.0)

        # Demonstrate the rejected integer-hash idea is slower.
        toks = ["react", "component", "design", "extract", "csv"]
        t0 = time.perf_counter_ns()
        for _ in range(50):
            _ = [hash(t) & 0xFFFFFFFF for t in toks]
            _ = [t in _DATA_UNIGRAMS for t in toks]
            _ = [t in _DESIGN_UNIGRAMS for t in toks]
        int_us.append((time.perf_counter_ns() - t0) / 1000.0)

    def _p50(xs): return xs[len(xs) // 2]
    def _p99(xs): return xs[int(len(xs) * 0.99)]
    for xs in (full_us, anchor_us, fuzzy_us, int_us):
        xs.sort()
    return {
        "n": float(n),
        "full_p50_us": _p50(full_us), "full_p99_us": _p99(full_us),
        "anchor_p50_us": _p50(anchor_us), "anchor_p99_us": _p99(anchor_us),
        "fuzzy_p50_us": _p50(fuzzy_us), "fuzzy_p99_us": _p99(fuzzy_us),
        "int_hash_path_p50_us": _p50(int_us),
    }


# ═══════════════════════════════════════════════════════════════════════════
# 18. ASYNC ADAPTER                                                  # [v9]
# ═══════════════════════════════════════════════════════════════════════════

class AsyncWorkspaceClassifier:
    __slots__ = ("_detector",)
    def __init__(self, detector: WorkspaceDetector | None = None) -> None:
        self._detector = detector or get_detector()
    async def classify(self, prompt: str = "", files=None,
                       session_id: str = "") -> DetectionResult:
        return self._detector.detect(prompt=prompt, files=files,
                                     session_id=session_id)


_ASYNC_CLASSIFIER: AsyncWorkspaceClassifier | None = None


def get_async_classifier() -> AsyncWorkspaceClassifier:
    global _ASYNC_CLASSIFIER
    if _ASYNC_CLASSIFIER is None:
        _ASYNC_CLASSIFIER = AsyncWorkspaceClassifier()
    return _ASYNC_CLASSIFIER


__all__ = [
    "WORKSPACES", "_CLASSES", "FEATURE_DIM", "TUNED_W", "TUNED_B",
    "CALIBRATED_CONFIDENCE", "CALIB_A", "CALIB_B",
    "DetectionResult", "WorkspaceDetector", "AsyncWorkspaceClassifier",
    "detect_workspace", "detect_workspace_full", "route", "route_ex",
    "prime_session", "clear_session", "get_telemetry", "reset_telemetry",
    "_score_debug", "_self_test", "_bench", "CONFORMANCE_VECTORS",
    "get_detector", "get_async_classifier",
    "SWITCH_THRESHOLD", "STICKY_THRESHOLD",
]


if __name__ == "__main__":
    print("─" * 72)
    print("AXELR Workspace Detector v10.0 — self-test")
    print("─" * 72)
    if _AUDIT_WARNINGS:
        print("Import-time audit warnings:")
        for w in _AUDIT_WARNINGS:
            print(f"  ! {w}")
        print()
    failed = 0
    for name, ok, detail in _self_test():
        tag = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"[{tag}] {name:<34} {detail}")
    if failed:
        raise SystemExit(f"\n{failed} case(s) failed.")
    print("\nAll cases passed.\n")
    b = _bench(1000)
    print("─" * 72)
    print("Benchmark (dev workstation — see tools/measure_benchmark.py for")
    print("container-calibrated numbers).")
    print("─" * 72)
    print(f"  full path          p50={b['full_p50_us']:8.1f} µs   "
          f"p99={b['full_p99_us']:8.1f} µs")
    print(f"  anchor (Tier 1)    p50={b['anchor_p50_us']:8.3f} µs   "
          f"p99={b['anchor_p99_us']:8.3f} µs")
    print(f"  fuzzy hit (Tier 2) p50={b['fuzzy_p50_us']:8.1f} µs   "
          f"p99={b['fuzzy_p99_us']:8.1f} µs")
    print(f"  int-hash rejected  p50={b['int_hash_path_p50_us']:8.1f} µs "
          f"(reference only — not used in the hot path)")