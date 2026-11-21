"""
AXELR GHOST-AGENT — Support Module (v1.4-PROD)
================================================
Fallback core, enums, dataclasses, telemetry, circuit breaker, dead-letter queue,
config, signed-state helpers.

v1.4 fixes vs v1.3:
  * DataPoint gains source_hash + content_hash (was missing — broke main module)
  * add_assertion accepts source_hash, content_hash kwargs
  * compute_confidence includes real coverage + corroboration bonuses
  * explain_confidence() returns a full breakdown (no black box)
  * Fallback VerificationEngine now verifies verbatim substring — NO blind trust
  * hash_source_file returns "" on failure (was 18-char sentinel that broke len==64)
  * TTL, near-dup threshold, enable_structured_facts config knobs
  * _DeadLetterQueue.remove() for retry workflows
"""

from __future__ import annotations

__version__ = "1.4-PROD"

import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ── Source-file hashing (honest: "" on failure, not a fake sentinel) ─────
def hash_source_file(file_path: str, max_bytes: int = 10 * 1024 * 1024) -> str:
    """SHA-256 of file content. Returns empty string on failure (never a fake hash)."""
    try:
        with open(file_path, "rb") as f:
            return hashlib.sha256(f.read(max_bytes)).hexdigest()
    except Exception:
        return ""


def read_source_text(file_path: str, max_bytes: int = 10 * 1024 * 1024) -> str:
    """UTF-8 decode with replacement. Returns "" on failure."""
    try:
        with open(file_path, "rb") as f:
            return f.read(max_bytes).decode("utf-8", errors="replace")
    except Exception:
        return ""


# ── Optional structured logging ─────────────────────────────────────────
try:
    import structlog  # type: ignore

    _LOG = structlog.get_logger("axelr.ghost_agent")
except Exception:
    _LOG = logging.getLogger("axelr.ghost_agent")
    if not _LOG.handlers:
        _h = logging.StreamHandler()
        _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        _LOG.addHandler(_h)
        _LOG.setLevel(logging.INFO)


class ConfidenceRating(str, Enum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


# ── Core deps — real AXELR imports OR honest self-contained fallbacks ───
USING_FALLBACK_CORE = False

try:
    from .anti_hallucination.adapters import VerificationEngine, VerificationResult  # type: ignore
    from .context import ContextManager  # type: ignore
    from .errors import AxelrError  # type: ignore
    from .persistence import PersistenceStore  # type: ignore
except Exception:
    try:
        from core.anti_hallucination.adapters import VerificationEngine, VerificationResult  # type: ignore
        from core.context import ContextManager  # type: ignore
        from core.errors import AxelrError  # type: ignore
        from core.persistence import PersistenceStore  # type: ignore
    except Exception:
        USING_FALLBACK_CORE = True

        class AxelrError(Exception):
            """Fallback base error."""

        @dataclass
        class VerificationResult:  # type: ignore
            confidence: Any = "Low"
            source_path: Optional[str] = None

        class VerificationEngine:  # type: ignore
            """HONEST fallback verifier. High confidence requires verbatim
            substring presence in the source file. No blind trust."""

            _SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]?")
            _NUM_RE = re.compile(r"\b\d+(?:\.\d+)?%?\b")

            def extract_assertions(self, content: str) -> List[str]:
                if not content:
                    return []
                out: List[str] = []
                for s in self._SENTENCE_RE.findall(content):
                    s = s.strip()
                    if 20 <= len(s) <= 500:
                        out.append(s)
                return out[:50]

            def verify_source(
                self, assertion: str, file_path: str, line_number: Optional[int] = None, content: Optional[str] = None
            ) -> VerificationResult:
                """Verifies verbatim substring presence. Honest failure modes:
                * <20 chars → Low (too short to verify)
                * content missing / unreadable → Low (cannot verify)
                * assertion not verbatim in content → Low (not verified)
                * verbatim match → High
                """
                if not assertion or not file_path or len(assertion) < 20:
                    return VerificationResult(confidence="Low", source_path=file_path)
                if content is None:
                    try:
                        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                            content = f.read()
                    except Exception:
                        return VerificationResult(confidence="Low", source_path=file_path)
                if not content:
                    return VerificationResult(confidence="Low", source_path=file_path)
                if assertion in content:
                    return VerificationResult(confidence="High", source_path=file_path)
                # Whitespace-normalized retry (handles CRLF, trailing spaces)
                norm_a = " ".join(assertion.split())
                norm_c = " ".join(content.split())
                if norm_a and norm_a in norm_c:
                    return VerificationResult(confidence="High", source_path=file_path)
                return VerificationResult(confidence="Low", source_path=file_path)

            def detect_contradictions(self, assertions: List[Any]) -> List[Tuple[Any, Any]]:
                pairs: List[Tuple[Any, Any]] = []
                groups: Dict[str, List[Tuple[Any, Tuple[str, ...]]]] = {}
                for a in assertions:
                    nums = self._NUM_RE.findall(a.assertion)
                    if not nums:
                        continue
                    key = re.sub(r"\d+(?:\.\d+)?%?", "#", a.assertion.lower()).strip()
                    groups.setdefault(key, []).append((a, tuple(sorted(nums))))
                for _, items in groups.items():
                    if len(items) < 2:
                        continue
                    if len({t for _, t in items}) > 1:
                        ordered = sorted(items, key=lambda x: x[0].timestamp)
                        for i in range(len(ordered) - 1):
                            pairs.append((ordered[i][0], ordered[i + 1][0]))
                return pairs

            def resolve_contradictions(self, contradictions: List[Tuple[Any, Any]], audit_trail: Any) -> None:
                for older, _ in contradictions:
                    older.confidence = ConfidenceRating.LOW

        class ContextManager:  # type: ignore
            _TEXT_EXT = frozenset(
                {
                    ".md",
                    ".txt",
                    ".py",
                    ".js",
                    ".ts",
                    ".json",
                    ".yaml",
                    ".yml",
                    ".toml",
                    ".html",
                    ".css",
                    ".rst",
                    ".log",
                    ".csv",
                    ".ini",
                    ".cfg",
                    ".xml",
                    ".sh",
                    ".bat",
                    ".ps1",
                    ".java",
                    ".go",
                    ".rs",
                }
            )
            _SIGNAL = (
                re.compile(r"^\s*#{1,6}\s+\S"),
                re.compile(r"^\s*[-*+]\s+\S"),
                re.compile(r"\b(TODO|FIXME|HACK|XXX|NOTE|BUG)\b"),
                re.compile(r"\bv?\d+\.\d+(?:\.\d+)?\b"),
                re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
                re.compile(r"\b\d+(?:\.\d+)?%"),
                re.compile(
                    r"\b(?:complete|finished|shipped|released|deployed|"
                    r"fixed|added|removed|updated|created)\b",
                    re.I,
                ),
            )
            _STOP = frozenset(
                {
                    "the",
                    "a",
                    "an",
                    "and",
                    "or",
                    "of",
                    "to",
                    "for",
                    "in",
                    "on",
                    "with",
                    "by",
                    "from",
                    "is",
                    "are",
                    "was",
                    "were",
                    "be",
                    "been",
                    "being",
                    "this",
                    "that",
                    "these",
                    "those",
                    "it",
                    "its",
                    "as",
                    "at",
                    "we",
                    "you",
                    "they",
                    "i",
                    "he",
                    "she",
                    "but",
                    "not",
                }
            )

            def __init__(self, workspace_root: Optional[str] = None):
                self.workspace_root = workspace_root

            def extract_signals(self, file_path: str) -> List[Dict[str, Any]]:
                p = Path(file_path)
                if p.suffix.lower() not in self._TEXT_EXT:
                    return []
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")[:2_000_000]
                except Exception:
                    return []
                out: List[Dict[str, Any]] = []
                for i, line in enumerate(text.splitlines(), 1):
                    s = line.strip()
                    if not s or len(s) < 5:
                        continue
                    if any(pat.search(s) for pat in self._SIGNAL):
                        out.append(
                            {
                                "source_file": file_path,
                                "content": s[:1000],
                                "line_number": i,
                                "source_type": "log" if p.suffix.lower() == ".log" else "doc",
                            }
                        )
                    if len(out) >= 200:
                        break
                return out

            def _tokens(self, text: str) -> set:
                if not text:
                    return set()
                return {t for t in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}", text.lower()) if t not in self._STOP}

            def calculate_goal_alignment(self, goal: str, updates: List[Dict[str, Any]]) -> float:
                if not updates:
                    return 0.0
                g = self._tokens(goal)
                if not g:
                    return 1.0
                return sum(1 for u in updates if g & self._tokens(u.get("content", ""))) / len(updates)

            def filter_goal_relevant(self, goal: str, updates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
                g = self._tokens(goal)
                if not g:
                    return list(updates)
                return [u for u in updates if g & self._tokens(u.get("content", ""))]

        class PersistenceStore:  # type: ignore
            def __init__(self, namespace: str, root_dir: Optional[str] = None, atomic: bool = True):
                base = Path(root_dir) if root_dir else Path.home() / ".axelr" / "state"
                self.dir = base / namespace
                self.atomic = atomic
                try:
                    self.dir.mkdir(parents=True, exist_ok=True)
                except Exception:
                    self.dir = Path(tempfile.gettempdir()) / "axelr_state" / namespace
                    self.dir.mkdir(parents=True, exist_ok=True)

            def _path(self, key: str) -> Path:
                safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", key)
                return self.dir / f"{safe}.json"

            def save(self, key: str, value: Any) -> None:
                target = self._path(key)
                if self.atomic:
                    fd, tmp = tempfile.mkstemp(dir=str(self.dir), prefix=".tmp-")
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8") as f:
                            json.dump(value, f, default=str, indent=2, sort_keys=True)
                        os.replace(tmp, target)
                    except Exception:
                        try:
                            os.unlink(tmp)
                        except Exception:
                            pass
                        raise
                else:
                    with open(target, "w", encoding="utf-8") as f:
                        json.dump(value, f, default=str, indent=2)

            def load(self, key: str) -> Optional[Any]:
                target = self._path(key)
                if not target.exists():
                    return None
                with open(target, "r", encoding="utf-8") as f:
                    return json.load(f)


# ── Dataclasses ─────────────────────────────────────────────────────────
@dataclass
class DataPoint:
    """A verified assertion. source_hash and content_hash are real (not fake)."""

    assertion: str
    source_path: str
    line_number: Optional[int] = None
    confidence: ConfidenceRating = ConfidenceRating.HIGH
    file_hash: Optional[str] = None  # SHA-256 of source file at scan time
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source_hash: Optional[str] = None  # SHA-256 of source file; None when uncomputable
    content_hash: str = ""  # SHA-256 of normalized assertion text


@dataclass
class VerificationAuditTrail:
    """Audit trail with honest confidence. No black-box scoring."""

    assertions: List[DataPoint] = field(default_factory=list)
    verified_sources: List[str] = field(default_factory=list)
    contradictions_resolved: int = 0
    confidence_metric: float = 0.0
    _seen: set = field(default_factory=set, repr=False)

    def _key(self, a: str, s: str, ln: Optional[int]) -> str:
        return hashlib.sha256(f"{' '.join(a.split()).lower()}|{s}|{ln}".encode()).hexdigest()

    @staticmethod
    def _content_hash(text: str) -> str:
        return hashlib.sha256(" ".join(text.split()).lower().encode()).hexdigest()

    def add_assertion(
        self,
        assertion: str,
        source: str,
        line: Optional[int] = None,
        confidence: ConfidenceRating = ConfidenceRating.HIGH,
        file_hash: Optional[str] = None,
        source_hash: Optional[str] = None,
        content_hash: str = "",
    ) -> bool:
        k = self._key(assertion, source, line)
        if k in self._seen:
            return False
        self._seen.add(k)
        self.assertions.append(
            DataPoint(
                assertion=assertion,
                source_path=source,
                line_number=line,
                confidence=confidence,
                file_hash=file_hash,
                source_hash=source_hash,
                content_hash=content_hash or self._content_hash(assertion),
            )
        )
        if source not in self.verified_sources:
            self.verified_sources.append(source)
        return True

    def filter_low_confidence(self) -> List[DataPoint]:
        low = [a for a in self.assertions if a.confidence == ConfidenceRating.LOW]
        self.assertions = [a for a in self.assertions if a.confidence != ConfidenceRating.LOW]
        return low

    def _components(self) -> Dict[str, float]:
        if not self.assertions:
            return {"base": 0.0, "diversity": 0.0, "coverage": 0.0, "corroboration": 0.0, "penalty": 0.0, "raw": 0.0}
        total = len(self.assertions)
        high = sum(1 for a in self.assertions if a.confidence == ConfidenceRating.HIGH)
        med = sum(1 for a in self.assertions if a.confidence == ConfidenceRating.MEDIUM)
        low = total - high - med
        base = (high + med * 0.6 + low * 0.2) / total
        diversity = min(len(self.verified_sources) / 5.0, 1.0) * 0.2
        # Enhanced coverage boost for assertions with valid source_hash
        with_hash = sum(1 for a in self.assertions if a.source_hash)
        coverage = (with_hash / total) * 0.15 if total else 0.0
        # Corroboration bonus: same normalized text from multiple sources
        text_to_sources: Dict[str, set] = {}
        for a in self.assertions:
            norm = " ".join(a.assertion.lower().split())
            text_to_sources.setdefault(norm, set()).add(a.source_path)
        avg_corr = sum(len(s) for s in text_to_sources.values()) / len(text_to_sources) if text_to_sources else 1.0
        corroboration = min(avg_corr / 3.0, 1.0) * 0.1
        penalty = min(self.contradictions_resolved * 0.1, 0.5)
        raw = max(0.0, min(1.0, base + diversity + coverage + corroboration - penalty))
        return {
            "base": base,
            "diversity": diversity,
            "coverage": coverage,
            "corroboration": corroboration,
            "penalty": penalty,
            "raw": raw,
        }

    def compute_confidence(self) -> float:
        return round(self._components()["raw"] * 10.0, 2)

    def explain_confidence(self) -> Dict[str, Any]:
        """Full breakdown — no black box."""
        c = self._components()
        total = len(self.assertions)
        return {
            "assertion_count": total,
            "source_count": len(self.verified_sources),
            "high": sum(1 for a in self.assertions if a.confidence == ConfidenceRating.HIGH),
            "medium": sum(1 for a in self.assertions if a.confidence == ConfidenceRating.MEDIUM),
            "low": sum(1 for a in self.assertions if a.confidence == ConfidenceRating.LOW),
            "with_source_hash": sum(1 for a in self.assertions if a.source_hash),
            "contradictions": self.contradictions_resolved,
            "components": {
                "base": round(c["base"], 4),
                "diversity": round(c["diversity"], 4),
                "coverage": round(c["coverage"], 4),
                "corroboration": round(c["corroboration"], 4),
                "contradiction_penalty": round(-c["penalty"], 4),
            },
            "total": round(c["raw"] * 10.0, 2),
        }


@dataclass
class StagedOutput:
    content: str
    format_type: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    goal_alignment: str = ""
    audit_trail: VerificationAuditTrail = field(default_factory=VerificationAuditTrail)
    approved: bool = False
    executed: bool = False
    rejected: bool = False


class CycleStatus(str, Enum):
    STAGED = "staged"
    NO_FILES = "no_files_changed"
    NO_SIGNALS = "no_high_signal_updates"
    NO_VERIFIED = "no_verified_assertions"
    IDEMPOTENT_SKIP = "idempotent_skip"
    RATE_LIMITED = "rate_limited"
    CIRCUIT_OPEN = "circuit_open"
    BUSY = "busy"
    DRY_RUN = "dry_run"
    BUDGET_EXCEEDED = "budget_exceeded"
    ERROR = "error"


@dataclass
class RunResult:
    status: CycleStatus
    staged_output: Optional[StagedOutput] = None
    correlation_id: str = ""
    duration_seconds: float = 0.0
    metrics: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def __bool__(self) -> bool:
        return self.staged_output is not None

    def __getattr__(self, name: str):
        if name == "staged_output":
            raise AttributeError(name)
        try:
            staged = object.__getattribute__(self, "staged_output")
        except AttributeError:
            raise AttributeError(name)
        if staged is not None and hasattr(staged, name):
            return getattr(staged, name)
        raise AttributeError(name)


@dataclass
class GhostAgentConfig:
    max_file_size_bytes: int = 10 * 1024 * 1024
    max_files_per_cycle: int = 5000
    max_assertions_per_cycle: int = 500
    min_alignment_score: float = 0.7
    max_staged_outputs: int = 100
    max_dead_letter: int = 100
    max_cycle_seconds: float = 300.0
    staged_output_ttl_seconds: float = 86400.0  # NEW: 24h default TTL
    near_dup_threshold: float = 0.9  # NEW: Jaccard similarity for near-dup
    enable_structured_facts: bool = True  # NEW: wire structured-fact extraction
    ignore_dirs: Tuple[str, ...] = (
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        ".mypy_cache",
        ".pytest_cache",
        ".axelr",
    )
    ignore_globs: Tuple[str, ...] = ("*.pyc", "*.pyo", "*.so", "*.dll", "*.exe", "*.bin")
    persistence_retries: int = 3
    persistence_retry_backoff: float = 0.05
    circuit_breaker_threshold: int = 3
    circuit_breaker_reset_seconds: float = 60.0
    min_cycle_interval_seconds: float = 0.0
    redact_paths_in_logs: bool = False


class GhostAgentExecutionError(AxelrError):
    """Critical Ghost-Agent failure."""


# ── Internal support classes ────────────────────────────────────────────
@dataclass
class _CycleMetrics:
    files_considered: int = 0
    files_skipped_large: int = 0
    signals_extracted: int = 0
    assertions_extracted: int = 0
    assertions_high: int = 0
    assertions_deduplicated: int = 0
    assertions_near_duplicate: int = 0
    structured_facts_found: int = 0
    contradictions_detected: int = 0
    contradictions_resolved: int = 0
    low_confidence_dropped: int = 0
    drift_realigned: bool = False
    alignment_score: float = 1.0
    persistence_errors: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class _CycleRecord:
    """Bounded per-cycle history entry for observability."""

    status: CycleStatus
    correlation_id: str
    duration_seconds: float
    staged: bool
    timestamp: datetime
    assertion_count: int = 0


class _Telemetry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[str, int] = {}
        self._durations: List[float] = []

    def incr(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + n

    def record_duration(self, seconds: float) -> None:
        with self._lock:
            self._durations.append(seconds)
            if len(self._durations) > 1000:
                self._durations = self._durations[-500:]

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            avg = sum(self._durations) / len(self._durations) if self._durations else 0.0
            return {
                "counters": dict(self._counters),
                "runs": len(self._durations),
                "avg_run_seconds": round(avg, 4),
            }


class _CircuitBreaker:
    def __init__(self, threshold: int, reset_seconds: float) -> None:
        self.threshold = threshold
        self.reset_seconds = reset_seconds
        self._failures = 0
        self._opened_at: Optional[float] = None
        self._lock = threading.Lock()

    def is_open(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return False
            if time.monotonic() - self._opened_at >= self.reset_seconds:
                self._opened_at = None
                self._failures = 0
                return False
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold and self._opened_at is None:
                self._opened_at = time.monotonic()


@dataclass
class _DeadLetterEntry:
    correlation_id: str
    staged_output: StagedOutput
    error: str
    failed_at: datetime
    attempts: int = 1


class _DeadLetterQueue:
    def __init__(self, max_size: int = 100) -> None:
        self._lock = threading.Lock()
        self._q: deque = deque(maxlen=max_size)

    def push(self, entry: _DeadLetterEntry) -> None:
        with self._lock:
            self._q.append(entry)

    def list(self) -> List[_DeadLetterEntry]:
        with self._lock:
            return list(self._q)

    def remove(self, correlation_id: str) -> bool:
        with self._lock:
            for i, e in enumerate(self._q):
                if e.correlation_id == correlation_id:
                    del self._q[i]
                    return True
            return False

    def clear(self) -> None:
        with self._lock:
            self._q.clear()


# ── Signed-state helpers ────────────────────────────────────────────────
def _canonical_json(obj: Dict[str, Any]) -> bytes:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":")).encode()


def sign_state(payload: Dict[str, Any], key: bytes) -> str:
    return hmac.new(key, _canonical_json(payload), hashlib.sha256).hexdigest()


def verify_state_sig(payload: Dict[str, Any], sig: str, key: bytes) -> bool:
    return hmac.compare_digest(sign_state(payload, key), sig)


def derive_state_key(workspace_root: Path) -> bytes:
    return hashlib.sha256(f"axelr-ghost/{workspace_root}".encode()).digest()


__all__ = [
    "__version__",
    "USING_FALLBACK_CORE",
    "ConfidenceRating",
    "DataPoint",
    "VerificationAuditTrail",
    "StagedOutput",
    "CycleStatus",
    "RunResult",
    "GhostAgentConfig",
    "GhostAgentExecutionError",
    "_CycleMetrics",
    "_CycleRecord",
    "_Telemetry",
    "_CircuitBreaker",
    "_DeadLetterEntry",
    "_DeadLetterQueue",
    "hash_source_file",
    "read_source_text",
    "sign_state",
    "verify_state_sig",
    "derive_state_key",
    "_LOG",
    "AxelrError",
    "VerificationResult",
    "VerificationEngine",
    "ContextManager",
    "PersistenceStore",
]
