# core/routing.py
"""
AXELR Routing Layer
===================
Consolidated LLM-dispatch services:

  * IntentRouter        — two-tier workspace classifier (rules + ONNX)
  * get_router          — process-wide singleton router
  * Orchestrator        — DAG plan/execute/critique/revise/synthesize
  * Subtask             — DAG node model
  * OpenAICompatProvider / OPENAI_COMPAT / call_openai_compat / make_provider_func
                        — config-driven LLM provider factory

RAM footprint: < 5 MB idle, < 50 MB after ONNX model load.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
logger = logging.getLogger("axelr.routing")
# ── Section: intent_router ───────────────────────────────────────────────────
# AXELR Intent Router
# ===================
# Two-tier workspace classifier:
#
#   Tier 1 (always on): keyword + file-extension rules — sub-millisecond.
#   Tier 2 (lazy):      ONNX Runtime + Xenova/mobilebert-uncased-mnli (~21 MB).
#
# Returns ``{workspace, confidence, method}``.
#
# The ONNX session is created only on first ambiguous request; if onnxruntime
# or the model directory is unavailable, the router silently stays on Tier 1.

_DESIGN_KEYWORDS = re.compile(
    r"""(?ix)
    \b(
        design|ui|ux|mockup|wireframe|frontend|front-end|front\ end|
        html|css|scss|tailwind|bootstrap|react|vue|svelte|angular|
        component|navbar|sidebar|landing\ page|dashboard|layout|
        button|modal|dropdown|responsive|animation|svg|icon|
        prototype|figma|sketch|theme|dark\ mode
    )\b
    """
)

_DATA_KEYWORDS = re.compile(
    r"""(?ix)
    \b(
        extract|analyse|analyze|analysis|dataset|data\ set|csv|tsv|
        excel|xlsx|spreadsheet|sheet|table|column|row|pivot|
        aggregate|summarise|summarize|report|metric|kpi|chart|
        graph|plot|invoice|receipt|tabular|sql|query|database|
        etl|clean|normalise|normalize|pandas|dataframe
    )\b
    """
)

_CODE_KEYWORDS = re.compile(
    r"""(?ix)
    \b(
        function|class|refactor|debug|bug|error|stack\ trace|
        exception|compile|lint|unit\ test|pytest|jest|api\ endpoint|
        regex|algorithm|async|await|promise|closure|recursion
    )\b
    """
)


# File-extension buckets
_DATA_EXTS = {
    ".csv", ".tsv", ".xls", ".xlsx", ".xlsm", ".ods",
    ".parquet", ".feather", ".sql", ".db", ".sqlite",
    ".pdf", ".doc", ".docx",
}

_DESIGN_EXTS = {
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
    ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
    ".fig", ".sketch",
}

_CODE_EXTS = {
    ".py", ".pyi", ".java", ".go", ".rs", ".rb", ".php",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".kt", ".swift", ".scala",
    ".sh", ".bash", ".zsh", ".ps1",
}

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"}


@dataclass(slots=True)
class IntentResult:
    """Outcome of an intent classification."""

    workspace: str            # "design" | "data" | "core"
    confidence: float         # 0.0 – 1.0
    method: str               # "extension" | "rules" | "onnx" | "fallback"

    def to_dict(self) -> dict:
        """Return a JSON-serialisable representation."""
        return {
            "workspace": self.workspace,
            "confidence": round(self.confidence, 4),
            "method": self.method,
        }


class IntentRouter:
    """
    Two-tier intent router.

    Parameters
    ----------
    model_dir : str | None
        Directory containing the ONNX model (``model.onnx``) and tokenizer
        files (``tokenizer.json``). If ``None``, reads ``INTENT_MODEL_DIR``
        from the environment.
    confidence_threshold : float
        Minimum rule-based confidence to short-circuit ONNX. Defaults to 0.75.
    """

    __slots__ = (
        "_confidence_threshold",
        "_labels",
        "_model_dir",
        "_session",
        "_session_lock",
        "_tokenizer",
    )

    def __init__(
        self,
        model_dir: str | None = None,
        *,
        confidence_threshold: float = 0.75,
    ) -> None:
        """Initialise the router; ONNX loads lazily on first ambiguous request."""
        _raw = (model_dir or os.getenv("INTENT_MODEL_DIR") or "").strip()
        # ── Preserve legacy walrus-based resolution ─────────────────────
        self._model_dir: Path | None = (
            Path(raw)
            if (raw := (model_dir or os.getenv("INTENT_MODEL_DIR")))
            else None
        )
        self._confidence_threshold = float(confidence_threshold)
        self._session = None
        self._tokenizer = None
        self._session_lock = asyncio.Lock()
        # MobileBERT-MNLI was trained on these labels.
        self._labels: list[str] = ["data", "design", "core"]

    # -- public API ---------------------------------------------------------

    async def classify(
        self,
        prompt: str,
        files: Sequence[dict[str, str]] | None = None,
    ) -> IntentResult:
        """
        Classify the user request.

        ``files`` is a sequence of dicts each containing at least a
        ``filename`` key (mimetype optional). Never raises.
        """
        try:
            return await self._classify_inner(prompt, files)
        except Exception:
            return IntentResult("core", 0.0, "fallback")

    async def _classify_inner(
        self,
        prompt: str,
        files: Sequence[dict[str, str]] | None,
    ) -> IntentResult:
        prompt = (prompt or "").strip()
        files = list(files or [])

        # 1. File-extension signal
        ext_ws, ext_conf = self._classify_by_files(files)

        # 2. Keyword signal
        kw_ws, kw_conf = self._classify_by_keywords(prompt)

        # 3. Combine — file hints win when strong
        if ext_conf >= 0.9:
            return IntentResult(ext_ws, ext_conf, "extension")

        if kw_conf >= self._confidence_threshold:
            if ext_ws == kw_ws and ext_conf > 0.4:
                return IntentResult(kw_ws, min(1.0, kw_conf + 0.15), "rules")
            return IntentResult(kw_ws, kw_conf, "rules")

        # 4. Ambiguous — try ONNX if configured
        if self._session is not None or self._model_dir is not None:
            onnx_result = await self._classify_onnx(prompt)
            if onnx_result is not None:
                return onnx_result

        # 5. Fallback — use best available weak signal
        if ext_ws != "core":
            return IntentResult(ext_ws, max(0.5, ext_conf), "fallback")
        if kw_ws != "core":
            return IntentResult(kw_ws, max(0.4, kw_conf), "fallback")
        return IntentResult("core", 0.5, "fallback")

    def warm_up(self) -> None:
        """Eagerly load the ONNX model at startup (optional). Never raises."""
        if self._model_dir is None:
            return
        if self._session is None:
            try:
                self._load_onnx()
            except Exception:
                self._session = None

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _classify_by_files(
        files: Sequence[dict[str, str]],
    ) -> tuple[str, float]:
        """Return (workspace, confidence) from file extensions/mimetypes."""
        if not files:
            return "core", 0.0

        data_score = 0
        design_score = 0
        code_score = 0
        image_score = 0

        for f in files:
            name = (f.get("filename") or "").lower()
            ext = Path(name).suffix.lower()
            mimetype = (f.get("mimetype") or "").lower()

            if ext in _DATA_EXTS:
                data_score += 2
            elif ext in _IMAGE_EXTS or mimetype.startswith("image/"):
                image_score += 1
                design_score += 1
            elif ext in _DESIGN_EXTS:
                design_score += 1
            elif ext in _CODE_EXTS:
                code_score += 1

        best = max(data_score, design_score, code_score, image_score)
        if best == 0:
            return "core", 0.0

        if best == data_score and data_score >= 2:
            return "data", 0.9
        if best == design_score and design_score >= 2:
            return "design", 0.85
        if best == code_score and code_score >= 2:
            return "design", 0.8

        # Weak signal
        if data_score > design_score and data_score > code_score:
            return "data", 0.55
        if design_score > data_score and design_score > code_score:
            return "design", 0.55
        return "core", 0.3

    @staticmethod
    def _classify_by_keywords(prompt: str) -> tuple[str, float]:
        """Return (workspace, confidence) from keyword rules."""
        if not prompt:
            return "core", 0.0

        design_hits = len(_DESIGN_KEYWORDS.findall(prompt))
        data_hits = len(_DATA_KEYWORDS.findall(prompt))
        code_hits = len(_CODE_KEYWORDS.findall(prompt))

        total = design_hits + data_hits + code_hits
        if total == 0:
            return "core", 0.0

        weights = {
            "design": design_hits * 1.0,
            "data": data_hits * 1.0,
            "core": code_hits * 0.6,
        }
        best_ws = max(weights, key=weights.get)  # type: ignore[arg-type]
        best_score = weights[best_ws]
        confidence = min(1.0, best_score / max(1.0, total) * 1.4)
        return best_ws, confidence

    async def _classify_onnx(self, prompt: str) -> IntentResult | None:
        """Run the ONNX classifier. Returns ``None`` on any failure."""
        if self._session is None and self._model_dir is not None:
            async with self._session_lock:
                if self._session is None:
                    try:
                        await asyncio.to_thread(self._load_onnx)
                    except Exception:
                        self._session = None
                        return None

        if self._session is None or self._tokenizer is None:
            return None

        try:
            encoded = self._tokenizer.encode(prompt)
            input_ids = list(encoded.ids[:256])
            attention_mask = list(encoded.attention_mask[:256])

            pad_id = 0
            max_len = 256
            if len(input_ids) < max_len:
                pad = max_len - len(input_ids)
                input_ids = input_ids + [pad_id] * pad
                attention_mask = attention_mask + [0] * pad
            else:
                input_ids = input_ids[:max_len]
                attention_mask = attention_mask[:max_len]

            # Lazy numpy import
            import numpy as np

            inputs = {
                "input_ids": np.asarray([input_ids], dtype=np.int64),
                "attention_mask": np.asarray([attention_mask], dtype=np.int64),
            }
            try:
                input_names = {i.name for i in self._session.get_inputs()}
                if "token_type_ids" in input_names:
                    inputs["token_type_ids"] = np.zeros_like(inputs["input_ids"])
            except Exception:
                pass

            outputs = await asyncio.to_thread(self._session.run, None, inputs)
            logits = outputs[0][0]
            exp = np.exp(logits - logits.max())
            probs = exp / exp.sum()
            idx = int(probs.argmax())
            label = self._labels[idx] if idx < len(self._labels) else "core"
            confidence = float(probs[idx])
            if confidence < 0.5:
                return None
            return IntentResult(label, confidence, "onnx")
        except Exception:
            return None

    def _load_onnx(self) -> None:
        """Synchronous ONNX load — invoked via ``asyncio.to_thread``."""
        if self._model_dir is None:
            raise RuntimeError("no model dir configured")

        model_path = self._model_dir / "model.onnx"
        if not model_path.is_file():
            raise FileNotFoundError(f"missing {model_path}")

        # Lazy heavy imports
        import onnxruntime as ort  # type: ignore
        from tokenizers import Tokenizer  # type: ignore

        sess_options = ort.SessionOptions()
        sess_options.intra_op_num_threads = 1
        sess_options.inter_op_num_threads = 1
        sess_options.graph_optimization_level = (
            ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
        )
        session = ort.InferenceSession(
            str(model_path),
            sess_options=sess_options,
            providers=["CPUExecutionProvider"],
        )

        tokenizer_path = self._model_dir / "tokenizer.json"
        if not tokenizer_path.is_file():
            raise FileNotFoundError(f"missing {tokenizer_path}")
        tokenizer = Tokenizer.from_file(str(tokenizer_path))

        self._session = session
        self._tokenizer = tokenizer


_default_router: IntentRouter | None = None


def get_router() -> IntentRouter:
    """Return a lazily-instantiated singleton router."""
    global _default_router
    if _default_router is None:
        _default_router = IntentRouter()
    return _default_router


# ── Section: orchestrator ────────────────────────────────────────────────────
# AXELR Orchestrator v2.1
# ---------------------
# PLAN → EXECUTE → CRITIQUE → REVISE (loop) → SYNTHESIZE, with SSE-streamable events.
#
# Design:
#   - `stream()` yields typed dicts (see EVENTS below) so the frontend can
#     render a live agent trace.
#   - `run()` is a thin aggregator over `stream()` for non-streaming callers.
#   - A DAG executor runs subtasks in parallel where dependencies allow.
#   - Full critic loop: if critique returns "revise", automatically re-run
#     only the flagged subtasks and their dependents, then re-critique.
#   - Every LLM call goes through the caller-supplied `route_fn`, so all
#     existing provider racing, quota, and semantic caching still apply.
#
# EVENTS (each is a dict with `type` key):
#   {"type":"phase",        "name": "planning"|"executing"|"critique"|"revising"|"synthesis"}
#   {"type":"plan",         "subtasks": [Subtask.to_public(), ...]}
#   {"type":"subtask_start","id": "s1", "role": "code", "instruction": "...", "revision":1}
#   {"type":"subtask_done", "id": "s1", "output": "...", "latency_ms": 812.3, "error": null, "revision":1}
#   {"type":"critique",     "verdict":"pass"|"revise", "issues":[...], "severity":"low|med|high", "iteration":1}
#   {"type":"revision_loop", "iteration":2, "revising": ["s1", "s3"]}
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
    revision: int = 0  # tracks number of times this subtask has been revised

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

    # ---- low-level LLM call ----------------------------------------------
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

    # ---- phase 1: plan ---------------------------------------------------
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

    # ---- helper: find all subtasks that depend on a given subtask ------
    def _get_dependent_subtasks(self, subtask_id: str, subtasks: list[Subtask]) -> set[str]:
        """Return all subtask IDs that depend (directly or indirectly) on the given subtask ID."""
        dependents: set[str] = set()
        to_check = [subtask_id]

        while to_check:
            current = to_check.pop()
            for st in subtasks:
                if st.id not in dependents and current in st.depends_on:
                    dependents.add(st.id)
                    to_check.append(st.id)

        return dependents

    # ---- run one subtask (supports first run + revisions) ---------------
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

        # Build appropriate user message based on whether this is a revision
        if st.revision > 0 and critique_issues:
            # This is a revision: use the revision prompt
            user_msg = REVISION_PROMPT.format(
                instruction=st.instruction,
                previous_output=st.output,
                issues=", ".join(critique_issues)
            )
            system_prompt = ROLE_SYSTEMS.get(st.role, ROLE_SYSTEMS["core"])
        else:
            # First run: use standard subtask prompt
            user_msg = st.instruction
            system_prompt = ROLE_SYSTEMS.get(st.role, ROLE_SYSTEMS["core"])

        if dep_output:
            user_msg += f"\n\nUpstream results you must build on:\n{dep_output}"

        try:
            st.output = await self._call(
                "core",
                system_prompt,
                user_msg,
                tier,
                user,
            )
            st.error = None  # Clear error on successful re-run
        except Exception as e:  # noqa: BLE001
            st.error = str(e)[:200]
            st.output = ""
        st.latency_ms = (time.time() - t0) * 1000

    async def _execute_dag_stream(
        self,
        subtasks: list[Subtask],
        tier: str,
        user: dict | None,
        subtask_issues: dict[str, list[str]] | None = None,  # Maps subtask IDs to critique issues
    ) -> AsyncGenerator[dict, None]:
        ctx: dict[str, str] = {}
        pending = {s.id: s for s in subtasks}
        guard_iterations = 0
        subtask_issues = subtask_issues or {}

        while pending and guard_iterations < 10:
            guard_iterations += 1
            ready = [
                s for s in pending.values()
                if all(d in ctx for d in s.depends_on)
            ]
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

            # Run all ready subtasks, passing critique issues if this is a revision
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

    # ---- phase 3: critique ----------------------------------------------
    async def _critique(
        self,
        task: str,
        subtasks: list[Subtask],
        tier: str,
        user: dict | None,
    ) -> dict:
        bundle = json.dumps(
            [
                {
                    "id": s.id,
                    "role": s.role,
                    "instruction": s.instruction,
                    "output": (s.output or "")[:4000],
                }
                for s in subtasks
            ],
            indent=2,
        )
        raw = await self._call(
            "core",
            CRITIC_PROMPT,
            f"TASK: {task}\n\nSUBTASK OUTPUTS:\n{bundle}",
            tier,
            user,
            temp=0.0,
        )
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return {"overall": "pass", "per_subtask": [], "cross_cutting_issues": []}
        try:
            return json.loads(m.group(0))
        except Exception:
            return {"overall": "pass", "per_subtask": [], "cross_cutting_issues": []}

    # ---- phase 4: synthesize --------------------------------------------
    async def _synthesize(
        self,
        task: str,
        subtasks: list[Subtask],
        critique: dict,
        tier: str,
        user: dict | None,
    ) -> str:
        bundle = "\n\n".join(
            f"### [{s.id} · {s.role}]\n{s.output}" for s in subtasks
        )
        return await self._call(
            "core",
            SYNTH_PROMPT,
            (
                f"ORIGINAL TASK:\n{task}\n\n"
                f"VERIFIED SUBTASK OUTPUTS:\n{bundle}\n\n"
                f"CRITIC NOTES:\n{json.dumps(critique)}"
            ),
            tier,
            user,
            temp=0.3,
        )

    # ---- public: stream --------------------------------------------------
    async def stream(
        self,
        task: str,
        tier: str,
        user: dict | None,
        max_revisions: int = 2,  # Max number of full revision cycles
    ) -> AsyncGenerator[dict, None]:
        t_start = time.time()
        revision_iteration = 0
        try:
            # ---- PLAN ----
            yield {"type": "phase", "name": "planning"}
            plan = await self._plan(task, tier, user)
            subtask_by_id = {s.id: s for s in plan}
            yield {"type": "plan", "subtasks": [s.to_public() for s in plan]}

            # First execution pass
            yield {"type": "phase", "name": "executing"}
            async for evt in self._execute_dag_stream(plan, tier, user):
                yield evt

            # Critic loop - iterate until we pass or hit max revisions
            while revision_iteration <= max_revisions:
                # ---- CRITIQUE ----
                yield {"type": "phase", "name": "critique"}
                crit = await self._critique(task, plan, tier, user)
                # Add iteration number to critique event
                yield {"type": "critique", **crit, "iteration": revision_iteration + 1}

                # If we pass or we've hit max revisions, break out of the loop
                if crit.get("overall") != "revise" or revision_iteration >= max_revisions:
                    break

                # Otherwise, we need to revise - start the revision process
                revision_iteration += 1
                yield {"type": "phase", "name": "revising"}

                # Collect all subtasks that need revision
                to_revise = set()
                subtask_issues = {}  # Maps subtask IDs to their critique issues

                for issue in crit.get("per_subtask", []):
                    if issue.get("verdict") == "revise" and issue["id"] in subtask_by_id:
                        to_revise.add(issue["id"])
                        subtask_issues[issue["id"]] = issue.get("issues", [])
                        # Increment revision counter for this subtask
                        subtask_by_id[issue["id"]].revision += 1

                # Add all dependent subtasks (they need to be re-run too since their inputs changed)
                for subtask_id in list(to_revise):
                    dependents = self._get_dependent_subtasks(subtask_id, plan)
                    for dep_id in dependents:
                        if dep_id not in to_revise:
                            to_revise.add(dep_id)
                            subtask_by_id[dep_id].revision += 1

                # Yield event that we're starting a revision loop
                yield {
                    "type": "revision_loop",
                    "iteration": revision_iteration + 1,
                    "revising": list(to_revise)
                }

                # Re-run only the subtasks that need revision (and their dependents)
                revision_subtasks = [subtask_by_id[sid] for sid in to_revise]
                async for evt in self._execute_dag_stream(revision_subtasks, tier, user, subtask_issues):
                    yield evt

            # ---- SYNTHESIZE ----
            yield {"type": "phase", "name": "synthesis"}
            final = await self._synthesize(task, plan, crit, tier, user)
            yield {"type": "final", "text": final}

            yield {
                "type": "done",
                "total_latency_ms": round((time.time() - t_start) * 1000, 2),
                "revision_iterations": revision_iteration
            }
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            yield {"type": "error", "message": str(e)[:300]}

    # ---- public: aggregate (non-streaming callers) ----------------------
    async def run(self, task: str, tier: str, user: dict | None, max_revisions: int = 2) -> dict:
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
                {"id": s["id"], "role": s["role"], "response": s["output"], "revision": s.get("revision", 0)}
                for s in plan
            ],
            "total_latency_ms": total_ms,
            "error": error,
        }

# ── Section: provider_factory ────────────────────────────────────────────────
# Config-driven provider factory.
# Collapses ~30 near-identical OpenAI-compatible wrappers into one.
#
# Non-OpenAI-shaped providers (gemini, cloudflare, huggingface, github_models,
# zerotwo, freegpt4_api) still get their own adapter but live in this file.

@dataclass(frozen=True)
class OpenAICompatProvider:
    """A provider whose /chat/completions matches OpenAI's schema."""
    name: str
    url: str
    key_env: str
    default_model: str
    extra_headers: dict[str, str] | None = None
    response_path: tuple[str, ...] = ("choices", 0, "message", "content")


# ─────────────────────────────────────────────────────────────────────
# All OpenAI-compatible providers declared as data, not code.
# ─────────────────────────────────────────────────────────────────────
OPENAI_COMPAT: dict[str, OpenAICompatProvider] = {
    p.name: p
    for p in [
        OpenAICompatProvider(
            name="groq",
            url="https://api.groq.com/openai/v1/chat/completions",
            key_env="GROQ_API_KEY",
            default_model="llama3-70b-8192",
        ),
        OpenAICompatProvider(
            name="openrouter",
            url="https://openrouter.ai/api/v1/chat/completions",
            key_env="OPENROUTER_API_KEY",
            default_model="openrouter/auto",
            extra_headers={
                "HTTP-Referer": "https://axelr.in",
                "X-Title": "Axelr AI",
            },
        ),
        OpenAICompatProvider(
            name="mistral",
            url="https://api.mistral.ai/v1/chat/completions",
            key_env="MISTRAL_API_KEY",
            default_model="open-mistral-7b",
        ),
        OpenAICompatProvider(
            name="modelscope",
            url=os.getenv("MODELSCOPE_URL", "https://api.modelscope.cn/v1/chat/completions"),
            key_env="MODELSCOPE_API_KEY",
            default_model="qwen-max",
        ),
        OpenAICompatProvider(
            name="nara_router",
            url=os.getenv("NARA_ROUTER_URL", "https://router.bynara.id/v1/chat/completions"),
            key_env="NARAROUTER_API_KEY",
            default_model="minimax-m3",
        ),
        OpenAICompatProvider(
            name="ovhcloud",
            url=os.getenv("OVHCLOUD_URL", "https://api.ai.cloud.ovh.net/v1/chat/completions"),
            key_env="OVHCLOUD_API_KEY",
            default_model="llama-3.3-70b-instruct",
        ),
        OpenAICompatProvider(
            name="siliconflow",
            url="https://api.siliconflow.cn/v1/chat/completions",
            key_env="SILICONFLOW_API_KEY",
            default_model="deepseek-ai/DeepSeek-V3",
        ),
        OpenAICompatProvider(
            name="zhipuai",
            url="https://open.bigmodel.cn/api/paas/v4/chat/completions",
            key_env="ZAI_API_KEY",
            default_model="glm-4.5-flash",
        ),
        OpenAICompatProvider(
            name="teamorouter",
            url=os.getenv("TEAMOROUTER_URL", "https://api.teamorouter.io/v1/chat/completions"),
            key_env="TEAMOROUTER_API_KEY",
            default_model="teamorouter-free",
        ),
        OpenAICompatProvider(
            name="bazaarlink",
            url=os.getenv("BAZAARLINK_URL", "https://api.bazaarlink.io/v1/chat/completions"),
            key_env="BAZAARLINK_API_KEY",
            default_model="auto:free",
        ),
        OpenAICompatProvider(
            name="requesty",
            url=os.getenv("REQUESTY_URL", "https://api.requesty.ai/v1/chat/completions"),
            key_env="REQUESTY_API_KEY",
            default_model="auto:free",
        ),
        OpenAICompatProvider(
            name="nrouter",
            url=os.getenv("NROUTER_URL", "https://api.nrouter.io/v1/chat/completions"),
            key_env="NROUTER_API_KEY",
            default_model="meta-llama/llama-3.1-8b-instruct",
        ),
        OpenAICompatProvider(
            name="manifest",
            url=os.getenv("MANIFEST_URL", "https://api.manifest.build/v1/chat/completions"),
            key_env="MANIFEST_API_KEY",
            default_model="auto:free",
        ),
        OpenAICompatProvider(
            name="glama",
            url=os.getenv("GLAMA_URL", "https://api.glama.ai/v1/chat/completions"),
            key_env="GLAMA_API_KEY",
            default_model="gpt-3.5-turbo",
        ),
        OpenAICompatProvider(
            name="anyapi",
            url=os.getenv("BASEURL", "https://api.anyapi.ai/v1/chat/completions"),
            key_env="ANYAPI_API_KEY",
            default_model="poolside/laguna-xs.2:free",
        ),
        OpenAICompatProvider(
            name="agnes_ai",
            url=os.getenv("AGNES_URL", "https://api.agnes.ai/v1/chat/completions"),
            key_env="AGNES_API_KEY",
            default_model="agnes-2.0-flash",
        ),
        OpenAICompatProvider(
            name="qoder",
            url=os.getenv("QODER_URL", "https://api.qoder.com/v1/chat/completions"),
            key_env="QODER_API_KEY",
            default_model="qwen3-coder",
        ),
        # ... add the rest the same way. Each is ~6 lines.
    ]
}


async def call_openai_compat(
    provider: OpenAICompatProvider,
    prompt: str,
    max_tokens: int,
    temp: float,
    model: str | None,
    client: httpx.AsyncClient,
) -> str:
    api_key = (os.getenv(provider.key_env) or "").strip()
    if not api_key:
        raise RuntimeError(f"{provider.key_env} missing")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if provider.extra_headers:
        headers.update(provider.extra_headers)

    payload = {
        "model": model or provider.default_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False,
    }

    resp = await client.post(provider.url, headers=headers, json=payload, timeout=20.0)
    if resp.status_code == 429:
        raise RuntimeError(f"{provider.name}: quota exceeded — {resp.text[:120]}")
    if resp.status_code == 402:
        raise RuntimeError(f"{provider.name}: payment required")
    resp.raise_for_status()
    data = resp.json()

    # Walk the configured response_path
    cur: Any = data
    for key in provider.response_path:
        if isinstance(key, int):
            cur = cur[key]
        else:
            cur = cur[key]
    if not isinstance(cur, str):
        raise RuntimeError(f"{provider.name}: unexpected response shape")
    return cur


def make_provider_func(
    name: str,
    http_client: httpx.AsyncClient,
) -> Callable[..., Awaitable[str]]:
    prov = OPENAI_COMPAT[name]

    async def _fn(prompt: str, max_tokens: int, temp: float, model: str | None = None) -> str:
        return await call_openai_compat(prov, prompt, max_tokens, temp, model, http_client)

    return _fn


__all__ = [
    # Intent routing
    "IntentRouter", "IntentResult", "get_router",
    # Orchestration
    "Orchestrator", "Subtask",
    # Prompts
    "PLANNER_PROMPT", "CRITIC_PROMPT", "REVISION_PROMPT", "SYNTH_PROMPT",
    "ROLE_SYSTEMS",
    # LLM providers  ← ADD THESE
    "OpenAICompatProvider", "OPENAI_COMPAT",
    "make_provider_func", "call_openai_compat",
]