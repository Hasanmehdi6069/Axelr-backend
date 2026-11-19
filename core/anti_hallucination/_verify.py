"""_verify — verification orchestration + primitive re-exports.

Expanded from a shim into a substantive module: the primitives stay
re-exported for backward compatibility, but higher-level verification
utilities (payload audit, coordinate validation, shape summary,
provenance completeness) live here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from .primitives import (  # noqa: F401 — re-exports for backward compat
    REASON_CHUNK_NOT_FOUND,
    REASON_DETERMINISTIC,
    REASON_EMPTY_SNIPPET,
    REASON_NEGATED,
    REASON_NOT_SUBSTRING,
    REASON_OK,
    REASON_QUERY_ECHO,
    REASON_QUERY_SIMILAR,
    REASON_SHAPE_MISMATCH,
    REASON_SHORT_SNIPPET,
    REASON_STRICT_THRESHOLD,
    REASON_VALUE_NOT_DERIVABLE,
    REASON_VERBATIM,
    REASON_WEAK_VALUE,
    ByteTokenizer,
    LineageIndex,
    MinimalPerfectHash,
    balance_tags,
    check_arithmetic,
    chunk_hash,
    contradiction,
    cross_language_leak,
    json_fsa_valid,
    matches_shape,
    negation_window,
    pda_balanced,
    project_budget,
    py_undefined_names,
    replay_key,
    request_fingerprint,
    sign_root,
    stable_tie_break,
    verify_invariants,
)


# ═══════════════════════════════════════════════════════════════════════════
# Payload-level verification (v1.7 additions)
# ═══════════════════════════════════════════════════════════════════════════
def verify_payload_invariants(payload) -> List[str]:
    """Comprehensive invariant check. Returns list of violations (empty=ok)."""
    violations: List[str] = []
    for i, c in enumerate(payload.claims):
        if c.coordinate.char_end < c.coordinate.char_start:
            violations.append(f"claims[{i}]: char_end < char_start")
        if not c.verification.verified:
            violations.append(f"claims[{i}]: unverified claim in verified payload")
        if c.coordinate.char_start < 0:
            violations.append(f"claims[{i}]: negative char_start")
        if not c.coordinate.chunk_id:
            violations.append(f"claims[{i}]: empty chunk_id")
        if not c.field_name:
            violations.append(f"claims[{i}]: empty field_name")
    if payload.conformance_score < 0 or payload.conformance_score > 100:
        violations.append("conformance_score out of [0, 100]")
    violations.extend(verify_invariants(payload))
    return violations


def audit_payload_consistency(payload) -> Dict[str, bool]:
    """Cross-check audit fields for internal consistency."""
    from .schemas import compute_audit_hash
    checks: Dict[str, bool] = {}
    checks["audit_hash_present"] = bool(payload.audit_hash)
    checks["audit_hash_matches"] = (
        payload.audit_hash == compute_audit_hash(payload)
        if payload.audit_hash else False
    )
    checks["merkle_root_present"] = bool(payload.merkle_root)
    checks["signature_present"] = bool(payload.signature)
    checks["fingerprint_present"] = bool(payload.request_fingerprint)
    checks["replay_key_present"] = bool(payload.replay_key)
    checks["timestamps_present"] = bool(
        payload.asserted_at and payload.finalized_at
    )
    checks["claims_sorted"] = _is_sorted(payload.claims)
    checks["all_verified"] = all(
        c.verification.verified for c in payload.claims
    )
    return checks


def _is_sorted(claims) -> bool:
    key = lambda c: (
        c.field_name, c.coordinate.char_start,
        c.coordinate.char_end, c.value,
    )
    return all(key(claims[i]) <= key(claims[i + 1])
               for i in range(len(claims) - 1))


def provenance_completeness(payload) -> Tuple[int, int, float]:
    """Return (with_provenance, total, ratio)."""
    total = len(payload.claims)
    if total == 0:
        return 0, 0, 0.0
    with_prov = sum(1 for c in payload.claims if c.provenance)
    return with_prov, total, with_prov / total


# ═══════════════════════════════════════════════════════════════════════════
# Coordinate validation against chunks
# ═══════════════════════════════════════════════════════════════════════════
def validate_coordinates(
    claims: Sequence[Any], chunk_map: Dict[str, Any],
) -> List[str]:
    """Validate each claim's coordinates fall within its chunk."""
    errors: List[str] = []
    for i, c in enumerate(claims):
        ch = chunk_map.get(c.coordinate.chunk_id)
        if ch is None:
            errors.append(f"claims[{i}]: chunk {c.coordinate.chunk_id!r} unknown")
            continue
        lo = c.coordinate.char_start - ch.origin_offset
        hi = c.coordinate.char_end - ch.origin_offset
        if lo < 0 or hi > len(ch.text) or hi < lo:
            errors.append(
                f"claims[{i}]: span [{lo}, {hi}] outside chunk "
                f"(len={len(ch.text)})"
            )
    return errors


# ═══════════════════════════════════════════════════════════════════════════
# Shape validation summary
# ═══════════════════════════════════════════════════════════════════════════
def shape_validation_summary(
    claims: Sequence[Any], shapes: Dict[str, str],
) -> Dict[str, Any]:
    """Report shape conformance per field."""
    out: Dict[str, Any] = {"total": 0, "passed": 0, "failed": []}
    for c in claims:
        shape = shapes.get(c.field_name)
        if not shape:
            continue
        out["total"] += 1
        if matches_shape(c.value, shape):
            out["passed"] += 1
        else:
            out["failed"].append({
                "field": c.field_name, "value": c.value,
                "expected_shape": shape,
            })
    if out["total"] > 0:
        out["ratio"] = out["passed"] / out["total"]
    else:
        out["ratio"] = 1.0
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Canonical equality
# ═══════════════════════════════════════════════════════════════════════════
def claim_canonical_equality(a: str, b: str) -> bool:
    """True iff canonical forms match."""
    from .bounding import canonicalize
    return canonicalize(a)[0] == canonicalize(b)[0]


def canonical_form(text: str) -> str:
    from .bounding import canonicalize
    return canonicalize(text)[0]


# ═══════════════════════════════════════════════════════════════════════════
# Negation safety
# ═══════════════════════════════════════════════════════════════════════════
def negation_safe(claim_value: str, source_window: str) -> bool:
    """True iff claim is compatible with source polarity."""
    c = contradiction(claim_value, source_window)
    return c is None


__all__ = [  # re-exports + new utilities
    "balance_tags", "ByteTokenizer", "matches_shape", "pda_balanced",
    "json_fsa_valid", "py_undefined_names", "cross_language_leak",
    "check_arithmetic", "negation_window", "contradiction",
    "request_fingerprint", "replay_key", "chunk_hash", "sign_root",
    "project_budget", "stable_tie_break", "verify_invariants",
    "LineageIndex", "MinimalPerfectHash",
    "REASON_OK", "REASON_CHUNK_NOT_FOUND", "REASON_EMPTY_SNIPPET",
    "REASON_SHORT_SNIPPET", "REASON_NOT_SUBSTRING",
    "REASON_VALUE_NOT_DERIVABLE", "REASON_STRICT_THRESHOLD",
    "REASON_WEAK_VALUE", "REASON_QUERY_ECHO", "REASON_QUERY_SIMILAR",
    "REASON_NEGATED", "REASON_DETERMINISTIC", "REASON_VERBATIM",
    "REASON_SHAPE_MISMATCH",
    # v1.7 additions
    "verify_payload_invariants", "audit_payload_consistency",
    "provenance_completeness", "validate_coordinates",
    "shape_validation_summary", "claim_canonical_equality",
    "canonical_form", "negation_safe",
]
