<<<<<<< HEAD
# core/__init__.py
"""
AXELR Core — canonical package surface
========================================
Single import point for every public symbol in ``core``.

Every submodule remains independently importable. This file exists so
operators, tests, and downstream services can do ``from core import X``
without tracking which historical file X lives in.

Version history
---------------
v5.0 — full consolidation pass. Merged:
    elite_orchestrator   → orchestration
    heartbeat            → telemetry
    ghost_telemetry      → telemetry
    observe              → telemetry
    system_monitor       → resilience
    circuit_breaker      → infra (re-exported via resilience)
    bilingual            → compliance
    explainer            → utils.misc_utils
    char_cnn_matrix      → char_cnn
    polyfill/trie/ring   → ux_input
    context              → context_condenser + token_governor

Submodules removed (do not re-add)
----------------------------------
    elite_orchestrator.py  — replaced by orchestration.py
    heartbeat.py           — merged into telemetry.py
    ghost_telemetry.py     — merged into telemetry.py
    observe.py             — merged into telemetry.py
    observability.py       — placeholder, superseded by telemetry.py
    circuit_breaker.py     — CircuitBreaker lives in infra.py
    system_monitor.py      — MemorySnapshot lives in resilience.py
    bilingual.py           — merged into compliance.py
    explainer.py           — merged into utils/misc_utils.py
    char_cnn_matrix.py     — char_cnn.py supersedes it
    text_ring_buffer.py    — inside ux_input.py
    lexical_trie.py        — inside ux_input.py
    contextual_trie.py     — inside ux_input.py
    polyfill_core.py       — inside ux_input.py
    intent_parser.py       — inside ux_input.py
    homophone_corrector.py — inside ux_input.py
    ux_components.py       — inside ux_input.py
    twin_platform.py       — replaced by twin_engine internal helpers
    twin_process.py        — placeholder, never wired in
    token_governor.py      — kept, canonical
"""
from __future__ import annotations

from typing import Any

_VERSION = "5.0"


# ═══════════════════════════════════════════════════════════════════════════
# Foundation
# ═══════════════════════════════════════════════════════════════════════════
from .utils import (                              # noqa: F401
    AxelrError, ProviderError, DeadlineExceeded, ValidationFailed,
    RateLimited, NoProviderAvailable, CircuitOpen, ConfigError,
    is_retryable, to_result,
    RetryPolicy, with_retry,
    PIIType, sanitize, found_types,
    iter_payloads, stream_for_shape,
)
from .config import load as load_config, audit as config_audit  # noqa: F401
from .permissions import (                        # noqa: F401
    Permission, UserConsent, CORE_PERMISSIONS,
    get_required_permissions, get_optional_permissions, check_permissions,
)
from .resilience import (                         # noqa: F401
    CircuitBreaker as UnifiedCircuitBreaker, BREAKER,
)
from .meter import Meter, METER                   # noqa: F401


# ═══════════════════════════════════════════════════════════════════════════
# Infra (canonical home of CircuitBreaker + Redis + cf_embed_batch + Context)
# ═══════════════════════════════════════════════════════════════════════════
from .infra import (                              # noqa: F401
    CircuitBreaker, get_redis_client,
    get_redis_cache, set_redis_cache, delete_redis_cache,
    cf_embed_batch, ContextRegistry, ContextItem,
)


# ═══════════════════════════════════════════════════════════════════════════
# Telemetry (merged: ghost_telemetry · observe · heartbeat)
# ═══════════════════════════════════════════════════════════════════════════
from .telemetry import (                          # noqa: F401
    # Observe
    TRACES, TraceRing, trace_scope, span, record_stage, finish,
    bind as bind_log, unbind as unbind_log, current_id, new_id,
    log_middleware, observe_call, observe_ttft, flush_pending_spans,
    # Heartbeat
    ProviderHealth, HEARTBEAT,
    rank as heartbeat_rank, score as heartbeat_score,
    record_success as heartbeat_record_success,
    record_failure as heartbeat_record_failure,
    snapshot as heartbeat_snapshot,
    reset as heartbeat_reset,
    install_bridge as heartbeat_install_bridge,
    heartbeat_audit,
    # Ghost telemetry
    DeviceProfile, RoutingDecision,
    fingerprint_to_profile, match_category, accept_telemetry,
    ghost_snapshot as telemetry_snapshot,
    ghost_audit as telemetry_audit,
)


# ═══════════════════════════════════════════════════════════════════════════
# Cache + Streaming
# ═══════════════════════════════════════════════════════════════════════════
from .cache import (                              # noqa: F401
    CacheEntry, CloudflareVectorizeCache, get_vector_cache,
    canonical_prefix, remember_prefix, recall_prefix,
    provider_cache_hint, measure_hit,
)
from .utils.security_and_streaming import (       # noqa: F401
    CancellationToken, CancelledByClient, bind_to_request, cancellable,
    ChunkedReader, read_upload_streaming,
    InFlightScrubber, scrub_stream,
    FirstTokenRace, RaceOutcome, StreamerFunc,
    PII_DETECTED, CHUNK_BYTES, MAX_INFLIGHT_CHUNKS,
    audit as stream_audit,
)


# ═══════════════════════════════════════════════════════════════════════════
# Memory + Index
# ═══════════════════════════════════════════════════════════════════════════
from .memory import Memory, MemoryItem, get_memory          # noqa: F401
from .repo_indexer import RepoIndexer, IndexStats, CodeChunk  # noqa: F401


# ═══════════════════════════════════════════════════════════════════════════
# Quality Conductor
# ═══════════════════════════════════════════════════════════════════════════
from .quality_conductor import (                  # noqa: F401
    run as run_conductor,
    stream as stream_conductor,
    audit as conductor_audit,
    stats as conductor_stats,
    reset_stats as conductor_reset_stats,
    health as conductor_health,
)


# ═══════════════════════════════════════════════════════════════════════════
# Orchestration (canonical home — replaces elite_orchestrator)
# ═══════════════════════════════════════════════════════════════════════════
from .orchestration import (                      # noqa: F401
    # Pipeline singletons
    get_pipeline, run_pipeline, stream_pipeline,
    # Failover re-exports
    Tier, TIERS, ladder_run,
    ProviderFunc, HotSwapResult, HotSwapDetector, get_hot_swap_detector,
    # Pipeline re-export
    ResilientPipeline, ProviderCascade,
    # Elite
    hedge_wrap, audit as orchestration_audit, pipeline_stats,
    install_negative_cache, canonical_system,
    install_health_propagation, adapt_max_tokens, record_completion_length,
    # Scheduler
    TaskScheduler, get_task_scheduler, Priority, Task,
    # Prewarm
    signal as prewarm_signal,
    cancel as prewarm_cancel,
    snapshot as prewarm_snapshot,
    prewarm_audit,
)


# ═══════════════════════════════════════════════════════════════════════════
# Routing + Code Intelligence + Structured Gen + Hardening
# ═══════════════════════════════════════════════════════════════════════════
from .routing import Orchestrator, Subtask       # noqa: F401
from .code_intel import (                        # noqa: F401
    DependencyTracker, BlastRadius,
    CodeGuard, Finding, ScanResult, Severity, default_guard,
    SelfHealer, TouchFixEngine, HealResult, make_diff, RouteFunc,
    TestLoop, TestIteration, TestLoopResult, ExecuteFunc,
    PRShield, PRShieldInput,
)
from .structured_gen import (                    # noqa: F401
    classify as classify_task, RouteDecision, TASK_CLASSES,
    compile_schema, get_grammar, GRAMMAR_DIR, GRAMMAR_CACHE_DIR,
    verify as verify_local, Verdict, VerifierKind,
)
from .hardening import (                         # noqa: F401
    prune_system, Hyperparams,
    derive as derive_hyperparams,
    payload_extras as hyperparam_extras,
    ast_repair,
    decompose as decompose_task, race_tasks, is_decomposable,
    RING as PROVIDER_RING, TelemetryRing, ProviderEvent,
)


# ═══════════════════════════════════════════════════════════════════════════
# Twin + Cognitive Stack
# ═══════════════════════════════════════════════════════════════════════════
from .twin_engine import (                       # noqa: F401
    TwinEngine, get_twin_engine, twin_engine_snapshot,
    execute_local_inference,
    MAX_RESIDENT_MB, MODEL_ROSTER, ROSTER_BY_ID, ModelSpec, pick_local_model,
)
from .task_shedder import (                      # noqa: F401
    Complexity, assess as assess_complexity,
    should_escalate, should_shed,
)
from .semantic_anchor import compress as compress_anchors, anchors_of  # noqa: F401
from .cognitive_pipeline import run as run_cognitive_pipeline          # noqa: F401
from .reflection import scan as reflect_scan, repair as reflect_repair  # noqa: F401
from .adaptive_control import (                  # noqa: F401
    tune_temperature, AnchorLedger, LEDGER, stops_for,
    StreamQualityGate, GateDecision,
)
from .consensus import (                         # noqa: F401
    similarity, race_consensus, local_polish,
    ConsensusOutcome, PolishResult,
)
from .goat_layer import classify_workload, WorkloadProfile  # noqa: F401


# ═══════════════════════════════════════════════════════════════════════════
# Performance (token, burst, micro-volatile, override, condenser, thermal, bm25, ttc)
# ═══════════════════════════════════════════════════════════════════════════
from .token_governor import estimate_tokens      # noqa: F401
from .burst_scheduler import (                   # noqa: F401
    BurstToken, acquire as burst_acquire, release as burst_release,
    active as burst_active, stats as burst_stats,
    shutdown as burst_shutdown, audit as burst_audit,
)
from .micro_volatile import (                    # noqa: F401
    enter as micro_enter, exit as micro_exit,
    is_active as micro_active, ceiling_mb as micro_ceiling,
    clamp_max_tokens as micro_clamp_tokens,
    clamp_response as micro_clamp_response,
    policy as micro_policy, stats as micro_stats, audit as micro_audit,
)
from .override_timer import (                    # noqa: F401
    issue as override_issue,
    record_decision as override_record,
    should_proceed as override_should_proceed,
    stats as override_stats, audit as override_audit,
)
from .context_condenser import (                 # noqa: F401
    CondenseResult, condense as condense_context,
    prune_whitespace as prune_context_whitespace,
    extract_anchors as extract_context_anchors,
    audit as condenser_audit,
)
from .thermal_guard import (                     # noqa: F401
    start as thermal_start, stop as thermal_stop,
    running as thermal_running, budget as thermal_budget,
    pressure as thermal_pressure, snapshot as thermal_snapshot,
    audit as thermal_audit,
)
from .rank_bm25 import (                         # noqa: F401
    BM25Index, BM25Hit, get_bm25_index, audit as bm25_audit,
)
from .test_time_compute import (                 # noqa: F401
    decide_width as ttc_decide_width,
    run_with_ttc as ttc_run,
    audit as ttc_audit,
)


# ═══════════════════════════════════════════════════════════════════════════
# Persistence
# ═══════════════════════════════════════════════════════════════════════════
from .persistence import (                       # noqa: F401
    PERSIST, PersistenceEngine, UserProfile, ChatMessage,
)
from .supabase_async import (                    # noqa: F401
    track as supabase_track, drain as supabase_drain,
    inflight as supabase_inflight,
)


# ═══════════════════════════════════════════════════════════════════════════
# Anti-hallucination (subpackage)
# ═══════════════════════════════════════════════════════════════════════════
from . import anti_hallucination                 # noqa: F401


# ═══════════════════════════════════════════════════════════════════════════
# Aggregated audit
# ═══════════════════════════════════════════════════════════════════════════
def audit() -> dict[str, Any]:
    """Aggregate audit snapshot from every submodule that exposes one.

    Contract: never raises. Submodules that expose ``audit()`` contribute
    their snapshot; those that don't are silently skipped.
    """
    from . import infra, cache, resilience, hardening, orchestration, config

    out: dict[str, Any] = {"version": _VERSION}
    for name, mod in (
        ("infra", infra),
        ("cache", cache),
        ("resilience", resilience),
        ("hardening", hardening),
        ("orchestration", orchestration),
        ("config", config),
        ("conductor", __import__(
            __name__ + ".quality_conductor", fromlist=["audit"])),
        ("telemetry", __import__(
            __name__ + ".telemetry", fromlist=["audit"])),
        ("compliance", __import__(
            __name__ + ".compliance", fromlist=["audit"])),
        ("goat", __import__(
            __name__ + ".goat_layer", fromlist=["audit"])),
        ("thermal", __import__(
            __name__ + ".thermal_guard", fromlist=["audit"])),
        ("condenser", __import__(
            __name__ + ".context_condenser", fromlist=["audit"])),
        ("burst", __import__(
            __name__ + ".burst_scheduler", fromlist=["audit"])),
        ("micro", __import__(
            __name__ + ".micro_volatile", fromlist=["audit"])),
        ("override", __import__(
            __name__ + ".override_timer", fromlist=["audit"])),
        ("bm25", __import__(
            __name__ + ".rank_bm25", fromlist=["audit"])),
        ("ttc", __import__(
            __name__ + ".test_time_compute", fromlist=["audit"])),
        ("meter", __import__(
            __name__ + ".meter", fromlist=[])),
    ):
        try:
            if hasattr(mod, "audit"):
                out[name] = mod.audit()
        except Exception as e:                       # noqa: BLE001
            out[name] = {"error": str(e)[:120]}
    return out


__all__ = [
    "_VERSION", "audit",
    # Foundation
    "AxelrError", "ProviderError", "DeadlineExceeded", "ValidationFailed",
    "RateLimited", "NoProviderAvailable", "CircuitOpen", "ConfigError",
    "is_retryable", "to_result",
    "load_config", "config_audit",
    "UnifiedCircuitBreaker", "BREAKER",
    "Meter", "METER",
    "RetryPolicy", "with_retry",
    "sanitize", "found_types", "PIIType",
    # Permissions
    "Permission", "UserConsent", "CORE_PERMISSIONS",
    "get_required_permissions", "get_optional_permissions",
    "check_permissions",
    # Conductor
    "run_conductor", "stream_conductor",
    "conductor_audit", "conductor_stats",
    "conductor_reset_stats", "conductor_health",
    # Infra
    "get_redis_cache", "set_redis_cache", "delete_redis_cache",
    "get_redis_client", "cf_embed_batch",
    "CircuitBreaker", "ContextRegistry", "ContextItem",
    # Telemetry
    "TRACES", "TraceRing", "trace_scope",
    "span", "record_stage", "finish", "flush_pending_spans",
    "bind_log", "unbind_log", "current_id", "new_id",
    "log_middleware", "observe_call", "observe_ttft",
    # Heartbeat
    "ProviderHealth", "HEARTBEAT",
    "heartbeat_rank", "heartbeat_score",
    "heartbeat_record_success", "heartbeat_record_failure",
    "heartbeat_snapshot", "heartbeat_reset",
    "heartbeat_install_bridge", "heartbeat_audit",
    # Ghost telemetry
    "DeviceProfile", "RoutingDecision",
    "fingerprint_to_profile", "match_category", "accept_telemetry",
    "telemetry_snapshot", "telemetry_audit",
    # Cache
    "CloudflareVectorizeCache", "CacheEntry", "get_vector_cache",
    "canonical_prefix", "remember_prefix", "recall_prefix",
    "provider_cache_hint", "measure_hit",
    # Streaming
    "CancellationToken", "CancelledByClient", "bind_to_request", "cancellable",
    "ChunkedReader", "read_upload_streaming",
    "InFlightScrubber", "scrub_stream",
    "FirstTokenRace", "RaceOutcome", "StreamerFunc",
    "PII_DETECTED", "iter_payloads", "stream_for_shape", "stream_audit",
    "CHUNK_BYTES", "MAX_INFLIGHT_CHUNKS",
    # Memory + Repo
    "Memory", "MemoryItem", "get_memory",
    "RepoIndexer", "IndexStats", "CodeChunk",
    # Orchestration
    "ResilientPipeline", "ProviderCascade", "get_pipeline",
    "run_pipeline", "stream_pipeline",
    "Tier", "TIERS", "ladder_run",
    "ProviderFunc", "HotSwapResult", "HotSwapDetector",
    "get_hot_swap_detector",
    "hedge_wrap", "orchestration_audit", "pipeline_stats",
    "install_negative_cache", "canonical_system",
    "install_health_propagation", "adapt_max_tokens",
    "record_completion_length",
    "TaskScheduler", "get_task_scheduler", "Priority", "Task",
    # Prewarm
    "prewarm_signal", "prewarm_cancel", "prewarm_snapshot", "prewarm_audit",
    # Routing
    "Orchestrator", "Subtask",
    # Code intel
    "DependencyTracker", "BlastRadius",
    "CodeGuard", "Finding", "ScanResult", "Severity", "default_guard",
    "SelfHealer", "TouchFixEngine", "HealResult", "make_diff", "RouteFunc",
    "TestLoop", "TestIteration", "TestLoopResult", "ExecuteFunc",
    "PRShield", "PRShieldInput",
    # Structured gen
    "classify_task", "RouteDecision", "TASK_CLASSES",
    "compile_schema", "get_grammar", "GRAMMAR_DIR", "GRAMMAR_CACHE_DIR",
    "verify_local", "Verdict", "VerifierKind",
    # Hardening
    "prune_system", "Hyperparams", "derive_hyperparams",
    "hyperparam_extras", "ast_repair", "decompose_task", "race_tasks",
    "is_decomposable", "PROVIDER_RING", "TelemetryRing", "ProviderEvent",
    # Twin
    "TwinEngine", "ModelSpec", "MODEL_ROSTER", "ROSTER_BY_ID",
    "MAX_RESIDENT_MB", "pick_local_model",
    "get_twin_engine", "twin_engine_snapshot", "execute_local_inference",
    # Cognitive
    "Complexity", "assess_complexity", "should_escalate", "should_shed",
    "compress_anchors", "anchors_of",
    "run_cognitive_pipeline",
    "reflect_scan", "reflect_repair",
    "tune_temperature", "AnchorLedger", "LEDGER", "stops_for",
    "StreamQualityGate", "GateDecision",
    "similarity", "race_consensus", "local_polish",
    "ConsensusOutcome", "PolishResult",
    "classify_workload", "WorkloadProfile",
    # Performance
    "estimate_tokens",
    "BurstToken", "burst_acquire", "burst_release",
    "burst_active", "burst_stats", "burst_shutdown", "burst_audit",
    "micro_enter", "micro_exit", "micro_active", "micro_ceiling",
    "micro_clamp_tokens", "micro_clamp_response",
    "micro_policy", "micro_stats", "micro_audit",
    "override_issue", "override_record", "override_should_proceed",
    "override_stats", "override_audit",
    "CondenseResult", "condense_context", "prune_context_whitespace",
    "extract_context_anchors", "condenser_audit",
    "thermal_start", "thermal_stop", "thermal_running",
    "thermal_budget", "thermal_pressure", "thermal_snapshot", "thermal_audit",
    "BM25Index", "BM25Hit", "get_bm25_index", "bm25_audit",
    "ttc_decide_width", "ttc_run", "ttc_audit",
    # Persistence
    "PERSIST", "PersistenceEngine", "UserProfile", "ChatMessage",
    "supabase_track", "supabase_drain", "supabase_inflight",
    # Anti-hallucination
    "anti_hallucination",
=======
# core/__init__.py
"""
AXELR Core — public surface.

Final layout (27 files + 1 package, every file 200–650 LOC):
    infra                Redis · CB · ContextRegistry · CF embed · chaos · state
    observe              traces · OTLP spans · logs · provider + prefix metrics
    cache                prefix cache · semantic cache · counters
    context              intent tree · context window · prompt vault
    stream               chunked reader · scrubber · first-token race · cancel
    streaming_providers  SSE adapters (14 providers)
    memory               Memory + RepoIndexer + chunkers
    resilience           ResilientPipeline (single canonical)
    failover             TIERS · ladder_run · HotSwapDetector
    routing              DAG Orchestrator
    io_pipeline          worker sandbox · webhook pipeline
    code_intel           5-in-1: dep tracker · security · healing · PR · tests
    structured_gen       3-in-1: task router · grammar · verifier
    hardening            5-in-1: prune · hyperparams · ast_repair · split · ring
    elite_orchestrator   hedge · negative cache · health prop · prefix · trim
    task_shedder         complexity gate
    semantic_anchor      anchor compression
    adaptive_control     temperature · ledger · stream gate
    reflection           scan + repair
    cognitive_pipeline   multi-scope pipeline
    consensus            similarity · race consensus · polish
    goat_layer           workload classifier
    token_governor       sliding-window budget
    twin_roster          model specs
    twin_engine          process supervisor
    workspace_engine     TS-mirror emitter
    anti_hallucination   verify-before-output subsystem
"""
from typing import Any

_VERSION = "3.0"

# ── infra ─────────────────────────────────────────────────────────────────
from .infra import (
    CircuitBreaker, ContextItem, ContextRegistry, cf_embed_batch,
    delete_redis_cache, get_redis_cache, get_redis_client, set_redis_cache,
)
# ── observe ───────────────────────────────────────────────────────────────
from .observe import (
    TRACES, TraceRing, trace_scope, span, record_stage, finish,
    bind as bind_log, unbind as unbind_log, current_id, log_middleware,
    observe_call, observe_ttft, flush_pending_spans,
)
# ── cache ─────────────────────────────────────────────────────────────────
from .cache import (
    CacheEntry, CloudflareVectorizeCache, get_vector_cache,
    canonical_prefix, remember_prefix, recall_prefix,
    provider_cache_hint, measure_hit,
)
# ── context ───────────────────────────────────────────────────────────────
from .context import (
    IntentResult, IntentRouter, classify, classify_flat,
    estimate_tokens, clip_history, clip_prompt,
    build as build_prompt_directives,
)
# ── stream ────────────────────────────────────────────────────────────────
from .stream import (
    CancellationToken, CancelledByClient, bind_to_request, cancellable,
    ChunkedReader, read_upload_streaming,
    InFlightScrubber, scrub_stream,
    FirstTokenRace, RaceOutcome, StreamerFunc,
)
from .streaming_providers import PROVIDER_STREAMERS, stream_completion
# ── memory ────────────────────────────────────────────────────────────────
from .memory import (
    Memory, MemoryItem, get_memory,
    RepoIndexer, IndexStats, CodeChunk,
)
# ── resilience + failover ─────────────────────────────────────────────────
from .resilience import (
    ResilientPipeline, ProviderCascade, get_pipeline,
    run_pipeline, stream_pipeline,
)
from .failover import (
    Tier, TIERS, ladder_run,
    ProviderFunc, HotSwapResult, HotSwapDetector, get_hot_swap_detector,
)
# ── routing ───────────────────────────────────────────────────────────────
from .routing import Orchestrator, Subtask
# ── code_intel (5-in-1) ───────────────────────────────────────────────────
from .code_intel import (
    DependencyTracker, BlastRadius,
    CodeGuard, Finding, ScanResult, Severity, default_guard,
    SelfHealer, TouchFixEngine, HealResult, make_diff, RouteFunc,
    TestLoop, TestIteration, TestLoopResult, ExecuteFunc,
    PRShield, PRShieldInput,
)
# ── io_pipeline ───────────────────────────────────────────────────────────
from .io_pipeline import (
    ExtractWebhookPayload, make_webhook_router,
    execute_code_on_worker, close_worker_client,
)
# ── structured_gen (3-in-1) ───────────────────────────────────────────────
from .structured_gen import (
    classify as classify_task, RouteDecision, TASK_CLASSES,
    compile_schema, get_grammar, GRAMMAR_DIR, GRAMMAR_CACHE_DIR,
    verify as verify_local, Verdict, VerifierKind,
)
# ── hardening (5-in-1) ────────────────────────────────────────────────────
from .hardening import (
    prune_system,
    Hyperparams, derive as derive_hyperparams,
    payload_extras as hyperparam_extras,
    ast_repair,
    decompose as decompose_task, race_tasks, is_decomposable,
    RING as PROVIDER_RING, TelemetryRing, ProviderEvent,
)
# ── elite orchestrator ────────────────────────────────────────────────────
from .elite_orchestrator import (
    install as install_elite_orchestrator,
    hedge_wrap, canonical_system, adapt_max_tokens,
    record_token_ratio, audit as elite_audit,
)
# ── cognitive stack ───────────────────────────────────────────────────────
from .twin_engine import (
    TwinEngine, get_twin_engine, twin_engine_snapshot,
    execute_local_inference,
    MAX_RESIDENT_MB, MODEL_ROSTER, ROSTER_BY_ID, ModelSpec, pick_local_model,
)
from .task_shedder import (
    Complexity, assess as assess_complexity,
    should_escalate, should_shed,
)
from .semantic_anchor import compress as compress_anchors, anchors_of
from .cognitive_pipeline import run as run_cognitive_pipeline
from .reflection import scan as reflect_scan, repair as reflect_repair
from .adaptive_control import (
    tune_temperature, AnchorLedger, LEDGER, stops_for,
    StreamQualityGate, GateDecision,
)
from .consensus import (
    similarity, race_consensus, local_polish,
    ConsensusOutcome, PolishResult,
)
from .goat_layer import classify_workload, WorkloadProfile
from .heartbeat import (
    ProviderHealth, HEARTBEAT,
    rank as heartbeat_rank, score as heartbeat_score,
    record_success as heartbeat_record_success,
    record_failure as heartbeat_record_failure,
    snapshot as heartbeat_snapshot,
    reset as heartbeat_reset,
    install_bridge as heartbeat_install_bridge,
    audit as heartbeat_audit,
)
from .ghost_telemetry import (
    DeviceProfile, RoutingDecision,
    fingerprint_to_profile, match_category, accept_telemetry,
    snapshot as telemetry_snapshot,
    audit as telemetry_audit,
)
from .prewarm import (
    signal as prewarm_signal,
    cancel as prewarm_cancel,
    snapshot as prewarm_snapshot,
    audit as prewarm_audit,
)
from .semantic_cache_local import (
    LocalSemanticCache, get_local_cache, audit as local_cache_audit,
)
from .burst_scheduler import (
    BurstToken, acquire as burst_acquire, release as burst_release,
    active as burst_active, stats as burst_stats,
    shutdown as burst_shutdown, audit as burst_audit,
)
from .micro_volatile import (
    enter as micro_enter, exit as micro_exit,
    is_active as micro_active, ceiling_mb as micro_ceiling,
    clamp_max_tokens as micro_clamp_tokens,
    clamp_response as micro_clamp_response,
    policy as micro_policy, stats as micro_stats,
    audit as micro_audit,
)
from .override_timer import (
    issue as override_issue,
    record_decision as override_record,
    should_proceed as override_should_proceed,
    stats as override_stats, audit as override_audit,
)
from .context_condenser import (
    CondenseResult, condense as condense_context,
    prune_whitespace as prune_context_whitespace,
    extract_anchors as extract_context_anchors,
    audit as condenser_audit,
)
from .thermal_guard import (
    start as thermal_start, stop as thermal_stop,
    running as thermal_running, budget as thermal_budget,
    pressure as thermal_pressure, snapshot as thermal_snapshot,
    audit as thermal_audit,
)
from .rank_bm25 import (
    BM25Index, BM25Hit, get_bm25_index, audit as bm25_audit,
)
from .test_time_compute import (
    decide_width as ttc_decide_width,
    run_with_ttc as ttc_run,
    audit as ttc_audit,
)
# ── anti-hallucination ────────────────────────────────────────────────────
from . import anti_hallucination


def audit() -> dict[str, Any]:
    """Aggregate audit snapshot from every submodule that exposes one."""
    from . import infra, observe, cache, resilience, hardening, elite_orchestrator
    out: dict[str, Any] = {"version": _VERSION}
    for name, mod in (
        ("infra", infra), ("observe", observe),
        ("cache", cache), ("resilience", resilience),
        ("hardening", hardening), ("elite", elite_orchestrator),
        ("heartbeat", __import__(__name__ + ".heartbeat", fromlist=["audit"])),
        ("telemetry", __import__(__name__ + ".ghost_telemetry",
                                 fromlist=["audit"])),
        ("prewarm",   __import__(__name__ + ".prewarm",
                                 fromlist=["audit"])),
    ):
        try:
            if hasattr(mod, "audit"):
                out[name] = mod.audit()
        except Exception as e:
            out[name] = {"error": str(e)[:120]}
    return out


__all__ = [
    "_VERSION", "audit",
    # infra
    "get_redis_cache", "set_redis_cache", "delete_redis_cache",
    "get_redis_client", "cf_embed_batch",
    "CircuitBreaker", "ContextRegistry", "ContextItem",
    # observe
    "TRACES", "TraceRing", "trace_scope",
    "span", "record_stage", "finish", "flush_pending_spans",
    "bind_log", "unbind_log", "current_id", "log_middleware",
    "observe_call", "observe_ttft",
    # cache
    "CloudflareVectorizeCache", "CacheEntry", "get_vector_cache",
    "canonical_prefix", "remember_prefix", "recall_prefix",
    "provider_cache_hint", "measure_hit",
    # context
    "IntentResult", "IntentRouter", "classify", "classify_flat",
    "estimate_tokens", "clip_history", "clip_prompt", "build_prompt_directives",
    # stream
    "CancellationToken", "CancelledByClient", "bind_to_request", "cancellable",
    "ChunkedReader", "read_upload_streaming",
    "InFlightScrubber", "scrub_stream",
    "FirstTokenRace", "RaceOutcome", "StreamerFunc",
    # streaming providers
    "PROVIDER_STREAMERS", "stream_completion",
    # memory
    "Memory", "MemoryItem", "get_memory",
    "RepoIndexer", "IndexStats", "CodeChunk",
    # resilience + failover
    "ResilientPipeline", "ProviderCascade", "get_pipeline",
    "run_pipeline", "stream_pipeline",
    "Tier", "TIERS", "ladder_run",
    "ProviderFunc", "HotSwapResult", "HotSwapDetector", "get_hot_swap_detector",
    # routing
    "Orchestrator", "Subtask",
    # code_intel
    "DependencyTracker", "BlastRadius",
    "CodeGuard", "Finding", "ScanResult", "Severity", "default_guard",
    "SelfHealer", "TouchFixEngine", "HealResult", "make_diff", "RouteFunc",
    "TestLoop", "TestIteration", "TestLoopResult", "ExecuteFunc",
    "PRShield", "PRShieldInput",
    # io
    "make_webhook_router", "ExtractWebhookPayload",
    "execute_code_on_worker", "close_worker_client",
    # structured_gen
    "classify_task", "RouteDecision", "TASK_CLASSES",
    "compile_schema", "get_grammar", "GRAMMAR_DIR", "GRAMMAR_CACHE_DIR",
    "verify_local", "Verdict", "VerifierKind",
    # hardening
    "prune_system", "Hyperparams", "derive_hyperparams", "hyperparam_extras",
    "ast_repair", "decompose_task", "race_tasks", "is_decomposable",
    "PROVIDER_RING", "TelemetryRing", "ProviderEvent",
    # elite
    "install_elite_orchestrator", "hedge_wrap", "canonical_system",
    "adapt_max_tokens", "record_token_ratio", "elite_audit",
    # twin
    "TwinEngine", "ModelSpec", "MODEL_ROSTER", "ROSTER_BY_ID",
    "MAX_RESIDENT_MB", "pick_local_model",
    "get_twin_engine", "twin_engine_snapshot", "execute_local_inference",
    # cognitive stack
    "Complexity", "assess_complexity", "should_escalate", "should_shed",
    "compress_anchors", "anchors_of",
    "run_cognitive_pipeline",
    "reflect_scan", "reflect_repair",
    "tune_temperature", "AnchorLedger", "LEDGER", "stops_for",
    "StreamQualityGate", "GateDecision",
    "similarity", "race_consensus", "local_polish",
    "ConsensusOutcome", "PolishResult",
    "classify_workload", "WorkloadProfile",
    # anti-hallucination
    "anti_hallucination",
        # heartbeat
    "ProviderHealth", "HEARTBEAT",
    "heartbeat_rank", "heartbeat_score",
    "heartbeat_record_success", "heartbeat_record_failure",
    "heartbeat_snapshot", "heartbeat_reset",
    "heartbeat_install_bridge", "heartbeat_audit",
    # ghost telemetry
    "DeviceProfile", "RoutingDecision",
    "fingerprint_to_profile", "match_category", "accept_telemetry",
    "telemetry_snapshot", "telemetry_audit",
    # prewarm
    "prewarm_signal", "prewarm_cancel",
    "prewarm_snapshot", "prewarm_audit",
>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
]