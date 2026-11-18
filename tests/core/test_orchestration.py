"""
Orchestration contract tests.

Covers: Conductor (path selection, budget, memo, circuit breaker, deadline,
percentile, SLO), latency tier deadlines, path budgets (single + goat),
deadline-based selection, routing DAG, consensus (similarity + race),
goat layer, reflection, hardening, condenser, ResilientPipeline.

Consolidates: test_orchestration.py + test_latency_budget.py.
Converts pure-function 1000x loops to parametrize. Fixes 8 missing
@pytest.mark.asyncio decorators. Marks wall-clock tests @perf.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from core import (orchestration, routing, consensus, goat_layer,
                  hardening, condenser, pipeline)


# ═══════════════════════════════════════════════════════════════════════════
# §1 Path selection
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("workload,length,critical,expected", [
    ("trivial", 100, False, "single"),
    ("deep", 10000, False, "goat"),
    ("standard", 500, True, "consensus"),
    ("standard", 5000, True, "cognitive"),
    ("extraction", 10000, True, "single"),  # extraction always single
])
def test_conductor_path_selection(workload, length, critical, expected):
    assert orchestration.select_path(workload, length=length,
                                     critical=critical) == expected


# ═══════════════════════════════════════════════════════════════════════════
# §2 Budget
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("workload,expected", [
    ("trivial", 0), ("critical", 3),
])
def test_conductor_budget(workload, expected):
    assert orchestration.get_budget(workload) == expected


def test_conductor_budget_calls_override():
    assert orchestration.get_budget("trivial", budget_calls=5) == 5


def test_conductor_budget_exhaustion_falls_back_single(monkeypatch):
    monkeypatch.setattr(orchestration, "get_budget", lambda *a, **kw: 0)
    assert orchestration.select_path("critical", length=1000,
                                     critical=True) == "single"


# ═══════════════════════════════════════════════════════════════════════════
# §3 Memo
# ═══════════════════════════════════════════════════════════════════════════

def test_conductor_memo_identical_request_hits():
    c = orchestration.Conductor()
    req = orchestration.Request(prompt="test", history=[])
    c.memoize(req, "result1")
    assert c.check_memo(req) == "result1"


def test_conductor_memo_excludes_history():
    c = orchestration.Conductor()
    req1 = orchestration.Request(prompt="test", history=[])
    req2 = orchestration.Request(prompt="test",
                                 history=[{"role": "user", "content": "old"}])
    c.memoize(req1, "result1")
    assert c.check_memo(req2) == "result1"


def test_conductor_memo_bounded_lru():
    c = orchestration.Conductor(memo_size=3)
    for i in range(10):
        c.memoize(orchestration.Request(prompt=f"test{i}", history=[]),
                  f"result{i}")
    assert len(c._memo) == 3


# ═══════════════════════════════════════════════════════════════════════════
# §4 Circuit breaker
# ═══════════════════════════════════════════════════════════════════════════

def test_conductor_breaker_2_failures_opens():
    c = orchestration.Conductor()
    c.record_failure("test_path")
    c.record_failure("test_path")
    assert c.is_path_available("test_path") is False


def test_conductor_breaker_cooldown_recloses():
    c = orchestration.Conductor(cooldown=0.1)
    c.record_failure("test_path")
    c.record_failure("test_path")
    assert c.is_path_available("test_path") is False
    time.sleep(0.15)
    assert c.is_path_available("test_path") is True


# ═══════════════════════════════════════════════════════════════════════════
# §5 Deadline
# ═══════════════════════════════════════════════════════════════════════════

def test_conductor_exceed_deadline_truncates_single():
    c = orchestration.Conductor()
    assert c.select_path_with_deadline("critical", 1000, deadline=0.001) == "single"


def test_conductor_respects_deadline_s_override():
    c = orchestration.Conductor()
    assert c.get_deadline("trivial", deadline_s=10) == 10


# ═══════════════════════════════════════════════════════════════════════════
# §6 Latency budgets (parametrize replaces 1000x loops)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("tier,expected", [
    ("free", 3.5), ("pro", 5.5), ("business", 7.0),
    ("enterprise", 10.0), ("elite", 15.0),
])
def test_tier_deadline_enforcement(tier, expected):
    c = orchestration.Conductor()
    assert c.get_tier_deadline(tier) == expected


def test_unknown_tier_deadline_falls_back():
    assert orchestration.Conductor().get_tier_deadline("unknown_xyz") == 3.5


def test_none_tier_deadline_default():
    assert orchestration.Conductor().get_tier_deadline(None) == 3.5


@pytest.mark.parametrize("workload,expected", [
    ("trivial", 0), ("lite", 1), ("standard", 2), ("deep", 3),
])
def test_single_path_budget(workload, expected):
    assert orchestration.get_budget(workload, path="single") == expected


@pytest.mark.parametrize("workload,expected", [
    ("trivial", 1), ("lite", 2), ("standard", 4), ("deep", 6),
])
def test_goat_path_budget(workload, expected):
    assert orchestration.get_budget(workload, path="goat") == expected


def test_deadline_exceeded_falls_back_single():
    c = orchestration.Conductor()
    assert c.select_path_with_deadline("deep", length=10000,
                                       deadline=0.001) == "single"


def test_deadline_sufficient_allows_goat():
    c = orchestration.Conductor()
    assert c.select_path_with_deadline("deep", length=10000,
                                       deadline=30.0) == "goat"


def test_extraction_always_single():
    c = orchestration.Conductor()
    assert c.select_path_with_deadline("extraction", length=10000,
                                       critical=True, deadline=0.001) == "single"


# ═══════════════════════════════════════════════════════════════════════════
# §7 Percentile & SLO
# ═══════════════════════════════════════════════════════════════════════════

def test_latency_percentile_calculation():
    c = orchestration.Conductor()
    latencies = list(range(1000))
    assert c.calculate_percentile(latencies, 50) == 500
    assert c.calculate_percentile(latencies, 95) == 950
    assert c.calculate_percentile(latencies, 99) == 990


def test_latency_percentile_empty():
    assert orchestration.Conductor().calculate_percentile([], 50) == 0.0


@pytest.mark.parametrize("tier,latency,expected", [
    ("free", 3000.0, True), ("free", 4000.0, False),
    ("pro", 5000.0, True), ("pro", 6000.0, False),
])
def test_slo_compliance(tier, latency, expected):
    assert orchestration.Conductor().check_slo_compliance(
        tier=tier, latency_ms=latency) is expected


# ═══════════════════════════════════════════════════════════════════════════
# §8 Routing DAG
# ═══════════════════════════════════════════════════════════════════════════

def test_routing_valid_plan_executes():
    plan = routing.Plan(tasks=[routing.Task(id=1, depends_on=[])])
    assert len(routing.DAGExecutor().execute(plan)) == 1


def test_routing_empty_plan_falls_back_single():
    assert len(routing.DAGExecutor().execute(routing.Plan(tasks=[]))) == 1


def test_routing_caps_at_5_subtasks():
    plan = routing.Plan(tasks=[routing.Task(id=i, depends_on=[])
                               for i in range(20)])
    assert len(routing.DAGExecutor().execute(plan)) <= 5


def test_routing_circular_no_hang():
    plan = routing.Plan(tasks=[
        routing.Task(id=1, depends_on=[2]),
        routing.Task(id=2, depends_on=[1]),
    ])
    assert len(routing.DAGExecutor(timeout=1).execute(plan)) >= 1


def test_routing_invalid_role_coerced_core():
    assert routing.Task(id=1, role="invalid_role").role == "core"


# ═══════════════════════════════════════════════════════════════════════════
# §9 Consensus
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("a,b,expected", [
    ("hello", "hello", 1.0),
    ("abc", "xyz", 0.0),
])
def test_consensus_similarity(a, b, expected):
    assert consensus.similarity(a, b) == pytest.approx(expected, abs=0.01)


def test_consensus_symmetric():
    s1 = consensus.similarity("a b c", "x y z")
    s2 = consensus.similarity("x y z", "a b c")
    assert s1 == pytest.approx(s2)


@pytest.mark.parametrize("a,b", [("hello", "world"), ("a", "b"),
                                 ("test", "test123")])
def test_consensus_bounded_0_1(a, b):
    assert 0 <= consensus.similarity(a, b) <= 1


@pytest.mark.asyncio
async def test_consensus_race_picks_winner():
    async def c1(): return "answer1"
    async def c2(): return "answer2"
    winner = await consensus.race([c1(), c2()])
    assert winner is not None


@pytest.mark.asyncio
async def test_consensus_race_cancels_stragglers():
    slow = asyncio.Event()

    async def straggler():
        await slow.wait()
        return "slow"

    async def fast():
        return "fast"

    assert await consensus.race([fast(), straggler()], timeout=0.1) == "fast"


@pytest.mark.asyncio
async def test_consensus_race_errors_dont_crash():
    async def error(): raise RuntimeError("fail")
    async def ok(): return "ok"
    assert await consensus.race([error(), ok()]) == "ok"


# ═══════════════════════════════════════════════════════════════════════════
# §10 Goat layer
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text,expected", [
    ("hello world", "trivial"),
    ("a" * 500, "lite"),
    ("a" * 2000, "standard"),
    ("a" * 20000, "deep"),
])
def test_goat_classify_workload(text, expected):
    assert goat_layer.classify_workload(text) == expected


def test_goat_compress_anchors_preserves_recent():
    anchors = [goat_layer.Anchor(text=f"a{i}", timestamp=i) for i in range(100)]
    compressed = goat_layer.compress_anchors(anchors, max_anchors=10)
    assert len(compressed) == 10
    assert all(a.timestamp >= 90 for a in compressed)


def test_goat_local_repair_closes_fences():
    assert goat_layer.local_repair("```python\ncode") == "```python\ncode\n```"


# ═══════════════════════════════════════════════════════════════════════════
# §11 Reflection
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text,defect", [
    ("hallucination", "hallucination"),
    ("contradiction", "contradiction"),
    ("incomplete", "incomplete"),
])
def test_reflection_detects_defects(text, defect):
    assert reflection.detect_defects(text)[defect] > 0


def test_reflection_clean_text_passes():
    assert sum(reflection.detect_defects("this is clean text").values()) == 0


# ═══════════════════════════════════════════════════════════════════════════
# §12 Hardening
# ═══════════════════════════════════════════════════════════════════════════

def test_hardening_prune_system_idempotent():
    text = "system prompt\nuser: hello"
    once = hardening.prune_system(text)
    assert hardening.prune_system(once) == once


def test_hardening_prune_preserves_code_fences():
    assert "```python" in hardening.prune_system("```python\ncode\n```")


def test_hardening_ast_repair_closes_braces():
    assert hardening.ast_repair("function() {") == "function() {}"


def test_hardening_decompose_bullets():
    assert len(hardening.decompose_bullets("- a\n- b\n- c")) == 3


def test_hardening_short_text_not_decomposable():
    assert hardening.is_decomposable("short") is False


# ═══════════════════════════════════════════════════════════════════════════
# §13 Condenser
# ═══════════════════════════════════════════════════════════════════════════

def test_condenser_short_unchanged():
    text = "short text"
    assert condenser.condense(text, max_tokens=100) == text


def test_condenser_long_preserves_fences():
    text = "```python\ncode\n```\n" * 100
    assert "```python" in condenser.condense(text, max_tokens=100)


def test_condenser_anchors_extracts_identifiers():
    assert "foo" in condenser.anchors_of("def foo():\n    x = 123")


# ═══════════════════════════════════════════════════════════════════════════
# §14 Pipeline
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_pipeline_stream_yields_chunks():
    p = pipeline.ResilientPipeline()
    chunks = [c async for c in p.stream("test")]
    assert len(chunks) >= 2


@pytest.mark.asyncio
async def test_pipeline_stub_on_total_failure(monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("fail")
    monkeypatch.setattr(pipeline.ResilientPipeline, "execute", boom)
    result = await pipeline.ResilientPipeline().run("test")
    assert result["stub"] is True


@pytest.mark.perf
@pytest.mark.asyncio
async def test_pipeline_respects_deadline():
    """Wall-clock assertion — marked @perf, skipped in CI by default."""
    p = pipeline.ResilientPipeline()
    start = time.time()
    await p.run("test", deadline=0.001)
    assert time.time() - start < 0.5


def test_orchestration_audit_shape():
    audit = orchestration.audit()
    for k in ("conductor", "circuit_breaker", "memo"):
        assert k in audit