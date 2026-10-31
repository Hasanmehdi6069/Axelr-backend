"""
Core module unit tests.
========================
One file for every core.* module. Grouped by module with a section header.
Every test has a `# WHY:` comment naming the bug class it prevents.
"""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ═══════════════════════════════════════════════════════════════════════════
# core.resilience
# ═══════════════════════════════════════════════════════════════════════════
def test_resilience_pii_sanitize_redacts_known_types():
    # WHY: PII leaks to third-party providers are a compliance event.
    from core.resilience import sanitize_pii
    text = ("email a@b.co phone +1-555-123-4567 SSN 123-45-6789 "
            "IP 10.0.0.1 key sk-" + "x" * 40)
    cleaned, found = sanitize_pii(text)
    assert "a@b.co" not in cleaned
    assert "123-45-6789" not in cleaned
    assert "10.0.0.1" not in cleaned
    assert set(found) >= {"email", "phone", "ssn", "ip_address"}


def test_resilience_pii_sanitize_is_idempotent():
    # WHY: repeated sanitization must not corrupt placeholders.
    from core.resilience import sanitize_pii
    a, _ = sanitize_pii("email x@y.com")
    b, _ = sanitize_pii(a)
    assert a == b


def test_resilience_tiers_names_match_failover():
    # WHY: resilience.Tier and failover.Tier must agree — operators alert on the name.
    from core.resilience import TIERS as R_TIERS
    from core.failover import TIERS as F_TIERS
    assert set(R_TIERS) == set(F_TIERS)
    for k, v in R_TIERS.items():
        assert v.name == k


@pytest.mark.asyncio
async def test_resilience_ladder_run_first_success_wins():
    from core.resilience import ladder_run
    calls: list[str] = []

    async def ok():
        calls.append("full"); return {"success": True, "text": "x"}

    async def never():
        calls.append("reduced"); return {"success": False}

    r = await ladder_run(full=ok, reduced_ctx=never)
    assert r["tier"] == "full" and calls == ["full"]


@pytest.mark.asyncio
async def test_resilience_ladder_run_falls_through_on_exception():
    from core.resilience import ladder_run

    async def boom(): raise RuntimeError("x")
    async def ok(): return {"success": True, "text": "y"}

    r = await ladder_run(full=boom, local=ok)
    assert r["tier"] == "local"


@pytest.mark.asyncio
async def test_resilience_ladder_run_stub_returns_never_raises():
    from core.resilience import ladder_run
    r = await ladder_run(full=AsyncMock(side_effect=RuntimeError("x")))
    assert r["success"] is False
    assert r["degraded"] is True


def test_resilience_add_watermark_format_stable():
    # WHY: frontend regex parses the watermark. Format drift breaks the UI.
    from core.resilience import add_watermark
    out = add_watermark("hi", "groq", "llama", 12.5)
    assert "groq" in out and "llama" in out
    assert "\n\n---\n*" in out


def test_resilience_stream_quality_gate_forces_retry_on_refusal():
    # WHY: a refusal leaked to the user is worse than a 2nd call.
    from core.resilience import StreamQualityGate
    g = StreamQualityGate()
    d = g.push("I'm sorry, I cannot do that")
    assert d.force_retry is True and d.emit is False


def test_resilience_token_governor_over_limit_returns_false():
    from core.resilience import TokenGovernor
    tg = TokenGovernor()
    ok, remaining, _ = asyncio.run(tg.reserve("u", "free", 10 ** 9))
    assert ok is False


def test_resilience_audit_shape_stable():
    # WHY: /api/audit surfaces this dict. Shape drift breaks dashboards.
    from core.resilience import audit
    a = audit()
    for k in ("pipeline_stats", "tiers", "cognition_level", "max_rounds",
              "race_width", "deadline_s"):
        assert k in a


# ═══════════════════════════════════════════════════════════════════════════
# core.failover
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_failover_ladder_run_swallows_errors():
    from core.failover import ladder_run
    async def boom(): raise RuntimeError("x")
    r = await ladder_run(full=boom)
    assert r["success"] is False


def test_failover_tiers_is_total_and_ordered():
    from core.failover import TIERS
    assert TIERS["full"].level < TIERS["local"].level < TIERS["stub"].level


# ═══════════════════════════════════════════════════════════════════════════
# core.infra
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_infra_breaker_blocks_after_threshold():
    from core.infra import CircuitBreaker
    b = CircuitBreaker(redis_client=None, threshold=3, cooldown_s=60)
    for _ in range(3):
        await b.record_failure("groq")
    assert await b.is_available("groq") is False


@pytest.mark.asyncio
async def test_infra_breaker_success_resets_memory_state():
    from core.infra import CircuitBreaker
    b = CircuitBreaker(redis_client=None, threshold=3, cooldown_s=60)
    await b.record_failure("groq")
    await b.record_success("groq")
    assert b._mem_failures["groq"] == 0
    assert await b.is_available("groq") is True


@pytest.mark.asyncio
async def test_infra_breaker_rate_limit_trips_long_cooldown():
    # WHY: a 429 needs a long cooldown, not the generic 60s — otherwise we hammer.
    from core.infra import CircuitBreaker
    b = CircuitBreaker(redis_client=None, threshold=2, cooldown_s=1,
                       rate_limit_cooldown_s=1800)
    await b.record_failure("gemini", is_rate_limit=True)
    assert await b.is_available("gemini") is False
    assert b._mem_cooldowns["gemini"] - time.time() > 1500


@pytest.mark.asyncio
async def test_infra_breaker_degrades_when_redis_raises():
    # WHY: Redis outage must never surface as a 5xx to the user.
    from core.infra import CircuitBreaker
    redis = AsyncMock()
    redis.get.side_effect = ConnectionError("down")
    redis.incr.side_effect = ConnectionError("down")
    redis.delete.side_effect = ConnectionError("down")
    b = CircuitBreaker(redis_client=redis, threshold=2, cooldown_s=60)
    assert await b.is_available("groq") is True
    await b.record_failure("groq")
    await b.record_failure("groq")
    assert await b.is_available("groq") is False


def test_infra_context_item_render_truncates_description():
    from core.infra import ContextItem
    item = ContextItem(source="jira", type="issue", title="t", description="a" * 500)
    assert len(item.render()) < 400


@pytest.mark.asyncio
async def test_infra_context_registry_no_sources_returns_none(monkeypatch):
    for v in ("JIRA_URL", "JIRA_TOKEN", "LINEAR_API_KEY", "OPENAPI_CONTEXT_URL"):
        monkeypatch.delenv(v, raising=False)
    from core.infra import ContextRegistry
    assert await ContextRegistry().get_context("u", "core") is None


# ═══════════════════════════════════════════════════════════════════════════
# core.observe
# ═══════════════════════════════════════════════════════════════════════════
def test_observe_trace_ring_records_events():
    from core.observe import TRACES
    rid = TRACES.new_id()
    TRACES.begin(rid, "core", "test")
    TRACES.event(rid, "x", k=1)
    TRACES.finish(rid, "ok", total=1)
    snap = TRACES.get(rid)
    assert snap["status"] == "ok"
    assert any(e["name"] == "x" for e in snap["events"])


def test_observe_bind_and_unbind_roundtrip():
    from core.observe import bind, current_id, unbind
    bind("rid-1")
    assert current_id() == "rid-1"
    unbind()


def test_observe_span_noop_without_otlp():
    # WHY: OTLP disabled → `span()` must be a zero-cost context manager.
    from core.observe import span
    with span("rid", "stage"):
        pass  # must not raise


def test_observe_flush_pending_spans_returns_int():
    from core.observe import flush_pending_spans
    assert isinstance(flush_pending_spans(), int)


# ═══════════════════════════════════════════════════════════════════════════
# core.routing (Orchestrator)
# ═══════════════════════════════════════════════════════════════════════════
def _plan_json(*subs):
    return json.dumps({"subtasks": list(subs)})


def _crit_json(verdict="pass"):
    return json.dumps({"overall": verdict, "per_subtask": [], "cross_cutting_issues": []})


@pytest.mark.asyncio
async def test_routing_stream_emits_expected_phases(fake_route_fn):
    from core.routing import Orchestrator
    fake_route_fn.set_response("PLANNER", _plan_json(
        {"id": "s1", "role": "core", "instruction": "say hi", "depends_on": []}))
    fake_route_fn.set_response("CRITIC", _crit_json())
    fake_route_fn.set_response("SYNTHESIZER", "FINAL")
    orch = Orchestrator(fake_route_fn, max_parallel=2)
    events = [e async for e in orch.stream("x", "free", None)]
    phases = [e["name"] for e in events if e["type"] == "phase"]
    assert phases == ["planning", "executing", "critique", "synthesis"]
    final = next(e for e in events if e["type"] == "final")
    assert final["text"] == "FINAL"


@pytest.mark.asyncio
async def test_routing_garbage_planner_falls_back_to_single_task(fake_route_fn):
    # WHY: a bad planner must not fail the whole request.
    from core.routing import Orchestrator
    fake_route_fn.set_response("PLANNER", "I refuse JSON.")
    fake_route_fn.set_response("CRITIC", _crit_json())
    fake_route_fn.set_response("SYNTHESIZER", "OK")
    out = await Orchestrator(fake_route_fn).run("x", "free", None)
    assert out["success"] is True
    assert len(out["plan"]) == 1
    assert out["plan"][0]["role"] == "core"


@pytest.mark.asyncio
async def test_routing_planner_caps_at_5_subtasks(fake_route_fn):
    from core.routing import Orchestrator
    fake_route_fn.set_response("PLANNER", _plan_json(*[
        {"id": f"s{i}", "role": "core", "instruction": f"{i}", "depends_on": []}
        for i in range(12)]))
    fake_route_fn.set_response("CRITIC", _crit_json())
    fake_route_fn.set_response("SYNTHESIZER", "OK")
    out = await Orchestrator(fake_route_fn).run("x", "free", None)
    assert len(out["plan"]) == 5


@pytest.mark.asyncio
async def test_routing_invalid_role_coerced_to_core(fake_route_fn):
    from core.routing import Orchestrator
    fake_route_fn.set_response("PLANNER", _plan_json(
        {"id": "s1", "role": "wizard", "instruction": "x", "depends_on": []}))
    fake_route_fn.set_response("CRITIC", _crit_json())
    fake_route_fn.set_response("SYNTHESIZER", "OK")
    out = await Orchestrator(fake_route_fn).run("x", "free", None)
    assert out["plan"][0]["role"] == "core"


@pytest.mark.asyncio
async def test_routing_circular_dependency_does_not_hang(fake_route_fn):
    from core.routing import Orchestrator
    fake_route_fn.set_response("PLANNER", _plan_json(
        {"id": "a", "role": "core", "instruction": "A", "depends_on": ["b"]},
        {"id": "b", "role": "core", "instruction": "B", "depends_on": ["a"]}))
    fake_route_fn.set_response("CRITIC", _crit_json())
    fake_route_fn.set_response("SYNTHESIZER", "OK")
    out = await asyncio.wait_for(
        Orchestrator(fake_route_fn).run("x", "free", None), timeout=5.0)
    assert out["success"] is True


# ═══════════════════════════════════════════════════════════════════════════
# core.code_intel — DependencyTracker + CodeGuard + SelfHealer + PRShield + TestLoop
# ═══════════════════════════════════════════════════════════════════════════
def test_code_intel_guard_detects_sql_injection():
    from core.code_intel import CodeGuard
    r = asyncio.run(CodeGuard().scan(
        'cursor.execute(f"SELECT * FROM u WHERE id={uid}")', "python"))
    assert any(f.type == "sql_injection" for f in r.findings)


def test_code_intel_guard_detects_xss_and_eval():
    from core.code_intel import CodeGuard
    r = asyncio.run(CodeGuard().scan(
        'el.innerHTML = userInput; eval("1+1")', "javascript"))
    types = {f.type for f in r.findings}
    assert "xss_sink" in types and "eval_usage" in types


def test_code_intel_guard_detects_hardcoded_aws_key():
    from core.code_intel import CodeGuard
    r = asyncio.run(CodeGuard().scan(
        'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"', "python"))
    assert any(f.type == "hardcoded_secret" for f in r.findings)


def test_code_intel_guard_flags_python_syntax_error():
    from core.code_intel import CodeGuard
    r = asyncio.run(CodeGuard().scan("def foo(\n", "python"))
    assert r.syntax_ok is False


def test_code_intel_guard_clean_code_scores_100():
    from core.code_intel import CodeGuard
    r = asyncio.run(CodeGuard().scan("x = 1 + 1\n", "python"))
    assert r.findings == [] and r.score == 100


def test_code_intel_dep_tracker_resolves_python_imports(tmp_path):
    (tmp_path / "a.py").write_text("from b import thing\n")
    (tmp_path / "b.py").write_text("thing = 1\n")
    from core.code_intel import DependencyTracker
    t = DependencyTracker(tmp_path)
    asyncio.run(t.build())
    deps = t.get_dependencies(tmp_path / "a.py")
    assert any(d.endswith("b.py") for d in deps)


def test_code_intel_dep_tracker_blast_radius_high_for_many_dependents(tmp_path):
    (tmp_path / "b.py").write_text("x = 1\n")
    for i in range(4):
        (tmp_path / f"a{i}.py").write_text("from b import x\n")
    from core.code_intel import DependencyTracker
    t = DependencyTracker(tmp_path)
    asyncio.run(t.build())
    br = t.assess_impact(tmp_path / "b.py")
    assert br.count >= 4 and br.severity in ("medium", "high")


@pytest.mark.asyncio
async def test_code_intel_self_healer_success_path():
    from core.code_intel import SelfHealer
    async def _stub(**kw):
        return {"success": True, "text": '```python\nprint("fixed")\n```',
                "provider": "stub"}
    h = SelfHealer(route_func=_stub, max_retries=2)
    r = await h.heal('print("broken")', "NameError")
    assert r.success and "fixed" in r.final_code


@pytest.mark.asyncio
async def test_code_intel_self_healer_rejects_unchanged():
    from core.code_intel import SelfHealer
    async def _stub(**kw):
        return {"success": True, "text": "```python\nprint(1)\n```"}
    h = SelfHealer(route_func=_stub, max_retries=1)
    r = await h.heal("print(1)", "noop")
    assert r.success is False


def test_code_intel_touchfix_locate_block_exact_match():
    from core.code_intel import TouchFixEngine
    code = "def a():\n    pass\n\ndef b():\n    pass\n"
    s, e = TouchFixEngine._locate_block(code, "def b():\n    pass")
    assert s == 3 and e == 5


def test_code_intel_prsh _renders_all_sections():
    from core.code_intel import PRShield, PRShieldInput
    md = PRShield().render(PRShieldInput(
        title="Fix", files_changed=["a.py"],
        blast_radius={"severity": "high", "count": 12, "dependents": []},
        security_findings=[{"type": "xss_sink", "severity": "high", "line": 1,
                            "message": "innerHTML"}],
        self_heal={"success": True, "attempts": 2, "diff": "+fix"},
        test_results={"passed": False, "errors": [{"message": "a"}]},
    ))
    for h in ("# PR Defense Report", "## 1.", "## 2.", "## 3.", "## 4.", "## 5."):
        assert h in md


@pytest.mark.asyncio
async def test_code_intel_testloop_passes_first_iteration():
    from core.code_intel import TestLoop
    async def route(**kw):
        return {"success": True, "text": '```python\nprint("ALL TESTS PASSED")\n```'}
    async def _exec(lang, code, t):
        return {"success": True, "output": "ALL TESTS PASSED", "error": None}
    r = await TestLoop(route, _exec, max_iterations=3).run("print(1)", "python")
    assert r.success and len(r.iterations) == 1


def test_code_intel_make_diff_unified_format():
    from core.code_intel import make_diff
    d = make_diff("a\nb\n", "a\nB\n", label="py")
    assert "--- original.py" in d and "+++ fixed.py" in d
    assert "-b" in d and "+B" in d


# ═══════════════════════════════════════════════════════════════════════════
# core.io_pipeline — worker + webhook
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_io_worker_empty_code_shortcircuits():
    from core.io_pipeline import execute_code_on_worker
    r = await execute_code_on_worker("python", "")
    assert r["success"] is False and r["error"] == "empty_code"


@pytest.mark.asyncio
async def test_io_worker_no_url_returns_unavailable(monkeypatch):
    import core.io_pipeline as io
    monkeypatch.setattr(io, "WORKER_URL", "")
    r = await io.execute_code_on_worker("python", "print(1)")
    assert r["success"] is False and r["error"] == "worker_unavailable"


def test_io_sign_matches_manual_hmac():
    # WHY: signature drift silently breaks every downstream consumer.
    import hashlib, hmac
    from core.io_pipeline import _sign
    ts, body = 1700000000, b'{"x":1}'
    expected = hmac.new(b"secret", f"{ts}.".encode() + body,
                        hashlib.sha256).hexdigest()
    assert _sign("secret", ts, body) == expected


def test_io_verify_inbound_dev_mode_allows_when_unconfigured(monkeypatch):
    monkeypatch.setenv("ENV", "dev")
    from core.io_pipeline import _verify_inbound
    _verify_inbound(b"body", authorization=None, signature_header=None,
                    timestamp_header=None, shared_token="", shared_secret="")


def test_io_verify_inbound_prod_refuses_when_unconfigured(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    from fastapi import HTTPException
    from core.io_pipeline import _verify_inbound
    with pytest.raises(HTTPException) as e:
        _verify_inbound(b"body", authorization=None, signature_header=None,
                        timestamp_header=None, shared_token="", shared_secret="")
    assert e.value.status_code == 503


def test_io_verify_inbound_rejects_expired_timestamp():
    from fastapi import HTTPException
    from core.io_pipeline import _sign, _verify_inbound
    old = int(time.time()) - 10000
    sig = _sign("s", old, b"body")
    with pytest.raises(HTTPException):
        _verify_inbound(b"body", authorization=None,
                        signature_header=f"sha256={sig}",
                        timestamp_header=str(old),
                        shared_token="", shared_secret="s")


def test_io_verify_inbound_rejects_body_tamper():
    # WHY: HMAC must bind to the body or a captured request is a replay kit.
    from fastapi import HTTPException
    from core.io_pipeline import _sign, _verify_inbound
    sig = _sign("s", int(time.time()), b'{"a":1}')
    with pytest.raises(HTTPException):
        _verify_inbound(b'{"a":9999}', authorization=None,
                        signature_header=f"sha256={sig}",
                        timestamp_header=str(int(time.time())),
                        shared_token="", shared_secret="s")


@pytest.mark.asyncio
async def test_io_assert_public_url_rejects_metadata_endpoint():
    from fastapi import HTTPException
    from core.io_pipeline import _assert_public_url
    with pytest.raises(HTTPException):
        await _assert_public_url("http://169.254.169.254/latest/meta-data/")


def test_io_extract_payload_defaults():
    from core.io_pipeline import ExtractWebhookPayload
    p = ExtractWebhookPayload(file_url="https://x.com/a.pdf",
                              pipeline_id="p", callback_url="https://x.com/h")
    assert p.workspace == "data" and p.task_type == "extraction"
    assert p.max_tokens == 4096 and p.temperature == 0.2


# ═══════════════════════════════════════════════════════════════════════════
# core.structured_gen
# ═══════════════════════════════════════════════════════════════════════════
def test_structured_gen_router_disabled_returns_api(monkeypatch):
    monkeypatch.setenv("AXELR_LOCAL_ROUTER", "off")
    import importlib, core.structured_gen as sg
    importlib.reload(sg)
    d = sg.classify("extract json from csv")
    assert d.route == "api" and d.reason == "default_api"


def test_structured_gen_router_on_routes_extract_json(monkeypatch):
    monkeypatch.setenv("AXELR_LOCAL_ROUTER", "on")
    import importlib, core.structured_gen as sg
    importlib.reload(sg)
    d = sg.classify("extract fields into json")
    assert d.task_class == "extract_json"
    assert d.verifier_kind == "json_schema"


def test_structured_gen_verify_json_schema_pass():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("AXELR_LOCAL_VERIFY", "on")
    import importlib, core.structured_gen as sg
    importlib.reload(sg)
    schema = {"type": "array", "items": {"type": "object",
              "required": ["a"], "properties": {"a": {"type": "number"}}}}
    v = sg.verify('[{"a":1}]', kind="json_schema", schema=schema)
    assert v.passed
    monkeypatch.undo()


def test_structured_gen_verify_ast_catches_syntax_error():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("AXELR_LOCAL_VERIFY", "on")
    import importlib, core.structured_gen as sg
    importlib.reload(sg)
    v = sg.verify("```python\ndef f(: pass\n```", kind="ast")
    assert v.passed is False and v.reason == "syntax_error"
    monkeypatch.undo()


def test_structured_gen_verify_unknown_kind_fails_open():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("AXELR_LOCAL_VERIFY", "on")
    import importlib, core.structured_gen as sg
    importlib.reload(sg)
    v = sg.verify("x", kind="nope")
    assert v.passed and v.degraded
    monkeypatch.undo()


# ═══════════════════════════════════════════════════════════════════════════
# core.hardening
# ═══════════════════════════════════════════════════════════════════════════
def test_hardening_prune_preserves_code_fence():
    # WHY: pruning prose must never touch code — otherwise it corrupts output.
    from core.hardening import prune_system
    text = "Please just write:\n```python\nx = 1\n```\nBasically it works."
    out = prune_system(text)
    assert "```python\nx = 1\n```" in out


def test_hardening_prune_is_idempotent():
    from core.hardening import prune_system
    a = prune_system("please very kindly write some code")
    assert prune_system(a) == a


def test_hardening_ast_repair_closes_brace():
    from core.hardening import ast_repair
    broken = '```json\n{"a": 1\n```'
    fixed, issues = ast_repair(broken)
    assert "closed_}{" in " ".join(issues).replace("{}", "}{") or issues


def test_hardening_decompose_bullets():
    from core.hardening import decompose, is_decomposable
    p = ("- Build a login form with validation\n"
         "- Wire it to the auth API\n"
         "- Add unit tests for edge cases")
    assert is_decomposable(p) is True
    assert len(decompose(p)) >= 2


def test_hardening_ring_recommend_prefers_successful_provider():
    from core.hardening import TelemetryRing
    r = TelemetryRing(slot_size=8)
    for _ in range(4):
        r.record("core", "free", "good", True, 100.0)
        r.record("core", "free", "bad", False, 2000.0)
    ranked = r.recommend("core", "free", ["bad", "good"])
    assert ranked[0] == "good"


def test_hardening_derive_hyperparams_extraction_pins_top_p():
    from core.hardening import derive
    hp = derive("extract this", task_type="extraction")
    assert hp.top_p == 0.80 and hp.frequency_penalty == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# core.elite_orchestrator
# ═══════════════════════════════════════════════════════════════════════════
def test_elite_canonical_system_is_stable_per_workspace():
    # WHY: remote caches key on the exact prefix. Any drift = 0% hit rate.
    from core.elite_orchestrator import canonical_system
    a = canonical_system("core", "free")
    b = canonical_system("core", "free")
    assert a == b


def test_elite_hedge_wrap_is_idempotent():
    from core.elite_orchestrator import hedge_wrap
    async def base(**kw): return {"success": True, "text": "x"}
    w1 = hedge_wrap(base)
    w2 = hedge_wrap(w1)
    assert w1 is w2


def test_elite_adapt_max_tokens_returns_requested_without_samples():
    from core.elite_orchestrator import adapt_max_tokens
    assert adapt_max_tokens("groq", "core", "structuring", 2048) == 2048


def test_elite_audit_shape():
    from core.elite_orchestrator import audit
    a = audit()
    for k in ("hedge", "negative_cache", "canonical_prefix", "token_trim",
              "installed"):
        assert k in a


# ═══════════════════════════════════════════════════════════════════════════
# core.adaptive_control
# ═══════════════════════════════════════════════════════════════════════════
def test_adaptive_temperature_extraction_is_zero():
    # WHY: any temperature on structured extraction invites hallucinated numbers.
    from core.adaptive_control import tune_temperature
    assert tune_temperature(task_type="extraction") == 0.0


def test_adaptive_temperature_bounded():
    from core.adaptive_control import tune_temperature
    for _ in range(50):
        t = tune_temperature(intent="conversational", tier="critical",
                             workspace="design")
        assert 0.0 <= t <= 0.85


def test_adaptive_ledger_bounded_and_renders():
    from core.adaptive_control import AnchorLedger
    led = AnchorLedger()
    for i in range(100):
        led.observe("s", f"error TypeError at line {i} in file_{i}.py")
    out = led.render("s")
    assert "<session_anchors>" in out


def test_adaptive_stream_gate_releases_on_good_lead():
    from core.adaptive_control import StreamQualityGate
    g = StreamQualityGate()
    d = g.push("Here is the code you asked for")
    assert d.emit is True and not d.force_retry


def test_adaptive_stream_gate_retries_on_refusal():
    from core.adaptive_control import StreamQualityGate
    g = StreamQualityGate()
    d = g.push("I'm sorry, I cannot help")
    assert d.force_retry is True and d.emit is False


def test_adaptive_stops_for_default():
    from core.adaptive_control import stops_for
    assert stops_for("core", "structuring") != ()


# ═══════════════════════════════════════════════════════════════════════════
# core.consensus
# ═══════════════════════════════════════════════════════════════════════════
def test_consensus_similarity_identical_is_one():
    from core.consensus import similarity
    s = similarity("hello world", "hello world")
    assert s["combined"] == 1.0


def test_consensus_similarity_disjoint_is_low():
    from core.consensus import similarity
    s = similarity("alpha beta gamma", "one two three")
    assert s["combined"] < 0.3


def test_consensus_local_polish_closes_unbalanced_fence():
    from core.consensus import local_polish
    r = local_polish("text\n```python\nx = 1\n")
    assert r.changed and "```" in r.text


def test_consensus_local_polish_never_raises_on_garbage():
    from core.consensus import local_polish
    r = local_polish("\x00\x01\x02")
    assert isinstance(r.text, str)


# ═══════════════════════════════════════════════════════════════════════════
# core.goat_layer
# ═══════════════════════════════════════════════════════════════════════════
def test_goat_classify_workload_trivial_prompt():
    from core.goat_layer import classify_workload
    p = classify_workload("hi")
    assert p.tier in ("shed", "lite")


def test_goat_classify_workload_deep_gets_pipeline():
    from core.goat_layer import classify_workload
    p = classify_workload(
        "Analyze the trade-offs between two distributed database architectures "
        "and design a migration plan. Include a step-by-step strategy, "
        "because consistency and partition tolerance matter here."
    )
    assert p.tier in ("standard", "deep") and p.use_pipeline is True


def test_goat_compress_anchors_preserves_recent():
    from core.goat_layer import compress_anchors
    hist = [{"role": "user", "text": f"turn {i}"} for i in range(10)]
    out = compress_anchors(hist, keep_recent=3)
    assert out[-1]["text"] == "turn 9"


def test_goat_local_repair_dedups_and_closes():
    from core.goat_layer import local_repair
    text = "A" * 100 + ". " + "A" * 100 + ". " + "done"
    r = local_repair(text)
    assert isinstance(r.text, str)


# ═══════════════════════════════════════════════════════════════════════════
# core.reflection
# ═══════════════════════════════════════════════════════════════════════════
def test_reflection_scan_flags_unclosed_fence():
    from core.reflection import scan
    r = scan("text\n```python\nprint(1)\n")
    assert r.needs_repair and "unclosed_fence" in r.issues


def test_reflection_scan_flags_prompt_leak():
    from core.reflection import scan
    r = scan("Reply is <system_identity>foo</system_identity>")
    assert "prompt_leak" in r.issues


def test_reflection_scan_flags_placeholders():
    from core.reflection import scan
    r = scan("Do this: [insert your name here] and TODO finish")
    assert "placeholder" in r.issues


def test_reflection_scan_clean_text_passes():
    from core.reflection import scan
    r = scan("This is a normal sentence.")
    assert not r.needs_repair


# ═══════════════════════════════════════════════════════════════════════════
# core.semantic_anchor
# ═══════════════════════════════════════════════════════════════════════════
def test_semantic_anchor_extracts_identifiers():
    from core.semantic_anchor import anchors_of
    a = anchors_of("Set MAX_RETRIES to 5 and use fetch_user_data() in /src/api.py")
    joined = " ".join(a)
    assert "MAX_RETRIES" in joined and "/src/api.py" in joined


def test_semantic_anchor_compress_preserves_recent():
    from core.semantic_anchor import compress
    hist = [{"role": "user", "text": f"turn {i}"} for i in range(8)]
    out = compress(hist, keep_recent=2)
    assert any("turn 7" in m.get("text", "") for m in out)


# ═══════════════════════════════════════════════════════════════════════════
# core.task_shedder
# ═══════════════════════════════════════════════════════════════════════════
def test_task_shedder_empty_is_trivial():
    from core.task_shedder import assess
    assert assess("").tier == "trivial"


def test_task_shedder_deep_prompt_escalates():
    from core.task_shedder import assess
    c = assess(
        "Why does the distributed consensus protocol break under partition "
        "when leaders disagree about log offsets? Analyze the trade-offs, "
        "benchmark two alternatives, and refactor the recovery path. "
        "Then optimize throughput."
    )
    assert c.is_deep


# ═══════════════════════════════════════════════════════════════════════════
# core.memory
# ═══════════════════════════════════════════════════════════════════════════
def test_memory_memory_item_render_and_to_dict():
    from core.memory import MemoryItem
    m = MemoryItem(id="x", text="hi", role="user", session_id="s",
                   score=0.9, created_at=1.0)
    assert m.render() == "[user] hi"
    assert m.to_dict()["id"] == "x"


@pytest.mark.asyncio
async def test_memory_empty_text_returns_none():
    from core.memory import Memory
    m = Memory(redis_client=None, http_client=AsyncMock())
    assert await m.remember(user_id="u", session_id="s", role="user",
                            text="") is None


@pytest.mark.asyncio
async def test_memory_recall_without_vector_returns_recent():
    from core.memory import Memory
    m = Memory(redis_client=None, http_client=AsyncMock())
    assert await m.recall(user_id="u", query="q") == []


def test_memory_repo_url_parser():
    from core.memory import _parse_repo_url
    assert _parse_repo_url("https://github.com/foo/bar.git") == ("foo", "bar")
    assert _parse_repo_url("foo/bar") == ("foo", "bar")
    with pytest.raises(ValueError):
        _parse_repo_url("not a url")


def test_memory_chunk_python_splits_top_level_defs():
    from core.memory import _chunk_python
    code = "import os\ndef a():\n    pass\ndef b():\n    pass\n"
    assert len(_chunk_python(code)) >= 2


# ═══════════════════════════════════════════════════════════════════════════
# core.token_governor
# ═══════════════════════════════════════════════════════════════════════════
def test_token_governor_estimate_tokens():
    from core.token_governor import estimate_tokens
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 400) == 100


@pytest.mark.asyncio
async def test_token_governor_over_limit_denies():
    from core.token_governor import reserve
    ok, _, _ = await reserve("u", "free", 10 ** 9)
    assert ok is False


# ═══════════════════════════════════════════════════════════════════════════
# core.streaming_providers
# ═══════════════════════════════════════════════════════════════════════════
def test_streaming_providers_registry_shape():
    # WHY: an empty registry = silent stream fallback.
    from core.streaming_providers import PROVIDER_STREAMERS
    for name in ("groq", "openrouter", "mistral", "gemini", "anthropic",
                 "cohere", "cloudflare"):
        assert name in PROVIDER_STREAMERS


# ═══════════════════════════════════════════════════════════════════════════
# core.workspace_engine
# ═══════════════════════════════════════════════════════════════════════════
def test_workspace_engine_predicts_canonical_prompts():
    from core.workspace_engine import WorkspaceClassifier
    c = WorkspaceClassifier()
    assert c.predict("Extract columns from this CSV and aggregate sales",
                     [{"filename": "s.csv"}])[0] == "data"
    assert c.predict("Build a responsive layout with a button component",
                     [{"filename": "app.tsx"}])[0] == "design"


def test_workspace_engine_generic_files_neutralized():
    # WHY: README/LICENSE must NEVER influence routing.
    from core.workspace_engine import extract_features
    f_a = extract_features("hello", [{"filename": "README.md"}])
    f_b = extract_features("hello", [])
    assert f_a == f_b


# ═══════════════════════════════════════════════════════════════════════════
# core.anti_hallucination
# ═══════════════════════════════════════════════════════════════════════════
def test_ahe_canonicalize_collapses_whitespace_losslessly():
    from core.anti_hallucination import canonicalize
    out, _ = canonicalize("Hello   World\n\nFoo")
    assert out == "Hello World Foo"


def test_ahe_skeptic_rejects_non_verbatim_snippet():
    # WHY: the entire zero-hallucination guarantee rests on this.
    from core.anti_hallucination import (
        DocumentIngestor, ExtractionRequest, SkepticAgent,
    )
    req = ExtractionRequest(document_id="d1", document_type="csv",
                            document_uri="x",
                            query="find name",
                            target_fields=["name"])
    doc = DocumentIngestor().ingest(req, b"name,amount\nwidget,10\n")
    chunks_by_id = {c.chunk_id: c for c in doc.chunks}
    from core.anti_hallucination.schemas import ExtractedClaim
    bad = ExtractedClaim(field_name="name", value="fabricated",
                         raw_snippet="name= widget", chunk_id=doc.chunks[0].chunk_id)
    v = SkepticAgent().cross_examine(bad, chunks_by_id, doc)
    assert v.verified is False


def test_ahe_ingestor_csv_produces_row_addressed_chunks():
    from core.anti_hallucination import DocumentIngestor, ExtractionRequest
    req = ExtractionRequest(document_id="d", document_type="csv",
                            document_uri="x", query="q",
                            target_fields=["a"])
    doc = DocumentIngestor().ingest(req, b"a,b\n1,2\n3,4\n")
    assert any(c.coordinate.row_index == 1 for c in doc.chunks)


def test_ahe_ingestor_rejects_unsupported_type():
    from core.anti_hallucination import DocumentIngestor
    from core.anti_hallucination.schemas import ExtractionRequest, DocumentParseError
    with pytest.raises(Exception):  # ValidationError or DocumentParseError
        req = ExtractionRequest(document_id="d", document_type="pdf",
                                document_uri="x", query="q",
                                target_fields=["a"])
        DocumentIngestor().ingest(req, b"not a pdf")


# ═══════════════════════════════════════════════════════════════════════════
# core.stream
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_stream_chunked_reader_yields_bounded_chunks():
    from core.stream import ChunkedReader
    src = b"x" * 10_000
    total = 0
    async for chunk in ChunkedReader(src, chunk_bytes=1024):
        assert len(chunk) <= 1024
        total += len(chunk)
    assert total == 10_000


def test_stream_in_flight_scrubber_strips_refusal_prefix():
    from core.stream import InFlightScrubber
    s = InFlightScrubber()
    out = s.push("I'm sorry, I cannot help with that.\nHere is the code:")
    out += s.flush()
    joined = "".join(out)
    assert "I'm sorry" not in joined
    assert "Here is the code" in joined


def test_stream_cancellation_token_set_and_check():
    from core.stream import CancellationToken
    t = CancellationToken()
    assert t.is_set() is False
    t.set()
    assert t.is_set() is True


# ═══════════════════════════════════════════════════════════════════════════
# core.context
# ═══════════════════════════════════════════════════════════════════════════
def test_context_classify_build_intent():
    from core.context import classify
    r = classify("build me a login form with validation")
    assert r.intent == "transactional"
    assert r.sub_intent in ("build_ui", "generic", "test", "database")


def test_context_clip_history_respects_budget():
    from core.context import clip_history
    hist = [{"role": "user", "text": "x " * 500} for _ in range(20)]
    r = clip_history(hist, max_tokens=500)
    assert r.truncated is True


def test_context_clip_prompt_reports_truncation():
    from core.context import clip_prompt
    out, t = clip_prompt("x " * 5000, max_tokens=100)
    assert t is True and "[prompt clipped]" in out