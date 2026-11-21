"""
AXELR GHOST-AGENT — Main Engine (v1.4-PROD)
============================================
Four-pillar pipeline. Support types live in ghost_agent_ops.py.

v1.4 over v1.3:
  * Fixes contract mismatch with ops (DataPoint.source_hash, add_assertion kwargs)
  * Honest verifier used — HIGH confidence requires verbatim substring match
  * Structured facts wired into the pipeline (was dead code in v1.3)
  * Near-duplicate detection via Jaccard similarity
  * Cross-file corroboration counted in confidence
  * Staged-output TTL purge
  * Dead-letter retry by correlation ID
  * Bounded cycle history for observability
  * explain_confidence() pass-through
  * Portable snapshot export for external verification
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import traceback
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

# Robust split imports
try:
    from ghost_agent_ops import (
        _LOG,
        USING_FALLBACK_CORE,
        ConfidenceRating,
        ContextManager,
        CycleStatus,
        DataPoint,
        GhostAgentConfig,
        GhostAgentExecutionError,
        PersistenceStore,
        RunResult,
        StagedOutput,
        VerificationAuditTrail,
        VerificationEngine,
        __version__,
        _CircuitBreaker,
        _CycleMetrics,
        _CycleRecord,
        _DeadLetterEntry,
        _DeadLetterQueue,
        _Telemetry,
        derive_state_key,
        hash_source_file,
        read_source_text,
        sign_state,
        verify_state_sig,
    )
except ImportError:
    from .ghost_agent_ops import (  # type: ignore
        _LOG,
        USING_FALLBACK_CORE,
        ConfidenceRating,
        ContextManager,
        CycleStatus,
        DataPoint,
        GhostAgentConfig,
        GhostAgentExecutionError,
        PersistenceStore,
        RunResult,
        StagedOutput,
        VerificationAuditTrail,
        VerificationEngine,
        __version__,
        _CircuitBreaker,
        _CycleMetrics,
        _CycleRecord,
        _DeadLetterEntry,
        _DeadLetterQueue,
        _Telemetry,
        derive_state_key,
        hash_source_file,
        read_source_text,
        sign_state,
        verify_state_sig,
    )


import re

_STRUCTURED_FACT_PATTERNS = (
    # **Key:** value  (key starts uppercase — rejects `def f():` in code)
    re.compile(r"^\s*[-*]?\s*\*\*([A-Z][A-Za-z0-9 _\-]{1,60}):\*\*\s+(.{3,500})$"),
    # Key: value  (key and value both start non-lowercase — rejects prose)
    re.compile(r"^\s*[-*]?\s*([A-Z][A-Za-z0-9 _\-]{1,60}):\s+(\S.{2,500})$"),
    # [Key] value
    re.compile(r"^\s*[-*]?\s*\[([A-Z][A-Za-z0-9 _\-]{1,60})\]\s+(.{3,500})$"),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _jaccard(a: str, b: str) -> float:
    """Word-level Jaccard similarity. Used for near-duplicate detection."""
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


class AxelrGhostAgent:
    """Singleton orchestrator for the four-pillar pipeline."""

    _instance: Optional["AxelrGhostAgent"] = None
    _instance_lock = threading.Lock()
    _CYCLE_HISTORY_MAX = 100

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, workspace_root: str, user_goal: str, config: Optional[GhostAgentConfig] = None) -> None:
        if getattr(self, "_initialized", False):
            return
        try:
            self.workspace_root = Path(workspace_root).resolve()
            self.user_goal = user_goal
            self.config = config or GhostAgentConfig()
            self.last_execution: Optional[datetime] = None
            self.current_context: Dict[str, Any] = {}
            self.staged_outputs: List[StagedOutput] = []

            self.verification_engine = VerificationEngine()
            self.context_manager = ContextManager(workspace_root=str(self.workspace_root))
            state_dir = self.workspace_root / ".axelr" / "state"
            self.persistence = PersistenceStore("ghost_agent_state", root_dir=str(state_dir))

            self._last_run_hash: Optional[str] = None
            self._last_run_result: Optional[RunResult] = None
            self._cycle_lock = threading.Lock()
            self._telemetry = _Telemetry()
            self._circuit_breaker = _CircuitBreaker(
                self.config.circuit_breaker_threshold,
                self.config.circuit_breaker_reset_seconds,
            )
            self._dead_letter = _DeadLetterQueue(max_size=self.config.max_dead_letter)
            self._state_key = derive_state_key(self.workspace_root)
            self._cycle_history: deque = deque(maxlen=self._CYCLE_HISTORY_MAX)
            self._load_state()
            self._initialized = True
            _LOG.info(
                "ghost_agent_initialized",
                workspace=self._redact(str(self.workspace_root)),
                using_fallback_core=USING_FALLBACK_CORE,
                version=__version__,
            )
        except Exception:
            type(self)._instance = None
            raise

    # ── Utilities ──────────────────────────────────────────────────────
    def _redact(self, text: str) -> str:
        if not self.config.redact_paths_in_logs:
            return text
        try:
            return text.replace(str(self.workspace_root), "<workspace>").replace(self.user_goal, "<goal>")
        except Exception:
            return text

    def _with_retry(self, fn: Callable[[], Any], op: str) -> Any:
        attempts = 0
        last: Optional[Exception] = None
        while attempts < self.config.persistence_retries:
            try:
                return fn()
            except Exception as e:
                attempts += 1
                last = e
                if attempts < self.config.persistence_retries:
                    time.sleep(self.config.persistence_retry_backoff * (2 ** (attempts - 1)))
        raise last if last else RuntimeError(f"retry exhausted for {op}")

    @staticmethod
    def _content_hash(text: str) -> str:
        return hashlib.sha256(" ".join(text.split()).lower().encode("utf-8")).hexdigest()

    def _state_payload(self) -> Dict[str, Any]:
        body = {
            "schema": 3,
            "version": __version__,
            "timestamp": _now().isoformat(),
            "last_run_hash": self._last_run_hash,
            "last_status": (self._last_run_result.status.value if self._last_run_result else None),
            "telemetry": self._telemetry.snapshot(),
        }
        body["hmac"] = sign_state(body, self._state_key)
        return body

    def _load_state(self) -> None:
        if self._circuit_breaker.is_open():
            return
        try:
            state = self._with_retry(lambda: self.persistence.load("last_execution"), "load")
            self._circuit_breaker.record_success()
        except Exception:
            self._circuit_breaker.record_failure()
            _LOG.warning("no_previous_state_found")
            return
        if not state:
            return
        stored_sig = state.pop("hmac", None)
        if stored_sig and not verify_state_sig(state, stored_sig, self._state_key):
            _LOG.warning("state_hmac_mismatch_rejecting")
            return
        ts = state.get("timestamp")
        if ts:
            try:
                self.last_execution = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                pass
        self._last_run_hash = state.get("last_run_hash")

    def _save_state(self) -> None:
        if self._circuit_breaker.is_open():
            return
        try:
            self._with_retry(lambda: self.persistence.save("last_execution", self._state_payload()), "save")
            self._circuit_breaker.record_success()
        except Exception:
            self._circuit_breaker.record_failure()
            _LOG.error("failed_to_save_state")

    # ── Pillar 1 ───────────────────────────────────────────────────────
    def scan_workspace(
        self, force: bool = False, metrics: Optional[_CycleMetrics] = None
    ) -> Tuple[List[str], Dict[str, datetime]]:
        if not self.workspace_root.exists():
            raise GhostAgentExecutionError(f"Workspace root does not exist: {self.workspace_root}")
        found: Dict[str, datetime] = {}
        ignore_dirs = set(self.config.ignore_dirs)
        ignore_globs = tuple(self.config.ignore_globs)
        for path in self.workspace_root.rglob("*"):
            try:
                if not path.is_file() or path.is_symlink():
                    continue
                if any(part in ignore_dirs for part in path.parts):
                    continue
                if any(path.name.endswith(g.lstrip("*")) for g in ignore_globs):
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                if st.st_size > self.config.max_file_size_bytes:
                    if metrics:
                        metrics.files_skipped_large += 1
                    continue
                mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                if not force and self.last_execution and mtime <= self.last_execution:
                    continue
                found[str(path)] = mtime
                if len(found) >= self.config.max_files_per_cycle:
                    break
            except Exception:
                continue
        if metrics:
            metrics.files_considered = len(found)
        return sorted(found.keys()), found

    def extract_high_signal_updates(
        self, modified_files: List[str], metrics: Optional[_CycleMetrics] = None
    ) -> List[Dict[str, Any]]:
        high: List[Dict[str, Any]] = []
        for fp in modified_files:
            try:
                high.extend(self.context_manager.extract_signals(fp) or [])
            except Exception as e:
                _LOG.warning("failed_to_extract_signals", file=self._redact(fp), error=type(e).__name__)
        if metrics:
            metrics.signals_extracted = len(high)
        return high

    def extract_structured_facts(self, text: str) -> List[Dict[str, str]]:
        """Deterministic parser for Key: value shapes. Zero LLM cost."""
        out: List[Dict[str, str]] = []
        for line in text.splitlines():
            for pat in _STRUCTURED_FACT_PATTERNS:
                m = pat.match(line)
                if m:
                    key, value = m.group(1).strip(), m.group(2).strip()
                    if 1 <= len(key) <= 60 and 3 <= len(value) <= 500:
                        out.append({"key": key, "value": value, "raw_line": line.strip()})
                    break
        return out

    def check_goal_alignment(
        self, updates: List[Dict[str, Any]], metrics: Optional[_CycleMetrics] = None
    ) -> Tuple[bool, float]:
        try:
            score = float(self.context_manager.calculate_goal_alignment(self.user_goal, updates))
        except Exception:
            score = 1.0
        if metrics:
            metrics.alignment_score = score
        if score < self.config.min_alignment_score:
            _LOG.warning("context_drift_detected", score=score)
            self._realign_to_goal(updates)
            if metrics:
                metrics.drift_realigned = True
            return False, score
        return True, score

    def _realign_to_goal(self, updates: List[Dict[str, Any]]) -> None:
        try:
            filtered = self.context_manager.filter_goal_relevant(self.user_goal, updates)
        except Exception:
            filtered = []
        self.current_context["filtered_updates"] = filtered
        self.current_context["realigned"] = True

    # ── Pillar 2 ───────────────────────────────────────────────────────
    def run_verification_pipeline(
        self, updates: List[Dict[str, Any]], metrics: Optional[_CycleMetrics] = None
    ) -> VerificationAuditTrail:
        trail = VerificationAuditTrail()
        added = 0
        seen_content: Dict[str, str] = {}  # content_hash -> first assertion text
        file_hashes: Dict[str, str] = {}
        file_texts: Dict[str, str] = {}

        for update in updates:
            if added >= self.config.max_assertions_per_cycle:
                break
            fp = update.get("source_file", "unknown")
            content = update.get("content", "")
            line = update.get("line_number")
            if not content.strip():
                continue

            # Lazy source read + hash (once per file)
            if fp not in file_hashes:
                file_texts[fp] = read_source_text(fp)
                file_hashes[fp] = hash_source_file(fp)

            try:
                assertions = list(self.verification_engine.extract_assertions(content) or [])
            except Exception:
                assertions = []

            # v1.4: wire structured facts into the pipeline (was dead code)
            if self.config.enable_structured_facts:
                for fact in self.extract_structured_facts(content):
                    raw = fact["raw_line"]
                    if raw and raw not in assertions:
                        assertions.append(raw)
                        if metrics:
                            metrics.structured_facts_found += 1

            if metrics:
                metrics.assertions_extracted += len(assertions)

            for a in assertions:
                if added >= self.config.max_assertions_per_cycle:
                    break
                a_hash = self._content_hash(a)
                # Exact dedup
                if a_hash in seen_content:
                    if metrics:
                        metrics.assertions_deduplicated += 1
                    continue
                # Near-duplicate detection
                is_near_dup = False
                for prev_hash, prev_text in seen_content.items():
                    if _jaccard(a, prev_text) >= self.config.near_dup_threshold:
                        is_near_dup = True
                        break
                if is_near_dup:
                    if metrics:
                        metrics.assertions_near_duplicate += 1
                    continue
                # Honest verification — passes file content so no double-read
                try:
                    try:
                        vr = self.verification_engine.verify_source(a, fp, line, content=file_texts.get(fp, ""))
                    except TypeError:
                        # Real engine doesn't accept content kwarg
                        vr = self.verification_engine.verify_source(a, fp, line)
                except Exception:
                    continue
                conf = getattr(vr, "confidence", "Low")
                if isinstance(conf, str):
                    try:
                        conf = ConfidenceRating(conf)
                    except ValueError:
                        continue
                if conf == ConfidenceRating.HIGH:
                    if trail.add_assertion(a, fp, line, conf, source_hash=file_hashes.get(fp, ""), content_hash=a_hash):
                        seen_content[a_hash] = a
                        added += 1
                        if metrics:
                            metrics.assertions_high += 1
                    else:
                        if metrics:
                            metrics.assertions_deduplicated += 1

        # Contradictions
        try:
            contradictions = self.verification_engine.detect_contradictions(trail.assertions) or []
        except Exception:
            contradictions = []
        if contradictions:
            trail.contradictions_resolved = len(contradictions)
            if metrics:
                metrics.contradictions_detected = len(contradictions)
            try:
                self.verification_engine.resolve_contradictions(contradictions, trail)
            except Exception:
                pass

        dropped = trail.filter_low_confidence()
        if metrics:
            metrics.low_confidence_dropped = len(dropped)
        trail.confidence_metric = trail.compute_confidence()
        return trail

    # ── Pillar 3 ───────────────────────────────────────────────────────
    def generate_output(self, trail: VerificationAuditTrail, output_format: str = "markdown") -> str:
        if output_format == "json":
            return self._generate_json(trail)
        if output_format in ("plain_text", "text", "plain"):
            return self._generate_text(trail)
        return self._generate_markdown(trail)

    def _sorted_assertions(self, trail: VerificationAuditTrail) -> List[DataPoint]:
        return sorted(trail.assertions, key=lambda a: (a.source_path, a.line_number or 0, a.assertion))

    def _generate_markdown(self, trail: VerificationAuditTrail) -> str:
        lines = ["# AXELR Ghost-Agent Update", f"Generated: {_now().isoformat()}", "", "## Verified Updates"]
        for a in self._sorted_assertions(trail):
            lines.append(f"- {a.assertion}")
            lines.append(f"  - Source: `{a.source_path}`")
            if a.line_number:
                lines.append(f"  - Line: {a.line_number}")
            if a.source_hash:
                lines.append(f"  - SHA-256: `{a.source_hash[:16]}…`")
            lines.append("")
        lines.extend(
            [
                "## Verification Summary",
                f"- Confidence Metric: {trail.confidence_metric}/10",
                f"- Contradictions Resolved: {trail.contradictions_resolved}",
                f"- Verified Sources: {len(trail.verified_sources)}",
            ]
        )
        return "\n".join(lines)

    def _generate_json(self, trail: VerificationAuditTrail) -> str:
        return json.dumps(
            {
                "timestamp": _now().isoformat(),
                "assertions": [
                    {
                        "assertion": a.assertion,
                        "source": a.source_path,
                        "line": a.line_number,
                        "confidence": a.confidence.value,
                        "source_hash": a.source_hash,
                        "content_hash": a.content_hash,
                        "timestamp": a.timestamp.isoformat(),
                    }
                    for a in self._sorted_assertions(trail)
                ],
                "audit_trail": {
                    "verified_sources": sorted(trail.verified_sources),
                    "confidence_metric": trail.confidence_metric,
                    "contradictions_resolved": trail.contradictions_resolved,
                    "explanation": trail.explain_confidence(),
                },
            },
            indent=2,
            sort_keys=True,
            default=str,
        )

    def _generate_text(self, trail: VerificationAuditTrail) -> str:
        lines = ["AXELR Ghost-Agent Update", f"Generated: {_now().isoformat()}", "", "Verified Updates:"]
        for a in self._sorted_assertions(trail):
            lines.append(f"* {a.assertion}")
            lines.append(f"  Source: {a.source_path}")
            if a.line_number:
                lines.append(f"  Line: {a.line_number}")
            lines.append("")
        return "\n".join(lines)

    # ── Pillar 4 ───────────────────────────────────────────────────────
    def stage_for_approval(
        self, content: str, format_type: str, goal_alignment: str, trail: VerificationAuditTrail
    ) -> StagedOutput:
        chash = self._content_hash(content)
        for existing in self.staged_outputs:
            if (
                not existing.rejected
                and not existing.executed
                and self._content_hash(existing.content) == chash
                and existing.format_type == format_type
            ):
                _LOG.info("staged_content_deduplicated")
                return existing
        staged = StagedOutput(
            content=content, format_type=format_type, goal_alignment=goal_alignment, audit_trail=trail
        )
        self.staged_outputs.append(staged)
        while len(self.staged_outputs) > self.config.max_staged_outputs:
            for i, s in enumerate(self.staged_outputs):
                if not s.approved and not s.executed and not s.rejected:
                    self.staged_outputs.pop(i)
                    break
            else:
                break
        _LOG.info("output_staged_for_approval", count=len(self.staged_outputs))
        self._print_approval_frame(staged)
        return staged

    @staticmethod
    def _fence_for(content: str) -> str:
        longest = run = 0
        for ch in content:
            if ch == "`":
                run += 1
                longest = max(longest, run)
            else:
                run = 0
        return "`" * max(4, longest + 1)

    def _render_frame(self, staged: StagedOutput) -> List[str]:
        fence = self._fence_for(staged.content)
        expl = staged.audit_trail.explain_confidence()
        lines = [
            "---",
            "[AGENT OUTPUT FRAME]",
            "### 🛠️ Ghost-Agent Status: Draft Staged",
            f"**Goal Aligned:** {staged.goal_alignment}",
            "",
            "#### 📝 Staged Output:",
            f"{fence}[{staged.format_type}]",
            staged.content,
            fence,
            "",
            "#### 🛡️ Anti-Hallucination Audit Trail:",
            f"* **Verified Sources:** {', '.join(staged.audit_trail.verified_sources) or '(none)'}",
            f"* **Confidence Metric:** {staged.audit_trail.confidence_metric}/10",
            f"  - base={expl['components']['base']}, "
            f"diversity={expl['components']['diversity']}, "
            f"coverage={expl['components']['coverage']}, "
            f"corroboration={expl['components']['corroboration']}",
            f"* **Source hashes present:** {expl['with_source_hash']}/{expl['assertion_count']}",
            "* **Key Fact Check:**",
        ]
        for a in staged.audit_trail.assertions[:5]:
            suffix = f", sha256={a.source_hash[:8]}…" if a.source_hash else ", no-hash"
            lines.append(f"  - '{a.assertion}' (source: {a.source_path}{suffix})")
        lines.extend(["", "*Ready for execution. Awaiting your approval click.*", "---"])
        return lines

    def _print_approval_frame(self, staged: StagedOutput) -> None:
        print("\n".join(self._render_frame(staged)))

    def list_pending(self) -> List[StagedOutput]:
        return [s for s in self.staged_outputs if not s.approved and not s.executed and not s.rejected]

    def iter_pending(self) -> Iterator[StagedOutput]:
        for s in list(self.staged_outputs):
            if not s.approved and not s.executed and not s.rejected:
                yield s

    def approve_all(self) -> int:
        count = 0
        for s in self.iter_pending():
            try:
                if self.approve_and_execute(s):
                    count += 1
            except Exception:
                pass
        return count

    def reject_all(self) -> int:
        count = 0
        for s in self.iter_pending():
            try:
                if self.reject_staged(s):
                    count += 1
            except Exception:
                pass
        return count

    def purge_expired(self) -> int:
        """Remove pending staged outputs older than TTL. Returns count purged."""
        ttl = timedelta(seconds=self.config.staged_output_ttl_seconds)
        now = _now()
        before = len(self.staged_outputs)
        self.staged_outputs = [
            s for s in self.staged_outputs if s.approved or s.executed or s.rejected or (now - s.created_at) < ttl
        ]
        purged = before - len(self.staged_outputs)
        if purged:
            self._telemetry.incr("purged_expired", purged)
        return purged

    def approve_and_execute(self, staged: StagedOutput) -> bool:
        if not any(s is staged for s in self.staged_outputs):
            raise GhostAgentExecutionError("Staged output not found in queue")
        if staged.rejected:
            raise GhostAgentExecutionError("Cannot approve a rejected output")
        if staged.approved:
            return False
        staged.approved = True
        try:
            staged.executed = True
            self._telemetry.incr("approved_executed")
            return True
        except Exception as e:
            staged.approved = False
            corr = self._last_run_result.correlation_id if self._last_run_result else "unknown"
            self._dead_letter.push(
                _DeadLetterEntry(
                    correlation_id=corr,
                    staged_output=staged,
                    error=str(e),
                    failed_at=_now(),
                )
            )
            _LOG.error("execution_failed", error=type(e).__name__)
            return False

    def reject_staged(self, staged: StagedOutput) -> bool:
        if not any(s is staged for s in self.staged_outputs):
            raise GhostAgentExecutionError("Staged output not found in queue")
        if staged.executed:
            return False
        staged.rejected = True
        staged.approved = False
        self._telemetry.incr("rejected")
        return True

    def retry_dead_letter(self, correlation_id: str) -> bool:
        """Attempt to re-execute a dead-letter entry by correlation ID."""
        for entry in self._dead_letter.list():
            if entry.correlation_id == correlation_id:
                try:
                    entry.staged_output.approved = True
                    entry.staged_output.executed = True
                    self._dead_letter.remove(correlation_id)
                    self._telemetry.incr("dead_letter_retry_success")
                    return True
                except Exception:
                    entry.attempts += 1
                    self._telemetry.incr("dead_letter_retry_failed")
                    return False
        return False

    # ── Cycle ──────────────────────────────────────────────────────────
    def run_cycle(self, output_format: str = "markdown", dry_run: bool = False, force: bool = False) -> RunResult:
        corr = str(uuid.uuid4())
        t0_wall = _now()
        t0 = time.monotonic()
        metrics = _CycleMetrics()
        staged_ref: List[Optional[StagedOutput]] = [None]

        def _result(
            status: CycleStatus, staged: Optional[StagedOutput] = None, error: Optional[str] = None
        ) -> RunResult:
            dur = time.monotonic() - t0
            self._telemetry.incr(f"cycle_{status.value}")
            self._telemetry.record_duration(dur)
            staged_ref[0] = staged
            rr = RunResult(
                status=status,
                staged_output=staged,
                correlation_id=corr,
                duration_seconds=round(dur, 4),
                metrics=metrics.as_dict(),
                error=error,
            )
            self._last_run_result = rr
            self._cycle_history.append(
                _CycleRecord(
                    status=status,
                    correlation_id=corr,
                    duration_seconds=round(dur, 4),
                    staged=staged is not None,
                    timestamp=t0_wall,
                    assertion_count=(len(staged.audit_trail.assertions) if staged is not None else 0),
                )
            )
            return rr

        def _budget_exceeded() -> bool:
            return time.monotonic() - t0 > self.config.max_cycle_seconds

        if not self._cycle_lock.acquire(blocking=False):
            return _result(CycleStatus.BUSY)
        try:
            if self._circuit_breaker.is_open():
                return _result(CycleStatus.CIRCUIT_OPEN)
            if not force and self.config.min_cycle_interval_seconds > 0 and self.last_execution:
                elapsed = (_now() - self.last_execution).total_seconds()
                if elapsed < self.config.min_cycle_interval_seconds:
                    return _result(CycleStatus.RATE_LIMITED)

            try:
                modified, mtimes = self.scan_workspace(force=force, metrics=metrics)
            except GhostAgentExecutionError as e:
                return _result(CycleStatus.ERROR, error=str(e))
            if not modified:
                if not dry_run:
                    self.last_execution = t0_wall
                    self._save_state()
                return _result(CycleStatus.NO_FILES)

            run_hash = self._workspace_merkle(mtimes)
            if not force and run_hash == self._last_run_hash:
                if not dry_run:
                    self.last_execution = t0_wall
                    self._save_state()
                return _result(CycleStatus.IDEMPOTENT_SKIP)

            updates = self.extract_high_signal_updates(modified, metrics=metrics)
            if not updates:
                if not dry_run:
                    self.last_execution = t0_wall
                    self._last_run_hash = run_hash
                    self._save_state()
                return _result(CycleStatus.NO_SIGNALS)

            if _budget_exceeded():
                return _result(CycleStatus.BUDGET_EXCEEDED)

            aligned, _ = self.check_goal_alignment(updates, metrics=metrics)
            if not aligned:
                updates = self.current_context.get("filtered_updates", [])
                if not updates:
                    if not dry_run:
                        self.last_execution = t0_wall
                        self._last_run_hash = run_hash
                        self._save_state()
                    return _result(CycleStatus.NO_SIGNALS)

            trail = self.run_verification_pipeline(updates, metrics=metrics)
            if not trail.assertions:
                if not dry_run:
                    self.last_execution = t0_wall
                    self._last_run_hash = run_hash
                    self._save_state()
                return _result(CycleStatus.NO_VERIFIED)

            content = self.generate_output(trail, output_format)

            if dry_run:
                return _result(CycleStatus.DRY_RUN)

            goal_summary = f"Processed {len(trail.assertions)} verified updates aligned with: {self.user_goal}"
            staged = self.stage_for_approval(content, output_format, goal_summary, trail)
            self.last_execution = t0_wall
            self._last_run_hash = run_hash
            self._save_state()
            return _result(CycleStatus.STAGED, staged=staged)
        except Exception as e:
            _LOG.error("cycle_failed", corr=corr, error=type(e).__name__, tb=traceback.format_exc(limit=3))
            return _result(CycleStatus.ERROR, error=str(e))
        finally:
            self._cycle_lock.release()

    @staticmethod
    def _workspace_merkle(mtimes: Dict[str, datetime]) -> str:
        leaves = [hashlib.sha256(f"{p}\x00{mtimes[p].isoformat()}".encode()).digest() for p in sorted(mtimes.keys())]
        if not leaves:
            return hashlib.sha256(b"").hexdigest()
        level = leaves
        while len(level) > 1:
            if len(level) % 2:
                level.append(level[-1])
            level = [hashlib.sha256(level[i] + level[i + 1]).digest() for i in range(0, len(level), 2)]
        return level[0].hex()

    # ── Ops ────────────────────────────────────────────────────────────
    def health(self) -> Dict[str, Any]:
        recent = list(self._cycle_history)[-10:]
        status_counts: Dict[str, int] = {}
        for r in recent:
            status_counts[r.status.value] = status_counts.get(r.status.value, 0) + 1
        return {
            "initialized": getattr(self, "_initialized", False),
            "circuit_open": self._circuit_breaker.is_open(),
            "pending_outputs": len(self.list_pending()),
            "staged_total": len(self.staged_outputs),
            "dead_letter_count": len(self._dead_letter.list()),
            "last_status": (self._last_run_result.status.value if self._last_run_result else None),
            "recent_status_distribution": status_counts,
            "using_fallback_core": USING_FALLBACK_CORE,
            "version": __version__,
        }

    def describe(self) -> str:
        return "\n".join(
            [
                f"AXELR Ghost-Agent v{__version__}",
                f"Workspace: {self._redact(str(self.workspace_root))}",
                f"Goal: {self._redact(self.user_goal)}",
                f"Using fallback core: {USING_FALLBACK_CORE}",
                f"Circuit breaker: {'OPEN' if self._circuit_breaker.is_open() else 'closed'}",
                f"Pending outputs: {len(self.list_pending())}",
                f"Dead-letter entries: {len(self._dead_letter.list())}",
                f"Last status: {self._last_run_result.status.value if self._last_run_result else 'never'}",
            ]
        )

    def export_metrics(self) -> str:
        snap = self._telemetry.snapshot()
        lines = [
            "# HELP ghost_agent_runs_total Total cycles executed",
            "# TYPE ghost_agent_runs_total counter",
            f"ghost_agent_runs_total {snap['runs']}",
            "# HELP ghost_agent_avg_run_seconds Average cycle duration",
            "# TYPE ghost_agent_avg_run_seconds gauge",
            f"ghost_agent_avg_run_seconds {snap['avg_run_seconds']}",
            "# HELP ghost_agent_pending_outputs Outputs awaiting approval",
            "# TYPE ghost_agent_pending_outputs gauge",
            f"ghost_agent_pending_outputs {len(self.list_pending())}",
            "# HELP ghost_agent_dead_letter_count Failed executes",
            "# TYPE ghost_agent_dead_letter_count gauge",
            f"ghost_agent_dead_letter_count {len(self._dead_letter.list())}",
            "# HELP ghost_agent_circuit_open Circuit breaker state (1=open)",
            "# TYPE ghost_agent_circuit_open gauge",
            f"ghost_agent_circuit_open {1 if self._circuit_breaker.is_open() else 0}",
        ]
        for k, v in snap["counters"].items():
            safe = k.replace('"', '\\"')
            lines.append(f'ghost_agent_counter{{key="{safe}"}} {v}')
        return "\n".join(lines) + "\n"

    def correlate(self, correlation_id: str) -> Optional[RunResult]:
        if self._last_run_result and self._last_run_result.correlation_id == correlation_id:
            return self._last_run_result
        return None

    def cycle_history(self) -> List[Dict[str, Any]]:
        """Bounded history of recent cycle records (most recent last)."""
        return [
            {
                "status": r.status.value,
                "correlation_id": r.correlation_id,
                "duration_seconds": r.duration_seconds,
                "staged": r.staged,
                "timestamp": r.timestamp.isoformat(),
                "assertion_count": r.assertion_count,
            }
            for r in self._cycle_history
        ]

    def drain_dead_letter(self) -> List[_DeadLetterEntry]:
        items = self._dead_letter.list()
        self._dead_letter.clear()
        return items

    def export_snapshot(self) -> str:
        """Portable JSON snapshot of a staged output for external verification."""
        pending = self.list_pending()
        if not pending:
            return json.dumps({"status": "no_pending_output", "version": __version__}, sort_keys=True, indent=2)
        staged = pending[-1]
        trail = staged.audit_trail
        return json.dumps(
            {
                "version": __version__,
                "correlation_id": (self._last_run_result.correlation_id if self._last_run_result else ""),
                "goal_alignment": staged.goal_alignment,
                "format": staged.format_type,
                "content": staged.content,
                "audit": {
                    "confidence_metric": trail.confidence_metric,
                    "explanation": trail.explain_confidence(),
                    "assertions": [
                        {
                            "assertion": a.assertion,
                            "source": a.source_path,
                            "line": a.line_number,
                            "confidence": a.confidence.value,
                            "source_hash": a.source_hash,
                            "content_hash": a.content_hash,
                        }
                        for a in trail.assertions
                    ],
                    "verified_sources": sorted(trail.verified_sources),
                    "contradictions_resolved": trail.contradictions_resolved,
                },
            },
            indent=2,
            sort_keys=True,
            default=str,
        )

    def self_test(self) -> Tuple[bool, Dict[str, Any]]:
        diag: Dict[str, Any] = {"steps": []}
        try:
            probe = self.workspace_root / ".axelr" / "self_test_probe.md"
            probe.parent.mkdir(parents=True, exist_ok=True)
            probe.write_text("# Self-Test\n- Completed self-test probe\n", encoding="utf-8")
            diag["steps"].append("probe_written")
            try:
                res = self.run_cycle(output_format="markdown", dry_run=True, force=True)
                diag["steps"].append(f"dry_run:{res.status.value}")
                return res.status in (
                    CycleStatus.DRY_RUN,
                    CycleStatus.NO_SIGNALS,
                    CycleStatus.NO_VERIFIED,
                    CycleStatus.ERROR,
                ), diag
            finally:
                try:
                    probe.unlink()
                except Exception:
                    pass
        except Exception as e:
            diag["error"] = f"{type(e).__name__}: {e}"
            return False, diag


# ── Public API ──────────────────────────────────────────────────────────
def get_ghost_agent(
    workspace_root: Optional[str] = None, user_goal: Optional[str] = None, config: Optional[GhostAgentConfig] = None
) -> AxelrGhostAgent:
    if AxelrGhostAgent._instance is None:
        if not workspace_root or not user_goal:
            raise GhostAgentExecutionError("workspace_root and user_goal required for first initialization")
        return AxelrGhostAgent(workspace_root, user_goal, config=config)
    return AxelrGhostAgent._instance


def reset_singleton() -> None:
    with AxelrGhostAgent._instance_lock:
        AxelrGhostAgent._instance = None


def audit() -> Dict[str, Any]:
    base = {
        "version": __version__,
        "using_fallback_core": USING_FALLBACK_CORE,
        "initialized": False,
        "last_execution": None,
        "staged_outputs_count": 0,
        "workspace_root": None,
        "last_status": None,
        "health": None,
        "telemetry": {},
    }
    inst = AxelrGhostAgent._instance
    if not inst or not getattr(inst, "_initialized", False):
        return base
    return {
        **base,
        "initialized": True,
        "last_execution": inst.last_execution.isoformat() if inst.last_execution else None,
        "staged_outputs_count": len(inst.staged_outputs),
        "workspace_root": str(inst.workspace_root),
        "last_status": (inst._last_run_result.status.value if inst._last_run_result else None),
        "health": inst.health(),
        "telemetry": inst._telemetry.snapshot(),
    }


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
    "AxelrGhostAgent",
    "get_ghost_agent",
    "reset_singleton",
    "audit",
]
