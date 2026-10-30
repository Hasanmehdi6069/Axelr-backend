<<<<<<< HEAD
# core/test_time_compute.py
"""
AXELR — Test-Time Compute (TTC) Dynamic Scaling
================================================
Frontier models spend compute during training. This module spends it
during inference — but only when the prompt actually needs it.

Design:
    * Trivial prompts → width=1 (single call, sub-second)
    * Standard prompts → width=1
    * Deep prompts → width=2 (race two providers)
    * Critical prompts → width=3

Width is decided by core.task_shedder.assess. The wrapper never exceeds
the caller's deadline and never raises. On any failure, falls through to
a single-shot call.

Env gate:
    AXELR_ENABLE_TTC=true   (default off — opt-in)

Public:
    decide_width(prompt, history, files, default) -> int
    run_with_ttc(route_fn, ...) -> dict
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger("axelr.ttc")

_ENABLED = os.getenv("AXELR_ENABLE_TTC", "false").lower() in (
    "1", "true", "yes")
_MAX_WIDTH = 3
_MIN_WIDTH = 1

_TIER_WIDTH = {
    "trivial":  1,
    "standard": 1,
    "deep":     2,
    "critical": 3,
}

RouteFn = Callable[..., Awaitable[dict[str, Any]]]


def decide_width(
    prompt: str,
    *,
    history: Any = None,
    files: Any = None,
    default: int = 1,
) -> int:
    """Return the TTC width for this prompt. Bounded [1, 3]. Never raises."""
    if not _ENABLED:
        return max(_MIN_WIDTH, min(_MAX_WIDTH, int(default)))
    try:
        from .task_shedder import assess
        c = assess(prompt, history=history, files=files)
        w = _TIER_WIDTH.get(c.tier, 1)
        return max(_MIN_WIDTH, min(_MAX_WIDTH, w))
    except Exception:
        return max(_MIN_WIDTH, min(_MAX_WIDTH, int(default)))


async def run_with_ttc(
    route_fn: RouteFn,
    *,
    workspace: str,
    task_type: str,
    prompt: str,
    history: Any,
    files: Any,
    max_tokens: int,
    temp: float,
    tier: str,
    user: dict | None,
    context: str,
    request: Any,
    force_width: int | None = None,
    timeout_s: float = 22.0,
) -> dict[str, Any]:
    """
    Run the request with TTC scaling. On width==1, exactly one route_fn
    call. On width>=2, delegates to consensus.race_consensus.
    """
    width = (int(force_width)
             if force_width is not None
             else decide_width(prompt, history=history, files=files, default=1))
    width = max(_MIN_WIDTH, min(_MAX_WIDTH, width))

    if width <= 1:
        try:
            r = await route_fn(
                workspace, task_type, prompt, history, files,
                max_tokens, temp, tier, user, context, request,
            )
            if isinstance(r, dict):
                r.setdefault("ttc", {"width": 1, "enabled": _ENABLED})
            return r if isinstance(r, dict) else {
                "success": False, "text": "", "provider": "none",
                "model_used": "", "ttc": {"width": 1, "enabled": _ENABLED},
            }
        except Exception as e:
            logger.debug("ttc_single_failed error=%s", e)
            return {"success": False, "text": "", "provider": "none",
                    "model_used": "", "error": str(e)[:200],
                    "ttc": {"width": 1, "enabled": _ENABLED}}

    # width >= 2 — race via consensus
    try:
        from .consensus import race_consensus
        t0 = time.monotonic()
        outcome = await race_consensus(
            route_fn,
            workspace=workspace, task_type=task_type, prompt=prompt,
            history=history, files=files,
            max_tokens=max_tokens, temp=temp, tier=tier,
            user=user, context=context, request=request,
            width=width, timeout_s=timeout_s,
            consensus_floor=0.55, prefer_non_stream=True,
        )
        elapsed_ms = (time.monotonic() - t0) * 1000
        result = dict(outcome.winner) if outcome else {}
        result.setdefault("ttc", {
            "width": width, "enabled": _ENABLED,
            "participants": outcome.participants if outcome else 0,
            "agreement": round(outcome.agreement, 3) if outcome else 0.0,
            "elapsed_ms": round(elapsed_ms, 1),
        })
        return result
    except Exception as e:
        logger.debug("ttc_race_failed_falling_back error=%s", e)
        try:
            r = await route_fn(
                workspace, task_type, prompt, history, files,
                max_tokens, temp, tier, user, context, request,
            )
            if isinstance(r, dict):
                r.setdefault("ttc", {"width": 1, "fallback": True,
                                     "enabled": _ENABLED})
                return r
        except Exception as e2:
            logger.debug("ttc_fallback_failed error=%s", e2)
        return {"success": False, "text": "", "provider": "none",
                "model_used": "", "error": str(e)[:200],
                "ttc": {"width": width, "enabled": _ENABLED, "error": True}}


def audit() -> dict[str, Any]:
    return {
        "enabled": _ENABLED,
        "max_width": _MAX_WIDTH,
        "tier_map": dict(_TIER_WIDTH),
    }


=======
# core/test_time_compute.py
"""
AXELR — Test-Time Compute (TTC) Dynamic Scaling
================================================
Frontier models spend compute during training. This module spends it
during inference — but only when the prompt actually needs it.

Design:
    * Trivial prompts → width=1 (single call, sub-second)
    * Standard prompts → width=1
    * Deep prompts → width=2 (race two providers)
    * Critical prompts → width=3

Width is decided by core.task_shedder.assess. The wrapper never exceeds
the caller's deadline and never raises. On any failure, falls through to
a single-shot call.

Env gate:
    AXELR_ENABLE_TTC=true   (default off — opt-in)

Public:
    decide_width(prompt, history, files, default) -> int
    run_with_ttc(route_fn, ...) -> dict
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger("axelr.ttc")

_ENABLED = os.getenv("AXELR_ENABLE_TTC", "false").lower() in (
    "1", "true", "yes")
_MAX_WIDTH = 3
_MIN_WIDTH = 1

_TIER_WIDTH = {
    "trivial":  1,
    "standard": 1,
    "deep":     2,
    "critical": 3,
}

RouteFn = Callable[..., Awaitable[dict[str, Any]]]


def decide_width(
    prompt: str,
    *,
    history: Any = None,
    files: Any = None,
    default: int = 1,
) -> int:
    """Return the TTC width for this prompt. Bounded [1, 3]. Never raises."""
    if not _ENABLED:
        return max(_MIN_WIDTH, min(_MAX_WIDTH, int(default)))
    try:
        from .task_shedder import assess
        c = assess(prompt, history=history, files=files)
        w = _TIER_WIDTH.get(c.tier, 1)
        return max(_MIN_WIDTH, min(_MAX_WIDTH, w))
    except Exception:
        return max(_MIN_WIDTH, min(_MAX_WIDTH, int(default)))


async def run_with_ttc(
    route_fn: RouteFn,
    *,
    workspace: str,
    task_type: str,
    prompt: str,
    history: Any,
    files: Any,
    max_tokens: int,
    temp: float,
    tier: str,
    user: dict | None,
    context: str,
    request: Any,
    force_width: int | None = None,
    timeout_s: float = 22.0,
) -> dict[str, Any]:
    """
    Run the request with TTC scaling. On width==1, exactly one route_fn
    call. On width>=2, delegates to consensus.race_consensus.
    """
    width = (int(force_width)
             if force_width is not None
             else decide_width(prompt, history=history, files=files, default=1))
    width = max(_MIN_WIDTH, min(_MAX_WIDTH, width))

    if width <= 1:
        try:
            r = await route_fn(
                workspace, task_type, prompt, history, files,
                max_tokens, temp, tier, user, context, request,
            )
            if isinstance(r, dict):
                r.setdefault("ttc", {"width": 1, "enabled": _ENABLED})
            return r if isinstance(r, dict) else {
                "success": False, "text": "", "provider": "none",
                "model_used": "", "ttc": {"width": 1, "enabled": _ENABLED},
            }
        except Exception as e:
            logger.debug("ttc_single_failed error=%s", e)
            return {"success": False, "text": "", "provider": "none",
                    "model_used": "", "error": str(e)[:200],
                    "ttc": {"width": 1, "enabled": _ENABLED}}

    # width >= 2 — race via consensus
    try:
        from .consensus import race_consensus
        t0 = time.monotonic()
        outcome = await race_consensus(
            route_fn,
            workspace=workspace, task_type=task_type, prompt=prompt,
            history=history, files=files,
            max_tokens=max_tokens, temp=temp, tier=tier,
            user=user, context=context, request=request,
            width=width, timeout_s=timeout_s,
            consensus_floor=0.55, prefer_non_stream=True,
        )
        elapsed_ms = (time.monotonic() - t0) * 1000
        result = dict(outcome.winner) if outcome else {}
        result.setdefault("ttc", {
            "width": width, "enabled": _ENABLED,
            "participants": outcome.participants if outcome else 0,
            "agreement": round(outcome.agreement, 3) if outcome else 0.0,
            "elapsed_ms": round(elapsed_ms, 1),
        })
        return result
    except Exception as e:
        logger.debug("ttc_race_failed_falling_back error=%s", e)
        try:
            r = await route_fn(
                workspace, task_type, prompt, history, files,
                max_tokens, temp, tier, user, context, request,
            )
            if isinstance(r, dict):
                r.setdefault("ttc", {"width": 1, "fallback": True,
                                     "enabled": _ENABLED})
                return r
        except Exception as e2:
            logger.debug("ttc_fallback_failed error=%s", e2)
        return {"success": False, "text": "", "provider": "none",
                "model_used": "", "error": str(e)[:200],
                "ttc": {"width": width, "enabled": _ENABLED, "error": True}}


def audit() -> dict[str, Any]:
    return {
        "enabled": _ENABLED,
        "max_width": _MAX_WIDTH,
        "tier_map": dict(_TIER_WIDTH),
    }


>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
__all__ = ["decide_width", "run_with_ttc", "audit"]