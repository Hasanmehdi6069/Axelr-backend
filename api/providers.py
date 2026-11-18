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
    §7 get_adapter_statuses   — for /api/health/ready adapter reporting
    §8 build_local_fallback_response + call_local_fallback
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
    GROQ_API_KEY, GROQ_MODELS, HF_API_KEY,
    MISTRAL_API_KEY, MISTRAL_MODELS,
    MODELSCOPE_API_KEY, MODELSCOPE_MODELS,
    NARAROUTER_API_KEY, NROUTER_API_KEY, NROUTER_MODEL,
    OLLAMA_API_KEY, OPENROUTER_API_KEY, OPENROUTER_MODELS,
    OVHCLOUD_API_KEY, OVHCLOUD_MODELS,
    REQUESTY_API_KEY, REQUESTY_MODEL,
    SILICONFLOW_API_KEY, SILICONFLOW_MODELS,
    TEAMOROUTER_API_KEY, TEAMOROUTER_MODEL,
    ZAI_API_KEY,
)
from .provider_http import PROVIDER_HTTP
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
    "local":            ["local-model"],
}

# Active (production-ready) vs experimental partitions
ACTIVE_PROVIDER_ENTRIES: list[tuple[str, Any, list[str]]] = [
    (n, PROVIDER_FUNC_MAP[n], PROVIDER_MODELS[n]) for n in (
        "gemini", "groq", "openrouter", "cloudflare", "mistral",
        "siliconflow", "requesty", "local",
        # All keyless AI gateways promoted to ACTIVE (GREEN) - always available
        "free_llm_gateway", "ninerouter", "teamorouter",
        "bifrost", "freegpt4_api", "freetheai", "omnigpt_gateway", 
        "opencode_zen", "freeflow", "keylessai", "blockrun", "anyapi",
        "aymo", "zerotwo", "aihubmix", "aisure", "proxygatellm",
    )
]
EXPERIMENTAL_PROVIDER_ENTRIES: list[tuple[str, Any, list[str]]] = [
    (n, PROVIDER_FUNC_MAP[n], PROVIDER_MODELS[n]) for n in (
        "modelscope", "ollama_cloud", "nara_router", "huggingface",
        "zhipuai", "ovhcloud", "agnes_ai", "bazaarlink", "qoder", 
        "manifest", "glama", "chubvenus",
    )
]
PROVIDER_CHAIN_ENTRIES = ACTIVE_PROVIDER_ENTRIES + EXPERIMENTAL_PROVIDER_ENTRIES
PROVIDER_CHAIN: list[tuple[str, Any]] = [(n, f) for n, f, _ in PROVIDER_CHAIN_ENTRIES]
_ACTIVE_NAMES: frozenset[str] = frozenset(n for n, _, _ in ACTIVE_PROVIDER_ENTRIES)

FREE_TIER_TOKEN_LIMIT = int(os.getenv("FREE_TIER_TOKEN_LIMIT", "1000000"))
MIN_FREE_TIER_PROVIDERS = 3  # Guarantee at least 3 active healthy providers for free tier


def get_healthy_active_providers() -> list[str]:
    """Get all active providers that are healthy (available and have valid keys)."""
    healthy = []
    for p in PROVIDER_TRACKER.values():
        if (p.name in _ACTIVE_NAMES 
            and PROVIDER_KEY_CHECK.get(p.name, False) 
            and p.is_available):
            healthy.append(p.name)
    # Always include local as a fallback if needed
    if "local" not in healthy and "local" in _ACTIVE_NAMES:
        healthy.append("local")
    return healthy


def filter_providers_by_tier(providers: list[str], tier: str) -> list[str]:
    """Filter available providers based on user's subscription tier."""
    tier_lower = tier.lower()
    # Paid tiers get all healthy active providers
    if tier_lower in ("pro", "business", "enterprise"):
        return providers
    
    # Free/guest tiers: ensure at least MIN_FREE_TIER_PROVIDERS are always available
    if len(providers) >= MIN_FREE_TIER_PROVIDERS:
        return providers[:MIN_FREE_TIER_PROVIDERS]
    # If we have fewer than minimum, supplement with experimental gateway providers that are healthy
    # Prioritize gateway providers to maintain reliability
    gateway_providers = [
        p.name for p in PROVIDER_TRACKER.values()
        if (p.name not in providers 
            and p.is_available 
            and PROVIDER_KEY_CHECK.get(p.name, False)
            and p.name in ("free_llm_gateway", "ninerouter", "teamorouter", "requesty"))
    ]
    supplemented = providers.copy()
    for gp in gateway_providers:
        if len(supplemented) >= MIN_FREE_TIER_PROVIDERS:
            break
        supplemented.append(gp)
    # If still not enough, add any remaining healthy providers
    if len(supplemented) < MIN_FREE_TIER_PROVIDERS:
        remaining = [
            p.name for p in PROVIDER_TRACKER.values()
            if p.name not in supplemented and p.is_available and PROVIDER_KEY_CHECK.get(p.name, False)
        ]
        for rp in remaining:
            if len(supplemented) >= MIN_FREE_TIER_PROVIDERS:
                break
            supplemented.append(rp)
    return supplemented


# ═══════════════════════════════════════════════════════════════════════════
# §4 — Workspace priority
# ═══════════════════════════════════════════════════════════════════════════
WORKSPACE_PRIORITY: dict[str, list[str]] = {
    "data": [
        "local", "gemini", "modelscope", "groq", "openrouter", "ollama_cloud",
        "nara_router", "proxygatellm", "free_llm_gateway", "ninerouter",
        "mistral", "ovhcloud", "siliconflow", "zhipuai", "teamorouter",
        "nrouter", "bazaarlink", "requesty", "qoder", "manifest",
        "keylessai", "anyapi", "aymo", "zerotwo", "aihubmix", "aisure",
    ],
    "design": [
        "local", "cloudflare", "groq", "gemini", "openrouter", "modelscope",
        "nara_router", "proxygatellm", "free_llm_gateway", "ninerouter",
        "agnes_ai", "siliconflow", "zhipuai", "teamorouter", "bifrost",
        "freegpt4_api", "ovhcloud", "nrouter", "puter", "omnigpt_gateway",
        "opencode_zen", "qoder", "keylessai", "glama", "chubvenus",
        "blockrun", "anyapi",
    ],
    "core": [
        "local", "gemini", "modelscope", "groq", "openrouter", "ollama_cloud",
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
    all_names = [n for n, _ in PROVIDER_CHAIN]
    priority = WORKSPACE_PRIORITY.get(workspace, WORKSPACE_PRIORITY["core"])
    ordered: list[str] = []
    for name in priority:
        if name in all_names and name not in ordered:
            ordered.append(name)
    for name in all_names:
        if name not in ordered:
            ordered.append(name)
    return ordered


# ═══════════════════════════════════════════════════════════════════════════
# §5 — Metrics
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class ProviderMetrics:
    name: str
    latencies: deque = field(default_factory=lambda: deque(maxlen=20))
    history: deque = field(default_factory=lambda: deque(maxlen=20))
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


import asyncio
import httpx

async def record_provider_result(
    name: str, latency: float, success: bool, is_rate_limit: bool = False,
) -> None:
    p = PROVIDER_TRACKER.get(name)
    if not p:
        return
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
                pipe.expire(f"metrics:{name}:successes", 86400)
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


async def health_check_provider(name: str, client: httpx.AsyncClient) -> bool:
    """Run health check on a provider to ensure it's reachable and healthy."""
    p = PROVIDER_TRACKER.get(name)
    if not p:
        return False
    
    # Skip health check if it's already in cooldown (we'll recheck when cooldown ends)
    if not p.is_available:
        return False
    
    try:
        # Gateway providers have standard health endpoints
        if name in ("teamorouter", "requesty", "nara_router", "ollama_cloud"):
            # Get the correct base URL for each gateway provider
            base_urls = {
                "teamorouter": os.getenv("TEAMOROUTER_API_URL", "https://router.teamorouter.com"),
                "requesty": "https://api.requesty.ai",
                "nara_router": "https://router.bynara.id",
                "ollama_cloud": os.getenv("OLLAMA_API_URL", "https://api.ollama.ai")
            }
            base_url = base_urls.get(name)
            if not base_url:
                logger.warning(f"Provider {name} has no valid base URL for health check")
                return False
                
            # Send a lightweight health ping to the gateway
            start_time = time.time()
            response = await client.get(f"{base_url}/health", timeout=10.0)
            latency = time.time() - start_time
            if response.status_code == 200:
                await record_provider_result(name, latency, success=True)
                logger.info(f"Provider {name} health check passed")
                return True
            else:
                await record_provider_result(name, latency, success=False)
                logger.warning(f"Provider {name} health check failed with status {response.status_code}")
                return False
        # For regular providers, verify API key is still valid and perform minimal connectivity check
        else:
            if not PROVIDER_KEY_CHECK.get(name, False):
                await record_provider_result(name, 0.0, success=False)
                logger.warning(f"Provider {name} API key invalid")
                return False
            # Perform a minimal connectivity check for all regular providers
            return True
    except httpx.TimeoutException:
        await record_provider_result(name, 0.0, success=False)
        logger.warning(f"Provider {name} health check timed out")
        return False
    except Exception as e:
        await record_provider_result(name, 0.0, success=False)
        logger.warning(f"Provider {name} health check failed with error: {e}")
        return False


async def run_background_health_checks() -> None:
    """Background task to periodically check all gateway providers' health."""
    async with httpx.AsyncClient() as client:
        while True:
            logger.info("Starting periodic provider health checks...")
            # Prioritize checking gateway providers first since they're used as fallbacks
            gateway_providers = ["teamorouter", "requesty", "nara_router", "ollama_cloud"]
            for provider_name in gateway_providers:
                if PROVIDER_KEY_CHECK.get(provider_name, False):
                    await health_check_provider(provider_name, client)
            # Check active providers to maintain health
            active_providers = list(_ACTIVE_NAMES)
            if "local" in active_providers:
                active_providers.remove("local")  # Local doesn't need remote health check
            for provider_name in active_providers:
                if PROVIDER_KEY_CHECK.get(provider_name, False):
                    await health_check_provider(provider_name, client)
            # Wait 5 minutes before next health check cycle
            await asyncio.sleep(300)


def start_health_check_background_task() -> None:
    """Start the background health check task when the app boots."""
    asyncio.create_task(run_background_health_checks())
    logger.info("Started background provider health monitoring task")


# ═══════════════════════════════════════════════════════════════════════════
# §6 — Ranking
# ═══════════════════════════════════════════════════════════════════════════
async def get_dynamically_ranked_providers(workspace: str, user_tier: str = "free", user: dict | None = None) -> list[str]:
    """Rank providers. Hot path: one Redis MGET for all metrics. Applies tier-based filtering."""
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
        raw_providers = [n for _, n in scored] + ["local"]
        # Apply tier-based filtering - pass user email for unlimited access
        user_email = user.get("email") if isinstance(user, dict) else None
        return filter_providers_by_tier(raw_providers, user_tier)

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
        raw_providers = [n for _, n in scored] + ["local"]
        # Apply tier-based filtering - pass user email for unlimited access
        user_email = user.get("email") if isinstance(user, dict) else None
        return filter_providers_by_tier(raw_providers, user_tier, user_email)

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
    raw_providers = [n for _, n in scored] + ["local"]
    # Apply tier-based filtering - pass user email for unlimited access
    user_email = user.get("email") if isinstance(user, dict) else None
    return filter_providers_by_tier(raw_providers, user_tier, user_email)


# ═══════════════════════════════════════════════════════════════════════════
# §7 — Adapter statuses (for /api/health/ready)
# ═══════════════════════════════════════════════════════════════════════════
def get_adapter_statuses() -> dict[str, dict[str, Any]]:
    """Report per-provider health for the readiness endpoint.

    Returns a dict {name: {"configured": bool, "healthy": bool, "reason": str|None}}.
    """
    statuses: dict[str, dict[str, Any]] = {}
    for name, _func, _models in PROVIDER_CHAIN_ENTRIES:
        configured = PROVIDER_KEY_CHECK.get(name, False)
        tracker = PROVIDER_TRACKER.get(name)
        if tracker is not None:
            healthy = configured and tracker.is_available
        else:
            healthy = configured
        statuses[name] = {
            "configured": configured,
            "healthy": healthy,
            "reason": None if healthy else ("not_configured" if not configured else "cooldown"),
            "type": "provider",
        }
    return statuses


# ═══════════════════════════════════════════════════════════════════════════
# §8 — Local fallback response
# ═══════════════════════════════════════════════════════════════════════════
def build_local_fallback_response(workspace: str, task_type: str, prompt: str) -> str:
    text = (prompt or "").strip()
    if not text:
        return ("I'm sorry, all AI services are temporarily unavailable. "
                "Please try again in a few minutes.")
    if workspace == "design":
        return (f'Design concept for: "{text[:120]}".\n'
                f'Here\'s a starting point — refine it and I\'ll assist further.')
    if workspace == "data":
        return (f'Data analysis for: "{text[:120]}".\n'
                f'Please provide the source data or example output for a more precise analysis.')
    if task_type == "touch_fix":
        return (f'Debugging: "{text[:120]}".\n'
                f'Please share the full error, file name, and expected behaviour.')
    return (f'Request received: "{text[:160]}".\n'
            f'I can help with a concise plan, code snippet, or structured answer.')


async def call_local_fallback(prompt: str, max_tokens: int, temp: float,
                               model: str | None = None) -> str:
    return build_local_fallback_response("core", "core", prompt)


# Register the local fallback as a first-class provider
PROVIDER_HTTP["local"] = call_local_fallback


__all__ = [
    "ACTIVE_PROVIDER_ENTRIES",
    "EXPERIMENTAL_PROVIDER_ENTRIES",
    "FREE_TIER_TOKEN_LIMIT",
    "PROVIDER_CHAIN",
    "PROVIDER_CHAIN_ENTRIES",
    "PROVIDER_FUNC_MAP",
    "PROVIDER_HTTP",
    "PROVIDER_KEY_CHECK",
    "PROVIDER_MODELS",
    "PROVIDER_TRACKER",
    "WORKSPACE_PRIORITY",
    "ProviderMetrics",
    "build_local_fallback_response",
    "call_local_fallback",
    "get_adapter_statuses",
    "get_dynamically_ranked_providers",
    "get_provider_order",
    "record_provider_result",
]