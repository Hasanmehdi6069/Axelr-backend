# api/providers.py
"""
AXELR Provider Layer
====================
Every LLM provider adapter + the function map + the chain + the ranking
helpers. This is the only place provider HTTP gets called.

Design:
  * One `async def call_*(prompt, max_tokens, temp, model)` per provider.
  * `PROVIDER_FUNC_MAP` — name → callable. Used by routing.
  * `PROVIDER_KEY_CHECK` — name → bool. Used to skip unconfigured providers.
  * `PROVIDER_CHAIN_ENTRIES` — (name, func, models). Single source of truth.
  * `PROVIDER_CHAIN` + `PROVIDER_MODELS` — derived from the entries.
  * `WORKSPACE_PRIORITY` — per-workspace provider ordering.
  * `ProviderMetrics` + `record_provider_result` — latency-aware scoring.
  * `get_provider_order`, `get_dynamically_ranked_providers` — used by routers.
"""
from __future__ import annotations

import logging
import os
import time
import urllib.parse
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import (
    AGNES_API_KEY, AGNES_MODEL,
    AIHUBMIX_MODELS, AISURE_MODEL, ANYAPI_API_KEY, ANYAPI_MODEL,
    AYMO_MODELS, BAZAARLINK_API_KEY, BAZAARLINK_MODEL,
    BIFROST_MODELS, BLOCKRUN_MODELS,
    CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_KEY, CLOUDFLARE_MODEL,
    FREE_TIER_TOKEN_LIMIT, FREEGPT4_MODELS, FREEFLOW_MODEL, FREETHEAI_MODEL,
    GEMINI_API_KEY, GEMINI_MODELS,
    GITHUB_MODEL, GITHUB_MODELS_TOKEN, GLAMA_API_KEY, GLAMA_MODEL,
    GROQ_API_KEY, GROQ_MODELS,
    HF_API_KEY, HF_MODELS, HTTP_CLIENT,
    KEYLESS_MODEL, MANIFEST_API_KEY, MANIFEST_MODEL,
    MISTRAL_API_KEY, MISTRAL_MODELS,
    MODELSCOPE_API_KEY, MODELSCOPE_MODELS,
    NARA_MODELS, NARAROUTER_API_KEY,
    NROUTER_API_KEY, NROUTER_MODEL,
    OLLAMA_API_KEY, OLLAMA_MODELS, OMNIGPT_MODELS, OPENDODE_MODELS,
    OPENROUTER_API_KEY, OPENROUTER_MODELS,
    OVHCLOUD_API_KEY, OVHCLOUD_MODELS,
    PUTER_MODEL, QODER_MODEL, REQUESTY_API_KEY, REQUESTY_MODEL,
    SILICONFLOW_API_KEY, SILICONFLOW_MODELS,
    TEAMOROUTER_API_KEY, TEAMOROUTER_MODEL,
    ZAI_API_KEY, ZEROTWO_MODELS, ZHIPU_MODEL,
)

logger = logging.getLogger("axelr.providers")


# ── Helpers ──────────────────────────────────────────────────────────────────

async def http_post_async(
    url: str,
    headers: dict[str, str],
    json_data: dict[str, Any],
    timeout: float = 8.0,
) -> Any:
    """POST JSON; follow redirects; raise on 4xx (429/402 classified)."""
    try:
        resp = await HTTP_CLIENT.post(url, headers=headers, json=json_data, timeout=timeout)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"text": resp.text}
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code == 429:
            raise Exception(f"Quota exceeded: {e.response.text}")
        if code == 402:
            raise Exception("Payment required – skipping provider")
        if code in (301, 302, 303, 307, 308):
            loc = e.response.headers.get("Location")
            if loc:
                return await http_post_async(loc, headers, json_data, timeout)
        raise Exception(f"HTTP error {code}: {e.response.text[:200]}")
    except Exception as e:
        raise Exception(f"HTTP request failed: {e}")


# ── Gemini (text + vision) ───────────────────────────────────────────────────

async def _call_gemini_internal(
    prompt: str,
    max_tokens: int,
    temp: float,
    model: str | None = None,
    image_data_b64: str | None = None,
) -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set")

    model_name = model or GEMINI_MODELS[0]
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model_name}:generateContent?key={GEMINI_API_KEY}"
    )
    parts: list[dict[str, Any]] = []
    if image_data_b64:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": image_data_b64}})
    parts.append({"text": prompt})

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": temp, "maxOutputTokens": max_tokens},
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT",        "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH",       "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ],
    }
    resp = await HTTP_CLIENT.post(
        url, json=payload, headers={"Content-Type": "application/json"}, timeout=45.0,
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Gemini malformed response: {data}") from e


async def call_gemini(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    return await _call_gemini_internal(prompt, max_tokens, temp, model=model)


async def call_gemini_vision(
    prompt: str, image_data_b64: str, max_tokens: int, temp: float, model: str | None = None,
) -> str:
    return await _call_gemini_internal(
        prompt, max_tokens, temp, model=model, image_data_b64=image_data_b64,
    )


# ── Groq ─────────────────────────────────────────────────────────────────────

async def call_groq(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    if not GROQ_API_KEY:
        raise Exception("GROQ_API_KEY missing")
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": model or GROQ_MODELS[0],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens, "temperature": temp, "stream": False,
    }
    try:
        resp = await http_post_async(url, headers, payload)
        if resp.get("choices"):
            return resp["choices"][0]["message"]["content"]
        raise Exception("No choices returned")
    except Exception as e:
        raise Exception(f"Groq error: {e}")


# ── Cloudflare ───────────────────────────────────────────────────────────────

async def call_cloudflare(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    if not CLOUDFLARE_API_KEY or not CLOUDFLARE_ACCOUNT_ID:
        raise Exception("Cloudflare credentials missing")
    url = f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run/{model or CLOUDFLARE_MODEL}"
    headers = {"Authorization": f"Bearer {CLOUDFLARE_API_KEY}", "Content-Type": "application/json"}
    payload = {"messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp}
    resp = await http_post_async(url, headers, payload)
    return resp.get("result", {}).get("response", "")


# ── OpenAI-compatible providers (each ~10 lines, same shape) ─────────────────

async def call_openrouter(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    if not OPENROUTER_API_KEY:
        raise Exception("OPENROUTER_API_KEY missing")
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://axelr.in",
        "X-Title": "Axelr AI",
    }
    payload = {"model": model or OPENROUTER_MODELS[0],
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_modelscope(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    if not MODELSCOPE_API_KEY:
        raise Exception("MODELSCOPE_API_KEY missing")
    url = os.getenv("MODELSCOPE_URL", "https://api.modelscope.cn/v1/chat/completions")
    headers = {"Authorization": f"Bearer {MODELSCOPE_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": model or MODELSCOPE_MODELS[0],
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_ollama_cloud(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    if not OLLAMA_API_KEY:
        raise Exception("OLLAMA_API_KEY missing")
    url = os.getenv("OLLAMA_API_URL", "https://api.ollama.ai/v1/chat/completions")
    headers = {"Authorization": f"Bearer {OLLAMA_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": model or OLLAMA_MODELS[0],
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_nara_router(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    if not NARAROUTER_API_KEY:
        raise Exception("NARAROUTER_API_KEY missing")
    url = os.getenv("NARA_ROUTER_URL", "https://router.bynara.id/v1/chat/completions")
    headers = {"Authorization": f"Bearer {NARAROUTER_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": model or NARA_MODELS[0],
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_mistral(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    if not MISTRAL_API_KEY:
        raise Exception("MISTRAL_API_KEY missing")
    url = "https://api.mistral.ai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {MISTRAL_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": model or MISTRAL_MODELS[0],
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_huggingface(prompt: str, max_tokens: int, temp: float, model: str) -> str:
    if not HF_API_KEY:
        raise Exception("HF_API_KEY missing")
    url = f"https://api-inference.huggingface.co/models/{model}"
    headers = {"Authorization": f"Bearer {HF_API_KEY}"}
    payload = {"inputs": prompt,
               "parameters": {"max_new_tokens": max_tokens, "temperature": temp,
                              "return_full_text": False}}
    resp = await http_post_async(url, headers, payload)
    if isinstance(resp, dict):
        if "generated_text" in resp:
            return resp["generated_text"]
        if "text" in resp:
            return resp["text"]
    elif isinstance(resp, list) and resp:
        first = resp[0]
        if isinstance(first, dict):
            return first.get("generated_text", "")
        if isinstance(first, str):
            return first
    if isinstance(resp, dict):
        for v in resp.values():
            if isinstance(v, str) and len(v) > 10:
                return v
    return ""


async def call_github_models(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    if not GITHUB_MODELS_TOKEN:
        raise Exception("GITHUB_MODELS_TOKEN missing")
    base = os.getenv("GITHUB_MODELS_URL", "https://models.inference.ai.azure.com/chat/completions")
    full = f"{base}?{urllib.parse.urlencode({'api-version': '2024-05-01-preview'})}"
    headers = {"Authorization": f"Bearer {GITHUB_MODELS_TOKEN}", "Content-Type": "application/json"}
    payload = {"model": model or GITHUB_MODEL,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(full, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_ovhcloud(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    if not OVHCLOUD_API_KEY:
        raise Exception("OVHCLOUD_API_KEY missing")
    url = os.getenv("OVHCLOUD_URL", "https://api.ai.cloud.ovh.net/v1/chat/completions")
    headers = {"Authorization": f"Bearer {OVHCLOUD_API_KEY}",
               "Content-Type": "application/json",
               "X-OVH-Project": os.getenv("OVH_PROJECT_ID", "")}
    payload = {"model": model or OVHCLOUD_MODELS[0],
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_siliconflow(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    if not SILICONFLOW_API_KEY:
        raise Exception("SILICONFLOW_API_KEY missing")
    url = "https://api.siliconflow.cn/v1/chat/completions"
    headers = {"Authorization": f"Bearer {SILICONFLOW_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": model or SILICONFLOW_MODELS[0],
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_agnes_ai(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    if not AGNES_API_KEY:
        raise Exception("AGNES_API_KEY missing")
    url = os.getenv("AGNES_URL", "https://api.agnes.ai/v1/chat/completions")
    headers = {"Authorization": f"Bearer {AGNES_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": model or AGNES_MODEL,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_bifrost(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    url = os.getenv("BIFROST_URL", "http://localhost:8080/v1/chat/completions")
    headers = {"Content-Type": "application/json"}
    payload = {"model": model or BIFROST_MODELS[0],
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_freegpt4_api(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    url = os.getenv("FREEGPT4_URL", "http://localhost:5500/")
    full = f"{url}?text={urllib.parse.quote(prompt)}"
    resp = await HTTP_CLIENT.get(full)
    resp.raise_for_status()
    return resp.text.strip()


async def call_bazaarlink(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    if not BAZAARLINK_API_KEY:
        raise Exception("BAZAARLINK_API_KEY missing")
    url = os.getenv("BAZAARLINK_URL", "https://api.bazaarlink.io/v1/chat/completions")
    headers = {"Authorization": f"Bearer {BAZAARLINK_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": model or BAZAARLINK_MODEL,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_requesty(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    if not REQUESTY_API_KEY:
        raise Exception("REQUESTY_API_KEY missing")
    url = os.getenv("REQUESTY_URL", "https://api.requesty.ai/v1/chat/completions")
    headers = {"Authorization": f"Bearer {REQUESTY_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": model or REQUESTY_MODEL,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_nrouter(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    if not NROUTER_API_KEY:
        raise Exception("NROUTER_API_KEY missing")
    url = os.getenv("NROUTER_URL", "https://api.nrouter.io/v1/chat/completions")
    headers = {"Authorization": f"Bearer {NROUTER_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": model or NROUTER_MODEL,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_puter(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    url = os.getenv("PUTER_URL", "https://api.puter.com/v1/chat/completions")
    headers = {"Content-Type": "application/json"}
    payload = {"model": model or PUTER_MODEL,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_freetheai(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    url = os.getenv("FREETHEAI_URL", "https://api.freetheai.com/v1/chat/completions")
    headers = {"Content-Type": "application/json"}
    payload = {"model": model or FREETHEAI_MODEL,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_omnigpt_gateway(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    url = os.getenv("OMNIGPT_URL", "https://api.omnigpt.io/v1/chat/completions")
    headers = {"Content-Type": "application/json"}
    payload = {"model": model or OMNIGPT_MODELS[0],
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_opencode_zen(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    url = os.getenv("OPENCODE_URL", "https://api.opencode.zen/v1/chat/completions")
    headers = {"Content-Type": "application/json"}
    payload = {"model": model or OPENDODE_MODELS[0],
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_freeflow(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    url = os.getenv("FREEFLOW_URL", "https://freeflow.llm/v1/chat/completions")
    headers = {"Content-Type": "application/json"}
    payload = {"model": model or FREEFLOW_MODEL,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_qoder(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    key = os.getenv("QODER_API_KEY")
    if not key:
        raise Exception("QODER_API_KEY missing")
    url = os.getenv("QODER_URL", "https://api.qoder.com/v1/chat/completions")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {"model": model or QODER_MODEL,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_manifest(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    if not MANIFEST_API_KEY:
        raise Exception("MANIFEST_API_KEY missing")
    url = os.getenv("MANIFEST_URL", "https://api.manifest.build/v1/chat/completions")
    headers = {"Authorization": f"Bearer {MANIFEST_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": model or MANIFEST_MODEL,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_keylessai(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    url = os.getenv("KEYLESS_URL", "https://api.keyless.ai/v1/chat/completions")
    headers = {"Content-Type": "application/json"}
    payload = {"model": model or KEYLESS_MODEL,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_glama(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    if not GLAMA_API_KEY:
        raise Exception("GLAMA_API_KEY missing")
    url = os.getenv("GLAMA_URL", "https://api.glama.ai/v1/chat/completions")
    headers = {"Authorization": f"Bearer {GLAMA_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": model or GLAMA_MODEL,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_chubvenus(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    url = os.getenv("CHUBVENUS_URL", "https://api.chub.ai/v1/chat/completions")
    headers = {"Content-Type": "application/json"}
    payload = {"model": model or "gpt-3.5-turbo",
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_blockrun(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    url = os.getenv("BLOCKRUN_URL", "https://api.blockrun.com/v1/chat/completions")
    headers = {"Content-Type": "application/json"}
    payload = {"model": model or BLOCKRUN_MODELS[0],
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_anyapi(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    if not ANYAPI_API_KEY:
        raise Exception("ANYAPI_API_KEY missing")
    url = os.getenv("BASEURL", "https://api.anyapi.ai/v1/chat/completions")
    headers = {"Authorization": f"Bearer {ANYAPI_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": model or ANYAPI_MODEL,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_aymo(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    url = os.getenv("AYMO_URL", "https://api.aymo.ai/v1/chat/completions")
    headers = {"Content-Type": "application/json"}
    payload = {"model": model or AYMO_MODELS[0],
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_zerotwo(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    # ZeroTwo requires a CSRF token from the homepage.
    url = os.getenv("ZEROTWO_URL", "https://api.zerotwo.ai/v1/chat/completions")
    async with httpx.AsyncClient() as client:
        r = await client.get("https://zerotwo.ai/")
        csrf = r.cookies.get("__Host-next-auth.csrf-token")
    if not csrf:
        raise Exception("Could not get CSRF token from zerotwo.ai")
    headers = {"Content-Type": "application/json", "X-CSRF-Token": csrf}
    payload = {"model": model or ZEROTWO_MODELS[0],
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_aihubmix(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    url = os.getenv("AIHUBMIX_URL", "https://api.inferera.com/chat/completions")
    headers = {"Content-Type": "application/json"}
    payload = {"model": model or AIHUBMIX_MODELS[0],
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_aisure(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    url = os.getenv("AISURE_URL", "https://api.aisure.ai/v1/chat/completions")
    headers = {"Content-Type": "application/json"}
    payload = {"model": model or AISURE_MODEL,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_zhipu(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    if not ZAI_API_KEY:
        raise Exception("ZAI_API_KEY missing")
    url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    headers = {"Authorization": f"Bearer {ZAI_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": model or ZHIPU_MODEL,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_teamorouter(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    if not TEAMOROUTER_API_KEY:
        raise Exception("TEAMOROUTER_API_KEY missing")
    url = os.getenv("TEAMOROUTER_URL", "https://api.teamorouter.io/v1/chat/completions")
    headers = {"Authorization": f"Bearer {TEAMOROUTER_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": model or TEAMOROUTER_MODEL,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_proxygatellm(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    url = "https://api.proxygatellm.com/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {"model": model or "gpt-3.5-turbo",
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_free_llm_gateway(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    url = os.getenv("FREE_LLM_GATEWAY_URL", "http://free-llm-gateway:8000/v1/chat/completions")
    headers = {"Content-Type": "application/json"}
    payload = {"model": model or "auto",
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


async def call_ninerouter(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    url = os.getenv("NINEROUTER_URL", "https://api.9router.io/v1/chat/completions")
    headers = {"Content-Type": "application/json"}
    payload = {"model": model or "auto",
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


# ── Local fallback ───────────────────────────────────────────────────────────

def build_local_fallback_response(workspace: str, task_type: str, prompt: str) -> str:
    text = (prompt or "").strip()
    if not text:
        return "I'm sorry, all AI services are temporarily unavailable. Please try again in a few minutes."
    if workspace == "design":
        return f'Design concept for: "{text[:120]}".\nHere\'s a starting point – refine it and I\'ll assist further.'
    if workspace == "data":
        return f'Data analysis for: "{text[:120]}".\nPlease provide the source data or example output for a more precise analysis.'
    if task_type == "touch_fix":
        return f'Debugging: "{text[:120]}".\nPlease share the full error, file name, and expected behaviour.'
    return f'Request received: "{text[:160]}".\nI can help with a concise plan, code snippet, or structured answer – tell me more specifics.'


async def call_local_fallback(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    return build_local_fallback_response("core", "core", prompt)


# ── Function map ─────────────────────────────────────────────────────────────

PROVIDER_FUNC_MAP: dict[str, Any] = {
    "gemini": call_gemini,
    "groq": call_groq,
    "cloudflare": call_cloudflare,
    "openrouter": call_openrouter,
    "modelscope": call_modelscope,
    "ollama_cloud": call_ollama_cloud,
    "nara_router": call_nara_router,
    "mistral": call_mistral,
    "huggingface": call_huggingface,
    "github_models": call_github_models,
    "ovhcloud": call_ovhcloud,
    "siliconflow": call_siliconflow,
    "agnes_ai": call_agnes_ai,
    "bifrost": call_bifrost,
    "freegpt4_api": call_freegpt4_api,
    "bazaarlink": call_bazaarlink,
    "requesty": call_requesty,
    "nrouter": call_nrouter,
    "puter": call_puter,
    "freetheai": call_freetheai,
    "omnigpt_gateway": call_omnigpt_gateway,
    "opencode_zen": call_opencode_zen,
    "freeflow": call_freeflow,
    "qoder": call_qoder,
    "manifest": call_manifest,
    "keylessai": call_keylessai,
    "glama": call_glama,
    "chubvenus": call_chubvenus,
    "blockrun": call_blockrun,
    "anyapi": call_anyapi,
    "aymo": call_aymo,
    "zerotwo": call_zerotwo,
    "aihubmix": call_aihubmix,
    "aisure": call_aisure,
    "zhipuai": call_zhipu,
    "teamorouter": call_teamorouter,
    "proxygatellm": call_proxygatellm,
    "free_llm_gateway": call_free_llm_gateway,
    "ninerouter": call_ninerouter,
    "local": call_local_fallback,
}


# ── Key checks ───────────────────────────────────────────────────────────────

PROVIDER_KEY_CHECK: dict[str, bool] = {
    "gemini": bool(GEMINI_API_KEY),
    "groq": bool(GROQ_API_KEY),
    "cloudflare": bool(CLOUDFLARE_API_KEY and CLOUDFLARE_ACCOUNT_ID),
    "openrouter": bool(OPENROUTER_API_KEY),
    "modelscope": bool(MODELSCOPE_API_KEY),
    "ollama_cloud": bool(OLLAMA_API_KEY),
    "nara_router": bool(NARAROUTER_API_KEY),
    "mistral": bool(MISTRAL_API_KEY),
    "huggingface": bool(HF_API_KEY),
    "github_models": bool(GITHUB_MODELS_TOKEN),
    "ovhcloud": bool(OVHCLOUD_API_KEY),
    "siliconflow": bool(SILICONFLOW_API_KEY),
    "agnes_ai": bool(AGNES_API_KEY),
    "bifrost": True,
    "freegpt4_api": True,
    "bazaarlink": bool(BAZAARLINK_API_KEY),
    "requesty": bool(REQUESTY_API_KEY),
    "nrouter": bool(NROUTER_API_KEY),
    "puter": True,
    "freetheai": True,
    "omnigpt_gateway": True,
    "opencode_zen": True,
    "freeflow": True,
    "qoder": bool(os.getenv("QODER_API_KEY")),
    "manifest": bool(MANIFEST_API_KEY),
    "keylessai": True,
    "glama": bool(GLAMA_API_KEY),
    "chubvenus": True,
    "blockrun": True,
    "anyapi": bool(ANYAPI_API_KEY),
    "aymo": True,
    "zerotwo": True,
    "aihubmix": True,
    "aisure": True,
    "zhipuai": bool(ZAI_API_KEY),
    "teamorouter": bool(TEAMOROUTER_API_KEY),
    "proxygatellm": True,
    "free_llm_gateway": bool(os.getenv("FREE_LLM_GATEWAY_URL")),
    "ninerouter": True,
    "local": True,
}


# ── Chain (single source of truth for name + func + models) ──────────────────

PROVIDER_CHAIN_ENTRIES: list[tuple[str, Any, list[str]]] = [
    ("gemini", call_gemini, GEMINI_MODELS),
    ("groq", call_groq, GROQ_MODELS),
    ("openrouter", call_openrouter, OPENROUTER_MODELS),
    ("cloudflare", call_cloudflare, [CLOUDFLARE_MODEL]),
    ("modelscope", call_modelscope, MODELSCOPE_MODELS),
    ("ollama_cloud", call_ollama_cloud, OLLAMA_MODELS),
    ("nara_router", call_nara_router, NARA_MODELS),
    ("mistral", call_mistral, MISTRAL_MODELS),
    ("huggingface", call_huggingface, HF_MODELS),
    ("github_models", call_github_models, [GITHUB_MODEL]),
    ("zhipuai", call_zhipu, [ZHIPU_MODEL]),
    ("proxygatellm", call_proxygatellm, ["auto"]),
    ("free_llm_gateway", call_free_llm_gateway, ["auto"]),
    ("ninerouter", call_ninerouter, ["auto"]),
    ("teamorouter", call_teamorouter, [TEAMOROUTER_MODEL]),
    ("ovhcloud", call_ovhcloud, OVHCLOUD_MODELS),
    ("siliconflow", call_siliconflow, SILICONFLOW_MODELS),
    ("agnes_ai", call_agnes_ai, [AGNES_MODEL]),
    ("bifrost", call_bifrost, BIFROST_MODELS),
    ("freegpt4_api", call_freegpt4_api, FREEGPT4_MODELS),
    ("bazaarlink", call_bazaarlink, [BAZAARLINK_MODEL]),
    ("requesty", call_requesty, [REQUESTY_MODEL]),
    ("freetheai", call_freetheai, [FREETHEAI_MODEL]),
    ("omnigpt_gateway", call_omnigpt_gateway, OMNIGPT_MODELS),
    ("opencode_zen", call_opencode_zen, OPENDODE_MODELS),
    ("freeflow", call_freeflow, [FREEFLOW_MODEL]),
    ("qoder", call_qoder, [QODER_MODEL]),
    ("manifest", call_manifest, [MANIFEST_MODEL]),
    ("keylessai", call_keylessai, [KEYLESS_MODEL]),
    ("glama", call_glama, [GLAMA_MODEL]),
    ("chubvenus", call_chubvenus, ["gpt-3.5-turbo"]),
    ("blockrun", call_blockrun, BLOCKRUN_MODELS),
    ("anyapi", call_anyapi, [ANYAPI_MODEL]),
    ("aymo", call_aymo, AYMO_MODELS),
    ("zerotwo", call_zerotwo, ZEROTWO_MODELS),
    ("aihubmix", call_aihubmix, AIHUBMIX_MODELS),
    ("aisure", call_aisure, [AISURE_MODEL]),
    ("local", call_local_fallback, []),
]

PROVIDER_CHAIN: list[tuple[str, Any]] = [(n, f) for n, f, _ in PROVIDER_CHAIN_ENTRIES]
PROVIDER_MODELS: dict[str, list[str]] = {n: m for n, _, m in PROVIDER_CHAIN_ENTRIES}


# ── Per-workspace ordering ───────────────────────────────────────────────────

WORKSPACE_PRIORITY: dict[str, list[str]] = {
    "data": [
        "gemini", "modelscope", "groq", "openrouter", "ollama_cloud", "nara_router",
        "proxygatellm", "free_llm_gateway", "ninerouter",
        "mistral", "ovhcloud", "siliconflow", "zhipuai", "teamorouter", "nrouter",
        "bazaarlink", "requesty", "qoder", "manifest",
        "keylessai", "anyapi", "aymo", "zerotwo", "aihubmix", "aisure",
    ],
    "design": [
        "cloudflare", "groq", "gemini", "openrouter", "modelscope", "nara_router",
        "proxygatellm", "free_llm_gateway", "ninerouter",
        "agnes_ai", "siliconflow", "zhipuai", "teamorouter", "bifrost",
        "freegpt4_api", "ovhcloud", "nrouter", "puter", "omnigpt_gateway",
        "opencode_zen", "qoder", "keylessai", "glama", "chubvenus", "blockrun", "anyapi",
    ],
    "core": [
        "gemini", "modelscope", "groq", "openrouter", "ollama_cloud", "nara_router",
        "proxygatellm", "free_llm_gateway", "ninerouter",
        "mistral", "huggingface", "github_models", "zhipuai", "teamorouter",
        "ovhcloud", "siliconflow", "nrouter", "bazaarlink", "requesty",
        "qoder", "freeflow", "manifest", "keylessai", "glama", "chubvenus",
        "anyapi", "aymo", "zerotwo", "aihubmix", "aisure",
    ],
    "prompt": [
        "gemini", "openrouter", "modelscope", "groq", "nara_router", "ollama_cloud",
        "proxygatellm", "free_llm_gateway", "ninerouter",
        "zhipuai", "teamorouter", "requesty", "bazaarlink",
    ],
    "touch_fix": [
        "groq", "mistral", "github_models", "zhipuai", "teamorouter", "nara_router",
        "proxygatellm", "free_llm_gateway", "ninerouter",
        "ovhcloud", "qoder", "opencode_zen",
    ],
}


def get_provider_order(workspace: str) -> list[str]:
    provider_names = [n for n, _ in PROVIDER_CHAIN if n != "local"]
    priority = WORKSPACE_PRIORITY.get(workspace, WORKSPACE_PRIORITY["core"])
    ordered: list[str] = []
    for name in priority:
        if name in provider_names and name not in ordered:
            ordered.append(name)
    for name in provider_names:
        if name not in ordered:
            ordered.append(name)
    ordered.append("local")
    return ordered


# ── Latency-aware scoring ────────────────────────────────────────────────────

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

    def get_score(self, workspace: str) -> float:
        if not self.is_available:
            return -9999.0
        total = len(self.history)
        success_rate = (sum(self.history) / total) if total > 0 else 0.85
        avg_lat = (sum(self.latencies) / len(self.latencies)) if self.latencies else 1.5
        affinity = 15 if (
            (workspace == "design" and self.name in {"cloudflare", "groq", "gemini"})
            or (workspace == "data" and self.name in {"gemini", "modelscope", "groq"})
        ) else 0
        return (success_rate * 100) - (avg_lat * 12) - (self.consecutive_failures * 25) + affinity


PROVIDER_TRACKER: dict[str, ProviderMetrics] = {
    name: ProviderMetrics(name=name)
    for name, _ in PROVIDER_CHAIN
    if name != "local"
}


def record_provider_result(
    name: str,
    latency: float,
    success: bool,
    is_rate_limit: bool = False,
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
        if is_rate_limit:
            p.cooldown_until = time.time() + 1800
            logger.warning("provider_rate_limited provider=%s", name)
        elif p.consecutive_failures >= 3:
            p.cooldown_until = time.time() + 300
            logger.warning("provider_circuit_tripped provider=%s", name)


def get_dynamically_ranked_providers(workspace: str) -> list[str]:
    valid = [
        p for p in PROVIDER_TRACKER.values()
        if p.is_available and PROVIDER_KEY_CHECK.get(p.name, False)
    ]
    valid.sort(key=lambda x: x.get_score(workspace), reverse=True)
    ranked = [p.name for p in valid]
    ranked.append("local")
    return ranked


# ── Public surface ───────────────────────────────────────────────────────────
__all__ = [
    "http_post_async",
    "call_gemini", "call_gemini_vision",
    "call_groq", "call_cloudflare", "call_openrouter", "call_modelscope",
    "call_ollama_cloud", "call_nara_router", "call_mistral", "call_huggingface",
    "call_github_models", "call_ovhcloud", "call_siliconflow", "call_agnes_ai",
    "call_bifrost", "call_freegpt4_api", "call_bazaarlink", "call_requesty",
    "call_nrouter", "call_puter", "call_freetheai", "call_omnigpt_gateway",
    "call_opencode_zen", "call_freeflow", "call_qoder", "call_manifest",
    "call_keylessai", "call_glama", "call_chubvenus", "call_blockrun",
    "call_anyapi", "call_aymo", "call_zerotwo", "call_aihubmix", "call_aisure",
    "call_zhipu", "call_teamorouter", "call_proxygatellm",
    "call_free_llm_gateway", "call_ninerouter",
    "call_local_fallback", "build_local_fallback_response",
    "PROVIDER_FUNC_MAP", "PROVIDER_KEY_CHECK",
    "PROVIDER_CHAIN_ENTRIES", "PROVIDER_CHAIN", "PROVIDER_MODELS",
    "WORKSPACE_PRIORITY", "get_provider_order",
    "ProviderMetrics", "PROVIDER_TRACKER", "record_provider_result",
    "get_dynamically_ranked_providers",
    "FREE_TIER_TOKEN_LIMIT",
]