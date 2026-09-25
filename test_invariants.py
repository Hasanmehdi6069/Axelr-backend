"""
Router / provider-catalog invariants.
=====================================
Migrated from phase5_checks.py, which was an asyncio script that asserted
a handful of cross-cutting invariants about the provider chain, tier
config, and workspace classifier. Those invariants protect the router
from silently degrading: a provider missing from one of the three
catalogs (FUNC_MAP / KEY_CHECK / MODELS) means a runtime KeyError the
moment that workspace is exercised; a mis-ordered chain means "local"
fallback is not last and the router returns "local" while a real
provider is still available.

Every test here is offline — no provider is called, no key is required.
"""
from __future__ import annotations

import pytest

from api.quota import TIER_CONFIG, estimate_tokens
from api.prompts import strip_fluff
from api.providers import (
    PROVIDER_CHAIN,
    PROVIDER_FUNC_MAP,
    PROVIDER_KEY_CHECK,
    PROVIDER_MODELS,
    get_dynamically_ranked_providers,
    get_provider_order,
)
from api.routes_core import detect_workspace


# ═══════════════════════════════════════════════════════════════════════════
# Provider catalog consistency
# ═══════════════════════════════════════════════════════════════════════════

def test_every_chain_provider_is_registered_in_all_three_catalogs():
    """
    Protects: PROVIDER_CHAIN is the single source of truth the router
    iterates. Every name in it must exist in FUNC_MAP (callable),
    KEY_CHECK (config flag), and MODELS (default models). A missing
    entry surfaces as a runtime KeyError deep inside a request, not at
    boot — so we assert it at test time instead.
    """
    for name, _ in PROVIDER_CHAIN:
        assert name in PROVIDER_FUNC_MAP, f"{name} missing from FUNC_MAP"
        assert name in PROVIDER_KEY_CHECK, f"{name} missing from KEY_CHECK"
        assert name in PROVIDER_MODELS, f"{name} missing from MODELS"


# ═══════════════════════════════════════════════════════════════════════════
# Provider ordering
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("workspace", ["data", "design", "core", "prompt", "touch_fix"])
def test_provider_order_is_deterministic_and_ends_with_local(workspace):
    """
    Protects: two invariants in one.
      (1) get_provider_order(ws) called twice must return the *same* list
          — otherwise the router's fallback path is non-reproducible.
      (2) "local" must be last — otherwise the router can return the
          no-op local fallback while a real provider is still healthy.
    """
    order_a = get_provider_order(workspace)
    order_b = get_provider_order(workspace)
    assert order_a == order_b, f"{workspace}: non-deterministic order"
    assert order_a[-1] == "local", f"{workspace}: local not last"
    assert len(order_a) == len(set(order_a)), f"{workspace}: duplicate providers"


# ═══════════════════════════════════════════════════════════════════════════
# Dynamic ranking key-gate
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("workspace", ["data", "design", "core"])
def test_dynamic_ranking_never_includes_unkeyed_providers(workspace):
    """
    Protects: get_dynamically_ranked_providers() must respect KEY_CHECK.
    If a provider without a key were ranked, the router would attempt it,
    burn a retry slot, and log a spurious failure that trips the circuit
    breaker for a provider that was never configured.
    """
    for p in get_dynamically_ranked_providers(workspace):
        if p == "local":
            continue
        assert PROVIDER_KEY_CHECK.get(p), (
            f"{workspace}: {p} has no key but was ranked"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Tier config shape
# ═══════════════════════════════════════════════════════════════════════════

def test_tier_config_is_dict_and_every_tier_has_providers():
    """
    Protects: quota enforcement reads TIER_CONFIG[tier]["providers"] to
    decide which providers a user may hit. A tier missing that key
    raises KeyError at request time for exactly the paying customers
    whose tier was added most recently — the worst time to fail.
    """
    assert isinstance(TIER_CONFIG, dict)
    for tier, cfg in TIER_CONFIG.items():
        assert "providers" in cfg, f"tier {tier!r} missing 'providers'"
        assert isinstance(cfg["providers"], (list, str)), (
            f"tier {tier!r}: providers must be list or '*'"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Workspace classifier
# ═══════════════════════════════════════════════════════════════════════════

def test_detect_workspace_classifies_canonical_prompts():
    """
    Protects: the intent classifier's three happy-path workspaces. If
    any of these drifts, the router sends the request to the wrong
    provider family — a CSV prompt to the design chain, for example,
    which then fails over to local and returns a canned reply.
    """
    assert detect_workspace("extract columns from this CSV", []) == "data"
    assert detect_workspace("design a navbar", []) == "design"
    assert detect_workspace("hello", []) == "core"


# ═══════════════════════════════════════════════════════════════════════════
# Utility functions
# ═══════════════════════════════════════════════════════════════════════════

def test_estimate_tokens_empty_is_zero():
    """
    Protects: empty prompts must not be billed as >= 1 token. A nonzero
    value here inflates usage accounting for every no-op turn and
    eventually locks free users out of a quota they never spent.
    """
    assert estimate_tokens("") == 0


def test_estimate_tokens_is_chars_over_four():
    """
    Protects: the ~4-chars-per-token heuristic used for quota pre-flight.
    If this drift, quota checks become either over- or under-permissive
    relative to the real tokenizer downstream.
    """
    assert estimate_tokens("a" * 400) == 100


def test_strip_fluff_removes_acknowledgement_prefix():
    """
    Protects: providers that prepend "Sure! Here you go:" must have that
    stripped before the text reaches the user — otherwise the UI shows a
    chatty LLM tic on every response. The assertion is deliberately
    substring-based so refactors to the exact strip regex do not break
    the invariant (user-visible output must not contain the prefix).
    """
    out = strip_fluff("Sure! Here you go: hello")
    assert "Sure!" not in out
    assert "hello" in out