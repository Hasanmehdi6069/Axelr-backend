# core/streaming.py
"""
AXELR — Streaming Layer (intake · scrub · race · cancel · providers)
=====================================================================
Every streaming primitive in the codebase, in one file.

    §1  Chunked reader      bounded intake from any async byte source
    §2  In-flight scrubber  chunk-by-chunk regex state machine
    §3  First-token race    race N streamers, first valid token wins
    §4  Cancellation        propagate client disconnect to upstream tasks
    §5  Provider streamers  15 provider SSE adapters
"""
from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("axelr.stream")

# ═══════════════════════════════════════════════════════════════════════════
# §1 — Chunked reader (back-pressured intake)
# ═══════════════════════════════════════════════════════════════════════════
CHUNK_BYTES = 4096
MAX_INFLIGHT_CHUNKS = 32
_SENTINEL: object = object()


def _wrap_source(src: Any, chunk: int) -> AsyncIterator[bytes]:
    if src is None:
        async def _empty():
            if False: yield b""
        return _empty()
    if isinstance(src, (bytes, bytearray, memoryview)):
        data = bytes(src)
        async def _from_bytes():
            for i in range(0, len(data), chunk):
                yield data[i:i + chunk]
        return _from_bytes()
    if hasattr(src, "__aiter__"):
        return src
    read = getattr(src, "read", None)
    if read is not None:
        async def _from_read():
            while True:
                piece = read(chunk)
                if asyncio.iscoroutine(piece):
                    piece = await piece
                if not piece:
                    return
                yield piece if isinstance(piece, bytes) else bytes(piece)
        return _from_read()
    raise TypeError(f"unsupported stream source: {type(src).__name__}")


class ChunkedReader:
    """Bounded async byte reader. One producer, one bounded queue."""
    __slots__ = ("_queue", "_producer", "_total", "_max", "_closed", "_source")

    def __init__(self, source: Any, *, chunk_bytes: int = CHUNK_BYTES,
                 max_inflight: int = MAX_INFLIGHT_CHUNKS) -> None:
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max(1, max_inflight))
        self._producer: asyncio.Task | None = None
        self._total = 0
        self._max = max(256, int(chunk_bytes))
        self._closed = False
        self._source = source

    @property
    def total_bytes(self) -> int: return self._total

    async def _pump(self) -> None:
        try:
            async for chunk in _wrap_source(self._source, self._max):
                if not chunk: continue
                self._total += len(chunk)
                await self._queue.put(chunk)
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            with contextlib.suppress(asyncio.QueueFull):
                await self._queue.put(e)
        finally:
            with contextlib.suppress(asyncio.QueueFull):
                await self._queue.put(_SENTINEL)

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[bytes]:
        if self._producer is None:
            self._producer = asyncio.create_task(self._pump(), name="chunk_pump")
        try:
            while True:
                item = await self._queue.get()
                if item is _SENTINEL: return
                if isinstance(item, BaseException): raise item
                yield item
        finally:
            self._closed = True
            if self._producer is not None and not self._producer.done():
                self._producer.cancel()
                with contextlib.suppress(BaseException):
                    await self._producer

    async def read_text(self, *, max_bytes: int | None = None,
                        encoding: str = "utf-8") -> str:
        decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        parts: list[str] = []
        seen = 0
        async for chunk in self:
            seen += len(chunk)
            if max_bytes is not None and seen > max_bytes:
                chunk = chunk[:max(0, max_bytes - (seen - len(chunk)))]
            parts.append(decoder.decode(chunk, final=False))
            if max_bytes is not None and seen >= max_bytes:
                break
        parts.append(decoder.decode(b"", final=True))
        return "".join(parts)

    async def read_b64(self, *, max_bytes: int | None = None) -> str:
        import base64
        buf = bytearray()
        async for chunk in self:
            buf.extend(chunk)
            if max_bytes is not None and len(buf) >= max_bytes:
                del buf[max_bytes:]
                break
        return base64.b64encode(bytes(buf)).decode("ascii")

    async def close(self) -> None:
        self._closed = True
        if self._producer is not None and not self._producer.done():
            self._producer.cancel()
            with contextlib.suppress(BaseException):
                await self._producer


async def read_upload_streaming(
    upload: Any, *, max_bytes: int = 25 * 1024 * 1024,
    chunk_bytes: int = CHUNK_BYTES,
) -> dict[str, Any]:
    import base64
    filename = getattr(upload, "filename", "upload.bin") or "upload.bin"
    mimetype = getattr(upload, "content_type", "application/octet-stream") \
        or "application/octet-stream"
    total = 0
    b64 = bytearray()
    async for chunk in ChunkedReader(upload, chunk_bytes=chunk_bytes):
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"upload exceeds {max_bytes} bytes")
        b64.extend(base64.b64encode(chunk))
    return {"filename": filename, "mimetype": mimetype,
            "content_base64": b64.decode("ascii"), "size": total}


# ═══════════════════════════════════════════════════════════════════════════
# §2 — In-flight scrubber
# ═══════════════════════════════════════════════════════════════════════════
_MAX_CARRY = 512
_REFUSAL_RE = re.compile(
    r"(?i)^\s*(?:i(?:'m| am)?\s+(?:sorry|unable)|i\s+cannot|i\s+can'?t|"
    r"as an ai|i'?m just an ai)[^\n]{0,200}\n+")
_LEAK_RE = re.compile(
    r"</?(?:system_identity|core_directives|reasoning_protocol|"
    r"intent_veto|data_drift_defiance|modern_styling|thinking)>", re.I)
_GENERIC_OPENER_RE = re.compile(
    r"(?i)^\s*(?:sure[!.,]?|of course[!.,]?|certainly[!.,]?|absolutely[!.,]?|"
    r"great question[!.,]?|happy to help[!.,]?)\s+")
_MULTISPACE_RE = re.compile(r"[ \t]{3,}")
_MULTINEWLINE_RE = re.compile(r"\n{4,}")
_WATERMARK_RE = re.compile(r"\n\n---\n\*[^*]{0,200}\*\s*$")


class InFlightScrubber:
    __slots__ = ("_carry", "_emitted_any", "_aggressive", "_closed",
                 "_fired_leak", "_fired_refusal", "_fired_opener", "_watermark_cut")

    def __init__(self, *, aggressive: bool = True) -> None:
        self._carry = ""
        self._emitted_any = False
        self._aggressive = aggressive
        self._closed = False
        self._fired_leak = False
        self._fired_refusal = False
        self._fired_opener = False
        self._watermark_cut = False

    def push(self, chunk: str) -> list[str]:
        if self._closed or not chunk: return []
        self._carry += chunk
        out: list[str] = []
        if self._aggressive:
            m = _WATERMARK_RE.search(self._carry)
            if m:
                keep = self._carry[:m.start()]
                self._carry = ""
                if keep: out.append(keep)
                self._watermark_cut = True
                self._closed = True
                return out
        if not self._emitted_any and self._aggressive and not self._fired_refusal:
            m = _REFUSAL_RE.match(self._carry)
            if m:
                self._carry = self._carry[m.end():]
                self._fired_refusal = True
        if not self._fired_leak:
            new = _LEAK_RE.sub("", self._carry)
            if new != self._carry:
                self._fired_leak = True
                self._carry = new
        if not self._emitted_any and self._aggressive and not self._fired_opener:
            m = _GENERIC_OPENER_RE.match(self._carry)
            if m:
                self._carry = self._carry[m.end():]
                self._fired_opener = True
        if self._aggressive:
            self._carry = _MULTISPACE_RE.sub(" ", self._carry)
            self._carry = _MULTINEWLINE_RE.sub("\n\n\n", self._carry)
        if len(self._carry) > _MAX_CARRY:
            cut = len(self._carry) - _MAX_CARRY
            out.append(self._carry[:cut])
            self._carry = self._carry[cut:]
            self._emitted_any = True
        return out

    def flush(self) -> list[str]:
        if self._closed and not self._carry: return []
        tail = self._carry
        self._carry = ""
        self._closed = True
        return [tail] if tail else []

    @property
    def watermark_seen(self) -> bool: return self._watermark_cut


async def scrub_stream(
    upstream: AsyncIterator[str], *, aggressive: bool = True, queue_depth: int = 16,
) -> AsyncIterator[str]:
    scrb = InFlightScrubber(aggressive=aggressive)
    queue: asyncio.Queue = asyncio.Queue(maxsize=max(2, queue_depth))
    _END = object()

    async def _producer() -> None:
        try:
            async for tok in upstream:
                for piece in scrb.push(tok):
                    if piece: await queue.put(piece)
            for piece in scrb.flush():
                if piece: await queue.put(piece)
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            with contextlib.suppress(asyncio.QueueFull):
                await queue.put(e)
        finally:
            with contextlib.suppress(asyncio.QueueFull):
                await queue.put(_END)

    task = asyncio.create_task(_producer(), name="scrub_producer")
    try:
        while True:
            item = await queue.get()
            if item is _END: return
            if isinstance(item, BaseException):
                logger.warning("scrub_upstream_error error=%s", item); return
            yield item
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


# ═══════════════════════════════════════════════════════════════════════════
# §3 — First-token race
# ═══════════════════════════════════════════════════════════════════════════
StreamerFunc = Callable[..., AsyncIterator[str]]


@dataclass(slots=True)
class RaceOutcome:
    provider: str
    model: str
    first_token_ms: float
    total_ms: float
    tokens: int


class _LoserAbort(Exception):
    pass


class FirstTokenRace:
    __slots__ = ("_streamers", "_race_width", "_stall_s")

    def __init__(self, streamers: dict[str, StreamerFunc], *,
                 race_width: int = 4, stall_ms: int = 3000) -> None:
        self._streamers = dict(streamers)
        self._race_width = max(2, int(race_width))
        self._stall_s = max(0.5, stall_ms / 1000.0)

    async def race(self, prompt: str, *, candidates: list[str] | None = None,
                   model_map: dict[str, list[str]] | None = None,
                   max_tokens: int = 2048, temp: float = 0.4) -> AsyncIterator[str]:
        pool = candidates or list(self._streamers.keys())
        pool = [p for p in pool if p in self._streamers][:self._race_width]
        if not pool: return
        model_map = model_map or {}
        winner_q: asyncio.Queue = asyncio.Queue(maxsize=256)
        losers: list[asyncio.Task] = []
        winner_holder: dict[str, RaceOutcome] = {}
        first_token_event = asyncio.Event()
        winner_provider = {"name": ""}

        async def _run(provider: str, model: str) -> None:
            streamer = self._streamers[provider]
            t0 = time.monotonic()
            first_ms: float | None = None
            tokens = 0
            try:
                async for tok in streamer(model=model, prompt=prompt,
                                          max_tokens=max_tokens, temp=temp):
                    if not tok: continue
                    if first_ms is None:
                        first_ms = (time.monotonic() - t0) * 1000.0
                        if not first_token_event.is_set():
                            winner_provider["name"] = provider
                            winner_holder["outcome"] = RaceOutcome(
                                provider=provider, model=model,
                                first_token_ms=first_ms, total_ms=first_ms, tokens=0)
                            first_token_event.set()
                        else:
                            raise _LoserAbort()
                    if winner_provider["name"] != provider:
                        raise _LoserAbort()
                    tokens += 1
                    await winner_q.put(tok)
            except _LoserAbort:
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("race_stream_error provider=%s error=%s", provider, e)
                return
            finally:
                if winner_provider["name"] == provider:
                    with contextlib.suppress(asyncio.QueueFull):
                        await winner_q.put(None)
                    if "outcome" in winner_holder:
                        o = winner_holder["outcome"]
                        winner_holder["outcome"] = RaceOutcome(
                            provider=o.provider, model=o.model,
                            first_token_ms=o.first_token_ms,
                            total_ms=(time.monotonic() - t0) * 1000.0, tokens=tokens)

        for p in pool:
            models = model_map.get(p) or []
            model = models[0] if models else ""
            if not model: continue
            losers.append(asyncio.create_task(_run(p, model), name=f"race::{p}"))
        if not losers: return

        try:
            await asyncio.wait_for(first_token_event.wait(), timeout=self._stall_s * 2)
        except asyncio.TimeoutError:
            for t in losers:
                if not t.done(): t.cancel()
            await asyncio.gather(*losers, return_exceptions=True)
            return

        for t in losers:
            if not t.done() and t.get_name() != f"race::{winner_provider['name']}":
                t.cancel()
        try:
            while True:
                item = await winner_q.get()
                if item is None: return
                yield item
        finally:
            for t in losers:
                if not t.done(): t.cancel()
            await asyncio.gather(*losers, return_exceptions=True)


# ═══════════════════════════════════════════════════════════════════════════
# §4 — Cancellation
# ═══════════════════════════════════════════════════════════════════════════
_POLL_INTERVAL = 0.25


class CancellationToken:
    __slots__ = ("_event",)

    def __init__(self) -> None: self._event = asyncio.Event()
    def set(self) -> None: self._event.set()
    def is_set(self) -> bool: return self._event.is_set()
    async def wait(self) -> None: await self._event.wait()


class CancelledByClient(Exception):
    pass


def bind_to_request(request: Any, token: CancellationToken) -> asyncio.Task | None:
    is_disconnected = getattr(request, "is_disconnected", None)
    if not callable(is_disconnected):
        return None

    async def _watch() -> None:
        try:
            while not token.is_set():
                with contextlib.suppress(Exception):
                    if await is_disconnected():
                        token.set(); return
                await asyncio.sleep(_POLL_INTERVAL)
        except asyncio.CancelledError:
            return

    return asyncio.create_task(_watch(), name="cancel_watcher")


async def cancellable(agen: AsyncIterator[Any], token: CancellationToken) -> AsyncIterator[Any]:
    if not token.is_set():
        try:
            async for item in agen:
                yield item
                if token.is_set(): break
        except CancelledByClient:
            pass
        finally:
            aclose = getattr(agen, "aclose", None)
            if callable(aclose):
                with contextlib.suppress(Exception):
                    await aclose()
    else:
        aclose = getattr(agen, "aclose", None)
        if callable(aclose):
            with contextlib.suppress(Exception):
                await aclose()


# ═══════════════════════════════════════════════════════════════════════════
# §5 — Provider streamers (from streaming_providers.py)
# ═══════════════════════════════════════════════════════════════════════════
_MAX_LINE_BYTES = 64 * 1024

_ANTHROPIC_URL = os.getenv("ANTHROPIC_URL", "https://api.anthropic.com/v1/messages")
_COHERE_URL = os.getenv("COHERE_URL", "https://api.cohere.com/v2/chat")
_GEMINI_URL = os.getenv("GEMINI_URL", "https://generativelanguage.googleapis.com/v1beta")
_CF_AI_URL = os.getenv(
    "CF_AI_URL",
    "https://api.cloudflare.com/client/v4/accounts/{acct}/ai/v1/chat/completions")

_FALLBACK_CLIENT: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    try:
        from api.config import get_http_client  # type: ignore
        return get_http_client()
    except Exception:
        global _FALLBACK_CLIENT
        if _FALLBACK_CLIENT is None or _FALLBACK_CLIENT.is_closed:
            _FALLBACK_CLIENT = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=8.0),
                limits=httpx.Limits(max_connections=32,
                                    max_keepalive_connections=16),
                follow_redirects=False,
            )
        return _FALLBACK_CLIENT


async def _iter_sse_lines(resp: httpx.Response) -> AsyncIterator[bytes]:
    buf = bytearray()
    async for chunk in resp.aiter_bytes(4096):
        buf.extend(chunk)
        if len(buf) > _MAX_LINE_BYTES * 4:
            buf.clear(); continue
        while True:
            nl = buf.find(b"\n")
            if nl == -1: break
            line = bytes(buf[:nl]).rstrip(b"\r")
            del buf[: nl + 1]
            if not line or line.startswith(b":"): continue
            if not line.startswith(b"data:"): continue
            payload = line[5:].lstrip()
            if payload == b"[DONE]": return
            yield payload


async def _stream_openai_sse(resp: httpx.Response) -> AsyncIterator[str]:
    async for raw in _iter_sse_lines(resp):
        try: obj = json.loads(raw)
        except Exception: continue
        choices = obj.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            piece = delta.get("content")
            if piece: yield piece
            if choices[0].get("finish_reason"): return


async def _stream_anthropic_sse(resp: httpx.Response) -> AsyncIterator[str]:
    async for raw in _iter_sse_lines(resp):
        try: obj = json.loads(raw)
        except Exception: continue
        t = obj.get("type")
        if t == "content_block_delta":
            d = (obj.get("delta") or {}).get("text")
            if d: yield d
        elif t == "message_stop": return


async def _stream_gemini_sse(resp: httpx.Response) -> AsyncIterator[str]:
    async for raw in _iter_sse_lines(resp):
        try: obj = json.loads(raw)
        except Exception: continue
        for cand in obj.get("candidates") or []:
            for part in (cand.get("content") or {}).get("parts") or []:
                txt = part.get("text")
                if txt: yield txt
            if cand.get("finishReason"): return


async def _iter_sse_for_shape(shape: str, resp: httpx.Response) -> AsyncIterator[str]:
    """Public-ish dispatcher for gateway.py."""
    if shape == "anthropic":
        async for t in _stream_anthropic_sse(resp): yield t
    elif shape == "gemini":
        async for t in _stream_gemini_sse(resp): yield t
    else:
        async for t in _stream_openai_sse(resp): yield t


async def _stream_openai_compat(
    *, url: str, api_key: str, model: str, prompt: str,
    max_tokens: int, temp: float, timeout_s: float = 60.0,
    extra_headers: dict[str, str] | None = None,
    extra_body: dict[str, Any] | None = None,
) -> AsyncIterator[str]:
    if not api_key or not model: return
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json", "Accept": "text/event-stream",
               **(extra_headers or {})}
    body: dict[str, Any] = {"model": model,
                            "messages": [{"role": "user", "content": prompt}],
                            "max_tokens": max_tokens, "temperature": temp,
                            "stream": True, **(extra_body or {})}
    client = _client()
    try:
        req = client.build_request("POST", url, headers=headers, json=body)
        resp = await client.send(req, stream=True, timeout=timeout_s)
        try:
            if resp.status_code != 200:
                logger.debug("openai_compat_non200 status=%s", resp.status_code)
                return
            async for piece in _stream_openai_sse(resp):
                yield piece
        finally:
            with contextlib.suppress(Exception): await resp.aclose()
    except asyncio.CancelledError: raise
    except Exception as e:
        logger.debug("openai_compat_error error=%s", e); return


async def _stream_gemini(*, api_key: str, model: str, prompt: str,
                         max_tokens: int, temp: float,
                         timeout_s: float = 60.0) -> AsyncIterator[str]:
    if not api_key or not model: return
    url = f"{_GEMINI_URL}/models/{model}:streamGenerateContent?alt=sse"
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json",
               "Accept": "text/event-stream"}
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temp}}
    client = _client()
    try:
        req = client.build_request("POST", url, headers=headers, json=body)
        resp = await client.send(req, stream=True, timeout=timeout_s)
        try:
            if resp.status_code != 200: return
            async for piece in _stream_gemini_sse(resp): yield piece
        finally:
            with contextlib.suppress(Exception): await resp.aclose()
    except asyncio.CancelledError: raise
    except Exception as e:
        logger.debug("gemini_error error=%s", e); return


async def _stream_anthropic(*, api_key: str, model: str, prompt: str,
                            max_tokens: int, temp: float,
                            timeout_s: float = 60.0) -> AsyncIterator[str]:
    if not api_key or not model: return
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
               "Content-Type": "application/json", "Accept": "text/event-stream"}
    body = {"model": model, "max_tokens": max_tokens, "temperature": temp,
            "stream": True, "messages": [{"role": "user", "content": prompt}]}
    client = _client()
    try:
        req = client.build_request("POST", _ANTHROPIC_URL, headers=headers, json=body)
        resp = await client.send(req, stream=True, timeout=timeout_s)
        try:
            if resp.status_code != 200: return
            async for piece in _stream_anthropic_sse(resp): yield piece
        finally:
            with contextlib.suppress(Exception): await resp.aclose()
    except asyncio.CancelledError: raise
    except Exception as e:
        logger.debug("anthropic_error error=%s", e); return


async def _stream_cohere(*, api_key: str, model: str, prompt: str,
                         max_tokens: int, temp: float,
                         timeout_s: float = 60.0) -> AsyncIterator[str]:
    if not api_key or not model: return
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json", "Accept": "text/event-stream"}
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "temperature": temp, "stream": True}
    client = _client()
    try:
        req = client.build_request("POST", _COHERE_URL, headers=headers, json=body)
        resp = await client.send(req, stream=True, timeout=timeout_s)
        try:
            if resp.status_code != 200: return
            async for raw in _iter_sse_lines(resp):
                try: obj = json.loads(raw)
                except Exception: continue
                t = obj.get("type")
                if t == "content-delta":
                    piece = (((obj.get("delta") or {}).get("message") or {})
                             .get("content") or {}).get("text")
                    if piece: yield piece
                elif t == "message-end": return
        finally:
            with contextlib.suppress(Exception): await resp.aclose()
    except asyncio.CancelledError: raise
    except Exception as e:
        logger.debug("cohere_error error=%s", e); return


# ── Registry ─────────────────────────────────────────────────────────
_OPENAI_SHAPED: dict[str, tuple[str, str, str, dict[str, str]]] = {
    "groq":       ("GROQ_URL", "GROQ_API_KEY",
                   "https://api.groq.com/openai/v1/chat/completions", {}),
    "openrouter": ("OPENROUTER_URL", "OPENROUTER_API_KEY",
                   "https://openrouter.ai/api/v1/chat/completions",
                   {"HTTP-Referer": os.getenv("OPENROUTER_REFERER", "https://axelr.ai"),
                    "X-Title":      os.getenv("OPENROUTER_TITLE", "Axelr AI")}),
    "mistral":    ("MISTRAL_URL", "MISTRAL_API_KEY",
                   "https://api.mistral.ai/v1/chat/completions", {}),
    "deepseek":   ("DEEPSEEK_URL", "DEEPSEEK_API_KEY",
                   "https://api.deepseek.com/v1/chat/completions", {}),
    "together":   ("TOGETHER_URL", "TOGETHER_API_KEY",
                   "https://api.together.xyz/v1/chat/completions", {}),
    "cerebras":   ("CEREBRAS_URL", "CEREBRAS_API_KEY",
                   "https://api.cerebras.ai/v1/chat/completions", {}),
    "hyperbolic": ("HYPERBOLIC_URL", "HYPERBOLIC_API_KEY",
                   "https://api.hyperbolic.xyz/v1/chat/completions", {}),
    "sambanova":  ("SAMBANOVA_URL", "SAMBANOVA_API_KEY",
                   "https://api.sambanova.ai/v1/chat/completions", {}),
    "novita":     ("NOVITA_URL", "NOVITA_API_KEY",
                   "https://api.novita.ai/v3/openai/chat/completions", {}),
    "fireworks":  ("FIREWORKS_URL", "FIREWORKS_API_KEY",
                   "https://api.fireworks.ai/inference/v1/chat/completions", {}),
    "nebius":     ("NEBIUS_API_ENDPOINT", "NEBIUS_API_KEY",
                   "https://api.studio.nebius.ai/v1/chat/completions", {}),
}


async def _make_openai_streamer(name: str, **kw: Any) -> AsyncIterator[str]:
    url_env, key_env, default_url, extra = _OPENAI_SHAPED[name]
    url = os.getenv(url_env, default_url)
    api_key = os.getenv(key_env, "").strip()
    async for t in _stream_openai_compat(url=url, api_key=api_key,
                                          extra_headers=extra, **kw):
        yield t


async def _groq(**kw):
    async for t in _make_openai_streamer("groq", **kw):
        yield t
async def _openrouter(**kw):
    async for t in _make_openai_streamer("openrouter", **kw):
        yield t
async def _mistral(**kw):
    async for t in _make_openai_streamer("mistral", **kw):
        yield t
async def _deepseek(**kw):
    async for t in _make_openai_streamer("deepseek", **kw):
        yield t
async def _together(**kw):
    async for t in _make_openai_streamer("together", **kw):
        yield t
async def _cerebras(**kw):
    async for t in _make_openai_streamer("cerebras", **kw):
        yield t
async def _hyperbolic(**kw):
    async for t in _make_openai_streamer("hyperbolic", **kw):
        yield t
async def _sambanova(**kw):
    async for t in _make_openai_streamer("sambanova", **kw):
        yield t
async def _novita(**kw):
    async for t in _make_openai_streamer("novita", **kw):
        yield t
async def _fireworks(**kw):
    async for t in _make_openai_streamer("fireworks", **kw):
        yield t
async def _nebius(**kw):
    async for t in _make_openai_streamer("nebius", **kw):
        yield t


async def _gemini(**kw: Any) -> AsyncIterator[str]:
    api_key = os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")
    async for t in _stream_gemini(api_key=api_key, **kw):
        yield t


async def _anthropic(**kw: Any) -> AsyncIterator[str]:
    async for t in _stream_anthropic(
        api_key=os.getenv("ANTHROPIC_API_KEY", ""), **kw):
        yield t


async def _cohere(**kw: Any) -> AsyncIterator[str]:
    async for t in _stream_cohere(
        api_key=os.getenv("COHERE_API_KEY", ""), **kw):
        yield t


async def _cloudflare(**kw: Any) -> AsyncIterator[str]:
    acct = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
    key = os.getenv("CLOUDFLARE_API_KEY", "").strip()
    if not acct or not key: return
    async for t in _stream_openai_compat(
        url=_CF_AI_URL.format(acct=acct), api_key=key,
        extra_body={"account_id": acct}, **kw): yield t


PROVIDER_STREAMERS: dict[str, StreamerFunc] = {
    "groq": _groq, "openrouter": _openrouter, "mistral": _mistral,
    "deepseek": _deepseek, "together": _together, "cerebras": _cerebras,
    "hyperbolic": _hyperbolic, "sambanova": _sambanova, "novita": _novita,
    "fireworks": _fireworks, "nebius": _nebius, "nvidia": _nebius,
    "gemini": _gemini, "anthropic": _anthropic, "cohere": _cohere,
    "cloudflare": _cloudflare,
}


async def stream_completion(
    provider: str, prompt: str, *, model: str,
    max_tokens: int = 2048, temp: float = 0.4, timeout_s: float = 60.0,
) -> AsyncIterator[str]:
    fn = PROVIDER_STREAMERS.get(provider)
    if fn is None: return
    async for tok in fn(model=model, prompt=prompt,
                        max_tokens=max_tokens, temp=temp, timeout_s=timeout_s):
        if tok: yield tok


__all__ = [
    "CHUNK_BYTES", "MAX_INFLIGHT_CHUNKS", "ChunkedReader", "read_upload_streaming",
    "InFlightScrubber", "scrub_stream",
    "FirstTokenRace", "RaceOutcome", "StreamerFunc",
    "CancellationToken", "CancelledByClient", "bind_to_request", "cancellable",
    "PROVIDER_STREAMERS", "stream_completion",
    "_stream_openai_sse", "_stream_anthropic_sse", "_stream_gemini_sse",
    "_iter_sse_for_shape",
]