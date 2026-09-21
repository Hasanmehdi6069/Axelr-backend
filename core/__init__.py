# core/__init__.py
"""
AXELR Core — production-grade, memory-optimized services.

Every submodule import is guarded so a single missing optional dependency
(e.g. onnxruntime, fastembed, aiohttp) cannot take down the whole package.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("axelr.core")

# ── Safe exports ──────────────────────────────────────────────────────────
# Each block is independent — one failure does not cascade.

try:
    from .ai_engine import ProviderMetrics, ResilientAIRouter
except Exception as e:                                          # noqa: BLE001
    logger.warning("core.ai_engine unavailable: %s", e)
    ProviderMetrics = None                                      # type: ignore
    ResilientAIRouter = None                                    # type: ignore

try:
    from .code_guard import CodeGuard, Finding, ScanResult
except Exception as e:                                          # noqa: BLE001
    logger.warning("core.code_guard unavailable: %s", e)
    CodeGuard = None                                            # type: ignore
    Finding = None                                              # type: ignore
    ScanResult = None                                           # type: ignore

try:
    from .context_registry import ContextRegistry
except Exception as e:                                          # noqa: BLE001
    logger.warning("core.context_registry unavailable: %s", e)
    ContextRegistry = None                                      # type: ignore

try:
    from .dependency_tracker import BlastRadius, DependencyTracker
except Exception as e:                                          # noqa: BLE001
    logger.warning("core.dependency_tracker unavailable: %s", e)
    BlastRadius = None                                          # type: ignore
    DependencyTracker = None                                    # type: ignore

try:
    from .intent_router import IntentResult, IntentRouter, get_router
except Exception as e:                                          # noqa: BLE001
    logger.warning("core.intent_router unavailable: %s", e)
    IntentResult = None                                         # type: ignore
    IntentRouter = None                                         # type: ignore

    def get_router():                                           # type: ignore
        class _Fallback:
            async def classify(self, *a, **kw):
                class R:
                    workspace = "core"
                    confidence = 0.0
                    method = "fallback"
                return R()
        return _Fallback()

try:
    from .pr_shield import PRShield, PRShieldInput
except Exception as e:                                          # noqa: BLE001
    logger.warning("core.pr_shield unavailable: %s", e)
    PRShield = None                                             # type: ignore
    PRShieldInput = None                                        # type: ignore

try:
    from .prompts import SYSTEM_PROMPTS
except Exception as e:                                          # noqa: BLE001
    logger.warning("core.prompts unavailable: %s", e)
    SYSTEM_PROMPTS = {}                                         # type: ignore

try:
    from .self_healer import HealResult, SelfHealer
except Exception as e:                                          # noqa: BLE001
    logger.warning("core.self_healer unavailable: %s", e)
    HealResult = None                                           # type: ignore
    SelfHealer = None                                           # type: ignore

try:
    from .semantic_cache import SemanticCache, get_semantic_cache
except Exception as e:                                          # noqa: BLE001
    logger.warning("core.semantic_cache unavailable: %s", e)
    SemanticCache = None                                        # type: ignore

    def get_semantic_cache():                                   # type: ignore
        class _FallbackCache:
            async def get(self, *a, **kw): return None
            async def set(self, *a, **kw): return None
            async def clear(self, *a, **kw): return None
            async def _ensure_model(self): return None
        return _FallbackCache()

__all__ = [
    "ResilientAIRouter", "ProviderMetrics",
    "CodeGuard", "Finding", "ScanResult",
    "ContextRegistry",
    "DependencyTracker", "BlastRadius",
    "IntentRouter", "IntentResult", "get_router",
    "PRShield", "PRShieldInput",
    "SelfHealer", "HealResult",
    "SemanticCache", "get_semantic_cache",
    "SYSTEM_PROMPTS",
]
