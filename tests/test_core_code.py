"""
Unit tests for the core/ security + graph + heal + testloop + pr + worker modules.
==================================================================================
Every public symbol now lives in its own ``core.*`` submodule; this file
imports them all and exercises them.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.security import (
    CodeGuard,
    Finding,
    ScanResult,
    default_guard,
)
from core.dependency import (
    BlastRadius,
    DependencyTracker,
)
from core.healing import (
    HealResult,
    RouteFunc,
    SelfHealer,
    TouchFixEngine,
    make_diff,
)
from core.testloop import (
    ExecuteFunc,
    TestIteration,
    TestLoop,
    TestLoopResult,
)
from core.pr_shield import (
    PRShield,
    PRShieldInput,
)
from core.worker_client import (
    close_worker_client,
    execute_code_on_worker,
)


# ═══════════════════════════════════════════════════════════════════════════
# Section: CodeGuard
# ═══════════════════════════════════════════════════════════════════════════

def test_guard_detects_sql_injection():
    g = CodeGuard()
    code = 'cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")'
    result = g.scan(code, language="python")
    types = {f.type for f in result.findings}
    assert "sql_injection" in types


def test_guard_detects_xss_sink():
    g = CodeGuard()
    result = g.scan('el.innerHTML = userInput;', language="javascript")
    types = {f.type for f in result.findings}
    assert "xss_sink" in types


def test_guard_detects_eval():
    g = CodeGuard()
    result = g.scan('eval("1+1")', language="javascript")
    types = {f.type for f in result.findings}
    assert "eval_usage" in types


def test_guard_detects_command_injection():
    g = CodeGuard()
    code = 'os.system(f"ls {user_path}")'
    result = g.scan(code, language="python")
    assert any(f.type == "command_injection" for f in result.findings)


def test_guard_detects_aws_key():
    g = CodeGuard()
    code = 'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"'
    result = g.scan(code, language="python")
    assert any(f.type == "hardcoded_secret" for f in result.findings)


def test_guard_detects_private_key_block():
    g = CodeGuard()
    code = '-----BEGIN RSA PRIVATE KEY-----\nxxx\n-----END RSA PRIVATE KEY-----'
    result = g.scan(code, language="python")
    assert any(
        f.type == "hardcoded_secret" and "Private Key" in f.message
        for f in result.findings
    )


def test_guard_detects_python_syntax_error():
    g = CodeGuard()
    result = g.scan("def foo(\n", language="python")
    assert result.syntax_ok is False
    assert result.syntax_error is not None
    assert any(f.type == "syntax_error" for f in result.findings)


def test_guard_clean_code_scores_100():
    g = CodeGuard()
    result = g.scan("x = 1 + 1\nprint(x)\n", language="python")
    assert result.findings == []
    assert result.score == 100


def test_guard_never_raises_on_garbage():
    g = CodeGuard()
    result = g.scan("\x00\x01\x02 random bytes", language="python")
    assert isinstance(result, ScanResult)


def test_scan_result_to_dict_roundtrip():
    g = CodeGuard()
    result = g.scan('eval("x")')
    d = result.to_dict()
    assert isinstance(d["findings"], list)
    assert d["findings"][0]["type"] == "eval_usage"
    assert "score" in d


def test_finding_to_dict():
    f = Finding(type="xss_sink", severity="high", message="m", line=3)
    d = f.to_dict()
    assert d["type"] == "xss_sink"
    assert d["line"] == 3


def test_default_guard_singleton_is_codeguard():
    assert isinstance(default_guard, CodeGuard)


# ═══════════════════════════════════════════════════════════════════════════
# Section: DependencyTracker
# ═══════════════════════════════════════════════════════════════════════════

def test_dep_tracker_builds_empty_graph_for_empty_dir(tmp_path):
    t = DependencyTracker(tmp_path)
    t.build()
    assert t.to_dict() == {"graph": {}, "reverse": {}}


def test_dep_tracker_resolves_python_imports(tmp_path):
    (tmp_path / "a.py").write_text("from b import thing\n")
    (tmp_path / "b.py").write_text("thing = 1\n")
    t = DependencyTracker(tmp_path)
    t.build()
    deps = t.get_dependencies(tmp_path / "a.py")
    assert any(d.endswith("b.py") for d in deps)


def test_dep_tracker_reverse_lookup(tmp_path):
    (tmp_path / "a.py").write_text("from b import thing\n")
    (tmp_path / "b.py").write_text("thing = 1\n")
    t = DependencyTracker(tmp_path)
    t.build()
    dependents = t.get_dependents(tmp_path / "b.py")
    assert any(d.endswith("a.py") for d in dependents)


def test_dep_tracker_blast_radius_severity(tmp_path):
    # b is imported by 4 files → "medium" severity (>=3)
    (tmp_path / "b.py").write_text("x = 1\n")
    for i in range(4):
        (tmp_path / f"a{i}.py").write_text("from b import x\n")
    t = DependencyTracker(tmp_path)
    t.build()
    br = t.assess_impact(tmp_path / "b.py")
    assert isinstance(br, BlastRadius)
    assert br.count >= 4
    assert br.severity in ("medium", "high")


def test_dep_tracker_ast_parses_valid_python():
    imports = DependencyTracker._extract_python_imports_ast(
        "import os\nfrom sys import argv\n"
    )
    assert "os" in imports
    assert "sys" in imports


def test_dep_tracker_ast_returns_empty_on_syntax_error():
    imports = DependencyTracker._extract_python_imports_ast("def foo(\n")
    assert imports == []


def test_blast_radius_to_dict():
    br = BlastRadius(file="/x.py", dependents=["/y.py"], severity="low", count=1)
    d = br.to_dict()
    assert d["file"] == "/x.py"
    assert d["severity"] == "low"


# ═══════════════════════════════════════════════════════════════════════════
# Section: SelfHealer
# ═══════════════════════════════════════════════════════════════════════════

def _route_returning(text: str):
    async def _fn(**kwargs):
        return {"success": True, "text": text, "provider": "stub"}
    return _fn


@pytest.mark.asyncio
async def test_self_healer_success_path():
    fixed = '```python\nprint("fixed")\n```'
    healer = SelfHealer(route_func=_route_returning(fixed), max_retries=2)
    result = await healer.heal('print("broken")', "NameError", language="python")
    assert isinstance(result, HealResult)
    assert result.success is True
    assert "fixed" in result.final_code
    assert result.attempts == 1
    assert "original.py" in result.diff


@pytest.mark.asyncio
async def test_self_healer_rejects_unchanged_code():
    async def _stub(**kwargs):
        return {"success": True, "text": "```python\nprint(1)\n```"}
    healer = SelfHealer(route_func=_stub, max_retries=2)
    result = await healer.heal("print(1)", "noop", language="python")
    # Same content → treated as failure
    assert result.success is False


@pytest.mark.asyncio
async def test_self_healer_empty_code_shortcircuits():
    healer = SelfHealer(route_func=_route_returning("x"), max_retries=2)
    result = await healer.heal("", "err")
    assert result.success is False
    assert result.error == "empty code"


@pytest.mark.asyncio
async def test_self_healer_route_exception_is_caught():
    async def _raise(**kwargs):
        raise RuntimeError("boom")
    healer = SelfHealer(route_func=_raise, max_retries=1)
    result = await healer.heal("print(1)", "err")
    assert result.success is False
    assert "route error" in (result.error or "")


def test_self_healer_requires_route_func():
    with pytest.raises(ValueError):
        SelfHealer(route_func=None)


def test_heal_result_to_dict():
    r = HealResult(success=True, final_code="x", diff="d", attempts=2)
    d = r.to_dict()
    assert d["success"] is True
    assert d["attempts"] == 2


# ═══════════════════════════════════════════════════════════════════════════
# Section: TouchFixEngine
# ═══════════════════════════════════════════════════════════════════════════

def test_touchfix_apply_diff_basic():
    original = "line1\nline2\nline3\n"
    diff = (
        "--- original\n"
        "+++ fixed\n"
        "@@ -1,3 +1,3 @@\n"
        " line1\n"
        "-line2\n"
        "+LINE-TWO\n"
        " line3\n"
    )
    engine = TouchFixEngine(route_func=None)
    out = engine.apply_diff(original, diff)
    assert "LINE-TWO" in out
    assert "line2" not in out


def test_touchfix_apply_diff_returns_original_on_bad_input():
    engine = TouchFixEngine(route_func=None)
    assert engine.apply_diff("code", "") == "code"
    assert engine.apply_diff("", "diff") == ""
    assert engine.apply_diff("code", "garbage diff") == "code"


def test_touchfix_locate_block_exact_match():
    code = "def a():\n    pass\n\ndef b():\n    pass\n"
    block = "def b():\n    pass"
    start, end = TouchFixEngine._locate_block(code, block)
    assert start == 3
    assert end == 5


@pytest.mark.asyncio
async def test_touchfix_fix_block_no_route_returns_original():
    engine = TouchFixEngine(route_func=None)
    out = await engine.fix_block("def f():\n    pass\n", "def f():\n    pass", "err")
    assert "def f()" in out


# ═══════════════════════════════════════════════════════════════════════════
# Section: TestLoop
# ═══════════════════════════════════════════════════════════════════════════

async def _route_for_testloop(**kwargs):
    p = kwargs["prompt"]
    if "Generate" in p or "assert" in p:
        return {
            "success": True,
            "text": '```python\nprint("ALL TESTS PASSED")\n```',
        }
    return {"success": True, "text": "```python\nprint(1)\n```"}


@pytest.mark.asyncio
async def test_testloop_passes_on_first_iteration():
    async def _exec(lang, code, timeout):
        return {"success": True, "output": "ALL TESTS PASSED", "error": None}

    loop = TestLoop(_route_for_testloop, _exec, max_iterations=3)
    result = await loop.run("print(1)", language="python")
    assert isinstance(result, TestLoopResult)
    assert result.success is True
    assert len(result.iterations) == 1


@pytest.mark.asyncio
async def test_testloop_fails_when_sandbox_fails():
    async def _exec(lang, code, timeout):
        return {"success": False, "output": "", "error": "boom"}

    loop = TestLoop(_route_for_testloop, _exec, max_iterations=1)
    result = await loop.run("print(1)", language="python")
    assert result.success is False
    assert len(result.iterations) == 1


@pytest.mark.asyncio
async def test_testloop_empty_code_shortcircuits():
    async def _exec(lang, code, timeout):
        return {"success": True, "output": "ok"}

    loop = TestLoop(_route_for_testloop, _exec)
    result = await loop.run("", language="python")
    assert result.success is False
    assert result.error == "empty code"


def test_testloop_requires_both_callables():
    with pytest.raises(ValueError):
        TestLoop(route_func=None, execute_func=None)


def test_test_iteration_to_dict_truncates_output():
    it = TestIteration(iteration=1, tests="t", passed=True, output="x" * 5000)
    d = it.to_dict()
    assert len(d["output"]) == 2000


def test_testloop_result_to_dict():
    r = TestLoopResult(success=True, final_code="x", final_tests="t")
    d = r.to_dict()
    assert d["success"] is True
    assert d["iterations"] == []


# ═══════════════════════════════════════════════════════════════════════════
# Section: PRShield
# ═══════════════════════════════════════════════════════════════════════════

def test_prshield_renders_all_sections():
    inp = PRShieldInput(
        title="Fix login flow",
        author="alice",
        files_changed=["app.py"],
        blast_radius={"severity": "high", "count": 12,
                      "dependents": [f"mod{i}.py" for i in range(3)]},
        security_findings=[
            {"type": "xss_sink", "severity": "high",
             "line": 10, "message": "innerHTML"},
        ],
        self_heal={"success": True, "attempts": 2, "diff": "+fix"},
        test_results={"passed": False, "errors": [{"message": "assert failed"}]},
    )
    md = PRShield().render(inp)
    assert "# PR Defense Report" in md
    assert "Fix login flow" in md
    assert "## 1. Data Flow" in md
    assert "## 2. Validation" in md
    assert "## 3. Security" in md
    assert "## 4. Test Results" in md
    assert "## 5. Recommendations" in md
    assert "**Severity:** `HIGH`" in md


def test_prshield_handles_none_input():
    md = PRShield().render(None)
    assert "No data supplied" in md


def test_prshield_auto_recommends_on_high_blast():
    inp = PRShieldInput(
        blast_radius={"severity": "high", "count": 20, "dependents": []},
    )
    md = PRShield().render(inp)
    assert "high" in md.lower()


def test_prshield_input_to_dict_roundtrip():
    inp = PRShieldInput(title="x", files_changed=["a.py"])
    d = inp.to_dict()
    assert d["title"] == "x"
    assert d["files_changed"] == ["a.py"]


# ═══════════════════════════════════════════════════════════════════════════
# Section: worker_client
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_worker_client_empty_code_shortcircuits():
    r = await execute_code_on_worker("python", "")
    assert r["success"] is False
    assert r["error"] == "empty_code"


@pytest.mark.asyncio
async def test_worker_client_no_url_returns_unavailable(monkeypatch):
    import core.worker_client as code_mod
    monkeypatch.setattr(code_mod, "WORKER_URL", "")
    r = await execute_code_on_worker("python", "print(1)")
    assert r["success"] is False
    assert r["error"] == "worker_unavailable"


@pytest.mark.asyncio
async def test_worker_client_happy_path(monkeypatch):
    import core.worker_client as code_mod

    monkeypatch.setattr(code_mod, "WORKER_URL", "http://worker.test")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = lambda: {
        "success": True, "output": "hello\n", "error": "",
    }

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    monkeypatch.setattr(code_mod, "_HTTP", mock_client)

    r = await execute_code_on_worker("python", "print('hello')")
    assert r["success"] is True
    assert r["output"] == "hello\n"


@pytest.mark.asyncio
async def test_worker_client_client_error_not_retried(monkeypatch):
    import core.worker_client as code_mod

    monkeypatch.setattr(code_mod, "WORKER_URL", "http://worker.test")
    monkeypatch.setattr(code_mod, "MAX_RETRIES", 2)

    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = "bad"

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    monkeypatch.setattr(code_mod, "_HTTP", mock_client)

    r = await execute_code_on_worker("python", "print(1)")
    assert r["success"] is False
    assert "worker_client_error_400" in r["error"]
    assert mock_client.post.call_count == 1  # not retried


@pytest.mark.asyncio
async def test_worker_client_too_large_code(monkeypatch):
    import core.worker_client as code_mod
    monkeypatch.setattr(code_mod, "WORKER_URL", "http://x")
    big = "x" * (code_mod.MAX_CODE_BYTES + 10)
    r = await execute_code_on_worker("python", big)
    assert r["success"] is False
    assert "code_too_large" in r["error"]


# ═══════════════════════════════════════════════════════════════════════════
# Section: make_diff helper
# ═══════════════════════════════════════════════════════════════════════════

def test_make_diff_produces_unified_format():
    d = make_diff("a\nb\n", "a\nB\n", label="py")
    assert "--- original.py" in d
    assert "+++ fixed.py" in d
    assert "-b" in d
    assert "+B" in d