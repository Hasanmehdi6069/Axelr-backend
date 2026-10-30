# core/utils/misc_utils.py
"""
AXELR — Miscellaneous Utility Functions (canonical)
====================================================
Consolidated home for three orthogonal utilities previously scattered
across ``core/warmup.py``, ``core/prewarm.py``, and ``core/explainer.py``:

    Warmup / pacemaker    — predictive readiness signaling
    Prewarm               — synthetic inference to prime the local twin
    Error explainer       — bounded LLM call that maps a traceback to
                            {cause, fix, guard}

What was removed from this file
-------------------------------
``RepoIndexer``, ``IndexStats``, ``CodeChunk`` were previously defined
here as well as in ``core/repo_indexer.py``. The duplicate had a broken
``await self._vc.embed_and_upload(...)`` call — no such method exists on
``_VectorizeClient``. The canonical implementations live in
``core/repo_indexer.py``; this module no longer imports
``_VectorizeClient`` or ``_sha``.

Contract:
    * Every public function never raises to its caller.
    * Prewarm state is bounded: max ``_MAX_CONCURRENT`` in-flight tasks,
      ``_COOLDOWN_S`` between signals per ``(workspace, task_type)``.
    * Prewarm deduplication is best-effort, not lock-guarded — concurrent
      calls can at worst spawn one redundant prewarm, never crash.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger("axelr.utils.misc")

# ═══════════════════════════════════════════════════════════════════════════
# §1 — Predictive warmup + pacemaker
# ═══════════════════════════════════════════════════════════════════════════
_WARMUP_KEY = "axelr:warmup:signal"
_WARMUP_TTL_S = 5


async def signal_warmup(redis_client: Any) -> bool:
    """Record that a submit is imminent. Best-effort; never raises.

    Returns ``True`` if the signal was written, ``False`` if Redis is
    unavailable or the write failed.
    """
    if redis_client is None:
        return False
    try:
        await redis_client.setex(
            _WARMUP_KEY, _WARMUP_TTL_S, str(time.time()))
        return True
    except Exception:
        return False


async def pacemaker_loop(
    state: object, *, interval_s: float = 0.5,
) -> None:
    """Long-running readiness pinger. Called as a lifespan task.

    Pings Redis and the DB whenever a warmup signal is fresh, keeping
    connections and TLS sessions hot so the next user request is fast.

    Contract:
        * Loops until cancelled; returns cleanly on ``CancelledError``.
        * Reads ``state.redis_client`` and ``state.db`` dynamically; a
          missing attribute simply skips that step.
        * Never raises out of the loop.
    """
    while True:
        try:
            await asyncio.sleep(interval_s)
            redis = getattr(state, "redis_client", None)
            if redis is None:
                continue
            with contextlib.suppress(Exception):
                if await redis.get(_WARMUP_KEY) is None:
                    continue
            db = getattr(state, "db", None)
            if getattr(state, "db_available", False) and db is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        db.command("ping"), timeout=0.5)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(redis.ping(), timeout=0.3)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.debug("pacemaker_loop_error error=%s", e)


# ═══════════════════════════════════════════════════════════════════════════
# §2 — Prewarm
# ═══════════════════════════════════════════════════════════════════════════
_COOLDOWN_S = float(os.getenv("AXELR_PREWARM_COOLDOWN_S", "90.0"))
_MAX_CONCURRENT = int(os.getenv("AXELR_PREWARM_MAX_CONCURRENT", "4"))
_PREWARM_TIMEOUT_S = float(os.getenv("AXELR_PREWARM_TIMEOUT_S", "6.0"))
_PREWARM_ENABLED = (os.getenv("AXELR_PREWARM", "true").lower()
                    in ("1", "true", "yes"))

_SYNTHETIC_PROMPTS: dict[str, str] = {
    "extraction": (
        "Extract the requested fields from the following record. "
        "Return a compact JSON array inside ```json fences.\n\n"
        "<record>\nfield1: value1\nfield2: value2\n</record>\n"
    ),
    "structuring": (
        "Return a single JSON object with the requested keys.\n\n"
        "<request>summarize the following in a structured form</request>\n"
    ),
    "frontend": (
        "Produce a single self-contained HTML document with inline CSS "
        "and JS. Modern 2025 styling.\n\n"
        "<request>build a small landing page</request>\n"
    ),
    "touch_fix": (
        "Fix the following code. Return the full corrected file inside "
        "a single fenced block.\n\n"
        "<code>def f(): pass</code>\n"
    ),
}


@dataclass(slots=True)
class _PrewarmEntry:
    workspace: str
    task_type: str
    started_at: float
    task: asyncio.Task[None] | None = None
    result: str = ""


# Module-level prewarm state. Not a singleton — a bounded state machine
# for prewarm slot accounting. Every dict entry is mutated only by
# ``signal_prewarm`` / ``_run_prewarm`` in the event loop; concurrent
# access is best-effort (one redundant prewarm at worst, never a crash).
_state: dict[str, Any] = {
    "in_flight": {},
    "last_ts": {},
    "stats": {
        "signals": 0, "fires": 0, "coalesced": 0,
        "cancelled": 0, "skipped_cooldown": 0, "skipped_cap": 0,
        "skipped_disabled": 0, "skipped_unavailable": 0,
    },
}


def _key(workspace: str, task_type: str) -> tuple[str, str]:
    return (workspace or "core", task_type or "structuring")


def _synthetic(task_type: str) -> str:
    return _SYNTHETIC_PROMPTS.get(
        task_type, _SYNTHETIC_PROMPTS["structuring"])


async def _run_prewarm(
    workspace: str, task_type: str, prompt_hint: str,
) -> None:
    key = _key(workspace, task_type)
    entry: _PrewarmEntry | None = _state["in_flight"].get(key)
    try:
        try:
            from ..twin_engine import execute_local_inference
        except Exception as e:
            logger.debug("prewarm_twin_import_failed error=%s", e)
            _state["stats"]["skipped_unavailable"] += 1
            if entry:
                entry.result = "skipped:no_engine"
            return
        synthetic = _synthetic(task_type)
        if prompt_hint:
            synthetic = prompt_hint[:200] + "\n\n" + synthetic
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(
                execute_local_inference(workspace, task_type, synthetic),
                timeout=_PREWARM_TIMEOUT_S)
        except asyncio.TimeoutError:
            if entry:
                entry.result = "error:timeout"
            return
        except asyncio.CancelledError:
            if entry:
                entry.result = "cancelled"
            raise
        except Exception as e:
            if entry:
                entry.result = f"error:{type(e).__name__}"
            logger.debug("prewarm_inference_failed error=%s", e)
            return
        if entry:
            entry.result = "ok"
        _state["stats"]["fires"] += 1
        logger.debug(
            "prewarm_done workspace=%s task=%s elapsed_ms=%.0f",
            workspace, task_type, (time.monotonic() - t0) * 1000)
    finally:
        _state["in_flight"].pop(key, None)


def signal_prewarm(
    workspace: str, task_type: str, prompt_hint: str = "",
) -> bool:
    """Signal that a prewarm for ``(workspace, task_type)`` is worth doing.

    Contract:
        * Returns ``True`` when a prewarm task was scheduled (or is
          already in flight and got coalesced).
        * Returns ``False`` on: disabled env, no event loop, cap reached,
          or within ``_COOLDOWN_S`` of the last signal.
        * Never raises.
    """
    if not _PREWARM_ENABLED:
        _state["stats"]["skipped_disabled"] += 1
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    _state["stats"]["signals"] += 1
    key = _key(workspace, task_type)
    now = time.monotonic()
    if key in _state["in_flight"]:
        _state["stats"]["coalesced"] += 1
        return True
    last = _state["last_ts"].get(key, 0.0)
    if (now - last) < _COOLDOWN_S:
        _state["stats"]["skipped_cooldown"] += 1
        return False
    if len(_state["in_flight"]) >= _MAX_CONCURRENT:
        _state["stats"]["skipped_cap"] += 1
        return False
    _state["last_ts"][key] = now
    entry = _PrewarmEntry(
        workspace=workspace, task_type=task_type, started_at=now)
    _state["in_flight"][key] = entry
    entry.task = loop.create_task(
        _run_prewarm(workspace, task_type, prompt_hint),
        name=f"prewarm::{key[0]}::{key[1]}")
    return True


def cancel_prewarm(workspace: str, task_type: str) -> bool:
    """Cancel an in-flight prewarm. Returns ``True`` if one was cancelled."""
    key = _key(workspace, task_type)
    entry = _state["in_flight"].get(key)
    if entry is None or entry.task is None or entry.task.done():
        return False
    entry.task.cancel()
    _state["stats"]["cancelled"] += 1
    return True


def prewarm_snapshot() -> dict[str, Any]:
    """JSON-serialisable view of the prewarm state machine."""
    now = time.monotonic()
    return {
        "enabled": _PREWARM_ENABLED,
        "in_flight": [
            {
                "workspace": e.workspace,
                "task_type": e.task_type,
                "started_s": now - e.started_at,
                "result": e.result,
            }
            for e in _state["in_flight"].values()
        ],
        "stats": dict(_state["stats"]),
    }


# ═══════════════════════════════════════════════════════════════════════════
# §3 — Error explainer
# ═══════════════════════════════════════════════════════════════════════════
RouteFn = Callable[..., Awaitable[dict[str, Any]]]

_TRACEBACK_RE = re.compile(
    r"Traceback \(most recent call last\):[\s\S]{0,3000}")
_JS_ERROR_RE = re.compile(
    r"^\s*(?:Uncaught\s+)?([A-Z][A-Za-z]+Error):\s*(.+)$", re.MULTILINE)
_HTTP_5XX_RE = re.compile(r"\b(50[0-9])\b[^\n]{0,120}")
_PY_EXC_RE = re.compile(
    r"^([A-Z][A-Za-z]+(?:Error|Exception|Warning)):\s*(.+)$",
    re.MULTILINE)
_MAX_SIGNATURE_CHARS = 1200

_EXPLAIN_PROMPT = """\
<instant_explainer>
You are a senior engineer. Explain this error in ≤ 80 words.
Error: {signature}
Return ONLY a compact JSON object with these exact keys:
{{"cause": "<one sentence>", "fix": "<one sentence>", "guard": "<one line code or check>"}}
No prose before or after. No markdown fence.
</instant_explainer>"""


@dataclass(frozen=True, slots=True)
class ErrorSignature:
    """A detected error snippet. ``kind`` is one of: ``traceback``,
    ``js_error``, ``py_exception``, ``http_5xx``. ``line`` is unused by
    the current detector but preserved for callers that set it."""
    kind: str
    text: str
    line: int = 0


def detect_error(text: str) -> ErrorSignature | None:
    """Find the first error-like substring in ``text``. Never raises."""
    if not text:
        return None
    m = _TRACEBACK_RE.search(text)
    if m:
        return ErrorSignature(
            "traceback", m.group(0)[:_MAX_SIGNATURE_CHARS])
    m = _JS_ERROR_RE.search(text)
    if m:
        return ErrorSignature(
            "js_error", m.group(0)[:_MAX_SIGNATURE_CHARS])
    m = _PY_EXC_RE.search(text)
    if m:
        return ErrorSignature(
            "py_exception", m.group(0)[:_MAX_SIGNATURE_CHARS])
    m = _HTTP_5XX_RE.search(text)
    if m:
        return ErrorSignature(
            "http_5xx", m.group(0)[:_MAX_SIGNATURE_CHARS])
    return None


async def explain(
    signature: ErrorSignature, *,
    route_fn: RouteFn, language: str = "en",
    tier: str = "free", user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One bounded LLM call that turns an ``ErrorSignature`` into
    ``{cause, fix, guard, source}``.

    Contract:
        * Returns ``{"success": True, "kind": ..., "cause": ..., "fix": ...,
          "guard": ..., "source": "llm" | "static"}`` on any path.
        * ``source="static"`` indicates the LLM call failed or returned
          garbage; the answer is still useful, just not context-specific.
        * Never raises.
    """
    prompt = _EXPLAIN_PROMPT.format(signature=signature.text)
    try:
        result = await route_fn(
            workspace="core", task_type="touch_fix", prompt=prompt,
            history=[], files=[], max_tokens=512, temp=0.1,
            tier=tier, user=user, context="")
    except Exception as e:
        logger.debug("explainer_route_failed error=%s", e)
        return _static(signature)
    if not isinstance(result, dict) or not result.get("success"):
        return _static(signature)
    text = (result.get("text") or "").strip()
    try:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return _static(signature)
        data = json.loads(m.group(0))
        if not isinstance(data, dict):
            return _static(signature)
        return {
            "success": True, "kind": signature.kind,
            "cause": str(data.get("cause", ""))[:300],
            "fix": str(data.get("fix", ""))[:300],
            "guard": str(data.get("guard", ""))[:200],
            "source": "llm",
        }
    except Exception:
        return _static(signature)


def _static(signature: ErrorSignature) -> dict[str, Any]:
    """Deterministic fallback. Never raises."""
    return {
        "success": True, "kind": signature.kind,
        "cause": "Error detected; no live explanation available.",
        "fix": "Inspect the traceback and check the referenced line.",
        "guard": "Add an assertion or explicit try/except around the call site.",
        "source": "static",
    }


__all__ = [
    "signal_warmup", "pacemaker_loop",
    "signal_prewarm", "cancel_prewarm", "prewarm_snapshot",
    "ErrorSignature", "detect_error", "explain",
]