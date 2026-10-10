# api/providers.py
"""
AXELR — Provider Catalog & Ranking
====================================
The router's view of the world. Wire-level adapters live in
`api.provider_http`. This module owns:

    §1 PROVIDER_FUNC_MAP      — name → callable (from provider_http)
    §2 PROVIDER_KEY_CHECK     — name → bool
    §3 PROVIDER_MODELS        — name → [model strings]
    §4 WORKSPACE_PRIORITY     — name ordering per workspace
    §5 ProviderMetrics + record_provider_result
    §6 get_provider_order + get_dynamically_ranked_providers
    §7 get_hot_swap_detector (bridge to core.failover)
"""
from __future__ import annotations

import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .config import (
    CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_KEY, CLOUDFLARE_MODEL,
    GEMINI_API_KEY, GEMINI_MODELS, GITHUB_MODEL, GITHUB_MODELS_TOKEN,
    GROQ_API_KEY, GROQ_MODELS, HF_API_KEY, MISTRAL_API_KEY, MISTRAL_MODELS,
    MODELSCOPE_API_KEY, MODELSCOPE_MODELS, NARAROUTER_API_KEY, NROUTER_API_KEY,
    NROUTER_MODEL, OLLAMA_API_KEY, OPENROUTER_API_KEY, OPENROUTER_MODELS,
    OVHCLOUD_API_KEY, OVHCLOUD_MODELS, REQUESTY_API_KEY, REQUESTY_MODEL,
    SILICONFLOW_API_KEY, SILICONFLOW_MODELS, TEAMOROUTER_API_KEY,
    TEAMOROUTER_MODEL, ZAI_API_KEY,
)
from .provider_http import (
    PROVIDER_HTTP, build_local_fallback_response,
    call_freegpt4_api,
)
from .state import state

logger = logging.getLogger("axelr.providers")


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Function map
# ═══════════════════════════════════════════════════════════════════════════
PROVIDER_FUNC_MAP: dict[str, Any] = dict(PROVIDER_HTTP)


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Key checks
# ═══════════════════════════════════════════════════════════════════════════
PROVIDER_KEY_CHECK: dict[str, bool] = {
    "gemini":           bool(GEMINI_API_KEY),
    "groq":             bool(GROQ_API_KEY),
    "cloudflare":       bool(CLOUDFLARE_API_KEY and CLOUDFLARE_ACCOUNT_ID),
    "openrouter":       bool(OPENROUTER_API_KEY),
    "modelscope":       bool(MODELSCOPE_API_KEY),
    "ollama_cloud":     bool(OLLAMA_API_KEY),
    "nara_router":      bool(NARAROUTER_API_KEY),
    "mistral":          bool(MISTRAL_API_KEY),
    "huggingface":      bool(HF_API_KEY),
    "github_models":    bool(GITHUB_MODELS_TOKEN),
    "ovhcloud":         bool(OVHCLOUD_API_KEY),
    "siliconflow":      bool(SILICONFLOW_API_KEY),
    "agnes_ai":         bool(os.getenv("AGNES_API_KEY")),
    "bazaarlink":       bool(os.getenv("BAZAARLINK_API_KEY")),
    "requesty":         bool(REQUESTY_API_KEY),
    "nrouter":          bool(NROUTER_API_KEY),
    "qoder":            bool(os.getenv("QODER_API_KEY")),
    "manifest":         bool(os.getenv("MANIFEST_API_KEY")),
    "glama":            bool(os.getenv("GLAMA_API_KEY")),
    "anyapi":           bool(os.getenv("ANYAPI_API_KEY")),
    "zhipuai":          bool(ZAI_API_KEY),
    "teamorouter":      bool(TEAMOROUTER_API_KEY),
    "free_llm_gateway": bool(os.getenv("FREE_LLM_GATEWAY_URL")),
    # keyless providers → always eligible
    "bifrost": True, "freegpt4_api": True, "puter": True, "freetheai": True,
    "omnigpt_gateway": True, "opencode_zen": True, "freeflow": True,
    "keylessai": True, "chubvenus": True, "blockrun": True, "aymo": True,
    "zerotwo": True, "aihubmix": True, "aisure": True, "proxygatellm": True,
    "ninerouter": True, "local": True,
}


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Model catalog
# ═══════════════════════════════════════════════════════════════════════════
PROVIDER_MODELS: dict[str, list[str]] = {
    "gemini":           GEMINI_MODELS,
    "groq":             GROQ_MODELS,
    "openrouter":       OPENROUTER_MODELS,
    "cloudflare":       [CLOUDFLARE_MODEL],
    "mistral":          MISTRAL_MODELS,
    "modelscope":       MODELSCOPE_MODELS,
    "ollama_cloud":     ["mistral-large-3:675b-cloud", "kimi-k2.6", "glm-5.3"],
    "nara_router":      ["minimax-m3", "deepseek-v3"],
    "github_models":    [GITHUB_MODEL],
    "ovhcloud":         OVHCLOUD_MODELS,
    "siliconflow":      SILICONFLOW_MODELS,
    "huggingface":      ["meta-llama/Llama-3.2-3B-Instruct",
                          "mistralai/Mistral-7B-Instruct-v0.3"],
    "agnes_ai":         ["agnes-2.0-flash"],
    "zhipuai":          ["glm-4.5-flash"],
    "teamorouter":      [TEAMOROUTER_MODEL],
    "bazaarlink":       ["auto:free"],
    "requesty":         [REQUESTY_MODEL],
    "nrouter":          [NROUTER_MODEL],
    "glama":            ["gpt-3.5-turbo"],
    "bifrost":          ["llama3.1:70b", "mistral:7b"],
    "freegpt4_api":     ["gpt-4"],
    "puter":            ["gpt-3.5-turbo"],
    "freetheai":        ["gpt-3.5-turbo"],
    "omnigpt_gateway":  ["gpt-3.5-turbo"],
    "opencode_zen":     ["qwen3-coder"],
    "freeflow":         ["gpt-3.5-turbo"],
    "qoder":            ["qwen3-coder"],
    "manifest":         ["auto:free"],
    "keylessai":        ["gpt-3.5-turbo"],
    "chubvenus":        ["gpt-3.5-turbo"],
    "blockrun":         ["deepseek-v4-flash"],
    "anyapi":           ["poolside/laguna-xs.2:free"],
    "aymo":             ["gemini-flash", "deepseek-v3.2", "qwen3"],
    "zerotwo":          ["gpt-5-mini", "gemini-flash-lite"],
    "aihubmix":         ["gpt-5.5", "gemini-3", "glm-5.1", "kimi", "minimax"],
    "aisure":           ["gpt-4o"],
    "proxygatellm":     ["gpt-3.5-turbo"],
    "free_llm_gateway": ["auto"],
    "ninerouter":       ["auto"],
    "local":            [],
}

# Active (production-ready) vs experimental
ACTIVE_PROVIDER_ENTRIES: list[tuple[str, Any, list[str]]] = [
    (n, PROVIDER_FUNC_MAP[n], PROVIDER_MODELS[n]) for n in (
        "gemini", "groq", "openrouter", "cloudflare", "mistral",
        "github_models", "siliconflow", "requesty", "local",
    )
]
EXPERIMENTAL_PROVIDER_ENTRIES: list[tuple[str, Any, list[str]]] = [
    (n, PROVIDER_FUNC_MAP[n], PROVIDER_MODELS[n]) for n in (
        "modelscope", "ollama_cloud", "nara_router", "huggingface",
        "zhipuai", "proxygatellm", "free_llm_gateway", "ninerouter",
        "teamorouter", "ovhcloud", "agnes_ai", "bifrost", "freegpt4_api",
        "bazaarlink", "freetheai", "omnigpt_gateway", "opencode_zen",
        "freeflow", "qoder", "manifest", "keylessai", "glama", "chubvenus",
        "blockrun", "anyapi", "aymo", "zerotwo", "aihubmix", "aisure",
    )
]
PROVIDER_CHAIN_ENTRIES = ACTIVE_PROVIDER_ENTRIES + EXPERIMENTAL_PROVIDER_ENTRIES
PROVIDER_CHAIN: list[tuple[str, Any]] = [(n, f) for n, f, _ in PROVIDER_CHAIN_ENTRIES]
_ACTIVE_NAMES: frozenset[str] = frozenset(n for n, _, _ in ACTIVE_PROVIDER_ENTRIES)

FREE_TIER_TOKEN_LIMIT = int(os.getenv("FREE_TIER_TOKEN_LIMIT", "1000000"))


# ═══════════════════════════════════════════════════════════════════════════
# §4 — Workspace priority
# ═══════════════════════════════════════════════════════════════════════════
WORKSPACE_PRIORITY: dict[str, list[str]] = {
    "data": [
        "gemini", "modelscope", "groq", "openrouter", "ollama_cloud",
        "nara_router", "proxygatellm", "free_llm_gateway", "ninerouter",
        "mistral", "ovhcloud", "siliconflow", "zhipuai", "teamorouter",
        "nrouter", "bazaarlink", "requesty", "qoder", "manifest",
        "keylessai", "anyapi", "aymo", "zerotwo", "aihubmix", "aisure",
    ],
    "design": [
        "cloudflare", "groq", "gemini", "openrouter", "modelscope",
        "nara_router", "proxygatellm", "free_llm_gateway", "ninerouter",
        "agnes_ai", "siliconflow", "zhipuai", "teamorouter", "bifrost",
        "freegpt4_api", "ovhcloud", "nrouter", "puter", "omnigpt_gateway",
        "opencode_zen", "qoder", "keylessai", "glama", "chubvenus",
        "blockrun", "anyapi",
    ],
    "core": [
        "gemini", "modelscope", "groq", "openrouter", "ollama_cloud",
        "nara_router", "proxygatellm", "free_llm_gateway", "ninerouter",
        "mistral", "huggingface", "github_models", "zhipuai", "teamorouter",
        "ovhcloud", "siliconflow", "nrouter", "bazaarlink", "requesty",
        "qoder", "freeflow", "manifest", "keylessai", "glama", "chubvenus",
        "anyapi", "aymo", "zerotwo", "aihubmix", "aisure",
    ],
    "prompt": [
        "gemini", "openrouter", "modelscope", "groq", "nara_router",
        "ollama_cloud", "proxygatellm", "free_llm_gateway", "ninerouter",
        "zhipuai", "teamorouter", "requesty", "bazaarlink",
    ],
    "touch_fix": [
        "groq", "mistral", "github_models", "zhipuai", "teamorouter",
        "nara_router", "proxygatellm", "free_llm_gateway", "ninerouter",
        "ovhcloud", "qoder", "opencode_zen",
    ],
}


def get_provider_order(workspace: str) -> list[str]:
    all_names = [n for n, _ in PROVIDER_CHAIN if n != "local"]
    priority = WORKSPACE_PRIORITY.get(workspace, WORKSPACE_PRIORITY["core"])
    ordered: list[str] = []
    for name in priority:
        if name in all_names and name not in ordered:
            ordered.append(name)
    for name in all_names:
        if name not in ordered:
            ordered.append(name)
    ordered.append("local")
    return ordered


# ═══════════════════════════════════════════════════════════════════════════
# §5 — Metrics
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class ProviderMetrics:
    name: str
    latencies: deque = field(default_factory=lambda: deque(maxlen=10))
    history: deque = field(default_factory=lambda: deque(maxlen=10))
    consecutive_failures: int = 0
    cooldown_until: float = 0.0

    @property
    def is_available(self) -> bool:
        return time.time() >= self.cooldown_until

    async def get_score(self, workspace: str) -> float:
        if not self.is_available:
            return -9999.0
        success_rate = 0.85
        avg_lat = 1.5
        if state.redis_client:
            try:
                successes = int(await state.redis_client.get(f"metrics:{self.name}:successes") or 0)
                failures = int(await state.redis_client.get(f"metrics:{self.name}:failures") or 0)
                total = successes + failures
                if total > 0:
                    success_rate = successes / total
                avg_ttft = await state.redis_client.get(f"metrics:{self.name}:avg_ttft")
                if avg_ttft:
                    avg_lat = float(avg_ttft)
                consecutive = int(await state.redis_client.get(f"metrics:{self.name}:consecutive_failures") or 0)
                self.consecutive_failures = consecutive
            except Exception as e:
                logger.warning("redis_metrics_fetch_failed provider=%s error=%s", self.name, e)
                total = len(self.history)
                if total > 0:
                    success_rate = sum(self.history) / total
                if self.latencies:
                    avg_lat = sum(self.latencies) / len(self.latencies)
        affinity = 15 if (
            (workspace == "design" and self.name in {"cloudflare", "groq", "gemini"})
            or (workspace == "data" and self.name in {"gemini", "modelscope", "groq"})
        ) else 0
        return (success_rate * 100) - (avg_lat * 12) - (self.consecutive_failures * 25) + affinity


PROVIDER_TRACKER: dict[str, ProviderMetrics] = {
    name: ProviderMetrics(name=name) for name, _ in PROVIDER_CHAIN if name != "local"
}


async def record_provider_result(
    name: str, latency: float, success: bool, is_rate_limit: bool = False,
) -> None:
    p = PROVIDER_TRACKER.get(name)
    if not p:
        return
    # Metric decay: trim history to last 50 entries, keep only recent metrics
    if len(p.history) > 50:
        p.history = p.history[-50:]
    if len(p.latencies) > 50:
        p.latencies = p.latencies[-50:]
    # Add new entry
    if success:
        p.history.append(True)
        p.latencies.append(latency)
        p.consecutive_failures = 0
    else:
        p.history.append(False)
        p.consecutive_failures += 1
    if state.redis_client:
        try:
            pipe = state.redis_client.pipeline()
            if success:
                pipe.incr(f"metrics:{name}:successes")
                pipe.expire(f"metrics:{name}:successes", 86400)  # TTL 24h
                pipe.set(f"metrics:{name}:consecutive_failures", 0)
            else:
                pipe.incr(f"metrics:{name}:failures")
                pipe.expire(f"metrics:{name}:failures", 86400)
                pipe.incr(f"metrics:{name}:consecutive_failures")
            if is_rate_limit:
                pipe.set(f"metrics:{name}:cooldown_until", time.time() + 1800)
                p.cooldown_until = time.time() + 1800
                logger.warning("provider_rate_limited_global provider=%s", name)
            elif p.consecutive_failures >= 3:
                pipe.set(f"metrics:{name}:cooldown_until", time.time() + 300)
                p.cooldown_until = time.time() + 300
                logger.warning("provider_circuit_tripped_global provider=%s", name)
            await pipe.execute()
        except Exception as e:
            logger.warning("redis_metrics_update_failed provider=%s error=%s", name, e)


# ═══════════════════════════════════════════════════════════════════════════
# §6 — Ranking
# ═══════════════════════════════════════════════════════════════════════════
async def get_dynamically_ranked_providers(workspace: str) -> list[str]:
    """Rank providers. Hot path: 1 Redis MGET for all metrics."""
    eligible = [
        p for p in PROVIDER_TRACKER.values()
        if p.name in _ACTIVE_NAMES and PROVIDER_KEY_CHECK.get(p.name, False)
    ]
    if not eligible:
        return ["local"]

    if state.redis_client is None:
        scored: list[tuple[float, str]] = []
        for p in eligible:
            score = await p.get_score(workspace)
            if score > -9999:
                scored.append((score, p.name))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [n for _, n in scored] + ["local"]

    KEYS_PER = 5
    keys: list[str] = []
    for p in eligible:
        keys += [f"metrics:{p.name}:successes", f"metrics:{p.name}:failures",
                 f"metrics:{p.name}:avg_ttft", f"metrics:{p.name}:consecutive_failures",
                 f"metrics:{p.name}:cooldown_until"]

    try:
        values = await state.redis_client.mget(keys)
    except Exception as e:
        logger.warning("redis_mget_failed_falling_back error=%s", e)
        scored = []
        for p in eligible:
            score = await p.get_score(workspace)
            if score > -9999:
                scored.append((score, p.name))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [n for _, n in scored] + ["local"]

    now = time.time()
    scored: list[tuple[float, str]] = []
    for idx, p in enumerate(eligible):
        base = idx * KEYS_PER
        raw_succ, raw_fail, raw_ttft, raw_consec, raw_cool = values[base:base + KEYS_PER]
        cooldown_until = float(raw_cool) if raw_cool else 0.0
        if now < cooldown_until:
            continue
        successes = int(raw_succ or 0)
        failures = int(raw_fail or 0)
        total = successes + failures
        success_rate = (successes / total) if total > 0 else 0.85
        avg_lat = float(raw_ttft) if raw_ttft else 1.5
        consecutive = int(raw_consec or 0)
        affinity = 15 if (
            (workspace == "design" and p.name in {"cloudflare", "groq", "gemini"})
            or (workspace == "data" and p.name in {"gemini", "modelscope", "groq"})
        ) else 0
        score = (success_rate * 100) - (avg_lat * 12) - (consecutive * 25) + affinity
        scored.append((score, p.name))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [n for _, n in scored] + ["local"]


__all__ = [
    "PROVIDER_FUNC_MAP", "PROVIDER_KEY_CHECK", "PROVIDER_MODELS",
    "PROVIDER_CHAIN", "PROVIDER_CHAIN_ENTRIES",
    "ACTIVE_PROVIDER_ENTRIES", "EXPERIMENTAL_PROVIDER_ENTRIES",
    "WORKSPACE_PRIORITY", "get_provider_order",
    "ProviderMetrics", "PROVIDER_TRACKER", "record_provider_result",
    "get_dynamically_ranked_providers",
    "build_local_fallback_response",
    "FREE_TIER_TOKEN_LIMIT",
]