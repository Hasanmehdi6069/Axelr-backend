# core/streaming_providers.py
"""
AXELR — Real Token Streaming Adapters
======================================
Per-provider SSE streaming. Yields tokens as the upstream model emits
them. No buffering. No re-chunking. This is what makes Axelr feel like
ChatGPT instead of a batched HTTP request.

Adapters:
    * OpenAI-compatible SSE  — Groq, OpenRouter, Mistral, DeepSeek,
      Together, Cerebras, Fireworks, SambaNova, Perplexity, Anyscale,
      Hyperbolic, Novita, Lepton, Cloudflare Workers AI (OpenAI mode).
    * Google Gemini SSE      — v1beta streamGenerateContent.
    * Anthropic SSE          — /v1/messages with stream=true.
    * Cohere SSE             — /v2/chat stream.

Public API:
    stream_completion(provider, prompt, *, max_tokens, temp, model, timeout)
        -> AsyncIterator[str]           # yields raw text deltas
    PROVIDER_STREAMERS: dict[str, StreamerFunc]

Every adapter:
    * Uses the shared httpx.AsyncClient (connection pooling).
    * Yields nothing on error — the caller decides fallback.
    * Cancellation-safe: an async-for break closes the HTTP stream.
    * Bounded: at most `max_inflight_bytes` buffered internally.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

logger = logging.getLogger("axelr.streaming_providers")

# Provider endpoints — env-overridable.
_GROQ_URL       = os.getenv("GROQ_URL", "https://api.groq.com/openai/v1/chat/completions")
_OPENROUTER_URL = os.getenv("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")
_MISTRAL_URL    = os.getenv("MISTRAL_URL", "https://api.mistral.ai/v1/chat/completions")
_DEEPSEEK_URL   = os.getenv("DEEPSEEK_URL", "https://api.deepseek.com/v1/chat/completions")
_TOGETHER_URL   = os.getenv("TOGETHER_URL", "https://api.together.xyz/v1/chat/completions")
_CEREBRAS_URL   = os.getenv("CEREBRAS_URL", "https://api.cerebras.ai/v1/chat/completions")
_HYPERBOLIC_URL = os.getenv("HYPERBOLIC_URL", "https://api.hyperbolic.xyz/v1/chat/completions")
_SAMBANOVA_URL  = os.getenv("SAMBANOVA_URL", "https://api.sambanova.ai/v1/chat/completions")
_NOVITA_URL     = os.getenv("NOVITA_URL", "https://api.novita.ai/v3/openai/chat/completions")
_FIREWORKS_URL  = os.getenv("FIREWORKS_URL", "https://api.fireworks.ai/inference/v1/chat/completions")
_GEMINI_URL     = os.getenv("GEMINI_URL", "https://generativelanguage.googleapis.com/v1beta")
_CF_AI_URL      = os.getenv("CF_AI_URL", "https://api.cloudflare.com/client/v4/accounts/{acct}/ai/v1/chat/completions")
_ANTHROPIC_URL  = os.getenv("ANTHROPIC_URL", "https://api.anthropic.com/v1/messages")
_COHERE_URL     = os.getenv("COHERE_URL", "https://api.cohere.com/v2/chat")

_MAX_LINE_BYTES = 64 * 1024          # refuse to buffer a runaway SSE line


StreamerFunc = Callable[..., AsyncIterator[str]]


def _client() -> httpx.AsyncClient:
    """Reuse the config-level shared client if available; else make one."""
    try:
        from api.config import get_http_client            # type: ignore
        return get_http_client()
    except Exception:
        # Fallback for standalone use — keeps the module importable.
        global _FALLBACK_CLIENT
        if _FALLBACK_CLIENT is None or _FALLBACK_CLIENT.is_closed:
            _FALLBACK_CLIENT = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=8.0),
                limits=httpx.Limits(max_connections=32,
                                    max_keepalive_connections=16),
                follow_redirects=False,
            )
        return _FALLBACK_CLIENT


_FALLBACK_CLIENT: httpx.AsyncClient | None = None


async def _sse_lines(resp: httpx.Response) -> AsyncIterator[str]:
    """
    Iterate an SSE response line-by-line with a hard cap on line length.
    Yields decoded `data:` payloads, filters `[DONE]` and comments.
    """
    buf = bytearray()
    async for chunk in resp.aiter_bytes(4096):
        buf.extend(chunk)
        if len(buf) > _MAX_LINE_BYTES * 4:
            # Reset — the upstream is emitting garbage.
            buf.clear()
            continue
        while True:
            nl = buf.find(b"\n")
            if nl == -1:
                break
            line = bytes(buf[:nl]).rstrip(b"\r")
            del buf[: nl + 1]
            if not line or line.startswith(b":"):
                continue
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].lstrip()
            if payload == b"[DONE]":
                return
            yield payload.decode("utf-8", errors="ignore")


async def _stream_openai_compat(
    *,
    url: str,
    api_key: str,
    model: str,
    prompt: str,
    max_tokens: int,
    temp: float,
    timeout_s: float = 60.0,
    extra_headers: dict[str, str] | None = None,
    extra_body: dict[str, Any] | None = None,
) -> AsyncIterator[str]:
    """Generic OpenAI-compatible SSE streamer. Covers ~12 providers."""
    if not api_key or not model:
        return
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        **(extra_headers or {}),
    }
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": True,
        **(extra_body or {}),
    }
    client = _client()
    try:
        req = client.build_request("POST", url, headers=headers, json=body)
        resp = await client.send(req, stream=True, timeout=timeout_s)
        try:
            if resp.status_code != 200:
                logger.debug("openai_compat_non200 provider=%s status=%s",
                             url, resp.status_code)
                return
            async for raw in _sse_lines(resp):
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                # OpenAI shape
                choices = obj.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        yield piece
                    if choices[0].get("finish_reason"):
                        return
                    continue
                # Some providers wrap differently
                text = obj.get("text") or obj.get("content")
                if isinstance(text, str) and text:
                    yield text
        finally:
            with contextlib.suppress(Exception):
                await resp.aclose()
    except asyncio.CancelledError:
        raise
    except Exception as e:                                          # noqa: BLE001
        logger.debug("openai_compat_stream_error url=%s error=%s", url, e)
        return


async def _stream_gemini(
    *, api_key: str, model: str, prompt: str,
    max_tokens: int, temp: float, timeout_s: float = 60.0,
) -> AsyncIterator[str]:
    """Gemini v1beta streamGenerateContent SSE."""
    if not api_key or not model:
        return
    url = f"{_GEMINI_URL}/models/{model}:streamGenerateContent?alt=sse"
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": temp,
        },
    }
    client = _client()
    try:
        req = client.build_request("POST", url, headers=headers, json=body)
        resp = await client.send(req, stream=True, timeout=timeout_s)
        try:
            if resp.status_code != 200:
                return
            async for raw in _sse_lines(resp):
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                for cand in obj.get("candidates") or []:
                    for part in (cand.get("content") or {}).get("parts") or []:
                        txt = part.get("text")
                        if txt:
                            yield txt
                    if cand.get("finishReason"):
                        return
        finally:
            with contextlib.suppress(Exception):
                await resp.aclose()
    except asyncio.CancelledError:
        raise
    except Exception as e:                                          # noqa: BLE001
        logger.debug("gemini_stream_error error=%s", e)
        return


async def _stream_anthropic(
    *, api_key: str, model: str, prompt: str,
    max_tokens: int, temp: float, timeout_s: float = 60.0,
) -> AsyncIterator[str]:
    """Anthropic /v1/messages SSE."""
    if not api_key or not model:
        return
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": True,
        "messages": [{"role": "user", "content": prompt}],
    }
    client = _client()
    try:
        req = client.build_request("POST", _ANTHROPIC_URL, headers=headers, json=body)
        resp = await client.send(req, stream=True, timeout=timeout_s)
        try:
            if resp.status_code != 200:
                return
            async for raw in _sse_lines(resp):
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                t = obj.get("type")
                if t == "content_block_delta":
                    d = (obj.get("delta") or {}).get("text")
                    if d:
                        yield d
                elif t == "message_stop":
                    return
        finally:
            with contextlib.suppress(Exception):
                await resp.aclose()
    except asyncio.CancelledError:
        raise
    except Exception as e:                                          # noqa: BLE001
        logger.debug("anthropic_stream_error error=%s", e)
        return


async def _stream_cohere(
    *, api_key: str, model: str, prompt: str,
    max_tokens: int, temp: float, timeout_s: float = 60.0,
) -> AsyncIterator[str]:
    """Cohere v2 chat SSE."""
    if not api_key or not model:
        return
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": True,
    }
    client = _client()
    try:
        req = client.build_request("POST", _COHERE_URL, headers=headers, json=body)
        resp = await client.send(req, stream=True, timeout=timeout_s)
        try:
            if resp.status_code != 200:
                return
            async for raw in _sse_lines(resp):
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                t = obj.get("type")
                if t == "content-delta":
                    piece = (((obj.get("delta") or {}).get("message") or {})
                             .get("content") or {}).get("text")
                    if piece:
                        yield piece
                elif t == "message-end":
                    return
        finally:
            with contextlib.suppress(Exception):
                await resp.aclose()
    except asyncio.CancelledError:
        raise
    except Exception as e:                                          # noqa: BLE001
        logger.debug("cohere_stream_error error=%s", e)
        return


# ── Cloudflare Workers AI streaming ──────────────────────────────────────
def _cf_streamer(*, account_id: str) -> StreamerFunc:
    async def _run(*, api_key: str, model: str, prompt: str,
                   max_tokens: int, temp: float,
                   timeout_s: float = 60.0) -> AsyncIterator[str]:
        url = _CF_AI_URL.format(acct=account_id)
        async for tok in _stream_openai_compat(
            url=url, api_key=api_key, model=model, prompt=prompt,
            max_tokens=max_tokens, temp=temp, timeout_s=timeout_s,
        ):
            yield tok
    return _run


# ── Registry ─────────────────────────────────────────────────────────────
def _env_key(*names: str) -> str:
    for n in names:
        v = (os.getenv(n) or "").strip()
        if v:
            return v
    return ""


async def _groq(**kw: Any) -> AsyncIterator[str]:
    api_key = _env_key("GROQ_API_KEY")
    async for t in _stream_openai_compat(
        url=_GROQ_URL, api_key=api_key, **kw,
    ):
        yield t


async def _openrouter(**kw: Any) -> AsyncIterator[str]:
    api_key = _env_key("OPENROUTER_API_KEY")
    async for t in _stream_openai_compat(
        url=_OPENROUTER_URL, api_key=api_key,
        extra_headers={
            "HTTP-Referer": os.getenv("OPENROUTER_REFERER", "https://axelr.ai"),
            "X-Title": os.getenv("OPENROUTER_TITLE", "Axelr AI"),
        },
        **kw,
    ):
        yield t


async def _mistral(**kw: Any) -> AsyncIterator[str]:
    async for t in _stream_openai_compat(
        url=_MISTRAL_URL, api_key=_env_key("MISTRAL_API_KEY"), **kw,
    ):
        yield t


async def _deepseek(**kw: Any) -> AsyncIterator[str]:
    async for t in _stream_openai_compat(
        url=_DEEPSEEK_URL, api_key=_env_key("DEEPSEEK_API_KEY"), **kw,
    ):
        yield t


async def _together(**kw: Any) -> AsyncIterator[str]:
    async for t in _stream_openai_compat(
        url=_TOGETHER_URL, api_key=_env_key("TOGETHER_API_KEY"), **kw,
    ):
        yield t


async def _cerebras(**kw: Any) -> AsyncIterator[str]:
    async for t in _stream_openai_compat(
        url=_CEREBRAS_URL, api_key=_env_key("CEREBRAS_API_KEY"), **kw,
    ):
        yield t


async def _hyperbolic(**kw: Any) -> AsyncIterator[str]:
    async for t in _stream_openai_compat(
        url=_HYPERBOLIC_URL, api_key=_env_key("HYPERBOLIC_API_KEY"), **kw,
    ):
        yield t


async def _sambanova(**kw: Any) -> AsyncIterator[str]:
    async for t in _stream_openai_compat(
        url=_SAMBANOVA_URL, api_key=_env_key("SAMBANOVA_API_KEY"), **kw,
    ):
        yield t


async def _novita(**kw: Any) -> AsyncIterator[str]:
    async for t in _stream_openai_compat(
        url=_NOVITA_URL, api_key=_env_key("NOVITA_API_KEY"), **kw,
    ):
        yield t


async def _fireworks(**kw: Any) -> AsyncIterator[str]:
    async for t in _stream_openai_compat(
        url=_FIREWORKS_URL, api_key=_env_key("FIREWORKS_API_KEY"), **kw,
    ):
        yield t


async def _gemini(**kw: Any) -> AsyncIterator[str]:
    async for t in _stream_gemini(
        api_key=_env_key("GEMINI_API_KEY", "GOOGLE_API_KEY"), **kw,
    ):
        yield t


async def _anthropic(**kw: Any) -> AsyncIterator[str]:
    async for t in _stream_anthropic(
        api_key=_env_key("ANTHROPIC_API_KEY"), **kw,
    ):
        yield t


async def _cohere(**kw: Any) -> AsyncIterator[str]:
    async for t in _stream_cohere(
        api_key=_env_key("COHERE_API_KEY"), **kw,
    ):
        yield t


async def _cloudflare(**kw: Any) -> AsyncIterator[str]:
    acct = _env_key("CLOUDFLARE_ACCOUNT_ID")
    key = _env_key("CLOUDFLARE_API_KEY")
    if not acct or not key:
        return
    async for t in _stream_openai_compat(
        url=_CF_AI_URL.format(acct=acct),
        api_key=key,
        extra_body={"account_id": acct},
        **kw,
    ):
        yield t


PROVIDER_STREAMERS: dict[str, StreamerFunc] = {
    "groq":        _groq,
    "openrouter":  _openrouter,
    "mistral":     _mistral,
    "deepseek":    _deepseek,
    "together":    _together,
    "cerebras":    _cerebras,
    "hyperbolic":  _hyperbolic,
    "sambanova":   _sambanova,
    "novita":      _novita,
    "fireworks":   _fireworks,
    "gemini":      _gemini,
    "anthropic":   _anthropic,
    "cohere":      _cohere,
    "cloudflare":  _cloudflare,
}


async def stream_completion(
    provider: str,
    prompt: str,
    *,
    model: str,
    max_tokens: int = 2048,
    temp: float = 0.4,
    timeout_s: float = 60.0,
) -> AsyncIterator[str]:
    """
    Public entry. Yields text deltas. On any failure yields nothing and
    returns — caller decides fallback.
    """
    fn = PROVIDER_STREAMERS.get(provider)
    if fn is None:
        return
    async for tok in fn(
        model=model, prompt=prompt, max_tokens=max_tokens,
        temp=temp, timeout_s=timeout_s,
    ):
        if tok:
            yield tok


__all__ = ["PROVIDER_STREAMERS", "stream_completion", "StreamerFunc"]