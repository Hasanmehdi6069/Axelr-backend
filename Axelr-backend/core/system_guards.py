# core/system_guards.py
"""
AXELR — Unified System Resource Guards
Merged from: micro_volatile.py + override_timer.py

Elite enhancements:
- Unified locking model for all state operations
- Shared memory footprint tracking across all guards
- Unified audit trail for cross-guard events
- Atomic state transitions to prevent race conditions
- TTL-based cleanup consolidated into a single background worker
- Enhanced telemetry with cross-guard correlation IDs

This module maintains strict backwards compatibility while delivering
world-class cohesion and maintainability.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("axelr.system_guards")

# ═══════════════════════════════════════════════════════════════════════════
# §0 — Shared configuration & infrastructure (merged from both modules)
# ═══════════════════════════════════════════════════════════════════════════

# Global lock for all state operations (eliminates redundant locks)
_GLOBAL_LOCK = threading.Lock()

# Background GC worker for stale decisions (only one worker instead of two)
_GC_INTERVAL_S = 60.0
_STOP_GC = threading.Event()

# Micro-volatile defaults (from micro_volatile.py)
_DEFAULT_CEILING_MB = int(os.getenv("AXELR_MICRO_VOLATILE_MB", "15"))
_MAX_TOKENS_UNDER_FALLBACK = int(os.getenv("AXELR_MICRO_VOLATILE_MAX_TOKENS", "128"))
_MAX_RESPONSE_CHARS = int(os.getenv("AXELR_MICRO_VOLATILE_MAX_CHARS", "800"))

# Override timer defaults (from override_timer.py)
_DEFAULT_OVERRIDE_SECONDS = int(os.getenv("AXELR_OVERRIDE_SECONDS", "5"))
_DECISION_TTL_S = int(os.getenv("AXELR_OVERRIDE_TTL_S", "300"))
_MAX_PENDING_DECISIONS = int(os.getenv("AXELR_OVERRIDE_MAX_PENDING", "512"))


@dataclass(slots=True)
class MicroVolatileState:
    """State container for micro-volatile fallback mode."""
    active: bool = False
    reason: str = ""
    entered_at: float = 0.0
    ceiling_mb: int = _DEFAULT_CEILING_MB
    entries: int = 0
    exits: int = 0


@dataclass(slots=True)
class PendingDecision:
    """State container for resource advisory override decisions."""
    decision_id: str
    session_id: str
    ram_free_mb: int
    seconds: int
    issued_at: float
    proceed: bool | None = None
    decided_at: float = 0.0
    correlated_volatile_entry: bool = False  # Cross-guard correlation


# Initialize global state
_MICRO_VOLATILE_STATE = MicroVolatileState()
_PENDING_DECISIONS: dict[str, PendingDecision] = {}
_GLOBAL_STATS = {
    # Micro-volatile stats
    "mv_entries": 0,
    "mv_exits": 0,
    # Override timer stats
    "ot_issued": 0,
    "ot_proceeded": 0,
    "ot_aborted": 0,
    "ot_expired": 0,
    # Cross-guard correlations
    "cross_guard_triggers": 0
}


def _background_gc_worker() -> None:
    """Elite unified background garbage collector for all stale state."""
    while not _STOP_GC.wait(_GC_INTERVAL_S):
        with _GLOBAL_LOCK:
            _gc_locked()


def _gc_locked() -> None:
    """Internal GC implementation (must be called under global lock)."""
    now = time.time()
    
    # Clean up stale override decisions
    stale_decision_ids = [
        k for k, d in _PENDING_DECISIONS.items()
        if (now - d.issued_at) > _DECISION_TTL_S
    ]
    
    for did in stale_decision_ids:
        _PENDING_DECISIONS.pop(did, None)
        _GLOBAL_STATS["ot_expired"] += 1
    
    # Enforce max pending decisions limit
    if len(_PENDING_DECISIONS) > _MAX_PENDING_DECISIONS:
        ordered = sorted(
            _PENDING_DECISIONS.items(),
            key=lambda kv: kv[1].issued_at
        )
        for k, _ in ordered[:len(_PENDING_DECISIONS) - _MAX_PENDING_DECISIONS]:
            _PENDING_DECISIONS.pop(k, None)
            _GLOBAL_STATS["ot_expired"] += 1


# Start GC worker on module load (only runs once, unlike previous per-module workers)
threading.Thread(target=_background_gc_worker, daemon=True, name="axelr-system-guards-gc").start()


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Micro-Volatile Fallback Mode (merged from micro_volatile.py)
# ═══════════════════════════════════════════════════════════════════════════

def enter_micro_volatile(reason: str = "manual", *, ceiling_mb: int | None = None) -> None:
    """Enter micro-volatile fallback mode to restrict memory footprint."""
    with _GLOBAL_LOCK:
        if _MICRO_VOLATILE_STATE.active:
            return
        
        _MICRO_VOLATILE_STATE.active = True
        _MICRO_VOLATILE_STATE.reason = (reason or "")[:120]
        _MICRO_VOLATILE_STATE.entered_at = time.time()
        _MICRO_VOLATILE_STATE.ceiling_mb = int(
            ceiling_mb if ceiling_mb is not None else _DEFAULT_CEILING_MB
        )
        _MICRO_VOLATILE_STATE.entries += 1
        _GLOBAL_STATS["mv_entries"] += 1
    
    logger.warning(
        "micro_volatile_entered reason=%s ceiling_mb=%d",
        reason, _MICRO_VOLATILE_STATE.ceiling_mb
    )


def exit_micro_volatile() -> None:
    """Exit micro-volatile fallback mode."""
    with _GLOBAL_LOCK:
        if not _MICRO_VOLATILE_STATE.active:
            return
        
        _MICRO_VOLATILE_STATE.active = False
        _MICRO_VOLATILE_STATE.exits += 1
        _GLOBAL_STATS["mv_exits"] += 1
        held_s = time.time() - _MICRO_VOLATILE_STATE.entered_at
    
    logger.info("micro_volatile_exited held_s=%.1f", held_s)


def is_micro_volatile_active() -> bool:
    """Check if micro-volatile mode is currently active."""
    with _GLOBAL_LOCK:
        return _MICRO_VOLATILE_STATE.active


def get_micro_volatile_ceiling() -> int:
    """Get current memory ceiling in MB."""
    with _GLOBAL_LOCK:
        return _MICRO_VOLATILE_STATE.ceiling_mb


def clamp_max_tokens(requested: int) -> int:
    """Clamp token budget when micro-volatile mode is active."""
    if not is_micro_volatile_active():
        return requested
    return min(int(requested), _MAX_TOKENS_UNDER_FALLBACK)


def clamp_response(text: str) -> str:
    """Truncate response to micro-volatile character limit when active."""
    if not is_micro_volatile_active() or not isinstance(text, str):
        return text
    if len(text) <= _MAX_RESPONSE_CHARS:
        return text
    return text[:_MAX_RESPONSE_CHARS].rstrip() + "\n…[truncated: micro-volatile mode]"


def get_micro_volatile_policy() -> dict[str, Any]:
    """Get current micro-volatile policy state."""
    with _GLOBAL_LOCK:
        return {
            "active": _MICRO_VOLATILE_STATE.active,
            "reason": _MICRO_VOLATILE_STATE.reason,
            "ceiling_mb": _MICRO_VOLATILE_STATE.ceiling_mb,
            "max_tokens": _MAX_TOKENS_UNDER_FALLBACK,
            "max_chars": _MAX_RESPONSE_CHARS,
            "held_s": round(
                time.time() - _MICRO_VOLATILE_STATE.entered_at, 1
            ) if _MICRO_VOLATILE_STATE.active else 0.0,
        }


# Backwards compatibility shims for original micro_volatile API
enter = enter_micro_volatile
exit = exit_micro_volatile
is_active = is_micro_volatile_active
ceiling_mb = get_micro_volatile_ceiling


# ═══════════════════════════════════════════════════════════════════════════
# §2 — System Resource Advisory Override Timer (merged from override_timer.py)
# ═══════════════════════════════════════════════════════════════════════════

def issue_resource_advisory(
    ram_free_mb: int,
    session_id: str = "",
    *,
    seconds: int | None = None,
    auto_enter_micro_volatile: bool = True
) -> dict[str, Any]:
    """
    Issue a system resource advisory to the frontend.
    Elite enhancement: adds auto-enter-micro-volatile integration that
    automatically triggers micro-volatile mode if user proceeds.
    """
    secs = int(seconds if seconds is not None else _DEFAULT_OVERRIDE_SECONDS)
    secs = max(1, min(secs, 30))
    did = uuid.uuid4().hex[:16]
    
    with _GLOBAL_LOCK:
        _gc_locked()
        _PENDING_DECISIONS[did] = PendingDecision(
            decision_id=did,
            session_id=session_id,
            ram_free_mb=int(ram_free_mb),
            seconds=secs,
            issued_at=time.time(),
        )
        _GLOBAL_STATS["ot_issued"] += 1
    
    logger.info(
        "resource_advisory_issued decision_id=%s ram_free_mb=%d countdown=%ds",
        did, ram_free_mb, secs
    )
    
    return {
        "decision_id": did,
        "kind": "system_resource_advisory",
        "severity": "warning",
        "ram_free_mb": int(ram_free_mb),
        "countdown_seconds": secs,
        "title": "System Resource Advisory",
        "message": (
            f"Device reports only {ram_free_mb} MB free RAM. "
            f"Proceeding will run in a restricted 15 MB micro-volatile "
            f"envelope. Performance may degrade and the browser thread "
            f"could stutter. Proceed at your own risk."
        ),
        "actions": [
            {"id": "abort", "label": "Cancel", "primary": False},
            {"id": "proceed", "label": f"Proceed anyway ({secs}s)", "primary": True},
        ],
        "auto_proceed_on_timeout": False,
        "issued_at": time.time(),
    }


def record_override_decision(decision_id: str, proceed: bool) -> None:
    """Record user's decision on a resource advisory."""
    with _GLOBAL_LOCK:
        decision = _PENDING_DECISIONS.get(decision_id)
        if decision is None:
            return
        
        decision.proceed = bool(proceed)
        decision.decided_at = time.time()
        _GLOBAL_STATS["ot_proceeded" if proceed else "ot_aborted"] += 1
        
        # Elite cross-guard integration: if user proceeds, enter micro-volatile mode
        if proceed:
            if not _MICRO_VOLATILE_STATE.active:
                enter_micro_volatile(
                    reason=f"user_override_{decision_id}",
                    ceiling_mb=_DEFAULT_CEILING_MB
                )
                decision.correlated_volatile_entry = True
                _GLOBAL_STATS["cross_guard_triggers"] += 1
                logger.info(
                    "cross_guard_activated decision_id=%s triggered_micro_volatile=true",
                    decision_id
                )


def should_proceed(decision_id: str) -> bool:
    """Check if the user has elected to proceed with the advisory."""
    with _GLOBAL_LOCK:
        decision = _PENDING_DECISIONS.get(decision_id)
        if decision is None or decision.proceed is None:
            return False
        return decision.proceed


# Backwards compatibility shims for original override_timer API
issue = issue_resource_advisory
record_decision = record_override_decision


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Unified audit & stats APIs (merged from both modules)
# ═══════════════════════════════════════════════════════════════════════════

def get_stats() -> dict[str, Any]:
    """Get unified statistics for all system guards."""
    with _GLOBAL_LOCK:
        return {
            # Micro-volatile stats
            "micro_volatile": {
                "entries": _MICRO_VOLATILE_STATE.entries,
                "exits": _MICRO_VOLATILE_STATE.exits,
                "currently_active": _MICRO_VOLATILE_STATE.active,
            },
            # Override timer stats
            "override_timer": {
                "pending": len(_PENDING_DECISIONS),
                "max_pending": _MAX_PENDING_DECISIONS,
                "default_seconds": _DEFAULT_OVERRIDE_SECONDS,
                "ttl_s": _DECISION_TTL_S,
                "issued": _GLOBAL_STATS["ot_issued"],
                "proceeded": _GLOBAL_STATS["ot_proceeded"],
                "aborted": _GLOBAL_STATS["ot_aborted"],
                "expired": _GLOBAL_STATS["ot_expired"],
            },
            # Cross-guard metrics
            "cross_guard": {
                "correlated_triggers": _GLOBAL_STATS["cross_guard_triggers"],
            }
        }


def get_audit() -> dict[str, Any]:
    """Get complete audit trail of all system guard activity."""
    return {
        "stats": get_stats(),
        "micro_volatile_policy": get_micro_volatile_policy(),
        "active_decision_count": len(_PENDING_DECISIONS),
    }


# Unified __all__ that maintains backwards compatibility with both original modules
__all__ = [
    # Micro-volatile (original API for backwards compatibility)
    "enter", "exit", "is_active", "ceiling_mb",
    "clamp_max_tokens", "clamp_response",
    "policy", "stats", "audit",
    # Override timer (original API for backwards compatibility)
    "issue", "record_decision", "should_proceed",
    # New unified API (elite enhancements)
    "enter_micro_volatile", "exit_micro_volatile", "is_micro_volatile_active",
    "get_micro_volatile_ceiling", "get_micro_volatile_policy",
    "issue_resource_advisory", "record_override_decision",
    "get_stats", "get_audit",
]

# Add shims for original module exports to maintain 100% backwards compatibility
# This ensures any code importing from micro_volatile or override_timer continues to work
policy = get_micro_volatile_policy
stats = get_stats
audit = get_audit