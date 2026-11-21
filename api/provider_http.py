# api/provider_http.py
"""
AXELR — Wire-level HTTP provider adapters.
==========================================
Every adapter has the canonical signature:

    async def call(prompt: str, max_tokens: int, temp: float,
                   model: str | None = None) -> str

Adapters raise on failure. Callers in `providers.py` handle retries and
metric recording.

The registry `PROVIDER_HTTP` is a `dict[str, Callable]`.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp

from .config import (
    CLOUDFLARE_ACCOUNT_ID,
    CLOUDFLARE_API_KEY,
    CLOUDFLARE_MODEL,
    GEMINI_API_KEY,
    GEMINI_MODELS,
    GITHUB_MODEL,
    GITHUB_MODELS_TOKEN,
    GROQ_API_KEY,
    GROQ_MODELS,
    HF_API_KEY,
    MISTRAL_API_KEY,
    MISTRAL_MODELS,
    MODELSCOPE_API_KEY,
    MODELSCOPE_MODELS,
    NARAROUTER_API_KEY,
    OLLAMA_API_KEY,
    OPENROUTER_API_KEY,
    OPENROUTER_MODELS,
    OVHCLOUD_API_KEY,
    OVHCLOUD_MODELS,
    REQUESTY_API_KEY,
    REQUESTY_MODEL,
    SILICONFLOW_API_KEY,
    SILICONFLOW_MODELS,
    TEAMOROUTER_API_KEY,
    TEAMOROUTER_MODEL,
    ZAI_API_KEY,
)

logger = logging.getLogger("axelr.provider_http")


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Shared POST helper
# ═══════════════════════════════════════════════════════════════════════════
async def _post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: int = 60,
) -> dict[str, Any]:
    """POST JSON, return parsed body. Raises on non-2xx."""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status >= 400:
                err = await resp.text()
                logger.error(
                    "provider_http_failed status=%d url=%s err=%s",
                    resp.status,
                    url,
                    err[:300],
                )
                raise Exception(f"HTTP {resp.status}: {err[:200]}")
            return await resp.json()


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Non-standard adapters
# ═══════════════════════════════════════════════════════════════════════════
async def gemini_adapter(
    prompt: str,
    max_tokens: int,
    temp: float,
    model: str | None = None,
) -> str:
    if not GEMINI_API_KEY:
        raise Exception("GEMINI_API_KEY missing")
    model_name = model or (GEMINI_MODELS[0] if GEMINI_MODELS else "gemini-1.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temp,
            "maxOutputTokens": max_tokens,
        },
        "safetySettings": [
            {"category": c, "threshold": "BLOCK_NONE"}
            for c in (
                "HARM_CATEGORY_HARASSMENT",
                "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "HARM_CATEGORY_DANGEROUS_CONTENT",
            )
        ],
    }
    data = await _post_json(url, {"Content-Type": "application/json"}, payload)
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise Exception(f"Gemini malformed response: {data}") from e


async def cloudflare_adapter(
    prompt: str,
    max_tokens: int,
    temp: float,
    model: str | None = None,
) -> str:
    if not CLOUDFLARE_API_KEY or not CLOUDFLARE_ACCOUNT_ID:
        raise Exception("Cloudflare credentials missing")
    url = f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run/{model or CLOUDFLARE_MODEL}"
    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
    }
    data = await _post_json(
        url,
        {"Authorization": f"Bearer {CLOUDFLARE_API_KEY}", "Content-Type": "application/json"},
        payload,
    )
    return data.get("result", {}).get("response", "")


# ═══════════════════════════════════════════════════════════════════════════
# §3 — OpenAI-compatible factory
# ═══════════════════════════════════════════════════════════════════════════
def _openai_compat(
    *,
    name: str,
    base_url: str,
    key: str,
    default_model: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    async def _call(
        prompt: str,
        max_tokens: int,
        temp: float,
        model: str | None = None,
    ) -> str:
        if not key:
            raise Exception(f"{name}: API key missing")
        headers: dict[str, str] = {"Content-Type": "application/json"}
        headers["Authorization"] = f"Bearer {key}"
        if extra_headers:
            headers.update(extra_headers)
        payload = {
            "model": model or default_model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temp,
            "stream": False,
        }
        try:
            data = await _post_json(base_url, headers, payload)
        except Exception as e:
            raise Exception(f"{name} error: {e}") from e
        if not data.get("choices"):
            raise Exception(f"{name}: no choices returned")
        return data["choices"][0]["message"]["content"]

    return _call


# ═══════════════════════════════════════════════════════════════════════════
# §4 — Local fallback (registered by providers.py)
# ═══════════════════════════════════════════════════════════════════════════
async def local_adapter(
    prompt: str,
    max_tokens: int,
    temp: float,
    model: str | None = None,
) -> str:
    # The actual message is built by providers.build_local_fallback_response.
    # This adapter is a thin passthrough so `PROVIDER_HTTP` has a `local` entry.
    # The proper implementation with workspace-aware messaging is registered
    # by `providers.call_local_fallback` which OVERWRITES this at import time.
    return "I'm sorry, all AI services are temporarily unavailable. Please try again in a few minutes."


# ═══════════════════════════════════════════════════════════════════════════
# §5 — Registry
# ═══════════════════════════════════════════════════════════════════════════
PROVIDER_HTTP: dict[str, Any] = {
    "gemini": gemini_adapter,
    "cloudflare": cloudflare_adapter,
    "groq": _openai_compat(
        name="groq",
        base_url="https://api.groq.com/openai/v1/chat/completions",
        key=GROQ_API_KEY,
        default_model=GROQ_MODELS[0] if GROQ_MODELS else None,
    ),
    "openrouter": _openai_compat(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1/chat/completions",
        key=OPENROUTER_API_KEY,
        default_model=OPENROUTER_MODELS[0] if OPENROUTER_MODELS else None,
        extra_headers={"HTTP-Referer": "https://axelr.in", "X-Title": "Axelr AI"},
    ),
    "mistral": _openai_compat(
        name="mistral",
        base_url="https://api.mistral.ai/v1/chat/completions",
        key=MISTRAL_API_KEY,
        default_model=MISTRAL_MODELS[0] if MISTRAL_MODELS else None,
    ),
    "siliconflow": _openai_compat(
        name="siliconflow",
        base_url="https://api.siliconflow.cn/v1/chat/completions",
        key=SILICONFLOW_API_KEY,
        default_model=SILICONFLOW_MODELS[0] if SILICONFLOW_MODELS else None,
    ),
    "requesty": _openai_compat(
        name="requesty",
        base_url="https://api.requesty.ai/v1/chat/completions",
        key=REQUESTY_API_KEY,
        default_model=REQUESTY_MODEL,
    ),
    "modelscope": _openai_compat(
        name="modelscope",
        base_url="https://api-inference.modelscope.cn/v1/chat/completions",
        key=MODELSCOPE_API_KEY,
        default_model=MODELSCOPE_MODELS[0] if MODELSCOPE_MODELS else None,
    ),
    "ollama_cloud": _openai_compat(
        name="ollama_cloud",
        base_url=os.getenv("OLLAMA_API_URL", "https://api.ollama.ai/v1/chat/completions"),
        key=OLLAMA_API_KEY,
        default_model="mistral-large-3:675b-cloud",
    ),
    "nara_router": _openai_compat(
        name="nara_router",
        base_url="https://api.nararouter.com/v1/chat/completions",
        key=NARAROUTER_API_KEY,
        default_model="minimax-m3",
    ),
    "huggingface": _openai_compat(
        name="huggingface",
        base_url="https://api-inference.huggingface.co/v1/chat/completions",
        key=HF_API_KEY,
        default_model="meta-llama/Llama-3.2-3B-Instruct",
    ),
    "github_models": _openai_compat(
        name="github_models",
        base_url="https://models.inference.ai.azure.com/v1/chat/completions",
        key=GITHUB_MODELS_TOKEN,
        default_model=GITHUB_MODEL,
    ),
    "zhipuai": _openai_compat(
        name="zhipuai",
        base_url="https://open.bigmodel.cn/api/paas/v4/chat/completions",
        key=ZAI_API_KEY,
        default_model="glm-4.5-flash",
    ),
    "teamorouter": _openai_compat(
        name="teamorouter",
        base_url=os.getenv("TEAMOROUTER_API_URL", "https://router.teamorouter.com/v1/chat/completions"),
        key=TEAMOROUTER_API_KEY,
        default_model=TEAMOROUTER_MODEL,
    ),
    "ovhcloud": _openai_compat(
        name="ovhcloud",
        base_url="https://api.ai.cloud.ovh.net/v1/chat/completions",
        key=OVHCLOUD_API_KEY,
        default_model=OVHCLOUD_MODELS[0] if OVHCLOUD_MODELS else None,
    ),
    # Keyless providers
    "agnes_ai": _openai_compat(
        name="agnes_ai",
        base_url="https://api.agnes.ai/v1/chat/completions",
        key=os.getenv("AGNES_API_KEY", ""),
        default_model="agnes-2.0-flash",
    ),
    "bazaarlink": _openai_compat(
        name="bazaarlink",
        base_url="https://api.bazaarlink.ai/v1/chat/completions",
        key=os.getenv("BAZAARLINK_API_KEY", ""),
        default_model="auto:free",
    ),
    "nrouter": _openai_compat(
        name="nrouter",
        base_url="https://api.nrouter.ai/v1/chat/completions",
        key=os.getenv("NROUTER_API_KEY", ""),
        default_model=os.getenv("NROUTER_MODEL", ""),
    ),
    "qoder": _openai_compat(
        name="qoder",
        base_url="https://api.qoder.ai/v1/chat/completions",
        key=os.getenv("QODER_API_KEY", ""),
        default_model="qwen3-coder",
    ),
    "manifest": _openai_compat(
        name="manifest",
        base_url="https://api.manifest.ai/v1/chat/completions",
        key=os.getenv("MANIFEST_API_KEY", ""),
        default_model="auto:free",
    ),
    "glama": _openai_compat(
        name="glama",
        base_url="https://api.glama.ai/v1/chat/completions",
        key=os.getenv("GLAMA_API_KEY", ""),
        default_model="gpt-3.5-turbo",
    ),
    "anyapi": _openai_compat(
        name="anyapi",
        base_url="https://api.anyapi.ai/v1/chat/completions",
        key=os.getenv("ANYAPI_API_KEY", ""),
        default_model="poolside/laguna-xs.2:free",
    ),
    "bifrost": _openai_compat(
        name="bifrost",
        base_url="https://api.bifrost.ai/v1/chat/completions",
        key="",
        default_model="llama3.1:70b",
    ),
    "freegpt4_api": _openai_compat(
        name="freegpt4_api",
        base_url="https://api.freegpt4.ai/v1/chat/completions",
        key="",
        default_model="gpt-4",
    ),
    "puter": _openai_compat(
        name="puter",
        base_url="https://api.puter.com/v1/chat/completions",
        key="",
        default_model="gpt-3.5-turbo",
    ),
    "freetheai": _openai_compat(
        name="freetheai",
        base_url="https://api.freetheai.ai/v1/chat/completions",
        key="",
        default_model="gpt-3.5-turbo",
    ),
    "omnigpt_gateway": _openai_compat(
        name="omnigpt_gateway",
        base_url="https://api.omnigpt.ai/v1/chat/completions",
        key="",
        default_model="gpt-3.5-turbo",
    ),
    "opencode_zen": _openai_compat(
        name="opencode_zen",
        base_url="https://api.opencodezen.ai/v1/chat/completions",
        key="",
        default_model="qwen3-coder",
    ),
    "freeflow": _openai_compat(
        name="freeflow",
        base_url="https://api.freeflow.ai/v1/chat/completions",
        key="",
        default_model="gpt-3.5-turbo",
    ),
    "keylessai": _openai_compat(
        name="keylessai",
        base_url="https://api.keylessai.ai/v1/chat/completions",
        key="",
        default_model="gpt-3.5-turbo",
    ),
    "chubvenus": _openai_compat(
        name="chubvenus",
        base_url="https://api.chubvenus.ai/v1/chat/completions",
        key="",
        default_model="gpt-3.5-turbo",
    ),
    "blockrun": _openai_compat(
        name="blockrun",
        base_url="https://api.blockrun.ai/v1/chat/completions",
        key="",
        default_model="deepseek-v4-flash",
    ),
    "aymo": _openai_compat(
        name="aymo",
        base_url="https://api.aymo.ai/v1/chat/completions",
        key="",
        default_model="gemini-flash",
    ),
    "zerotwo": _openai_compat(
        name="zerotwo",
        base_url="https://api.zerotwo.ai/v1/chat/completions",
        key="",
        default_model="gpt-5-mini",
    ),
    "aihubmix": _openai_compat(
        name="aihubmix",
        base_url="https://api.aihubmix.ai/v1/chat/completions",
        key="",
        default_model="gpt-5.5",
    ),
    "aisure": _openai_compat(
        name="aisure",
        base_url="https://api.aisure.ai/v1/chat/completions",
        key="",
        default_model="gpt-4o",
    ),
    "proxygatellm": _openai_compat(
        name="proxygatellm",
        base_url="https://api.proxygatellm.ai/v1/chat/completions",
        key="",
        default_model="gpt-3.5-turbo",
    ),
    "free_llm_gateway": _openai_compat(
        name="free_llm_gateway",
        base_url=os.getenv("FREE_LLM_GATEWAY_URL", "http://localhost:8000/v1/chat/completions"),
        key="",
        default_model="auto",
    ),
    "ninerouter": _openai_compat(
        name="ninerouter",
        base_url="https://api.ninerouter.ai/v1/chat/completions",
        key="",
        default_model="auto",
    ),
    "local": local_adapter,
}


__all__ = [
    "PROVIDER_HTTP",
    "cloudflare_adapter",
    "gemini_adapter",
    "local_adapter",
]
