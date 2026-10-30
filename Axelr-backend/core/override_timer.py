<<<<<<< HEAD
# core/override_timer.py
"""
AXELR — Polite Advisory Override Timer

Prompt 2 §2: "If live memory analytics return a critical deficit
(<150 MB Free RAM), display the structural System Resource Advisory
overlay with a firm countdown clock."

This module generates the structured payload the frontend uses to
render the modal, and tracks whether the user proceeded or aborted.
The backend does not render UI; it supplies the contract.

Public:
    issue(ram_free_mb, session_id, seconds=5) -> dict
    should_proceed(decision_id) -> bool
    record_decision(decision_id, proceed: bool) -> None
    stats() -> dict

Never raises.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("axelr.override_timer")

_DEFAULT_SECONDS = int(os.getenv("AXELR_OVERRIDE_SECONDS", "5"))
_DECISION_TTL_S = int(os.getenv("AXELR_OVERRIDE_TTL_S", "300"))
_MAX_PENDING = int(os.getenv("AXELR_OVERRIDE_MAX_PENDING", "512"))

_LOCK = threading.Lock()
_PENDING: dict[str, "_Decision"] = {}
_STATS = {"issued": 0, "proceeded": 0, "aborted": 0, "expired": 0}


@dataclass(slots=True)
class _Decision:
    decision_id: str
    session_id: str
    ram_free_mb: int
    seconds: int
    issued_at: float
    proceed: bool | None = None
    decided_at: float = 0.0


def _gc_locked() -> None:
    now = time.time()
    stale = [k for k, d in _PENDING.items()
             if (now - d.issued_at) > _DECISION_TTL_S]
    for k in stale:
        _PENDING.pop(k, None)
        _STATS["expired"] += 1
    if len(_PENDING) > _MAX_PENDING:
        ordered = sorted(_PENDING.items(), key=lambda kv: kv[1].issued_at)
        for k, _ in ordered[: len(_PENDING) - _MAX_PENDING]:
            _PENDING.pop(k, None)
            _STATS["expired"] += 1


def issue(ram_free_mb: int, session_id: str = "",
          *, seconds: int | None = None) -> dict[str, Any]:
    secs = int(seconds if seconds is not None
               else _DEFAULT_SECONDS)
    secs = max(1, min(secs, 30))
    did = uuid.uuid4().hex[:16]
    with _LOCK:
        _gc_locked()
        _PENDING[did] = _Decision(
            decision_id=did, session_id=session_id,
            ram_free_mb=int(ram_free_mb), seconds=secs,
            issued_at=time.time(),
        )
        _STATS["issued"] += 1
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
            {"id": "proceed", "label": f"Proceed anyway ({secs}s)",
             "primary": True},
        ],
        "auto_proceed_on_timeout": False,
        "issued_at": time.time(),
    }


def record_decision(decision_id: str, proceed: bool) -> None:
    with _LOCK:
        d = _PENDING.get(decision_id)
        if d is None:
            return
        d.proceed = bool(proceed)
        d.decided_at = time.time()
        _STATS["proceeded" if proceed else "aborted"] += 1


def should_proceed(decision_id: str) -> bool:
    with _LOCK:
        d = _PENDING.get(decision_id)
        if d is None or d.proceed is None:
            return False
        return d.proceed


def stats() -> dict[str, Any]:
    with _LOCK:
        pending = len(_PENDING)
        return {
            "pending": pending,
            "max_pending": _MAX_PENDING,
            "default_seconds": _DEFAULT_SECONDS,
            "ttl_s": _DECISION_TTL_S,
            **dict(_STATS),
        }


def audit() -> dict[str, Any]:
    return stats()


__all__ = [
    "issue", "record_decision", "should_proceed", "stats", "audit",
=======
# core/override_timer.py
"""
AXELR — Polite Advisory Override Timer

Prompt 2 §2: "If live memory analytics return a critical deficit
(<150 MB Free RAM), display the structural System Resource Advisory
overlay with a firm countdown clock."

This module generates the structured payload the frontend uses to
render the modal, and tracks whether the user proceeded or aborted.
The backend does not render UI; it supplies the contract.

Public:
    issue(ram_free_mb, session_id, seconds=5) -> dict
    should_proceed(decision_id) -> bool
    record_decision(decision_id, proceed: bool) -> None
    stats() -> dict

Never raises.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("axelr.override_timer")

_DEFAULT_SECONDS = int(os.getenv("AXELR_OVERRIDE_SECONDS", "5"))
_DECISION_TTL_S = int(os.getenv("AXELR_OVERRIDE_TTL_S", "300"))
_MAX_PENDING = int(os.getenv("AXELR_OVERRIDE_MAX_PENDING", "512"))

_LOCK = threading.Lock()
_PENDING: dict[str, "_Decision"] = {}
_STATS = {"issued": 0, "proceeded": 0, "aborted": 0, "expired": 0}


@dataclass(slots=True)
class _Decision:
    decision_id: str
    session_id: str
    ram_free_mb: int
    seconds: int
    issued_at: float
    proceed: bool | None = None
    decided_at: float = 0.0


def _gc_locked() -> None:
    now = time.time()
    stale = [k for k, d in _PENDING.items()
             if (now - d.issued_at) > _DECISION_TTL_S]
    for k in stale:
        _PENDING.pop(k, None)
        _STATS["expired"] += 1
    if len(_PENDING) > _MAX_PENDING:
        ordered = sorted(_PENDING.items(), key=lambda kv: kv[1].issued_at)
        for k, _ in ordered[: len(_PENDING) - _MAX_PENDING]:
            _PENDING.pop(k, None)
            _STATS["expired"] += 1


def issue(ram_free_mb: int, session_id: str = "",
          *, seconds: int | None = None) -> dict[str, Any]:
    secs = int(seconds if seconds is not None
               else _DEFAULT_SECONDS)
    secs = max(1, min(secs, 30))
    did = uuid.uuid4().hex[:16]
    with _LOCK:
        _gc_locked()
        _PENDING[did] = _Decision(
            decision_id=did, session_id=session_id,
            ram_free_mb=int(ram_free_mb), seconds=secs,
            issued_at=time.time(),
        )
        _STATS["issued"] += 1
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
            {"id": "proceed", "label": f"Proceed anyway ({secs}s)",
             "primary": True},
        ],
        "auto_proceed_on_timeout": False,
        "issued_at": time.time(),
    }


def record_decision(decision_id: str, proceed: bool) -> None:
    with _LOCK:
        d = _PENDING.get(decision_id)
        if d is None:
            return
        d.proceed = bool(proceed)
        d.decided_at = time.time()
        _STATS["proceeded" if proceed else "aborted"] += 1


def should_proceed(decision_id: str) -> bool:
    with _LOCK:
        d = _PENDING.get(decision_id)
        if d is None or d.proceed is None:
            return False
        return d.proceed


def stats() -> dict[str, Any]:
    with _LOCK:
        pending = len(_PENDING)
        return {
            "pending": pending,
            "max_pending": _MAX_PENDING,
            "default_seconds": _DEFAULT_SECONDS,
            "ttl_s": _DECISION_TTL_S,
            **dict(_STATS),
        }


def audit() -> dict[str, Any]:
    return stats()


__all__ = [
    "issue", "record_decision", "should_proceed", "stats", "audit",
>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
]