# core/ux_input.py
"""
AXELR — Unified UX Input Primitives
=====================================
Seven low-level components, one cohesive module. Each class is
self-contained; none imports another. They share only the module
import surface.

    §1 TextRingBuffer        — Bounded ring buffer for streaming text
    §2 LexicalTrie           — Immutable prefix tree (commands)
    §3 ContextualTrie        — Mutable bounded trie (viewport tokens)
    §4 PolyfillCore          — Micro prediction state machine
    §5 IntentParser          — Verbal correction / stack rollback
    §6 HomophoneCorrector    — Context-free homophone validator
    §7 UXComponents          — Velocity + shadow-typing aggregates

Memory ceilings (per component):
    TextRingBuffer      ≤ 1 MB
    LexicalTrie         ≤ 1 MB
    ContextualTrie      ≤ 0.5 MB
    PolyfillCore        ≤ 1.5 MB
    IntentParser        ≤ 0.5 MB
    HomophoneCorrector  ≤ 1 MB
    UXComponents        ≤ 2 MB
"""
from __future__ import annotations

import re
import sys
import time
from collections import defaultdict, deque
from typing import Optional

__all__ = [
    "TextRingBuffer", "LexicalTrie", "ContextualTrie",
    "PolyfillCore", "IntentParser", "HomophoneCorrector", "UXComponents",
]


# ═══════════════════════════════════════════════════════════════════════════
# §1 — TextRingBuffer
# ═══════════════════════════════════════════════════════════════════════════
class TextRingBuffer:
    """Bounded UTF-8 ring buffer preserving streaming text across context switches."""
    __slots__ = ("_buffer", "_current_size_bytes", "max_size_bytes")

    def __init__(self, max_size_bytes: int = 1 * 1024 * 1024) -> None:
        self._buffer: deque[tuple[str, int]] = deque()
        self._current_size_bytes = 0
        self.max_size_bytes = max_size_bytes

    def append(self, text: str) -> None:
        tb = text.encode("utf-8")
        size = sys.getsizeof(tb)
        while self._current_size_bytes + size > self.max_size_bytes and self._buffer:
            _, removed = self._buffer.popleft()
            self._current_size_bytes -= removed
        if self._current_size_bytes + size <= self.max_size_bytes:
            self._buffer.append((text, size))
            self._current_size_bytes += size

    def get_content(self) -> str:
        return "".join(t for t, _ in self._buffer)

    def clear(self) -> None:
        self._buffer.clear()
        self._current_size_bytes = 0

    def __len__(self) -> int:
        return len(self._buffer)

    def current_memory_usage(self) -> int:
        return self._current_size_bytes


# ═══════════════════════════════════════════════════════════════════════════
# §2 — LexicalTrie (immutable command map)
# ═══════════════════════════════════════════════════════════════════════════
class LexicalTrie:
    """Immutable, pre-built trie mapping command phrases → endpoints."""
    __slots__ = ("_trie",)

    def __init__(self, commands: dict | None = None) -> None:
        self._trie: dict = {}
        if commands:
            for phrase, endpoint in commands.items():
                node = self._trie
                for ch in phrase:
                    node = node.setdefault(ch, {})
                node["#"] = endpoint

    def find_command(self, text: str) -> Optional[str]:
        node = self._trie
        longest: Optional[str] = None
        for i, ch in enumerate(text):
            if ch not in node:
                break
            node = node[ch]
            if "#" in node:
                longest = node["#"]
        return longest


# ═══════════════════════════════════════════════════════════════════════════
# §3 — ContextualTrie (mutable, bounded)
# ═══════════════════════════════════════════════════════════════════════════
class ContextualTrie:
    """Mutable bounded trie for viewport-scraped tokens."""
    __slots__ = ("_trie", "_current_size_bytes", "max_size_bytes", "_word_count")

    _NODE_OVERHEAD = sys.getsizeof({}) + sys.getsizeof("a") + sys.getsizeof(True)

    def __init__(self, max_size_bytes: int = 500 * 1024) -> None:
        self._trie: dict = {}
        self._current_size_bytes = sys.getsizeof(self._trie)
        self.max_size_bytes = max_size_bytes
        self._word_count = 0

    def add_word(self, word: str) -> None:
        if not word:
            return
        word = word.lower()
        # estimate incremental cost
        node = self._trie
        new_nodes = 0
        for ch in word:
            if ch not in node:
                new_nodes += 1
                node[ch] = {}
            node = node[ch]
        if "#" not in node:
            new_nodes += 1

        estimated_increase = new_nodes * self._NODE_OVERHEAD
        if self._current_size_bytes + estimated_increase > self.max_size_bytes:
            return

        node = self._trie
        for ch in word:
            node = node.setdefault(ch, {})
        node["#"] = True
        self._current_size_bytes += estimated_increase
        self._word_count += 1

    def check_word_existence(self, word: str) -> bool:
        node = self._trie
        for ch in word.lower():
            if ch not in node:
                return False
            node = node[ch]
        return "#" in node

    def clear(self) -> None:
        self._trie = {}
        self._current_size_bytes = sys.getsizeof(self._trie)
        self._word_count = 0

    def current_memory_usage(self) -> int:
        return self._current_size_bytes

    def __len__(self) -> int:
        return self._word_count


# ═══════════════════════════════════════════════════════════════════════════
# §4 — PolyfillCore (micro prediction state machine)
# ═══════════════════════════════════════════════════════════════════════════
class PolyfillCore:
    """Fallback n-gram predictor for connection jitter / OS lag."""
    __slots__ = ("max_total_memory_bytes", "_recent_words", "_model",
                 "_is_active", "_last_input_time", "_lag_threshold_ms",
                 "_current_memory_usage_bytes")

    _RECENT_BUFFER_SIZE = 20
    _MODEL_MAX_BYTES = 100 * 1024

    def __init__(self, max_total_memory_bytes: int = 1_500_000) -> None:
        self.max_total_memory_bytes = max_total_memory_bytes
        self._recent_words: deque[str] = deque(maxlen=self._RECENT_BUFFER_SIZE)
        self._model: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._is_active = False
        self._last_input_time = time.perf_counter()
        self._lag_threshold_ms = 50
        self._current_memory_usage_bytes = sys.getsizeof(self._recent_words)

    def _update_model(self, word: str, next_word: str) -> None:
        if self._current_memory_usage_bytes < self._MODEL_MAX_BYTES:
            self._model[word][next_word] += 1

    def process_input(self, word: str) -> None:
        now = time.perf_counter()
        lag_ms = (now - self._last_input_time) * 1000
        self._is_active = lag_ms > self._lag_threshold_ms
        if self._recent_words:
            self._update_model(self._recent_words[-1], word)
        self._recent_words.append(word)
        self._last_input_time = now

    def predict_next_token(self) -> Optional[str]:
        if not self._is_active or not self._recent_words:
            return None
        nxt = self._model.get(self._recent_words[-1])
        return max(nxt, key=nxt.get) if nxt else None

    def is_fallback_active(self) -> bool:
        return self._is_active

    def clear(self) -> None:
        self._recent_words.clear()
        self._model.clear()

    def current_memory_usage(self) -> int:
        return self._current_memory_usage_bytes


# ═══════════════════════════════════════════════════════════════════════════
# §5 — IntentParser (verbal corrections)
# ═══════════════════════════════════════════════════════════════════════════
class IntentParser:
    """Streaming parser for verbal corrections with stack rollback."""
    __slots__ = ("_token_buffer", "_current_size_bytes", "max_buffer_size_bytes")

    _PATTERNS = {
        "scratch_that": re.compile(r"\b(wait\s+)?scratch\s+that\b", re.IGNORECASE),
        "no_wait": re.compile(r"\bno\s+wait\s+tell\s+him\b", re.IGNORECASE),
        "delete_last_word": re.compile(r"\bdelete\s+the\s+last\s+word\b", re.IGNORECASE),
        "change_that_to": re.compile(r"\bactually\s+change\s+that\s+to\b", re.IGNORECASE),
    }
    _TOKENIZER = re.compile(r"(\b\w+\b|[.,!?;:\"'\s]+)")

    def __init__(self, max_buffer_size_bytes: int = 500 * 1024) -> None:
        self._token_buffer: deque[tuple[str, int]] = deque()
        self._current_size_bytes = 0
        self.max_buffer_size_bytes = max_buffer_size_bytes

    def _add(self, chunk: str) -> None:
        for tok in self._TOKENIZER.findall(chunk):
            size = sys.getsizeof(tok.encode("utf-8"))
            while (self._current_size_bytes + size > self.max_buffer_size_bytes
                   and self._token_buffer):
                _, removed = self._token_buffer.popleft()
                self._current_size_bytes -= removed
            if self._current_size_bytes + size <= self.max_buffer_size_bytes:
                self._token_buffer.append((tok, size))
                self._current_size_bytes += size

    def _get_text(self) -> str:
        return "".join(t for t, _ in self._token_buffer)

    def _remove_last_words(self, count: int) -> None:
        temp: deque[tuple[str, int]] = deque()
        removed = 0
        while self._token_buffer:
            tok, size = self._token_buffer.pop()
            if tok.strip() and removed < count:
                self._current_size_bytes -= size
                removed += 1
            else:
                temp.appendleft((tok, size))
        self._token_buffer = temp

    def parse_and_correct(self, chunk: str) -> str:
        self._add(chunk)
        text = self._get_text()
        for name, rx in self._PATTERNS.items():
            m = list(rx.finditer(text))
            if not m:
                continue
            prefix = text[:m[-1].start()]
            self.clear()
            self._add(prefix)
            if name == "scratch_that":
                return ""
            if name == "no_wait":
                self._remove_last_words(3)
            elif name == "delete_last_word":
                self._remove_last_words(1)
            elif name == "change_that_to":
                self._remove_last_words(2)
            return self.get_processed_text()
        return text

    def get_processed_text(self) -> str:
        return self._get_text().strip()

    def clear(self) -> None:
        self._token_buffer.clear()
        self._current_size_bytes = 0

    def current_memory_usage(self) -> int:
        return self._current_size_bytes


# ═══════════════════════════════════════════════════════════════════════════
# §6 — HomophoneCorrector
# ═══════════════════════════════════════════════════════════════════════════
class HomophoneCorrector:
    """Context-keyed homophone resolver. Pure dict lookup, no LLM."""
    __slots__ = ()

    _HOMOPHONES = {
        "their": {"group": "tri", "context_after": ["team", "house", "car"]},
        "there": {"group": "tri", "context_after": ["is", "are", "was", "were"]},
        "they're": {"group": "tri", "context_after": ["going", "eating", "running"]},
        "to": {"group": "ttt", "context_after": ["go", "eat", "run"]},
        "too": {"group": "ttt", "context_after": ["much", "little", "bad"]},
        "two": {"group": "ttt", "context_after": ["apples", "cars", "people"]},
        "your": {"group": "yy", "context_after": ["book", "idea", "turn"]},
        "you're": {"group": "yy", "context_after": ["happy", "sad", "tired"]},
        "its": {"group": "ii", "context_after": ["color", "size", "shape"]},
        "it's": {"group": "ii", "context_after": ["a", "an", "the"]},
    }
    _GROUPS = {
        "tri": ["their", "there", "they're"],
        "ttt": ["to", "too", "two"],
        "yy": ["your", "you're"],
        "ii": ["its", "it's"],
    }

    def correct_homophones(self, text: str) -> str:
        words = text.lower().split()
        out = list(words)
        for i, w in enumerate(words):
            info = self._HOMOPHONES.get(w)
            if not info:
                continue
            nxt = words[i + 1] if i + 1 < len(words) else None
            for candidate in self._GROUPS.get(info["group"], []):
                cinfo = self._HOMOPHONES.get(candidate)
                if cinfo and nxt in cinfo.get("context_after", []):
                    out[i] = candidate
                    break
        return " ".join(out)


# ═══════════════════════════════════════════════════════════════════════════
# §7 — UXComponents (velocity + shadow typing)
# ═══════════════════════════════════════════════════════════════════════════
class UXComponents:
    """Predictive shadow typing + dynamic velocity adaptation."""
    __slots__ = ("max_total_memory_bytes", "_command_history",
                 "_keystroke_timestamps", "_word_count_in_window",
                 "_current_wpm", "_current_memory_usage_bytes")

    _MAX_COMMAND_HISTORY = 50
    _WPM_WINDOW_S = 5
    _HIGH_TOLERANCE_WPM = 100

    def __init__(self, max_total_memory_bytes: int = 2 * 1024 * 1024) -> None:
        self.max_total_memory_bytes = max_total_memory_bytes
        self._command_history: deque[str] = deque(maxlen=self._MAX_COMMAND_HISTORY)
        self._keystroke_timestamps: deque[float] = deque()
        self._word_count_in_window = 0
        self._current_wpm = 0.0
        self._current_memory_usage_bytes = (
            sys.getsizeof(self._command_history)
            + sys.getsizeof(self._keystroke_timestamps)
        )

    def record_command_action(self, command: str) -> None:
        if command not in self._command_history:
            self._command_history.append(command)

    def get_suggestions(self, prefix: str) -> list[str]:
        if not prefix:
            return []
        return [c for c in self._command_history if c.startswith(prefix)]

    def record_keystroke(self, is_word_boundary: bool = False) -> None:
        now = time.perf_counter()
        self._keystroke_timestamps.append(now)
        if is_word_boundary:
            self._word_count_in_window += 1
        while (self._keystroke_timestamps
               and self._keystroke_timestamps[0] < now - self._WPM_WINDOW_S):
            self._keystroke_timestamps.popleft()
        if len(self._keystroke_timestamps) > 1:
            elapsed = self._keystroke_timestamps[-1] - self._keystroke_timestamps[0]
            if elapsed > 0:
                self._current_wpm = (len(self._keystroke_timestamps) / 6) / elapsed * 60
            else:
                self._current_wpm = 0.0
        else:
            self._current_wpm = 0.0

    def get_current_wpm(self) -> float:
        return self._current_wpm

    def get_correction_mode(self) -> bool:
        return self._current_wpm > self._HIGH_TOLERANCE_WPM

    def clear(self) -> None:
        self._command_history.clear()
        self._keystroke_timestamps.clear()
        self._word_count_in_window = 0
        self._current_wpm = 0.0

    def current_memory_usage(self) -> int:
        return self._current_memory_usage_bytes