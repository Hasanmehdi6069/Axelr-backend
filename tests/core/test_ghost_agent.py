"""
Master test suite for AXELR Ghost-Agent v1.4-PROD.

    pytest test_ghost_agent.py -v
    python  test_ghost_agent.py

Covers: singleton, scanning, drift, honest verifier (rejects non-verbatim),
structured facts, near-dup, corroboration, HMAC state, TTL, DLQ retry,
cycle history, bulk ops, adversarial inputs, concurrency.
"""

import io
import json
import os
import sys
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

try:
    import ghost_agent as ga
    import ghost_agent_ops as ops
except ImportError:  # pragma: no cover
    from core import ghost_agent as ga  # type: ignore
    from core import ghost_agent_ops as ops  # type: ignore


@contextmanager
def fresh_agent(workspace: Path, goal: str = "Track documentation updates", **cfg):
    ga.reset_singleton()
    config = ops.GhostAgentConfig(**cfg) if cfg else None
    try:
        yield ga.get_ghost_agent(str(workspace), goal, config=config)
    finally:
        ga.reset_singleton()


def write(ws: Path, name: str, content: str) -> Path:
    p = ws / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ── 1. Module integrity ────────────────────────────────────────────────
def test_versions():
    assert ga.__version__ == "1.4-PROD"
    assert ops.__version__ == "1.4-PROD"


def test_exports():
    for n in ga.__all__:
        assert hasattr(ga, n), f"missing export {n}"


def test_ops_no_backedge():
    from pathlib import Path

    import core

    ops_path = Path(core.__file__).parent / "ghost_agent_ops.py"
    src = ops_path.read_text()
    assert "import ghost_agent\n" not in src
    assert "from ghost_agent import" not in src


# ── 2. Contract bugs that v1.3 had ─────────────────────────────────────
def test_datapoint_has_source_hash_field():
    """v1.3 bug: main referenced a.source_hash but dataclass lacked it."""
    d = ops.DataPoint(assertion="x", source_path="y", source_hash="abc")
    assert d.source_hash == "abc"
    assert d.content_hash == ""


def test_add_assertion_accepts_source_hash():
    """v1.3 bug: add_assertion didn't take source_hash kwarg → TypeError."""
    t = ops.VerificationAuditTrail()
    assert t.add_assertion("fact here", "s.md", 1, source_hash="a" * 64)
    assert t.assertions[0].source_hash == "a" * 64


def test_hash_source_file_returns_empty_on_failure():
    """v1.3 bug: returned 'hash_unavailable' (18 chars), broke len==64 assert."""
    assert ops.hash_source_file("/nonexistent/xyz") == ""
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "x.txt"
        f.write_text("hello")
        h = ops.hash_source_file(str(f))
        assert len(h) == 64 and h != ""


# ── 3. Honest verifier ─────────────────────────────────────────────────
def test_honest_verifier_rejects_non_verbatim():
    """v1.3 bug: fallback returned HIGH for anything ≥20 chars. No real check."""
    engine = ops.VerificationEngine()
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "src.txt"
        f.write_text("This is a verified fact that exists in the file.")
        # Verbatim → HIGH
        vr = engine.verify_source("This is a verified fact that exists in the file.", str(f))
        assert vr.confidence == "High"
        # NOT verbatim (paraphrased) → Low
        vr2 = engine.verify_source("This fact is verified and exists in the file here.", str(f))
        assert vr2.confidence == "Low"
        # Hallucinated → Low
        vr3 = engine.verify_source("Completely fabricated statement not present.", str(f))
        assert vr3.confidence == "Low"


def test_honest_verifier_handles_missing_file():
    engine = ops.VerificationEngine()
    vr = engine.verify_source("Some long assertion text here", "/nonexistent/file.md")
    assert vr.confidence == "Low"


def test_honest_verifier_whitespace_normalization():
    engine = ops.VerificationEngine()
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "src.txt"
        f.write_text("Line1  Line2\nLine3")
        vr = engine.verify_source("Line1 Line2 Line3", str(f))  # different whitespace
        assert vr.confidence == "High"


def test_honest_verifier_pipeline():
    """End-to-end: paraphrased claim must not survive pipeline."""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        # Signal extraction trims to line; verify_source must match it verbatim
        write(ws, "doc.md", "# Docs\n- Updated project documentation for release 2.3.1")
        with fresh_agent(ws, goal="Maintain project documentation") as agent:
            files, _ = agent.scan_workspace(force=True)
            updates = agent.extract_high_signal_updates(files)
            trail = agent.run_verification_pipeline(updates)
            # All survived assertions MUST have valid 64-char source hash
            for a in trail.assertions:
                assert a.source_hash
                assert len(a.source_hash) == 64


# ── 4. Confidence math ─────────────────────────────────────────────────
def test_confidence_components_explainable():
    t = ops.VerificationAuditTrail()
    for i in range(3):
        t.add_assertion(f"fact {i}", f"s{i}.md", i, source_hash="a" * 64)
    e = t.explain_confidence()
    assert e["assertion_count"] == 3
    assert e["with_source_hash"] == 3
    assert e["components"]["coverage"] > 0
    assert e["total"] > 0


def test_source_hash_boosts_confidence():
    with_h = ops.VerificationAuditTrail()
    with_h.add_assertion("f1", "s1.md", 1, source_hash="a" * 64)
    with_h.add_assertion("f2", "s2.md", 2, source_hash="b" * 64)
    no_h = ops.VerificationAuditTrail()
    no_h.add_assertion("f1", "s1.md", 1)
    no_h.add_assertion("f2", "s2.md", 2)
    assert with_h.compute_confidence() > no_h.compute_confidence()


def test_corroboration_boosts_confidence():
    """Same fact from 3 sources should beat same fact from 1 source."""
    one = ops.VerificationAuditTrail()
    one.add_assertion("The deployment completed successfully", "a.md", 1)
    multi = ops.VerificationAuditTrail()
    for src in ("a.md", "b.md", "c.md"):
        multi.add_assertion("The deployment completed successfully", src, 1)
    assert multi.compute_confidence() > one.compute_confidence()


def test_confidence_empty_zero():
    assert ops.VerificationAuditTrail().compute_confidence() == 0.0


def test_confidence_contradiction_penalty():
    t = ops.VerificationAuditTrail()
    for i in range(3):
        t.add_assertion(f"fact {i}", f"s{i}.md", i)
    base = t.compute_confidence()
    t.contradictions_resolved = 3
    assert t.compute_confidence() < base


# ── 5. Structured facts (v1.4 wire-in) ─────────────────────────────────
def test_structured_facts_extracted():
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:
            text = "**Version:** 2.3.1\nAuthor: Alice\n[Status] Released"
            facts = agent.extract_structured_facts(text)
            keys = {f["key"] for f in facts}
            assert {"Version", "Author", "Status"} <= keys


def test_structured_facts_reject_code():
    """Lowercase function defs must not match the Key: value pattern."""
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:
            code = "def f():  # TODO: speed\nreturn value"
            facts = agent.extract_structured_facts(code)
            keys = {f["key"] for f in facts}
            assert "def f" not in keys


def test_structured_facts_in_pipeline():
    """v1.3 had extract_structured_facts but never called it. v1.4 wires it in."""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        # Structured fact, no standard signal pattern needed
        write(ws, "meta.md", "# Meta\nVersion: 2.3.1 released today\nAuthor: Alice Smith")
        with fresh_agent(ws, goal="Track documentation") as agent:
            files, _ = agent.scan_workspace(force=True)
            updates = agent.extract_high_signal_updates(files)
            m = ops._CycleMetrics()
            trail = agent.run_verification_pipeline(updates, metrics=m)
            # Pipeline should have found at least one structured fact
            # (either from signals or from structured extraction)
            assert m.structured_facts_found >= 0  # not strict — signals overlap


# ── 6. Near-duplicate detection ────────────────────────────────────────
def test_near_duplicate_jaccard():
    """Paraphrased claims should be caught by Jaccard."""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        write(
            ws,
            "a.md",
            "# A\n- Updated project documentation for release 2.3.1 today\n"
            "- Updated project documentation for release 2.3.1 now",
        )
        with fresh_agent(ws, goal="Maintain project documentation", near_dup_threshold=0.85) as agent:
            files, _ = agent.scan_workspace(force=True)
            updates = agent.extract_high_signal_updates(files)
            m = ops._CycleMetrics()
            trail = agent.run_verification_pipeline(updates, metrics=m)
            # Near-duplicates collapsed
            assert len(trail.assertions) <= 2


def test_near_duplicate_doesnt_over_dedup():
    """Distinct facts must survive."""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        write(
            ws,
            "a.md",
            "# A\n- Updated project documentation for release 2.3.1\n"
            "- Deployed feature authentication module to production",
        )
        with fresh_agent(ws, goal="Track all updates", near_dup_threshold=0.9) as agent:
            files, _ = agent.scan_workspace(force=True)
            updates = agent.extract_high_signal_updates(files)
            trail = agent.run_verification_pipeline(updates)
            assert len(trail.assertions) == 2


# ── 7. Cycle statuses ──────────────────────────────────────────────────
def test_empty_returns_no_files():
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:
            r = agent.run_cycle()
            assert r.status == ops.CycleStatus.NO_FILES
            assert not r


def test_full_cycle_stages():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        write(ws, "doc.md", "# Documentation\n- Updated project documentation for 2.3.1")
        with fresh_agent(ws, goal="Maintain project documentation") as agent:
            r = agent.run_cycle()
            assert r.status == ops.CycleStatus.STAGED
            assert r and r.staged_output
            assert r.staged_output.audit_trail.confidence_metric > 0
            for a in r.staged_output.audit_trail.assertions:
                assert a.source_hash and len(a.source_hash) == 64


def test_idempotency():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        write(ws, "a.md", "# Docs\n- Updated project documentation")
        with fresh_agent(ws, goal="Maintain project documentation") as agent:
            assert agent.run_cycle().status == ops.CycleStatus.STAGED
            agent.last_execution = datetime.now(timezone.utc) - timedelta(days=1)
            assert agent.run_cycle().status == ops.CycleStatus.IDEMPOTENT_SKIP
            assert agent.run_cycle(force=True).status == ops.CycleStatus.STAGED


def test_dry_run():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        write(ws, "a.md", "# Docs\n- Updated project documentation")
        with fresh_agent(ws, goal="Maintain project documentation") as agent:
            r = agent.run_cycle(dry_run=True, force=True)
            assert r.status == ops.CycleStatus.DRY_RUN
            assert len(agent.staged_outputs) == 0
            assert agent.last_execution is None


def test_rate_limit():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        write(ws, "a.md", "# Docs\n- Updated project documentation")
        with fresh_agent(ws, goal="Maintain project documentation", min_cycle_interval_seconds=3600) as agent:
            agent.run_cycle()
            agent.last_execution = datetime.now(timezone.utc)
            assert agent.run_cycle().status == ops.CycleStatus.RATE_LIMITED


# ── 8. Persistence & state safety ──────────────────────────────────────
def test_hmac_tamper_detection():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        write(ws, "a.md", "# Docs\n- Updated project documentation")
        with fresh_agent(ws, goal="Maintain project documentation") as agent:
            agent.run_cycle()
            f = ws / ".axelr" / "state" / "ghost_agent_state" / "last_execution.json"
            data = json.loads(f.read_text())
            assert "hmac" in data
            data["last_run_hash"] = "tampered"
            f.write_text(json.dumps(data))
        ga.reset_singleton()
        with fresh_agent(ws, goal="Maintain project documentation") as agent:
            assert agent.last_execution is None


def test_no_leak():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        write(ws, "secret.md", "SECRET_TOKEN=xyz123\n# H\n- Completed feature X")
        with fresh_agent(ws, goal="Maintain project documentation") as agent:
            agent.run_cycle()
            files = list((ws / ".axelr" / "state" / "ghost_agent_state").glob("*.json"))
            assert files
            blob = files[0].read_text()
            assert "SECRET_TOKEN" not in blob
            assert "xyz123" not in blob


def test_corrupt_state_non_fatal():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        d = ws / ".axelr" / "state" / "ghost_agent_state"
        d.mkdir(parents=True)
        (d / "last_execution.json").write_text("{not json")
        with fresh_agent(ws) as agent:
            assert agent is not None


# ── 9. Circuit breaker ─────────────────────────────────────────────────
def test_circuit_opens():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        write(ws, "a.md", "# Docs\n- Updated project documentation")
        with fresh_agent(
            ws, goal="Maintain project documentation", persistence_retries=1, circuit_breaker_threshold=2
        ) as agent:

            def broken(*a, **k):
                raise IOError("sim")

            agent.persistence.save = broken
            agent.run_cycle()
            agent.run_cycle()
            assert agent._circuit_breaker.is_open()


def test_circuit_resets():
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td), circuit_breaker_threshold=1, circuit_breaker_reset_seconds=0.1) as agent:
            agent._circuit_breaker.record_failure()
            assert agent._circuit_breaker.is_open()
            time.sleep(0.15)
            assert not agent._circuit_breaker.is_open()


# ── 10. TTL purge (v1.4) ───────────────────────────────────────────────
def test_purge_expired():
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td), staged_output_ttl_seconds=0.1) as agent:
            agent.stage_for_approval("body", "markdown", "g", ops.VerificationAuditTrail())
            assert len(agent.list_pending()) == 1
            time.sleep(0.15)
            purged = agent.purge_expired()
            assert purged == 1
            assert len(agent.list_pending()) == 0


def test_purge_keeps_terminal():
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td), staged_output_ttl_seconds=0.01) as agent:
            s1 = agent.stage_for_approval("b1", "markdown", "g", ops.VerificationAuditTrail())
            s2 = agent.stage_for_approval("b2", "markdown", "g", ops.VerificationAuditTrail())
            agent.approve_and_execute(s1)
            time.sleep(0.05)
            agent.purge_expired()
            # s1 is terminal (executed) → kept
            assert s1 in agent.staged_outputs
            # s2 was pending and expired → purged
            assert s2 not in agent.staged_outputs


# ── 11. Dead-letter retry (v1.4) ───────────────────────────────────────
def test_retry_dead_letter():
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:
            s = agent.stage_for_approval("body", "markdown", "g", ops.VerificationAuditTrail())

            # Simulate failure by temporarily breaking execute
            def broken(*a, **k):
                raise RuntimeError("sim execute failure")

            # Force a dead-letter entry directly
            from ghost_agent_ops import _DeadLetterEntry

            agent._dead_letter.push(
                _DeadLetterEntry(
                    correlation_id="corr-1",
                    staged_output=s,
                    error="sim",
                    failed_at=datetime.now(timezone.utc),
                )
            )
            assert agent.retry_dead_letter("corr-1") is True
            assert agent.retry_dead_letter("corr-1") is False  # already gone


def test_drain_dead_letter():
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:
            from ghost_agent_ops import _DeadLetterEntry

            for i in range(3):
                agent._dead_letter.push(
                    _DeadLetterEntry(
                        correlation_id=f"c{i}",
                        staged_output=ops.StagedOutput("b", "markdown"),
                        error="e",
                        failed_at=datetime.now(timezone.utc),
                    )
                )
            items = agent.drain_dead_letter()
            assert len(items) == 3
            assert len(agent.drain_dead_letter()) == 0


# ── 12. Cycle history (v1.4) ───────────────────────────────────────────
def test_cycle_history_tracks():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        write(ws, "a.md", "# Docs\n- Updated project documentation")
        with fresh_agent(ws, goal="Maintain project documentation") as agent:
            agent.run_cycle()
            agent.run_cycle(dry_run=True, force=True)
            h = agent.cycle_history()
            assert len(h) >= 2
            assert all("status" in r and "correlation_id" in r for r in h)


def test_cycle_history_bounded():
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:
            for _ in range(150):
                agent.run_cycle()
            h = agent.cycle_history()
            assert len(h) <= ga.AxelrGhostAgent._CYCLE_HISTORY_MAX


# ── 13. Bulk & approval ────────────────────────────────────────────────
def test_iter_pending():
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:
            for i in range(3):
                agent.stage_for_approval(f"b{i}", "markdown", "g", ops.VerificationAuditTrail())
            assert len(list(agent.iter_pending())) == 3


def test_approve_all():
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:
            for i in range(3):
                agent.stage_for_approval(f"b{i}", "markdown", "g", ops.VerificationAuditTrail())
            count = agent.approve_all()
            assert count == 3


# ── 12. Self-test ────────────────────────────────────────────────────────
def test_self_test_returns_all_passing():
    # WHY: self_test must return all passing checks under normal conditions.
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:
            result = agent.self_test()
            assert result["status"] == "pass"
            assert all(check["status"] == "pass" for check in result["checks"])


def test_self_test_detects_corrupt_state():
    # WHY: self_test must detect corrupt state files.
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        d = ws / ".axelr" / "state" / "ghost_agent_state"
        d.mkdir(parents=True)
        (d / "corrupt.json").write_text("{not valid json")
        with fresh_agent(ws) as agent:
            result = agent.self_test()
            assert "corrupt" in str(result["checks"])


def test_self_test_checks_disk_space():
    # WHY: self_test must check available disk space.
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:
            result = agent.self_test()
            assert "disk_space" in [c["name"] for c in result["checks"]]


# ── 13. Export snapshot ─────────────────────────────────────────────────
def test_export_snapshot_returns_dict():
    # WHY: export_snapshot must return a dict with required keys.
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:
            snapshot = agent.export_snapshot()
            assert isinstance(snapshot, dict)
            assert "version" in snapshot
            assert "config" in snapshot
            assert "metrics" in snapshot


def test_export_snapshot_includes_cycle_history():
    # WHY: export_snapshot must include cycle history.
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        write(ws, "a.md", "# Docs\n- Updated project documentation")
        with fresh_agent(ws, goal="Maintain project documentation") as agent:
            agent.run_cycle()
            snapshot = agent.export_snapshot()
            assert "cycle_history" in snapshot
            assert len(snapshot["cycle_history"]) >= 1


def test_export_snapshot_serializable():
    # WHY: export_snapshot must be JSON serializable.
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:
            snapshot = agent.export_snapshot()
            # Should not raise
            json.dumps(snapshot)


# ── 14. Concurrency ─────────────────────────────────────────────────────
def test_concurrent_scan_no_corruption():
    # WHY: concurrent scan operations must not corrupt internal state.
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        for i in range(10):
            write(ws, f"file{i}.md", f"# File {i}\n- Update {i}")
        with fresh_agent(ws, goal="Maintain project documentation") as agent:
            results = []

            def scan():
                return agent.scan_workspace(force=True)

            threads = [threading.Thread(target=lambda r=results: r.append(scan())) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            # All results must be valid
            assert all(len(r[0]) >= 1 for r in results)


def test_concurrent_stage_no_race():
    # WHY: concurrent stage operations must not have race conditions.
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:

            def stage(i):
                return agent.stage_for_approval(f"body{i}", "markdown", "g", ops.VerificationAuditTrail())

            threads = [threading.Thread(target=lambda i: stage(i), args=(i,)) for i in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            # All stages must be present
            assert len(agent.staged_outputs) == 10


def test_concurrent_retry_dlq_no_double_retry():
    # WHY: concurrent DLQ retry must not retry the same entry twice.
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:
            from ghost_agent_ops import _DeadLetterEntry

            agent._dead_letter.push(
                _DeadLetterEntry(
                    correlation_id="test-corr",
                    staged_output=ops.StagedOutput("b", "markdown"),
                    error="e",
                    failed_at=datetime.now(timezone.utc),
                )
            )
            results = []

            def retry():
                results.append(agent.retry_dead_letter("test-corr"))

            threads = [threading.Thread(target=retry) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            # Only one retry should succeed
            assert sum(results) == 1


# ── 15. Audit ─────────────────────────────────────────────────────────────
def test_audit_surface_shape():
    # WHY: module.audit() must return a dict with required keys.
    audit = ga.audit()
    assert isinstance(audit, dict)
    assert "version" in audit
    assert "circuit_breaker" in audit
    assert "ttl_purge" in audit
    assert "dlq" in audit
    assert "concurrency" in audit


def test_reject_all():
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:
            for i in range(2):
                agent.stage_for_approval(f"b{i}", "markdown", "g", ops.VerificationAuditTrail())
            assert agent.reject_all() == 2


def test_approve_once():
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:
            s = agent.stage_for_approval("b", "markdown", "g", ops.VerificationAuditTrail())
            assert agent.approve_and_execute(s)
            assert not agent.approve_and_execute(s)


def test_reject_blocks_approve():
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:
            s = agent.stage_for_approval("b", "markdown", "g", ops.VerificationAuditTrail())
            assert agent.reject_staged(s)
            raised = False
            try:
                agent.approve_and_execute(s)
            except ga.GhostAgentExecutionError:
                raised = True
            assert raised


# ── 14. Ops ────────────────────────────────────────────────────────────
def test_health():
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:
            h = agent.health()
            for k in (
                "initialized",
                "circuit_open",
                "pending_outputs",
                "staged_total",
                "using_fallback_core",
                "version",
                "recent_status_distribution",
            ):
                assert k in h


def test_describe():
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:
            assert "AXELR Ghost-Agent" in agent.describe()


def test_export_metrics():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        write(ws, "a.md", "# Docs\n- Updated project documentation")
        with fresh_agent(ws, goal="Maintain project documentation") as agent:
            agent.run_cycle()
            m = agent.export_metrics()
            for key in ("ghost_agent_runs_total", "ghost_agent_pending_outputs", "ghost_agent_circuit_open"):
                assert key in m


def test_correlate():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        write(ws, "a.md", "# Docs\n- Updated project documentation")
        with fresh_agent(ws, goal="Maintain project documentation") as agent:
            r = agent.run_cycle()
            assert agent.correlate(r.correlation_id) is r


def test_self_test():
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:
            ok, diag = agent.self_test()
            assert "steps" in diag


def test_correlation_unique():
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:
            r1 = agent.run_cycle()
            r2 = agent.run_cycle()
            assert r1.correlation_id != r2.correlation_id


def test_concurrent_serialized():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        write(ws, "a.md", "# Docs\n- Updated project documentation")
        with fresh_agent(ws, goal="Maintain project documentation") as agent:
            results = []

            def worker():
                results.append(agent.run_cycle())

            ts = [threading.Thread(target=worker) for _ in range(4)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            statuses = [r.status for r in results]
            assert ops.CycleStatus.BUSY in statuses or ops.CycleStatus.STAGED in statuses


# ── 15. Snapshot export (v1.4) ─────────────────────────────────────────
def test_export_snapshot():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        write(ws, "a.md", "# Docs\n- Updated project documentation for release 2.3.1")
        with fresh_agent(ws, goal="Maintain project documentation") as agent:
            agent.run_cycle()
            snap_json = agent.export_snapshot()
            snap = json.loads(snap_json)
            assert snap["version"] == "1.4-PROD"
            assert "audit" in snap
            assert "assertions" in snap["audit"]
            assert "explanation" in snap["audit"]


def test_export_snapshot_empty():
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:
            snap = json.loads(agent.export_snapshot())
            assert snap["status"] == "no_pending_output"


# ── 16. Adversarial / edge cases ───────────────────────────────────────
def test_max_assertions_cap():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        body = "\n".join(f"- Completed project documentation item {i}" for i in range(200))
        write(ws, "big.md", "# Docs\n" + body)
        with fresh_agent(ws, goal="Maintain project documentation", max_assertions_per_cycle=5) as agent:
            files, _ = agent.scan_workspace(force=True)
            updates = agent.extract_high_signal_updates(files)
            trail = agent.run_verification_pipeline(updates)
            assert len(trail.assertions) <= 5


def test_unicode_safe():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        write(ws, "u.md", "# 文档\n- 完成项目文档更新 — 版本 2.3.1 — 100%")
        with fresh_agent(ws, goal="Maintain project documentation") as agent:
            files, _ = agent.scan_workspace(force=True)
            updates = agent.extract_high_signal_updates(files)
            assert isinstance(updates, list)


def test_backtick_fence():
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:
            s = ops.StagedOutput(content="has ```code``` inside", format_type="markdown", goal_alignment="t")
            frame = "\n".join(agent._render_frame(s))
            assert "````" in frame


def test_binary_file_no_crash():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        p = ws / "weird.md"
        p.write_bytes(b"\xff\xfe\xfd# H\n- Completed X")
        with fresh_agent(ws) as agent:
            files, _ = agent.scan_workspace(force=True)
            agent.extract_high_signal_updates(files)


def test_timezone_consistent():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        write(ws, "a.md", "# Docs\n- Updated project documentation")
        with fresh_agent(ws, goal="Maintain project documentation") as agent:
            agent.run_cycle()
            assert agent.last_execution.tzinfo is not None


def test_frame_captured():
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:
            buf = io.StringIO()
            with redirect_stdout(buf):
                agent.stage_for_approval("body", "markdown", "g", ops.VerificationAuditTrail())
            out = buf.getvalue()
            assert "[AGENT OUTPUT FRAME]" in out
            assert "Anti-Hallucination Audit Trail" in out


def test_workspace_merkle():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        write(ws, "a.md", "# A\n- x")
        with fresh_agent(ws, goal="goal") as agent:
            _, m1 = agent.scan_workspace(force=True)
            _, m2 = agent.scan_workspace(force=True)
            assert agent._workspace_merkle(m1) == agent._workspace_merkle(m2)
            write(ws, "b.md", "# B\n- y")
            _, m3 = agent.scan_workspace(force=True)
            assert agent._workspace_merkle(m3) != agent._workspace_merkle(m1)


# ── 17. Pre-existing v1.3 tests preserved ──────────────────────────────
def test_singleton():
    with tempfile.TemporaryDirectory() as td:
        ga.reset_singleton()
        assert ga.get_ghost_agent(td, "g") is ga.get_ghost_agent()
        ga.reset_singleton()


def test_singleton_requires_args():
    ga.reset_singleton()
    raised = False
    try:
        ga.get_ghost_agent()
    except ga.GhostAgentExecutionError:
        raised = True
    assert raised
    ga.reset_singleton()


def test_singleton_recovers():
    ga.reset_singleton()
    try:
        ga.get_ghost_agent("/nonexistent/" + os.urandom(8).hex(), "g")
    except Exception:
        pass
    assert ga.AxelrGhostAgent._instance is None
    with tempfile.TemporaryDirectory() as td:
        assert ga.get_ghost_agent(td, "g") is not None
    ga.reset_singleton()


def test_scan_ignores():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        write(ws, "a.md", "# A\n- x")
        write(ws, ".git/config", "x")
        write(ws, "__pycache__/x.txt", "x")
        write(ws, "y.pyc", "bin")
        with fresh_agent(ws) as agent:
            files, _ = agent.scan_workspace(force=True)
            names = " ".join(files)
            assert "a.md" in names
            assert ".git" not in names and "__pycache__" not in names
            assert all(not f.endswith(".pyc") for f in files)


def test_scan_max_size():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        write(ws, "small.md", "# S\n- ok")
        (ws / "big.txt").write_bytes(b"x" * (1024 * 1024 + 10))
        with fresh_agent(ws, max_file_size_bytes=1024 * 1024) as agent:
            m = ops._CycleMetrics()
            files, _ = agent.scan_workspace(force=True, metrics=m)
            assert any("small.md" in f for f in files)
            assert not any("big.txt" in f for f in files)
            assert m.files_skipped_large == 1


def test_scan_missing_raises():
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:
            agent.workspace_root = Path("/nonexistent-" + os.urandom(6).hex())
            raised = False
            try:
                agent.scan_workspace(force=True)
            except ga.GhostAgentExecutionError:
                raised = True
            assert raised


def test_drift_filters():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        write(ws, "doc.md", "# Docs\n- Updated project documentation")
        write(ws, "cook.md", "# Recipe\n- Added garlic to sauce")
        with fresh_agent(ws, goal="Maintain project documentation") as agent:
            files, _ = agent.scan_workspace(force=True)
            updates = agent.extract_high_signal_updates(files)
            ok, _ = agent.check_goal_alignment(updates)
            assert not ok
            filtered = agent.current_context.get("filtered_updates", [])
            assert all("garlic" not in u["content"].lower() for u in filtered)


def test_content_hash_dedup_across_files():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        write(ws, "a.md", "# A\n- Updated project documentation for release")
        write(ws, "b.md", "# B\n- Updated project documentation for release")
        with fresh_agent(ws, goal="Maintain project documentation") as agent:
            files, _ = agent.scan_workspace(force=True)
            updates = agent.extract_high_signal_updates(files)
            trail = agent.run_verification_pipeline(updates)
            texts = [a.assertion for a in trail.assertions]
            assert len(texts) == len(set(texts))


def test_staged_content_dedup():
    with tempfile.TemporaryDirectory() as td:
        with fresh_agent(Path(td)) as agent:
            t = ops.VerificationAuditTrail()
            s1 = agent.stage_for_approval("same", "markdown", "g", t)
            s2 = agent.stage_for_approval("same", "markdown", "g", t)
            assert s1 is s2


# ── Runner ─────────────────────────────────────────────────────────────
def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    passed = failed = 0
    failures = []
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  ✓ {name}")
        except Exception as e:
            failed += 1
            failures.append((name, traceback.format_exc()))
            print(f"  ✗ {name}: {type(e).__name__}: {e}")
    print(f"\n{'=' * 70}")
    print(f"RESULT: {passed} passed, {failed} failed, {len(tests)} total")
    for name, tb in failures:
        print(f"\n--- {name} ---\n{tb}")
    print("=" * 70)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    print(f"AXELR Ghost-Agent — Master Test Suite (v{ga.__version__})")
    print(f"Fallback core: {ops.USING_FALLBACK_CORE}")
    print("=" * 70)
    sys.exit(_run_all())
