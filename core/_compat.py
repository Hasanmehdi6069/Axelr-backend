# core/_compat.py
"""
AXELR — Backward-compat shim for v6 → v7 consolidation
========================================================
Temporary. Every symbol here lives canonically in a merged module. This
file re-exports them so existing call sites (tests, api/, external code)
import without change.

Scheduled for removal in v8.0. Do not add new symbols here.
"""
from __future__ import annotations

from .shedder import (
    Complexity as TaskShedderComplexity,
    assess as task_shedder_assess,
    adaptive_budget, decide_width, shed_stats,
)
from .guards import (
    estimate_tokens as token_governor_estimate_tokens,
    reserve as token_governor_reserve,
    settle as token_governor_settle,
    snapshot_usage as token_governor_snapshot,
)
from .adaptive import (
    tune_temperature, LEDGER, AnchorLedger, stops_for,
    StreamQualityGate, GateDecision,
    hedge_wrap, canonical_system,
    adapt_max_tokens, record_token_ratio,
)
from .goat_layer import (
    Reflection as ReflectionScanResult,
    scan as reflection_scan,
    repair as reflection_repair,
)
from .consensus import (
    Stage as CognitiveStage,
    run_cognitive as cognitive_run,
)
from .condenser import (
    anchors_of,
    compress_history as semantic_anchor_compress,
    condense as context_condense,
)
from .resilience import (
    TIERS, Tier as FailoverTier, ladder_run,
    HotSwapDetector, HotSwapResult, get_hot_swap_detector,
    WaveRacer, RACER,
)
from .observe import (
    Meter, METER,
    TRACES, trace_scope, span, record_stage,
    bind, unbind, current_id, log_middleware,
    observe_call, observe_ttft, flush_pending_spans,
    arm as memory_tuner_arm, disarm as memory_tuner_disarm,
    trim_middleware, tuner_stats as memory_tuner_stats,
)
from .foundation import (
    AxelrError, ProviderError, DeadlineExceeded, ValidationFailed,
    RateLimited, NoProviderAvailable, CircuitOpen, ConfigError,
    is_retryable, to_result,
    RetryPolicy, with_retry,
    POLICY_DEFAULT, POLICY_NETWORK, POLICY_IDEMPOTENT,
)
from .guards import (
    enter as micro_enter, exit as micro_exit,
    is_active as micro_active, ceiling_mb as micro_ceiling,
    clamp_max_tokens as micro_clamp_tokens,
    clamp_response as micro_clamp_response,
    policy as micro_policy,
    issue as override_issue, record_decision as override_record,
    should_proceed as override_should_proceed,
    BurstToken, burst_acquire, burst_release, burst_active,
    burst_stats, burst_shutdown,
    start as thermal_start, stop as thermal_stop,
    running as thermal_running, budget as thermal_budget,
    pressure as thermal_pressure, snapshot as thermal_snapshot,
)
from .gateway import (
    GatewayRouter, GatewayEndpoint, get_gateway_router,
    to_openai, to_anthropic, to_gemini,
    from_openai_response, from_anthropic_response, from_gemini_response,
    detect_shape, build_payload, parse_response,
)
from .cache import (
    LocalSemanticCache, get_local_cache,
    BM25Index, BM25Hit, get_bm25_index,
)
from .persistence import PERSIST, PersistenceEngine
try:
    from .compliance import (
        mask_pii, restore_pii,
        start_pii_eviction_task, stop_pii_eviction_task,
        clear_vault_for_session,
    )
except ImportError:
    async def mask_pii(session_id, tenant_id, text, sensitive_values): return text
    async def restore_pii(session_id, tenant_id, text): return text
    def start_pii_eviction_task(): pass
    def stop_pii_eviction_task(): pass
    def clear_vault_for_session(session_id, tenant_id): pass

__all__ = [n for n in dir() if not n.startswith("_")]