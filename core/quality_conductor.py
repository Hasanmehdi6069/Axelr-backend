# core/quality_conductor.py
"""
AXELR — Quality Conductor v1.2 (unified quality orchestration)
==============================================================
One classification. One decision. One budget. One trace.

Every quality module in the repo remains importable and usable
standalone.  This module does not delete, merge, or replace any of
them.  It arbitrates *which one runs per request*, and how many
provider calls the request is allowed to spend.

Capabilities the conductor dispatches to (all optional, all gated):

    task_shedder.assess                   one-shot complexity classification
    goat_layer.run_cognitive_pipeline     plan → generate → critique → polish
    cognitive_pipeline.run                logic → editorial → verify
    consensus.race_consensus              width=2 draft + central pick (critical)
    consensus.local_polish                zero-cost final delivery pass
    reflection.scan / repair              targeted repair, one call max
    hardening.prune_system                zero-cost prose cleanup
    hardening.ast_repair                  zero-cost fenced-block repair
    hardening.is_decomposable / race_tasks  multi-part split (opt-in)
    adaptive_control.tune_temperature     opt-in temperature tuning
    adaptive_control.LEDGER               session anchor ledger
    adaptive_control.stops_for            stop-sequence injection
    bilingual.detect_language             language tag for the model
    bilingual.compose_language_directive  directive block injection
    explainer.detect_error                never auto-fires; UI hook only

Budget (per tier, hard-capped by env):
    trivial   → 0 extra calls
    standard  → 1 extra call  (single + reflection)
    deep      → 2 extra calls (goat pipeline + reflection)
    critical  → 3 extra calls (cognitive|consensus + reflection + optional split)
    ABSOLUTE  → AXELR_CONDUCTOR_MAX_CALLS (default 3)

Circuit breaker: any expensive path that fails twice within
CIRCUIT_WINDOW_S is skipped for CIRCUIT_COOLDOWN_S.  This is the
difference between "one bad provider cools down" and "every request
retries the same broken path forever."

Stats: process-local counters exposed via stats() for observability.
"""
from __future__ import annotations

import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

logger = logging.getLogger("axelr.conductor")

_VERSION = "1.2"

RouteFn = Callable[..., Awaitable[dict[str, Any]]]

# ── Config ─────────────────────────────────────────────────────────────
_MAX_EXTRA_CALLS     = max(0, int(os.getenv("AXELR_CONDUCTOR_MAX_CALLS", "3")))
_DEADLINE_S          = float(os.getenv("AXELR_CONDUCTOR_DEADLINE_S", "25.0"))
_ENABLE_MEMO         = os.getenv("AXELR_CONDUCTOR_MEMO", "true").lower() in ("1", "true", "yes")
_MEMO_MAX            = max(16, int(os.getenv("AXELR_CONDUCTOR_MEMO_SIZE", "128")))
_TEMP_OVERRIDE       = os.getenv("AXELR_CONDUCTOR_TUNE_TEMP", "false").lower() in ("1", "true", "yes")
_ENABLE_BILINGUAL    = os.getenv("AXELR_CONDUCTOR_BILINGUAL", "true").lower() in ("1", "true", "yes")
_ENABLE_CONSENSUS    = os.getenv("AXELR_CONDUCTOR_CONSENSUS", "true").lower() in ("1", "true", "yes")
_ENABLE_SPLIT        = os.getenv("AXELR_CONDUCTOR_SPLIT", "false").lower() in ("1", "true", "yes")
_CIRCUIT_WINDOW_S    = float(os.getenv("AXELR_CONDUCTOR_CIRCUIT_WINDOW_S", "60.0"))
_CIRCUIT_COOLDOWN_S  = float(os.getenv("AXELR_CONDUCTOR_CIRCUIT_COOLDOWN_S", "300.0"))
_CIRCUIT_FAIL_THRESH = max(1, int(os.getenv("AXELR_CONDUCTOR_CIRCUIT_FAILS", "2")))
_TRACE_MAX           = 40

# ── Tier budgets and paths (mutually exclusive) ────────────────────────
_TIER_BUDGET: dict[str, int] = {
    "trivial":  0,
    "standard": 1,
    "deep":     2,
    "critical": 3,
}
_PATH_BY_TIER: dict[str, str] = {
    "trivial":  "single",
    "standard": "single",
    "deep":     "goat",
    "critical": "cognitive",     # consensus is chosen by _decide_path()
}
_GOAT_TIER_MAP = {
    "trivial": "shed", "standard": "lite",
    "deep": "standard", "critical": "deep",
}
_GOAT_ROUNDS = {"shed": 1, "lite": 1, "standard": 2, "deep": 3}
_GOAT_TOKENS = {"shed": 1024, "lite": 1536, "standard": 2048, "deep": 3072}


# ═══════════════════════════════════════════════════════════════════════════
# Stats (process-local, bounded, observability only)
# ═══════════════════════════════════════════════════════════════════════════
_STATS: dict[str, int] = {
    "requests_total": 0,
    "requests_failed": 0,
    "memo_hits": 0,
    "memo_misses": 0,
    "calls_total": 0,
    "path_single": 0,
    "path_goat": 0,
    "path_cognitive": 0,
    "path_consensus": 0,
    "reflection_repaired": 0,
    "reflection_skipped_budget": 0,
    "circuit_trips": 0,
}


def reset_stats() -> None:
    for k in _STATS:
        _STATS[k] = 0


def stats() -> dict[str, int]:
    return dict(_STATS)


# ═══════════════════════════════════════════════════════════════════════════
# Circuit breaker (expensive paths only)
# ═══════════════════════════════════════════════════════════════════════════
_CIRCUIT: dict[str, list[float]] = {"goat": [], "cognitive": [], "consensus": []}
_CIRCUIT_TRIPPED: dict[str, float] = {}


def _circuit_ok(path: str) -> bool:
    now = time.monotonic()
    cooldown_until = _CIRCUIT_TRIPPED.get(path, 0.0)
    return now >= cooldown_until


def _circuit_record(path: str, ok: bool) -> None:
    if path not in _CIRCUIT:
        return
    now = time.monotonic()
    fails = _CIRCUIT[path]
    fails[:] = [t for t in fails if now - t < _CIRCUIT_WINDOW_S]
    if ok:
        fails.clear()
        _CIRCUIT_TRIPPED.pop(path, None)
        return
    fails.append(now)
    if len(fails) >= _CIRCUIT_FAIL_THRESH:
        _CIRCUIT_TRIPPED[path] = now + _CIRCUIT_COOLDOWN_S
        _STATS["circuit_trips"] += 1
        logger.warning("conductor.circuit_tripped path=%s cooldown_s=%.0f",
                       path, _CIRCUIT_COOLDOWN_S)


# ═══════════════════════════════════════════════════════════════════════════
# Trace + context
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class _RunContext:
    route_fn: RouteFn
    workspace: str
    task_type: str
    prompt: str
    history: list[dict] | None
    files: list[dict] | None
    max_tokens: int
    temp: float
    tier: str
    user: dict | None
    context: str
    request: Any
    intent: str
    session_id: str
    budget: int
    deadline: float
    enable_reflection: bool
    stream: bool = False
    lang: str = "en"
    calls: int = 0
    trace: list[dict[str, Any]] = field(default_factory=list)
    t0: float = field(default_factory=time.monotonic)

    def charge(self) -> bool:
        if self.calls >= self.budget:
            return False
        if (time.monotonic() - self.t0) >= self.deadline:
            return False
        self.calls += 1
        return True

    def record(self, stage: str, **kw: Any) -> None:
        if len(self.trace) >= _TRACE_MAX:
            return
        self.trace.append({
            "stage": stage,
            "t_ms": round((time.monotonic() - self.t0) * 1000, 1),
            **kw,
        })

    def call_kwargs(self, **overrides: Any) -> dict[str, Any]:
        base = {
            "workspace":  self.workspace, "task_type": self.task_type,
            "prompt":     self.prompt,    "history":   self.history,
            "files":      self.files,     "max_tokens": self.max_tokens,
            "temp":       self.temp,      "tier":      self.tier,
            "user":       self.user,      "context":   self.context,
            "request":    self.request,
        }
        base.update(overrides)
        return base


# ═══════════════════════════════════════════════════════════════════════════
# Memo — deterministic outcomes for identical single-path requests
# ═══════════════════════════════════════════════════════════════════════════
_MEMO: "OrderedDict[tuple, dict[str, Any]]" = OrderedDict()


def _memo_key(prompt: str, files: Iterable[dict] | None,
              tier: str, workspace: str) -> tuple:
    files_key = tuple(
        (str(f.get("filename", "")), str(f.get("name", "")))
        for f in (files or []) if isinstance(f, dict)
    )
    return (prompt[:500], files_key, tier, workspace)


def _memo_get(key: tuple) -> dict[str, Any] | None:
    if not _ENABLE_MEMO:
        return None
    v = _MEMO.get(key)
    if v is None:
        return None
    _MEMO.move_to_end(key)
    return v


def _memo_set(key: tuple, value: dict[str, Any]) -> None:
    if not _ENABLE_MEMO:
        return
    if key in _MEMO:
        _MEMO.move_to_end(key)
    _MEMO[key] = value
    while len(_MEMO) > _MEMO_MAX:
        _MEMO.popitem(last=False)


# ═══════════════════════════════════════════════════════════════════════════
# Import-safe adaptors
# ═══════════════════════════════════════════════════════════════════════════
def _classify(prompt: str, history, files):
    try:
        from core.task_shedder import assess
        return assess(prompt, history=history, files=files)
    except Exception as e:
        logger.debug("conductor.classify_failed err=%s", e)
        return None


def _to_goat_profile(complexity) -> Any:
    try:
        from core.goat_layer import WorkloadProfile, _SHED_PROFILE
    except Exception:
        return None
    if complexity is None:
        return _SHED_PROFILE
    goat_tier = _GOAT_TIER_MAP.get(complexity.tier, "shed")
    try:
        return WorkloadProfile(
            tier=goat_tier,
            score=round(complexity.score * 100, 1),
            signals=tuple(complexity.reasons),
            use_pipeline=goat_tier in ("standard", "deep"),
            use_reflection=goat_tier in ("standard", "deep"),
            recommended_rounds=_GOAT_ROUNDS[goat_tier],
            recommended_max_tokens=_GOAT_TOKENS[goat_tier],
        )
    except Exception:
        return _SHED_PROFILE


def _detect_language(prompt: str) -> str:
    if not _ENABLE_BILINGUAL:
        return "en"
    try:
        from core.bilingual import detect_language
        return detect_language(prompt)
    except Exception:
        return "en"


def _tune_temp(ctx: _RunContext) -> float:
    if not _TEMP_OVERRIDE:
        return ctx.temp
    try:
        from core.adaptive_control import tune_temperature
        return tune_temperature(
            intent=ctx.intent, task_type=ctx.task_type,
            tier=ctx.tier, workspace=ctx.workspace,
        )
    except Exception:
        return ctx.temp


def _enrich_context(ctx: _RunContext, prompt: str) -> str:
    parts: list[str] = []
    if ctx.context:
        parts.append(ctx.context)

    if ctx.session_id:
        try:
            from core.adaptive_control import LEDGER
            LEDGER.observe(ctx.session_id, prompt)
            anchors = LEDGER.render(ctx.session_id)
            if anchors:
                parts.append(anchors)
        except Exception:
            pass

    try:
        from core.adaptive_control import stops_for
        stops = stops_for(ctx.workspace, ctx.task_type)
        if stops:
            lines = "\n".join(f"  · {s!r}" for s in stops)
            parts.append(
                "<stop_sequences>\n"
                "End your output before any of the following appear:\n"
                f"{lines}\n</stop_sequences>"
            )
    except Exception:
        pass

    if ctx.lang and ctx.lang != "en":
        try:
            from core.bilingual import compose_language_directive
            directive = compose_language_directive(ctx.lang)
            if directive:
                parts.append(directive)
        except Exception:
            pass

    return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════
# Path selection — one pipeline runs, never two
# ═══════════════════════════════════════════════════════════════════════════
def _decide_path(complexity, task_type: str, workspace: str,
                 force_path: str | None, ctx: "_RunContext") -> str:
    if force_path:
        return force_path
    if complexity is None:
        return "single"

    # Deterministic tasks never escalate.
    if task_type in ("extraction", "touch_fix"):
        return "single"

    tier = complexity.tier

    if tier == "critical":
        if (_ENABLE_CONSENSUS
                and not ctx.stream
                and ctx.budget >= 2
                and len(ctx.prompt) < 800
                and _circuit_ok("consensus")):
            return "consensus"
        if _circuit_ok("cognitive"):
            return "cognitive"
        return "goat"

    return _PATH_BY_TIER.get(tier, "single")


async def _call(route_fn: RouteFn, **kw: Any) -> dict[str, Any]:
    try:
        r = await route_fn(**kw)
    except Exception as e:
        logger.warning("conductor.route_failed err=%s", e)
        return {"success": False, "text": "", "provider": "none",
                "model_used": "", "error": str(e)[:200]}
    if not isinstance(r, dict):
        return {"success": False, "text": "", "provider": "none", "model_used": ""}
    return r


async def _execute_single(ctx: _RunContext) -> dict[str, Any]:
    ctx.record("single_start")
    r = await _call(ctx.route_fn, **ctx.call_kwargs())
    ctx.calls += 1
    ctx.record("single_done", ok=bool(r.get("success")))
    return r


async def _execute_goat(ctx: _RunContext, profile: Any) -> dict[str, Any]:
    ctx.record("goat_start")
    try:
        from core.goat_layer import run_cognitive_pipeline
    except Exception as e:
        logger.warning("conductor.goat_import_failed err=%s", e)
        _circuit_record("goat", False)
        return await _execute_single(ctx)
    r = await run_cognitive_pipeline(
        route_fn=ctx.route_fn,
        workspace=ctx.workspace, task_type=ctx.task_type,
        prompt=ctx.prompt, history=ctx.history, files=ctx.files,
        max_tokens=ctx.max_tokens, temp=ctx.temp, tier=ctx.tier,
        user=ctx.user, context=ctx.context, request=ctx.request,
        profile=profile,
    )
    ok = bool(r.get("success"))
    _circuit_record("goat", ok)
    meta = r.get("goat", {}) if isinstance(r, dict) else {}
    ctx.calls += max(1, int(meta.get("calls", 1)))
    ctx.record("goat_done", calls=meta.get("calls", 0),
               tier=meta.get("tier"), ok=ok)
    return r


async def _execute_cognitive(ctx: _RunContext, complexity) -> dict[str, Any]:
    ctx.record("cognitive_start")
    try:
        from core.cognitive_pipeline import run as cog_run
    except Exception as e:
        logger.warning("conductor.cognitive_import_failed err=%s", e)
        _circuit_record("cognitive", False)
        return await _execute_single(ctx)
    try:
        r = await cog_run(
            ctx.route_fn,
            workspace=ctx.workspace, task_type=ctx.task_type,
            prompt=ctx.prompt, history=ctx.history, files=ctx.files,
            max_tokens=ctx.max_tokens, temp=ctx.temp, tier=ctx.tier,
            user=ctx.user, context=ctx.context, request=ctx.request,
            _preassessed=complexity,
        )
    except TypeError:
        # Older cognitive_pipeline without _preassessed kwarg.
        r = await cog_run(
            ctx.route_fn,
            workspace=ctx.workspace, task_type=ctx.task_type,
            prompt=ctx.prompt, history=ctx.history, files=ctx.files,
            max_tokens=ctx.max_tokens, temp=ctx.temp, tier=ctx.tier,
            user=ctx.user, context=ctx.context, request=ctx.request,
        )
    ok = bool(r.get("success"))
    _circuit_record("cognitive", ok)
    meta = r.get("cognitive", {}) if isinstance(r, dict) else {}
    ctx.calls += max(1, int(meta.get("stages_run", 1)))
    ctx.record("cognitive_done", stages=meta.get("stages_run", 0), ok=ok)
    return r


async def _execute_consensus(ctx: _RunContext) -> dict[str, Any]:
    ctx.record("consensus_start")
    try:
        from core.consensus import race_consensus
    except Exception as e:
        logger.warning("conductor.consensus_import_failed err=%s", e)
        _circuit_record("consensus", False)
        return await _execute_single(ctx)
    if ctx.calls + 2 > ctx.budget:
        ctx.record("consensus_skipped", reason="budget")
        return await _execute_single(ctx)
    try:
        outcome = await race_consensus(
            ctx.route_fn,
            workspace=ctx.workspace, task_type=ctx.task_type,
            prompt=ctx.prompt, history=ctx.history, files=ctx.files,
            max_tokens=ctx.max_tokens, temp=ctx.temp, tier=ctx.tier,
            user=ctx.user, context=ctx.context, request=ctx.request,
            width=2, timeout_s=max(6.0, ctx.deadline * 0.5),
            consensus_floor=0.55, prefer_non_stream=True,
        )
    except Exception as e:
        logger.warning("conductor.consensus_failed err=%s", e)
        _circuit_record("consensus", False)
        return await _execute_single(ctx)
    ok = bool(outcome and outcome.winner.get("success"))
    _circuit_record("consensus", ok)
    ctx.calls += max(1, outcome.participants if outcome else 1)
    result = dict(outcome.winner) if outcome else {}
    result["consensus"] = outcome.to_dict() if outcome else {}
    ctx.record("consensus_done",
               participants=outcome.participants if outcome else 0,
               agreement=round(outcome.agreement, 3) if outcome else 0.0,
               ok=ok)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Postprocess — deterministic, zero provider calls until reflection
# ═══════════════════════════════════════════════════════════════════════════
async def _postprocess(ctx: _RunContext, result: dict[str, Any]) -> dict[str, Any]:
    text = result.get("text") or ""
    if not text:
        return result

    # 1. Deterministic prune + AST repair. Zero provider calls.
    try:
        from core.hardening import prune_system, ast_repair
        pruned = prune_system(text)
        repaired, issues = ast_repair(pruned)
        if issues:
            ctx.record("ast_repair", issues=issues[:5])
        if repaired != text:
            ctx.record("prune", saved_chars=len(text) - len(repaired))
            text = repaired
            result["text"] = text
    except Exception as e:
        logger.debug("conductor.prune_failed err=%s", e)

    # 2. Local polish — always runs, ~15 µs, zero network.
    try:
        from core.consensus import local_polish
        polished = local_polish(text)
        if polished.changed:
            text = polished.text
            result["text"] = text
            ctx.record("local_polish", issues=polished.issues[:5])
    except Exception as e:
        logger.debug("conductor.local_polish_failed err=%s", e)

    # 3. Reflection scan — always. Repair is budgeted.
    if not ctx.enable_reflection:
        return result
    try:
        from core.reflection import scan, repair
        r = scan(text)
        if not r.needs_repair:
            ctx.record("reflection_clean")
            result["reflection"] = {"repaired": False, "issues": []}
            return result
        ctx.record("reflection_flagged", issues=list(r.issues))
        if not ctx.charge():
            _STATS["reflection_skipped_budget"] += 1
            result["reflection"] = {"repaired": False, "issues": list(r.issues),
                                    "reason": "budget"}
            return result
        repaired_text, meta = await repair(
            ctx.route_fn,
            workspace=ctx.workspace, task_type=ctx.task_type,
            prompt=ctx.prompt, text=text,
            max_tokens=min(ctx.max_tokens, 2048),
            temp=0.2, tier=ctx.tier, user=ctx.user,
            context=ctx.context, request=ctx.request,
        )
        ctx.record("reflection_done", repaired=meta.get("repaired", False))
        if meta.get("repaired"):
            result["text"] = repaired_text
            _STATS["reflection_repaired"] += 1
        result["reflection"] = meta
    except Exception as e:
        logger.debug("conductor.reflection_failed err=%s", e)

    return result


# ═══════════════════════════════════════════════════════════════════════════
# Public entry point
# ═══════════════════════════════════════════════════════════════════════════
async def run(
    route_fn: RouteFn,
    *,
    workspace: str,
    task_type: str,
    prompt: str,
    history: list[dict] | None = None,
    files: list[dict] | None = None,
    max_tokens: int = 2048,
    temp: float = 0.7,
    tier: str = "free",
    user: dict | None = None,
    context: str = "",
    request: Any = None,
    intent: str = "",
    session_id: str = "",
    budget_calls: int | None = None,
    deadline_s: float | None = None,
    force_path: str | None = None,
    enable_reflection: bool = True,
    allow_memo: bool = True,
    stream: bool = False,
) -> dict[str, Any]:
    """
    Single entry point for the entire quality stack.
    Every existing module stays importable and usable standalone.
    """
    t_start = time.monotonic()
    _STATS["requests_total"] += 1

    complexity = _classify(prompt, history, files)
    profile    = _to_goat_profile(complexity)
    lang       = _detect_language(prompt)

    provisional_budget = (
        max(0, min(int(budget_calls), _MAX_EXTRA_CALLS))
        if budget_calls is not None
        else min(_TIER_BUDGET.get(complexity.tier if complexity else "standard", 1),
                 _MAX_EXTRA_CALLS)
    )

    ctx = _RunContext(
        route_fn=route_fn,
        workspace=workspace, task_type=task_type, prompt=prompt,
        history=history, files=files,
        max_tokens=max_tokens, temp=temp, tier=tier,
        user=user, context=context, request=request,
        intent=intent, session_id=session_id,
        budget=provisional_budget,
        deadline=deadline_s if deadline_s is not None else _DEADLINE_S,
        enable_reflection=enable_reflection,
        stream=stream, lang=lang,
    )

    path = _decide_path(complexity, task_type, workspace, force_path, ctx)
    _STATS[f"path_{path}"] = _STATS.get(f"path_{path}", 0) + 1

    # Memo only fires on the deterministic single path with no history.
    memo_key = None
    if allow_memo and path == "single" and not history and temp <= 0.3:
        memo_key = _memo_key(prompt, files, tier, workspace)
        cached = _memo_get(memo_key)
        if cached is not None:
            _STATS["memo_hits"] += 1
            cached = dict(cached)
            cached["conductor"] = {**cached.get("conductor", {}), "memo_hit": True}
            return cached
        _STATS["memo_misses"] += 1

    ctx.temp = _tune_temp(ctx)
    ctx.context = _enrich_context(ctx, prompt)

    try:
        if path == "consensus":
            result = await _execute_consensus(ctx)
        elif path == "goat" and profile is not None:
            result = await _execute_goat(ctx, profile)
        elif path == "cognitive" and complexity is not None:
            result = await _execute_cognitive(ctx, complexity)
        else:
            result = await _execute_single(ctx)
    except Exception as e:
        logger.warning("conductor.execute_failed err=%s", e)
        result = {"success": False, "text": "", "provider": "none",
                  "model_used": "", "error": str(e)[:200]}

    if result.get("success") and result.get("text"):
        try:
            result = await _postprocess(ctx, result)
        except Exception as e:
            logger.debug("conductor.postprocess_failed err=%s", e)

    signals: list[str] = []
    if complexity is not None:
        signals.extend(complexity.reasons)
    result["conductor"] = {
        "version": _VERSION,
        "path": path,
        "tier": complexity.tier if complexity else "unknown",
        "score": round(complexity.score, 3) if complexity else 0.0,
        "signals": signals,
        "budget": ctx.budget,
        "calls": ctx.calls,
        "memo_hit": False,
        "lang": ctx.lang,
        "trace": ctx.trace,
        "elapsed_ms": round((time.monotonic() - t_start) * 1000, 2),
    }

    _STATS["calls_total"] += ctx.calls
    if not result.get("success"):
        _STATS["requests_failed"] += 1

    if memo_key is not None and result.get("success"):
        _memo_set(memo_key, {k: v for k, v in result.items() if k != "conductor"})

    return result


def audit() -> dict[str, Any]:
    return {
        "version": _VERSION,
        "max_extra_calls": _MAX_EXTRA_CALLS,
        "deadline_s": _DEADLINE_S,
        "memo_enabled": _ENABLE_MEMO,
        "memo_size": len(_MEMO),
        "memo_max": _MEMO_MAX,
        "tier_budget": dict(_TIER_BUDGET),
        "path_by_tier": dict(_PATH_BY_TIER),
        "temp_override": _TEMP_OVERRIDE,
        "bilingual": _ENABLE_BILINGUAL,
        "consensus": _ENABLE_CONSENSUS,
        "split": _ENABLE_SPLIT,
        "circuit_tripped": dict(_CIRCUIT_TRIPPED),
    }


def health() -> dict[str, Any]:
    """Single payload for /health — audit + live stats."""
    return {**audit(), "stats": stats()}


__all__ = ["run", "audit", "health", "stats", "reset_stats"]