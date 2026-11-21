# core/adaptive.py
"""
AXELR — Adaptive Control (temp · anchors · stops · gate · hedge · canonical · trim)
====================================================================================
Five orthogonal quality knobs, all deterministic, all bounded.

    §1  Temperature tuning          tune_temperature(...)  → float
    §2  Session anchor ledger       LEDGER.observe / render
    §3  Stop sequences              stops_for(workspace, task_type)
    §4  Streaming quality gate      StreamQualityGate
    §5  Local-first hedge race      hedge_wrap(base_route)
    §6  Canonical byte-prefix       canonical_system(workspace, tier)
    §7  P95 max_tokens trim         adapt_max_tokens / record_token_ratio
    §8  Audit

Design:
    * Every primitive is deterministic and idempotent.
    * hedge_wrap is the ONLY function that wraps another callable — the
      old elite_orchestrator monkey-patched CircuitBreaker.record_failure;
      that patch is gone. Provider-failure propagation is now a
      constructor parameter on `CircuitBreaker._propagate` (see resilience.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger("axelr.adaptive")

_VERSION = "2.0"

# ═══════════════════════════════════════════════════════════════════════════
# §1 — Temperature tuning
# ═══════════════════════════════════════════════════════════════════════════
_BASE_TEMP: dict[str, float] = {
    "conversational": 0.55,
    "transactional": 0.20,
    "extraction": 0.00,
    "styling": 0.50,
    "strategy": 0.65,
    "diagnostic": 0.10,
}
_TASK_TEMP: dict[str, float] = {
    "frontend": 0.45,
    "extraction": 0.00,
    "touch_fix": 0.10,
    "structuring": 0.35,
}
_TIER_ADJ: dict[str, float] = {
    "trivial": -0.05,
    "standard": 0.00,
    "deep": +0.05,
    "critical": +0.05,
    "shed": -0.05,
    "lite": -0.02,
}
_MIN_TEMP, _MAX_TEMP = 0.0, 0.85


def tune_temperature(
    *,
    intent: str = "",
    task_type: str = "",
    tier: str = "",
    user_override: float | None = None,
    workspace: str = "",
) -> float:
    """Deterministic. Bounded [0.0, 0.85]. Same inputs → same temp."""
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
_MAX_LEDGER_ENTRIES = 40
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
    """Bounded thread-safe LRU of anchors per session."""

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
        if not session_id:
            return ""
        with self._lock:
            b = self._store.get(session_id)
            if not b:
                return ""
            ranked = sorted(b.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        lines = [f"• {a}" for a, _ in ranked]
        return (
            "<session_anchors>\n"
            "Identifiers, paths, and error strings referenced this "
            "session — preserve exact spelling.\n" + "\n".join(lines) + "\n</session_anchors>"
        )

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._store.pop(session_id, None)

    def size(self) -> int:
        with self._lock:
            return len(self._store)


LEDGER = AnchorLedger()


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Stop sequences
# ═══════════════════════════════════════════════════════════════════════════
_STOP_TABLE: dict[tuple[str, str], tuple[str, ...]] = {
    ("core", "structuring"): ("\n\nWould you like", "\n\nLet me know if"),
    ("data", "extraction"): ("\n\nThis data", "\n\nNote that"),
    ("design", "frontend"): ("\n\nThis component", "\n\nFeel free"),
    ("core", "touch_fix"): ("\n\nWould you like", "\n\nLet me know"),
}
_DEFAULT_STOPS: tuple[str, ...] = ("\n\nWould you like",)


def stops_for(workspace: str, task_type: str) -> tuple[str, ...]:
    return _STOP_TABLE.get((workspace, task_type), _DEFAULT_STOPS)


# ═══════════════════════════════════════════════════════════════════════════
# §4 — Streaming quality gate
# ═══════════════════════════════════════════════════════════════════════════
_REFUSAL_LEAD_RE = re.compile(
    r"^\s*(?:i\s+(?:can(?:'?t|not)|am\s+unable|won'?t)|"
    r"i'?m\s+sorry|as\s+an?\s+ai|i\s+apologize|unfortunately,?\s+i)",
    re.I,
)
_GOOD_LEAD_RE = re.compile(
    r"^\s*(?:here|below|the\s+\w+|sure|certainly|"
    r"```|#{1,6}\s|\d+[.)]\s|[-*•]\s|\w{3,})"
)


@dataclass(slots=True)
class GateDecision:
    emit: bool
    buffered: str
    force_retry: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"emit": self.emit, "force_retry": self.force_retry, "buf_len": len(self.buffered)}


class StreamQualityGate:
    """Buffer the first ~80 chars; suppress on refusal."""

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
        if _REFUSAL_LEAD_RE.match(self._buf):
            self._buf = ""
            self._gate_passed = True
            return GateDecision(False, "", force_retry=True)
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
# §5 — Local-first hedge race
# ═══════════════════════════════════════════════════════════════════════════
_ENABLE_HEDGE = os.getenv("AXELR_ELITE_HEDGE", "true").lower() in ("1", "true", "yes")
_HEDGE_LOCAL_MS = float(os.getenv("AXELR_ELITE_HEDGE_LOCAL_MS", "450"))


def hedge_wrap(
    base_route: Callable[..., Awaitable[dict[str, Any]]],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Wrap route_fn. For verifiable tasks, race local vs base."""
    if getattr(base_route, "_axelr_elite_hedge", False):
        return base_route
    if not _ENABLE_HEDGE:
        return base_route

    async def _hedged(
        workspace: str,
        task_type: str,
        prompt: str,
        history: Any,
        files: Any,
        max_tokens: int,
        temp: float,
        tier: str,
        user: dict[str, Any] | None = None,
        context: str = "",
        request: Any = None,
    ) -> dict[str, Any]:
        try:
            from .structured_gen import classify as _classify

            decision = _classify(prompt, files, intent=task_type, tier=tier or "free", workspace=workspace)
        except Exception:
            decision = None

        if decision is None or getattr(decision, "route", "api") != "local":
            return await base_route(
                workspace, task_type, prompt, history, files, max_tokens, temp, tier, user, context, request
            )

        local_task = asyncio.create_task(_local_attempt(workspace, task_type, prompt, decision), name="hedge_local")
        base_task = asyncio.create_task(
            base_route(workspace, task_type, prompt, history, files, max_tokens, temp, tier, user, context, request),
            name="hedge_remote",
        )

        try:
            done, _ = await asyncio.wait(
                [local_task], timeout=_HEDGE_LOCAL_MS / 1000.0, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            if not base_task.done():
                base_task.cancel()
            raise

        if done:
            local_result: dict[str, Any] | None = None
            with contextlib.suppress(Exception):
                local_result = local_task.result()
            if local_result is not None and local_result.get("success"):
                if not base_task.done():
                    base_task.cancel()
                with contextlib.suppress(Exception):
                    await base_task
                # Update Prometheus local wins counter (GAP4)
                from core.model_specialization import _HEDGE_LOCAL_WINS

                if _HEDGE_LOCAL_WINS is not None:
                    with contextlib.suppress(Exception):
                        _HEDGE_LOCAL_WINS.inc()
                local_result.setdefault("router", decision.to_dict() if hasattr(decision, "to_dict") else {})
                local_result.setdefault("hedge", "local_win")
                return local_result

        try:
            result = await base_task
        finally:
            if not local_task.done():
                local_task.cancel()
                with contextlib.suppress(Exception):
                    await local_task
        if isinstance(result, dict):
            result.setdefault("router", decision.to_dict() if hasattr(decision, "to_dict") else {})
            result.setdefault("hedge", "remote_won")
        return result

    _hedged._axelr_elite_hedge = True  # type: ignore[attr-defined]
    _hedged._axelr_original = base_route  # type: ignore[attr-defined]
    return _hedged


async def _local_attempt(
    workspace: str,
    task_type: str,
    prompt: str,
    decision: Any,
) -> dict[str, Any] | None:
    try:
        from .twin_engine import execute_local_inference
    except Exception:
        return None
    try:
        text = await asyncio.wait_for(
            execute_local_inference(workspace, task_type, prompt), timeout=max(0.3, _HEDGE_LOCAL_MS / 1000.0)
        )
    except (asyncio.TimeoutError, Exception):
        return None
    if not text or text.lstrip().startswith("[stub]"):
        return None
    try:
        from .structured_gen import verify as _verify

        v = _verify(text, kind=getattr(decision, "verifier_kind", "none"), prompt=prompt)
    except Exception:
        return None
    # Degraded verdicts = verifier didn't actually run. Not a pass.
    if not v.passed or getattr(v, "degraded", False):
        return None
    return {
        "success": True,
        "text": text,
        "provider": "local_twin",
        "model_used": f"local:{getattr(decision, 'task_class', 'unknown')}",
        "tokens_used": len(text.split()),
        "latency_ms": 0.0,
        "verifier": v.to_dict() if hasattr(v, "to_dict") else {},
    }


# ═══════════════════════════════════════════════════════════════════════════
# §6 — Canonical system prefix (byte-stable per workspace+tier)
# ═══════════════════════════════════════════════════════════════════════════
_ENABLE_CANON = os.getenv("AXELR_ELITE_CANONICAL_PREFIX", "true").lower() in ("1", "true", "yes")
_CANON_CACHE: dict[tuple[str, str], str] = {}
_CANON_LOCK = threading.Lock()


def canonical_system(workspace: str, tier: str) -> str:
    """Byte-stable system directive per (workspace, tier)."""
    if not _ENABLE_CANON:
        return f"<workspace_anchor>{workspace or 'core'}</workspace_anchor>\n<tier>{tier or 'free'}</tier>"
    key = (workspace or "core", tier or "free")
    with _CANON_LOCK:
        cached = _CANON_CACHE.get(key)
        if cached is not None:
            return cached
        text = ""
        try:
            from .context import build as _vault_build

            text = (
                _vault_build(
                    workspace=workspace or "core", intent="generic", sub_intent="", prompt="", include_drift=True
                )
                or ""
            )
        except Exception:
            text = ""
        if not text:
            text = f"<workspace_anchor>{workspace or 'core'}</workspace_anchor>\n<tier>{tier or 'free'}</tier>"
        _CANON_CACHE[key] = text
        return text


# ═══════════════════════════════════════════════════════════════════════════
# §7 — P95-based max_tokens trim
# ═══════════════════════════════════════════════════════════════════════════
_ENABLE_TRIM = os.getenv("AXELR_ELITE_TOKEN_TRIM", "true").lower() in ("1", "true", "yes")
_TRIM_MIN_SAMPLES = int(os.getenv("AXELR_ELITE_TRIM_MIN_N", "8"))
_TRIM_HEADROOM = float(os.getenv("AXELR_ELITE_TRIM_HEADROOM", "1.30"))
_TOKEN_HISTORY: dict[tuple[str, str], list[int]] = {}


def adapt_max_tokens(provider: str, workspace: str, requested: int) -> int:
    if not _ENABLE_TRIM:
        return requested
    key = (provider, workspace or "core")
    hist = _TOKEN_HISTORY.get(key, [])
    if len(hist) < _TRIM_MIN_SAMPLES:
        return requested
    sorted_h = sorted(hist)
    p95 = sorted_h[int(len(sorted_h) * 0.95)]
    adapted = min(requested, int(p95 * _TRIM_HEADROOM))
    return max(adapted, 256)


def record_token_ratio(
    provider: str,
    workspace: str,
    output_tokens: int,
    max_tokens: int,
) -> None:
    if max_tokens <= 0 or output_tokens <= 0:
        return
    key = (provider, workspace or "core")
    hist = _TOKEN_HISTORY.setdefault(key, [])
    if len(hist) >= 100:
        hist.pop(0)
    hist.append(int(output_tokens))


# ═══════════════════════════════════════════════════════════════════════════
# §8 — Audit
# ═══════════════════════════════════════════════════════════════════════════
def audit() -> dict[str, Any]:
    return {
        "version": _VERSION,
        "hedge": _ENABLE_HEDGE,
        "canonical_prefix": _ENABLE_CANON,
        "token_trim": _ENABLE_TRIM,
        "ledger_sessions": LEDGER.size(),
        "canon_cache_size": len(_CANON_CACHE),
        "token_ratio_tables": len(_TOKEN_HISTORY),
        "notes": {
            "install_health_propagation": "REMOVED — propagation is now a constructor parameter on CircuitBreaker",
            "install_negative_cache": "REMOVED — see resilience.HotSwapDetector cooldown handling",
        },
    }


__all__ = [
    # Temperature
    "tune_temperature",
    # Anchors
    "AnchorLedger",
    "LEDGER",
    # Stops
    "stops_for",
    # Gate
    "StreamQualityGate",
    "GateDecision",
    # Hedge
    "hedge_wrap",
    # Canonical
    "canonical_system",
    # Trim
    "adapt_max_tokens",
    "record_token_ratio",
    # Audit
    "audit",
]
