"""
Unit tests for core/routing.py — intent router + orchestrator + providers.
=========================================================================
Migrated from test_orchestrator.py + test_provider_factory.py and merged
with new intent-router tests.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from core.routing import (
    OPENAI_COMPAT,
    ROLE_SYSTEMS,
    IntentResult,
    IntentRouter,
    OpenAICompatProvider,
    Orchestrator,
    Subtask,
    _VALID_ROLES,
    call_openai_compat,
    get_router,
    make_provider_func,
)


# ═══════════════════════════════════════════════════════════════════════════
# Section: IntentRouter
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_intent_classifier_detects_design():
    r = IntentRouter()
    result = await r.classify("design a responsive navbar")
    assert result.workspace == "design"
    assert result.confidence > 0.5


@pytest.mark.asyncio
async def test_intent_classifier_detects_data():
    r = IntentRouter()
    result = await r.classify("analyze this csv of sales data")
    assert result.workspace == "data"


@pytest.mark.asyncio
async def test_intent_classifier_detects_code():
    r = IntentRouter()
    result = await r.classify("debug this python function with a stack trace")
    assert result.workspace == "core"


@pytest.mark.asyncio
async def test_intent_classifier_falls_back_to_core():
    r = IntentRouter()
    result = await r.classify("hello there")
    assert result.workspace == "core"
    assert result.method in ("fallback", "rules")


@pytest.mark.asyncio
async def test_intent_classifier_uses_file_extensions():
    r = IntentRouter()
    result = await r.classify(
        "process this",
        files=[{"filename": "data.csv"}, {"filename": "more.csv"}],
    )
    assert result.workspace == "data"


@pytest.mark.asyncio
async def test_intent_classifier_never_raises():
    r = IntentRouter()
    # Even garbage input must produce a valid result
    result = await r.classify("")
    assert isinstance(result, IntentResult)


def test_intent_result_to_dict():
    r = IntentResult("design", 0.88, "rules")
    d = r.to_dict()
    assert d["workspace"] == "design"
    assert d["confidence"] == 0.88
    assert d["method"] == "rules"


def test_get_router_singleton():
    import core.routing as routing_mod
    routing_mod._default_router = None
    r1 = get_router()
    r2 = get_router()
    assert r1 is r2


# ═══════════════════════════════════════════════════════════════════════════
# Section: Orchestrator — happy path
# ═══════════════════════════════════════════════════════════════════════════

def _plan_json(*subtasks):
    return json.dumps({"subtasks": list(subtasks)})


def _crit_json(verdict="pass"):
    return json.dumps({
        "overall": verdict,
        "per_subtask": [{
            "id": "s1", "verdict": verdict,
            "issues": [], "severity": "low",
        }],
        "cross_cutting_issues": [],
    })


@pytest.mark.asyncio
async def test_stream_emits_expected_phase_sequence(fake_route_fn):
    fake_route_fn.set_response("PLANNER", _plan_json(
        {"id": "s1", "role": "code", "instruction": "Write fizzbuzz",
         "depends_on": []},
    ))
    fake_route_fn.set_response("CRITIC", _crit_json())
    fake_route_fn.set_response("SYNTHESIZER", "FINAL_DELIVERABLE")

    orch = Orchestrator(fake_route_fn, max_parallel=2)
    events = [e async for e in orch.stream("Write fizzbuzz", "free", None)]
    types = [e["type"] for e in events]

    assert "phase" in types
    assert "plan" in types
    assert "subtask_start" in types
    assert "subtask_done" in types
    assert "critique" in types
    assert "final" in types
    assert "done" in types

    phases = [e["name"] for e in events if e["type"] == "phase"]
    assert phases == ["planning", "executing", "critique", "synthesis"]

    final = next(e for e in events if e["type"] == "final")
    assert final["text"] == "FINAL_DELIVERABLE"


@pytest.mark.asyncio
async def test_run_aggregates_stream(fake_route_fn):
    fake_route_fn.set_response("PLANNER", _plan_json(
        {"id": "s1", "role": "core", "instruction": "Say hi",
         "depends_on": []},
    ))
    fake_route_fn.set_response("CRITIC", _crit_json())
    fake_route_fn.set_response("SYNTHESIZER", "HI")

    orch = Orchestrator(fake_route_fn)
    out = await orch.run("Say hi", "free", None)

    assert out["success"] is True
    assert out["final"] == "HI"
    assert len(out["plan"]) == 1
    assert out["total_latency_ms"] >= 0


# ═══════════════════════════════════════════════════════════════════════════
# Section: Orchestrator — graceful degradation
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_planner_garbage_falls_back_to_single_task(fake_route_fn):
    fake_route_fn.set_response("PLANNER", "I refuse to give JSON.")
    fake_route_fn.set_response("CRITIC", _crit_json())
    fake_route_fn.set_response("SYNTHESIZER", "FALLBACK")

    orch = Orchestrator(fake_route_fn)
    out = await orch.run("do something", "free", None)
    assert out["success"] is True
    assert len(out["plan"]) == 1
    assert out["plan"][0]["role"] == "core"


@pytest.mark.asyncio
async def test_invalid_role_coerced_to_core(fake_route_fn):
    fake_route_fn.set_response("PLANNER", _plan_json(
        {"id": "s1", "role": "wizard", "instruction": "Cast spell",
         "depends_on": []},
    ))
    fake_route_fn.set_response("CRITIC", _crit_json())
    fake_route_fn.set_response("SYNTHESIZER", "OK")

    orch = Orchestrator(fake_route_fn)
    out = await orch.run("x", "free", None)
    assert out["plan"][0]["role"] == "core"


@pytest.mark.asyncio
async def test_planner_caps_subtasks_at_five(fake_route_fn):
    fake_route_fn.set_response("PLANNER", _plan_json(*[
        {"id": f"s{i}", "role": "core", "instruction": f"step {i}",
         "depends_on": []}
        for i in range(10)
    ]))
    fake_route_fn.set_response("CRITIC", _crit_json())
    fake_route_fn.set_response("SYNTHESIZER", "OK")

    orch = Orchestrator(fake_route_fn)
    out = await orch.run("x", "free", None)
    assert len(out["plan"]) == 5


@pytest.mark.asyncio
async def test_duplicate_ids_disambiguated(fake_route_fn):
    fake_route_fn.set_response("PLANNER", _plan_json(
        {"id": "s1", "role": "core", "instruction": "a", "depends_on": []},
        {"id": "s1", "role": "core", "instruction": "b", "depends_on": []},
    ))
    fake_route_fn.set_response("CRITIC", _crit_json())
    fake_route_fn.set_response("SYNTHESIZER", "OK")

    orch = Orchestrator(fake_route_fn)
    out = await orch.run("x", "free", None)
    ids = [s["id"] for s in out["plan"]]
    assert len(ids) == len(set(ids))


# ═══════════════════════════════════════════════════════════════════════════
# Section: Orchestrator — DAG semantics
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_dependency_ordering_respected(fake_route_fn):
    fake_route_fn.set_response("PLANNER", _plan_json(
        {"id": "a", "role": "core", "instruction": "TASK_A",
         "depends_on": []},
        {"id": "b", "role": "core", "instruction": "TASK_B",
         "depends_on": ["a"]},
    ))
    fake_route_fn.set_response("CRITIC", _crit_json())
    fake_route_fn.set_response("SYNTHESIZER", "OK")

    order: list[str] = []

    async def spy_route(**kwargs):
        p = kwargs["prompt"]
        if "TASK_A" in p:
            order.append("a")
        if "TASK_B" in p:
            order.append("b")
        for marker, reply in fake_route_fn.responses.items():
            if marker in p:
                return {"success": True, "text": reply}
        return {"success": True, "text": fake_route_fn.default}

    orch = Orchestrator(spy_route)
    await orch.run("x", "free", None)
    assert order.index("a") < order.index("b")


@pytest.mark.asyncio
async def test_circular_dependency_does_not_hang(fake_route_fn):
    fake_route_fn.set_response("PLANNER", _plan_json(
        {"id": "a", "role": "core", "instruction": "A",
         "depends_on": ["b"]},
        {"id": "b", "role": "core", "instruction": "B",
         "depends_on": ["a"]},
    ))
    fake_route_fn.set_response("CRITIC", _crit_json())
    fake_route_fn.set_response("SYNTHESIZER", "OK")

    orch = Orchestrator(fake_route_fn)
    out = await asyncio.wait_for(orch.run("x", "free", None), timeout=5.0)
    assert out["success"] is True


@pytest.mark.asyncio
async def test_subtask_failure_is_isolated(fake_route_fn):
    fake_route_fn.set_response("PLANNER", _plan_json(
        {"id": "good", "role": "core", "instruction": "GOOD_TASK",
         "depends_on": []},
        {"id": "bad", "role": "core", "instruction": "BAD_TASK",
         "depends_on": []},
    ))
    fake_route_fn.set_response("CRITIC", _crit_json())
    fake_route_fn.set_response("SYNTHESIZER", "SURVIVED")

    async def flaky_route(**kwargs):
        if "BAD_TASK" in kwargs["prompt"]:
            raise RuntimeError("simulated upstream failure")
        for marker, reply in fake_route_fn.responses.items():
            if marker in kwargs["prompt"]:
                return {"success": True, "text": reply}
        return {"success": True, "text": fake_route_fn.default}

    orch = Orchestrator(flaky_route)
    out = await orch.run("x", "free", None)
    by_id = {s["id"]: s for s in out["plan"]}
    assert by_id["good"]["error"] is None
    assert by_id["bad"]["error"] is not None


# ═══════════════════════════════════════════════════════════════════════════
# Section: Orchestrator — helpers
# ═══════════════════════════════════════════════════════════════════════════

def test_dependent_subtasks_bfs():
    orch = Orchestrator(AsyncMock())
    subtasks = [
        Subtask("a", "core", "x"),
        Subtask("b", "core", "y", depends_on=["a"]),
        Subtask("c", "core", "z", depends_on=["b"]),
        Subtask("d", "core", "w", depends_on=["a"]),
    ]
    deps = orch._get_dependent_subtasks("a", subtasks)
    assert deps == {"b", "c", "d"}


def test_valid_roles_matches_role_systems():
    assert _VALID_ROLES == set(ROLE_SYSTEMS.keys())


# ═══════════════════════════════════════════════════════════════════════════
# Section: Provider factory
# ═══════════════════════════════════════════════════════════════════════════

def test_catalog_has_expected_providers():
    for name in ("groq", "openrouter", "mistral", "modelscope", "zhipuai"):
        assert name in OPENAI_COMPAT, f"{name} missing from catalog"


def test_each_provider_is_well_formed():
    for name, p in OPENAI_COMPAT.items():
        assert p.url.startswith(("http://", "https://")), f"{name} bad url"
        assert p.key_env, f"{name} missing key_env"
        assert p.default_model, f"{name} missing default_model"


@pytest.mark.asyncio
async def test_call_openai_compat_happy_path(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    prov = OPENAI_COMPAT["groq"]

    mock_client = AsyncMock()
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.json = lambda: {"choices": [{"message": {"content": "hello"}}]}
    mock_resp.raise_for_status = lambda: None
    mock_client.post = AsyncMock(return_value=mock_resp)

    out = await call_openai_compat(prov, "hi", 16, 0.1, None, mock_client)
    assert out == "hello"
    args, _ = mock_client.post.call_args
    assert args[0] == prov.url


@pytest.mark.asyncio
async def test_call_openai_compat_429_raises_quota(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    prov = OPENAI_COMPAT["groq"]

    mock_client = AsyncMock()
    mock_resp = AsyncMock()
    mock_resp.status_code = 429
    mock_resp.text = "quota exceeded"
    mock_client.post = AsyncMock(return_value=mock_resp)

    with pytest.raises(RuntimeError, match="quota"):
        await call_openai_compat(prov, "hi", 16, 0.1, None, mock_client)


@pytest.mark.asyncio
async def test_call_openai_compat_missing_key_raises(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    prov = OPENAI_COMPAT["groq"]

    mock_client = AsyncMock()
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        await call_openai_compat(prov, "hi", 16, 0.1, None, mock_client)


def test_make_provider_func_returns_callable():
    mock_client = AsyncMock()
    fn = make_provider_func("groq", mock_client)
    assert callable(fn)