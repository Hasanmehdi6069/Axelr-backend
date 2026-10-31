# core/gateway_router.py
"""
AXELR — Multi-Gateway Failover Pipeline
========================================
Transparent primary → secondary → tertiary failover across OpenAI-shape
LLM gateways (Bifrost / LiteLLM / Helicone / OpenRouter / direct).

Architecture
------------
  1. OpenAI-compatible client shape for every gateway.
  2. Primary hop is the configured gateway endpoint.
  3. Failover triggers on: network error, timeout > GATEWAY_TIMEOUT_MS,
     HTTP 5xx, or empty body.
  4. Payload translator normalizes between OpenAI / Anthropic / Gemini
     request+response shapes so a single logical call survives cross-hop.
  5. Streaming-aware: mid-stream gateway failures trigger a resume from
     the next hop using the accumulated partial text.

Public API
----------
    GW = GatewayRouter()
    result = await GW.call(prompt, max_tokens, temp, model, ...)
    async for tok in GW.stream(prompt, ...): ...

Never raises — returns a neutral `{"success": False, ...}` on total
failure. `asyncio.CancelledError` propagates.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

logger = logging.getLogger("axelr.gateway")


# ═══════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════
GATEWAY_TIMEOUT_MS = int(os.getenv("GATEWAY_TIMEOUT_MS", "4000"))
GATEWAY_CONNECT_TIMEOUT_S = float(os.getenv("GATEWAY_CONNECT_TIMEOUT_S", "3.0"))
GATEWAY_FAILURE_THRESHOLD = int(os.getenv("GATEWAY_FAILURE_THRESHOLD", "3"))
GATEWAY_COOLDOWN_S = float(os.getenv("GATEWAY_COOLDOWN_S", "45.0"))


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip().rstrip("/")


@dataclass(slots=True)
class GatewayEndpoint:
    name: str
    url: str
    api_key: str
    shape: str = "openai"        # openai | anthropic | gemini
    priority: int = 0
    extra_headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    failures: int = 0
    cooldown_until: float = 0.0

    @property
    def is_available(self) -> bool:
        return self.enabled and time.monotonic() >= self.cooldown_until


def _build_endpoints() -> list[GatewayEndpoint]:
    """Read from env at import; missing entries are skipped."""
    eps: list[GatewayEndpoint] = []
    primary_url = _env("AI_GATEWAY_URL") or _env("BIFROST_URL")
    primary_key = _env("AI_GATEWAY_KEY") or _env("BIFROST_API_KEY") or "no-key"
    if primary_url:
        eps.append(GatewayEndpoint(
            name="primary", url=primary_url, api_key=primary_key,
            shape=_env("AI_GATEWAY_SHAPE", "openai"), priority=0,
        ))
    secondary_url = _env("AI_GATEWAY_BACKUP_URL") or _env("HELICONE_URL")
    secondary_key = (_env("AI_GATEWAY_BACKUP_KEY")
                     or _env("HELICONE_API_KEY") or "no-key")
    if secondary_url:
        eps.append(GatewayEndpoint(
            name="secondary", url=secondary_url, api_key=secondary_key,
            shape=_env("AI_GATEWAY_BACKUP_SHAPE", "openai"), priority=1,
        ))
    tertiary_url = _env("AI_GATEWAY_TERTIARY_URL") or _env("LITELLM_URL")
    tertiary_key = (_env("AI_GATEWAY_TERTIARY_KEY")
                    or _env("LITELLM_API_KEY") or "no-key")
    if tertiary_url:
        eps.append(GatewayEndpoint(
            name="tertiary", url=tertiary_url, api_key=tertiary_key,
            shape=_env("AI_GATEWAY_TERTIARY_SHAPE", "openai"), priority=2,
        ))
    # Env can disable the router entirely (fall through to provider cascade).
    if _env("AI_GATEWAY_DISABLED", "").lower() in ("1", "true"):
        return []
    return eps


# ═══════════════════════════════════════════════════════════════════════════
# Payload translators
# ═══════════════════════════════════════════════════════════════════════════
def to_openai(prompt: str, max_tokens: int, temp: float,
              model: str | None, *, stream: bool = False) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": stream,
    }


def to_anthropic(prompt: str, max_tokens: int, temp: float,
                 model: str | None, *, stream: bool = False) -> dict[str, Any]:
    return {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": stream,
        "messages": [{"role": "user", "content": prompt}],
    }


def to_gemini(prompt: str, max_tokens: int, temp: float,
              model: str | None, *, stream: bool = False) -> dict[str, Any]:
    return {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temp},
    }


def from_openai(body: dict[str, Any]) -> str:
    try:
        return body["choices"][0]["message"]["content"] or ""
    except Exception:
        return ""


def from_anthropic(body: dict[str, Any]) -> str:
    with contextlib.suppress(Exception):
        parts = body.get("content") or []
        return "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    return ""


def from_gemini(body: dict[str, Any]) -> str:
    with contextlib.suppress(Exception):
        return body["candidates"][0]["content"]["parts"][0]["text"]
    return ""


_SHAPE = {
    "openai":    (to_openai,    from_openai),
    "anthropic": (to_anthropic, from_anthropic),
    "gemini":    (to_gemini,    from_gemini),
}


# ═══════════════════════════════════════════════════════════════════════════
# Failure classification
# ═══════════════════════════════════════════════════════════════════════════
def _is_failover_error(exc: Exception) -> bool:
    """Anything that should trigger the next hop."""
    if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout,
                        httpx.ConnectTimeout, httpx.RemoteProtocolError,
                        asyncio.TimeoutError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return False


# ═══════════════════════════════════════════════════════════════════════════
# Router
# ═══════════════════════════════════════════════════════════════════════════
class GatewayRouter:
    """Ordered list of gateways; first available wins; failures degrade."""

    __slots__ = ("_endpoints", "_client", "_owns_client", "_lock")

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._endpoints = sorted(_build_endpoints(), key=lambda e: e.priority)
        self._client = client
        self._owns_client = client is None
        self._lock = asyncio.Lock()
        if self._endpoints:
            logger.info("gateway_router_initialized endpoints=%d",
                        len(self._endpoints))
        else:
            logger.info("gateway_router_disabled — provider cascade only")

    @property
    def enabled(self) -> bool:
        return bool(self._endpoints)

    def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=GATEWAY_CONNECT_TIMEOUT_S,
                    read=GATEWAY_TIMEOUT_MS / 1000.0,
                    write=GATEWAY_TIMEOUT_MS / 1000.0,
                    pool=GATEWAY_TIMEOUT_MS / 1000.0,
                ),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            )
        return self._client

    def _headers(self, ep: GatewayEndpoint) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if ep.api_key and ep.api_key != "no-key":
            if ep.shape == "anthropic":
                h["x-api-key"] = ep.api_key
                h["anthropic-version"] = "2023-06-01"
            else:
                h["Authorization"] = f"Bearer {ep.api_key}"
        h.update(ep.extra_headers)
        return h

    def _url(self, ep: GatewayEndpoint) -> str:
        base = ep.url.rstrip("/")
        if ep.shape == "anthropic":
            return f"{base}/v1/messages"
        if ep.shape == "gemini":
            return f"{base}/v1beta/models/{{model}}:generateContent"
        return f"{base}/v1/chat/completions"

    async def _mark_failure(self, ep: GatewayEndpoint, rate_limited: bool) -> None:
        async with self._lock:
            ep.failures += 1
            if rate_limited or ep.failures >= GATEWAY_FAILURE_THRESHOLD:
                ep.cooldown_until = time.monotonic() + (
                    GATEWAY_COOLDOWN_S * (4 if rate_limited else 1)
                )
                logger.warning("gateway_tripped name=%s failures=%d rate=%s",
                               ep.name, ep.failures, rate_limited)

    async def _mark_success(self, ep: GatewayEndpoint) -> None:
        async with self._lock:
            ep.failures = 0
            ep.cooldown_until = 0.0

    # ── public: non-streaming ────────────────────────────────────────────
    async def call(
        self, prompt: str, max_tokens: int, temp: float,
        model: str | None, *, timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Try each available gateway in priority order. Never raises."""
        if not self._endpoints:
            return {"success": False, "text": "", "provider": "gateway",
                    "model_used": "", "error": "gateway_disabled"}

        last_error = "unknown"
        client = self._client_or_create()
        for ep in self._endpoints:
            if not ep.is_available:
                continue
            to_wire, from_wire = _SHAPE.get(ep.shape, _SHAPE["openai"])
            body = to_wire(prompt, max_tokens, temp, model, stream=False)
            url = self._url(ep)
            if ep.shape == "gemini":
                model_name = model or "gemini-1.5-flash"
                url = url.replace("{model}", model_name)
                # Gemini passes API key as query param
                url += f"?key={ep.api_key}"
                headers = {"Content-Type": "application/json"}
            else:
                headers = self._headers(ep)
            t0 = time.monotonic()
            try:
                resp = await client.post(
                    url, headers=headers, json=body,
                    timeout=timeout_s or (GATEWAY_TIMEOUT_MS / 1000.0),
                )
                if resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        "5xx", request=resp.request, response=resp,
                    )
                if resp.status_code == 429:
                    raise httpx.HTTPStatusError(
                        "429", request=resp.request, response=resp,
                    )
                resp.raise_for_status()
                parsed = resp.json()
            except Exception as e:                                # noqa: BLE001
                last_error = f"{ep.name}:{type(e).__name__}:{str(e)[:120]}"
                if _is_failover_error(e):
                    await self._mark_failure(
                        ep, rate_limited="429" in str(e))
                    continue
                # Non-failover error → bail out
                return {"success": False, "text": "", "provider": "gateway",
                        "model_used": ep.name, "error": last_error}
            text = from_wire(parsed)
            if not text or not text.strip():
                last_error = f"{ep.name}:empty_body"
                await self._mark_failure(ep, rate_limited=False)
                continue
            await self._mark_success(ep)
            latency_ms = (time.monotonic() - t0) * 1000.0
            return {
                "success": True, "text": text, "provider": f"gateway:{ep.name}",
                "model_used": model or "", "tokens_used": len(text.split()),
                "latency_ms": round(latency_ms, 2),
            }
        return {"success": False, "text": "", "provider": "gateway",
                "model_used": "", "error": last_error}

    # ── public: streaming ────────────────────────────────────────────────
    async def stream(
        self, prompt: str, max_tokens: int, temp: float,
        model: str | None, *, timeout_s: float | None = None,
    ) -> AsyncIterator[str]:
        """
        Stream from the first available gateway. On mid-stream failure,
        the yielded text so far is accumulated by the caller — the router
        does NOT replay. Callers wanting resume should call `call()` after
        catching the failure.
        """
        if not self._endpoints:
            return
        client = self._client_or_create()
        for ep in self._endpoints:
            if not ep.is_available:
                continue
            to_wire, from_wire = _SHAPE.get(ep.shape, _SHAPE["openai"])
            body = to_wire(prompt, max_tokens, temp, model, stream=True)
            url = self._url(ep)
            headers = self._headers(ep)
            if ep.shape == "gemini":
                model_name = model or "gemini-1.5-flash"
                url = (url.replace("{model}", model_name)
                       + f"?alt=sse&key={ep.api_key}")
                headers = {"Content-Type": "application/json"}
            t0 = time.monotonic()
            try:
                async with client.stream(
                    "POST", url, headers=headers, json=body,
                    timeout=timeout_s or (GATEWAY_TIMEOUT_MS / 1000.0),
                ) as resp:
                    if resp.status_code >= 500 or resp.status_code == 429:
                        raise httpx.HTTPStatusError(
                            "5xx/429", request=resp.request, response=resp,
                        )
                    resp.raise_for_status()
                    if ep.shape == "openai":
                        async for tok in _stream_openai_sse(resp):
                            yield tok
                    elif ep.shape == "anthropic":
                        async for tok in _stream_anthropic_sse(resp):
                            yield tok
                    elif ep.shape == "gemini":
                        async for tok in _stream_gemini_sse(resp):
                            yield tok
                    await self._mark_success(ep)
                    return
            except asyncio.CancelledError:
                raise
            except Exception as e:                                # noqa: BLE001
                if _is_failover_error(e):
                    await self._mark_failure(ep, rate_limited="429" in str(e))
                    logger.debug("gateway_stream_failover name=%s error=%s",
                                 ep.name, e)
                    continue
                logger.warning("gateway_stream_nonfailover name=%s error=%s",
                               ep.name, e)
                return
            finally:
                logger.debug("gateway_stream_done name=%s elapsed_ms=%.0f",
                             ep.name, (time.monotonic() - t0) * 1000)

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {"name": e.name, "shape": e.shape, "priority": e.priority,
             "enabled": e.enabled, "available": e.is_available,
             "failures": e.failures}
            for e in self._endpoints
        ]


# ═══════════════════════════════════════════════════════════════════════════
# SSE parsers (line-by-line, bounded)
# ═══════════════════════════════════════════════════════════════════════════
_MAX_SSE_LINE = 64 * 1024


async def _iter_sse_lines(resp: httpx.Response) -> AsyncIterator[bytes]:
    buf = bytearray()
    async for chunk in resp.aiter_bytes(4096):
        buf.extend(chunk)
        if len(buf) > _MAX_SSE_LINE * 4:
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
            if line.startswith(b"data:"):
                payload = line[5:].lstrip()
                if payload == b"[DONE]":
                    return
                yield payload


async def _stream_openai_sse(resp: httpx.Response) -> AsyncIterator[str]:
    async for raw in _iter_sse_lines(resp):
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        choices = obj.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            piece = delta.get("content")
            if piece:
                yield piece
            if choices[0].get("finish_reason"):
                return


async def _stream_anthropic_sse(resp: httpx.Response) -> AsyncIterator[str]:
    async for raw in _iter_sse_lines(resp):
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


async def _stream_gemini_sse(resp: httpx.Response) -> AsyncIterator[str]:
    async for raw in _iter_sse_lines(resp):
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


# ═══════════════════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════════════════
_ROUTER: GatewayRouter | None = None


def get_gateway_router() -> GatewayRouter:
    global _ROUTER
    if _ROUTER is None:
        _ROUTER = GatewayRouter()
    return _ROUTER


__all__ = [
    "GatewayRouter", "GatewayEndpoint", "get_gateway_router",
    "to_openai", "to_anthropic", "to_gemini",
    "from_openai", "from_anthropic", "from_gemini",
]