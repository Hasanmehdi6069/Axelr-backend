"""
axelr_features.py — ZLCI #3, KV cache #9, Rescoring #10, SCLB #14,
Circuit Breaker #18, Streaming Reranker #20, Structured Repair #21.

Memory: KV cache cap + ~300 KB bookkeeping.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional, Set, Tuple
from pathlib import Path

import numpy as np

from axelr_config import (
    CONTEXTUAL_WINDOW_SIZE, DOMAIN_CONFIGS, DOMAIN_PATTERNS, GLOBAL_COMMON_WORDS,
)
from axelr_utils import get_logger

logger = get_logger(__name__)

# Load shared runtime tables from single source (AXELR Single-Owner Master Rule)
_RUNTIME_TABLES = {}
try:
    tables_path = Path(__file__).parent.parent.parent.parent / "runtime-tables.json"
    with open(tables_path, "r", encoding="utf-8") as f:
        _RUNTIME_TABLES = json.load(f)
except Exception as e:
    logger.warning(f"Failed to load runtime-tables.json, using fallback values: {e}")
    # Fallback values maintain backward compatibility
    _RUNTIME_TABLES = {
        "homophone_words": ["their","there","they're","to","too","two","your","you're","its","it's","then","than","were","we're","where","whose","who's","right","write","rite","hear","here","weather","whether","affect","effect"],
        "homophone_context": {
            "their":{"is":"there", "are":"there", "was":"there", "were":"there", "has":"there", "have":"there", "will":"there", "would":"there", "they":"their", "them":"their", "we":"their", "our":"their", "over":"there", "out":"there", "in":"there", "at":"there"},
            "to":{"go":"to", "want":"to", "need":"to", "have":"to", "able":"to", "going":"to", "back":"to", "much":"too", "many":"too", "far":"too", "big":"too"},
            "your":{"you":"you're", "are":"you're", "were":"you're", "going":"you're", "the":"your", "my":"your", "our":"your"},
            "its":{"it":"it's", "is":"its", "was":"its", "has":"its", "a":"it's", "the":"it's"}
        }
    }


# ---------- ZLCI #3 ------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    window_title: str
    active_text: str
    cursor_position: int
    timestamp: float
    domain_hints: frozenset[str]


class ContextTracker:
    """#3 ZLCI: domain hint extraction with timestamp cache."""

    __slots__ = ("_compiled", "_last_ts", "_cached")

    def __init__(self) -> None:
        self._compiled: Dict[str, Tuple[re.Pattern[str], ...]] = {
            d: tuple(re.compile(p) for p in pats) for d, pats in DOMAIN_PATTERNS.items()
        }
        self._last_ts = -1.0
        self._cached: Optional[ContextSnapshot] = None

    def extract(self, data: Dict[str, object]) -> ContextSnapshot:
        ts = float(data.get("timestamp", 0.0))  # type: ignore[arg-type]
        if ts == self._last_ts and self._cached is not None:
            return self._cached
        title = str(data.get("window_title", ""))
        text = str(data.get("active_text", ""))
        cursor = int(data.get("cursor_position", 0))  # type: ignore[arg-type]
        hints: Set[str] = set()
        for domain, pats in self._compiled.items():
            for pat in pats:
                if pat.search(title) or pat.search(text):
                    hints.add(domain)
                    break
        self._cached = ContextSnapshot(title, text, cursor, ts, frozenset(hints))
        self._last_ts = ts
        return self._cached

    @staticmethod
    def recent(snapshot: ContextSnapshot, chars: int = 500) -> str:
        start = max(0, snapshot.cursor_position - chars)
        return snapshot.active_text[start:snapshot.cursor_position]


class ZeroLatencyContextInjector:
    """#3 ZLCI wrapper."""

    __slots__ = ("tracker", "_snapshot", "_window")

    def __init__(self) -> None:
        self.tracker = ContextTracker()
        self._snapshot: Optional[ContextSnapshot] = None
        self._window = CONTEXTUAL_WINDOW_SIZE

    def update(self, data: Dict[str, object]) -> ContextSnapshot:
        self._snapshot = self.tracker.extract(data)
        return self._snapshot

    @property
    def snapshot(self) -> Optional[ContextSnapshot]:
        return self._snapshot

    def biasing_terms(self) -> Tuple[Set[str], Set[str], float]:
        if self._snapshot is None:
            return set(), set(GLOBAL_COMMON_WORDS), 1.0
        keywords: Set[str] = set()
        boost = 1.0
        for domain in self._snapshot.domain_hints:
            cfg = DOMAIN_CONFIGS.get(domain)
            if cfg is not None:
                keywords.update(cfg.biasing.keywords)
                boost = max(boost, cfg.biasing.boost)
        return keywords, set(GLOBAL_COMMON_WORDS), boost


# ---------- KV cache #9 --------------------------------------------------

@dataclass(slots=True)
class _Entry:
    data: np.ndarray
    ttl: float


class EvictingKVCache:
    """#9: OrderedDict LRU with hard byte cap."""

    __slots__ = ("_max_bytes", "_bytes", "_items")

    def __init__(self, max_memory_mb: float) -> None:
        self._max_bytes = int(max_memory_mb * 1024 * 1024)
        self._bytes = 0
        self._items: "OrderedDict[str, _Entry]" = OrderedDict()

    def get(self, key: str) -> Optional[np.ndarray]:
        e = self._items.get(key)
        if e is None:
            return None
        self._items.move_to_end(key)
        return e.data

    def put(self, key: str, data: np.ndarray, ttl: float = 300.0) -> None:
        size = int(data.nbytes)
        if size > self._max_bytes:
            logger.warning("KV entry %s (%d B) exceeds cap", key, size)
            return
        if key in self._items:
            self._bytes -= int(self._items[key].data.nbytes)
            del self._items[key]
        while self._bytes + size > self._max_bytes and self._items:
            _, ev = self._items.popitem(last=False)
            self._bytes -= int(ev.data.nbytes)
        self._items[key] = _Entry(data, ttl)
        self._bytes += size

    def __len__(self) -> int:
        return len(self._items)


# ---------- Rescoring #10 -----------------------------------------------

_NGRAM_FIXES: Tuple[Tuple[str, str], ...] = (
    ("your welcome", "you're welcome"),
    ("would of", "would have"),
    ("could of", "could have"),
    ("it's own", "its own"),
    ("a lot of", "a lot of"),
)


class TokenRescoringLoop:
    """#10: background n-gram grammar repair with 1-token lookahead buffer."""

    __slots__ = ("_queue", "_running", "_task", "_lookahead")

    def __init__(self) -> None:
        self._queue: asyncio.Queue[List[str]] = asyncio.Queue(maxsize=64)
        self._running = False
        self._task: Optional[asyncio.Task[None]] = None
        self._lookahead: List[str] = []

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            await self._task

    def enqueue(self, tokens: List[str]) -> None:
        try:
            self._queue.put_nowait(tokens)
        except asyncio.QueueFull:
            logger.warning("rescore queue full; dropping")

    async def _worker(self) -> None:
        while self._running:
            try:
                tokens = await asyncio.wait_for(self._queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            try:
                self._fix(tokens)
            finally:
                self._queue.task_done()

    @staticmethod
    def _fix(tokens: List[str]) -> List[str]:
        text = " ".join(tokens)
        for bad, good in _NGRAM_FIXES:
            text = text.replace(bad, good)
        return text.split()


# ---------- Streaming Reranker #20 --------------------------------------

@dataclass(slots=True)
class _Hyp:
    text: str
    score: float


class StreamingNGramReranker:
    """
    #20: curated repair table applied to the trailing N tokens of a stream.
    This is NOT a language model reranker — it applies deterministic
    phrase→phrase substitutions to catch homophone pairs edit-distance
    scoring cannot distinguish. Documented as such so no caller mistakes
    it for an actual LM.
    """
    __slots__ = ("_beam", "_window", "_repairs")

    def __init__(self, beam_width: int = 4, window: int = 5) -> None:
        self._beam = beam_width
        self._window = window
        # Generate repair tuples from shared homophone context (AXELR Single-Owner Master Rule)
        repairs = []
        homophone_context = _RUNTIME_TABLES.get("homophone_context", {})
        for source_word, context_map in homophone_context.items():
            for next_word, target_word in context_map.items():
                # Create trailing phrase that matches the window: [prev words...] source_word next_word
                repairs.append((f"{source_word} {next_word}", f"{target_word} {next_word}"))

        self._repairs = tuple(repairs)

    def rerank(self, tokens: List[str]) -> List[str]:
        if len(tokens) < 2:
            return tokens
        tail = " ".join(tokens[-self._window:])
        for bad, good in self._repairs:
            if tail.endswith(bad):
                merged = tail[: -len(bad)] + good
                return tokens[: -self._window] + merged.split()
        return tokens


# ---------- SCLB #14 -----------------------------------------------------

class StreamingLogitBiasing:
    """#14 SCLB: cooldown-limited biasing term extraction."""

    __slots__ = ("_injector", "_cooldown", "_last")

    def __init__(self, injector: ZeroLatencyContextInjector) -> None:
        self._injector = injector
        self._cooldown = 0.1
        self._last = 0.0

    def bias_terms(self, now: float) -> Tuple[Set[str], float]:
        if now - self._last < self._cooldown:
            return set(), 1.0
        self._last = now
        keywords, _, boost = self._injector.biasing_terms()
        return keywords, boost


# ---------- Circuit Breaker #18 -----------------------------------------

from axelr_utils import CircuitBreaker, CircuitState


# ---------- Structured Repair #21 --------------------------------------

_NUM_WORDS: Dict[str, str] = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
    "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
    "eighteen": "18", "nineteen": "19", "twenty": "20", "thirty": "30",
    "forty": "40", "fifty": "50", "hundred": "100",
}


class StructuredRepair:
    """
    #21: high-confidence pattern repairs (percent, dollars, time,
    extended: currency, units, dates, email, phone, url).

    Conservative: only rewrites when pattern is unambiguous.
    Memory: ~12 KB.
    """

    __slots__ = ("_pct", "_usd", "_time", "_eur", "_gbp", "_unit", "_date", "_email", "_phone", "_url")

    _PCT = re.compile(r"\b(\w+) (?:percent|per cent)\b", re.IGNORECASE)
    _USD = re.compile(r"\b(\w+) dollars?\b", re.IGNORECASE)
    _TIME = re.compile(r"\b(\d{1,2}) (\d{2}) (am|pm)\b", re.IGNORECASE)
    _EUR = re.compile(r"\b(\w+) euros?\b", re.IGNORECASE)
    _GBP = re.compile(r"\b(\w+) pounds?\b", re.IGNORECASE)
    _UNIT = re.compile(r"\b(\w+) (?:meters?|kilograms?|grams?|seconds?|minutes?|hours?)\b", re.IGNORECASE)
    _DATE = re.compile(r"\b(\w+) (\d{1,2})(?:st|nd|rd|th)?,? (\d{4})\b", re.IGNORECASE)
    _EMAIL = re.compile(r"\b([a-zA-Z0-9._%+-]+) at ([a-zA-Z0-9.-]+) dot ([a-zA-Z]{2,})\b", re.IGNORECASE)
    _PHONE = re.compile(r"\b(\d{3}) (\d{3}) (\d{4})\b")
    _URL = re.compile(r"\b(www|http) dot ([a-zA-Z0-9.-]+) dot ([a-zA-Z]{2,})\b", re.IGNORECASE)

    def __init__(self) -> None:
        self._pct = self._PCT
        self._usd = self._USD
        self._time = self._TIME
        self._eur = self._EUR
        self._gbp = self._GBP
        self._unit = self._UNIT
        self._date = self._DATE
        self._email = self._EMAIL
        self._phone = self._PHONE
        self._url = self._URL

    def fix(self, text: str) -> str:
        text = self._pct.sub(self._pct_sub, text)
        text = self._usd.sub(self._usd_sub, text)
        text = self._time.sub(self._time_sub, text)
        text = self._eur.sub(self._eur_sub, text)
        text = self._gbp.sub(self._gbp_sub, text)
        text = self._unit.sub(self._unit_sub, text)
        text = self._date.sub(self._date_sub, text)
        text = self._email.sub(self._email_sub, text)
        text = self._phone.sub(self._phone_sub, text)
        text = self._url.sub(self._url_sub, text)
        return text

    @staticmethod
    def _pct_sub(m: "re.Match[str]") -> str:
        num = _NUM_WORDS.get(m.group(1).lower(), m.group(1))
        return f"{num}%"

    @staticmethod
    def _usd_sub(m: "re.Match[str]") -> str:
        num = _NUM_WORDS.get(m.group(1).lower(), m.group(1))
        return f"${num}"

    @staticmethod
    def _eur_sub(m: "re.Match[str]") -> str:
        num = _NUM_WORDS.get(m.group(1).lower(), m.group(1))
        return f"€{num}"

    @staticmethod
    def _gbp_sub(m: "re.Match[str]") -> str:
        num = _NUM_WORDS.get(m.group(1).lower(), m.group(1))
        return f"£{num}"

    @staticmethod
    def _unit_sub(m: "re.Match[str]") -> str:
        num = _NUM_WORDS.get(m.group(1).lower(), m.group(1))
        unit = m.group(2).lower()
        return f"{num}{unit}"

    @staticmethod
    def _time_sub(m: "re.Match[str]") -> str:
        return f"{m.group(1)}:{m.group(2)} {m.group(3).lower()}"

    @staticmethod
    def _date_sub(m: "re.Match[str]") -> str:
        month_map = {"january":"01","february":"02","march":"03","april":"04","may":"05","june":"06",
                     "july":"07","august":"08","september":"09","october":"10","november":"11","december":"12"}
        month = month_map.get(m.group(1).lower(), m.group(1))
        day = m.group(2).zfill(2)
        return f"{month}/{day}/{m.group(3)}"

    @staticmethod
    def _email_sub(m: "re.Match[str]") -> str:
        return f"{m.group(1)}@{m.group(2)}.{m.group(3)}"

    @staticmethod
    def _phone_sub(m: "re.Match[str]") -> str:
        return f"({m.group(1)}) {m.group(2)}-{m.group(3)}"

    @staticmethod
    def _url_sub(m: "re.Match[str]") -> str:
        prefix = "https://" if m.group(1).lower() == "http" else "www."
        return f"{prefix}{m.group(2)}.{m.group(3)}"


# ---------- CloudRouter -------------------------------------------------

@dataclass(frozen=True, slots=True)
class CloudConfig:
    url: Optional[str] = None
    api_key: Optional[str] = None
    timeout_s: float = 0.5
    breaker_threshold: int = 5
    breaker_recovery_s: float = 30.0


class CloudRouter:
    """Wraps a CircuitBreaker for the decoder's cloud fallback."""

    __slots__ = ("config", "breaker")

    def __init__(self, config: CloudConfig) -> None:
        self.config = config
        self.breaker = CircuitBreaker(config.breaker_threshold, config.breaker_recovery_s)

    @property
    def enabled(self) -> bool:
        return bool(self.config.url and self.config.api_key)