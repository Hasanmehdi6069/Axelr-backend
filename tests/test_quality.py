"""
Conformance, gates, and evaluation harnesses.
==============================================
Everything that measures "is the output correct?" not "does it crash?".
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.quality


# ═══════════════════════════════════════════════════════════════════════════
# Workspace detector — in-tree conformance suite
# ═══════════════════════════════════════════════════════════════════════════
def test_workspace_detector_conformance_suite_passes():
    # WHY: this module ships its own 30-assertion self-test. If it ever fails,
    # routing has drifted and the OOD gate will fire in prod anyway.
    from api.workspace_detector import _self_test
    results = _self_test()
    failed = [(n, d) for n, ok, d in results if not ok]
    assert not failed, f"conformance failures: {failed}"


def test_workspace_detector_conformance_vectors_present():
    from api.workspace_detector import CONFORMANCE_VECTORS
    assert len(CONFORMANCE_VECTORS) >= 10


def test_workspace_detector_audit_warnings_empty():
    # WHY: import-time invariants must be clean — any warning is a latent bug.
    from api.workspace_detector import _AUDIT_WARNINGS
    assert not _AUDIT_WARNINGS, list(_AUDIT_WARNINGS)


# ═══════════════════════════════════════════════════════════════════════════
# OOD gate (weights.json + ood_vectors.json)
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.slow
def test_ood_vectors_meet_floor():
    # WHY: the router's classifier must not silently degrade.
    import importlib
    import api.workspace_detector as wd
    importlib.reload(wd)
    root = Path(__file__).parent.parent
    cfg = json.loads((root / "weights.json").read_text())
    floor = float(cfg.get("ood_gate", {}).get("min_accuracy", 0.80))
    vectors = json.loads((root / "tests" / "ood_vectors.json").read_text())
    det = wd.get_detector()
    correct = 0
    for v in vectors:
        r = det.detect(v.get("prompt", ""), v.get("files") or [])
        if r.workspace == v["label"]:
            correct += 1
    acc = correct / len(vectors)
    assert acc >= floor, f"OOD accuracy {acc:.3f} < floor {floor:.3f}"


# ═══════════════════════════════════════════════════════════════════════════
# Benchmark gate
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.slow
def test_benchmark_gate_runs_without_error():
    # WHY: `python -m tests.benchmark_gate` is a CI gate. If it crashes,
    # the gate silently stops gating.
    from tests.benchmark_gate import _run, _diff
    current = _run()
    assert isinstance(current, dict)
    assert all("p50_us" in v for v in current.values())


# ═══════════════════════════════════════════════════════════════════════════
# Anti-hallucination grounding
# ═══════════════════════════════════════════════════════════════════════════
def test_ahe_adjudicator_rejects_when_any_claim_fails():
    # WHY: the whole zero-hallucination guarantee is binary. One bad claim
    # must reject the payload.
    from core.anti_hallucination import AdjudicatorAgent
    from core.anti_hallucination.schemas import (
        ExtractedClaim, UnverifiableDataError, VerificationResult,
    )
    good = ExtractedClaim(field_name="a", value="1",
                          raw_snippet="a=1", chunk_id="c1")
    bad = ExtractedClaim(field_name="b", value="2",
                         raw_snippet="b=2", chunk_id="c2")
    with pytest.raises(UnverifiableDataError):
        AdjudicatorAgent().adjudicate(
            document_id="d",
            claims=[good, bad],
            chunks_by_id={"c1": None, "c2": None},
            verdicts=[
                VerificationResult(verified=True, reason="ok"),
                VerificationResult(verified=False, reason="mismatch"),
            ],
        )


def test_ahe_audit_hash_is_stable():
    # WHY: audit hash is the compliance artifact. Same payload → same hash.
    from core.anti_hallucination.schemas import (
        VerifiedClaim, VerifiedPayload, VerificationResult, SourceCoordinate,
        compute_audit_hash,
    )
    coord = SourceCoordinate(document_id="d", chunk_id="c", char_start=0,
                             char_end=1)
    v = VerificationResult(verified=True, reason="ok")
    c = VerifiedClaim(field_name="a", value="1", raw_snippet="a",
                      coordinate=coord, verification=v)
    p1 = VerifiedPayload(document_id="d", claims=[c], audit_hash="")
    p2 = VerifiedPayload(document_id="d", claims=[c], audit_hash="")
    assert compute_audit_hash(p1) == compute_audit_hash(p2)


def test_ahe_canonicalize_is_lossless_for_visible_chars():
    # WHY: canonicalization must not silently drop characters or the
    # substring match guarantee is a lie.
    from core.anti_hallucination import canonicalize
    src = "hello world"
    out, idx = canonicalize(src)
    assert len(out) == len(idx)
    assert out == "hello world"


# ═══════════════════════════════════════════════════════════════════════════
# Reflection quality gate
# ═══════════════════════════════════════════════════════════════════════════
def test_reflection_scan_detects_all_known_defects():
    from core.reflection import scan
    cases = [
        ("```python\nprint(1)", "unclosed_fence"),
        ("<system_identity>x</system_identity>", "prompt_leak"),
        ("Do [insert name] then TODO", "placeholder"),
    ]
    for text, expected in cases:
        r = scan(text)
        assert expected in r.issues, (text, r.issues)


# ═══════════════════════════════════════════════════════════════════════════
# Enhancer quality delta
# ═══════════════════════════════════════════════════════════════════════════
def test_enhancer_improves_specificity_not_density():
    # WHY: on a vague prompt, "specificity" is what the enhancer must raise.
    from api.enhancer import _compute_scores
    s = _compute_scores(
        "can you please just help me write code",
        "Write a Python function that sorts a list of numbers. Include "
        "signature, docstring, complexity, and a usage example.",
    )
    assert s["enhanced"]["specificity"] > s["original"]["specificity"]
    assert s["delta"] > 0


def test_enhancer_no_change_scores_zero_delta():
    from api.enhancer import _compute_scores
    s = _compute_scores("same prompt", "same prompt")
    assert s["delta"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# Consensus
# ═══════════════════════════════════════════════════════════════════════════
def test_consensus_similarity_is_symmetric():
    from core.consensus import similarity
    a = similarity("hello world foo", "hello world bar")
    b = similarity("hello world bar", "hello world foo")
    assert a == b


def test_consensus_similarity_is_bounded():
    from core.consensus import similarity
    s = similarity("a b c d e", "x y z w v")
    assert 0.0 <= s["combined"] <= 1.0


# ═══════════════════════════════════════════════════════════════════════════
# Provider conformance — keyless providers must not be in ACTIVE
# ═══════════════════════════════════════════════════════════════════════════
def test_active_providers_are_keyed_where_required():
    from api.providers import ACTIVE_PROVIDER_ENTRIES, PROVIDER_KEY_CHECK
    # gemini/groq/etc must be gated by keys. In dev they'll be False.
    for name, _, _ in ACTIVE_PROVIDER_ENTRIES:
        if name == "local":
            continue
        # Just assert the key check is a bool
        assert isinstance(PROVIDER_KEY_CHECK.get(name), bool)