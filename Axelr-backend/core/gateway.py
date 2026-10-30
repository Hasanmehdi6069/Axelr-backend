# core/gateway.py
"""
AXELR — Unified Gateway Layer (v4.0)
=====================================
Consolidated replacement for ``core/gateway.py`` and ``core/gateway/``.
Single-file surface: translators, router, and OpenAI-compatible client.

Fixes over the pre-consolidation files:

    * ``NoProviderAvailable`` was referenced but never imported in the
      legacy single-file gateway; now imported from ``core.utils.errors_and_retry``.
    * The legacy ``AsyncAxelrClient._create`` called
      ``GatewayRouter.route()`` / ``route_stream()`` — neither method
      exists. The client now uses ``GatewayRouter.call()`` /
      ``GatewayRouter.stream()``, which is the actual contract.
    * ``GatewayRouter.stream()`` previously yielded the *entire* response
      once. It now streams real per-token deltas via
      ``utils.security_and_streaming.stream_for_shape``.
    * Empty conversation turns previously raised ``IndexError``; now
      raises a typed ``NoProviderAvailable`` before any network call.

Contract:
    * ``get_gateway_router()`` is the lazy process-wide singleton. Safe to
      call concurrently; the first call constructs and later calls return
      the same instance (the ``client`` kwarg is used on first call only).
    * ``AsyncAxelrClient`` is a drop-in for ``openai.AsyncOpenAI``.
    * ``audit()`` returns a JSON-serialisable snapshot.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from .utils.errors_and_retry import NoProviderAvailable
from .utils.security_and_streaming import stream_for_shape

logger = logging.getLogger("axelr.gateway")
__version__ = "4.0.0"

# ── Config ────────────────────────────────────────────────────────────
GATEWAY_TIMEOUT_MS = int(os.getenv("GATEWAY_TIMEOUT_MS", "4000"))
GATEWAY_CONNECT_TIMEOUT_S = float(os.getenv("GATEWAY_CONNECT_TIMEOUT_S", "3.0"))
GATEWAY_FAILURE_THRESHOLD = int(os.getenv("GATEWAY_FAILURE_THRESHOLD", "3"))
GATEWAY_COOLDOWN_S = float(os.getenv("GATEWAY_COOLDOWN_S", "45.0"))
GATEWAY_MAX_COOLDOWN_S = float(os.getenv("GATEWAY_MAX_COOLDOWN_S", "600.0"))


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip().rstrip("/")


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Payload translation
# ═══════════════════════════════════════════════════════════════════════════
def _split_messages(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, str]]]:
    """Split OpenAI-style messages into ``(system_text, non_system_turns)``.

    Contract: never raises. Missing or malformed entries are skipped.
    """
    system_parts: list[str] = []
    turns: list[dict[str, str]] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role", "user"))
        content = m.get("content")
        if isinstance(content, list):
            content = "\n".join(
                p.get("text", "") for p in content if isinstance(p, dict))
        content = str(content or "")
        if role == "system":
            system_parts.append(content)
        else:
            turns.append({"role": role, "content": content})
    return "\n\n".join(system_parts).strip(), turns


def to_openai(messages, *, model, max_tokens, temp, system="", stream=False):
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    out.extend(messages or [])
    return {"model": model, "messages": out, "max_tokens": int(max_tokens),
            "temperature": float(temp), "stream": bool(stream)}


def to_anthropic(messages, *, model, max_tokens, temp, system="", stream=False):
    sys_extra, turns = _split_messages(messages)
    merged = "\n\n".join(filter(None, [system, sys_extra]))
    cleaned: list[dict[str, str]] = []
    for t in turns:
        role = t.get("role", "user")
        if role not in ("user", "assistant"):
            role = "user"
        cleaned.append({"role": role, "content": t.get("content", "")})
    if not cleaned:
        cleaned = [{"role": "user", "content": ""}]
    payload: dict[str, Any] = {
        "model": model, "messages": cleaned,
        "max_tokens": int(max_tokens), "temperature": float(temp),
        "stream": bool(stream),
    }
    if merged:
        payload["system"] = merged
    return payload


def to_gemini(messages, *, model, max_tokens, temp, system="", stream=False):
    sys_extra, turns = _split_messages(messages)
    merged = "\n\n".join(filter(None, [system, sys_extra]))
    contents: list[dict[str, Any]] = []
    for t in turns:
        role = t.get("role", "user")
        if role == "assistant":
            role = "model"
        contents.append({"role": role, "parts": [{"text": t.get("content", "")}]})
    if not contents:
        contents = [{"role": "user", "parts": [{"text": ""}]}]
    payload: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {
            "maxOutputTokens": int(max_tokens),
            "temperature": float(temp),
        },
    }
    if merged:
        payload["systemInstruction"] = {"parts": [{"text": merged}]}
    return payload


def from_openai_response(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    choices = body.get("choices") or []
    if not choices:
        return ""
    msg = (choices[0] or {}).get("message") or {}
    text = msg.get("content")
    if isinstance(text, list):
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    return str(text or "")


def from_anthropic_response(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    return "".join(
        str(b.get("text", ""))
        for b in (body.get("content") or [])
        if isinstance(b, dict) and b.get("type") == "text"
    )


def from_gemini_response(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    candidates = body.get("candidates") or []
    if not candidates:
        return ""
    parts = ((candidates[0] or {}).get("content") or {}).get("parts") or []
    return "".join(
        str(p.get("text", "")) for p in parts if isinstance(p, dict))


_ANTHROPIC_HINTS = ("anthropic", "/v1/messages", "claude")
_GEMINI_HINTS = ("gemini", "generativelanguage", "googleapis")


def detect_shape(url_or_provider: str) -> str:
    low = (url_or_provider or "").lower()
    if any(h in low for h in _ANTHROPIC_HINTS):
        return "anthropic"
    if any(h in low for h in _GEMINI_HINTS):
        return "gemini"
    return "openai"


def build_payload(shape, messages, *, model, max_tokens, temp,
                  system="", stream=False):
    if shape == "anthropic":
        return to_anthropic(messages, model=model, max_tokens=max_tokens,
                            temp=temp, system=system, stream=stream)
    if shape == "gemini":
        return to_gemini(messages, model=model, max_tokens=max_tokens,
                         temp=temp, system=system, stream=stream)
    return to_openai(messages, model=model, max_tokens=max_tokens,
                     temp=temp, system=system, stream=stream)


def parse_response(shape, body):
    if shape == "anthropic":
        return from_anthropic_response(body)
    if shape == "gemini":
        return from_gemini_response(body)
    return from_openai_response(body)


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Gateway router
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class GatewayEndpoint:
    name: str
    url: str
    api_key: str
    shape: str = "openai"
    priority: int = 0
    extra_headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    failures: int = 0
    cooldown_until: float = 0.0
    ewma_latency_ms: float = 500.0
    last_success_ts: float = 0.0
    total_calls: int = 0
    total_failures: int = 0

    @property
    def is_available(self) -> bool:
        return self.enabled and time.monotonic() >= self.cooldown_until

    def effective_rank(self) -> float:
        return (self.priority * 10_000.0
                + self.failures * 250.0
                + self.ewma_latency_ms * 0.5)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "url": self.url, "shape": self.shape,
            "priority": self.priority, "enabled": self.enabled,
            "is_available": self.is_available, "failures": self.failures,
            "cooldown_until": self.cooldown_until,
            "ewma_latency_ms": self.ewma_latency_ms,
            "last_success_ts": self.last_success_ts,
            "total_calls": self.total_calls,
            "total_failures": self.total_failures,
        }


def _build_endpoints() -> list[GatewayEndpoint]:
    if _env("AI_GATEWAY_DISABLED", "").lower() in ("1", "true"):
        return []
    tiers = [
        ("primary", "AI_GATEWAY_URL", "BIFROST_URL",
         "AI_GATEWAY_KEY", "BIFROST_API_KEY", "AI_GATEWAY_SHAPE"),
        ("secondary", "AI_GATEWAY_BACKUP_URL", "HELICONE_URL",
         "AI_GATEWAY_BACKUP_KEY", "HELICONE_API_KEY",
         "AI_GATEWAY_BACKUP_SHAPE"),
        ("tertiary", "AI_GATEWAY_TERTIARY_URL", "LITELLM_URL",
         "AI_GATEWAY_TERTIARY_KEY", "LITELLM_API_KEY",
         "AI_GATEWAY_TERTIARY_SHAPE"),
    ]
    eps: list[GatewayEndpoint] = []
    for priority, (name, ua, ub, ka, kb, se) in enumerate(tiers):
        url = _env(ua) or _env(ub)
        if not url:
            continue
        eps.append(GatewayEndpoint(
            name=name, url=url,
            api_key=_env(ka) or _env(kb) or "no-key",
            shape=_env(se, "openai"),
            priority=priority,
        ))
    return eps


def _is_failover_error(exc: Exception) -> bool:
    if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout,
                        httpx.ConnectTimeout, httpx.RemoteProtocolError,
                        httpx.PoolTimeout, asyncio.TimeoutError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code >= 500 or code in (408, 429)
    return False


class GatewayRouter:
    """Multi-hop failover with circuit breaking and real SSE streaming.

    Contract:
        * ``call()`` returns ``(text, endpoint_name)`` or raises
          ``NoProviderAvailable`` if every endpoint fails.
        * ``stream()`` yields ``(delta, endpoint_name)`` tuples or raises
          ``NoProviderAvailable`` if the first endpoint that starts
          streaming yields nothing and all remaining endpoints also fail.
        * Circuit state (``failures`` / ``cooldown_until``) is mutated
          only under ``self._lock``.
    """

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._endpoints = _build_endpoints()
        self._client = client
        self._owns_client = client is None
        self._lock = asyncio.Lock()
        if self._endpoints:
            logger.info("gateway_router_initialized endpoints=%d",
                        len(self._endpoints))
        else:
            logger.warning("gateway_router_disabled: no endpoints configured")

    @property
    def enabled(self) -> bool:
        return bool(self._endpoints)

    def get_all_endpoints(self) -> list[GatewayEndpoint]:
        return list(self._endpoints)

    def _ordered(self) -> list[GatewayEndpoint]:
        live = [e for e in self._endpoints if e.is_available]
        return sorted(live, key=lambda e: e.effective_rank())

    def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=GATEWAY_CONNECT_TIMEOUT_S,
                    read=GATEWAY_TIMEOUT_MS / 1000.0,
                    write=GATEWAY_TIMEOUT_MS / 1000.0,
                    pool=GATEWAY_TIMEOUT_MS / 1000.0,
                ),
                limits=httpx.Limits(max_connections=16,
                                    max_keepalive_connections=8),
                http2=True,
            )
        return self._client

    def _headers(self, ep: GatewayEndpoint) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if ep.api_key and ep.api_key != "no-key":
            if ep.shape == "anthropic":
                h["x-api-key"] = ep.api_key
                h["anthropic-version"] = "2023-06-01"
            else:
                h["Authorization"] = f"Bearer {ep.api_key}"
        h.update(ep.extra_headers)
        return h

    def _url(self, ep: GatewayEndpoint, model: str | None) -> str:
        base = ep.url.rstrip("/")
        if ep.shape == "anthropic":
            return f"{base}/v1/messages"
        if ep.shape == "gemini":
            m = model or "gemini-1.5-flash"
            return f"{base}/v1beta/models/{m}:generateContent?key={ep.api_key}"
        return f"{base}/v1/chat/completions"

    async def _mark_failure(self, ep: GatewayEndpoint,
                            rate_limited: bool) -> None:
        async with self._lock:
            ep.failures += 1
            ep.total_failures += 1
            if ep.failures >= GATEWAY_FAILURE_THRESHOLD:
                backoff = GATEWAY_COOLDOWN_S * (
                    2 ** (ep.failures - GATEWAY_FAILURE_THRESHOLD))
                jitter = backoff * 0.2 * random.random()
                cooldown = min(GATEWAY_MAX_COOLDOWN_S, backoff + jitter)
                ep.cooldown_until = time.monotonic() + cooldown
                logger.warning(
                    "gateway_circuit_open endpoint=%s cooldown_s=%.1f failures=%d",
                    ep.name, cooldown, ep.failures)
            if rate_limited:
                ep.cooldown_until = time.monotonic() + 5.0

    async def _mark_success(self, ep: GatewayEndpoint,
                            latency_ms: float) -> None:
        async with self._lock:
            ep.failures = 0
            ep.cooldown_until = 0.0
            ep.last_success_ts = time.monotonic()
            ep.ewma_latency_ms = 0.8 * ep.ewma_latency_ms + 0.2 * latency_ms

    async def call(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int,
        temp: float,
        system: str = "",
    ) -> tuple[str, str]:
        if not self._endpoints:
            raise NoProviderAvailable("GatewayRouter has no endpoints")
        last_err: Exception | None = None
        for ep in self._ordered():
            start = time.monotonic()
            ep.total_calls += 1
            try:
                payload = build_payload(
                    ep.shape, messages, model=model, max_tokens=max_tokens,
                    temp=temp, system=system, stream=False)
                client = self._client_or_create()
                resp = await client.post(
                    self._url(ep, model), json=payload,
                    headers=self._headers(ep))
                resp.raise_for_status()
                latency_ms = (time.monotonic() - start) * 1000
                await self._mark_success(ep, latency_ms)
                return parse_response(ep.shape, resp.json()), ep.name
            except Exception as e:
                last_err = e
                await self._mark_failure(
                    ep,
                    isinstance(e, httpx.HTTPStatusError)
                    and e.response.status_code == 429)
                if not _is_failover_error(e):
                    raise
        raise NoProviderAvailable(
            f"All gateway endpoints failed: {last_err!r}")

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int,
        temp: float,
        system: str = "",
    ) -> AsyncIterator[tuple[str, str]]:
        if not self._endpoints:
            raise NoProviderAvailable("GatewayRouter has no endpoints")
        last_err: Exception | None = None
        for ep in self._ordered():
            start = time.monotonic()
            ep.total_calls += 1
            try:
                payload = build_payload(
                    ep.shape, messages, model=model, max_tokens=max_tokens,
                    temp=temp, system=system, stream=True)
                client = self._client_or_create()
                async with client.stream(
                    "POST", self._url(ep, model),
                    json=payload, headers=self._headers(ep),
                ) as resp:
                    resp.raise_for_status()
                    got_any = False
                    async for delta in stream_for_shape(ep.shape, resp):
                        got_any = True
                        yield delta, ep.name
                    if got_any:
                        await self._mark_success(
                            ep, (time.monotonic() - start) * 1000)
                        return
            except Exception as e:
                last_err = e
                await self._mark_failure(
                    ep,
                    isinstance(e, httpx.HTTPStatusError)
                    and e.response.status_code == 429)
                if not _is_failover_error(e):
                    raise
        raise NoProviderAvailable(
            f"All gateway endpoints failed during stream: {last_err!r}")


# ═══════════════════════════════════════════════════════════════════════════
# §3 — OpenAI-compatible client
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class _Message:
    role: str
    content: str


@dataclass(slots=True)
class _Choice:
    index: int
    message: _Message
    finish_reason: str = "stop"


@dataclass(slots=True)
class _Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(slots=True)
class ChatCompletion:
    id: str
    object: str = "chat.completion"
    created: int = field(default_factory=lambda: int(time.time()))
    model: str = ""
    choices: list[_Choice] = field(default_factory=list)
    usage: _Usage = field(default_factory=_Usage)
    provider: str = ""

    @property
    def text(self) -> str:
        return self.choices[0].message.content if self.choices else ""


@dataclass(slots=True)
class ChatCompletionChunk:
    id: str
    object: str = "chat.completion.chunk"
    created: int = field(default_factory=lambda: int(time.time()))
    model: str = ""
    delta: str = ""
    finish_reason: str | None = None
    provider: str = ""


class _CompletionsResource:
    __slots__ = ("_parent",)

    def __init__(self, parent: "AsyncAxelrClient") -> None:
        self._parent = parent

    async def create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 1024,
        temperature: float = 0.7,
        stream: bool = False,
        **_ignored: Any,
    ) -> ChatCompletion | AsyncIterator[ChatCompletionChunk]:
        return await self._parent._create(
            model=model, messages=messages, max_tokens=max_tokens,
            temperature=temperature, stream=stream)


class _ChatNamespace:
    __slots__ = ("completions",)

    def __init__(self, parent: "AsyncAxelrClient") -> None:
        self.completions = _CompletionsResource(parent)


class AsyncAxelrClient:
    """Drop-in replacement for ``openai.AsyncOpenAI`` routed through GatewayRouter.

    Contract:
        * ``chat.completions.create(..., stream=False)`` → ``ChatCompletion``.
        * ``chat.completions.create(..., stream=True)`` → async iterator of
          ``ChatCompletionChunk``.
        * Raises ``NoProviderAvailable`` if messages contain no non-system turn.
    """

    __slots__ = ("api_key", "base_url", "timeout", "_router", "chat")

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        **_ignored: Any,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self._router = get_gateway_router()
        self.chat = _ChatNamespace(self)

    async def _create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
        stream: bool,
    ) -> ChatCompletion | AsyncIterator[ChatCompletionChunk]:
        system, turns = _split_messages(messages)
        if not turns:
            raise NoProviderAvailable(
                "messages must contain at least one non-system turn")

        if stream:
            async def _gen() -> AsyncIterator[ChatCompletionChunk]:
                chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
                async for delta, provider in self._router.stream(
                    turns, model=model, max_tokens=max_tokens,
                    temp=temperature, system=system,
                ):
                    yield ChatCompletionChunk(
                        id=chunk_id, model=model,
                        delta=delta, provider=provider)
            return _gen()

        text, provider = await self._router.call(
            turns, model=model, max_tokens=max_tokens,
            temp=temperature, system=system)
        return ChatCompletion(
            id=f"chatcmpl-{uuid.uuid4().hex}",
            model=model, provider=provider,
            choices=[_Choice(index=0, message=_Message(
                role="assistant", content=text))])


# ═══════════════════════════════════════════════════════════════════════════
# §4 — Lazy singleton + audit
# ═══════════════════════════════════════════════════════════════════════════
_router_lock = threading.Lock()
_ROUTER: GatewayRouter | None = None


def get_gateway_router(
    *, client: httpx.AsyncClient | None = None,
) -> GatewayRouter:
    """Return the process-wide ``GatewayRouter`` singleton.

    Contract:
        * Thread-safe via ``_router_lock``.
        * First call constructs with ``client``; subsequent calls ignore
          the argument (return the same instance).
        * Never raises for a no-config deployment: returns a router whose
          ``enabled`` is ``False``.
    """
    global _ROUTER
    if _ROUTER is not None:
        return _ROUTER
    with _router_lock:
        if _ROUTER is not None:
            return _ROUTER
        _ROUTER = GatewayRouter(client=client)
        return _ROUTER


def audit() -> dict[str, Any]:
    """JSON-serialisable snapshot of the gateway state."""
    router = get_gateway_router()
    return {
        "version": __version__,
        "enabled": router.enabled,
        "endpoints": [ep.to_dict() for ep in router.get_all_endpoints()],
    }


__all__ = [
    "AsyncAxelrClient", "ChatCompletion", "ChatCompletionChunk",
    "GatewayRouter", "GatewayEndpoint", "get_gateway_router",
    "to_openai", "to_anthropic", "to_gemini",
    "from_openai_response", "from_anthropic_response", "from_gemini_response",
    "detect_shape", "build_payload", "parse_response",
    "audit",
]