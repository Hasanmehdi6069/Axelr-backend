# core/__init__.py
"""
AXELR Core — production-grade, memory-optimized services.

All modules are:
    * Fully typed
    * Self-contained (no cross-module imports)
    * Lazy-loaded for heavy dependencies (ONNX, numpy)
    * Safe to import on Render free tier (512 MB / 0.1 CPU)
"""
from .ai_engine import ResilientAIRouter, ProviderMetrics
from .code_guard import CodeGuard, Finding, ScanResult
from .context_registry import ContextRegistry
from .dependency_tracker import DependencyTracker, BlastRadius
from .intent_router import IntentRouter, IntentResult, get_router
from .pr_shield import PRShield, PRShieldInput
from .self_healer import SelfHealer, HealResult
from .semantic_cache import SemanticCache, get_semantic_cache
from .prompts import SYSTEM_PROMPTS

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