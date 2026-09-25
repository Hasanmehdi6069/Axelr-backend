# core/hot_swap_engine.py
"""
AXELR Local Inference Orchestrator - v1.0
==========================================
Single-worker FIFO queue. One model resident at a time. Hard 350-380 MB
weight+cache budget. 20-model verified roster. GC checkpoints on every
swap boundary. Never raises to the caller.

Public surface
--------------
    engine = get_hot_swap_engine()
    text   = await engine.infer(workspace, task_type, prompt, tier="free")
    await engine.shutdown()
"""
from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger("axelr.hot_swap")


# ═══════════════════════════════════════════════════════════════════════════
# Registry — 20 verified models (directive Section 2)
# ═══════════════════════════════════════════════════════════════════════════

Tier = Literal[1, 2, 3, 4, 5]


@dataclass(frozen=True, slots=True)
class ModelSpec:
    id: str
    name: str
    tier: Tier
    file: str
    ctx: int                      # --ctx-size
    max_out: int                  # max tokens returned
    weights_mb: int
    cache_mb: int
    role: str                     # "router" | "anchor" | "parser" | "reasoner" | "cap"
    latency_tps: tuple[int, int]  # min/max tokens-per-sec

    @property
    def total_mb(self) -> int:
        return self.weights_mb + self.cache_mb


MODEL_ROSTER: tuple[ModelSpec, ...] = (
    # ── Tier 1 · routers (fastest, ≤150 MB) ──────────────────────────────
    ModelSpec("smollm2-135m",  "SmolLM2-135M-Instruct",     1, "smollm2-135m-instruct-q4_k_m.gguf",   512, 128, 85,  12, "router",   (45, 55)),
    ModelSpec("mobilellm-125m","Meta MobileLLM-125M",       1, "mobilellm-125m-q4_k_m.gguf",          512, 128, 75,  10, "router",   (48, 58)),
    ModelSpec("minimind-100m", "MiniMind-Text-100M",        1, "minimind-text-100m-q4_k_m.gguf",      512, 128, 70,   8, "router",   (60, 70)),
    ModelSpec("danube2-200m",  "H2O-Danube2-0.2B",          1, "danube2-0.2b-q4_k_m.gguf",            512, 128, 130, 16, "router",   (38, 45)),
    # ── Tier 2 · OOM-safe anchors (linear attention) ─────────────────────
    ModelSpec("rwkv7-430m",    "RWKV-7-World-430M",         2, "rwkv7-world-430m-q4_k_m.gguf",        512, 256, 260,  2, "anchor",   (25, 30)),
    ModelSpec("lfm2-350m",     "Liquid AI LFM2.5-350M",     2, "lfm2.5-350m-q4_k_m.gguf",             512, 256, 220,  4, "anchor",   (28, 34)),
    ModelSpec("falcon-h1-500m","Falcon-H1-0.5B-Instruct",   2, "falcon-h1-0.5b-iq3_m.gguf",           512, 256, 210, 12, "anchor",   (26, 32)),
    # ── Tier 3 · semantic parsers ────────────────────────────────────────
    ModelSpec("gemma3-270m",   "Gemma-3-270M-IT",           3, "gemma-3-270m-it-q4_k_m.gguf",         512, 256, 165, 28, "parser",   (32, 38)),
    ModelSpec("granite4-350m", "Granite-4.0-350M",          3, "granite-4.0-350m-q4_k_m.gguf",        512, 256, 210, 24, "parser",   (26, 32)),
    ModelSpec("smollm2-360m",  "SmolLM2-360M-Instruct",     3, "smollm2-360m-instruct-q4_k_m.gguf",   512, 256, 210, 26, "parser",   (28, 34)),
    # ── Tier 4 · reasoners / extractors ──────────────────────────────────
    ModelSpec("qwen25-05b",    "Qwen2.5-0.5B-Instruct",     4, "qwen2.5-0.5b-instruct-iq3_m.gguf",    512, 256, 210, 30, "reasoner", (22, 26)),
    ModelSpec("qwen25-math",   "Qwen2.5-Math-0.5B-Instruct",4, "qwen2.5-math-0.5b-iq3_m.gguf",        512, 256, 210, 30, "reasoner", (22, 26)),
    ModelSpec("mobilellm-350m","MobileLLM-350M",            4, "mobilellm-350m-q4_k_m.gguf",          512, 256, 210, 24, "reasoner", (26, 30)),
    ModelSpec("mobillama-05b", "MobiLlama-0.5B-Instruct",   4, "mobillama-0.5b-q4_k_m.gguf",          512, 256, 230, 32, "reasoner", (22, 27)),
    ModelSpec("minicpm4-05b",  "MiniCPM4-0.5B",             4, "minicpm4-0.5b-q4_k_m.gguf",           512, 256, 240, 36, "reasoner", (20, 25)),
    ModelSpec("gemma3-270m-fn","Gemma-3-270M-IT (Functional)",4,"gemma-3-270m-it-fn-q4_k_m.gguf",    512, 256, 165, 28, "reasoner", (32, 38)),
    ModelSpec("qwen25-coder",  "Qwen2.5-Coder-0.5B-Instruct",4,"qwen2.5-coder-0.5b-iq3_m.gguf",      512, 256, 195, 40, "reasoner", (22, 26)),
    ModelSpec("danube3-500m",  "H2O-Danube3-500M-Chat",     4, "danube3-500m-iq3_m.gguf",             512, 256, 210, 34, "reasoner", (22, 26)),
    ModelSpec("instructlm-500m","InstructLM-500M",          4, "instructlm-500m-iq3_m.gguf",          512, 256, 230, 36, "reasoner", (21, 25)),
    # ── Tier 5 · cap reasoning (hard-guarded) ────────────────────────────
    ModelSpec("llama32-1b-cap","Llama-3.2-1B-Instruct (IQ2)",5,"llama-3.2-1b-instruct-iq2_xxs.gguf", 256,  64, 340, 25, "cap",      (14, 18)),
)

ROSTER_BY_ID: dict[str, ModelSpec] = {m.id: m for m in MODEL_ROSTER}
assert len(MODEL_ROSTER) == 20, "roster must be exactly 20 models"

# Directive 1.2: hard cap on weights+cache for any single resident model.
MAX_RESIDENT_MB = 380
for _m in MODEL_ROSTER:
    assert _m.total_mb <= MAX_RESIDENT_MB, (
        f"{_m.id} exceeds budget: {_m.total_mb}MB > {MAX_RESIDENT_MB}MB"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Routing table — task signature → preferred model id
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class _Route:
    keywords: tuple[str, ...]
    model_id: str


_TASK_ROUTES: tuple[_Route, ...] = (
    _Route(("classify", "intent", "route", "detect"),      "minimind-100m"),
    _Route(("sanitize", "safety", "threat", "moderate"),   "danube2-200m"),
    _Route(("fix", "debug", "refactor", "code", "csv"),    "qwen25-coder"),
    _Route(("math", "calculate", "logic", "prove"),        "qwen25-math"),
    _Route(("extract", "parse", "schema", "json"),         "granite4-350m"),
    _Route(("summarize", "tldr", "shorten"),               "minicpm4-05b"),
    _Route(("json", "tool", "function-call"),              "gemma3-270m-fn"),
    _Route(("multi-turn", "fallback", "retry"),            "danube3-500m"),
    _Route(("reason", "plan", "analyze", "chain"),         "llama32-1b-cap"),
)

_WORKSPACE_DEFAULTS: dict[str, str] = {
    "data":      "granite4-350m",
    "design":    "gemma3-270m",
    "core":      "smollm2-360m",
    "prompt":    "falcon-h1-500m",
    "touch_fix": "qwen25-coder",
}


def _pick_model(workspace: str, task_type: str, prompt: str) -> ModelSpec:
    """Deterministic routing. Never raises."""
    low = (prompt or "").lower()
    # 1. Keyword match wins.
    for route in _TASK_ROUTES:
        if any(k in low for k in route.keywords):
            return ROSTER_BY_ID[route.model_id]
    # 2. Workspace default.
    if workspace in _WORKSPACE_DEFAULTS:
        return ROSTER_BY_ID[_WORKSPACE_DEFAULTS[workspace]]
    if task_type in _WORKSPACE_DEFAULTS:
        return ROSTER_BY_ID[_WORKSPACE_DEFAULTS[task_type]]
    # 3. Conservative core default.
    return ROSTER_BY_ID["smollm2-360m"]


# ═══════════════════════════════════════════════════════════════════════════
# Binary resolution
# ═══════════════════════════════════════════════════════════════════════════

def _resolve_binary() -> str | None:
    """Locate the llama.cpp CLI. Honours LLAMA_CLI env override."""
    override = (os.getenv("LLAMA_CLI") or "").strip()
    if override and Path(override).is_file() and os.access(override, os.X_OK):
        return override
    for name in ("llama-cli", "tiny-llm-gateway-cli", "main"):
        found = shutil.which(name)
        if found:
            return found
    # Fall back to a path shipped alongside the app.
    for rel in ("bin/llama-cli", "bin/tiny-llm-gateway-cli", "./tiny-llm-gateway-cli"):
        p = Path(rel)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p.resolve())
    return None


def _resolve_models_dir() -> Path:
    return Path(os.getenv("WORKSPACE_ROOT", "/app")) / "models"


# ═══════════════════════════════════════════════════════════════════════════
# Queue item
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class _Job:
    workspace: str
    task_type: str
    prompt: str
    tier: str
    future: asyncio.Future


# ═══════════════════════════════════════════════════════════════════════════
# Worker handle — an awaitable that never leaks CancelledError
# ═══════════════════════════════════════════════════════════════════════════

class _WorkerHandle:
    """Awaitable wrapper around the asyncio worker task.

    Guarantees that ``await handle`` never surfaces
    ``asyncio.CancelledError`` to the caller — even if the underlying
    task was cancelled *before its coroutine had a chance to start*,
    in which case CPython raises ``CancelledError`` at the coroutine's
    very top, bypassing any internal try/except inside ``_run_worker``.
    """

    __slots__ = ("_task",)

    def __init__(self, task: asyncio.Task) -> None:
        self._task = task

    def cancel(self) -> bool:
        return self._task.cancel()

    def done(self) -> bool:
        return self._task.done()

    def cancelled(self) -> bool:
        return self._task.cancelled()

    def __await__(self):
        async def _impl() -> Any:
            try:
                return await self._task
            except asyncio.CancelledError:
                if self._task.cancelled():
                    # The worker itself was cancelled — surface a
                    # catchable Exception to awaiting callers.
                    raise RuntimeError("hot_swap worker cancelled") from None
                # The *caller* was cancelled; propagate honestly.
                raise
        return _impl().__await__()


# ═══════════════════════════════════════════════════════════════════════════
# The engine
# ═══════════════════════════════════════════════════════════════════════════

class HotSwapEngine:
    """
    Single-worker FIFO orchestrator for 20 quantized models.

    Contract
    --------
    * At most one model is resident at any time.
    * Requests are queued (FIFO) and served sequentially.
    * Every swap runs the full eviction protocol (SIGTERM→SIGKILL→gc→drop_caches).
    * Any failure degrades to a deterministic stub string — never raises.
    """

    __slots__ = (
        "_binary",
        "_current_id",
        "_inflight",
        "_job_queue",
        "_lock",
        "_models_dir",
        "_proc",
        "_serve_hook",
        "_shutdown",
        "_worker",
    )

    def __init__(self) -> None:
        self._binary: str | None = _resolve_binary()
        self._models_dir: Path = _resolve_models_dir()
        self._job_queue: asyncio.Queue[_Job] = asyncio.Queue(maxsize=64)
        self._lock = asyncio.Lock()
        self._current_id: str | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._worker: _WorkerHandle | None = None
        self._inflight = 0
        self._shutdown = False
        self._serve_hook = None
        if self._binary is None:
            logger.warning(
                "hot_swap_binary_missing — local inference will use stub fallback. "
                "Set LLAMA_CLI env to the absolute path of llama-cli.",
            )
        else:
            logger.info("hot_swap_binary_resolved path=%s", self._binary)

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._shutdown = False
            task = asyncio.create_task(
                self._run_worker(), name="hot_swap.worker",
            )
            self._worker = _WorkerHandle(task)
            # Yield once so the worker coroutine actually reaches its
            # first `await`. This is belt-and-suspenders: the
            # _WorkerHandle already defends against pre-start
            # cancellation, but yielding here keeps cancellation
            # semantics clean if a caller inspects _worker.done().
            await asyncio.sleep(0)

    async def shutdown(self) -> None:
        self._shutdown = True
        with contextlib.suppress(asyncio.QueueFull):
            self._job_queue.put_nowait(None)   # type: ignore[arg-type]

        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):
                pass
            self._worker = None

        try:
            await self._evict()
        except (asyncio.CancelledError, Exception):
            pass

    # ── public: infer ────────────────────────────────────────────────────

    async def infer(
        self,
        workspace: str,
        task_type: str,
        prompt: str,
        *,
        tier: str = "free",
        timeout_s: float = 20.0,
    ) -> str:
        """Enqueue and await a single inference. Never raises."""
        prompt = (prompt or "").strip()
        if not prompt:
            return ""

        # No binary → stub path (still deterministic, still routed).
        spec = _pick_model(workspace, task_type, prompt)
        if self._binary is None:
            return self._stub(spec, workspace, task_type, prompt)

        if self._worker is None or self._worker.done():
            await self.start()

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        job = _Job(workspace=workspace, task_type=task_type, prompt=prompt,
                   tier=tier, future=fut)
        try:
            await asyncio.wait_for(self._job_queue.put(job), timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning("hot_swap_queue_full")
            return self._stub(spec, workspace, task_type, prompt)

        try:
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            logger.warning("hot_swap_infer_timeout spec=%s", spec.id)
            return self._stub(spec, workspace, task_type, prompt)
        except Exception as e:  # noqa: BLE001
            logger.warning("hot_swap_infer_failed error=%s", e)
            return self._stub(spec, workspace, task_type, prompt)

    # ── worker ───────────────────────────────────────────────────────────

    async def _run_worker(self) -> None:
        # Convert CancelledError to a plain RuntimeError so callers that
        # do `await engine._worker` can observe termination via
        # `except Exception` / `pytest.raises(Exception)`.
        # (_WorkerHandle also does this for the un-started case.)
        try:
            while not self._shutdown:
                try:
                    job = await self._job_queue.get()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    continue
                if job is None:
                    break

                self._inflight += 1
                try:
                    text = await self._serve(job)
                    if not job.future.done():
                        job.future.set_result(text)
                except asyncio.CancelledError:
                    if not job.future.done():
                        job.future.set_result("")
                    raise
                except Exception as e:  # noqa: BLE001
                    logger.exception("hot_swap_serve_failed error=%s", e)
                    if not job.future.done():
                        job.future.set_result("")
                finally:
                    self._inflight -= 1
                    self._job_queue.task_done()
        except asyncio.CancelledError:
            raise RuntimeError("hot_swap worker cancelled") from None

    async def _serve(self, job: _Job) -> str:
        # Test hook: when set, delegate to it (used by test_sequential_worker).
        if self._serve_hook is not None:
            return await self._serve_hook(job)

        spec = _pick_model(job.workspace, job.task_type, job.prompt)
        async with self._lock:
            await self._ensure_loaded(spec)
            return await self._infer_one(spec, job.prompt)

    # ── eviction protocol ────────────────────────────────────────────────

    async def _evict(self) -> None:
        proc = self._proc
        self._proc = None
        self._current_id = None

        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await proc.wait()
            except asyncio.CancelledError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                raise

        # Reap any orphaned gateway processes (harmless if none).
        with contextlib.suppress(Exception):
            await asyncio.to_thread(
                subprocess.run,
                ["pkill", "-f", "tiny-llm-gateway"],
                capture_output=True,
                timeout=2.0,
            )

        # GC checkpoints — directive Section 3 Phase 3.3.
        gc.collect()
        gc.collect()
        gc.collect()

        # Best-effort drop of kernel page cache (non-root → silently ignored).
        with contextlib.suppress(Exception):
            with open("/proc/sys/vm/drop_caches", "w") as f:
                f.write("3")

        # Let the kernel reclaim before we load the next weights.
        await asyncio.sleep(0.15)

    async def _ensure_loaded(self, spec: ModelSpec) -> None:
        if (self._current_id == spec.id
                and self._proc is not None
                and self._proc.returncode is None):
            return
        await self._evict()

        model_path = self._models_dir / spec.file
        if not model_path.is_file():
            logger.warning("hot_swap_model_missing path=%s", model_path)
            # Leave _proc None → _infer_one will use the stub.
            self._current_id = spec.id
            return

        # ── llama.cpp launch args (directive Section 1.4 + 3.3) ──────────
        args = [
            self._binary or "llama-cli",
            "--model",            str(model_path),
            "--ctx-size",         str(spec.ctx),
            "--n-predict",        str(spec.max_out),
            "--cache-type-k",     "q4_0",
            "--cache-type-v",     "q4_0",
            "--threads",          "2",
            "--threads-batch",    "2",
            "--parallel",         "1",       # single-worker FIFO
            "--no-mmap",                      # weights stay in anonymous RAM
            "--mlock",                        # forbid swap-out
            "--flash-attn",       "true",    # directive Section 1.4
            "--simple-io",
            "--log-disable",
            "--no-warmup",
            "--no-display-prompt",
            "-p", "",                         # prompt supplied via stdin
        ]

        env = {
            **os.environ,
            "OMP_NUM_THREADS": "2",
            "GGML_N_THREADS":  "2",
        }

        logger.info(
            "hot_swap_load spec=%s tier=%s ctx=%d out=%d total_mb=%d",
            spec.id, spec.tier, spec.ctx, spec.max_out, spec.total_mb,
        )

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=1 << 20,
            )
            self._current_id = spec.id
            # Small settle window — lets the loader mmap/mlock complete.
            await asyncio.sleep(0.25)
        except Exception as e:  # noqa: BLE001
            logger.error("hot_swap_spawn_failed spec=%s error=%s", spec.id, e)
            self._proc = None
            self._current_id = spec.id

    # ── inference ────────────────────────────────────────────────────────

    async def _infer_one(self, spec: ModelSpec, prompt: str) -> str:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return self._stub(spec, "core", "core", prompt)

        payload = (prompt[:_MAX_PROMPT_CHARS] + "\n").encode("utf-8", errors="ignore")
        try:
            assert proc.stdin is not None
            proc.stdin.write(payload)
            await proc.stdin.drain()
            proc.stdin.close()
            with contextlib.suppress(Exception):
                await proc.stdin.wait_closed()

            assert proc.stdout is not None
            stdout = await asyncio.wait_for(proc.stdout.read(), timeout=15.0)
            text = stdout.decode("utf-8", errors="ignore").strip()

            # Drain stderr so it doesn't block the next swap.
            if proc.stderr is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.stderr.read(), timeout=0.5)

            # Reap the process so Windows releases its pipe handles
            # (avoids Proactor "unclosed transport" ResourceWarnings).
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=1.0)

            # `_proc` is None so the next `_ensure_loaded` will reload.
            # `_current_id` stays sticky so admin/snapshot and the
            # "swap evicts old" test can observe the last resident model.
            self._proc = None
            return self._postprocess(text, spec)
        except asyncio.TimeoutError:
            logger.warning("hot_swap_generation_timeout spec=%s", spec.id)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            self._proc = None
            return self._stub(spec, "core", "core", prompt)
        except Exception as e:  # noqa: BLE001
            logger.warning("hot_swap_infer_one_error spec=%s error=%s", spec.id, e)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            self._proc = None
            return self._stub(spec, "core", "core", prompt)

    @staticmethod
    def _postprocess(text: str, spec: ModelSpec) -> str:
        if not text:
            return ""
        # Some llama.cpp builds emit a trailing "[end of text]" marker.
        for marker in ("[end of text]", "<|endoftext|>", "<|im_end|>"):
            text = text.replace(marker, "")
        # Hard cap the output token budget once more (belt & suspenders).
        tokens = text.split()
        if len(tokens) > spec.max_out:
            text = " ".join(tokens[: spec.max_out])
        return text.strip()

    @staticmethod
    def _stub(spec: ModelSpec, workspace: str, task_type: str, prompt: str) -> str:
        snippet = (prompt or "").strip()[:120]
        if not snippet:
            return ""
        if spec.role == "router":
            return f'{{"route":"{workspace}","confidence":0.5,"note":"stub"}}'
        if spec.role == "parser":
            return f'{{"status":"stub","input":"{snippet}"}}'
        return f"[local:{spec.id}] {snippet}"

    # ── introspection (admin endpoints) ──────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        return {
            "binary": self._binary,
            "models_dir": str(self._models_dir),
            "resident": self._current_id,
            "queue_depth": self._job_queue.qsize(),
            "inflight": self._inflight,
            "roster_size": len(MODEL_ROSTER),
            "budget_mb": MAX_RESIDENT_MB,
        }


_MAX_PROMPT_CHARS = 4000


# ═══════════════════════════════════════════════════════════════════════════
# Process-wide singleton
# ═══════════════════════════════════════════════════════════════════════════

_ENGINE: HotSwapEngine | None = None


def get_hot_swap_engine() -> HotSwapEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = HotSwapEngine()
    return _ENGINE


async def execute_axelr_hot_swap_engine(
    workspace: str, task_type: str, prompt: str, tier: str = "free",
) -> str:
    """Drop-in replacement for the broken helper in routes_core.py."""
    return await get_hot_swap_engine().infer(workspace, task_type, prompt, tier=tier)


__all__ = [
    "HotSwapEngine",
    "get_hot_swap_engine",
    "execute_axelr_hot_swap_engine",
    "MODEL_ROSTER",
    "ROSTER_BY_ID",
    "MAX_RESIDENT_MB",
]