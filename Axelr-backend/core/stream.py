# core/stream.py
"""
AXELR — Streaming Primitives (pipe · scrub · race · cancellation)
=================================================================
Four streaming concerns, one file. Every one holds a bounded queue,
a carry buffer, or a cancel token — and every one must never leak on
client disconnect.

    §1 Chunked reader    bounded intake from any async byte source
    §2 In-flight scrubber chunk-by-chunk regex state machine
    §3 First-token race  race N streamers, first valid token wins
    §4 Cancellation      propagate client disconnect to upstream tasks
"""
from __future__ import annotations

import asyncio
import codecs
import contextlib
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("axelr.stream")

CHUNK_BYTES = 4096
MAX_INFLIGHT_CHUNKS = 32
_SENTINEL: object = object()


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Chunked reader (back-pressured intake)
# ═══════════════════════════════════════════════════════════════════════════
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
    """Bounded async byte reader. One producer task, one bounded queue."""

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
    """Stateful regex filter for streaming tokens. Bounded carry buffer."""

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


__all__ = [
    # pipe
    "CHUNK_BYTES", "MAX_INFLIGHT_CHUNKS", "ChunkedReader", "read_upload_streaming",
    # scrub
    "InFlightScrubber", "scrub_stream",
    # race
    "FirstTokenRace", "RaceOutcome", "StreamerFunc",
    # cancel
    "CancellationToken", "CancelledByClient", "bind_to_request", "cancellable",
]