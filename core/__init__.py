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
from .adaptive import (
    LEDGER,
    AnchorLedger,
    GateDecision,
    StreamQualityGate,
    adapt_max_tokens,
    canonical_system,
    hedge_wrap,
    record_token_ratio,
    stops_for,
    tune_temperature,
)
from .adaptive import (
    audit as adaptive_audit,
)

# ── L3: Cache · Persistence ─────────────────────────────────
from .cache import (
    BM25Hit,
    BM25Index,
    CacheEntry,
    CloudflareVectorizeCache,
    LocalSemanticCache,
    canonical_prefix,
    get_bm25_index,
    get_local_cache,
    get_vector_cache,
    measure_hit,
    prefix_hit,
    prefix_miss,
    provider_cache_hint,
    recall_prefix,
    remember_prefix,
)
from .cache import (
    audit as cache_audit,
)

# ── L9: Code Intel ──────────────────────────────────────────
from .code_intel import (
    BlastRadius,
    CodeGuard,
    DependencyTracker,
    ExecuteFunc,
    Finding,
    HealResult,
    PRShield,
    PRShieldInput,
    RouteFunc,
    ScanResult,
    SelfHealer,
    Severity,
    TestIteration,
    TestLoop,
    TestLoopResult,
    TouchFixEngine,
    default_guard,
    make_diff,
)
from .compliance import (
    PIIType,
    compose_language_directive,
    detect_language,
    detect_language_with_confidence,
    extract_lang_tag,
    found_types,
    is_rtl,
    sanitize,
)
from .compliance import (
    audit as compliance_audit,
)

# ── L5: Condenser · Hardening ───────────────────────────────
from .condenser import (
    CondenseResult,
    anchors_of,
    compress_history,
    condense,
    prune_whitespace,
)
from .condenser import (
    estimate_tokens as condenser_estimate_tokens,
)
from .consensus import (
    ConsensusOutcome,
    PolishResult,
    Stage,
    local_polish,
    race_consensus,
    run_cognitive,
    similarity,
)
from .context import (
    ClipResult,
    IntentResult,
    IntentRouter,
    classify_flat,
    clip_history,
    clip_prompt,
    install_into_prompts,
)
from .context import (
    build as build_vault,
)
from .context import (
    classify as classify_intent,
)
from .context import (
    estimate_tokens as context_estimate_tokens,
)
from .foundation import (
    POLICY_DEFAULT,
    POLICY_IDEMPOTENT,
    POLICY_NETWORK,
    AxelrError,
    CircuitOpen,
    ConfigError,
    DeadlineExceeded,
    NoProviderAvailable,
    ProviderError,
    RateLimited,
    RetryPolicy,
    Settings,
    ValidationFailed,
    get_adapter_statuses,
    initialize_all,
    is_retryable,
    to_result,
    with_retry,
)
from .foundation import (
    audit as config_audit,
)
from .foundation import (
    load as load_config,
)
from .foundation import (
    reload as reload_config,
)
from .gateway import (
    AsyncAxelrClient,
    ChatCompletion,
    ChatCompletionChunk,
    GatewayEndpoint,
    GatewayRouter,
    build_payload,
    detect_shape,
    from_anthropic_response,
    from_gemini_response,
    from_openai_response,
    get_gateway_router,
    parse_response,
    to_anthropic,
    to_gemini,
    to_openai,
)
from .gateway import (
    audit as gateway_audit,
)

# ── L11: Ghost-Agent Autonomous Execution Engine ───────────
from .ghost_agent import (
    AxelrGhostAgent,
    ConfidenceRating,
    DataPoint,
    GhostAgentExecutionError,
    StagedOutput,
    VerificationAuditTrail,
    get_ghost_agent,
)

# ── L6: Goat · Consensus · Adaptive ─────────────────────────
from .goat_layer import (
    Reflection,
    ReflectionIssue,
    ReflectionResult,
    WorkloadProfile,
    classify_workload,
    compress_anchors,
    llm_reflect,
    local_repair,
    run_cognitive_pipeline,
)
from .goat_layer import (
    audit as goat_audit,
)
from .goat_layer import (
    repair as reflection_repair,
)
from .goat_layer import (
    scan as reflection_scan,
)
from .guards import (
    BurstToken,
    burst_acquire,
    burst_active,
    burst_release,
    burst_shutdown,
    burst_stats,
)
from .guards import (
    audit as guards_audit,
)
from .guards import (
    budget as thermal_budget,
)
from .guards import (
    ceiling_mb as micro_ceiling,
)
from .guards import (
    clamp_max_tokens as micro_clamp_tokens,
)
from .guards import (
    clamp_response as micro_clamp_response,
)
from .guards import (
    enter as micro_enter,
)
from .guards import (
    estimate_tokens as governor_estimate_tokens,
)
from .guards import (
    exit as micro_exit,
)
from .guards import (
    is_active as micro_active,
)
from .guards import (
    issue as override_issue,
)
from .guards import (
    policy as micro_policy,
)
from .guards import (
    pressure as thermal_pressure,
)
from .guards import (
    record_decision as override_record,
)
from .guards import (
    reserve as governor_reserve,
)
from .guards import (
    running as thermal_running,
)
from .guards import (
    settle as governor_settle,
)
from .guards import (
    should_proceed as override_should_proceed,
)
from .guards import (
    snapshot as thermal_snapshot,
)
from .guards import (
    snapshot_usage as governor_snapshot,
)

# ── L2: Guards · Shedder · Infra ─────────────────────────────
from .guards import (
    start as thermal_start,
)
from .guards import (
    stop as thermal_stop,
)
from .hardening import (
    RING,
    Hyperparams,
    ProviderEvent,
    TelemetryRing,
    ast_repair,
    decompose,
    derive,
    is_decomposable,
    payload_extras,
    prune_system,
    race_tasks,
)
from .hardening import (
    audit as hardening_audit,
)
from .infra import (
    ContextItem,
    ContextRegistry,
    cf_embed_batch,
    delete_redis_cache,
    get_redis_cache,
    get_redis_client,
    init_shared_redis_cache,
    local_cache,
    set_redis_cache,
    shared_async_cache,
)
from .infra import (
    audit as infra_audit,
)

# ── L10: I/O · Token Tools · UX ─────────────────────────────
from .io_pipeline import (
    ExtractWebhookPayload,
    close_worker_client,
    execute_code_on_worker,
    make_webhook_router,
)

# ── L4: Memory · RepoIndexer · Context ──────────────────────
from .memory import (
    ConversationMemory,
    ConversationMemoryItem,
    Memory,
    MemoryBus,
    MemoryHit,
    MemoryItem,
    _sha,
    _VectorizeClient,
    get_memory,
)
from .model_expertise import (
    audit as expertise_audit,
)

# ── Meta: Model expertise ───────────────────────────────────
from .model_expertise import (
    route as route_expertise,
)
from .model_expertise import (
    snapshot as expertise_snapshot,
)
from .observe import (
    METER,
    TRACES,
    Meter,
    TraceRing,
    bind,
    current_id,
    finish,
    flush_pending_spans,
    log_middleware,
    observe_call,
    observe_ttft,
    record_stage,
    span,
    trace_scope,
    trim_middleware,
    tuner_stats,
    unbind,
)
from .observe import (
    arm as memory_tuner_arm,
)
from .observe import (
    audit as telemetry_audit,
)
from .observe import (
    disarm as memory_tuner_disarm,
)
from .observe import (
    new_id as observe_new_id,
)
from .persistence import (
    PERSIST,
    ChatMessage,
    PersistenceEngine,
    UserProfile,
)
from .quality_conductor import (
    audit as conductor_audit,
)
from .quality_conductor import (
    health as conductor_health,
)
from .quality_conductor import (
    reset_stats as conductor_reset_stats,
)

# ── L7: Conductor · Twin · Structured Gen ───────────────────
from .quality_conductor import (
    run as run_conductor,
)
from .quality_conductor import (
    stats as conductor_stats,
)
from .quality_conductor import (
    stream as stream_conductor,
)
from .repo_indexer import CodeChunk, IndexStats, RepoIndexer

# ── L1: Resilience + Observe ─────────────────────────────────
from .resilience import (
    AXELR_COGNITION_LEVEL,
    BREAKER,
    RACER,
    TIERS,
    CircuitBreaker,
    HotSwapDetector,
    HotSwapResult,
    WaveRacer,
    get_breaker,
    get_hot_swap_detector,
    ladder_run,
)
from .resilience import (
    Tier as FailoverTier,
)

# ── L8: Routing · Gateway · Streaming ───────────────────────
from .routing import (
    CRITIC_PROMPT,
    PLANNER_PROMPT,
    REVISION_PROMPT,
    ROLE_SYSTEMS,
    SYNTH_PROMPT,
    Orchestrator,
    Subtask,
)
from .shedder import (
    Complexity,
    adaptive_budget,
    decide_width,
    shed_stats,
)
from .shedder import (
    assess as assess_complexity,
)
from .shedder import (
    audit as shedder_audit,
)
from .shedder import (
    should_escalate as shedder_should_escalate,
)
from .shedder import (
    should_shed as shedder_should_shed,
)

# Streaming primitives are in `streaming.py` (merged in next step)
from .streaming import (
    CHUNK_BYTES,
    MAX_INFLIGHT_CHUNKS,
    PROVIDER_STREAMERS,
    CancellationToken,
    CancelledByClient,
    ChunkedReader,
    FirstTokenRace,
    InFlightScrubber,
    RaceOutcome,
    StreamerFunc,
    bind_to_request,
    cancellable,
    read_upload_streaming,
    scrub_stream,
    stream_completion,
)
from .structured_gen import (
    GRAMMAR_CACHE_DIR,
    GRAMMAR_DIR,
    TASK_CLASSES,
    RouteDecision,
    Verdict,
    VerifierKind,
    compile_schema,
    get_grammar,
)
from .structured_gen import (
    classify as structured_classify,
)
from .structured_gen import (
    verify as structured_verify,
)
from .token_tools import (
    ContextWindow,
    EdgeCloudOrchestrator,
    FinancialStatePipeline,
    IsolatedSandbox,
    NVIDIAInferenceStream,
    TokenBiasingConfig,
    TokenBiasingEngine,
)
from .token_tools import (
    get_engine as get_biasing_engine,
)
from .twin_engine import (
    MAX_RESIDENT_MB,
    MODEL_ROSTER,
    ROSTER_BY_ID,
    ModelSpec,
    TwinEngine,
    diagnose_environment,
    execute_local_inference,
    get_hot_swap_engine,
    get_hot_swap_engine_async,
    get_twin_engine,
    get_twin_engine_async,
    mark_twin_unavailable,
    pick_local_model,
    twin_engine_snapshot,
    twin_health_payload,
    twin_health_router,
)
from .twin_roster import (
    BLITZ_MODELS,
    ELITE_MODELS,
    FAST_MODELS,
    MAX_BLITZ_MB,
    MAX_SOLO_MB,
    SOLO_MODELS,
    Category,
    can_serve_concurrent,
    category_for_concurrency,
)
from .twin_roster import (
    MAX_RESIDENT_MB as ROSTER_MAX_RESIDENT_MB,
)
from .twin_roster import (
    MODEL_ROSTER as ROSTER_CATALOG,
)
from .twin_roster import (
    ROSTER_BY_ID as ROSTER_CATALOG_BY_ID,
)
from .twin_roster import (
    ModelSpec as RosterModelSpec,
)
from .ux_input import (
    ContextualTrie,
    HomophoneCorrector,
    IntentParser,
    LexicalTrie,
    PolyfillCore,
    TextRingBuffer,
    UXComponents,
)


def audit() -> dict[str, Any]:
    """Aggregate audit across every module that exposes one."""
    out: dict[str, Any] = {"version": _VERSION}
    for name, mod in (
        ("foundation", __import__(__name__ + ".foundation", fromlist=["audit"])),
        ("compliance", __import__(__name__ + ".compliance", fromlist=["audit"])),
        (
            "resilience",
            __import__(__name__ + ".resilience", fromlist=["audit"])
            if hasattr(__import__(__name__ + ".resilience", fromlist=["x"]), "audit")
            else None,
        ),
        ("observe", __import__(__name__ + ".observe", fromlist=["audit"])),
        ("guards", __import__(__name__ + ".guards", fromlist=["audit"])),
        ("shedder", __import__(__name__ + ".shedder", fromlist=["audit"])),
        ("infra", __import__(__name__ + ".infra", fromlist=["audit"])),
        ("cache", __import__(__name__ + ".cache", fromlist=["audit"])),
        ("conductor", __import__(__name__ + ".quality_conductor", fromlist=["audit"])),
        ("goat", __import__(__name__ + ".goat_layer", fromlist=["audit"])),
        ("adaptive", __import__(__name__ + ".adaptive", fromlist=["audit"])),
        ("hardening", __import__(__name__ + ".hardening", fromlist=["audit"])),
        ("gateway", __import__(__name__ + ".gateway", fromlist=["audit"])),
        ("expertise", __import__(__name__ + ".model_expertise", fromlist=["audit"])),
        (
            "ghost_agent",
            __import__(__name__ + ".ghost_agent", fromlist=["audit"])
            if hasattr(__import__(__name__ + ".ghost_agent", fromlist=["x"]), "audit")
            else None,
        ),
    ):
        try:
            if mod is not None and hasattr(mod, "audit"):
                out[name] = mod.audit()
        except Exception as e:
            out[name] = {"error": str(e)[:120]}
    return out


__all__ = [
    "_VERSION",
    "audit",
    # Foundation
    "Settings",
    "load_config",
    "reload_config",
    "config_audit",
    "initialize_all",
    "get_adapter_statuses",
    "AxelrError",
    "ProviderError",
    "DeadlineExceeded",
    "ValidationFailed",
    "RateLimited",
    "NoProviderAvailable",
    "CircuitOpen",
    "ConfigError",
    "is_retryable",
    "to_result",
    "RetryPolicy",
    "with_retry",
    "POLICY_DEFAULT",
    "POLICY_NETWORK",
    "POLICY_IDEMPOTENT",
    # Compliance
    "sanitize",
    "found_types",
    "PIIType",
    "detect_language",
    "detect_language_with_confidence",
    "is_rtl",
    "compose_language_directive",
    "extract_lang_tag",
    "compliance_audit",
    # Resilience
    "CircuitBreaker",
    "BREAKER",
    "get_breaker",
    "FailoverTier",
    "TIERS",
    "ladder_run",
    "HotSwapResult",
    "HotSwapDetector",
    "get_hot_swap_detector",
    "WaveRacer",
    "RACER",
    # Observe
    "TraceRing",
    "TRACES",
    "trace_scope",
    "span",
    "record_stage",
    "finish",
    "bind",
    "unbind",
    "current_id",
    "observe_new_id",
    "log_middleware",
    "observe_call",
    "observe_ttft",
    "flush_pending_spans",
    "Meter",
    "METER",
    "memory_tuner_arm",
    "memory_tuner_disarm",
    "trim_middleware",
    "tuner_stats",
    "telemetry_audit",
    # Guards
    "thermal_start",
    "thermal_stop",
    "thermal_running",
    "thermal_budget",
    "thermal_pressure",
    "thermal_snapshot",
    "micro_enter",
    "micro_exit",
    "micro_active",
    "micro_ceiling",
    "micro_clamp_tokens",
    "micro_clamp_response",
    "micro_policy",
    "override_issue",
    "override_record",
    "override_should_proceed",
    "BurstToken",
    "burst_acquire",
    "burst_release",
    "burst_active",
    "burst_stats",
    "burst_shutdown",
    "governor_estimate_tokens",
    "governor_reserve",
    "governor_settle",
    "governor_snapshot",
    "guards_audit",
    # Shedder
    "Complexity",
    "assess_complexity",
    "shedder_should_escalate",
    "shedder_should_shed",
    "adaptive_budget",
    "shed_stats",
    "decide_width",
    "shedder_audit",
    # Infra
    "get_redis_client",
    "shared_async_cache",
    "init_shared_redis_cache",
    "get_redis_cache",
    "set_redis_cache",
    "delete_redis_cache",
    "local_cache",
    "ContextItem",
    "ContextRegistry",
    "cf_embed_batch",
    "infra_audit",
    # Cache
    "canonical_prefix",
    "remember_prefix",
    "recall_prefix",
    "provider_cache_hint",
    "measure_hit",
    "prefix_hit",
    "prefix_miss",
    "CacheEntry",
    "CloudflareVectorizeCache",
    "get_vector_cache",
    "LocalSemanticCache",
    "get_local_cache",
    "BM25Index",
    "BM25Hit",
    "get_bm25_index",
    "cache_audit",
    # Persistence
    "PERSIST",
    "PersistenceEngine",
    "UserProfile",
    "ChatMessage",
    # Memory
    "Memory",
    "MemoryItem",
    "get_memory",
    "ConversationMemory",
    "MemoryBus",
    "MemoryHit",
    "ConversationMemoryItem",
    # Repo Indexer
    "RepoIndexer",
    "IndexStats",
    "CodeChunk",
    # Context
    "IntentResult",
    "classify_intent",
    "classify_flat",
    "IntentRouter",
    "ClipResult",
    "context_estimate_tokens",
    "clip_history",
    "clip_prompt",
    "build_vault",
    "install_into_prompts",
    # Condenser
    "CondenseResult",
    "condense",
    "prune_whitespace",
    "anchors_of",
    "compress_history",
    "condenser_estimate_tokens",
    # Hardening
    "prune_system",
    "Hyperparams",
    "derive",
    "payload_extras",
    "ast_repair",
    "decompose",
    "race_tasks",
    "is_decomposable",
    "RING",
    "TelemetryRing",
    "ProviderEvent",
    "hardening_audit",
    # Goat
    "WorkloadProfile",
    "classify_workload",
    "compress_anchors",
    "ReflectionIssue",
    "ReflectionResult",
    "local_repair",
    "llm_reflect",
    "run_cognitive_pipeline",
    "Reflection",
    "reflection_scan",
    "reflection_repair",
    "goat_audit",
    # Consensus
    "similarity",
    "ConsensusOutcome",
    "race_consensus",
    "PolishResult",
    "local_polish",
    "Stage",
    "run_cognitive",
    # Adaptive
    "tune_temperature",
    "AnchorLedger",
    "LEDGER",
    "stops_for",
    "StreamQualityGate",
    "GateDecision",
    "hedge_wrap",
    "canonical_system",
    "adapt_max_tokens",
    "record_token_ratio",
    "adaptive_audit",
    # Conductor
    "run_conductor",
    "stream_conductor",
    "conductor_audit",
    "conductor_stats",
    "conductor_reset_stats",
    "conductor_health",
    # Twin
    "TwinEngine",
    "get_twin_engine",
    "twin_engine_snapshot",
    "execute_local_inference",
    "pick_local_model",
    "get_hot_swap_engine",
    "get_hot_swap_engine_async",
    "get_twin_engine_async",
    "mark_twin_unavailable",
    "twin_health_payload",
    "twin_health_router",
    "diagnose_environment",
    "MAX_RESIDENT_MB",
    "MODEL_ROSTER",
    "ROSTER_BY_ID",
    "ModelSpec",
    "RosterModelSpec",
    "Category",
    "ROSTER_CATALOG",
    "ROSTER_CATALOG_BY_ID",
    "ELITE_MODELS",
    "FAST_MODELS",
    "BLITZ_MODELS",
    "SOLO_MODELS",
    "ROSTER_MAX_RESIDENT_MB",
    "MAX_SOLO_MB",
    "MAX_BLITZ_MB",
    "can_serve_concurrent",
    "category_for_concurrency",
    # Structured Gen
    "structured_classify",
    "RouteDecision",
    "TASK_CLASSES",
    "compile_schema",
    "get_grammar",
    "GRAMMAR_DIR",
    "GRAMMAR_CACHE_DIR",
    "structured_verify",
    "Verdict",
    "VerifierKind",
    # Routing
    "Orchestrator",
    "Subtask",
    "PLANNER_PROMPT",
    "CRITIC_PROMPT",
    "REVISION_PROMPT",
    "SYNTH_PROMPT",
    "ROLE_SYSTEMS",
    # Gateway
    "AsyncAxelrClient",
    "ChatCompletion",
    "ChatCompletionChunk",
    "GatewayRouter",
    "GatewayEndpoint",
    "get_gateway_router",
    "to_openai",
    "to_anthropic",
    "to_gemini",
    "from_openai_response",
    "from_anthropic_response",
    "from_gemini_response",
    "detect_shape",
    "build_payload",
    "parse_response",
    "gateway_audit",
    # Streaming
    "CHUNK_BYTES",
    "MAX_INFLIGHT_CHUNKS",
    "ChunkedReader",
    "read_upload_streaming",
    "InFlightScrubber",
    "scrub_stream",
    "FirstTokenRace",
    "RaceOutcome",
    "StreamerFunc",
    "CancellationToken",
    "CancelledByClient",
    "bind_to_request",
    "cancellable",
    "PROVIDER_STREAMERS",
    "stream_completion",
    # Code Intel
    "DependencyTracker",
    "BlastRadius",
    "CodeGuard",
    "Finding",
    "ScanResult",
    "Severity",
    "default_guard",
    "SelfHealer",
    "TouchFixEngine",
    "HealResult",
    "make_diff",
    "RouteFunc",
    "PRShield",
    "PRShieldInput",
    "TestLoop",
    "TestIteration",
    "TestLoopResult",
    "ExecuteFunc",
    # I/O
    "execute_code_on_worker",
    "close_worker_client",
    "ExtractWebhookPayload",
    "make_webhook_router",
    # Token Tools
    "get_biasing_engine",
    "TokenBiasingEngine",
    "TokenBiasingConfig",
    "ContextWindow",
    "CharCNN",
    "NVIDIAInferenceStream",
    "IsolatedSandbox",
    "FinancialStatePipeline",
    "EdgeCloudOrchestrator",
    # UX
    "TextRingBuffer",
    "LexicalTrie",
    "ContextualTrie",
    "PolyfillCore",
    "IntentParser",
    "HomophoneCorrector",
    "UXComponents",
    # Ghost-Agent
    "AxelrGhostAgent",
    "GhostAgentExecutionError",
    "StagedOutput",
    "VerificationAuditTrail",
    "DataPoint",
    "ConfidenceRating",
    "get_ghost_agent",
    # Meta
    "route_expertise",
    "expertise_snapshot",
    "expertise_audit",
]

import sys
from types import ModuleType

from .gateway import detect_shape, build_payload

_gateway_translate_module = ModuleType("core.gateway_translate")
_gateway_translate_module.__all__ = ["detect_shape", "build_payload"]
_gateway_translate_module.detect_shape = detect_shape
_gateway_translate_module.build_payload = build_payload
sys.modules["core.gateway_translate"] = _gateway_translate_module
