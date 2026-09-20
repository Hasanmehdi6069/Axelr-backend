# core/__init__.py
"""
AXELR Core — production-grade, memory-optimized services.

All modules are:
    * Fully typed
    * Self-contained (no cross-module imports)
    * Lazy-loaded for heavy dependencies (ONNX, numpy)
    * Safe to import on Render free tier (512 MB / 0.1 CPU)
"""
from .ai_engine import ProviderMetrics, ResilientAIRouter
from .code_guard import CodeGuard, Finding, ScanResult
from .context_registry import ContextRegistry
from .dependency_tracker import BlastRadius, DependencyTracker
from .intent_router import IntentResult, IntentRouter, get_router
from .pr_shield import PRShield, PRShieldInput
from .prompts import SYSTEM_PROMPTS
from .self_healer import HealResult, SelfHealer
from .semantic_cache import SemanticCache, get_semantic_cache

__all__ = [
    # Routing
    "ResilientAIRouter",
    "ProviderMetrics",
    # Security
    "CodeGuard",
    "Finding",
    "ScanResult",
    # Context
    "ContextRegistry",
    # Dependencies
    "DependencyTracker",
    "BlastRadius",
    # Intent
    "IntentRouter",
    "IntentResult",
    "get_router",
    # PR Defense
    "PRShield",
    "PRShieldInput",
    # Self-heal
    "SelfHealer",
    "HealResult",
    # Semantic Cache
    "SemanticCache",
    "get_semantic_cache",
    # Prompts
    "SYSTEM_PROMPTS",
]