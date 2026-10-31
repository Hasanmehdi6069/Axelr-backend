<<<<<<< HEAD
# core/adaptive_control.py
"""
AXELR — Adaptive Control (sampling · anchors · stop · stream gate)
===================================================================
Four orthogonal primitives that raise output quality without touching
the provider layer. Pure stdlib. Zero imports from api/* or core/*.

    §1  Temperature controller   tune(intent, complexity, task_type) -> float
    §2  Session anchor ledger    AnchorLedger(store=dict) — LRU, no Redis
    §3  Stop sequences           stops_for(workspace, task_type) -> tuple[str, ...]
    §4  Streaming quality gate   StreamQualityGate — hold-and-release

Every primitive is deterministic, bounded, and independently testable.
The ledger is a plain dict — callers supply the scope (session_id).
"""
from __future__ import annotations

import logging
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger("axelr.adaptive")

_VERSION = "1.0"

# ═══════════════════════════════════════════════════════════════════════════
# §1 — Adaptive temperature
# ═══════════════════════════════════════════════════════════════════════════

# And in _BASE_TEMP:
_BASE_TEMP: dict[str, float] = {
    "conversational": 0.55,
    "transactional":  0.20,              # ← was 0.30
    "extraction":     0.0,               # ← was 0.10
    "styling":        0.50,
    "strategy":       0.65,
    "diagnostic":     0.10,              # ← was 0.20
}

# And in _TASK_TEMP:
_TASK_TEMP: dict[str, float] = {
    "frontend":     0.45,
    "extraction":   0.0,                 # ← was 0.05
    "touch_fix":    0.10,                # ← was 0.15
    "structuring":  0.35,                # ← was 0.40
}


# Tier adjustment: deep-tier requests get slightly more creative latitude.
_TIER_ADJ: dict[str, float] = {
    "trivial":  -0.05,
    "standard":  0.00,
    "deep":     +0.05,
    "critical": +0.05,
    "shed":     -0.05,
    "lite":     -0.02,
}
# Replace the existing constants:
_MIN_TEMP, _MAX_TEMP = 0.0, 0.85         # ← was 0.05


def tune_temperature(
    *,
    intent: str = "",
    task_type: str = "",
    tier: str = "",
    user_override: float | None = None,
    workspace: str = "",
) -> float:
    """Deterministic. Same inputs → same temperature. Bounded [0.05, 0.85]."""
    if user_override is not None:
        return max(_MIN_TEMP, min(_MAX_TEMP, float(user_override)))
    if task_type in _TASK_TEMP:
        base = _TASK_TEMP[task_type]
    else:
        base = _BASE_TEMP.get(intent, 0.40)
    base += _TIER_ADJ.get(tier, 0.0)
    if workspace == "data":
        base -= 0.05
    return round(max(_MIN_TEMP, min(_MAX_TEMP, base)), 3)


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Session anchor ledger
# ═══════════════════════════════════════════════════════════════════════════
# Cross-turn continuity: keeps the highest-value anchors the user has
# mentioned this session so they survive even after 20 turns of churn.
# In-memory only. Bounded. No Redis dependency. Throws away on restart
# (acceptable — anchor value decays fast).
_IDENT_RE = re.compile(
    r"\b(?:[a-z]+_[a-z_]{2,}|[a-z]+(?:[A-Z][a-z0-9]+)+|"
    r"[A-Z][a-z]+(?:[A-Z][a-z0-9]+)+|[A-Z][A-Z0-9_]{2,})\b"
)
_PATH_RE = re.compile(r"(?:\.{0,2}/[\w./-]{3,}\.\w{1,6}|[A-Za-z]:\\[\w\\.-]+)")
_URL_RE = re.compile(r"https?://[^\s)\]\"'>]{6,}")
_ERRSTR_RE = re.compile(
    r"\b(?:Error|Exception|Warning|TypeError|ValueError|KeyError|"
    r"AttributeError|ImportError|RuntimeError|SyntaxError|"
    r"NullPointerException|SegmentationFault)\b[^\n]{0,120}"
)
_QUOTED_RE = re.compile(r"[\"']([^\"']{4,80})[\"']")

_MAX_LEDGER_ENTRIES = 40          # per session
_MAX_ENTRY_LEN = 200


def _extract_anchors(text: str) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    for rx in (_ERRSTR_RE, _URL_RE, _PATH_RE, _IDENT_RE):
        for m in rx.finditer(text):
            s = m.group(0).strip()
            if 3 <= len(s) <= _MAX_ENTRY_LEN:
                out.append(s)
    return out


class AnchorLedger:
    """
    Bounded, thread-safe LRU of anchors per session.
    Each session gets ~40 slots. Total memory ≈ 40 × 200 B × N_sessions.
    A 1000-session deployment tops out at ~8 MB. Fine at 512 MB.
    """

    __slots__ = ("_lock", "_store", "_max_sessions")

    def __init__(self, *, max_sessions: int = 2000) -> None:
        self._lock = threading.Lock()
        self._store: OrderedDict[str, OrderedDict[str, int]] = OrderedDict()
        self._max_sessions = max_sessions

    def _bucket(self, session_id: str) -> OrderedDict[str, int]:
        b = self._store.get(session_id)
        if b is None:
            b = OrderedDict()
            self._store[session_id] = b
            if len(self._store) > self._max_sessions:
                self._store.popitem(last=False)
        else:
            self._store.move_to_end(session_id)
        return b

    def observe(self, session_id: str, text: str) -> None:
        if not session_id or not text:
            return
        found = _extract_anchors(text)
        if not found:
            return
        with self._lock:
            b = self._bucket(session_id)
            for a in found:
                b[a] = b.get(a, 0) + 1
                b.move_to_end(a)
                while len(b) > _MAX_LEDGER_ENTRIES:
                    b.popitem(last=False)

    def render(self, session_id: str, *, limit: int = 12) -> str:
        """Return a compact injectable block, or ''."""
        if not session_id:
            return ""
        with self._lock:
            b = self._store.get(session_id)
            if not b:
                return ""
            ranked = sorted(b.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        lines = [f"• {a}" for a, _ in ranked]
        return ("<session_anchors>\n"
                "Identifiers, paths, and error strings referenced this "
                "session — preserve exact spelling.\n"
                + "\n".join(lines) + "\n</session_anchors>")

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._store.pop(session_id, None)

    def size(self) -> int:
        with self._lock:
            return len(self._store)


# Module-level singleton — safe because the store is thread-safe.
LEDGER = AnchorLedger()


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Deterministic stop sequences
# ═══════════════════════════════════════════════════════════════════════════
# Per workspace/task, tell the model where to stop. Prevents:
#   • runaway "Would you like me to…" tails
#   • post-answer preamble in structured tasks
#   • "Let me know if…" trailers in code delivery
_STOP_TABLE: dict[tuple[str, str], tuple[str, ...]] = {
    ("core", "structuring"):  ("\n\nWould you like", "\n\nLet me know if"),
    ("data", "extraction"):   ("\n\nThis data", "\n\nNote that"),
    ("design", "frontend"):   ("\n\nThis component", "\n\nFeel free"),
    ("core", "touch_fix"):    ("\n\nWould you like", "\n\nLet me know"),
}
_DEFAULT_STOPS: tuple[str, ...] = ("\n\nWould you like",)


def stops_for(workspace: str, task_type: str) -> tuple[str, ...]:
    return _STOP_TABLE.get((workspace, task_type), _DEFAULT_STOPS)


# ═══════════════════════════════════════════════════════════════════════════
# §4 — Streaming quality gate
# ═══════════════════════════════════════════════════════════════════════════
# Hold the first N tokens until we can rule out a refusal. If the lead-in
# looks like "I can't help with that", we suppress and force a retry
# instead of streaming the refusal to the user.
_REFUSAL_LEAD_RE = re.compile(
    r"^\s*(?:i\s+(?:can(?:'?t|not)|am\s+unable|won'?t)|"
    r"i'?m\s+sorry|as\s+an?\s+ai|i\s+apologize|"
    r"unfortunately,?\s+i)",
    re.I,
)
_GOOD_LEAD_RE = re.compile(
    r"^\s*(?:here|below|the\s+\w+|sure|certainly|"
    r"```|#{1,6}\s|\d+[.)]\s|[-*•]\s|\w{3,})",
)


@dataclass(slots=True)
class GateDecision:
    emit: bool
    buffered: str
    force_retry: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"emit": self.emit, "force_retry": self.force_retry,
                "buf_len": len(self.buffered)}


class StreamQualityGate:
    """
    Buffer the first ~60 chars before releasing. Once passed, passthrough.
    If a refusal pattern matches within the buffer, force_retry=True and
    suppress the tokens. Once cleared, `push` returns the released text
    so the caller can yield it to the client.
    """

    __slots__ = ("_buf", "_gate_passed", "_max_hold")

    def __init__(self, *, max_hold_chars: int = 80) -> None:
        self._buf = ""
        self._gate_passed = False
        self._max_hold = max(24, int(max_hold_chars))

    def push(self, chunk: str) -> GateDecision:
        if not chunk:
            return GateDecision(False, "", False)
        if self._gate_passed:
            return GateDecision(True, chunk, False)

        self._buf += chunk

        # Fail fast on refusal.
        if _REFUSAL_LEAD_RE.match(self._buf):
            self._buf = ""
            self._gate_passed = True   # don't hold subsequent tokens hostage
            return GateDecision(False, "", force_retry=True)

        # Release once we have enough evidence, or we hit the ceiling.
        if _GOOD_LEAD_RE.match(self._buf) or len(self._buf) >= self._max_hold:
            released = self._buf
            self._buf = ""
            self._gate_passed = True
            return GateDecision(True, released, False)

        return GateDecision(False, "", False)

    def flush(self) -> str:
        tail = self._buf
        self._buf = ""
        self._gate_passed = True
        return tail


# ═══════════════════════════════════════════════════════════════════════════
# Audit
# ═══════════════════════════════════════════════════════════════════════════
def audit() -> dict[str, Any]:
    return {
        "version": _VERSION,
        "base_temp_intents": sorted(_BASE_TEMP),
        "ledger_sessions": LEDGER.size(),
        "ledger_max_entries_per_session": _MAX_LEDGER_ENTRIES,
        "stop_sequences_defined": len(_STOP_TABLE),
    }


__all__ = [
    "tune_temperature",
    "AnchorLedger", "LEDGER",
    "stops_for",
    "StreamQualityGate", "GateDecision",
    "audit",
=======
# core/adaptive_control.py
"""
AXELR — Adaptive Control (sampling · anchors · stop · stream gate)
===================================================================
Four orthogonal primitives that raise output quality without touching
the provider layer. Pure stdlib. Zero imports from api/* or core/*.

    §1  Temperature controller   tune(intent, complexity, task_type) -> float
    §2  Session anchor ledger    AnchorLedger(store=dict) — LRU, no Redis
    §3  Stop sequences           stops_for(workspace, task_type) -> tuple[str, ...]
    §4  Streaming quality gate   StreamQualityGate — hold-and-release

Every primitive is deterministic, bounded, and independently testable.
The ledger is a plain dict — callers supply the scope (session_id).
"""
from __future__ import annotations

import logging
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger("axelr.adaptive")

_VERSION = "1.0"

# ═══════════════════════════════════════════════════════════════════════════
# §1 — Adaptive temperature
# ═══════════════════════════════════════════════════════════════════════════

# And in _BASE_TEMP:
_BASE_TEMP: dict[str, float] = {
    "conversational": 0.55,
    "transactional":  0.20,              # ← was 0.30
    "extraction":     0.0,               # ← was 0.10
    "styling":        0.50,
    "strategy":       0.65,
    "diagnostic":     0.10,              # ← was 0.20
}

# And in _TASK_TEMP:
_TASK_TEMP: dict[str, float] = {
    "frontend":     0.45,
    "extraction":   0.0,                 # ← was 0.05
    "touch_fix":    0.10,                # ← was 0.15
    "structuring":  0.35,                # ← was 0.40
}


# Tier adjustment: deep-tier requests get slightly more creative latitude.
_TIER_ADJ: dict[str, float] = {
    "trivial":  -0.05,
    "standard":  0.00,
    "deep":     +0.05,
    "critical": +0.05,
    "shed":     -0.05,
    "lite":     -0.02,
}
# Replace the existing constants:
_MIN_TEMP, _MAX_TEMP = 0.0, 0.85         # ← was 0.05


def tune_temperature(
    *,
    intent: str = "",
    task_type: str = "",
    tier: str = "",
    user_override: float | None = None,
    workspace: str = "",
) -> float:
    """Deterministic. Same inputs → same temperature. Bounded [0.05, 0.85]."""
    if user_override is not None:
        return max(_MIN_TEMP, min(_MAX_TEMP, float(user_override)))
    if task_type in _TASK_TEMP:
        base = _TASK_TEMP[task_type]
    else:
        base = _BASE_TEMP.get(intent, 0.40)
    base += _TIER_ADJ.get(tier, 0.0)
    if workspace == "data":
        base -= 0.05
    return round(max(_MIN_TEMP, min(_MAX_TEMP, base)), 3)


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Session anchor ledger
# ═══════════════════════════════════════════════════════════════════════════
# Cross-turn continuity: keeps the highest-value anchors the user has
# mentioned this session so they survive even after 20 turns of churn.
# In-memory only. Bounded. No Redis dependency. Throws away on restart
# (acceptable — anchor value decays fast).
_IDENT_RE = re.compile(
    r"\b(?:[a-z]+_[a-z_]{2,}|[a-z]+(?:[A-Z][a-z0-9]+)+|"
    r"[A-Z][a-z]+(?:[A-Z][a-z0-9]+)+|[A-Z][A-Z0-9_]{2,})\b"
)
_PATH_RE = re.compile(r"(?:\.{0,2}/[\w./-]{3,}\.\w{1,6}|[A-Za-z]:\\[\w\\.-]+)")
_URL_RE = re.compile(r"https?://[^\s)\]\"'>]{6,}")
_ERRSTR_RE = re.compile(
    r"\b(?:Error|Exception|Warning|TypeError|ValueError|KeyError|"
    r"AttributeError|ImportError|RuntimeError|SyntaxError|"
    r"NullPointerException|SegmentationFault)\b[^\n]{0,120}"
)
_QUOTED_RE = re.compile(r"[\"']([^\"']{4,80})[\"']")

_MAX_LEDGER_ENTRIES = 40          # per session
_MAX_ENTRY_LEN = 200


def _extract_anchors(text: str) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    for rx in (_ERRSTR_RE, _URL_RE, _PATH_RE, _IDENT_RE):
        for m in rx.finditer(text):
            s = m.group(0).strip()
            if 3 <= len(s) <= _MAX_ENTRY_LEN:
                out.append(s)
    return out


class AnchorLedger:
    """
    Bounded, thread-safe LRU of anchors per session.
    Each session gets ~40 slots. Total memory ≈ 40 × 200 B × N_sessions.
    A 1000-session deployment tops out at ~8 MB. Fine at 512 MB.
    """

    __slots__ = ("_lock", "_store", "_max_sessions")

    def __init__(self, *, max_sessions: int = 2000) -> None:
        self._lock = threading.Lock()
        self._store: OrderedDict[str, OrderedDict[str, int]] = OrderedDict()
        self._max_sessions = max_sessions

    def _bucket(self, session_id: str) -> OrderedDict[str, int]:
        b = self._store.get(session_id)
        if b is None:
            b = OrderedDict()
            self._store[session_id] = b
            if len(self._store) > self._max_sessions:
                self._store.popitem(last=False)
        else:
            self._store.move_to_end(session_id)
        return b

    def observe(self, session_id: str, text: str) -> None:
        if not session_id or not text:
            return
        found = _extract_anchors(text)
        if not found:
            return
        with self._lock:
            b = self._bucket(session_id)
            for a in found:
                b[a] = b.get(a, 0) + 1
                b.move_to_end(a)
                while len(b) > _MAX_LEDGER_ENTRIES:
                    b.popitem(last=False)

    def render(self, session_id: str, *, limit: int = 12) -> str:
        """Return a compact injectable block, or ''."""
        if not session_id:
            return ""
        with self._lock:
            b = self._store.get(session_id)
            if not b:
                return ""
            ranked = sorted(b.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        lines = [f"• {a}" for a, _ in ranked]
        return ("<session_anchors>\n"
                "Identifiers, paths, and error strings referenced this "
                "session — preserve exact spelling.\n"
                + "\n".join(lines) + "\n</session_anchors>")

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._store.pop(session_id, None)

    def size(self) -> int:
        with self._lock:
            return len(self._store)


# Module-level singleton — safe because the store is thread-safe.
LEDGER = AnchorLedger()


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Deterministic stop sequences
# ═══════════════════════════════════════════════════════════════════════════
# Per workspace/task, tell the model where to stop. Prevents:
#   • runaway "Would you like me to…" tails
#   • post-answer preamble in structured tasks
#   • "Let me know if…" trailers in code delivery
_STOP_TABLE: dict[tuple[str, str], tuple[str, ...]] = {
    ("core", "structuring"):  ("\n\nWould you like", "\n\nLet me know if"),
    ("data", "extraction"):   ("\n\nThis data", "\n\nNote that"),
    ("design", "frontend"):   ("\n\nThis component", "\n\nFeel free"),
    ("core", "touch_fix"):    ("\n\nWould you like", "\n\nLet me know"),
}
_DEFAULT_STOPS: tuple[str, ...] = ("\n\nWould you like",)


def stops_for(workspace: str, task_type: str) -> tuple[str, ...]:
    return _STOP_TABLE.get((workspace, task_type), _DEFAULT_STOPS)


# ═══════════════════════════════════════════════════════════════════════════
# §4 — Streaming quality gate
# ═══════════════════════════════════════════════════════════════════════════
# Hold the first N tokens until we can rule out a refusal. If the lead-in
# looks like "I can't help with that", we suppress and force a retry
# instead of streaming the refusal to the user.
_REFUSAL_LEAD_RE = re.compile(
    r"^\s*(?:i\s+(?:can(?:'?t|not)|am\s+unable|won'?t)|"
    r"i'?m\s+sorry|as\s+an?\s+ai|i\s+apologize|"
    r"unfortunately,?\s+i)",
    re.I,
)
_GOOD_LEAD_RE = re.compile(
    r"^\s*(?:here|below|the\s+\w+|sure|certainly|"
    r"```|#{1,6}\s|\d+[.)]\s|[-*•]\s|\w{3,})",
)


@dataclass(slots=True)
class GateDecision:
    emit: bool
    buffered: str
    force_retry: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"emit": self.emit, "force_retry": self.force_retry,
                "buf_len": len(self.buffered)}


class StreamQualityGate:
    """
    Buffer the first ~60 chars before releasing. Once passed, passthrough.
    If a refusal pattern matches within the buffer, force_retry=True and
    suppress the tokens. Once cleared, `push` returns the released text
    so the caller can yield it to the client.
    """

    __slots__ = ("_buf", "_gate_passed", "_max_hold")

    def __init__(self, *, max_hold_chars: int = 80) -> None:
        self._buf = ""
        self._gate_passed = False
        self._max_hold = max(24, int(max_hold_chars))

    def push(self, chunk: str) -> GateDecision:
        if not chunk:
            return GateDecision(False, "", False)
        if self._gate_passed:
            return GateDecision(True, chunk, False)

        self._buf += chunk

        # Fail fast on refusal.
        if _REFUSAL_LEAD_RE.match(self._buf):
            self._buf = ""
            self._gate_passed = True   # don't hold subsequent tokens hostage
            return GateDecision(False, "", force_retry=True)

        # Release once we have enough evidence, or we hit the ceiling.
        if _GOOD_LEAD_RE.match(self._buf) or len(self._buf) >= self._max_hold:
            released = self._buf
            self._buf = ""
            self._gate_passed = True
            return GateDecision(True, released, False)

        return GateDecision(False, "", False)

    def flush(self) -> str:
        tail = self._buf
        self._buf = ""
        self._gate_passed = True
        return tail


# ═══════════════════════════════════════════════════════════════════════════
# Audit
# ═══════════════════════════════════════════════════════════════════════════
def audit() -> dict[str, Any]:
    return {
        "version": _VERSION,
        "base_temp_intents": sorted(_BASE_TEMP),
        "ledger_sessions": LEDGER.size(),
        "ledger_max_entries_per_session": _MAX_LEDGER_ENTRIES,
        "stop_sequences_defined": len(_STOP_TABLE),
    }


__all__ = [
    "tune_temperature",
    "AnchorLedger", "LEDGER",
    "stops_for",
    "StreamQualityGate", "GateDecision",
    "audit",
>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
]