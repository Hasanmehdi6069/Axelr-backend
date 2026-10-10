# api/provider_http.py
"""
AXELR — Provider HTTP Adapters
===============================
Every outbound LLM HTTP call lives here. The catalog, ranking, and metrics
live in `api.providers` — this module owns only the wire.

Design
------
  §1 Shared POST helper
  §2 OpenAI-compatible factory — one call site for all OpenAI-shaped providers
  §3 Five non-standard adapters (Gemini, HF, CF, FreeGPT4, ZeroTwo)
  §4 Registry: name -> callable

Every adapter has the shape:
    async def call(prompt: str, max_tokens: int, temp: float, model: str | None) -> str

Every adapter raises on failure. Callers retry and record metrics.
"""
from __future__ import annotations

import logging
import os
import urllib.parse
from typing import Any, Awaitable, Callable

import httpx

from .config import (
    AGNES_API_KEY, AGNES_MODEL, ANYAPI_API_KEY, ANYAPI_MODEL,
    BAZAARLINK_API_KEY, BAZAARLINK_MODEL, CLOUDFLARE_ACCOUNT_ID,
    CLOUDFLARE_API_KEY, CLOUDFLARE_MODEL, FREEFLOW_MODEL, FREETHEAI_MODEL,
    GEMINI_API_KEY, GEMINI_MODELS, GITHUB_MODEL, GITHUB_MODELS_TOKEN,
    GLAMA_API_KEY, GLAMA_MODEL, GROQ_API_KEY, GROQ_MODELS, HF_API_KEY,
    MANIFEST_API_KEY, MANIFEST_MODEL, MISTRAL_API_KEY, MISTRAL_MODELS,
    MODELSCOPE_API_KEY, MODELSCOPE_MODELS, NARAROUTER_API_KEY, NROUTER_API_KEY,
    NROUTER_MODEL, OLLAMA_API_KEY, OPENROUTER_API_KEY, OPENROUTER_MODELS,
    OVHCLOUD_API_KEY, OVHCLOUD_MODELS, PUTER_MODEL, QODER_MODEL,
    REQUESTY_API_KEY, REQUESTY_MODEL, SILICONFLOW_API_KEY, SILICONFLOW_MODELS,
    TEAMOROUTER_API_KEY, TEAMOROUTER_MODEL, ZAI_API_KEY, ZHIPU_MODEL,
    get_http_client,
)

logger = logging.getLogger("axelr.provider_http")

Adapter = Callable[..., Awaitable[str]]


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Shared POST helper
# ═══════════════════════════════════════════════════════════════════════════
async def http_post_async(
    url: str,
    headers: dict[str, str],
    json_data: dict[str, Any],
    timeout: float = 8.0,
) -> Any:
    """POST JSON; follow redirects; classify 429/402; return parsed body."""
    try:
        client = get_http_client()
        resp = await client.post(url, headers=headers, json=json_data, timeout=timeout)
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
        if code in (301, 307, 308):
            loc = e.response.headers.get("Location")
            if loc:
                if not loc.startswith(("http://", "https://")):
                    from urllib.parse import urljoin
                    loc = urljoin(url, loc)
                return await http_post_async(loc, headers, json_data, timeout)
        raise Exception(f"HTTP error {code}: {e.response.text[:200]}")
    except Exception as e:
        raise Exception(f"HTTP request failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# §2 — OpenAI-compatible factory
# ═══════════════════════════════════════════════════════════════════════════
# Keyless providers: no Authorization header required; a missing key
# is not an error for these names.
_KEYLESS_PROVIDERS: frozenset[str] = frozenset({
    "bifrost", "free_llm_gateway", "ninerouter", "chubvenus", "keylessai",
    "omnigpt_gateway", "freetheai", "freeflow", "puter", "blockrun", "aymo",
    "zerotwo", "aihubmix", "aisure", "proxygatellm", "opencode_zen",
    "requesty",
})


def _openai_compat(
    *,
    name: str,
    base_url: str,
    key: str,
    default_model: str | None = None,
    extra_headers: dict[str, str] | None = None,
    extra_body: dict[str, Any] | None = None,
    timeout: float = 8.0,
) -> Adapter:
    """
    One factory for the OpenAI-shaped providers.
    The returned adapter preserves the canonical
    `(prompt, max_tokens, temp, model)` signature.
    """
    async def _call(
        prompt: str, max_tokens: int, temp: float, model: str | None = None,
    ) -> str:
        if not key and name not in _KEYLESS_PROVIDERS:
            raise Exception(f"{name}: API key missing")

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if extra_headers:
            headers.update(extra_headers)

        payload: dict[str, Any] = {
            "model": model or default_model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temp,
            "stream": False,
        }
        # ── Stage 1: per-query hyperparameter injection ────────────────
        try:
            from core.hyperparams import payload_extras
            payload.update(payload_extras(prompt))
        except Exception:
            pass
        if extra_body:
            payload.update(extra_body)

        try:
            resp = await http_post_async(base_url, headers, payload, timeout=timeout)
        except Exception as e:
            raise Exception(f"{name} error: {e}")
        if not resp.get("choices"):
            raise Exception(f"{name}: no choices returned")
        return resp["choices"][0]["message"]["content"]
    return _call


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Non-standard adapters
# ═══════════════════════════════════════════════════════════════════════════
async def _call_gemini_internal(
    prompt: str, max_tokens: int, temp: float,
    model: str | None = None, image_data_b64: str | None = None,
) -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set")
    model_name = model or (GEMINI_MODELS[0] if GEMINI_MODELS else "gemini-1.5-flash")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model_name}:generateContent?key={GEMINI_API_KEY}")
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
    data = await http_post_async(url, {"Content-Type": "application/json"}, payload, timeout=45.0)
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Gemini malformed response: {data}") from e


async def call_gemini(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    return await _call_gemini_internal(prompt, max_tokens, temp, model=model)


async def call_gemini_vision(
    prompt: str, image_data_b64: str, max_tokens: int, temp: float, model: str | None = None,
) -> str:
    return await _call_gemini_internal(prompt, max_tokens, temp, model=model,
                                       image_data_b64=image_data_b64)


async def call_cloudflare(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    if not CLOUDFLARE_API_KEY or not CLOUDFLARE_ACCOUNT_ID:
        raise Exception("Cloudflare credentials missing")
    url = (f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}"
           f"/ai/run/{model or CLOUDFLARE_MODEL}")
    headers = {"Authorization": f"Bearer {CLOUDFLARE_API_KEY}",
               "Content-Type": "application/json"}
    payload = {"messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp}
    resp = await http_post_async(url, headers, payload)
    return resp.get("result", {}).get("response", "")


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


async def call_freegpt4_api(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    url = os.getenv("FREEGPT4_URL", "http://localhost:5500/")
    full = f"{url}?text={urllib.parse.quote(prompt)}"
    client = get_http_client()
    resp = await client.get(full)
    resp.raise_for_status()
    return resp.text.strip()


async def call_zerotwo(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
    """ZeroTwo requires a CSRF token from the homepage."""
    url = os.getenv("ZEROTWO_URL", "https://api.zerotwo.ai/v1/chat/completions")
    async with httpx.AsyncClient() as client:
        r = await client.get("https://zerotwo.ai/")
        csrf = r.cookies.get("__Host-next-auth.csrf-token")
    if not csrf:
        raise Exception("Could not get CSRF token from zerotwo.ai")
    headers = {"Content-Type": "application/json", "X-CSRF-Token": csrf}
    payload = {"model": model or "gpt-5-mini",
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": temp, "stream": False}
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]


# ═══════════════════════════════════════════════════════════════════════════
# §4 — Registry — name → callable
# ═══════════════════════════════════════════════════════════════════════════
def _env_key(*names: str) -> str:
    for n in names:
        v = (os.getenv(n) or "").strip()
        if v:
            return v
    return ""


PROVIDER_HTTP: dict[str, Adapter] = {
    # ── Special: non-OpenAI shapes ────────────────────────────────────────
    "gemini":              call_gemini,
    "cloudflare":          call_cloudflare,
    "huggingface":         call_huggingface,
    "freegpt4_api":        call_freegpt4_api,
    "zerotwo":             call_zerotwo,

    # ── OpenAI-compatible: keyed providers ────────────────────────────────
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
        extra_headers={
            "HTTP-Referer": "https://axelr.in",
            "X-Title": "Axelr AI",
        },
    ),
    "mistral": _openai_compat(
        name="mistral",
        base_url="https://api.mistral.ai/v1/chat/completions",
        key=MISTRAL_API_KEY,
        default_model=MISTRAL_MODELS[0] if MISTRAL_MODELS else None,
    ),
    "modelscope": _openai_compat(
        name="modelscope",
        base_url=os.getenv("MODELSCOPE_URL", "https://api.modelscope.cn/v1/chat/completions"),
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
        base_url=os.getenv("NARA_ROUTER_URL", "https://router.bynara.id/v1/chat/completions"),
        key=NARAROUTER_API_KEY,
        default_model="minimax-m3",
    ),
    "github_models": _openai_compat(
        name="github_models",
        base_url=os.getenv("GITHUB_MODELS_URL",
                           "https://models.inference.ai.azure.com/chat/completions")
                + "?api-version=2024-05-01-preview",
        key=GITHUB_MODELS_TOKEN,
        default_model=GITHUB_MODEL,
    ),
    "ovhcloud": _openai_compat(
        name="ovhcloud",
        base_url=os.getenv("OVHCLOUD_URL", "https://api.ai.cloud.ovh.net/v1/chat/completions"),
        key=OVHCLOUD_API_KEY,
        default_model=OVHCLOUD_MODELS[0] if OVHCLOUD_MODELS else None,
        extra_headers={"X-OVH-Project": os.getenv("OVH_PROJECT_ID", "")},
    ),
    "siliconflow": _openai_compat(
        name="siliconflow",
        base_url="https://api.siliconflow.cn/v1/chat/completions",
        key=SILICONFLOW_API_KEY,
        default_model=SILICONFLOW_MODELS[0] if SILICONFLOW_MODELS else None,
    ),
    "agnes_ai": _openai_compat(
        name="agnes_ai",
        base_url=os.getenv("AGNES_URL", "https://api.agnes.ai/v1/chat/completions"),
        key=AGNES_API_KEY,
        default_model=AGNES_MODEL,
    ),
    "bazaarlink": _openai_compat(
        name="bazaarlink",
        base_url=os.getenv("BAZAARLINK_URL", "https://api.bazaarlink.io/v1/chat/completions"),
        key=BAZAARLINK_API_KEY,
        default_model=BAZAARLINK_MODEL,
    ),
    "requesty": _openai_compat(
        name="requesty",
        base_url=os.getenv("REQUESTY_URL", "https://api.requesty.ai/v1/chat/completions"),
        key=REQUESTY_API_KEY,
        default_model=REQUESTY_MODEL,
    ),
    "nrouter": _openai_compat(
        name="nrouter",
        base_url=os.getenv("NROUTER_URL", "https://api.nrouter.io/v1/chat/completions"),
        key=NROUTER_API_KEY,
        default_model=NROUTER_MODEL,
    ),
    "manifest": _openai_compat(
        name="manifest",
        base_url=os.getenv("MANIFEST_URL", "https://api.manifest.build/v1/chat/completions"),
        key=MANIFEST_API_KEY,
        default_model=MANIFEST_MODEL,
    ),
    "glama": _openai_compat(
        name="glama",
        base_url=os.getenv("GLAMA_URL", "https://api.glama.ai/v1/chat/completions"),
        key=GLAMA_API_KEY,
        default_model=GLAMA_MODEL,
    ),
    "qoder": _openai_compat(
        name="qoder",
        base_url=os.getenv("QODER_URL", "https://api.qoder.com/v1/chat/completions"),
        key=_env_key("QODER_API_KEY"),
        default_model=QODER_MODEL,
    ),
    "zhipuai": _openai_compat(
        name="zhipuai",
        base_url="https://open.bigmodel.cn/api/paas/v4/chat/completions",
        key=ZAI_API_KEY,
        default_model=ZHIPU_MODEL,
    ),
    "teamorouter": _openai_compat(
        name="teamorouter",
        base_url=os.getenv("TEAMOROUTER_URL", "https://api.teamorouter.io/v1/chat/completions"),
        key=TEAMOROUTER_API_KEY,
        default_model=TEAMOROUTER_MODEL,
    ),
    "anyapi": _openai_compat(
        name="anyapi",
        base_url=os.getenv("BASEURL", "https://api.anyapi.ai/v1/chat/completions"),
        key=ANYAPI_API_KEY,
        default_model=ANYAPI_MODEL,
    ),

    # ── OpenAI-compatible: keyless providers ──────────────────────────────
    "bifrost": _openai_compat(
        name="bifrost",
        base_url=os.getenv("BIFROST_URL", "http://localhost:8080/v1/chat/completions"),
        key="",
        default_model="llama3.1:70b",
    ),
    "free_llm_gateway": _openai_compat(
        name="free_llm_gateway",
        base_url=os.getenv("FREE_LLM_GATEWAY_URL",
                           "http://free-llm-gateway:8000/v1/chat/completions"),
        key="",
        default_model="auto",
    ),
    "ninerouter": _openai_compat(
        name="ninerouter",
        base_url=os.getenv("NINEROUTER_URL", "https://api.9router.io/v1/chat/completions"),
        key="",
        default_model="auto",
    ),
    "puter": _openai_compat(
        name="puter",
        base_url=os.getenv("PUTER_URL", "https://api.puter.com/v1/chat/completions"),
        key="",
        default_model=PUTER_MODEL,
    ),
    "freetheai": _openai_compat(
        name="freetheai",
        base_url=os.getenv("FREETHEAI_URL", "https://api.freetheai.com/v1/chat/completions"),
        key="",
        default_model=FREETHEAI_MODEL,
    ),
    "omnigpt_gateway": _openai_compat(
        name="omnigpt_gateway",
        base_url=os.getenv("OMNIGPT_URL", "https://api.omnigpt.io/v1/chat/completions"),
        key="",
        default_model="gpt-3.5-turbo",
    ),
    "opencode_zen": _openai_compat(
        name="opencode_zen",
        base_url=os.getenv("OPENCODE_URL", "https://api.opencode.zen/v1/chat/completions"),
        key="",
        default_model="qwen3-coder",
    ),
    "freeflow": _openai_compat(
        name="freeflow",
        base_url=os.getenv("FREEFLOW_URL", "https://freeflow.llm/v1/chat/completions"),
        key="",
        default_model=FREEFLOW_MODEL,
    ),
    "keylessai": _openai_compat(
        name="keylessai",
        base_url=os.getenv("KEYLESS_URL", "https://api.keyless.ai/v1/chat/completions"),
        key="",
        default_model="gpt-3.5-turbo",
    ),
    "chubvenus": _openai_compat(
        name="chubvenus",
        base_url=os.getenv("CHUBVENUS_URL", "https://api.chub.ai/v1/chat/completions"),
        key="",
        default_model="gpt-3.5-turbo",
    ),
    "blockrun": _openai_compat(
        name="blockrun",
        base_url=os.getenv("BLOCKRUN_URL", "https://api.blockrun.com/v1/chat/completions"),
        key="",
        default_model="deepseek-v4-flash",
    ),
    "aymo": _openai_compat(
        name="aymo",
        base_url=os.getenv("AYMO_URL", "https://api.aymo.ai/v1/chat/completions"),
        key="",
        default_model="gemini-flash",
    ),
    "aihubmix": _openai_compat(
        name="aihubmix",
        base_url=os.getenv("AIHUBMIX_URL", "https://api.inferera.com/chat/completions"),
        key="",
        default_model="gpt-5.5",
    ),
    "aisure": _openai_compat(
        name="aisure",
        base_url=os.getenv("AISURE_URL", "https://api.aisure.ai/v1/chat/completions"),
        key="",
        default_model="gpt-4o",
    ),
    "proxygatellm": _openai_compat(
        name="proxygatellm",
        base_url="https://api.proxygatellm.com/v1/chat/completions",
        key="",
        default_model="gpt-3.5-turbo",
    ),
}


# ── Local fallback ────────────────────────────────────────────────────────
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


PROVIDER_HTTP["local"] = call_local_fallback


__all__ = [
    "Adapter", "PROVIDER_HTTP", "http_post_async",
    "call_gemini", "call_gemini_vision", "call_cloudflare", "call_huggingface",
    "call_freegpt4_api", "call_zerotwo", "call_local_fallback",
    "build_local_fallback_response",
]