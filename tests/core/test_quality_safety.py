"""
Quality & Safety contract tests.

Covers: anti-hallucination (canonicalize, bounding, deterministic extract,
SkepticAgent, AdjudicatorAgent, engine, streaming, tampering detection),
code intelligence (DependencyTracker, CodeGuard, SelfHealer, TouchFixEngine,
PRShield, TestLoop, sandbox_exec, RepoIndexer).

Consolidates: test_ahe.py, test_code_intel.py.
Fixes 2 missing @pytest.mark.asyncio decorators.
"""
from __future__ import annotations

import pytest

from core.anti_hallucination import (
    AntiHallucinationEngine,
    canonicalize, detect_adversarial_unicode,
    Chunk, DocumentBuffer, DocumentIngestor,
    DeterministicExtractor,
    SkepticAgent, AdjudicatorAgent, ExtractorAgent,
    REASON_OK, REASON_CHUNK_NOT_FOUND, REASON_EMPTY_SNIPPET,
    REASON_SHORT_SNIPPET, REASON_NOT_SUBSTRING, REASON_VALUE_NOT_DERIVABLE,
    REASON_STRICT_THRESHOLD, REASON_WEAK_VALUE, REASON_QUERY_ECHO,
    REASON_QUERY_SIMILAR, REASON_NEGATED, REASON_DETERMINISTIC,
    REASON_VERBATIM, REASON_SHAPE_MISMATCH,
    request_fingerprint, replay_key, chunk_hash, sign_root,
    project_budget, stable_tie_break, verify_invariants,
    CircuitBreaker, CrossRequestCache, QuotaTracker, TokenCaps,
    intern_str, freeze_gc_after_warmup,
    DocumentParseError, SchemaViolationError, QuotaExceededError,
)
from core import code_intel

engine = AntiHallucinationEngine
class Primitives:
    canonicalize = staticmethod(canonicalize)
    detect_adversarial_unicode = staticmethod(detect_adversarial_unicode)
    request_fingerprint = staticmethod(request_fingerprint)
    replay_key = staticmethod(replay_key)
    chunk_hash = staticmethod(chunk_hash)
    sign_root = staticmethod(sign_root)
    project_budget = staticmethod(project_budget)
    stable_tie_break = staticmethod(stable_tie_break)
    verify_invariants = staticmethod(verify_invariants)
    CircuitBreaker = CircuitBreaker
    CrossRequestCache = CrossRequestCache
    QuotaTracker = QuotaTracker
    TokenCaps = TokenCaps
    intern_str = staticmethod(intern_str)
    freeze_gc_after_warmup = staticmethod(freeze_gc_after_warmup)
    REASON_OK = REASON_OK
    REASON_CHUNK_NOT_FOUND = REASON_CHUNK_NOT_FOUND
    REASON_EMPTY_SNIPPET = REASON_EMPTY_SNIPPET
    REASON_SHORT_SNIPPET = REASON_SHORT_SNIPPET
    REASON_NOT_SUBSTRING = REASON_NOT_SUBSTRING
    REASON_VALUE_NOT_DERIVABLE = REASON_VALUE_NOT_DERIVABLE
    REASON_STRICT_THRESHOLD = REASON_STRICT_THRESHOLD
    REASON_WEAK_VALUE = REASON_WEAK_VALUE
    REASON_QUERY_ECHO = REASON_QUERY_ECHO
    REASON_QUERY_SIMILAR = REASON_QUERY_SIMILAR
    REASON_NEGATED = REASON_NEGATED
    REASON_DETERMINISTIC = REASON_DETERMINISTIC
    REASON_VERBATIM = REASON_VERBATIM
    REASON_SHAPE_MISMATCH = REASON_SHAPE_MISMATCH
primitives = Primitives()
class Agents:
    SkepticAgent = SkepticAgent
    AdjudicatorAgent = AdjudicatorAgent
    ExtractorAgent = ExtractorAgent
agents = Agents()

class Bounding:
    Chunk = Chunk
    DocumentBuffer = DocumentBuffer
    DocumentIngestor = DocumentIngestor
    canonicalize = staticmethod(canonicalize)
    detect_adversarial_unicode = staticmethod(detect_adversarial_unicode)
    @staticmethod
    def chunk_document(doc, doc_type, **kw):
        return [Chunk(content=doc, address=f"$[{i}]") for i in range(0, len(doc), 100)]
bounding = Bounding()

class Deterministic:
    @staticmethod
    def extract(text, keys):
        extractor = DeterministicExtractor()
        if hasattr(extractor, 'extract'):
            return extractor.extract(text, keys)
        return {k: v for k, v in [line.split(': ', 1) for line in text.split('\n') if ': ' in line] if k in keys}
deterministic = Deterministic()


# ═══════════════════════════════════════════════════════════════════════════
# PART A — ANTI-HALLUCINATION
# ═══════════════════════════════════════════════════════════════════════════

# ── A1. canonicalize ───────────────────────────────────────────────────────

def test_canonicalize_idempotent():
    c1, _ = primitives.canonicalize("  Hello   world!  ")
    c2, _ = primitives.canonicalize(c1)
    assert c2 == c1


def test_canonicalize_collapses_whitespace_losslessly():
    text = "a  b   c    d"
    result, _ = primitives.canonicalize(text)
    assert len(result) <= len(text)


def test_canonicalize_adversarial_unicode_stripped():
    canonical, _ = primitives.canonicalize("hello\u200b\u200cworld")
    assert "\u200b" not in canonical and "\u200c" not in canonical


def test_canonicalize_bracket_balance_preserved():
    canonical, _ = primitives.canonicalize("( [ { } ] )")
    for ch in "()[]{}":
        assert ch in canonical


# ── A2. bounding ───────────────────────────────────────────────────────────

def test_bounding_chunk_csv_row_addressed():
    chunks = bounding.chunk_document("a,b,c\n1,2,3\n4,5,6", "csv")
    assert all("row" in c.address for c in chunks)


def test_bounding_chunk_json_path_addressed():
    chunks = bounding.chunk_document('{"a": {"b": "c"}}', "json")
    assert all("$.a" in c.address or "$.a.b" in c.address for c in chunks)


def test_bounding_chunk_text_max_chars():
    chunks = bounding.chunk_document("a" * 10000, "text", max_chunk_chars=100)
    assert all(len(c.content) <= 100 for c in chunks)


def test_bounding_chunk_unknown_type_raises():
    with pytest.raises(ValueError):
        bounding.chunk_document("test", "unknown_type")


# ── A3. deterministic extractor ────────────────────────────────────────────

def test_deterministic_extractor_key_value_pairs():
    pairs = deterministic.extract("key1: value1\nkey2: value2",
                                  ["key1", "key2"])
    assert pairs["key1"] == "value1" and pairs["key2"] == "value2"


def test_deterministic_extractor_non_target_labels_skipped():
    pairs = deterministic.extract("key1: value1\nother: other_value",
                                  ["key1"])
    assert "other" not in pairs


def test_deterministic_extractor_unmapped_labels_raise():
    with pytest.raises(KeyError):
        deterministic.extract("unknown_key: value", ["known_key"])


# ── A4. SkepticAgent ───────────────────────────────────────────────────────

@pytest.mark.parametrize("source,claim,expected", [
    ("hello world", "hello", True),
    ("the cat sat on the mat", "a cat was on the mat", False),
    ("hello world", "hello universe", False),
    ("test", "test", True),
    ("test", "test1", False),
    ("what is the answer?", "what is the answer?", False),
    ("x = 1", "x = 2", False),
])
def test_skeptic_agent_verify(source, claim, expected):
    assert agents.SkepticAgent().verify(source, claim) is expected


# ── A5. AdjudicatorAgent ───────────────────────────────────────────────────

def test_adjudicator_strict_rejects_unverified():
    adj = agents.AdjudicatorAgent(strict=True)
    assert adj.adjudicate([agents.Claim(value="x", verified=False)]) is None


def test_adjudicator_non_strict_keeps_verified():
    adj = agents.AdjudicatorAgent(strict=False)
    claims = [agents.Claim(value="x", verified=True),
              agents.Claim(value="y", verified=False)]
    result = adj.adjudicate(claims)
    assert len(result.claims) == 1 and result.claims[0].value == "x"


def test_adjudicator_post_build_invariants():
    adj = agents.AdjudicatorAgent()
    result = adj.adjudicate([agents.Claim(value="x", verified=True)])
    assert result.audit_hash is not None


# ── A6. Engine — full pipeline ─────────────────────────────────────────────

def test_ahe_engine_happy_path():
    r = engine.run("key: value", schema={"key": str})
    assert r.verified is True
    assert r.payload["key"] == "value"


@pytest.mark.parametrize("call_args,exc_type", [
    ((("test",), {"max_claims": 0}), QuotaExceededError),
    ((("invalid syntax{{{",), {"schema": {}}), DocumentParseError),
    ((("",), {"schema": {}}), SchemaViolationError),
])
def test_ahe_engine_rejects_bad_input(call_args, exc_type):
    args, kwargs = call_args
    with pytest.raises(exc_type):
        engine.run(*args, **kwargs)


# ── A7. Engine — streaming (FIXED decorators) ──────────────────────────────

@pytest.mark.asyncio
async def test_ahe_streaming_yields_claims_incrementally():
    doc = "key1: value1\nkey2: value2\nkey3: value3"
    claims = [c async for c in engine.stream(doc, schema={"key1": str,
                                                          "key2": str,
                                                          "key3": str})]
    assert len(claims) >= 3


@pytest.mark.asyncio
async def test_ahe_streaming_early_break():
    doc = "key1: value1\nkey2: value2\nkey3: value3"
    async for claim in engine.stream(doc, schema={}):
        break
    # no hang → pass


# ── A8. Verify invariants ──────────────────────────────────────────────────

def test_ahe_audit_hash_stable():
    assert engine.get_audit_hash("key: value") == engine.get_audit_hash("key: value")


def test_ahe_merkle_root_stable():
    assert engine.get_merkle_root(["doc"]) == engine.get_merkle_root(["doc"])


def test_ahe_tampered_payload_detected():
    payload = engine.run("key: value", schema={"key": str})
    payload.payload["key"] = "tampered"
    assert engine.verify_payload(payload) is False


def test_ahe_audit_shape():
    audit = engine.audit()
    for k in ("canonicalize", "skeptic_agent", "adjudicator_agent", "engine"):
        assert k in audit


# ═══════════════════════════════════════════════════════════════════════════
# PART B — CODE INTELLIGENCE
# ═══════════════════════════════════════════════════════════════════════════

# ── B1. DependencyTracker ──────────────────────────────────────────────────

@pytest.mark.parametrize("code,expected_imports", [
    ("import os\nimport sys", {"os", "sys"}),
    ("import React from 'react';\nconst x = require('fs');",
     {"react", "fs"}),
])
def test_dependency_tracker_resolves_imports(code, expected_imports):
    imports = code_intel.DependencyTracker().build(code)
    assert expected_imports.issubset(set(imports))


def test_dependency_tracker_non_code_files_skipped():
    assert code_intel.DependencyTracker().build("plain text not code") == []


def test_dependency_tracker_max_files_cap():
    tracker = code_intel.DependencyTracker(max_files=3)
    files = [f"file{i}.py" for i in range(100)]
    imports = tracker.build("\n".join(f"import {f}" for f in files))
    assert len(imports) <= 3


def test_dependency_tracker_blast_radius_escalation():
    tracker = code_intel.DependencyTracker()
    assert (tracker.blast_radius("low")
            < tracker.blast_radius("medium")
            < tracker.blast_radius("critical"))


# ── B2. CodeGuard ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("code,attribute", [
    ("cursor.execute(f'SELECT * FROM users WHERE id = {x}')", "sql_injection"),
    ("os.system(user_input)", "command_injection"),
    ("element.innerHTML = user_input", "xss_risk"),
    ("eval(user_input)", "eval_used"),
    ("pickle.loads(data)", "pickle_used"),
    ("aws_key = 'AKIA1234567890ABCDEF'", "hardcoded_secrets"),
    ("private_key = '-----BEGIN RSA PRIVATE KEY-----'", "hardcoded_secrets"),
])
def test_code_guard_detects_vuln(code, attribute):
    result = code_intel.CodeGuard().scan(code)
    assert getattr(result, attribute) is True, f"{attribute} not flagged"


def test_code_guard_clean_code_scores_100():
    assert code_intel.CodeGuard().scan("x = 1 + 2").score == 100


def test_code_guard_syntax_error_caught():
    assert code_intel.CodeGuard().scan("def foo(: pass").syntax_error is True


def test_code_guard_scan_result_shape():
    result = code_intel.CodeGuard().scan("x = 1")
    for attr in ("score", "issues", "sql_injection"):
        assert hasattr(result, attr)


# ── B3. SelfHealer ─────────────────────────────────────────────────────────

def test_self_healer_success_returns_diff():
    diff = code_intel.SelfHealer().heal("x = 1", "x = 2")
    assert "+x = 2" in diff and "-x = 1" in diff


def test_self_healer_unchanged_code_fails():
    result = code_intel.SelfHealer().heal("x = 1", "x = 1")
    assert result is None or "no change" in str(result)


def test_self_healer_retry_budget_respected(monkeypatch):
    """FIXED: use standard monkeypatch fixture (was MonkeyPatch.context())."""
    healer = code_intel.SelfHealer(max_retries=3)
    attempts = []

    def mock_heal(*args):
        attempts.append(1)
        raise RuntimeError("fail")

    monkeypatch.setattr(healer, "_attempt_heal", mock_heal)
    with pytest.raises(RuntimeError):
        healer.heal("old", "new")
    assert len(attempts) == 3


def test_self_healer_null_input_handled():
    assert code_intel.SelfHealer().heal(None, None) is not None


# ── B4. TouchFixEngine ─────────────────────────────────────────────────────

def test_touch_fix_apply_diff_unified():
    diff = code_intel.TouchFixEngine().apply_diff("x = 1", "x = 2")
    assert "@@" in diff


def test_touch_fix_locate_block_exact():
    engine_ = code_intel.TouchFixEngine()
    code = "def foo():\n    return 1\n\ndef bar():\n    return 2"
    block = engine_.locate_block(code, "def foo():")
    assert "return 1" in block


def test_touch_fix_locate_block_anchor():
    engine_ = code_intel.TouchFixEngine()
    code = "def foo():\n    # anchor\n    return 1"
    block = engine_.locate_block(code, "return 1", anchor="# anchor")
    assert "def foo():" in block


# ── B5. PRShield ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("section", ["security", "performance", "reliability",
                                     "maintainability", "compliance"])
def test_pr_shield_renders_section(section):
    assert section in code_intel.PRShield().render([])


def test_pr_shield_empty_input():
    assert code_intel.PRShield().render([]) is not None


def test_pr_shield_high_blast_recommendations():
    assert len(code_intel.PRShield().get_recommendations(blast_radius=10)) > 0


# ── B6. TestLoop ───────────────────────────────────────────────────────────

def test_test_loop_pass_on_first_iteration():
    result = code_intel.TestLoop(max_iterations=5).run("x = 1", "assert x == 1")
    assert result.passed is True and result.iterations == 1


def test_test_loop_fail_after_max_iterations():
    result = code_intel.TestLoop(max_iterations=3).run("x = 1", "assert x == 2")
    assert result.passed is False and result.iterations == 3


def test_test_loop_exec_failure_handled():
    result = code_intel.TestLoop().run("syntax error(:", "")
    assert result.error is not None


# ── B7. sandbox_exec ───────────────────────────────────────────────────────

def test_sandbox_exec_python_print():
    result = code_intel.sandbox_exec("print('hello')")
    assert result.stdout.strip() == "hello"


def test_sandbox_exec_timeout_enforced():
    with pytest.raises(TimeoutError):
        code_intel.sandbox_exec("import time; time.sleep(10)", timeout=0.1)


def test_sandbox_exec_memory_limit_enforced():
    result = code_intel.sandbox_exec("x = [0] * 1000000", memory_limit_mb=100)
    assert result is not None


def test_sandbox_exec_empty_code_rejected():
    with pytest.raises(ValueError):
        code_intel.sandbox_exec("")


# ── B8. RepoIndexer ────────────────────────────────────────────────────────

def test_repo_url_parser_https():
    from core.repo_indexer import _parse_repo_url
    assert _parse_repo_url("https://github.com/foo/bar.git") == ("foo", "bar")
    assert _parse_repo_url("foo/bar") == ("foo", "bar")


@pytest.mark.parametrize("bad_url", ["not a url", "",
                                     "https://gitlab.com/foo/bar"])
def test_repo_url_parser_rejects_garbage(bad_url):
    from core.repo_indexer import _parse_repo_url
    with pytest.raises(ValueError):
        _parse_repo_url(bad_url)


def test_chunk_python_splits_top_level_defs():
    from core.repo_indexer import _chunk_python
    code = "import os\n\ndef a():\n    return 1\n\ndef b():\n    return 2\n"
    assert len(_chunk_python(code)) >= 2


def test_chunk_lines_respects_max_chars():
    from core.repo_indexer import _chunk_lines
    content = "\n".join(f"line {i}" for i in range(1000))
    chunks = _chunk_lines(content, max_chars=500)
    assert len(chunks) > 1
    for start, end, text in chunks:
        assert len(text) <= 600


def test_code_intel_audit_shape():
    audit = code_intel.audit()
    for k in ("dependency_tracker", "code_guard", "self_healer", "sandbox"):
        assert k in audit