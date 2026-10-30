# core/utils/security_and_streaming.py
"""
AXELR — Unified Security & Streaming Layer (v4.1)
==================================================
Merged home for PII sanitization, SSE parsing, and streaming primitives.

Fixes over v4.0
---------------
The v4.0 file called ``asyncio.run(...)`` from inside ``sanitize()``,
``ChunkedReader.__init__``, ``InFlightScrubber.__init__``, and ``audit()``.
Every one of those raised ``RuntimeError: asyncio.run() cannot be called
from a running event loop`` when invoked from async code — and
``sanitize()`` runs on every pipeline request via
``core.pipeline.sanitize_pii``. v4.1 replaces all four call-sites with a
``threading.Lock``-guarded counter dict; no event loop required, no
per-call async overhead, correct under concurrency.

The v4.0 ``_GLOBAL_LOCK`` async lock was also removed — the counters it
guarded are not awaited on any hot path, and mutating a plain dict under
a ``threading.Lock`` is cheaper and doesn't require an event loop.

Contract:
    * ``sanitize()`` is synchronous and safe from both sync and async
      call-sites. Never raises.
    * All iterators propagate ``CancelledError`` faithfully and release
      their producer tasks in ``finally`` blocks.
    * ``audit()`` returns a dict; the former async version is gone.
"""
from __future__ import annotations

import asyncio
import base64
import codecs
import contextlib
import json
import logging
import re
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("axelr.utils.security_and_streaming")
__version__ = "4.1.0"

# ═══════════════════════════════════════════════════════════════════════════
# Audit counters (sync, thread-safe, no event loop required)
# ═══════════════════════════════════════════════════════════════════════════
_STATS_LOCK = threading.Lock()
_AUDIT_STATS: dict[str, int] = {
    "sse_payloads_iterated": 0,
    "streams_shaped": 0,
    "chunked_readers_created": 0,
    "scrubbers_created": 0,
    "pii_items_redacted": 0,
}


def _bump(key: str, by: int = 1) -> None:
    """Atomic counter increment. Never raises."""
    with _STATS_LOCK:
        _AUDIT_STATS[key] = _AUDIT_STATS.get(key, 0) + by


def audit() -> dict[str, Any]:
    """Return a snapshot of streaming-security counters. Never raises."""
    with _STATS_LOCK:
        return {"version": __version__, "stats": dict(_AUDIT_STATS)}


# ═══════════════════════════════════════════════════════════════════════════
# §1 — PII sanitization
# ═══════════════════════════════════════════════════════════════════════════
PIIType = str

_PII_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
                re.IGNORECASE), "email", "[REDACTED_EMAIL]"),
    (re.compile(r"\b(\+\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}"
                r"[-.\s]?\d{4}\b"), "phone", "[REDACTED_PHONE]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
     "ip_address", "[REDACTED_IP]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "ssn", "[REDACTED_SSN]"),
    (re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
     "credit_card", "[REDACTED_CC]"),
    (re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),
     "api_key", "[REDACTED_API_KEY]"),
    (re.compile(r"\bpk_[A-Za-z0-9]{32,}\b"),
     "api_key", "[REDACTED_API_KEY]"),
    (re.compile(r"\b[A-Za-z0-9/+]{32,}\b"),
     "api_key", "[REDACTED_API_KEY]"),
    (re.compile(r"\b(?:password|passwd|pwd|secret|token|auth)[^\s]*"
                r"\s*[=:]\s*.{8,}\b", re.IGNORECASE),
     "credential", "[REDACTED_CREDENTIAL]"),
    (re.compile(r"\b(?:usd|eur|gbp|cad|aud|jpy|cny)\s*\d{1,3}"
                r"(?:,\d{3})*(?:\.\d{2})?\b", re.IGNORECASE),
     "financial", "[REDACTED_FINANCIAL]"),
    (re.compile(r"\$\d{1,3}(?:,\d{3})*(?:\.\d{2})?\b"),
     "financial", "[REDACTED_FINANCIAL]"),
]

# Backward-compatible accumulator. Unbounded growth was the v4.0 behavior;
# we preserve the name so external code reading it does not crash.
PII_DETECTED: list[str] = []


def sanitize(text: str) -> tuple[str, list[str]]:
    """Replace every detected PII pattern in ``text``.

    Returns ``(sanitized_text, sorted_list_of_types_found)``.
    Safe from sync and async callers. Never raises.
    """
    if not text:
        return "", []
    found: set[str] = set()
    out = text
    for rx, pii_type, repl in _PII_PATTERNS:
        try:
            if rx.search(out):
                out = rx.sub(repl, out)
                found.add(pii_type)
                _bump("pii_items_redacted")
        except Exception:
            continue
    result = sorted(found)
    PII_DETECTED.extend(result)
    return out, result


def found_types(text: str) -> list[str]:
    """Detection without mutation. Returns sorted type names. Never raises."""
    if not text:
        return []
    found: set[str] = set()
    for rx, pii_type, _ in _PII_PATTERNS:
        try:
            if rx.search(text):
                found.add(pii_type)
        except Exception:
            continue
    return sorted(found)


# ═══════════════════════════════════════════════════════════════════════════
# §2 — SSE parsing
# ═══════════════════════════════════════════════════════════════════════════
_MAX_LINE_BYTES = 64 * 1024


async def iter_payloads(resp: httpx.Response) -> AsyncIterator[str]:
    """Yield ``data:`` payloads from an SSE response.

    Filters ``[DONE]`` and ``:`` comments. Bounded buffer (cleared if it
    exceeds 8 × ``_MAX_LINE_BYTES``). Never raises on malformed UTF-8.
    """
    buf = bytearray()
    async for chunk in resp.aiter_bytes(8192):
        buf.extend(chunk)
        if len(buf) > _MAX_LINE_BYTES * 8:
            buf.clear()
        while True:
            nl = buf.find(b"\n")
            if nl < 0:
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
            _bump("sse_payloads_iterated")
            yield payload.decode("utf-8", errors="ignore")


async def stream_for_shape(
    shape: str, resp: httpx.Response,
) -> AsyncIterator[str]:
    """Dispatch SSE parsing by provider shape.

    Yields text deltas. Recognised shapes: ``openai`` (default),
    ``anthropic``, ``gemini``, ``cohere``. Unknown shapes fall through to
    ``openai``. Never raises on malformed payloads — bad lines are skipped.
    """
    _bump("streams_shaped")
    if shape == "anthropic":
        async for raw in iter_payloads(resp):
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if obj.get("type") == "content_block_delta":
                delta = (obj.get("delta") or {}).get("text")
                if delta:
                    yield delta
            elif obj.get("type") == "message_stop":
                return
    elif shape == "gemini":
        async for raw in iter_payloads(resp):
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
    elif shape == "cohere":
        async for raw in iter_payloads(resp):
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            t = obj.get("type")
            if t == "content-delta":
                piece = ((((obj.get("delta") or {}).get("message") or {})
                          .get("content") or {}).get("text"))
                if piece:
                    yield piece
            elif t == "message-end":
                return
    else:  # openai
        async for raw in iter_payloads(resp):
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
            else:
                txt = obj.get("text") or obj.get("content")
                if isinstance(txt, str) and txt:
                    yield txt


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Streaming primitives
# ═══════════════════════════════════════════════════════════════════════════
CHUNK_BYTES = 4096
MAX_INFLIGHT_CHUNKS = 32
_SENTINEL: object = object()


def _wrap_source(src: Any, chunk: int) -> AsyncIterator[bytes]:
    """Coerce ``src`` into an async byte iterator. Never raises on empty."""
    if src is None:
        async def _empty() -> AsyncIterator[bytes]:
            if False:
                yield b""
        return _empty()
    if isinstance(src, (bytes, bytearray, memoryview)):
        data = bytes(src)

        async def _from_bytes() -> AsyncIterator[bytes]:
            for i in range(0, len(data), chunk):
                yield data[i:i + chunk]
        return _from_bytes()
    if hasattr(src, "__aiter__"):
        return src
    read = getattr(src, "read", None)
    if read is not None:
        async def _from_read() -> AsyncIterator[bytes]:
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
    """Bounded async byte reader. One producer task, one bounded queue.

    Contract:
        * ``__init__`` schedules nothing — the producer task starts on
          first ``__aiter__`` access.
        * ``read_text(max_bytes=...)`` and ``read_b64(max_bytes=...)``
          terminate at the cap without draining the producer.
        * ``close()`` cancels the producer and awaits its exit. Safe to
          call twice.
        * ``total_bytes`` is a live counter.
    """
    __slots__ = ("_queue", "_producer", "_total", "_max", "_closed", "_source")

    def __init__(
        self, source: Any, *,
        chunk_bytes: int = CHUNK_BYTES,
        max_inflight: int = MAX_INFLIGHT_CHUNKS,
    ) -> None:
        self._queue: asyncio.Queue[Any] = asyncio.Queue(
            maxsize=max(1, max_inflight))
        self._producer: asyncio.Task[None] | None = None
        self._total = 0
        self._max = max(256, int(chunk_bytes))
        self._closed = False
        self._source = source
        _bump("chunked_readers_created")

    async def _pump(self) -> None:
        try:
            async for chunk in _wrap_source(self._source, self._max):
                if not chunk:
                    continue
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

    async def _iter(self) -> AsyncIterator[bytes]:
        if self._producer is None:
            self._producer = asyncio.create_task(
                self._pump(), name="chunk_pump")
        try:
            while True:
                item = await self._queue.get()
                if item is _SENTINEL:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            self._closed = True
            if self._producer is not None and not self._producer.done():
                self._producer.cancel()
                with contextlib.suppress(BaseException):
                    await self._producer

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iter()

    async def read_text(
        self, *, max_bytes: int | None = None, encoding: str = "utf-8",
    ) -> str:
        """Decode the stream to text. Stops at ``max_bytes`` if set."""
        decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        parts: list[str] = []
        seen = 0
        async for chunk in self:
            seen += len(chunk)
            if max_bytes is not None and seen > max_bytes:
                chunk = chunk[: max(0, max_bytes - (seen - len(chunk)))]
            parts.append(decoder.decode(chunk, final=False))
            if max_bytes is not None and seen >= max_bytes:
                break
        parts.append(decoder.decode(b"", final=True))
        return "".join(parts)

    async def read_b64(self, *, max_bytes: int | None = None) -> str:
        """Encode the stream to base64. Stops at ``max_bytes`` if set."""
        buf = bytearray()
        async for chunk in self:
            buf.extend(chunk)
            if max_bytes is not None and len(buf) >= max_bytes:
                del buf[max_bytes:]
                break
        return base64.b64encode(bytes(buf)).decode("ascii")

    async def close(self) -> None:
        """Cancel the producer and await its exit. Idempotent."""
        self._closed = True
        if self._producer is not None and not self._producer.done():
            self._producer.cancel()
            with contextlib.suppress(BaseException):
                await self._producer

    @property
    def total_bytes(self) -> int:
        return self._total


async def read_upload_streaming(
    upload: Any, *,
    max_bytes: int = 25 * 1024 * 1024,
    chunk_bytes: int = CHUNK_BYTES,
) -> dict[str, Any]:
    """Stream an upload into base64 without holding the whole thing in RAM.

    Returns ``{"filename", "mimetype", "content_base64", "size"}``. Raises
    ``ValueError`` if the upload exceeds ``max_bytes``.
    """
    filename = getattr(upload, "filename", "upload.bin") or "upload.bin"
    mimetype = (getattr(upload, "content_type", "application/octet-stream")
                or "application/octet-stream")
    total = 0
    b64 = bytearray()
    async for chunk in ChunkedReader(upload, chunk_bytes=chunk_bytes):
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"upload exceeds {max_bytes} bytes")
        b64.extend(base64.b64encode(chunk))
    return {
        "filename": filename, "mimetype": mimetype,
        "content_base64": b64.decode("ascii"), "size": total,
    }


# ═══════════════════════════════════════════════════════════════════════════
# §4 — In-flight scrubber
# ═══════════════════════════════════════════════════════════════════════════
_MAX_CARRY = 512
_REFUSAL_RE = re.compile(
    r"(?i)^\s*(?:i(?:'m| am)?\s+(?:sorry|unable)|i\s+cannot|"
    r"i\s+can'?t|as an ai|i'?m just an ai)[^\n]{0,200}\n+")
_LEAK_RE = re.compile(
    r"</?(?:system_identity|core_directives|reasoning_protocol|"
    r"intent_veto|data_drift_defiance|modern_styling|thinking)>", re.I)
_GENERIC_OPENER_RE = re.compile(
    r"(?i)^\s*(?:sure[!.,]?|of course[!.,]?|certainly[!.,]?|"
    r"absolutely[!.,]?|great question[!.,]?|happy to help[!.,]?)\s+")
_MULTISPACE_RE = re.compile(r"[ \t]{3,}")
_MULTINEWLINE_RE = re.compile(r"\n{4,}")
_WATERMARK_RE = re.compile(r"\n\n---\n\*[^*]{0,200}\*\s*$")


class InFlightScrubber:
    """Stateful regex filter for streaming token deltas.

    Contract:
        * ``push(chunk)`` returns a list of strings ready to emit. May be
          empty while the internal carry buffer accumulates.
        * ``flush()`` drains the carry. Call once at stream end.
        * ``watermark_seen`` reports whether the watermark line was
          stripped during the stream.
        * If the ``[stub]`` watermark is detected, ``push`` closes the
          scrubber and returns the pre-watermark text.
        * Never raises.
    """
    __slots__ = (
        "_carry", "_emitted_any", "_aggressive", "_closed",
        "_fired_leak", "_fired_refusal", "_fired_opener", "_watermark_cut",
    )

    def __init__(self, *, aggressive: bool = True) -> None:
        self._carry = ""
        self._emitted_any = False
        self._aggressive = aggressive
        self._closed = False
        self._fired_leak = False
        self._fired_refusal = False
        self._fired_opener = False
        self._watermark_cut = False
        _bump("scrubbers_created")

    def push(self, chunk: str) -> list[str]:
        if self._closed or not chunk:
            return []
        self._carry += chunk
        out: list[str] = []
        if self._aggressive:
            m = _WATERMARK_RE.search(self._carry)
            if m:
                keep = self._carry[:m.start()]
                self._carry = ""
                if keep:
                    out.append(keep)
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
        if self._closed and not self._carry:
            return []
        tail = self._carry
        self._carry = ""
        self._closed = True
        return [tail] if tail else []

    @property
    def watermark_seen(self) -> bool:
        return self._watermark_cut


async def scrub_stream(
    upstream: AsyncIterator[str], *,
    aggressive: bool = True, queue_depth: int = 16,
) -> AsyncIterator[str]:
    """Wrap ``upstream`` through ``InFlightScrubber``. Never leaks the tail."""
    scrb = InFlightScrubber(aggressive=aggressive)
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=max(2, queue_depth))
    _END = object()

    async def _producer() -> None:
        try:
            async for tok in upstream:
                for piece in scrb.push(tok):
                    if piece:
                        await queue.put(piece)
            for piece in scrb.flush():
                if piece:
                    await queue.put(piece)
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
            if item is _END:
                return
            if isinstance(item, BaseException):
                logger.warning("scrub_upstream_error error=%s", item)
                return
            yield item
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


# ═══════════════════════════════════════════════════════════════════════════
# §5 — First-token race
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
    """Race ``race_width`` streamers; first token wins, losers are aborted.

    Contract:
        * ``race()`` yields the winner's tokens. Returns silently if no
          provider emits a first token within ``2 * stall_ms``.
        * Losing producers are always cancelled in a ``finally``.
    """
    __slots__ = ("_streamers", "_race_width", "_stall_s")

    def __init__(
        self, streamers: dict[str, StreamerFunc], *,
        race_width: int = 4, stall_ms: int = 3000,
    ) -> None:
        self._streamers = dict(streamers)
        self._race_width = max(2, int(race_width))
        self._stall_s = max(0.5, stall_ms / 1000.0)

    async def race(
        self, prompt: str, *,
        candidates: list[str] | None = None,
        model_map: dict[str, list[str]] | None = None,
        max_tokens: int = 2048, temp: float = 0.4,
    ) -> AsyncIterator[str]:
        pool = candidates or list(self._streamers.keys())
        pool = [p for p in pool if p in self._streamers][: self._race_width]
        if not pool:
            return
        model_map = model_map or {}
        winner_q: asyncio.Queue[Any] = asyncio.Queue(maxsize=256)
        losers: list[asyncio.Task[None]] = []
        winner_holder: dict[str, RaceOutcome] = {}
        first_token_event = asyncio.Event()
        winner_provider = {"name": ""}

        async def _run(provider: str, model: str) -> None:
            streamer = self._streamers[provider]
            t0 = time.monotonic()
            first_ms: float | None = None
            tokens = 0
            try:
                async for tok in streamer(
                    model=model, prompt=prompt,
                    max_tokens=max_tokens, temp=temp,
                ):
                    if not tok:
                        continue
                    if first_ms is None:
                        first_ms = (time.monotonic() - t0) * 1000.0
                        if not first_token_event.is_set():
                            winner_provider["name"] = provider
                            winner_holder["outcome"] = RaceOutcome(
                                provider=provider, model=model,
                                first_token_ms=first_ms,
                                total_ms=first_ms, tokens=0)
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
                logger.debug(
                    "race_stream_error provider=%s error=%s", provider, e)
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
                            total_ms=(time.monotonic() - t0) * 1000.0,
                            tokens=tokens)

        for p in pool:
            models = model_map.get(p) or []
            model = models[0] if models else ""
            if not model:
                continue
            losers.append(asyncio.create_task(
                _run(p, model), name=f"race::{p}"))
        if not losers:
            return
        try:
            await asyncio.wait_for(
                first_token_event.wait(), timeout=self._stall_s * 2)
        except asyncio.TimeoutError:
            for t in losers:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*losers, return_exceptions=True)
            return
        for t in losers:
            if not t.done() and t.get_name() != f"race::{winner_provider['name']}":
                t.cancel()
        try:
            while True:
                item = await winner_q.get()
                if item is None:
                    return
                yield item
        finally:
            for t in losers:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*losers, return_exceptions=True)


# ═══════════════════════════════════════════════════════════════════════════
# §6 — Cancellation
# ═══════════════════════════════════════════════════════════════════════════
_POLL_INTERVAL = 0.25


class CancellationToken:
    """Simple asyncio.Event wrapper. Set once, observe many."""
    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def set(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()


class CancelledByClient(Exception):
    pass


def bind_to_request(
    request: Any, token: CancellationToken,
) -> asyncio.Task[None] | None:
    """Watch ``request.is_disconnected()`` and set ``token`` on disconnect.

    Returns the watcher task or ``None`` if ``request`` is not a FastAPI
    ``Request`` (no ``is_disconnected``). Never raises.
    """
    is_disconnected = getattr(request, "is_disconnected", None)
    if not callable(is_disconnected):
        return None

    async def _watch() -> None:
        try:
            while not token.is_set():
                with contextlib.suppress(Exception):
                    if await is_disconnected():
                        token.set()
                        return
                await asyncio.sleep(_POLL_INTERVAL)
        except asyncio.CancelledError:
            return
    return asyncio.create_task(_watch(), name="cancel_watcher")


async def cancellable(
    agen: AsyncIterator[Any], token: CancellationToken,
) -> AsyncIterator[Any]:
    """Iterate ``agen``, stopping early if ``token`` is set. Closes ``agen``."""
    if not token.is_set():
        try:
            async for item in agen:
                yield item
                if token.is_set():
                    break
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


__all__ = [
    # PII
    "sanitize", "found_types", "PIIType", "PII_DETECTED",
    # SSE
    "iter_payloads", "stream_for_shape",
    # Chunked reader
    "CHUNK_BYTES", "MAX_INFLIGHT_CHUNKS",
    "ChunkedReader", "read_upload_streaming",
    # Scrubber
    "InFlightScrubber", "scrub_stream",
    # First-token race
    "FirstTokenRace", "RaceOutcome", "StreamerFunc",
    # Cancellation
    "CancellationToken", "CancelledByClient",
    "bind_to_request", "cancellable",
    # Audit
    "audit",
]