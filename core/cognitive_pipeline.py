# core/cognitive_pipeline.py
"""
AXELR — Asynchronous Multi-Agent Cognitive Pipelining (Cognitive Stage 2)
==========================================================================
Slices a deep-tier request into 2–3 micro-specialist passes, each with
its own directive and token budget, all routed through the SAME
`route_ai_request_parallel` chain — no new providers, no new SDK, no
added RAM footprint.

Stage chain (ascending cost)
----------------------------
    logic      →  factual core, identifiers, code, numbers
    editorial  →  polish prose, strip hedging, match request shape
    verify     →  fix fences, contradictions, refusals, truncation
                  (only for `critical` tier)

Contract
--------
    from core.cognitive_pipeline import run
    result = await run(route_fn, workspace=..., task_type=..., ...)

Guarantees
----------
* Never raises — every stage is individually guarded.
* Never duplicates a call — a stage is skipped if the deadline is spent.
* Falls through to a single-shot `route_fn` call on any internal failure.
* The final returned dict is always the *last successful* stage's result,
  carrying a `cognitive` metadata block.

RAM
---
  Constant. Work buffers discarded at return.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

from core.task_shedder import Complexity, assess

logger = logging.getLogger("axelr.cognition")

__all__ = ["run", "Stage"]

RouteFn = Callable[..., Awaitable[dict[str, Any]]]


# ── Stage directives (compiled once) ─────────────────────────────────────
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
    min_prior_words: int = 0        # skip if the prior draft is shorter than this


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
) -> dict[str, Any]:
    """
    Escalate a deep request through the cognitive stages. If the request
    is not deep, or the pipeline fails on any axis, this delegates to a
    single-shot `route_fn` call and returns.
    """
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

    for stage in stages:
        if time.monotonic() >= deadline - 1.0:
            break
        if stage.min_prior_words and len(prior_text.split()) < stage.min_prior_words:
            continue
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
        # Total failure of the escalation — fall through to single-shot.
        return await route_fn(workspace, task_type, prompt, history, files,
                              max_tokens, temp, tier, user, context, request)

    last_result["cognitive"] = {
        "tier": c.tier,
        "score": round(c.score, 3),
        "stages_run": stages_run,
        "stages_total": len(stages),
        "reasons": list(c.reasons),
    }
    return last_result