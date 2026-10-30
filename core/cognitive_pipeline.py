<<<<<<< HEAD
# core/cognitive_pipeline.py
"""
AXELR — Asynchronous Multi-Agent Cognitive Pipelining (v2.1)
=============================================================
Slices a deep-tier request into 2–3 micro-specialist passes, each with
its own directive and token budget, all routed through the SAME
`route_fn` chain — no new providers, no new SDK, no added RAM.

Stage chain (ascending cost)
----------------------------
    logic      →  factual core, identifiers, code, numbers
    editorial  →  polish prose, strip hedging, match request shape
    verify     →  fix fences, contradictions, refusals, truncation
                  (only for `critical` tier)

v2.1 accepts a pre-assessed Complexity via `_preassessed` so callers
like core.quality_conductor do not re-run classification.  Also
reports stages_attempted / stages_skipped in the metadata block.

Contract
--------
    from core.cognitive_pipeline import run
    result = await run(route_fn, workspace=..., task_type=..., ...,
                       _preassessed=complexity)   # optional

Guarantees
----------
* Never raises — every stage is individually guarded.
* Never duplicates a call — a stage is skipped if the deadline is spent.
* Falls through to a single-shot `route_fn` call on any internal failure.

RAM
---
  Constant. Work buffers discarded at return.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

from core.task_shedder import Complexity, assess

logger = logging.getLogger("axelr.cognition")

__all__ = ["run", "Stage"]

RouteFn = Callable[..., Awaitable[dict[str, Any]]]


_LOGIC_DIRECTIVE = (
    "<cognitive_stage name=\"logic\">\n"
    "Produce ONLY the factual/logical core of the answer.\n"
    "• Bullet list of correct, verifiable claims.\n"
    "• Include every identifier, code block, and numeric fact the final\n"
    "  answer must contain.\n"
    "• No preamble. No prose polish. No sign-off.\n"
    "• No apologies, no 'here is', no meta-commentary.\n"
    "</cognitive_stage>"
)

_EDITORIAL_DIRECTIVE = (
    "<cognitive_stage name=\"editorial\">\n"
    "Rewrite the provided draft as the final user-facing answer.\n"
    "• Preserve EVERY identifier, number, and code block from the draft.\n"
    "• Remove hedging, filler, and meta-commentary.\n"
    "• Match the user's request shape (code / table / prose / blueprint).\n"
    "• Add zero new claims — do not invent.\n"
    "• If the request asked for code, output a single language-tagged fence.\n"
    "</cognitive_stage>"
)

_VERIFY_DIRECTIVE = (
    "<cognitive_stage name=\"verify\">\n"
    "Audit the draft. Fix ONLY the following classes of defect:\n"
    "• Unclosed code fences, broken brackets, truncation mid-sentence.\n"
    "• Contradictions or self-corrections left in the text.\n"
    "• Refusal markers (\"I cannot\", \"As an AI\") that leaked through.\n"
    "• Prompt-leak markers (<system_identity>, MASTER_PROMPT).\n"
    "Return the FULL corrected answer. Do not rewrite correct content.\n"
    "</cognitive_stage>"
)


@dataclass(frozen=True, slots=True)
class Stage:
    name: str
    directive: str
    tokens_frac: float
    min_prior_words: int = 0


_STAGES_STANDARD: tuple[Stage, ...] = (
    Stage("logic",     _LOGIC_DIRECTIVE,     0.60),
    Stage("editorial", _EDITORIAL_DIRECTIVE, 0.40, min_prior_words=40),
)

_STAGES_CRITICAL: tuple[Stage, ...] = (
    Stage("logic",     _LOGIC_DIRECTIVE,     0.50),
    Stage("editorial", _EDITORIAL_DIRECTIVE, 0.30, min_prior_words=40),
    Stage("verify",    _VERIFY_DIRECTIVE,    0.20, min_prior_words=60),
)


def _select_stages(c: Complexity, max_tokens: int) -> tuple[Stage, ...]:
    if c.tier == "critical":
        return _STAGES_CRITICAL
    if c.tier == "deep":
        return _STAGES_STANDARD
    return ()


def _stage_prompt(stage: Stage, user_prompt: str, prior_text: str) -> str:
    if not prior_text:
        return (
            f"{stage.directive}\n\n"
            f"<original_request>\n{user_prompt[:2000]}\n</original_request>"
        )
    return (
        f"{stage.directive}\n\n"
        f"<original_request>\n{user_prompt[:2000]}\n</original_request>\n\n"
        f"<prior_draft>\n{prior_text[:6000]}\n</prior_draft>"
    )


async def run(
    route_fn: RouteFn,
    *,
    workspace: str,
    task_type: str,
    prompt: str,
    history: Iterable[dict] | None,
    files: Iterable[dict] | None,
    max_tokens: int,
    temp: float,
    tier: str,
    user: dict | None = None,
    context: str = "",
    request: Any = None,
    budget_s: float = 22.0,
    _preassessed: "Complexity | None" = None,
) -> dict[str, Any]:
    """
    Escalate a deep request through the cognitive stages.  If the request
    is not deep, or the pipeline fails on any axis, this delegates to a
    single-shot `route_fn` call and returns.
    """
    # Classification — reuse if the caller already did it.
    if _preassessed is not None:
        c = _preassessed
    else:
        try:
            c = assess(prompt, history=history, files=files)
        except Exception:
            c = None

    if c is None or not c.is_deep:
        return await route_fn(workspace, task_type, prompt, history, files,
                              max_tokens, temp, tier, user, context, request)

    stages = _select_stages(c, max_tokens)
    if not stages:
        return await route_fn(workspace, task_type, prompt, history, files,
                              max_tokens, temp, tier, user, context, request)

    deadline = time.monotonic() + max(4.0, float(budget_s))
    prior_text = ""
    last_result: dict[str, Any] = {}
    stages_run = 0
    stages_attempted = 0
    stages_skipped = 0

    for stage in stages:
        if time.monotonic() >= deadline - 1.0:
            stages_skipped += 1
            continue
        if stage.min_prior_words and len(prior_text.split()) < stage.min_prior_words:
            stages_skipped += 1
            continue
        stages_attempted += 1
        stage_budget = max(128, int(max_tokens * stage.tokens_frac))
        stage_prompt = _stage_prompt(stage, prompt, prior_text)
        remaining = max(2.0, deadline - time.monotonic())
        try:
            r = await asyncio.wait_for(
                route_fn(
                    workspace, task_type, stage_prompt,
                    None, files, stage_budget, temp, tier,
                    user, context, request,
                ),
                timeout=remaining,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.debug("cognition_stage_timeout stage=%s", stage.name)
            break
        except Exception as e:
            logger.debug("cognition_stage_failed stage=%s error=%s", stage.name, e)
            continue

        if not isinstance(r, dict) or not r.get("success") or not r.get("text"):
            continue
        prior_text = str(r["text"])
        last_result = r
        stages_run += 1

    if not last_result:
        return await route_fn(workspace, task_type, prompt, history, files,
                              max_tokens, temp, tier, user, context, request)

    last_result["cognitive"] = {
        "tier": c.tier,
        "score": round(c.score, 3),
        "stages_run": stages_run,
        "stages_attempted": stages_attempted,
        "stages_skipped": stages_skipped,
        "stages_total": len(stages),
        "reasons": list(c.reasons),
    }
=======
# core/cognitive_pipeline.py
"""
AXELR — Asynchronous Multi-Agent Cognitive Pipelining (v2.1)
=============================================================
Slices a deep-tier request into 2–3 micro-specialist passes, each with
its own directive and token budget, all routed through the SAME
`route_fn` chain — no new providers, no new SDK, no added RAM.

Stage chain (ascending cost)
----------------------------
    logic      →  factual core, identifiers, code, numbers
    editorial  →  polish prose, strip hedging, match request shape
    verify     →  fix fences, contradictions, refusals, truncation
                  (only for `critical` tier)

v2.1 accepts a pre-assessed Complexity via `_preassessed` so callers
like core.quality_conductor do not re-run classification.  Also
reports stages_attempted / stages_skipped in the metadata block.

Contract
--------
    from core.cognitive_pipeline import run
    result = await run(route_fn, workspace=..., task_type=..., ...,
                       _preassessed=complexity)   # optional

Guarantees
----------
* Never raises — every stage is individually guarded.
* Never duplicates a call — a stage is skipped if the deadline is spent.
* Falls through to a single-shot `route_fn` call on any internal failure.

RAM
---
  Constant. Work buffers discarded at return.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

from core.task_shedder import Complexity, assess

logger = logging.getLogger("axelr.cognition")

__all__ = ["run", "Stage"]

RouteFn = Callable[..., Awaitable[dict[str, Any]]]


_LOGIC_DIRECTIVE = (
    "<cognitive_stage name=\"logic\">\n"
    "Produce ONLY the factual/logical core of the answer.\n"
    "• Bullet list of correct, verifiable claims.\n"
    "• Include every identifier, code block, and numeric fact the final\n"
    "  answer must contain.\n"
    "• No preamble. No prose polish. No sign-off.\n"
    "• No apologies, no 'here is', no meta-commentary.\n"
    "</cognitive_stage>"
)

_EDITORIAL_DIRECTIVE = (
    "<cognitive_stage name=\"editorial\">\n"
    "Rewrite the provided draft as the final user-facing answer.\n"
    "• Preserve EVERY identifier, number, and code block from the draft.\n"
    "• Remove hedging, filler, and meta-commentary.\n"
    "• Match the user's request shape (code / table / prose / blueprint).\n"
    "• Add zero new claims — do not invent.\n"
    "• If the request asked for code, output a single language-tagged fence.\n"
    "</cognitive_stage>"
)

_VERIFY_DIRECTIVE = (
    "<cognitive_stage name=\"verify\">\n"
    "Audit the draft. Fix ONLY the following classes of defect:\n"
    "• Unclosed code fences, broken brackets, truncation mid-sentence.\n"
    "• Contradictions or self-corrections left in the text.\n"
    "• Refusal markers (\"I cannot\", \"As an AI\") that leaked through.\n"
    "• Prompt-leak markers (<system_identity>, MASTER_PROMPT).\n"
    "Return the FULL corrected answer. Do not rewrite correct content.\n"
    "</cognitive_stage>"
)


@dataclass(frozen=True, slots=True)
class Stage:
    name: str
    directive: str
    tokens_frac: float
    min_prior_words: int = 0


_STAGES_STANDARD: tuple[Stage, ...] = (
    Stage("logic",     _LOGIC_DIRECTIVE,     0.60),
    Stage("editorial", _EDITORIAL_DIRECTIVE, 0.40, min_prior_words=40),
)

_STAGES_CRITICAL: tuple[Stage, ...] = (
    Stage("logic",     _LOGIC_DIRECTIVE,     0.50),
    Stage("editorial", _EDITORIAL_DIRECTIVE, 0.30, min_prior_words=40),
    Stage("verify",    _VERIFY_DIRECTIVE,    0.20, min_prior_words=60),
)


def _select_stages(c: Complexity, max_tokens: int) -> tuple[Stage, ...]:
    if c.tier == "critical":
        return _STAGES_CRITICAL
    if c.tier == "deep":
        return _STAGES_STANDARD
    return ()


def _stage_prompt(stage: Stage, user_prompt: str, prior_text: str) -> str:
    if not prior_text:
        return (
            f"{stage.directive}\n\n"
            f"<original_request>\n{user_prompt[:2000]}\n</original_request>"
        )
    return (
        f"{stage.directive}\n\n"
        f"<original_request>\n{user_prompt[:2000]}\n</original_request>\n\n"
        f"<prior_draft>\n{prior_text[:6000]}\n</prior_draft>"
    )


async def run(
    route_fn: RouteFn,
    *,
    workspace: str,
    task_type: str,
    prompt: str,
    history: Iterable[dict] | None,
    files: Iterable[dict] | None,
    max_tokens: int,
    temp: float,
    tier: str,
    user: dict | None = None,
    context: str = "",
    request: Any = None,
    budget_s: float = 22.0,
    _preassessed: "Complexity | None" = None,
) -> dict[str, Any]:
    """
    Escalate a deep request through the cognitive stages.  If the request
    is not deep, or the pipeline fails on any axis, this delegates to a
    single-shot `route_fn` call and returns.
    """
    # Classification — reuse if the caller already did it.
    if _preassessed is not None:
        c = _preassessed
    else:
        try:
            c = assess(prompt, history=history, files=files)
        except Exception:
            c = None

    if c is None or not c.is_deep:
        return await route_fn(workspace, task_type, prompt, history, files,
                              max_tokens, temp, tier, user, context, request)

    stages = _select_stages(c, max_tokens)
    if not stages:
        return await route_fn(workspace, task_type, prompt, history, files,
                              max_tokens, temp, tier, user, context, request)

    deadline = time.monotonic() + max(4.0, float(budget_s))
    prior_text = ""
    last_result: dict[str, Any] = {}
    stages_run = 0
    stages_attempted = 0
    stages_skipped = 0

    for stage in stages:
        if time.monotonic() >= deadline - 1.0:
            stages_skipped += 1
            continue
        if stage.min_prior_words and len(prior_text.split()) < stage.min_prior_words:
            stages_skipped += 1
            continue
        stages_attempted += 1
        stage_budget = max(128, int(max_tokens * stage.tokens_frac))
        stage_prompt = _stage_prompt(stage, prompt, prior_text)
        remaining = max(2.0, deadline - time.monotonic())
        try:
            r = await asyncio.wait_for(
                route_fn(
                    workspace, task_type, stage_prompt,
                    None, files, stage_budget, temp, tier,
                    user, context, request,
                ),
                timeout=remaining,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.debug("cognition_stage_timeout stage=%s", stage.name)
            break
        except Exception as e:
            logger.debug("cognition_stage_failed stage=%s error=%s", stage.name, e)
            continue

        if not isinstance(r, dict) or not r.get("success") or not r.get("text"):
            continue
        prior_text = str(r["text"])
        last_result = r
        stages_run += 1

    if not last_result:
        return await route_fn(workspace, task_type, prompt, history, files,
                              max_tokens, temp, tier, user, context, request)

    last_result["cognitive"] = {
        "tier": c.tier,
        "score": round(c.score, 3),
        "stages_run": stages_run,
        "stages_attempted": stages_attempted,
        "stages_skipped": stages_skipped,
        "stages_total": len(stages),
        "reasons": list(c.reasons),
    }
>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
    return last_result