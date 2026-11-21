# api/workspace_diagnostics.py — AXELR v19.1.0
"""
Diagnostics, telemetry, eval harness, warmup, conformance checks.

`evaluate()` is the single source of truth for "how accurate is this
really" — do not report accuracy figures not produced by it.

Public API:
    describe()               engine + vocab metadata
    evaluate(items)          labeled eval + confusion matrix + mismatches
    evaluate_by_method(items) per-method accuracy breakdown  (v19.1 new)
    warmup()                 prime caches, idempotent
    reset_all_state()        clear sessions + telemetry
    run_conformance_checks() list of warning strings (empty == OK)
    get_telemetry / reset_telemetry / set_log_sink / record_telemetry
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from typing import Any, Callable, Dict, List, Optional

from .workspace_engine import (
    ENGINE_VERSION,
    compute_full_detection,
    detect_workspace_core,
    reset_sessions,
)
from .workspace_vocab import (
    _PHRASES_CORE,
    _PHRASES_DATA,
    _PHRASES_DESIGN,
    _VERB_CORE,
    _VERB_DATA,
    _VERB_DESIGN,
    _VOCAB_CORE_TOKS,
    _VOCAB_CORE_WEIGHTS,
    _VOCAB_DATA_TOKS,
    _VOCAB_DATA_WEIGHTS,
    _VOCAB_DESIGN_TOKS,
    _VOCAB_DESIGN_WEIGHTS,
    DETECTOR_VERSION,
    WORKSPACES,
)

_TELEMETRY_ON = os.getenv("AXELR_TELEMETRY", "").lower() in ("1", "true", "yes")
_TELEMETRY: "deque[Dict[str, Any]]" = deque(maxlen=128)


def record_telemetry(ws: str, method: str, conf: float, plen: int) -> None:
    if _TELEMETRY_ON:
        _TELEMETRY.append(
            {
                "ts": time.perf_counter(),
                "ws": ws,
                "method": method,
                "conf": round(conf, 3),
                "prompt_len": plen,
            }
        )


def get_telemetry() -> List[Dict[str, Any]]:
    return list(_TELEMETRY)


def reset_telemetry() -> None:
    _TELEMETRY.clear()


_LOG_SINK: Optional[Callable[[Dict[str, Any]], None]] = None


def set_log_sink(sink: Optional[Callable[[Dict[str, Any]], None]]) -> None:
    global _LOG_SINK
    _LOG_SINK = sink


def _log(event: str, **fields: Any) -> None:
    if _LOG_SINK is None:
        return
    try:
        _LOG_SINK({"event": event, **fields, "ts": time.perf_counter()})
    except Exception:
        pass


def warmup() -> int:
    for p in ("hello", "analyze csv", "build react component", ""):
        detect_workspace_core(p, [])
    tokens = len(_VOCAB_DATA_TOKS) + len(_VOCAB_DESIGN_TOKS) + len(_VOCAB_CORE_TOKS)
    _log("warmup", tokens=tokens)
    return tokens


def reset_all_state() -> None:
    reset_sessions()
    reset_telemetry()


def describe() -> Dict[str, Any]:
    return {
        "version": ENGINE_VERSION,
        "vocab_version": DETECTOR_VERSION,
        "workspaces": list(WORKSPACES),
        "vocab_data": len(_VOCAB_DATA_TOKS),
        "vocab_design": len(_VOCAB_DESIGN_TOKS),
        "vocab_core": len(_VOCAB_CORE_TOKS),
        "phrases_data": len(_PHRASES_DATA),
        "phrases_design": len(_PHRASES_DESIGN),
        "phrases_core": len(_PHRASES_CORE),
        "verbs_data": len(_VERB_DATA),
        "verbs_design": len(_VERB_DESIGN),
        "verbs_core": len(_VERB_CORE),
        "telemetry_enabled": _TELEMETRY_ON,
        "telemetry_size": len(_TELEMETRY),
    }


def evaluate(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Labeled eval harness.

    items = [
        {"prompt": str,
         "files"?: list[str],
         "file_contents"?: str | list[str] | dict[str, str],
         "expected": "core" | "data" | "design"}
    ]

    Returns {"n", "accuracy", "confusion", "mismatches"}. Trust this
    number — it is the only honest accuracy figure.
    """
    confusion: Dict[str, Dict[str, int]] = {exp: {p: 0 for p in WORKSPACES} for exp in WORKSPACES}
    mismatches: List[Dict[str, Any]] = []
    for it in items or []:
        exp = it.get("expected")
        if exp not in WORKSPACES:
            continue
        res = compute_full_detection(
            it.get("prompt", ""),
            it.get("files", []),
            it.get("file_contents"),
            session_id="",  # eval must not be affected by hysteresis
            use_anchors=True,
        )
        pred = res["workspace"]
        confusion[exp][pred] += 1
        if pred != exp:
            mismatches.append(
                {
                    "prompt": it.get("prompt", ""),
                    "expected": exp,
                    "predicted": pred,
                    "confidence": res["confidence"],
                    "method": res["method"],
                }
            )
    total = sum(sum(r.values()) for r in confusion.values())
    correct = sum(confusion[w][w] for w in WORKSPACES)
    return {
        "n": total,
        "accuracy": round(correct / total, 4) if total else 0.0,
        "confusion": confusion,
        "mismatches": mismatches[:50],
    }


def evaluate_by_method(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    v19.1: per-method accuracy breakdown. Shows which signal tiers are
    reliable and which need vocabulary expansion. Use this to guide
    where to invest next — not guesswork.
    """
    stats: Dict[str, Dict[str, int]] = defaultdict(lambda: {"total": 0, "correct": 0})
    for it in items or []:
        exp = it.get("expected")
        if exp not in WORKSPACES:
            continue
        res = compute_full_detection(
            it.get("prompt", ""),
            it.get("files", []),
            it.get("file_contents"),
            session_id="",
            use_anchors=True,
        )
        method = res["method"]
        # Normalize method to top-level signal family
        family = (
            "code_fence"
            if method == "code_fence"
            else "anchor"
            if method == "anchor"
            else "file_evidence"
            if method == "file_evidence"
            else "content_evidence"
            if method == "content_evidence"
            else "fallback"
            if method == "fallback_ambiguous"
            else method.rsplit("_", 1)[0]
            if "_" in method
            else method
        )
        stats[family]["total"] += 1
        if res["workspace"] == exp:
            stats[family]["correct"] += 1
    out = {}
    for family, s in stats.items():
        out[family] = {
            "n": s["total"],
            "correct": s["correct"],
            "accuracy": round(s["correct"] / s["total"], 4) if s["total"] else 0.0,
        }
    return out


def run_conformance_checks() -> List[str]:
    """Return list of warning strings. Empty list means conformant."""
    warnings: List[str] = []

    if len(_VOCAB_DATA_TOKS) < 60:
        warnings.append(f"vocab_data too small: {len(_VOCAB_DATA_TOKS)} (<60)")
    if len(_VOCAB_DESIGN_TOKS) < 60:
        warnings.append(f"vocab_design too small: {len(_VOCAB_DESIGN_TOKS)} (<60)")
    if len(_VOCAB_CORE_TOKS) < 20:
        warnings.append(f"vocab_core too small: {len(_VOCAB_CORE_TOKS)} (<20)")

    for name, toks, wts in (
        ("data", _VOCAB_DATA_TOKS, _VOCAB_DATA_WEIGHTS),
        ("design", _VOCAB_DESIGN_TOKS, _VOCAB_DESIGN_WEIGHTS),
        ("core", _VOCAB_CORE_TOKS, _VOCAB_CORE_WEIGHTS),
    ):
        if len(toks) != len(wts):
            warnings.append(f"vocab_{name}: {len(toks)} toks vs {len(wts)} weights")
        if any(not (0 <= w <= 3) for w in wts):
            warnings.append(f"vocab_{name}: weight outside [0,3]")
        if tuple(sorted(toks)) != toks:
            warnings.append(f"vocab_{name}: not sorted (bisect will fail)")

    smoke = [
        ("build a react component", [], "design"),
        ("analyze this csv", [], "data"),
        ("hello there", [], "core"),
        ("", [], "core"),
        ("write a python function to sort a list", [], "design"),
        ("explain how to sort a list", [], "core"),
        ("sql query optimization", [], "data"),
        ("fix the bug in the login", [], "design"),
        # v19.1 additions — all pass with current weights:
        ("create a react component", [], "design"),
        ("train a machine learning model", [], "data"),
        ("how do i use usestate in react", [], "design"),
        ("don't analyze the csv", [], "core"),
    ]
    for prompt, files, expected in smoke:
        pred = detect_workspace_core(prompt, files)
        if pred != expected:
            warnings.append(f"smoke: {prompt!r} → {pred!r} (expected {expected!r})")
    return warnings


__all__ = [
    "describe",
    "evaluate",
    "evaluate_by_method",
    "get_telemetry",
    "reset_all_state",
    "reset_telemetry",
    "run_conformance_checks",
    "set_log_sink",
    "warmup",
    "record_telemetry",
]
