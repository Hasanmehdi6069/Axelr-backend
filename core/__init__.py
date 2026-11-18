# core/__init__.py
"""
AXELR Core — Unified Import Surface
=====================================
Single public entry point. Every sub-module is a re-export shell; the
implementation lives in flat files.

Layout (v6.0):
    L0  foundation · compliance
    L1  resilience · observe
    L2  guards · shedder · infra
    L3  cache · persistence
    L4  memory · repo_indexer · context
    L5  condenser · hardening
    L6  goat_layer · consensus · adaptive
    L7  quality_conductor · twin_engine · twin_roster · structured_gen
    L8  routing · gateway · streaming
    L9  code_intel
    L10 io_pipeline · token_tools · ux_input
    Meta model_expertise
"""
from typing import Any

_VERSION = "6.0.0"

# ── L0: Foundation ────────────────────────────────────────────
from .foundation import (
    Settings, load as load_config, reload as reload_config,
    audit as config_audit, initialize_all, get_adapter_statuses,
    AxelrError, ProviderError, DeadlineExceeded, ValidationFailed,
    RateLimited, NoProviderAvailable, CircuitOpen, ConfigError,
    is_retryable, to_result,
    RetryPolicy, with_retry,
    POLICY_DEFAULT, POLICY_NETWORK, POLICY_IDEMPOTENT,
)
from .compliance import (
    sanitize, found_types, PIIType,
    detect_language, detect_language_with_confidence,
    is_rtl, compose_language_directive, extract_lang_tag,
    audit as compliance_audit,
)

# ── L1: Resilience + Observe ─────────────────────────────────
from .resilience import (
    CircuitBreaker, BREAKER, get_breaker,
    Tier as FailoverTier, TIERS, ladder_run,
    HotSwapResult, HotSwapDetector, get_hot_swap_detector,
    WaveRacer, RACER,
    AXELR_COGNITION_LEVEL,
)
from .observe import (
    TraceRing, TRACES, trace_scope, span, record_stage, finish,
    bind, unbind, current_id, new_id as observe_new_id, log_middleware,
    observe_call, observe_ttft, flush_pending_spans,
    Meter, METER,
    arm as memory_tuner_arm, disarm as memory_tuner_disarm,
    trim_middleware, tuner_stats,
    audit as telemetry_audit,
)

# ── L2: Guards · Shedder · Infra ─────────────────────────────
from .guards import (
    start as thermal_start, stop as thermal_stop,
    running as thermal_running, budget as thermal_budget,
    pressure as thermal_pressure, snapshot as thermal_snapshot,
    enter as micro_enter, exit as micro_exit,
    is_active as micro_active, ceiling_mb as micro_ceiling,
    clamp_max_tokens as micro_clamp_tokens,
    clamp_response as micro_clamp_response,
    policy as micro_policy,
    issue as override_issue, record_decision as override_record,
    should_proceed as override_should_proceed,
    BurstToken, burst_acquire, burst_release, burst_active,
    burst_stats, burst_shutdown,
    estimate_tokens as governor_estimate_tokens,
    reserve as governor_reserve, settle as governor_settle,
    snapshot_usage as governor_snapshot,
    audit as guards_audit,
)
from .shedder import (
    Complexity, assess as assess_complexity,
    should_escalate as shedder_should_escalate,
    should_shed as shedder_should_shed,
    adaptive_budget, shed_stats, decide_width,
    audit as shedder_audit,
)
from .infra import (
    get_redis_client, shared_async_cache, init_shared_redis_cache,
    get_redis_cache, set_redis_cache, delete_redis_cache, local_cache,
    ContextItem, ContextRegistry,
    cf_embed_batch,
    audit as infra_audit,
)

# ── L3: Cache · Persistence ─────────────────────────────────
from .cache import (
    canonical_prefix, remember_prefix, recall_prefix,
    provider_cache_hint, measure_hit, prefix_hit, prefix_miss,
    CacheEntry, CloudflareVectorizeCache, get_vector_cache,
    LocalSemanticCache, get_local_cache,
    BM25Index, BM25Hit, get_bm25_index,
    audit as cache_audit,
)
from .persistence import (
    PERSIST, PersistenceEngine, UserProfile, ChatMessage,
)

# ── L4: Memory · RepoIndexer · Context ──────────────────────
from .memory import (
    Memory, MemoryItem, get_memory,
    ConversationMemory, MemoryBus,
    MemoryHit, ConversationMemoryItem,
    _VectorizeClient, _sha,
)
from .repo_indexer import RepoIndexer, IndexStats, CodeChunk
from .context import (
    IntentResult, classify as classify_intent,
    classify_flat, IntentRouter,
    ClipResult, estimate_tokens as context_estimate_tokens,
    clip_history, clip_prompt,
    build as build_vault, install_into_prompts,
)

# ── L5: Condenser · Hardening ───────────────────────────────
from .condenser import (
    CondenseResult, condense, prune_whitespace,
    anchors_of, compress_history, estimate_tokens as condenser_estimate_tokens,
)
from .hardening import (
    prune_system, Hyperparams, derive, payload_extras,
    ast_repair, decompose, race_tasks, is_decomposable,
    RING, TelemetryRing, ProviderEvent,
    audit as hardening_audit,
)

# ── L6: Goat · Consensus · Adaptive ─────────────────────────
from .goat_layer import (
    WorkloadProfile, classify_workload,
    compress_anchors,
    ReflectionIssue, ReflectionResult, local_repair, llm_reflect,
    run_cognitive_pipeline,
    Reflection, scan as reflection_scan, repair as reflection_repair,
    audit as goat_audit,
)
from .consensus import (
    similarity, ConsensusOutcome, race_consensus,
    PolishResult, local_polish,
    Stage, run_cognitive,
)
from .adaptive import (
    tune_temperature,
    AnchorLedger, LEDGER,
    stops_for,
    StreamQualityGate, GateDecision,
    hedge_wrap,
    canonical_system,
    adapt_max_tokens, record_token_ratio,
    audit as adaptive_audit,
)

# ── L7: Conductor · Twin · Structured Gen ───────────────────
from .quality_conductor import (
    run as run_conductor, stream as stream_conductor,
    audit as conductor_audit, stats as conductor_stats,
    reset_stats as conductor_reset_stats, health as conductor_health,
)
from .twin_engine import (
    TwinEngine, get_twin_engine, twin_engine_snapshot,
    execute_local_inference, pick_local_model,
    get_hot_swap_engine, get_hot_swap_engine_async,
    get_twin_engine_async, mark_twin_unavailable,
    twin_health_payload, twin_health_router,
    diagnose_environment, MAX_RESIDENT_MB,
    MODEL_ROSTER, ROSTER_BY_ID, ModelSpec,
)
from .twin_roster import (
    ModelSpec as RosterModelSpec, Category,
    MODEL_ROSTER as ROSTER_CATALOG,
    ROSTER_BY_ID as ROSTER_CATALOG_BY_ID,
    ELITE_MODELS, FAST_MODELS, BLITZ_MODELS, SOLO_MODELS,
    MAX_RESIDENT_MB as ROSTER_MAX_RESIDENT_MB,
    MAX_SOLO_MB, MAX_BLITZ_MB,
    can_serve_concurrent, category_for_concurrency,
)
from .structured_gen import (
    classify as structured_classify,
    RouteDecision, TASK_CLASSES,
    compile_schema, get_grammar,
    GRAMMAR_DIR, GRAMMAR_CACHE_DIR,
    verify as structured_verify, Verdict, VerifierKind,
)

# ── L8: Routing · Gateway · Streaming ───────────────────────
from .routing import (
    Orchestrator, Subtask,
    PLANNER_PROMPT, CRITIC_PROMPT, REVISION_PROMPT, SYNTH_PROMPT,
    ROLE_SYSTEMS,
)
from .gateway import (
    AsyncAxelrClient, ChatCompletion, ChatCompletionChunk,
    GatewayRouter, GatewayEndpoint, get_gateway_router,
    to_openai, to_anthropic, to_gemini,
    from_openai_response, from_anthropic_response, from_gemini_response,
    detect_shape, build_payload, parse_response,
    audit as gateway_audit,
)
# Streaming primitives are in `streaming.py` (merged in next step)
from .streaming import (
    CHUNK_BYTES, MAX_INFLIGHT_CHUNKS,
    ChunkedReader, read_upload_streaming,
    InFlightScrubber, scrub_stream,
    FirstTokenRace, RaceOutcome, StreamerFunc,
    CancellationToken, CancelledByClient, bind_to_request, cancellable,
    PROVIDER_STREAMERS, stream_completion,
)

# ── L9: Code Intel ──────────────────────────────────────────
from .code_intel import (
    DependencyTracker, BlastRadius,
    CodeGuard, Finding, ScanResult, Severity, default_guard,
    SelfHealer, TouchFixEngine, HealResult, make_diff, RouteFunc,
    PRShield, PRShieldInput,
    TestLoop, TestIteration, TestLoopResult, ExecuteFunc,
)

# ── L10: I/O · Token Tools · UX ─────────────────────────────
from .io_pipeline import (
    execute_code_on_worker, close_worker_client,
    ExtractWebhookPayload, make_webhook_router,
)
from .token_tools import (
    get_engine as get_biasing_engine,
    TokenBiasingEngine, TokenBiasingConfig, ContextWindow,
    NVIDIAInferenceStream, IsolatedSandbox,
    FinancialStatePipeline, EdgeCloudOrchestrator,
)
from .ux_input import (
    TextRingBuffer, LexicalTrie, ContextualTrie,
    PolyfillCore, IntentParser, HomophoneCorrector, UXComponents,
)

# ── Meta: Model expertise ───────────────────────────────────
from .model_expertise import (
    route as route_expertise, snapshot as expertise_snapshot,
    audit as expertise_audit,
)

# ── L11: Ghost-Agent Autonomous Execution Engine ───────────
from .ghost_agent import (
    AxelrGhostAgent,
    GhostAgentExecutionError,
    StagedOutput,
    VerificationAuditTrail,
    DataPoint,
    ConfidenceRating,
    get_ghost_agent,
)


def audit() -> dict[str, Any]:
    """Aggregate audit across every module that exposes one."""
    out: dict[str, Any] = {"version": _VERSION}
    for name, mod in (
        ("foundation",    __import__(__name__ + ".foundation", fromlist=["audit"])),
        ("compliance",    __import__(__name__ + ".compliance", fromlist=["audit"])),
        ("resilience",    __import__(__name__ + ".resilience", fromlist=["audit"])
                          if hasattr(__import__(__name__ + ".resilience", fromlist=["x"]), "audit") else None),
        ("observe",       __import__(__name__ + ".observe", fromlist=["audit"])),
        ("guards",        __import__(__name__ + ".guards", fromlist=["audit"])),
        ("shedder",       __import__(__name__ + ".shedder", fromlist=["audit"])),
        ("infra",         __import__(__name__ + ".infra", fromlist=["audit"])),
        ("cache",         __import__(__name__ + ".cache", fromlist=["audit"])),
        ("conductor",     __import__(__name__ + ".quality_conductor", fromlist=["audit"])),
        ("goat",          __import__(__name__ + ".goat_layer", fromlist=["audit"])),
        ("adaptive",      __import__(__name__ + ".adaptive", fromlist=["audit"])),
        ("hardening",     __import__(__name__ + ".hardening", fromlist=["audit"])),
        ("gateway",       __import__(__name__ + ".gateway", fromlist=["audit"])),
        ("expertise",     __import__(__name__ + ".model_expertise", fromlist=["audit"])),
        ("ghost_agent",   __import__(__name__ + ".ghost_agent", fromlist=["audit"]) 
                          if hasattr(__import__(__name__ + ".ghost_agent", fromlist=["x"]), "audit") else None),
    ):
        try:
            if mod is not None and hasattr(mod, "audit"):
                out[name] = mod.audit()
        except Exception as e:
            out[name] = {"error": str(e)[:120]}
    return out


__all__ = [
    "_VERSION", "audit",
    # Foundation
    "Settings", "load_config", "reload_config", "config_audit",
    "initialize_all", "get_adapter_statuses",
    "AxelrError", "ProviderError", "DeadlineExceeded", "ValidationFailed",
    "RateLimited", "NoProviderAvailable", "CircuitOpen", "ConfigError",
    "is_retryable", "to_result",
    "RetryPolicy", "with_retry",
    "POLICY_DEFAULT", "POLICY_NETWORK", "POLICY_IDEMPOTENT",
    # Compliance
    "sanitize", "found_types", "PIIType",
    "detect_language", "detect_language_with_confidence",
    "is_rtl", "compose_language_directive", "extract_lang_tag",
    "compliance_audit",
    # Resilience
    "CircuitBreaker", "BREAKER", "get_breaker",
    "FailoverTier", "TIERS", "ladder_run",
    "HotSwapResult", "HotSwapDetector", "get_hot_swap_detector",
    "WaveRacer", "RACER",
    # Observe
    "TraceRing", "TRACES", "trace_scope", "span", "record_stage", "finish",
    "bind", "unbind", "current_id", "observe_new_id", "log_middleware",
    "observe_call", "observe_ttft", "flush_pending_spans",
    "Meter", "METER",
    "memory_tuner_arm", "memory_tuner_disarm", "trim_middleware", "tuner_stats",
    "telemetry_audit",
    # Guards
    "thermal_start", "thermal_stop", "thermal_running", "thermal_budget",
    "thermal_pressure", "thermal_snapshot",
    "micro_enter", "micro_exit", "micro_active", "micro_ceiling",
    "micro_clamp_tokens", "micro_clamp_response", "micro_policy",
    "override_issue", "override_record", "override_should_proceed",
    "BurstToken", "burst_acquire", "burst_release", "burst_active",
    "burst_stats", "burst_shutdown",
    "governor_estimate_tokens", "governor_reserve", "governor_settle",
    "governor_snapshot", "guards_audit",
    # Shedder
    "Complexity", "assess_complexity", "shedder_should_escalate",
    "shedder_should_shed", "adaptive_budget", "shed_stats", "decide_width",
    "shedder_audit",
    # Infra
    "get_redis_client", "shared_async_cache", "init_shared_redis_cache",
    "get_redis_cache", "set_redis_cache", "delete_redis_cache", "local_cache",
    "ContextItem", "ContextRegistry", "cf_embed_batch", "infra_audit",
    # Cache
    "canonical_prefix", "remember_prefix", "recall_prefix",
    "provider_cache_hint", "measure_hit", "prefix_hit", "prefix_miss",
    "CacheEntry", "CloudflareVectorizeCache", "get_vector_cache",
    "LocalSemanticCache", "get_local_cache",
    "BM25Index", "BM25Hit", "get_bm25_index", "cache_audit",
    # Persistence
    "PERSIST", "PersistenceEngine", "UserProfile", "ChatMessage",
    # Memory
    "Memory", "MemoryItem", "get_memory",
    "ConversationMemory", "MemoryBus", "MemoryHit", "ConversationMemoryItem",
    # Repo Indexer
    "RepoIndexer", "IndexStats", "CodeChunk",
    # Context
    "IntentResult", "classify_intent", "classify_flat", "IntentRouter",
    "ClipResult", "context_estimate_tokens", "clip_history", "clip_prompt",
    "build_vault", "install_into_prompts",
    # Condenser
    "CondenseResult", "condense", "prune_whitespace",
    "anchors_of", "compress_history", "condenser_estimate_tokens",
    # Hardening
    "prune_system", "Hyperparams", "derive", "payload_extras",
    "ast_repair", "decompose", "race_tasks", "is_decomposable",
    "RING", "TelemetryRing", "ProviderEvent", "hardening_audit",
    # Goat
    "WorkloadProfile", "classify_workload", "compress_anchors",
    "ReflectionIssue", "ReflectionResult", "local_repair", "llm_reflect",
    "run_cognitive_pipeline",
    "Reflection", "reflection_scan", "reflection_repair", "goat_audit",
    # Consensus
    "similarity", "ConsensusOutcome", "race_consensus",
    "PolishResult", "local_polish", "Stage", "run_cognitive",
    # Adaptive
    "tune_temperature", "AnchorLedger", "LEDGER", "stops_for",
    "StreamQualityGate", "GateDecision", "hedge_wrap", "canonical_system",
    "adapt_max_tokens", "record_token_ratio", "adaptive_audit",
    # Conductor
    "run_conductor", "stream_conductor", "conductor_audit",
    "conductor_stats", "conductor_reset_stats", "conductor_health",
    # Twin
    "TwinEngine", "get_twin_engine", "twin_engine_snapshot",
    "execute_local_inference", "pick_local_model",
    "get_hot_swap_engine", "get_hot_swap_engine_async",
    "get_twin_engine_async", "mark_twin_unavailable",
    "twin_health_payload", "twin_health_router",
    "diagnose_environment", "MAX_RESIDENT_MB",
    "MODEL_ROSTER", "ROSTER_BY_ID", "ModelSpec",
    "RosterModelSpec", "Category", "ROSTER_CATALOG", "ROSTER_CATALOG_BY_ID",
    "ELITE_MODELS", "FAST_MODELS", "BLITZ_MODELS", "SOLO_MODELS",
    "ROSTER_MAX_RESIDENT_MB", "MAX_SOLO_MB", "MAX_BLITZ_MB",
    "can_serve_concurrent", "category_for_concurrency",
    # Structured Gen
    "structured_classify", "RouteDecision", "TASK_CLASSES",
    "compile_schema", "get_grammar", "GRAMMAR_DIR", "GRAMMAR_CACHE_DIR",
    "structured_verify", "Verdict", "VerifierKind",
    # Routing
    "Orchestrator", "Subtask",
    "PLANNER_PROMPT", "CRITIC_PROMPT", "REVISION_PROMPT", "SYNTH_PROMPT",
    "ROLE_SYSTEMS",
    # Gateway
    "AsyncAxelrClient", "ChatCompletion", "ChatCompletionChunk",
    "GatewayRouter", "GatewayEndpoint", "get_gateway_router",
    "to_openai", "to_anthropic", "to_gemini",
    "from_openai_response", "from_anthropic_response", "from_gemini_response",
    "detect_shape", "build_payload", "parse_response", "gateway_audit",
    # Streaming
    "CHUNK_BYTES", "MAX_INFLIGHT_CHUNKS",
    "ChunkedReader", "read_upload_streaming",
    "InFlightScrubber", "scrub_stream",
    "FirstTokenRace", "RaceOutcome", "StreamerFunc",
    "CancellationToken", "CancelledByClient", "bind_to_request", "cancellable",
    "PROVIDER_STREAMERS", "stream_completion",
    # Code Intel
    "DependencyTracker", "BlastRadius",
    "CodeGuard", "Finding", "ScanResult", "Severity", "default_guard",
    "SelfHealer", "TouchFixEngine", "HealResult", "make_diff", "RouteFunc",
    "PRShield", "PRShieldInput",
    "TestLoop", "TestIteration", "TestLoopResult", "ExecuteFunc",
    # I/O
    "execute_code_on_worker", "close_worker_client",
    "ExtractWebhookPayload", "make_webhook_router",
    # Token Tools
    "get_biasing_engine", "TokenBiasingEngine", "TokenBiasingConfig",
    "ContextWindow", "CharCNN",
    "NVIDIAInferenceStream", "IsolatedSandbox",
    "FinancialStatePipeline", "EdgeCloudOrchestrator",
    # UX
    "TextRingBuffer", "LexicalTrie", "ContextualTrie",
    "PolyfillCore", "IntentParser", "HomophoneCorrector", "UXComponents",
    # Ghost-Agent
    "AxelrGhostAgent", "GhostAgentExecutionError",
    "StagedOutput", "VerificationAuditTrail", "DataPoint",
    "ConfidenceRating", "get_ghost_agent",
    # Meta
    "route_expertise", "expertise_snapshot", "expertise_audit",
]