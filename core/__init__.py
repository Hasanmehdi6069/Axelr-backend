# core/__init__.py
"""
AXELR Core — public API surface.

All modules are:
    * Fully typed
    * Self-contained (no cross-module imports at the package level)
    * Lazy-loaded for heavy dependencies (ONNX, numpy)
    * Safe to import on Render free tier (512 MB / 0.1 CPU)

Sub-modules:
    infra           — Redis-backed state: cache, circuit breaker, external context
    security        — CodeGuard security scanner (Finding, ScanResult, Severity)
    dependency      — DependencyTracker import graph + blast radius
    healing         — SelfHealer / TouchFixEngine / make_diff / RouteFunc
    worker_client   — remote sandbox execution client
    testloop        — autonomous generate → execute → heal loop
    pr_shield       — Markdown PR defense report builder
    vectorize       — Cloudflare Vectorize semantic response cache
    conversation    — Async vector-backed conversation memory
    repo_indexer    — GitHub repo → Vectorize index + query
    routing         — intent classification + agentic orchestration + LLM providers
    webhook_pipeline — inbound extraction webhook pipeline
"""
from .infra import (
    # cache helpers
    get_redis_cache, set_redis_cache, delete_redis_cache,
    # circuit breaker
    CircuitBreaker,
    # external context
    ContextRegistry, ContextItem,
)

# ── code layer (6 files replacing the old core.code monolith) ───────────────
from .security import (
    CodeGuard, Finding, ScanResult, Severity, default_guard,
)
from .dependency import (
    DependencyTracker, BlastRadius,
)
from .healing import (
    SelfHealer, TouchFixEngine, HealResult, make_diff, RouteFunc,
)
from .worker_client import (
    execute_code_on_worker, close_worker_client,
)
from .testloop import (
    TestLoop, TestIteration, TestLoopResult, ExecuteFunc,
)
from .pr_shield import (
    PRShield, PRShieldInput,
)

# ── memory layer (3 files replacing the old core.memory monolith) ───────────
from .vectorize import (
    CloudflareVectorizeCache, CacheEntry, get_vector_cache,
)
from .conversation import (
    ConversationMemory, MemoryItem,
)
from .repo_indexer import (
    RepoIndexer, IndexStats, CodeChunk,
)

from .routing import (
    IntentRouter, IntentResult, get_router,
    Orchestrator, Subtask,
    # LLM providers
    OpenAICompatProvider, OPENAI_COMPAT, make_provider_func, call_openai_compat,
)

from .webhook_pipeline import (
    make_webhook_router, ExtractWebhookPayload,
)

__all__ = [
    # infra
    "get_redis_cache", "set_redis_cache", "delete_redis_cache",
    "CircuitBreaker",
    "ContextRegistry", "ContextItem",
    # code — security
    "CodeGuard", "Finding", "ScanResult", "Severity", "default_guard",
    # code — dependency graph
    "DependencyTracker", "BlastRadius",
    # code — healing
    "SelfHealer", "TouchFixEngine", "HealResult", "make_diff", "RouteFunc",
    # code — sandbox worker
    "execute_code_on_worker", "close_worker_client",
    # code — test loop
    "TestLoop", "TestIteration", "TestLoopResult", "ExecuteFunc",
    # code — PR shield
    "PRShield", "PRShieldInput",
    # memory — semantic cache
    "CloudflareVectorizeCache", "CacheEntry", "get_vector_cache",
    # memory — conversation
    "ConversationMemory", "MemoryItem",
    # memory — repo indexer
    "RepoIndexer", "IndexStats", "CodeChunk",
    # routing
    "IntentRouter", "IntentResult", "get_router",
    "Orchestrator", "Subtask",
    "OpenAICompatProvider", "OPENAI_COMPAT", "make_provider_func", "call_openai_compat",
    # webhooks
    "make_webhook_router", "ExtractWebhookPayload",
]