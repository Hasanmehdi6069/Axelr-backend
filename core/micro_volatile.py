<<<<<<< HEAD
# core/micro_volatile.py
"""
AXELR — Micro-Volatile Fallback Mode

Prompt 2 §2: "deploy the generation loop in a micro-volatile layer
footprint restricted strictly under a flat 15 MB boundary."

This module does not itself allocate memory. It enforces a policy
flag that downstream code (twin engine, provider cascade) reads to
degrade output complexity and refuse local inference.

Public:
    enter(reason, *, ceiling_mb=15) -> None
    exit() -> None
    is_active() -> bool
    ceiling_mb() -> int
    policy() -> dict
    stats() -> dict

Never raises.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("axelr.micro_volatile")

_DEFAULT_CEILING = int(os.getenv("AXELR_MICRO_VOLATILE_MB", "15"))
_MAX_TOKENS_UNDER_FALLBACK = int(
    os.getenv("AXELR_MICRO_VOLATILE_MAX_TOKENS", "128"))
_MAX_RESPONSE_CHARS = int(
    os.getenv("AXELR_MICRO_VOLATILE_MAX_CHARS", "800"))


@dataclass(slots=True)
class _State:
    active: bool = False
    reason: str = ""
    entered_at: float = 0.0
    ceiling_mb: int = _DEFAULT_CEILING
    entries: int = 0
    exits: int = 0


_LOCK = threading.Lock()
_STATE = _State()


def enter(reason: str = "manual", *,
          ceiling_mb: int | None = None) -> None:
    with _LOCK:
        if _STATE.active:
            return
        _STATE.active = True
        _STATE.reason = (reason or "")[:120]
        _STATE.entered_at = time.time()
        _STATE.ceiling_mb = int(
            ceiling_mb if ceiling_mb is not None else _DEFAULT_CEILING)
        _STATE.entries += 1
    logger.warning("micro_volatile_entered reason=%s ceiling_mb=%d",
                   reason, _STATE.ceiling_mb)


def exit() -> None:
    with _LOCK:
        if not _STATE.active:
            return
        _STATE.active = False
        _STATE.exits += 1
        held_s = time.time() - _STATE.entered_at
    logger.info("micro_volatile_exited held_s=%.1f", held_s)


def is_active() -> bool:
    with _LOCK:
        return _STATE.active


def ceiling_mb() -> int:
    with _LOCK:
        return _STATE.ceiling_mb


def clamp_max_tokens(requested: int) -> int:
    """Downstream code calls this to clamp token budget when active."""
    if not is_active():
        return requested
    return min(int(requested), _MAX_TOKENS_UNDER_FALLBACK)


def clamp_response(text: str) -> str:
    """Truncate the response to the micro-volatile ceiling when active."""
    if not is_active() or not isinstance(text, str):
        return text
    if len(text) <= _MAX_RESPONSE_CHARS:
        return text
    return text[:_MAX_RESPONSE_CHARS].rstrip() + "\n…[truncated: micro-volatile mode]"


def policy() -> dict[str, Any]:
    with _LOCK:
        return {
            "active": _STATE.active,
            "reason": _STATE.reason,
            "ceiling_mb": _STATE.ceiling_mb,
            "max_tokens": _MAX_TOKENS_UNDER_FALLBACK,
            "max_chars": _MAX_RESPONSE_CHARS,
            "held_s": round(time.time() - _STATE.entered_at, 1)
                if _STATE.active else 0.0,
        }


def stats() -> dict[str, Any]:
    with _LOCK:
        return {
            "entries": _STATE.entries,
            "exits": _STATE.exits,
            "currently_active": _STATE.active,
        }


def audit() -> dict[str, Any]:
    return {**stats(), "policy": policy()}


__all__ = [
    "enter", "exit", "is_active", "ceiling_mb",
    "clamp_max_tokens", "clamp_response",
    "policy", "stats", "audit",
=======
# core/micro_volatile.py
"""
AXELR — Micro-Volatile Fallback Mode

Prompt 2 §2: "deploy the generation loop in a micro-volatile layer
footprint restricted strictly under a flat 15 MB boundary."

This module does not itself allocate memory. It enforces a policy
flag that downstream code (twin engine, provider cascade) reads to
degrade output complexity and refuse local inference.

Public:
    enter(reason, *, ceiling_mb=15) -> None
    exit() -> None
    is_active() -> bool
    ceiling_mb() -> int
    policy() -> dict
    stats() -> dict

Never raises.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("axelr.micro_volatile")

_DEFAULT_CEILING = int(os.getenv("AXELR_MICRO_VOLATILE_MB", "15"))
_MAX_TOKENS_UNDER_FALLBACK = int(
    os.getenv("AXELR_MICRO_VOLATILE_MAX_TOKENS", "128"))
_MAX_RESPONSE_CHARS = int(
    os.getenv("AXELR_MICRO_VOLATILE_MAX_CHARS", "800"))


@dataclass(slots=True)
class _State:
    active: bool = False
    reason: str = ""
    entered_at: float = 0.0
    ceiling_mb: int = _DEFAULT_CEILING
    entries: int = 0
    exits: int = 0


_LOCK = threading.Lock()
_STATE = _State()


def enter(reason: str = "manual", *,
          ceiling_mb: int | None = None) -> None:
    with _LOCK:
        if _STATE.active:
            return
        _STATE.active = True
        _STATE.reason = (reason or "")[:120]
        _STATE.entered_at = time.time()
        _STATE.ceiling_mb = int(
            ceiling_mb if ceiling_mb is not None else _DEFAULT_CEILING)
        _STATE.entries += 1
    logger.warning("micro_volatile_entered reason=%s ceiling_mb=%d",
                   reason, _STATE.ceiling_mb)


def exit() -> None:
    with _LOCK:
        if not _STATE.active:
            return
        _STATE.active = False
        _STATE.exits += 1
        held_s = time.time() - _STATE.entered_at
    logger.info("micro_volatile_exited held_s=%.1f", held_s)


def is_active() -> bool:
    with _LOCK:
        return _STATE.active


def ceiling_mb() -> int:
    with _LOCK:
        return _STATE.ceiling_mb


def clamp_max_tokens(requested: int) -> int:
    """Downstream code calls this to clamp token budget when active."""
    if not is_active():
        return requested
    return min(int(requested), _MAX_TOKENS_UNDER_FALLBACK)


def clamp_response(text: str) -> str:
    """Truncate the response to the micro-volatile ceiling when active."""
    if not is_active() or not isinstance(text, str):
        return text
    if len(text) <= _MAX_RESPONSE_CHARS:
        return text
    return text[:_MAX_RESPONSE_CHARS].rstrip() + "\n…[truncated: micro-volatile mode]"


def policy() -> dict[str, Any]:
    with _LOCK:
        return {
            "active": _STATE.active,
            "reason": _STATE.reason,
            "ceiling_mb": _STATE.ceiling_mb,
            "max_tokens": _MAX_TOKENS_UNDER_FALLBACK,
            "max_chars": _MAX_RESPONSE_CHARS,
            "held_s": round(time.time() - _STATE.entered_at, 1)
                if _STATE.active else 0.0,
        }


def stats() -> dict[str, Any]:
    with _LOCK:
        return {
            "entries": _STATE.entries,
            "exits": _STATE.exits,
            "currently_active": _STATE.active,
        }


def audit() -> dict[str, Any]:
    return {**stats(), "policy": policy()}


__all__ = [
    "enter", "exit", "is_active", "ceiling_mb",
    "clamp_max_tokens", "clamp_response",
    "policy", "stats", "audit",
>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
]