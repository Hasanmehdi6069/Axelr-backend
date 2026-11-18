# axelr_streaming.py
"""axelr_streaming.py — Streaming cancellation + chunked ASR + phonetic beam.

Consolidates the former axelr_cancellation.py and axelr_streaming_asr.py.

Provides:
  * CancellationToken — cooperative cancellation primitive
  * CancellationScope — context manager for cleanup
  * CompositeToken — aggregate multiple tokens into one
  * RequestDeduplicator — idempotent cache with TTL
  * StreamingPartialEmitter — debounced partial emission (~5 Hz)
  * StreamingASRChunker — overlapping chunk splitter for whisper-family models
  * PhoneticBeamDecoder — phonetic re-ranking of ASR hypotheses
  * stream_and_transcribe — end-to-end helper

Honest contract: cancellation is cooperative. The transcribe path checks
the token between inference steps, not inside the model's forward pass.
"""
from __future__ import annotations

import asyncio
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional

import numpy as np

from axelr_engine import edit_distance, soundex, metaphone
from axelr_utils import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# §1 — Cancellation primitives
# ═══════════════════════════════════════════════════════════════════════
class CancelledError(Exception):
    """Raised when a cancellation token has been signalled."""


class CancellationToken:
    """Cooperative cancellation primitive. Thread-safe. Sync + async aware."""

    __slots__ = ("_cancelled", "_event", "_callbacks", "_reason", "_lock")

    def __init__(self) -> None:
        self._cancelled = False
        self._reason = ""
        self._callbacks: List[Callable[[], None]] = []
        self._lock = threading.Lock()
        try:
            self._event: Optional[asyncio.Event] = asyncio.Event()
        except RuntimeError:
            self._event = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str:
        return self._reason

    def cancel(self, reason: str = "user") -> None:
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            self._reason = reason
        logger.info("cancel_signalled reason=%s", reason)
        for cb in self._callbacks:
            try:
                cb()
            except Exception:
                logger.exception("cancel_callback_failed")
        if self._event is not None:
            try:
                self._event.set()
            except Exception:
                pass

    def throw_if_cancelled(self) -> None:
        if self._cancelled:
            raise CancelledError(self._reason or "cancelled")

    def on_cancel(self, cb: Callable[[], None]) -> None:
        with self._lock:
            if self._cancelled:
                try: cb()
                except Exception: logger.exception("cancel_callback_immediate")
                return
            self._callbacks.append(cb)

    async def wait_cancelled(self, timeout_s: Optional[float] = None) -> bool:
        if self._cancelled:
            return True
        if self._event is None:
            self._event = asyncio.Event()
        if self._cancelled:
            self._event.set()
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout_s)
            return True
        except asyncio.TimeoutError:
            return False

    @contextmanager
    def scope(self, reason: str = "scope_exit") -> Iterator["CancellationToken"]:
        """Context manager: token auto-cancels on exception."""
        try:
            yield self
        except Exception:
            self.cancel(reason)
            raise
        finally:
            if not self._cancelled:
                self.cancel("scope_complete")


class CompositeToken:
    """Aggregates multiple CancellationTokens. Cancel-all on any trigger."""

    __slots__ = ("_tokens", "_lock")

    def __init__(self) -> None:
        self._tokens: List[CancellationToken] = []
        self._lock = threading.Lock()

    def add(self, token: CancellationToken) -> None:
        with self._lock:
            self._tokens.append(token)
            if token.cancelled:
                self.cancel(token.reason)

    @property
    def cancelled(self) -> bool:
        return any(t.cancelled for t in self._tokens)

    @property
    def reason(self) -> str:
        for t in self._tokens:
            if t.cancelled:
                return t.reason
        return ""

    def cancel(self, reason: str = "composite") -> None:
        with self._lock:
            tokens = list(self._tokens)
        for t in tokens:
            try: t.cancel(reason)
            except Exception: logger.exception("composite_cancel_child_failed")

    def throw_if_cancelled(self) -> None:
        for t in self._tokens:
            t.throw_if_cancelled()


def cancel_after(seconds: float, reason: str = "timeout") -> CancellationToken:
    """Create a token that auto-cancels after `seconds`. Daemon thread."""
    token = CancellationToken()
    def _fire() -> None:
        time.sleep(seconds)
        token.cancel(reason)
    threading.Thread(target=_fire, daemon=True).start()
    return token


# ═══════════════════════════════════════════════════════════════════════
# §2 — RequestDeduplicator (idempotency)
# ═══════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class _DedupEntry:
    ts: float
    result: Any


class RequestDeduplicator:
    """LRU-bounded idempotency cache with TTL."""
    __slots__ = ("_entries", "_cap", "_ttl", "_lock")

    def __init__(self, cap: int = 256, ttl_s: float = 60.0) -> None:
        self._entries: Dict[str, _DedupEntry] = {}
        self._cap = cap
        self._ttl = ttl_s
        self._lock = threading.Lock()

    def check(self, key: str) -> Optional[Any]:
        with self._lock:
            e = self._entries.get(key)
            if e is None:
                return None
            if time.monotonic() - e.ts > self._ttl:
                del self._entries[key]
                return None
            return e.result

    def record(self, key: str, result: Any) -> None:
        with self._lock:
            if len(self._entries) >= self._cap:
                oldest = next(iter(self._entries))
                del self._entries[oldest]
            self._entries[key] = _DedupEntry(time.monotonic(), result)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {"size": len(self._entries), "capacity": self._cap, "ttl_s": self._ttl}

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


# ═══════════════════════════════════════════════════════════════════════
# §3 — StreamingPartialEmitter
# ═══════════════════════════════════════════════════════════════════════
class StreamingPartialEmitter:
    """Emits partial transcripts (~5 Hz) during long inferences. Cancellation-aware."""
    __slots__ = ("_send", "_min_interval_s", "_last_emit_ts", "_last_text",
                 "_token", "_sent_count", "_lock")

    def __init__(
        self,
        send_fn: Callable[[Dict[str, Any]], None],
        token: Optional[CancellationToken] = None,
        min_interval_s: float = 0.2,
    ) -> None:
        self._send = send_fn
        self._min_interval_s = min_interval_s
        self._last_emit_ts = 0.0
        self._last_text = ""
        self._token = token
        self._sent_count = 0
        self._lock = threading.Lock()

    def emit(self, partial_tokens: List[str], cursor: int = 0) -> bool:
        if self._token is not None and self._token.cancelled:
            return False
        with self._lock:
            now = time.monotonic()
            if now - self._last_emit_ts < self._min_interval_s:
                return False
            text = " ".join(partial_tokens)
            if text == self._last_text:
                return False
            self._last_text = text
            self._last_emit_ts = now
            self._sent_count += 1
            try:
                self._send({"text": text, "cursor": cursor, "ts": now, "seq": self._sent_count})
                return True
            except Exception:
                logger.exception("partial_emit_failed")
                return False

    def finish(self) -> None:
        with self._lock:
            self._last_text = ""
            self._sent_count = 0

    def stats(self) -> Dict[str, Any]:
        return {"sent_count": self._sent_count, "last_text_len": len(self._last_text)}


@dataclass
class RequestContext:
    """Bundles cancellation + emitter per IPC request."""
    req_id: int
    token: CancellationToken = field(default_factory=CancellationToken)
    partial_emitter: Optional[StreamingPartialEmitter] = None

    def cancel(self, reason: str = "user") -> None:
        self.token.cancel(reason)

    def check(self) -> None:
        self.token.throw_if_cancelled()


# ═══════════════════════════════════════════════════════════════════════
# §4 — StreamingASRChunker
# ═══════════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class ChunkerConfig:
    chunk_samples: int = 16000 * 5
    overlap_samples: int = 16000 // 2
    min_chunk_samples: int = 16000 // 4


class StreamingASRChunker:
    """Splits a growing PCM stream into overlapping chunks. Frame-accurate."""
    __slots__ = ("config", "_buffer", "_emitted_up_to", "_chunk_index")

    def __init__(self, config: Optional[ChunkerConfig] = None) -> None:
        self.config = config or ChunkerConfig()
        self._buffer = np.empty(0, dtype=np.int16)
        self._emitted_up_to = 0
        self._chunk_index = 0

    def feed(self, samples: np.ndarray) -> List[np.ndarray]:
        if samples.dtype != np.int16:
            samples = samples.astype(np.int16)
        if samples.size == 0:
            return []
        self._buffer = np.concatenate([self._buffer, samples]) if self._buffer.size else samples
        return self._drain_ready()

    def _drain_ready(self) -> List[np.ndarray]:
        out: List[np.ndarray] = []
        cfg = self.config
        while True:
            available = self._buffer.size - self._emitted_up_to
            if available < cfg.chunk_samples:
                break
            chunk = self._buffer[self._emitted_up_to : self._emitted_up_to + cfg.chunk_samples]
            out.append(chunk.copy())
            self._emitted_up_to += cfg.chunk_samples - cfg.overlap_samples
            self._chunk_index += 1
        return out

    def flush(self) -> List[np.ndarray]:
        cfg = self.config
        available = self._buffer.size - self._emitted_up_to
        if available < cfg.min_chunk_samples:
            return []
        chunk = self._buffer[self._emitted_up_to :].copy()
        self._emitted_up_to = self._buffer.size
        self._chunk_index += 1
        return [chunk]

    def reset(self) -> None:
        self._buffer = np.empty(0, dtype=np.int16)
        self._emitted_up_to = 0
        self._chunk_index = 0

    def stats(self) -> Dict[str, Any]:
        return {
            "buffer_samples": int(self._buffer.size),
            "emitted_up_to": int(self._emitted_up_to),
            "chunk_index": self._chunk_index,
            "chunk_seconds": self.config.chunk_samples / 16000,
            "overlap_seconds": self.config.overlap_samples / 16000,
        }


# ═══════════════════════════════════════════════════════════════════════
# §5 — PhoneticBeamDecoder
# ═══════════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class BeamConfig:
    beam_width: int = 5
    phonetic_weight: float = 0.4
    edit_weight: float = 0.3
    common_word_bonus: float = 0.2


_GLOBAL_COMMON = frozenset({
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were",
    "be", "been", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "must", "can", "i",
    "you", "he", "she", "it", "we", "they", "me", "him", "her", "us",
    "them", "my", "your", "his", "its", "our", "their", "this", "that",
    "these", "those", "to", "of", "in", "on", "at", "for", "with", "by",
    "from", "as", "into", "about", "like", "not", "no", "yes", "so",
    "if", "then", "than", "there", "here", "they're", "you're", "we're",
    "it's", "receive", "believe", "achieve", "separate", "occurred",
    "definitely", "their", "there", "they're", "your", "you're",
    "to", "too", "two",
})


class PhoneticBeamDecoder:
    """Re-ranks ASR token hypotheses by phonetic + edit-distance scoring."""
    __slots__ = ("config", "_cache")

    def __init__(self, config: Optional[BeamConfig] = None) -> None:
        self.config = config or BeamConfig()
        self._cache: Dict[str, str] = {}

    def _soundex(self, w: str) -> str:
        key = "s:" + w
        if key not in self._cache:
            self._cache[key] = soundex(w)
        return self._cache[key]

    def _metaphone(self, w: str) -> str:
        key = "m:" + w
        if key not in self._cache:
            self._cache[key] = metaphone(w)
        return self._cache[key]

    def score(self, hypothesis: str, candidate: str) -> float:
        h_lo, c_lo = hypothesis.lower(), candidate.lower()
        if h_lo == c_lo:
            return 1.0
        score = 0.0
        if self._soundex(h_lo) == self._soundex(c_lo):
            score += 2.5 * self.config.phonetic_weight
        if self._metaphone(h_lo) == self._metaphone(c_lo):
            score += 2.0 * self.config.phonetic_weight
        ed = edit_distance(h_lo, c_lo)
        score -= ed * self.config.edit_weight
        if c_lo in _GLOBAL_COMMON:
            score += self.config.common_word_bonus
        return score

    def decode(self, hypothesis: str, candidates: List[str]) -> List[str]:
        if not candidates:
            return []
        scored = [(c, self.score(hypothesis, c)) for c in candidates]
        scored.sort(key=lambda kv: kv[1], reverse=True)
        return [c for c, _ in scored[: self.config.beam_width]]

    def rerank_stream(self, tokens: List[str], lexicon: List[str]) -> List[str]:
        if not lexicon:
            return list(tokens)
        out: List[str] = []
        for tok in tokens:
            ranked = self.decode(tok, lexicon)
            out.append(ranked[0] if ranked else tok)
        return out

    def clear_cache(self) -> None:
        self._cache.clear()

    def stats(self) -> Dict[str, Any]:
        return {"cache_size": len(self._cache), "beam_width": self.config.beam_width}


# ═══════════════════════════════════════════════════════════════════════
# §6 — stream_and_transcribe — end-to-end helper
# ═══════════════════════════════════════════════════════════════════════
async def stream_and_transcribe(
    chunker: StreamingASRChunker,
    adapter,
    on_final: Callable[[str], None],
    on_partial: Optional[Callable[[str, int], None]] = None,
    token: Optional[CancellationToken] = None,
    pcm_feed: Optional[np.ndarray] = None,
) -> Dict[str, Any]:
    """Transcribe a whole stream; emits partials between chunks."""
    results: List[str] = []
    chunk_count = 0
    cancel = token or CancellationToken()

    def emit_partial(text: str, cursor: int) -> None:
        if on_partial:
            try:
                on_partial(text, cursor)
            except Exception:
                logger.exception("on_partial_failed")

    async def run_chunk(pcm: np.ndarray) -> None:
        nonlocal chunk_count
        cancel.throw_if_cancelled()
        if not hasattr(adapter, "transcribe_raw"):
            return
        tokens = await asyncio.to_thread(adapter.transcribe_raw, pcm)
        chunk_count += 1
        text = " ".join(t.text for t in tokens).strip()
        if text:
            results.append(text)
            emit_partial(text, len(results))

    if pcm_feed is not None:
        for chunk in chunker.feed(pcm_feed):
            await run_chunk(chunk)
    for chunk in chunker.flush():
        await run_chunk(chunk)

    final_text = " ".join(results).strip()
    try:
        on_final(final_text)
    except Exception:
        logger.exception("on_final_failed")
    return {"text": final_text, "chunks": chunk_count, "cancelled": cancel.cancelled}


__all__ = [
    "CancelledError", "CancellationToken", "CompositeToken", "cancel_after",
    "RequestDeduplicator", "StreamingPartialEmitter", "RequestContext",
    "ChunkerConfig", "StreamingASRChunker",
    "BeamConfig", "PhoneticBeamDecoder", "stream_and_transcribe",
]