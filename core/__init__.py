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
]