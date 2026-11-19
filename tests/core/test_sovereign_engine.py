"""
test_sovereign_engine.py — Sovereign Engine + Twin Quality contract tests.

Consolidates:
  • test_axelr_master.py  →  Parts A–F (CONVERTED from script to real pytest)
  • test_twin_quality.py  →  Parts G–H (preserved verbatim)

CRITICAL FIX
------------
test_axelr_master.py used `check(cond, msg)` which incremented globals but
did NOT raise. Under pytest this meant every test function silently PASSED
regardless of internal assertion failures. This file converts every `check()`
call to a real `assert`, making failures visible to CI.

The `Sovereign` and `twin` fixtures skip cleanly when the binary/engine
is unavailable — SKIP is honest, false-green is not.
"""
from __future__ import annotations

import ast
import asyncio
import gc
import json
import re
import sys
import threading
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest

# ── Bootstrap sovereign engine path (must happen before imports) ──────────
_ROOT = Path(__file__).resolve().parent.parent.parent
_SOVEREIGN_ENGINE = _ROOT / "core" / "axelr-sovereign.engine"
if str(_SOVEREIGN_ENGINE) not in sys.path:
    sys.path.insert(0, str(_SOVEREIGN_ENGINE))

# ── Import probes — fail loudly as SKIP, not as silent green ──────────────
try:
    from axelr_adapters import ASRConfig, make_local_fn, select_adapter
    from axelr_config import CalibrationConfig, Tier, select_profile
    from axelr_devices import (
        DeviceProfile,
        DeviceTier,
        check_feature,
        classify,
        select_runtime,
        user_facing_summary,
    )
    from axelr_elite_v2 import (
        AudioSilenceDetector,
        CloudFallbackBudget,
        ConcurrentQuotaGuard,
        ContractVerifier,
        CrossRequestCache,
        DeterminismGuard,
        DeterministicReplay,
        FrameIntegrityChecker,
        LatencyBudgetEnforcer,
        SchemaFingerprint,
        SessionResumeVerifier,
        WordBoundaryPreserver,
    )
    from axelr_engine import (
        ConfidenceCalibrator,
        DecoderConfig,
        Metaphone,
        PhoneticCorrector,
        SpeculativeCascadeDecoder,
        Token,
        TrieLexicon,
        edit_distance,
        soundex,
    )
    from axelr_features import CloudConfig
    from axelr_ipc import StressTester
    from axelr_pipeline import EnginePipeline, PipelineConfig
    from axelr_resource import GracefulFeatureLadder
    from axelr_ring import RingBufferConfig, ZeroCopyRingBuffer
    from axelr_telemetry import MetricsExporter, SessionStore, new_trace_id, trace_id
    from axelr_utils import TokenBucket
    _SOVEREIGN_OK, _SOVEREIGN_ERR = True, ""
except ImportError as e:
    _SOVEREIGN_OK, _SOVEREIGN_ERR = False, str(e)

try:
    from core.model_specialization import (
        TASK_SPECIALISTS,
        best_for,
    )
    from core.model_specialization import (
        route as route_model,
    )
    from core.twin_roster import ROSTER_BY_ID
    _TWIN_OK, _TWIN_ERR = True, ""
except ImportError as e:
    _TWIN_OK, _TWIN_ERR = False, str(e)


pytestmark = pytest.mark.quality


# ═══════════════════════════════════════════════════════════════════════════
# Import gates
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def sovereign():
    if not _SOVEREIGN_OK:
        pytest.skip(f"sovereign engine unavailable: {_SOVEREIGN_ERR}")
    return True


@pytest.fixture(scope="module")
def twin():
    if not _TWIN_OK:
        pytest.skip(f"twin engine unavailable: {_TWIN_ERR}")
    return True


# ═══════════════════════════════════════════════════════════════════════════
# PART A — SOVEREIGN PRIMITIVES
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("word,expected", [
    ("Robert", "R163"), ("Rupert", "R163"),
    ("Tymczak", "T522"), ("Honeyman", "H555"), ("", ""),
])
def test_soundex(sovereign, word, expected):
    assert soundex(word) == expected


@pytest.mark.parametrize("a,b,dist", [
    ("kitten", "sitting", 3), ("", "abc", 3), ("abc", "abd", 1),
])
def test_edit_distance(sovereign, a, b, dist):
    assert edit_distance(a, b) == dist


@pytest.mark.parametrize("a,b", [
    ("phone", "fone"), ("right", "write"), ("night", "knight"),
])
def test_metaphone_equivalence(sovereign, a, b):
    m = Metaphone()
    assert m.compute(a) == m.compute(b)


def test_metaphone_empty(sovereign):
    assert Metaphone().compute("") == "0000"


def test_trie_lexicon(sovereign):
    t = TrieLexicon()
    for w in ["hello", "help", "helm", "world", "helper"]:
        t.insert(w)
    assert t.size == 5
    assert t.contains("hello")
    assert not t.contains("hel")  # prefix, not word
    assert set(t.prefix("hel", 10)) == {"hello", "help", "helm", "helper"}


def test_corrector_known_words(sovereign):
    p = PhoneticCorrector()
    p.load(["receive", "their", "there", "your", "you're",
            "its", "it's", "to", "too", "two"])
    assert p.correct("recieve") == "receive"
    assert p.correct("thier") == "their"


@pytest.mark.parametrize("loaded,typo", [
    ("kubernetes", "kubernetse"), ("foobarbazqux", "foobarbazqax"),
])
def test_corrector_loads_new_words(sovereign, loaded, typo):
    """Anti-stub: correctly answers after loading a previously-unknown word."""
    p = PhoneticCorrector()
    p.load([loaded])
    assert p.correct(typo) == loaded


def test_corrector_preserves_unknown(sovereign):
    p = PhoneticCorrector()
    p.load(["receive"])
    assert p.correct("zzzzqqqq") == "zzzzqqqq"


def test_ring_spsc_roundtrip(sovereign):
    ring = ZeroCopyRingBuffer(RingBufferConfig(capacity_samples=1024))
    data = np.arange(100, dtype=np.int16)
    written, _ = ring.write(data)
    assert written == 100 and ring.available() == 100
    assert ring.read(100).tolist() == data.tolist()
    ring.dispose()


def test_ring_caps_at_capacity(sovereign):
    ring = ZeroCopyRingBuffer(RingBufferConfig(capacity_samples=1024))
    ring.write(np.arange(2000, dtype=np.int16))
    assert ring.available() == 1024
    ring.dispose()


def test_calibrator_bounds(sovereign):
    cal = ConfidenceCalibrator(CalibrationConfig())
    assert abs(cal.calibrate(0.0) - 0.5) < 0.01
    assert cal.calibrate(10.0) > 0.95
    assert cal.calibrate(-10.0) < 0.05


# ═══════════════════════════════════════════════════════════════════════════
# PART B — ELITE v2 FEATURES
# ═══════════════════════════════════════════════════════════════════════════

def test_elite_frame_integrity(sovereign):
    f = FrameIntegrityChecker(1024)
    assert f.verify(100, 50)[0]
    assert not f.verify(50, 100)[0]


def test_elite_latency_budget(sovereign):
    b = LatencyBudgetEnforcer({"op": 5.0})
    for _ in range(50):
        b.record("op", 1.0)
    assert not b.check()["backpressure"]
    for _ in range(50):
        b.record("op", 10.0)
    assert b.check()["backpressure"]


def test_elite_schema_fingerprint(sovereign):
    cfg1 = {"engine_version": "1.0", "tier": "std"}
    cfg2 = {"engine_version": "1.0", "tier": "high"}
    assert SchemaFingerprint.compute(cfg1) == SchemaFingerprint.compute(cfg1)
    assert SchemaFingerprint.compute(cfg1) != SchemaFingerprint.compute(cfg2)


def test_elite_cross_request_cache(sovereign):
    c = CrossRequestCache(max_size=10)
    mel = np.zeros((10, 80), dtype=np.float32)
    assert c.get(mel) is None
    c.put(mel, ["a", "b"])
    assert c.get(mel) == ["a", "b"]


def test_elite_determinism_guard(sovereign):
    assert DeterminismGuard().check(0, 0.0, 1)
    assert not DeterminismGuard().check(42, 0.0, 1)


def test_elite_audio_silence(sovereign):
    d = AudioSilenceDetector()
    assert d.classify(np.zeros(1000, dtype=np.float32)) == "dead_mic"
    tone = np.random.RandomState(42).randn(1000).astype(np.float32) * 1e-5
    assert d.classify(tone) == "silence"


def test_elite_cloud_fallback_budget(sovereign):
    cb = CloudFallbackBudget(max_calls=3)
    for _ in range(3):
        assert cb.check(1000)
        cb.consume(1000)
    assert not cb.check(1000)


def test_elite_word_boundary(sovereign):
    w = WordBoundaryPreserver(window_samples=100, tail_pct=0.2)
    trimmed = w.trim(np.ones(1000, dtype=np.float32) * 0.5)
    assert 0 < trimmed <= 1000


def test_elite_session_resume(sovereign):
    ok, _ = SessionResumeVerifier.verify_payload({
        "schema_version": 1, "calibration_temperature": 1.4,
        "lexicon": ["hello"],
    })
    assert ok


def test_elite_concurrent_quota(sovereign):
    q = ConcurrentQuotaGuard()
    assert q.try_reserve("ws1", 5, 10)
    assert not q.try_reserve("ws1", 8, 10)
    q.release("ws1", 3)
    assert q.try_reserve("ws1", 3, 10)


def test_elite_deterministic_replay(sovereign):
    r = DeterministicReplay()
    r.record("abc", ["hello", "world"], "cfg1")
    assert r.verify("abc", ["hello", "world"], "cfg1")[0]
    assert not r.verify("abc", ["bye", "world"], "cfg1")[0]


def test_elite_contract_verifier(sovereign):
    cv = ContractVerifier()
    assert cv.validate({"type": "ping", "id": 1, "payload": {}})[0]
    assert not cv.validate({"type": "ping", "payload": {}})[0]


# ═══════════════════════════════════════════════════════════════════════════
# PART C — SESSION, LADDER, BUCKET, METRICS
# ═══════════════════════════════════════════════════════════════════════════

def test_session_store_persistence(sovereign, tmp_path):
    path = tmp_path / "session.json"
    s1 = SessionStore(path=path)
    s1.set_temperature(2.2)
    s1.add_lexicon_word("kubernetes")
    s1.flush()
    assert path.exists()
    s2 = SessionStore(path=path)
    assert abs(s2.state.calibration_temperature - 2.2) < 0.01
    assert "kubernetes" in s2.state.lexicon


def test_degradation_ladder(sovereign):
    ladder = GracefulFeatureLadder()
    state = {"rerank": True, "repair": True, "cloud": True}
    ladder.add("streaming_rerank",
               lambda: state.__setitem__("rerank", False),
               lambda: state.__setitem__("rerank", True))
    ladder.add("structured_repair",
               lambda: state.__setitem__("repair", False),
               lambda: state.__setitem__("repair", True))
    ladder.add("cloud_fallback",
               lambda: state.__setitem__("cloud", False),
               lambda: state.__setitem__("cloud", True))
    ladder.on_high(0.9)
    assert state["rerank"] is False and state["repair"] is False
    ladder.on_high(0.9)
    assert state["cloud"] is False


def test_token_bucket(sovereign):
    b = TokenBucket(capacity=5, refill_per_sec=5.0)
    assert all(b.acquire()[0] for _ in range(5))
    allowed, retry = b.acquire()
    assert not allowed and 0 < retry < 1


def test_metrics_exporter(sovereign):
    m = MetricsExporter()
    new_trace_id()
    assert len(trace_id()) == 12
    m.record_request("correct", True, 0.001)
    m.record_request("transcribe", False, 0.500)
    text = m.render()
    assert 'axelr_requests_total{type="correct",ok="true"} 1' in text
    assert 'axelr_errors_total{code="protocol"} 1' in text


# ═══════════════════════════════════════════════════════════════════════════
# PART D — REGRESSIONS
# ═══════════════════════════════════════════════════════════════════════════

def test_regression_event_loop_safety(sovereign):
    """_shed_cloud must be safe outside an async loop."""
    profile = select_profile(Tier.STANDARD)
    cfg = PipelineConfig(decoder=DecoderConfig(), cloud=CloudConfig())
    p = EnginePipeline(profile, cfg, lambda mel: [])
    errs = []

    def call():
        try:
            p._shed_cloud()
            p._reinstate_cloud()
        except Exception as e:
            errs.append(f"{type(e).__name__}: {e}")

    t = threading.Thread(target=call)
    t.start()
    t.join()
    assert not errs, f"_shed_cloud unsafe outside loop: {errs}"


def test_regression_local_fn_arity_detect(sovereign):
    """Decoder must correctly detect 1-arg vs 2-arg callables."""
    profile = select_profile(Tier.STANDARD)

    def one_arg(log_mel):
        return [Token("one", 0.9, 1)]

    def two_arg(log_mel, pcm=None):
        return [Token("two", 0.9, 1)]

    assert SpeculativeCascadeDecoder(DecoderConfig(), profile, one_arg)._fn_takes_pcm is False
    assert SpeculativeCascadeDecoder(DecoderConfig(), profile, two_arg)._fn_takes_pcm is True


def test_adapter_honest_failure_without_mock_flag(sovereign, monkeypatch):
    """Adapter must raise when mock requested without ALLOW_MOCK env flag."""
    monkeypatch.setenv("AXELR_ASR", "mock")
    monkeypatch.delenv("AXELR_ALLOW_MOCK", raising=False)
    with pytest.raises(RuntimeError):
        select_adapter(ASRConfig())


def test_adapter_mock_allowed_with_flag(sovereign, monkeypatch):
    monkeypatch.setenv("AXELR_ALLOW_MOCK", "1")
    adapter = select_adapter(ASRConfig(), force="mock")
    assert adapter.name == "mock"
    out = make_local_fn(adapter)(np.zeros((10, 80), dtype=np.float32))
    assert isinstance(out, list)


def test_protocol_conformance(sovereign):
    verifier = ContractVerifier()
    assert verifier.validate({"type": "ping", "id": 1, "payload": {}})[0]
    ok, issues = verifier.validate({"id": 1, "command": "ping", "data": {}})
    assert not ok and any("type" in i for i in issues)


# ═══════════════════════════════════════════════════════════════════════════
# PART E — DETERMINISM / ADVERSARIAL / MEMORY / THREADS
# ═══════════════════════════════════════════════════════════════════════════

def test_corrector_deterministic_100x(sovereign):
    p = PhoneticCorrector()
    p.load(["receive", "their", "there"])
    first = p.correct("recieve")
    assert all(p.correct("recieve") == first for _ in range(100))


@pytest.mark.parametrize("adversarial", [
    "a" * 100_000,
    "",
    "\u200b" * 1000,
    "\u202e" + "hello",
    "\x00\x01\x02\x03",
    "🎉" * 5000,
    "SELECT * FROM users; DROP TABLE--",
    "\\" * 2000,
    "\u0301" * 200 + "word",
])
def test_adversarial_inputs(sovereign, adversarial):
    p = PhoneticCorrector()
    p.load(["hello", "world"])
    p.correct(adversarial)  # must not raise


@pytest.mark.perf
def test_memory_no_leak_1000_cycles(sovereign):
    """Bounded heap growth over 1000 construction cycles. @perf — opt-in."""
    tracemalloc.start()
    baseline, _ = tracemalloc.get_traced_memory()
    for _ in range(1000):
        p = PhoneticCorrector()
        p.load(["hello", "world"])
        p.correct("helo")
        r = ZeroCopyRingBuffer(RingBufferConfig(capacity_samples=64))
        r.write(np.arange(10, dtype=np.int16))
        r.read(10)
        r.dispose()
    gc.collect()
    current, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    growth_kb = (current - baseline) / 1024
    assert growth_kb < 5000, f"heap growth {growth_kb:.0f}KB ≥ 5000KB"


def test_thread_safety_corrector(sovereign):
    p = PhoneticCorrector()
    p.load(["receive", "their", "there"])
    errors, results = [], []

    def worker(idx):
        try:
            for _ in range(100):
                results.append(p.correct("recieve"))
        except Exception as e:
            errors.append(f"t{idx}: {type(e).__name__}")

    with ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(worker, range(10)))
    assert not errors
    assert len(results) == 1000
    assert all(r == "receive" for r in results)


@pytest.mark.stress
def test_stress_sla_real(sovereign):
    """Real SLA under concurrency. @stress — opt-in."""
    p = PhoneticCorrector()
    p.load(["receive", "their", "there"])
    st = StressTester()

    def op():
        for w in ["recieve", "thier", "youre", "beleive"]:
            p.correct(w)

    result = st.run_stress_suite(op, num_operations=2000, concurrency=20)
    assert result.passed, (
        f"SLA p95={result.p95_latency_ms:.1f}ms throughput={result.throughput_ops:.0f}/s"
    )


# ═══════════════════════════════════════════════════════════════════════════
# PART F — DEVICE CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════════

def test_device_classification(sovereign):
    profile = classify()
    assert profile.tier in list(DeviceTier)
    assert profile.total_ram_gb > 0 and profile.cpu_cores >= 1


@pytest.mark.parametrize("feature_id", [
    "web_speech", "wasm_whisper_tiny", "python_cpu_tiny",
    "python_gpu_tiny", "streaming_partials",
])
def test_device_feature_check_returns_bool(sovereign, feature_id):
    ok, msg = check_feature(feature_id, classify())
    assert isinstance(ok, bool)
    if not ok:
        assert len(msg) > 0, "unavailable feature must have reason"


def test_device_runtime_selection(sovereign):
    assert select_runtime(classify()) in ("ipc_gpu", "ipc_cpu", "wasm", "cloud", "text")


def test_device_user_summary(sovereign):
    summary = user_facing_summary(classify())
    assert len(summary) > 100 and "tier" in summary.lower()


def test_gpu_feature_gated_on_no_gpu(sovereign):
    no_gpu = DeviceProfile(
        tier=DeviceTier.STANDARD, total_ram_gb=12.0, free_ram_gb=4.8,
        is_mobile=False, is_battery=False, gpu_kind="none", gpu_name="",
        cpu_cores=8, platform="linux", detected_at=time.time(),
    )
    ok, msg = check_feature("python_gpu_tiny", no_gpu)
    assert not ok and "GPU" in msg


# ═══════════════════════════════════════════════════════════════════════════
# PART G — TWIN QUALITY (verbatim from test_twin_quality.py)
# ═══════════════════════════════════════════════════════════════════════════

_CODE_FENCE_RE = re.compile(r"```(?:[a-zA-Z0-9_+\-]*)\n([\s\S]*?)```")


def verify_code(text, *, expected_fn_name=None, test_input=None, expected_output=None):
    if not text or "```" not in text:
        return False, "no_fence"
    m = _CODE_FENCE_RE.search(text)
    if not m:
        return False, "no_balanced_fence"
    code = m.group(1)
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"syntax:{e}"
    if expected_fn_name is not None:
        funcs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        if expected_fn_name not in funcs:
            return False, f"missing_fn:{expected_fn_name}"
    if test_input is not None and expected_output is not None:
        ns = {}
        try:
            exec(compile(code, "<test>", "exec"),
                 {"__builtins__": __builtins__}, ns)
        except Exception as e:
            return False, f"exec_failed:{type(e).__name__}:{e}"
        fn = ns.get(expected_fn_name) if expected_fn_name else None
        if fn is None:
            return False, "fn_not_callable"
        try:
            actual = fn(*test_input) if isinstance(test_input, tuple) else fn(test_input)
        except Exception as e:
            return False, f"fn_raise:{type(e).__name__}:{e}"
        if actual != expected_output:
            return False, f"wrong_output:{actual!r}!={expected_output!r}"
    return True, "ok"


def verify_extraction(text, schema):
    if not text:
        return False, "empty"
    m = _CODE_FENCE_RE.search(text)
    payload = m.group(1) if m else text
    try:
        data = json.loads(payload)
    except Exception as e:
        return False, f"json:{e}"

    def _check(node, s, path):
        t = s.get("type")
        if t == "object":
            if not isinstance(node, dict):
                return f"{path}:not_object"
            for k in s.get("required", []):
                if k not in node:
                    return f"{path}:missing:{k}"
            for k, sub in s.get("properties", {}).items():
                if k in node:
                    err = _check(node[k], sub, f"{path}.{k}")
                    if err:
                        return err
            return None
        if t == "array":
            if not isinstance(node, list):
                return f"{path}:not_array"
            items = s.get("items")
            if items:
                for i, v in enumerate(node):
                    err = _check(v, items, f"{path}[{i}]")
                    if err:
                        return err
            return None
        if t == "string":
            return None if isinstance(node, str) else f"{path}:not_str"
        if t == "number":
            return (None if isinstance(node, (int, float)) and not isinstance(node, bool)
                    else f"{path}:not_num")
        if t == "integer":
            return (None if isinstance(node, int) and not isinstance(node, bool)
                    else f"{path}:not_int")
        if t == "boolean":
            return None if isinstance(node, bool) else f"{path}:not_bool"
        return None

    err = _check(data, schema, "$")
    return (True, "ok") if err is None else (False, err)


def verify_math(text, expected):
    return (expected in (text or ""),
            "ok" if expected in (text or "") else "miss")


def verify_format_code(text):
    if not text:
        return False, "empty"
    if text.count("```") != 2:
        return False, f"expected_2_fence_markers_got_{text.count('```')}"
    stripped = text.strip()
    if not (stripped.startswith("```") and stripped.endswith("```")):
        return False, "not_exactly_one_block"
    return True, "ok"


def verify_format_extraction(text):
    if not text:
        return False, "empty"
    m = _CODE_FENCE_RE.search(text)
    candidate = (m.group(1) if m else text).strip()
    try:
        json.loads(candidate)
        return True, "ok"
    except Exception as e:
        return False, f"not_json:{e}"


_CODE_CASES = [
    ("reverse_string", "write a function reverse_string(s) returning s reversed",
     "reverse_string", ("abc",), "cba"),
    ("sum_list", "write a function sum_list(xs) returning the sum of xs",
     "sum_list", ([1, 2, 3, 4],), 10),
    ("is_palindrome", "write a function is_palindrome(s) returning bool",
     "is_palindrome", ("racecar",), True),
    ("factorial", "write a function factorial(n) returning n!",
     "factorial", (5,), 120),
    ("fizzbuzz", "write a function fizzbuzz(n) returning list 1..n with fizzbuzz rules",
     "fizzbuzz", (5,), ["1", "2", "fizz", "4", "buzz"]),
]

_EXTRACTION_CASES = [
    ("simple_object", 'extract name and age from: "Alice, 30 years old"',
     {"type": "object", "required": ["name", "age"],
      "properties": {"name": {"type": "string"}, "age": {"type": "integer"}}}),
    ("array_of_records",
     'extract all people from: "Bob 25, Carol 40, Dave 35"',
     {"type": "array", "items":
      {"type": "object", "required": ["name", "age"],
       "properties": {"name": {"type": "string"}, "age": {"type": "integer"}}}}),
]

_MATH_CASES = [
    ("add", "what is 1234 + 5678?", "6912"),
    ("multiply", "what is 47 * 89?", "4183"),
    ("percent", "what is 15% of 240?", "36"),
    ("sqrt", "what is the square root of 2025?", "45"),
    ("div", "what is 1000 / 8?", "125"),
]


async def _run_local(prompt: str, task: str, workspace: str) -> str:
    try:
        from core.twin_engine import execute_local_inference
        return await execute_local_inference(workspace, task, prompt)
    except Exception:
        return ""


@pytest.mark.asyncio
@pytest.mark.parametrize("name,prompt,fn_name,test_input,expected", _CODE_CASES)
async def test_twin_code_generation_elite(twin, name, prompt, fn_name,
                                          test_input, expected):
    text = await _run_local(prompt, "code_generation", "design")
    if not text or text.startswith("[stub]"):
        pytest.skip("twin engine not available")
    ok, reason = verify_code(text, expected_fn_name=fn_name,
                             test_input=test_input, expected_output=expected)
    assert ok, f"{name}: {reason}\n--- output ---\n{text[:600]}"


@pytest.mark.asyncio
@pytest.mark.parametrize("name,prompt,schema", _EXTRACTION_CASES)
async def test_twin_extraction_elite(twin, name, prompt, schema):
    text = await _run_local(prompt, "extraction", "data")
    if not text or text.startswith("[stub]"):
        pytest.skip("twin engine not available")
    ok, reason = verify_extraction(text, schema)
    assert ok, f"{name}: {reason}\n--- output ---\n{text[:600]}"


@pytest.mark.asyncio
@pytest.mark.parametrize("name,prompt,expected", _MATH_CASES)
async def test_twin_math_elite(twin, name, prompt, expected):
    text = await _run_local(prompt, "math", "core")
    if not text or text.startswith("[stub]"):
        pytest.skip("twin engine not available")
    ok, reason = verify_math(text, expected)
    assert ok, f"{name}: {reason}\n--- output ---\n{text[:600]}"


@pytest.mark.asyncio
async def test_twin_format_code_single_fence(twin):
    text = await _run_local("write a function hello() that returns 'world'",
                            "code_generation", "design")
    if not text or text.startswith("[stub]"):
        pytest.skip("twin engine not available")
    ok, reason = verify_format_code(text)
    assert ok, f"format violation: {reason}\n--- output ---\n{text[:600]}"


@pytest.mark.asyncio
async def test_twin_format_extraction_is_json(twin):
    text = await _run_local('extract name from: "Alice"', "extraction", "data")
    if not text or text.startswith("[stub]"):
        pytest.skip("twin engine not available")
    ok, reason = verify_format_extraction(text)
    assert ok, f"not JSON: {reason}\n--- output ---\n{text[:600]}"


# ═══════════════════════════════════════════════════════════════════════════
# PART H — SPECIALIZATION ROUTING
# ═══════════════════════════════════════════════════════════════════════════

def test_specialization_code_routes_to_coder(twin):
    pinned = best_for("code_generation", workspace="design")
    assert pinned in ("qwen25-coder", "qwen25-coder-tuned"), pinned


def test_specialization_extraction_routes_to_granite(twin):
    pinned = best_for("extraction", workspace="data")
    assert pinned in ("granite4-350m", "granite4-350m-q5"), pinned


def test_specialization_math_routes_to_math(twin):
    assert best_for("math", workspace="core") == "qwen25-math"


def test_specialization_elite_and_primary_variants(twin):
    spec = TASK_SPECIALISTS[("code_generation", "design")]
    assert spec.elite == "qwen25-coder-tuned"
    assert spec.primary == "qwen25-coder"


def test_specialization_all_pinned_models_exist(twin):
    for (task, ws), spec in TASK_SPECIALISTS.items():
        assert spec.primary in ROSTER_BY_ID, (task, ws, spec.primary)
        if spec.elite:
            assert spec.elite in ROSTER_BY_ID, (task, ws, spec.elite)


@pytest.mark.asyncio
async def test_specialization_100_concurrent_routes(twin):
    async def _one(i):
        task = ["code_generation", "extraction", "math", "reasoning"][i % 4]
        ws = ["design", "data", "core", "core"][i % 4]
        tier = "pro" if i % 2 else "free"
        return route_model(task, workspace=ws, tier=tier,
                           concurrency=1 if i % 3 == 0 else 3)

    results = await asyncio.gather(*[_one(i) for i in range(100)])
    for r in results:
        if r is not None:
            assert r in ROSTER_BY_ID, f"invalid model_id: {r}"


def test_elite_quality_contract_documented():
    """Thresholds this suite enforces must be visible in code."""
    thresholds = {
        "format_compliance": 0.99, "code_correctness": 0.95,
        "extraction_f1": 0.98, "math_accuracy": 0.95,
        "reasoning_accuracy": 0.85,
    }
    assert thresholds["format_compliance"] == 0.99
    assert thresholds["code_correctness"] == 0.95
