<<<<<<< HEAD
# core/routing.py
"""
AXELR — Routing Layer
======================
Single home for the DAG orchestrator used by ``api.routes_agents``:

  * Orchestrator  — PLAN → EXECUTE → CRITIQUE → REVISE → SYNTHESIZE
                    with SSE-streamable events.
  * Subtask       — DAG node model.

Workspace classification lives in ``api.workspace_detector``.
Intent classification lives in ``core.context.classify``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("axelr.routing")


__all__ = ["Orchestrator", "Subtask",
           "PLANNER_PROMPT", "CRITIC_PROMPT", "REVISION_PROMPT",
           "SYNTH_PROMPT", "ROLE_SYSTEMS"]


# ═══════════════════════════════════════════════════════════════════════════
# Section 2 — Orchestrator
# ═══════════════════════════════════════════════════════════════════════════
# PLAN → EXECUTE → CRITIQUE → REVISE (loop) → SYNTHESIZE, with SSE-streamable
# events.
#
# Design:
#   - `stream()` yields typed dicts so the frontend can render a live trace.
#   - `run()` is a thin aggregator over `stream()` for non-streaming callers.
#   - A DAG executor runs subtasks in parallel where dependencies allow.
#   - Full critic loop: if critique returns "revise", automatically re-run
#     only the flagged subtasks and their dependents, then re-critique.
#   - Every LLM call goes through the caller-supplied `route_fn`, so all
#     existing provider racing, quota, and semantic caching still apply.
#
# EVENTS (each is a dict with a `type` key):
#   {"type":"phase",        "name": "planning"|"executing"|"critique"|"revising"|"synthesis"}
#   {"type":"plan",         "subtasks": [Subtask.to_public(), ...]}
#   {"type":"subtask_start","id": "s1", "role": "code", "instruction": "...", "revision":1}
#   {"type":"subtask_done", "id": "s1", "output": "...", "latency_ms": 812.3, "error": null, "revision":1}
#   {"type":"critique",     "verdict":"pass"|"revise", "issues":[...], "severity":"low|med|high", "iteration":1}
#   {"type":"revision_loop","iteration":2, "revising": ["s1", "s3"]}
#   {"type":"final",        "text": "..."}
#   {"type":"done",         "total_latency_ms": 4120.0}
#   {"type":"error",        "message": "..."}

PLANNER_PROMPT = """You are the PLANNER. Decompose the user task into 2-5
independent subtasks. Each subtask must be solvable by ONE specialist.

Return ONLY valid JSON:
{"subtasks": [{"id": "s1", "role": "code|research|review|data|design|core",
               "instruction": "...", "depends_on": []}]}

Rules:
- A subtask that requires another's output MUST list it in depends_on.
- The critic role is reserved — do not emit it.
- Max 5 subtasks. Min 2.
- If the task is trivial or a single question, emit exactly 1 subtask."""

CRITIC_PROMPT = """You are the CRITIC. You receive the original task and
each subtask's output. For each, output a JSON verdict:

{"overall": "pass|revise",
 "per_subtask": [
   {"id":"s1","verdict":"pass|revise","issues":["..."],"severity":"low|med|high"}
 ],
 "cross_cutting_issues": ["..."]}

Reject any output that is factually wrong, ungrounded, incomplete vs its
instruction, or contradicts another subtask. Return ONLY JSON."""

REVISION_PROMPT = """You are the REVISER. You previously worked on this subtask
but received critique that needs to be addressed.

Original subtask instruction: {instruction}
Previous output: {previous_output}
Critique issues to fix: {issues}

Produce a revised, complete output that addresses ALL the listed issues.
Maintain all working/valid parts of the previous output. Only change what's needed."""

SYNTH_PROMPT = """You are the SYNTHESIZER. Merge the verified subtask
outputs into ONE coherent deliverable. Resolve contradictions explicitly.
Do not introduce new facts. Output the final answer only."""

ROLE_SYSTEMS = {
    "code":     "You are a senior engineer. Produce working, production-ready code only.",
    "research": "You are a researcher. Cite reasoning; flag uncertainty explicitly.",
    "review":   "You are a reviewer. Find concrete bugs, perf, and security issues.",
    "data":     "You are an analyst. Return structured, typed, verifiable output.",
    "design":   "You are a UI engineer. Return complete, self-contained artifacts.",
    "core":     "You are a generalist. Be concise and direct.",
}

_VALID_ROLES = set(ROLE_SYSTEMS.keys())


@dataclass
class Subtask:
    id: str
    role: str
    instruction: str
    depends_on: list[str] = field(default_factory=list)
    output: str = ""
    latency_ms: float = 0.0
    error: str | None = None
    revision: int = 0

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "role": self.role,
            "instruction": self.instruction,
            "depends_on": self.depends_on,
            "output": self.output,
            "latency_ms": round(self.latency_ms, 2),
            "error": self.error,
            "revision": self.revision,
        }


class Orchestrator:
    """DAG orchestrator. Safe to construct once and reuse across requests."""

    def __init__(self, route_fn, max_parallel: int = 4):
        self.route = route_fn
        self.sem = asyncio.Semaphore(max_parallel)

    # ── low-level LLM call ──────────────────────────────────────────────
    async def _call(
        self,
        workspace: str,
        system: str,
        user_msg: str,
        tier: str,
        user_obj: dict | None,
        temp: float = 0.2,
    ) -> str:
        prompt = f"{system}\n\n---\n{user_msg}"
        async with self.sem:
            r = await self.route(
                workspace=workspace,
                task_type="structuring",
                prompt=prompt,
                history=[],
                files=[],
                max_tokens=2048,
                temp=temp,
                tier=tier,
                user=user_obj,
            )
        return r.get("text", "") if r.get("success") else ""

    # ── phase 1: plan ───────────────────────────────────────────────────
    async def _plan(self, task: str, tier: str, user: dict | None) -> list[Subtask]:
        raw = await self._call("core", PLANNER_PROMPT, task, tier, user, temp=0.1)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return [Subtask("s1", "core", task)]
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return [Subtask("s1", "core", task)]

        out: list[Subtask] = []
        seen_ids: set[str] = set()
        for idx, s in enumerate(data.get("subtasks", [])[:5]):
            sid = str(s.get("id") or f"s{idx+1}")
            if sid in seen_ids:
                sid = f"{sid}_{idx}"
            seen_ids.add(sid)
            role = s.get("role", "core")
            if role not in _VALID_ROLES:
                role = "core"
            out.append(
                Subtask(
                    id=sid,
                    role=role,
                    instruction=str(s.get("instruction") or task),
                    depends_on=[str(d) for d in (s.get("depends_on") or [])],
                )
            )
        return out or [Subtask("s1", "core", task)]

    # ── helper: transitive dependents ───────────────────────────────────
    def _get_dependent_subtasks(self, subtask_id: str, subtasks: list[Subtask]) -> set[str]:
        dependents: set[str] = set()
        to_check = [subtask_id]
        while to_check:
            current = to_check.pop()
            for st in subtasks:
                if st.id not in dependents and current in st.depends_on:
                    dependents.add(st.id)
                    to_check.append(st.id)
        return dependents

    # ── run one subtask (first run + revisions) ─────────────────────────
    async def _run_one(
        self,
        st: Subtask,
        ctx: dict[str, str],
        tier: str,
        user: dict | None,
        critique_issues: list[str] | None = None,
    ) -> None:
        t0 = time.time()
        dep_output = "\n\n".join(
            f"[{d}] {ctx.get(d, '')}" for d in st.depends_on if ctx.get(d)
        )

        if st.revision > 0 and critique_issues:
            user_msg = REVISION_PROMPT.format(
                instruction=st.instruction,
                previous_output=st.output,
                issues=", ".join(critique_issues),
            )
        else:
            user_msg = st.instruction

        system_prompt = ROLE_SYSTEMS.get(st.role, ROLE_SYSTEMS["core"])
        if dep_output:
            user_msg += f"\n\nUpstream results you must build on:\n{dep_output}"

        try:
            st.output = await self._call("core", system_prompt, user_msg, tier, user)
            st.error = None
        except Exception as e:                                    # noqa: BLE001
            st.error = str(e)[:200]
            st.output = ""
        st.latency_ms = (time.time() - t0) * 1000

    async def _execute_dag_stream(
        self,
        subtasks: list[Subtask],
        tier: str,
        user: dict | None,
        subtask_issues: dict[str, list[str]] | None = None,
        existing_ctx: dict[str, str] | None = None,
    ) -> AsyncGenerator[dict, None]:
        ctx: dict[str, str] = dict(existing_ctx) if existing_ctx is not None else {}
        pending = {s.id: s for s in subtasks}
        guard_iterations = 0
        subtask_issues = subtask_issues or {}

        while pending and guard_iterations < 10:
            guard_iterations += 1
            ready = [s for s in pending.values() if all(d in ctx for d in s.depends_on)]
            if not ready:
                ready = list(pending.values())

            for s in ready:
                yield {
                    "type": "subtask_start",
                    "id": s.id,
                    "role": s.role,
                    "instruction": s.instruction,
                    "revision": s.revision,
                }

            run_tasks = []
            for s in ready:
                issues = subtask_issues.get(s.id)
                run_tasks.append(self._run_one(s, ctx, tier, user, critique_issues=issues))
            await asyncio.gather(*run_tasks)

            for s in ready:
                ctx[s.id] = s.output
                pending.pop(s.id, None)
                yield {
                    "type": "subtask_done",
                    "id": s.id,
                    "output": s.output,
                    "latency_ms": round(s.latency_ms, 2),
                    "error": s.error,
                    "revision": s.revision,
                }

    # ── phase 3: critique ───────────────────────────────────────────────
    async def _critique(
        self, task: str, subtasks: list[Subtask], tier: str, user: dict | None,
    ) -> dict:
        bundle = json.dumps(
            [{"id": s.id, "role": s.role, "instruction": s.instruction,
              "output": (s.output or "")[:4000]} for s in subtasks],
            indent=2,
        )
        raw = await self._call(
            "core", CRITIC_PROMPT,
            f"TASK: {task}\n\nSUBTASK OUTPUTS:\n{bundle}",
            tier, user, temp=0.0,
        )
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return {"overall": "pass", "per_subtask": [], "cross_cutting_issues": []}
        try:
            return json.loads(m.group(0))
        except Exception:
            return {"overall": "pass", "per_subtask": [], "cross_cutting_issues": []}

    # ── phase 4: synthesize ─────────────────────────────────────────────
    async def _synthesize(
        self, task: str, subtasks: list[Subtask], critique: dict,
        tier: str, user: dict | None,
    ) -> str:
        bundle = "\n\n".join(f"### [{s.id} · {s.role}]\n{s.output}" for s in subtasks)
        return await self._call(
            "core", SYNTH_PROMPT,
            (
                f"ORIGINAL TASK:\n{task}\n\n"
                f"VERIFIED SUBTASK OUTPUTS:\n{bundle}\n\n"
                f"CRITIC NOTES:\n{json.dumps(critique)}"
            ),
            tier, user, temp=0.3,
        )

    # ── public: stream ──────────────────────────────────────────────────
    async def stream(
        self, task: str, tier: str, user: dict | None, max_revisions: int = 2,
    ) -> AsyncGenerator[dict, None]:
        t_start = time.time()
        revision_iteration = 0
        try:
            yield {"type": "phase", "name": "planning"}
            plan = await self._plan(task, tier, user)
            subtask_by_id = {s.id: s for s in plan}
            yield {"type": "plan", "subtasks": [s.to_public() for s in plan]}

            yield {"type": "phase", "name": "executing"}
            full_ctx: dict[str, str] = {}
            async for evt in self._execute_dag_stream(plan, tier, user):
                if evt.get("type") == "subtask_done" and evt.get("output"):
                    full_ctx[evt["id"]] = evt["output"]
                yield evt

            while revision_iteration <= max_revisions:
                yield {"type": "phase", "name": "critique"}
                crit = await self._critique(task, plan, tier, user)
                yield {"type": "critique", **crit, "iteration": revision_iteration + 1}

                if crit.get("overall") != "revise" or revision_iteration >= max_revisions:
                    break

                revision_iteration += 1
                yield {"type": "phase", "name": "revising"}

                to_revise: set[str] = set()
                subtask_issues: dict[str, list[str]] = {}
                for issue in crit.get("per_subtask", []):
                    if issue.get("verdict") == "revise" and issue["id"] in subtask_by_id:
                        to_revise.add(issue["id"])
                        subtask_issues[issue["id"]] = issue.get("issues", [])
                        subtask_by_id[issue["id"]].revision += 1

                for subtask_id in list(to_revise):
                    for dep_id in self._get_dependent_subtasks(subtask_id, plan):
                        if dep_id not in to_revise:
                            to_revise.add(dep_id)
                            subtask_by_id[dep_id].revision += 1

                yield {
                    "type": "revision_loop",
                    "iteration": revision_iteration + 1,
                    "revising": list(to_revise),
                }

                revision_subtasks = [subtask_by_id[sid] for sid in to_revise]
                async for evt in self._execute_dag_stream(
                    revision_subtasks, tier, user, subtask_issues, full_ctx,
                ):
                    if evt.get("type") == "subtask_done" and evt.get("output"):
                        full_ctx[evt["id"]] = evt["output"]
                    yield evt

            yield {"type": "phase", "name": "synthesis"}
            final = await self._synthesize(task, plan, crit, tier, user)
            yield {"type": "final", "text": final}
            yield {
                "type": "done",
                "total_latency_ms": round((time.time() - t_start) * 1000, 2),
                "revision_iterations": revision_iteration,
            }
        except asyncio.CancelledError:
            raise
        except Exception as e:                                    # noqa: BLE001
            yield {"type": "error", "message": str(e)[:300]}

    # ── public: aggregate ───────────────────────────────────────────────
    async def run(
        self, task: str, tier: str, user: dict | None, max_revisions: int = 2,
    ) -> dict:
        plan: list[dict] = []
        crit: dict = {}
        final: str = ""
        total_ms: float = 0.0
        error: str | None = None
        revision_iterations: int = 0

        async for evt in self.stream(task, tier, user, max_revisions=max_revisions):
            t = evt.get("type")
            if t == "plan":
                plan = evt["subtasks"]
            elif t == "critique":
                crit = {k: v for k, v in evt.items() if k != "type"}
            elif t == "final":
                final = evt["text"]
            elif t == "done":
                total_ms = evt["total_latency_ms"]
                revision_iterations = evt.get("revision_iterations", 0)
            elif t == "error":
                error = evt["message"]

        return {
            "success": error is None,
            "plan": plan,
            "critique": crit,
            "final": final,
            "revision_iterations": revision_iterations,
            "agent_responses": [
                {"id": s["id"], "role": s["role"], "response": s["output"],
                 "revision": s.get("revision", 0)}
                for s in plan
            ],
            "total_latency_ms": total_ms,
            "error": error,
        }

__all__ = [
    # Orchestration (live surface)
    "Orchestrator", "Subtask",
    # Prompts
    "PLANNER_PROMPT", "CRITIC_PROMPT", "REVISION_PROMPT", "SYNTH_PROMPT",
    "ROLE_SYSTEMS",
=======
# core/routing.py
"""
AXELR — Routing Layer
======================
Single home for the DAG orchestrator used by ``api.routes_agents``:

  * Orchestrator  — PLAN → EXECUTE → CRITIQUE → REVISE → SYNTHESIZE
                    with SSE-streamable events.
  * Subtask       — DAG node model.

Workspace classification lives in ``api.workspace_detector``.
Intent classification lives in ``core.context.classify``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("axelr.routing")


__all__ = ["Orchestrator", "Subtask",
           "PLANNER_PROMPT", "CRITIC_PROMPT", "REVISION_PROMPT",
           "SYNTH_PROMPT", "ROLE_SYSTEMS"]


# ═══════════════════════════════════════════════════════════════════════════
# Section 2 — Orchestrator
# ═══════════════════════════════════════════════════════════════════════════
# PLAN → EXECUTE → CRITIQUE → REVISE (loop) → SYNTHESIZE, with SSE-streamable
# events.
#
# Design:
#   - `stream()` yields typed dicts so the frontend can render a live trace.
#   - `run()` is a thin aggregator over `stream()` for non-streaming callers.
#   - A DAG executor runs subtasks in parallel where dependencies allow.
#   - Full critic loop: if critique returns "revise", automatically re-run
#     only the flagged subtasks and their dependents, then re-critique.
#   - Every LLM call goes through the caller-supplied `route_fn`, so all
#     existing provider racing, quota, and semantic caching still apply.
#
# EVENTS (each is a dict with a `type` key):
#   {"type":"phase",        "name": "planning"|"executing"|"critique"|"revising"|"synthesis"}
#   {"type":"plan",         "subtasks": [Subtask.to_public(), ...]}
#   {"type":"subtask_start","id": "s1", "role": "code", "instruction": "...", "revision":1}
#   {"type":"subtask_done", "id": "s1", "output": "...", "latency_ms": 812.3, "error": null, "revision":1}
#   {"type":"critique",     "verdict":"pass"|"revise", "issues":[...], "severity":"low|med|high", "iteration":1}
#   {"type":"revision_loop","iteration":2, "revising": ["s1", "s3"]}
#   {"type":"final",        "text": "..."}
#   {"type":"done",         "total_latency_ms": 4120.0}
#   {"type":"error",        "message": "..."}

PLANNER_PROMPT = """You are the PLANNER. Decompose the user task into 2-5
independent subtasks. Each subtask must be solvable by ONE specialist.

Return ONLY valid JSON:
{"subtasks": [{"id": "s1", "role": "code|research|review|data|design|core",
               "instruction": "...", "depends_on": []}]}

Rules:
- A subtask that requires another's output MUST list it in depends_on.
- The critic role is reserved — do not emit it.
- Max 5 subtasks. Min 2.
- If the task is trivial or a single question, emit exactly 1 subtask."""

CRITIC_PROMPT = """You are the CRITIC. You receive the original task and
each subtask's output. For each, output a JSON verdict:

{"overall": "pass|revise",
 "per_subtask": [
   {"id":"s1","verdict":"pass|revise","issues":["..."],"severity":"low|med|high"}
 ],
 "cross_cutting_issues": ["..."]}

Reject any output that is factually wrong, ungrounded, incomplete vs its
instruction, or contradicts another subtask. Return ONLY JSON."""

REVISION_PROMPT = """You are the REVISER. You previously worked on this subtask
but received critique that needs to be addressed.

Original subtask instruction: {instruction}
Previous output: {previous_output}
Critique issues to fix: {issues}

Produce a revised, complete output that addresses ALL the listed issues.
Maintain all working/valid parts of the previous output. Only change what's needed."""

SYNTH_PROMPT = """You are the SYNTHESIZER. Merge the verified subtask
outputs into ONE coherent deliverable. Resolve contradictions explicitly.
Do not introduce new facts. Output the final answer only."""

ROLE_SYSTEMS = {
    "code":     "You are a senior engineer. Produce working, production-ready code only.",
    "research": "You are a researcher. Cite reasoning; flag uncertainty explicitly.",
    "review":   "You are a reviewer. Find concrete bugs, perf, and security issues.",
    "data":     "You are an analyst. Return structured, typed, verifiable output.",
    "design":   "You are a UI engineer. Return complete, self-contained artifacts.",
    "core":     "You are a generalist. Be concise and direct.",
}

_VALID_ROLES = set(ROLE_SYSTEMS.keys())


@dataclass
class Subtask:
    id: str
    role: str
    instruction: str
    depends_on: list[str] = field(default_factory=list)
    output: str = ""
    latency_ms: float = 0.0
    error: str | None = None
    revision: int = 0

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "role": self.role,
            "instruction": self.instruction,
            "depends_on": self.depends_on,
            "output": self.output,
            "latency_ms": round(self.latency_ms, 2),
            "error": self.error,
            "revision": self.revision,
        }


class Orchestrator:
    """DAG orchestrator. Safe to construct once and reuse across requests."""

    def __init__(self, route_fn, max_parallel: int = 4):
        self.route = route_fn
        self.sem = asyncio.Semaphore(max_parallel)

    # ── low-level LLM call ──────────────────────────────────────────────
    async def _call(
        self,
        workspace: str,
        system: str,
        user_msg: str,
        tier: str,
        user_obj: dict | None,
        temp: float = 0.2,
    ) -> str:
        prompt = f"{system}\n\n---\n{user_msg}"
        async with self.sem:
            r = await self.route(
                workspace=workspace,
                task_type="structuring",
                prompt=prompt,
                history=[],
                files=[],
                max_tokens=2048,
                temp=temp,
                tier=tier,
                user=user_obj,
            )
        return r.get("text", "") if r.get("success") else ""

    # ── phase 1: plan ───────────────────────────────────────────────────
    async def _plan(self, task: str, tier: str, user: dict | None) -> list[Subtask]:
        raw = await self._call("core", PLANNER_PROMPT, task, tier, user, temp=0.1)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return [Subtask("s1", "core", task)]
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return [Subtask("s1", "core", task)]

        out: list[Subtask] = []
        seen_ids: set[str] = set()
        for idx, s in enumerate(data.get("subtasks", [])[:5]):
            sid = str(s.get("id") or f"s{idx+1}")
            if sid in seen_ids:
                sid = f"{sid}_{idx}"
            seen_ids.add(sid)
            role = s.get("role", "core")
            if role not in _VALID_ROLES:
                role = "core"
            out.append(
                Subtask(
                    id=sid,
                    role=role,
                    instruction=str(s.get("instruction") or task),
                    depends_on=[str(d) for d in (s.get("depends_on") or [])],
                )
            )
        return out or [Subtask("s1", "core", task)]

    # ── helper: transitive dependents ───────────────────────────────────
    def _get_dependent_subtasks(self, subtask_id: str, subtasks: list[Subtask]) -> set[str]:
        dependents: set[str] = set()
        to_check = [subtask_id]
        while to_check:
            current = to_check.pop()
            for st in subtasks:
                if st.id not in dependents and current in st.depends_on:
                    dependents.add(st.id)
                    to_check.append(st.id)
        return dependents

    # ── run one subtask (first run + revisions) ─────────────────────────
    async def _run_one(
        self,
        st: Subtask,
        ctx: dict[str, str],
        tier: str,
        user: dict | None,
        critique_issues: list[str] | None = None,
    ) -> None:
        t0 = time.time()
        dep_output = "\n\n".join(
            f"[{d}] {ctx.get(d, '')}" for d in st.depends_on if ctx.get(d)
        )

        if st.revision > 0 and critique_issues:
            user_msg = REVISION_PROMPT.format(
                instruction=st.instruction,
                previous_output=st.output,
                issues=", ".join(critique_issues),
            )
        else:
            user_msg = st.instruction

        system_prompt = ROLE_SYSTEMS.get(st.role, ROLE_SYSTEMS["core"])
        if dep_output:
            user_msg += f"\n\nUpstream results you must build on:\n{dep_output}"

        try:
            st.output = await self._call("core", system_prompt, user_msg, tier, user)
            st.error = None
        except Exception as e:                                    # noqa: BLE001
            st.error = str(e)[:200]
            st.output = ""
        st.latency_ms = (time.time() - t0) * 1000

    async def _execute_dag_stream(
        self,
        subtasks: list[Subtask],
        tier: str,
        user: dict | None,
        subtask_issues: dict[str, list[str]] | None = None,
        existing_ctx: dict[str, str] | None = None,
    ) -> AsyncGenerator[dict, None]:
        ctx: dict[str, str] = dict(existing_ctx) if existing_ctx is not None else {}
        pending = {s.id: s for s in subtasks}
        guard_iterations = 0
        subtask_issues = subtask_issues or {}

        while pending and guard_iterations < 10:
            guard_iterations += 1
            ready = [s for s in pending.values() if all(d in ctx for d in s.depends_on)]
            if not ready:
                ready = list(pending.values())

            for s in ready:
                yield {
                    "type": "subtask_start",
                    "id": s.id,
                    "role": s.role,
                    "instruction": s.instruction,
                    "revision": s.revision,
                }

            run_tasks = []
            for s in ready:
                issues = subtask_issues.get(s.id)
                run_tasks.append(self._run_one(s, ctx, tier, user, critique_issues=issues))
            await asyncio.gather(*run_tasks)

            for s in ready:
                ctx[s.id] = s.output
                pending.pop(s.id, None)
                yield {
                    "type": "subtask_done",
                    "id": s.id,
                    "output": s.output,
                    "latency_ms": round(s.latency_ms, 2),
                    "error": s.error,
                    "revision": s.revision,
                }

    # ── phase 3: critique ───────────────────────────────────────────────
    async def _critique(
        self, task: str, subtasks: list[Subtask], tier: str, user: dict | None,
    ) -> dict:
        bundle = json.dumps(
            [{"id": s.id, "role": s.role, "instruction": s.instruction,
              "output": (s.output or "")[:4000]} for s in subtasks],
            indent=2,
        )
        raw = await self._call(
            "core", CRITIC_PROMPT,
            f"TASK: {task}\n\nSUBTASK OUTPUTS:\n{bundle}",
            tier, user, temp=0.0,
        )
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return {"overall": "pass", "per_subtask": [], "cross_cutting_issues": []}
        try:
            return json.loads(m.group(0))
        except Exception:
            return {"overall": "pass", "per_subtask": [], "cross_cutting_issues": []}

    # ── phase 4: synthesize ─────────────────────────────────────────────
    async def _synthesize(
        self, task: str, subtasks: list[Subtask], critique: dict,
        tier: str, user: dict | None,
    ) -> str:
        bundle = "\n\n".join(f"### [{s.id} · {s.role}]\n{s.output}" for s in subtasks)
        return await self._call(
            "core", SYNTH_PROMPT,
            (
                f"ORIGINAL TASK:\n{task}\n\n"
                f"VERIFIED SUBTASK OUTPUTS:\n{bundle}\n\n"
                f"CRITIC NOTES:\n{json.dumps(critique)}"
            ),
            tier, user, temp=0.3,
        )

    # ── public: stream ──────────────────────────────────────────────────
    async def stream(
        self, task: str, tier: str, user: dict | None, max_revisions: int = 2,
    ) -> AsyncGenerator[dict, None]:
        t_start = time.time()
        revision_iteration = 0
        try:
            yield {"type": "phase", "name": "planning"}
            plan = await self._plan(task, tier, user)
            subtask_by_id = {s.id: s for s in plan}
            yield {"type": "plan", "subtasks": [s.to_public() for s in plan]}

            yield {"type": "phase", "name": "executing"}
            full_ctx: dict[str, str] = {}
            async for evt in self._execute_dag_stream(plan, tier, user):
                if evt.get("type") == "subtask_done" and evt.get("output"):
                    full_ctx[evt["id"]] = evt["output"]
                yield evt

            while revision_iteration <= max_revisions:
                yield {"type": "phase", "name": "critique"}
                crit = await self._critique(task, plan, tier, user)
                yield {"type": "critique", **crit, "iteration": revision_iteration + 1}

                if crit.get("overall") != "revise" or revision_iteration >= max_revisions:
                    break

                revision_iteration += 1
                yield {"type": "phase", "name": "revising"}

                to_revise: set[str] = set()
                subtask_issues: dict[str, list[str]] = {}
                for issue in crit.get("per_subtask", []):
                    if issue.get("verdict") == "revise" and issue["id"] in subtask_by_id:
                        to_revise.add(issue["id"])
                        subtask_issues[issue["id"]] = issue.get("issues", [])
                        subtask_by_id[issue["id"]].revision += 1

                for subtask_id in list(to_revise):
                    for dep_id in self._get_dependent_subtasks(subtask_id, plan):
                        if dep_id not in to_revise:
                            to_revise.add(dep_id)
                            subtask_by_id[dep_id].revision += 1

                yield {
                    "type": "revision_loop",
                    "iteration": revision_iteration + 1,
                    "revising": list(to_revise),
                }

                revision_subtasks = [subtask_by_id[sid] for sid in to_revise]
                async for evt in self._execute_dag_stream(
                    revision_subtasks, tier, user, subtask_issues, full_ctx,
                ):
                    if evt.get("type") == "subtask_done" and evt.get("output"):
                        full_ctx[evt["id"]] = evt["output"]
                    yield evt

            yield {"type": "phase", "name": "synthesis"}
            final = await self._synthesize(task, plan, crit, tier, user)
            yield {"type": "final", "text": final}
            yield {
                "type": "done",
                "total_latency_ms": round((time.time() - t_start) * 1000, 2),
                "revision_iterations": revision_iteration,
            }
        except asyncio.CancelledError:
            raise
        except Exception as e:                                    # noqa: BLE001
            yield {"type": "error", "message": str(e)[:300]}

    # ── public: aggregate ───────────────────────────────────────────────
    async def run(
        self, task: str, tier: str, user: dict | None, max_revisions: int = 2,
    ) -> dict:
        plan: list[dict] = []
        crit: dict = {}
        final: str = ""
        total_ms: float = 0.0
        error: str | None = None
        revision_iterations: int = 0

        async for evt in self.stream(task, tier, user, max_revisions=max_revisions):
            t = evt.get("type")
            if t == "plan":
                plan = evt["subtasks"]
            elif t == "critique":
                crit = {k: v for k, v in evt.items() if k != "type"}
            elif t == "final":
                final = evt["text"]
            elif t == "done":
                total_ms = evt["total_latency_ms"]
                revision_iterations = evt.get("revision_iterations", 0)
            elif t == "error":
                error = evt["message"]

        return {
            "success": error is None,
            "plan": plan,
            "critique": crit,
            "final": final,
            "revision_iterations": revision_iterations,
            "agent_responses": [
                {"id": s["id"], "role": s["role"], "response": s["output"],
                 "revision": s.get("revision", 0)}
                for s in plan
            ],
            "total_latency_ms": total_ms,
            "error": error,
        }

__all__ = [
    # Orchestration (live surface)
    "Orchestrator", "Subtask",
    # Prompts
    "PLANNER_PROMPT", "CRITIC_PROMPT", "REVISION_PROMPT", "SYNTH_PROMPT",
    "ROLE_SYSTEMS",
>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
]