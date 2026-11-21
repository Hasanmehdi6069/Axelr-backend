# axelr_engine.py
"""
axelr_engine.py — SCD #1, SGO #7, PEDA #6, TFLP #13, LPGN #15,
Confidence Calibrator #16, Phonetic Ensemble #19.

Memory: ~4 MB (trie 1.5 MB + phonetic indices 2 MB + calibrator negligible).
"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

import numpy as np

try:
    import aiohttp
except ImportError:
    aiohttp = None

from axelr_config import (
    CONFIDENCE_THRESHOLD,
    CalibrationConfig,
    HardwareProfile,
)
from axelr_utils import get_logger

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# §5 — PEDA #6 Phonetic primitives imported from axelr_phonetics
# ═══════════════════════════════════════════════════════════════════════════
# axelr_phonetics.py
"""
axelr_phonetics.py — PEDA #6, Phonetic primitives.

This file is the single source of truth for phonetic algorithms like Soundex
and Metaphone. It is designed to be portable to other languages, especially
JavaScript, for client-side validation or features.
"""


# ═══════════════════════════════════════════════════════════════════════════
# Phoneme to Grapheme mapping for normalization
# ═══════════════════════════════════════════════════════════════════════════
PHONEME_GRAPHEME_MAP: Dict[str, str] = {
    "ah": "a",
    "eh": "e",
    "ih": "i",
    "oh": "o",
    "uh": "u",
    "ae": "ai",
    "ee": "ea",
    "ie": "ei",
    "oo": "ou",
    "ck": "c",
    "ks": "x",
    "kw": "qu",
    "sh": "ti",
    "zh": "si",
    "ksh": "ction",
    "shun": "tion",
}


class PhonemeGraphemeNormalizer:
    __slots__ = ("_map",)

    def __init__(self) -> None:
        self._map = PHONEME_GRAPHEME_MAP

    def normalize(self, text: str) -> str:
        out = text.lower()
        for phon, graph in self._map.items():
            out = out.replace(phon, graph)
        return out


# ═══════════════════════════════════════════════════════════════════════════
# Soundex algorithm
# ═══════════════════════════════════════════════════════════════════════════
def soundex(word: str) -> str:
    """Standard Soundex algorithm. Single source to be ported to JS."""
    if not word:
        return ""
    # Mapping of letters to Soundex codes
    codes = {
        "b": "1",
        "f": "1",
        "p": "1",
        "v": "1",
        "c": "2",
        "g": "2",
        "j": "2",
        "k": "2",
        "q": "2",
        "s": "2",
        "x": "2",
        "z": "2",
        "d": "3",
        "t": "3",
        "l": "4",
        "m": "5",
        "n": "5",
        "r": "6",
    }
    # Process first character
    word = word.lower()
    first = word[0].upper()
    # Generate code for remaining characters
    result = [first]
    prev_code = codes.get(first.lower(), "")
    for ch in word[1:]:
        code = codes.get(ch, "")
        if code and code != prev_code:
            result.append(code)
            prev_code = code
        if len(result) >= 4:
            break
    # Pad with zeros if needed
    while len(result) < 4:
        result.append("0")
    return "".join(result)


# ═══════════════════════════════════════════════════════════════════════════
# Double Metaphone core algorithm
# ═══════════════════════════════════════════════════════════════════════════
def metaphone(word: str) -> str:
    """Double Metaphone core algorithm. Single source to be ported to JS."""
    if not word:
        return ""
    word = word.lower()
    result = []
    i = 0
    n = len(word)

    # Handle silent leading combinations first
    if n >= 2:
        # 'kn' → silent 'k', only 'n' remains
        if word[0] == "k" and word[1] == "n":
            i = 1  # Skip 'k', start at 'n'
        # 'gn' → silent 'g', only 'n' remains
        elif word[0] == "g" and word[1] == "n":
            i = 1  # Skip 'g', start at 'n'
        # 'ps' → silent 'p', only 's' remains
        elif word[0] == "p" and word[1] == "s":
            i = 1  # Skip 'p', start at 's'
        # 'rh' → silent 'h', only 'r' remains (rhyme → rime)
        elif word[0] == "r" and word[1] == "h":
            i = 1  # Skip 'h', start at 'r'
        # 'wh' → silent 'w', only 'h' remains (what → hat)
        elif word[0] == "w" and word[1] == "h":
            i = 1  # Skip 'w', start at 'h'
        # Keep original 'wr' handling for all words
        elif word[0] == "w" and word[1] == "r":
            i = 1  # Skip 'w', start at 'r'

    # Process remaining word with core metaphone rules
    while i < n:
        ch = word[i]
        # Align all other 'ei' and 'ie' cases phonetically
        if i + 1 < n:
            if word[i] == "e" and word[i + 1] == "i":
                result.append("i")
                i += 2
                continue
            elif word[i] == "i" and word[i + 1] == "e":
                result.append("i")
                i += 2
                continue
        # Handle simple mappings first
        if ch in "aeiou":
            if i == 0:  # Keep initial vowels
                result.append(ch)
            i += 1
        elif ch == "b":
            result.append("b")
            i += 1
        elif ch == "c":
            # Handle 'ch', 'ci', 'ce' cases
            if i + 1 < n and word[i + 1] == "h":
                result.append("x")
                i += 2
            elif i + 1 < n and word[i + 1] in "iey":
                result.append("s")
                i += 2
            else:
                result.append("k")
                i += 1
        elif ch == "d":
            if i + 1 < n and word[i + 1] == "g":
                result.append("j")
                i += 2
            else:
                result.append("t")
                i += 1
        elif ch == "f":
            result.append("f")
            i += 1
        elif ch == "g":
            if i + 1 < n and word[i + 1] == "h":
                result.append("k")
                i += 2
            else:
                result.append("g")
                i += 1
        elif ch == "h":
            if i == 0 or (i > 0 and word[i - 1] not in "aeiou"):
                result.append("h")
            i += 1
        elif ch == "j":
            result.append("j")
            i += 1
        elif ch == "k":
            result.append("k")
            i += 1
        elif ch == "l":
            result.append("l")
            i += 1
        elif ch == "m":
            result.append("m")
            i += 1
        elif ch == "n":
            result.append("n")
            i += 1
        elif ch == "p":
            if i + 1 < n and word[i + 1] == "h":
                result.append("f")
                i += 2
            else:
                result.append("p")
                i += 1
        elif ch == "q":
            result.append("k")
            i += 1
        elif ch == "r":
            result.append("r")
            i += 1
        elif ch == "s":
            if i + 1 < n and word[i + 1] == "h":
                result.append("x")
                i += 2
            else:
                result.append("s")
                i += 1
        elif ch == "t":
            if i + 1 < n and word[i + 1] == "h":
                result.append("0")
                i += 2
            else:
                result.append("t")
                i += 1
        elif ch == "v":
            result.append("f")
            i += 1
        elif ch == "w":
            if i + 1 < n and word[i + 1] in "aeiou":
                result.append("w")
            i += 1
        elif ch == "x":
            result.append("k")
            i += 1
        elif ch == "y":
            if i + 1 < n and word[i + 1] in "aeiou":
                result.append("y")
            i += 1
        elif ch == "z":
            result.append("s")
            i += 1
        else:
            i += 1
    # Pad with zeros if needed to ensure 4-character code for consistent output
    while len(result) < 4:
        result.append("0")
    return "".join(result)[:4]


class Metaphone:
    """Wrapper class for metaphone function to match test expectations."""

    def compute(self, word: str) -> str:
        return metaphone(word)


# ═══════════════════════════════════════════════════════════════════════════
# PhoneticEnsemble: Combined phonetic algorithms
# ═══════════════════════════════════════════════════════════════════════════
class PhoneticEnsemble:
    """Combined phonetic algorithms. Single source for JS porting."""

    __slots__ = ()

    @staticmethod
    def encode(word: str) -> Tuple[str, str]:
        """Return (soundex, metaphone) for a word."""
        return soundex(word), metaphone(word)

    @staticmethod
    def compare(a: str, b: str) -> float:
        """Compare two words phonetically. Returns 0.0-1.0 similarity."""
        s1, m1 = PhoneticEnsemble.encode(a)
        s2, m2 = PhoneticEnsemble.encode(b)
        score = 0.0
        if s1 == s2:
            score += 0.5
        if m1 == m2:
            score += 0.5
        return score


# ═══════════════════════════════════════════════════════════════════════════
# Token / config
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class Token:
    text: str
    confidence: float
    token_id: int


@dataclass(frozen=True, slots=True)
class DecoderConfig:
    confidence_threshold: float = CONFIDENCE_THRESHOLD
    cloud_fallback_url: Optional[str] = None
    api_key: Optional[str] = None
    max_local_memory_mb: int = 150
    model_quantization: str = "int4"


# ═══════════════════════════════════════════════════════════════════════════
# §1 — SGO #7 StaticGraphRuntime
# ═══════════════════════════════════════════════════════════════════════════
class StaticGraphRuntime:
    """
    Reserves a contiguous float32 arena of the profile's memory budget minus
    the SGO overhead. This is honest pre-allocation, not a promise: the
    arena is real numpy memory the decoder can slice into. Real GGML/ONNX
    would pass the arena pointer into their context init.
    """

    __slots__ = ("tensor_shapes", "memory_allocated", "is_initialized", "_warmed", "_arena", "_target_bytes")

    def __init__(self) -> None:
        self.tensor_shapes: Dict[str, Tuple[int, ...]] = {}
        self.memory_allocated = 0
        self.is_initialized = False
        self._warmed = False
        self._arena: Optional[np.ndarray] = None
        self._target_bytes = 0

    def reserve(self, profile):
        self._target_bytes = (profile.max_memory_mb - 20) * 1024 * 1024
        self.memory_allocated = 0
        self.is_initialized = False
        self._arena = None

    def commit(self) -> bool:
        """Actually allocate. Call after warmup or on first real inference."""
        if self._arena is not None:
            return True
        try:
            self._arena = np.empty(self._target_bytes // 4, dtype=np.float32)
            self.memory_allocated = self._target_bytes
            self.is_initialized = True
            return True
        except (MemoryError, ValueError):
            logger.warning("arena_alloc_failed_mb=%d", self._target_bytes // 1048576)
            return False

    @property
    def arena(self) -> Optional[np.ndarray]:
        return self._arena


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Calibrator (#16)
# ═══════════════════════════════════════════════════════════════════════════
class ConfidenceCalibrator:
    """Temperature-scaling calibrator: sigmoid(logit / T)."""

    __slots__ = ("_temp", "_min", "_max", "_samples")

    def __init__(self, config: CalibrationConfig) -> None:
        self._temp = max(config.min_temperature, min(config.max_temperature, config.initial_temperature))
        self._min = config.min_temperature
        self._max = config.max_temperature
        self._samples = 0

    @property
    def temperature(self) -> float:
        return self._temp

    def calibrate(self, raw_logit: float) -> float:
        z = raw_logit / self._temp
        if z >= 0:
            return float(1.0 / (1.0 + np.exp(-z)))
        e = np.exp(z)
        return float(e / (1.0 + e))

    def observe(self, raw_logit: float, was_correct: bool) -> None:
        p = self.calibrate(raw_logit)
        lr = 0.005
        if not was_correct and p > 0.8:
            self._temp = min(self._max, self._temp + lr)
        elif was_correct and p < 0.3:
            self._temp = max(self._min, self._temp - lr)
        self._samples += 1


# ═══════════════════════════════════════════════════════════════════════════
# §3 — LPGN #15 PhonemeGraphemeNormalizer
# ═══════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════
# §4 — TFLP #13 TrieLexicon
# ═══════════════════════════════════════════════════════════════════════════
class _TrieNode:
    __slots__ = ("children", "is_word", "word")

    def __init__(self) -> None:
        self.children: Dict[str, "_TrieNode"] = {}
        self.is_word = False
        self.word: Optional[str] = None


class TrieLexicon:
    __slots__ = ("_root", "size")

    def __init__(self) -> None:
        self._root = _TrieNode()
        self.size = 0

    def insert(self, word: str) -> None:
        node = self._root
        for ch in word.lower():
            if ch not in node.children:
                node.children[ch] = _TrieNode()
            node = node.children[ch]
        if not node.is_word:
            node.is_word = True
            node.word = word
            self.size += 1

    def contains(self, word: str) -> bool:
        node = self._root
        for ch in word.lower():
            node = node.children.get(ch)  # type: ignore[assignment]
            if node is None:
                return False
        return node.is_word

    def prefix(self, prefix: str, limit: int = 5) -> List[str]:
        node = self._root
        for ch in prefix.lower():
            node = node.children.get(ch)  # type: ignore[assignment]
            if node is None:
                return []
        out: List[str] = []
        self._collect(node, out, limit)
        return out

    def _collect(self, node: _TrieNode, out: List[str], limit: int) -> None:
        if len(out) >= limit:
            return
        if node.is_word and node.word is not None:
            out.append(node.word)
        for child in node.children.values():
            self._collect(child, out, limit)
            if len(out) >= limit:
                return


# ═══════════════════════════════════════════════════════════════════════════
# §5 — PEDA #6 Phonetic primitives imported from axelr_phonetics


def edit_distance(a: str, b: str, max_dist: Optional[int] = None) -> int:
    if len(a) < len(b):
        return edit_distance(b, a, max_dist)
    if not b:
        return len(a)
    if max_dist is not None and abs(len(a) - len(b)) > max_dist:
        return max_dist + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        row_min = i + 1
        for j, cb in enumerate(b):
            cost = 0 if ca == cb else 1
            curr_j = min(prev[j + 1] + 1, curr[j] + 1, prev[j] + cost)
            curr.append(curr_j)
            row_min = min(row_min, curr_j)
        if max_dist is not None and row_min > max_dist:
            return max_dist + 1
        prev = curr
    return prev[-1]


# ═══════════════════════════════════════════════════════════════════════════
# §6 — PhoneticCorrector (ensemble scoring)
# ═══════════════════════════════════════════════════════════════════════════
_COMMON_WORDS = frozenset(
    {
        "receive",
        "their",
        "they're",
        "there",
        "your",
        "you're",
        "its",
        "it's",
        "too",
        "to",
        "two",
    }
)


class PhoneticCorrector:
    __slots__ = ("_trie", "_sdx", "_mtn", "_lpg", "_common_words", "_common_bias")

    def __init__(self, common_words: Optional[frozenset] = None, common_bias: float = 0.5) -> None:
        self._trie = TrieLexicon()
        self._sdx: Dict[str, Set[str]] = {}
        self._mtn: Dict[str, Set[str]] = {}
        self._lpg = PhonemeGraphemeNormalizer()
        self._common_words = common_words if common_words is not None else _COMMON_WORDS
        self._common_bias = common_bias

    def load(self, words: List[str]) -> None:
        for w in words:
            self._trie.insert(w)
            sdx = soundex(w)
            mtn = metaphone(w)
            self._sdx.setdefault(sdx, set()).add(w)
            self._mtn.setdefault(mtn, set()).add(w)

    def correct(self, word: str) -> str:
        # LPGN normalization short-circuit
        norm = self._lpg.normalize(word)
        if norm != word and self._trie.contains(norm):
            return norm

        # Gather candidates from all three indices
        cands: Set[str] = set()
        word_sdx = soundex(word)
        word_mtn = metaphone(word)
        cands.update(self._sdx.get(word_sdx, ()))
        cands.update(self._mtn.get(word_mtn, ()))
        cands.update(self._trie.prefix(word[:3], 5))
        if not cands:
            return word

        return max(cands, key=lambda c: self._score(c, word))

    def top_k(self, word: str, k: int) -> List[Tuple[str, float]]:
        """Return top K candidates and their scores."""
        norm = self._lpg.normalize(word)
        cands: Set[str] = set()
        if norm != word and self._trie.contains(norm):
            cands.add(norm)

        word_sdx = soundex(word)
        word_mtn = metaphone(word)
        cands.update(self._sdx.get(word_sdx, ()))
        cands.update(self._mtn.get(word_mtn, ()))
        cands.update(self._trie.prefix(word[:3], 5))

        if not cands:
            return [(word, 0.0)]

        scored = [(c, self._score(c, word)) for c in cands]
        return sorted(scored, key=lambda x: x[1], reverse=True)[:k]

    def _score(self, candidate: str, word: str) -> float:
        """Weighted ensemble. Max ~8.0."""
        s = 0.0
        cand_sdx = soundex(candidate)
        cand_mtn = metaphone(candidate)
        word_sdx = soundex(word)
        word_mtn = metaphone(word)
        if cand_sdx == word_sdx:
            s += 2.5
        if cand_mtn == word_mtn:
            s += 2.0
        if candidate[:3].lower() == word[:3].lower():
            s += 1.5
        len_diff = abs(len(candidate) - len(word))
        s += max(0.0, 1.0 - len_diff * 0.2)
        s -= edit_distance(word, candidate) * 0.25
        if candidate.lower() in self._common_words:
            s += self._common_bias
        return s


# ═══════════════════════════════════════════════════════════════════════════
# §6.5 — SymSpell (fast delete-variant lookup)
# ═══════════════════════════════════════════════════════════════════════════
_WORDS_RAW = (
    "new clear open close save export import send stop undo redo delete settings "
    "cancel confirm apply reset retry back forward next previous search find filter "
    "sort format enhance summarize translate deploy preview debug run execute "
    "the be to of and a in that have i it for not on with he as you do at this "
    "but his by from they we say her she or an will my one all would there their "
    "what so up out if about who get which go me when make can like time no just "
    "him know take people into year your good some could them see other than then "
    "now look only come its over think also back after use two how our work first "
    "well way even new want because any these give day most us is was were been has "
    "had did does doing done being am are will would can could should may might "
    "must shall here where why when who whom whose which what that this those "
    "function class module package import export const let var async await promise "
    "array object string number boolean null undefined true false return break "
    "continue while for if else switch case default try catch finally throw new "
    "this super static public private protected extend implement interface type "
    "react vue svelte angular next nuxt astro solid css html javascript typescript "
    "node npm yarn pnpm python rust go java ruby php docker kubernetes sql "
    "api endpoint server client request response database query select insert "
    "update delete where group order limit join table index column row schema "
    "receive believe achieve separate occurred definitely their there they're "
    "your you're its it's then than were we're where whose who's right write "
    "hear here weather whether affect effect accept except advice advise"
)


class SymSpell:
    __slots__ = ("max_dist", "words", "freq", "word_index", "deletes", "_raw_cursor", "_built")

    def __init__(self, max_edit_distance: int = 2) -> None:
        self.max_dist = min(2, max(1, max_edit_distance))
        self.words: List[str] = []
        self.freq: List[int] = []
        self.word_index: Dict[str, int] = {}
        self.deletes: Dict[str, List[int]] = {}
        self._raw_cursor = 0
        self._built = False
        self._build_partial()

    @staticmethod
    def from_words(words: List[str], max_dist: int = 2) -> "SymSpell":
        s = SymSpell(max_dist)
        s.ensure_built()
        for w in words:
            s.add_custom_word(w)
        return s

    def _build_partial(self) -> None:
        self._build_from_source(400)

    def _build_rest(self) -> None:
        self._build_from_source(float("inf"))
        self._built = True

    def ensure_built(self) -> "SymSpell":
        if not self._built:
            self._build_rest()
        return self

    def _build_from_source(self, limit: float) -> None:
        raw = _WORDS_RAW.split()
        end = len(raw) if limit == float("inf") else min(len(raw), self._raw_cursor + int(limit))
        for i in range(self._raw_cursor, end):
            w = raw[i].lower()
            if len(w) < 2 or w in self.word_index:
                continue
            rank = len(self.words)
            self.words.append(w)
            self.freq.append(rank)
            self.word_index[w] = rank
            self._insert_deletes(w, rank)
        self._raw_cursor = end

    def _insert_deletes(self, word: str, rank: int) -> None:
        if not word or len(word) == 0:
            return
        for i in range(len(word)):
            del_var = word[:i] + word[i + 1 :]
            if del_var not in self.deletes:
                self.deletes[del_var] = []
            if len(self.deletes[del_var]) < 64:
                self.deletes[del_var].append(rank)

    def add_custom_word(self, word: str, frequency: Optional[int] = None) -> bool:
        w = str(word or "").lower().strip()
        if not w or len(w) < 2 or len(w) > 64 or w in self.word_index:
            return False
        rank = len(self.words)
        self.words.append(w)
        self.freq.append(frequency if frequency is not None else rank)
        self.word_index[w] = rank
        self._insert_deletes(w, rank)
        return True

    def lookup(self, input_str: str, max_dist: Optional[int] = None) -> List[Dict]:
        target = str(input_str or "").lower().strip()
        if not target or len(target) < 2:
            return []
        d = min(self.max_dist, 2 if max_dist is None else max_dist)
        if target in self.word_index:
            return [{"word": target, "distance": 0, "frequency": self.word_index[target]}]
        results: List[Dict] = []
        seen = set()
        self._collect(target, 1, results, seen)
        if d >= 2:
            for td in self._all_deletes(target):
                self._collect(td, 2, results, seen)
        results.sort(key=lambda r: (r["distance"], r["frequency"]))
        verified = []
        for r in results:
            true_dist = edit_distance(target, r["word"], d)
            if true_dist <= d:
                verified.append({"word": r["word"], "distance": true_dist, "frequency": r["frequency"]})
            if len(verified) >= 8:
                break
        return verified

    def lookup_batch(self, tokens: List[str], max_dist: Optional[int] = None) -> List[str]:
        out = []
        for t in tokens:
            hits = self.lookup(t, max_dist)
            out.append(hits[0]["word"] if hits else t)
        return out

    def _collect(self, variant: str, distance: int, results: List[Dict], seen: Set[int]) -> None:
        if variant not in self.deletes:
            return
        for rank in self.deletes[variant]:
            if rank in seen:
                continue
            seen.add(rank)
            results.append({"word": self.words[rank], "distance": distance, "frequency": self.freq[rank]})

    def _all_deletes(self, word: str) -> List[str]:
        return [word[:i] + word[i + 1 :] for i in range(len(word))]


# ═══════════════════════════════════════════════════════════════════════════
# §7 — UserLexicon
# ═══════════════════════════════════════════════════════════════════════════
class UserLexicon:
    """
    Atomic-commit user lexicon. Writes go through tempfile + os.replace so a
    crash mid-write leaves the previous file intact. Debounced to avoid
    O(N^2) rewrites on bulk adds — flush() must be called before shutdown.
    """

    __slots__ = ("_path", "_words", "_dirty", "_flush_timer", "_lock")

    def __init__(self, path: Path) -> None:
        import threading

        self._path = path
        self._words: Set[str] = set()
        self._dirty = False
        self._flush_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def load(self) -> List[str]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._words = set(map(str, data.get("words", [])))
        except (OSError, ValueError):
            logger.exception("user_lexicon_load_failed")
            self._words = set()
        return list(self._words)

    def add(self, word: str) -> None:
        with self._lock:
            if word in self._words:
                return
            self._words.add(word)
            self._dirty = True
            self._schedule_flush_locked()

    def _schedule_flush_locked(self) -> None:
        import threading

        if self._flush_timer is not None:
            self._flush_timer.cancel()
        self._flush_timer = threading.Timer(2.0, self._flush)
        self._flush_timer.daemon = True
        self._flush_timer.start()

    def flush(self) -> None:
        self._flush()

    def _flush(self) -> None:
        import os
        import tempfile

        with self._lock:
            if not self._dirty:
                return
            words_snapshot = sorted(self._words)
            self._dirty = False
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix=".userlex_", dir=str(self._path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"words": words_snapshot}, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self._path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception:
            logger.exception("user_lexicon_save_failed")


# ═══════════════════════════════════════════════════════════════════════════
# §8 — SCD #1 SpeculativeCascadeDecoder
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class ProcessResult:
    accepted: List["Token"]
    low_confidence: List["Token"]
    queued_to_cloud: int


def _detect_pcm_arg(fn) -> bool:
    """True if `fn` accepts a 2nd positional argument (pcm)."""
    try:
        params = inspect.signature(fn).parameters
        if "pcm" in params:
            return True
        return len(params) >= 2
    except (ValueError, TypeError):
        return False


LocalInferenceFn = Callable[[np.ndarray, Optional[np.ndarray]], List[Token]]


class SpeculativeCascadeDecoder:
    """Local streaming + async cloud. Returns accepted AND low-conf tokens."""

    __slots__ = (
        "config",
        "runtime",
        "calibrator",
        "sampling",
        "_local_fn",
        "_session",
        "_cloud_task",
        "_queue",
        "_low_conf",
        "_streaming",
        "_cloud_router",
        "_fn_takes_pcm",
    )

    def __init__(
        self,
        config: "DecoderConfig",
        profile: HardwareProfile,
        local_fn: "LocalInferenceFn",
        cloud_router: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.runtime = StaticGraphRuntime()
        self.runtime.reserve(profile)
        self.calibrator = ConfidenceCalibrator(profile.calibration)
        self.sampling = profile.sampling
        self._local_fn = local_fn
        self._session = None
        self._cloud_task = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._low_conf: List["Token"] = []
        self._streaming = False
        self._cloud_router = cloud_router
        self._fn_takes_pcm = _detect_pcm_arg(local_fn)

    async def start(self) -> None:
        router_ok = True
        if self._cloud_router is not None:
            router_ok = self._cloud_router.enabled
        if router_ok and self.config.cloud_fallback_url and self.config.api_key and aiohttp is not None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=0.5))
        self._streaming = True
        self._cloud_task = asyncio.create_task(self._cloud_worker())

    async def stop(self) -> None:
        self._streaming = False
        if self._cloud_task is not None:
            await self._cloud_task
            self._cloud_task = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def warm_up(self) -> None:
        if self.runtime._warmed:
            return
        self.runtime.commit()
        silent = np.zeros((1, 80), dtype=np.float32)
        _ = self._local_fn(silent)
        self.runtime._warmed = True

    async def process(self, log_mel: np.ndarray, pcm: Optional[np.ndarray] = None) -> "ProcessResult":
        if self._fn_takes_pcm:
            raw = self._local_fn(log_mel, pcm)
        else:
            raw = self._local_fn(log_mel)
        accepted: List[Token] = []
        low_conf_batch: List[Token] = []
        queued = 0
        for tok in raw:
            p = max(min(tok.confidence, 1.0 - 1e-6), 1e-6)
            calibrated = self.calibrator.calibrate(np.log(p) - np.log(1.0 - p))
            tok_cal = Token(tok.text, float(calibrated), tok.token_id)
            if tok_cal.confidence < self.config.confidence_threshold:
                self._low_conf.append(tok_cal)
                low_conf_batch.append(tok_cal)
            else:
                if self._low_conf:
                    try:
                        self._queue.put_nowait(self._low_conf)
                        queued += len(self._low_conf)
                    except asyncio.QueueFull:
                        logger.warning("scd_queue_full_dropping_batch")
                    self._low_conf = []
                accepted.append(tok_cal)
        return ProcessResult(accepted=accepted, low_confidence=low_conf_batch, queued_to_cloud=queued)

    async def _cloud_worker(self) -> None:
        while self._streaming:
            try:
                batch = await asyncio.wait_for(self._queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            if not batch:
                self._queue.task_done()
                continue
            try:
                text = " ".join(t.text for t in batch)
                await self._call_cloud(text)
            finally:
                self._queue.task_done()

    async def _call_cloud(self, text: str) -> Optional[str]:
        if self._session is None or not self.config.cloud_fallback_url:
            return text
        if self._cloud_router is not None and not self._cloud_router.breaker.allow():
            logger.debug("cloud_router_circuit_open")
            return text
        try:
            async with self._session.post(
                self.config.cloud_fallback_url,
                json={"text": text, "threshold": self.config.confidence_threshold},
                headers={"Authorization": f"Bearer {self.config.api_key}"},
            ) as resp:
                if resp.status != 200:
                    if self._cloud_router is not None:
                        self._cloud_router.breaker.record(False)
                    return None
                if self._cloud_router is not None:
                    self._cloud_router.breaker.record(True)
                data = await resp.json()
                return data.get("corrected_text")
        except Exception:
            if self._cloud_router is not None:
                self._cloud_router.breaker.record(False)
            logger.exception("cloud_fallback_failed")
            return text


WORDS_RAW = (
    "new clear open close save export import send stop undo redo delete settings "
    "cancel confirm apply reset retry back forward next previous search find filter "
    "sort format enhance summarize translate deploy preview debug run execute "
    "the be to of and a in that have i it for not on with he as you do at this "
    "but his by from they we say her she or an will my one all would there their "
    "what so up out if about who get which go me when make can like time no just "
    "him know take people into year your good some could them see other than then "
    "now look only come its over think also back after use two how our work first "
    "well way even new want because any these give day most us is was were been has "
    "had did does doing done being am are will would can could should may might "
    "must shall here where why when who whom whose which what that this those "
    "function class module package import export const let var async await promise "
    "array object string number boolean null undefined true false return break "
    "continue while for if else switch case default try catch finally throw new "
    "this super static public private protected extend implement interface type "
    "react vue svelte angular next nuxt astro solid css html javascript typescript "
    "node npm yarn pnpm python rust go java ruby php docker kubernetes sql "
    "api endpoint server client request response database query select insert "
    "update delete where group order limit join table index column row schema "
    "receive believe achieve separate occurred definitely their there they're "
    "your you're its it's then than were we're where whose who's right write "
    "hear here weather whether affect effect accept except advice advise"
)


class SymSpell:
    def __init__(self, max_edit_distance: int = 2):
        self.max_dist = min(2, max(1, max_edit_distance))
        self.words: List[str] = []
        self.freq: List[int] = []
        self.word_index: Dict[str, int] = {}
        self.deletes: Dict[str, List[int]] = {}
        self._raw_cursor = 0
        self._built = False
        self._build_partial()

    @staticmethod
    def from_words(words: List[str], max_dist: int = 2) -> "SymSpell":
        s = SymSpell(max_dist)
        s.ensure_built()
        for w in words:
            s.add_custom_word(w)
        return s

    def _build_partial(self) -> None:
        self._build_from_source(400)

    def _build_rest(self) -> None:
        self._build_from_source(float("inf"))
        self._built = True

    def ensure_built(self) -> "SymSpell":
        if not self._built:
            self._build_rest()
        return self

    def _build_from_source(self, limit: float) -> None:
        raw = WORDS_RAW.split()
        end = len(raw) if limit == float("inf") else min(len(raw), self._raw_cursor + int(limit))
        for i in range(self._raw_cursor, end):
            w = raw[i].lower()
            if len(w) < 2 or w in self.word_index:
                continue
            rank = len(self.words)
            self.words.append(w)
            self.freq.append(rank)
            self.word_index[w] = rank
            self._insert_deletes(w, rank)
        self._raw_cursor = end

    def _insert_deletes(self, word: str, rank: int) -> None:
        if not word or len(word) == 0:
            return
        for i in range(len(word)):
            del_str = word[:i] + word[i + 1 :]
            if del_str not in self.deletes:
                self.deletes[del_str] = []
            if len(self.deletes[del_str]) < 64:
                self.deletes[del_str].append(rank)

    def add_custom_word(self, word: str, frequency: Optional[int] = None) -> bool:
        w = str(word or "").lower().strip()
        if not w or len(w) < 2 or len(w) > 64 or w in self.word_index:
            return False
        rank = len(self.words)
        self.words.append(w)
        self.freq.append(frequency if isinstance(frequency, int) else rank)
        self.word_index[w] = rank
        self._insert_deletes(w, rank)
        return True

    def lookup(self, input_str: str, max_dist: Optional[int] = None) -> List[Dict]:
        target = str(input_str or "").lower().strip()
        if not target or len(target) < 2:
            return []
        d = min(self.max_dist, 2 if max_dist is None else max_dist)
        exact = self.word_index.get(target)
        if exact is not None:
            return [{"word": target, "distance": 0, "frequency": exact}]
        results: List[Dict] = []
        seen = set()
        self._collect(target, 1, results, seen)
        if d >= 2:
            for td in self._all_deletes(target):
                self._collect(td, 2, results, seen)
        results.sort(key=lambda x: (x["distance"], x["frequency"]))
        verified = []
        for r in results:
            if not r or "word" not in r:
                continue
            true_dist = edit_distance(target, r["word"], d)
            if true_dist <= d:
                verified.append({"word": r["word"], "distance": true_dist, "frequency": r["frequency"]})
            if len(verified) >= 8:
                break
        return verified

    def lookup_batch(self, tokens: List[str], max_dist: Optional[int] = None) -> List[str]:
        out = []
        for t in tokens:
            hits = self.lookup(t, max_dist)
            out.append(hits[0]["word"] if hits else t)
        return out

    def _collect(self, variant: str, distance: int, results: List[Dict], seen: Set[str]) -> None:
        list_ranks = self.deletes.get(variant)
        if not list_ranks:
            return
        for rank in list_ranks:
            word = self.words[rank]
            if not word or word in seen:
                continue
            seen.add(word)
            results.append({"word": word, "distance": distance, "frequency": rank})

    def _all_deletes(self, word: str) -> Set[str]:
        out = set()
        for i in range(len(word)):
            d1 = word[:i] + word[i + 1 :]
            out.add(d1)
            for j in range(len(d1)):
                out.add(d1[:j] + d1[j + 1 :])
        return out

    def size(self) -> int:
        return len(self.words)

    def stats(self) -> Dict:
        return {
            "words": self.size(),
            "deletes_keys": len(self.deletes),
            "built": self._built,
            "max_dist": self.max_dist,
        }

    def dispose(self) -> None:
        self.words = []
        self.freq = []
        self.word_index.clear()
        self.deletes.clear()
        self._raw_cursor = 0
        self._built = False


__all__ = [
    "Token",
    "DecoderConfig",
    "StaticGraphRuntime",
    "ConfidenceCalibrator",
    "TrieLexicon",
    "edit_distance",
    "PhoneticCorrector",
    "UserLexicon",
    "LocalInferenceFn",
    "SpeculativeCascadeDecoder",
    "soundex",
    "metaphone",
    "Metaphone",
    "SymSpell",
]
