# core/twin_engine.py
"""
AXELR — Unified Local Inference Engine — v4.2
==============================================
Linux-only. Two-tier local pool (FAST + ELITE). The roster lives in
`twin_roster.py`; this module owns process supervision only.

    engine = await get_twin_engine()
    await engine.start()
    text   = await engine.infer(prompt, cat="auto", task_type="extraction")
    await engine.shutdown()

Priority policy (v4.2):
    • FAST is sticky. It is loaded on start() and, in single-mode
      containers (RAM < TWIN_DUAL_MIN_RAM_MB), is NEVER evicted by an
      ELITE request — ELITE requests transparently fall back to the FAST
      slot instead.
    • FAST is launched with --parallel 2 by default, giving two
      concurrent inference slots on the FAST model ("two fast if
      possible, one always").
    • ELITE is only loaded in dual-mode containers where there is enough
      RAM to hold both simultaneously. It is idle-evicted back down to
      FAST after TWIN_IDLE_EVICT_ELITE_S of no use.
"""
from __future__ import annotations

import asyncio
import contextlib
import gc
import json
import logging
import os
import platform
import shutil
import signal
import socket
import time
import hashlib
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

# Roster + routing + constants (single source of truth)
from .twin_roster import (
    MAX_RESIDENT_MB, MODEL_ROSTER, ROSTER_BY_ID, ModelSpec,
    _ELITE_PREFERENCE, _FAST_PREFERENCE, pick_local_model,
)

logger = logging.getLogger("axelr.local_inference")

# ── Tunables ──────────────────────────────────────────────────────────────
_MAX_PROMPT_CHARS     = int(os.getenv("TWIN_MAX_PROMPT_CHARS", "2400"))
_FAST_PARALLEL        = int(os.getenv("TWIN_FAST_PARALLEL", "2"))
_ELITE_PARALLEL       = int(os.getenv("TWIN_ELITE_PARALLEL", "1"))
_FAST_THREADS         = int(os.getenv("TWIN_FAST_THREADS", "1"))
_ELITE_THREADS        = int(os.getenv("TWIN_ELITE_THREADS", "1"))
_FAST_TIMEOUT_S       = float(os.getenv("TWIN_FAST_TIMEOUT_S", "35"))
_ELITE_TIMEOUT_S      = float(os.getenv("TWIN_ELITE_TIMEOUT_S", "90"))
_LOAD_READY_S         = float(os.getenv("TWIN_LOAD_READY_S", "12"))
_IDLE_EVICT_ELITE_S   = float(os.getenv("TWIN_IDLE_EVICT_ELITE_S", "420"))
_REQUIRE_REAL         = os.getenv("HOT_SWAP_REQUIRE_REAL", "0") == "1"
_RSS_SOFT_CAP_MB      = int(os.getenv("TWIN_RSS_SOFT_CAP_MB", "380"))
_DUAL_MIN_MB          = int(os.getenv("TWIN_DUAL_MIN_RAM_MB", "1400"))
_MLOCK                = os.getenv("TWIN_MLOCK", "1") == "1"
_FLASH_ATTN           = os.getenv("TWIN_FLASH_ATTN", "0") == "1"

Cat = Literal["auto", "fast", "elite"]


# ═══════════════════════════════════════════════════════════════════════════
# Host & binary helpers
# ═══════════════════════════════════════════════════════════════════════════
def _models_dir() -> Path:
    root = (os.getenv("WORKSPACE_ROOT") or "").strip()
    return (Path(root) / "models") if root else (
        Path(__file__).resolve().parent.parent / "models"
    )


def _resolve_binary() -> str | None:
    for key in ("LLAMA_SERVER", "LLAMA_CLI"):
        p = (os.getenv(key) or "").strip()
        if p and Path(p).is_file() and os.access(p, os.X_OK):
            return str(Path(p).resolve())
    for name in ("llama-server", "llama-cli", "llama"):
        found = shutil.which(name)
        if found:
            return found
    base = Path(__file__).resolve().parent.parent / "bin"
    for rel in (base / "llama-server", base / "llama-cli"):
        if rel.is_file() and os.access(rel, os.X_OK):
            return str(rel)
    return None


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _rss_mb() -> int:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0


def _cgroup_memory_limit_bytes() -> int | None:
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.max"):
        try:
            with open(path) as f:
                v = f.read().strip()
            if v and v != "max":
                return int(v)
        except (OSError, ValueError):
            pass
    for path in ("/sys/fs/cgroup/memory/memory.limit_in_bytes",
                 "/sys/fs/cgroup/memory/memory.memsw.limit_in_bytes"):
        try:
            with open(path) as f:
                v = int(f.read().strip())
            if 0 < v < (1 << 62):
                return v
        except (OSError, ValueError):
            pass
    return None


def _effective_ram_mb() -> int:
    limit = _cgroup_memory_limit_bytes()
    if limit is not None:
        return limit // (1024 * 1024)
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 512


def _pick_existing(preference: tuple[str, ...], models_dir: Path) -> ModelSpec | None:
    for mid in preference:
        spec = ROSTER_BY_ID.get(mid)
        if spec and (models_dir / spec.file).is_file():
            return spec
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Resident process wrapper
# ═══════════════════════════════════════════════════════════════════════════
class _Resident:
    __slots__ = ("spec", "proc", "port", "loaded_at", "last_used")

    def __init__(self, spec: ModelSpec, proc: asyncio.subprocess.Process, port: int):
        self.spec = spec
        self.proc = proc
        self.port = port
        self.loaded_at = time.monotonic()
        self.last_used = time.monotonic()

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None


# ═══════════════════════════════════════════════════════════════════════════
# The engine
# ═══════════════════════════════════════════════════════════════════════════
class TwinEngine:
    """Unified local inference pool. See module docstring."""
    __slots__ = (
        "_binary", "_dual_mode", "_elite", "_elite_infers", "_elite_spec",
        "_fast", "_fast_infers", "_fast_spec", "_http", "_idle_task",
        "_inflight", "_lock", "_models_dir", "_shutdown", "_started_at",
        "_stats", "_swap_history", "_total_ram_mb",
        "_prompt_prefix_cache",
        "_max_inflight_fast",
        "_max_inflight_elite",
    )

    def __init__(self) -> None:
        if platform.system() != "Linux":
            raise RuntimeError(
                f"TwinEngine requires Linux (got {platform.system()}). "
                "Use a Linux container or WSL2."
            )
        self._binary: str | None = _resolve_binary()
        self._models_dir: Path = _models_dir()
        self._http: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._fast: _Resident | None = None
        self._elite: _Resident | None = None
        self._fast_spec = _pick_existing(_FAST_PREFERENCE, self._models_dir)
        self._elite_spec = _pick_existing(_ELITE_PREFERENCE, self._models_dir)
        self._total_ram_mb = _effective_ram_mb()
        self._dual_mode = (
            self._total_ram_mb >= _DUAL_MIN_MB
            and self._fast_spec is not None
            and self._elite_spec is not None
        )
        self._shutdown = False
        self._idle_task: asyncio.Task | None = None
        self._inflight = 0
        self._swap_history: deque[dict[str, Any]] = deque(maxlen=32)
        self._fast_infers = 0
        self._elite_infers = 0
        self._started_at = time.monotonic()
        self._prompt_prefix_cache: dict[str, str] = {}

        # FIX B6 — align with llama-server --parallel slot counts.
        # FAST model is launched with --parallel 2 (two slots), ELITE with
        # --parallel 1 (one slot). The application never allows more
        # concurrent infer() calls than the local engine can service.
        self._max_inflight_fast = int(os.getenv("TWIN_MAX_INFLIGHT_FAST", "2"))
        self._max_inflight_elite = int(os.getenv("TWIN_MAX_INFLIGHT_ELITE", "1"))

        self._stats: dict[str, int] = {
            "total_infers": 0, "total_stub_fallbacks": 0,
            "total_timeouts": 0, "total_errors": 0,
            "total_swaps": 0, "total_evictions": 0,
            "total_pool_rejections": 0, "total_cache_hits": 0,
            "total_elite_to_fast_fallbacks": 0,
        }

        logger.info(
            "twin_init ram_mb=%d dual=%s fast=%s elite=%s binary=%s "
            "max_inflight_fast=%d max_inflight_elite=%d fast_parallel=%d",
            self._total_ram_mb, self._dual_mode,
            self._fast_spec.id if self._fast_spec else None,
            self._elite_spec.id if self._elite_spec else None,
            self._binary, self._max_inflight_fast, self._max_inflight_elite,
            _FAST_PARALLEL,
        )

    # ── lifecycle ───────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._binary is None:
            if _REQUIRE_REAL:
                raise RuntimeError("TwinEngine: no llama-server binary found")
            logger.warning("twin_no_binary_stub_only")
            return
        if self._fast_spec is None:
            if _REQUIRE_REAL:
                raise RuntimeError("TwinEngine: no fast-cat model on disk")
            logger.warning("twin_no_fast_model_stub_only")
            return

        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(_ELITE_TIMEOUT_S + 10, connect=5.0),
            )

        try:
            p = await asyncio.create_subprocess_exec(
                self._binary, "--version",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(p.wait(), timeout=3.0)
            if p.returncode != 0:
                raise RuntimeError(f"llama --version exit {p.returncode}")
        except Exception as e:
            if _REQUIRE_REAL:
                raise
            logger.warning("twin_readiness_probe_failed error=%s", e)

        async with self._lock:
            await self._load_locked(self._fast_spec, slot="fast")

        self._idle_task = asyncio.create_task(
            self._idle_watchdog(), name="twin.idle",
        )

    async def shutdown(self) -> None:
        self._shutdown = True
        if self._idle_task is not None:
            self._idle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._idle_task
            self._idle_task = None
        async with self._lock:
            await self._kill_locked("elite")
            await self._kill_locked("fast")
        if self._http is not None:
            with contextlib.suppress(Exception):
                await self._http.aclose()
            self._http = None

    # ── public: infer ──────────────────────────────────────────────────
    async def infer(
        self,
        workspace: str,
        task_type: str,
        prompt: str,
        *,
        cat: Cat = "auto",
        tier: str = "free",
        timeout_s: float | None = None,
    ) -> str:
        prompt = (prompt or "").strip()
        if not prompt:
            return ""
        if self._binary is None or self._fast_spec is None:
            return self._stub(prompt, "no_binary")

        # Prompt prefix caching for multi-turn conversations
        _prefix_digest = hashlib.blake2b(prompt[:1000].encode("utf-8"), digest_size=8).hexdigest()
        cache_key = f"{workspace}:{_prefix_digest}"
        cached_prefix = self._prompt_prefix_cache.get(cache_key)
        if cached_prefix and prompt.startswith(cached_prefix):
            self._stats["total_cache_hits"] += 1
            prompt = prompt[len(cached_prefix):].strip()
        else:
            prefix_len = max(100, int(len(prompt) * 0.2))
            self._prompt_prefix_cache[cache_key] = prompt[:prefix_len]

        if not prompt:
            return ""

        if cat == "auto":
            cat = self._route_cat(prompt)

        # ── Single-mode: ELITE requests fall back to FAST ──────────────
        # FAST is sticky. We never evict FAST to load ELITE in a
        # container that can't hold both.
        if cat == "elite" and (self._elite_spec is None or not self._dual_mode):
            self._stats["total_elite_to_fast_fallbacks"] += 1
            logger.info("twin_elite_to_fast_fallback reason=%s",
                        "no_spec" if self._elite_spec is None else "single_mode")
            cat = "fast"

        # Respect tier-specific timeouts (wall-clock budget from the
        # resilience pipeline). Keep it tight — 5 s cap on the hot-swap
        # attempt regardless of tier so the overall request can still
        # finish inside 15 s (free) / 30 s (pro).
        if tier in ["pro", "business"]:
            timeout_s = min(timeout_s or _ELITE_TIMEOUT_S, 5.0)
        else:
            timeout_s = min(timeout_s or _FAST_TIMEOUT_S, 5.0)

        # Enforce inference pool limits (now aligned with llama-server)
        async with self._lock:
            if cat == "elite":
                if self._elite_infers >= self._max_inflight_elite:
                    self._stats["total_pool_rejections"] += 1
                    logger.warning("twin_pool_limit_rejected cat=elite current=%d max=%d",
                                  self._elite_infers, self._max_inflight_elite)
                    return self._stub(prompt, "pool_limit")
            else:
                if self._fast_infers >= self._max_inflight_fast:
                    self._stats["total_pool_rejections"] += 1
                    logger.warning("twin_pool_limit_rejected cat=fast current=%d max=%d",
                                  self._fast_infers, self._max_inflight_fast)
                    return self._stub(prompt, "pool_limit")

        self._inflight += 1
        try:
            async with self._lock:
                if cat == "elite":
                    await self._ensure_elite_locked()
                else:
                    await self._ensure_fast_locked()
                resident = self._elite if cat == "elite" else self._fast
                if resident is None or not resident.alive:
                    self._stats["total_stub_fallbacks"] += 1
                    return self._stub(prompt, "load_failed")

                text = await self._post_completion(resident, prompt, timeout_s)
                resident.last_used = time.monotonic()
                if cat == "elite":
                    self._elite_infers += 1
                else:
                    self._fast_infers += 1
                if text.startswith("[stub]"):
                    self._stats["total_stub_fallbacks"] += 1
                else:
                    self._stats["total_infers"] += 1
                return text
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("twin_infer_error cat=%s error=%s", cat, e)
            self._stats["total_errors"] += 1
            self._stats["total_stub_fallbacks"] += 1
            return self._stub(prompt, "error")
        finally:
            self._inflight -= 1

    # ── routing ─────────────────────────────────────────────────────────
    @staticmethod
    def _route_cat(prompt: str) -> Cat:
        low = prompt.lower()
        elite_kw = (
            "prove", "derive", "algorithm", "complexity",
            "step by step", "reason", "analyze in depth",
            "write a function", "refactor", "debug",
            "design a schema", "compare and contrast",
        )
        if any(k in low for k in elite_kw):
            return "elite"
        if len(prompt) > 900:
            return "elite"
        return "fast"

    # ── load / evict ────────────────────────────────────────────────────
    async def _ensure_fast_locked(self) -> None:
        if self._fast is not None and self._fast.alive:
            return
        # In single-mode, an idle ELITE (shouldn't happen, but defensive)
        # is evicted before FAST is reloaded.
        if not self._dual_mode and self._elite is not None:
            await self._kill_locked("elite")
        await self._load_locked(self._fast_spec, slot="fast")  # type: ignore[arg-type]

    async def _ensure_elite_locked(self) -> None:
        """
        FAST is sticky. Only load ELITE in dual-mode containers where
        both can be resident simultaneously. In single-mode this is a
        no-op — the caller already redirected ELITE requests to FAST.
        """
        if self._elite_spec is None:
            await self._ensure_fast_locked()
            return
        if self._elite is not None and self._elite.alive:
            return
        if not self._dual_mode:
            # Do NOT evict FAST. Just make sure it's loaded.
            await self._ensure_fast_locked()
            return
        await self._load_locked(self._elite_spec, slot="elite")

    async def _load_locked(self, spec: ModelSpec, *, slot: str) -> None:
        model_path = self._models_dir / spec.file
        if not model_path.is_file():
            logger.warning("twin_model_missing slot=%s file=%s", slot, spec.file)
            return

        # OOM pre-check
        rss = _rss_mb()
        cgroup_mb = (_cgroup_memory_limit_bytes() or 0) // (1024 * 1024)
        if cgroup_mb and rss > int(cgroup_mb * 0.7):
            logger.error("twin_rss_over_cgroup rss_mb=%d cgroup_mb=%d slot=%s",
                         rss, cgroup_mb, slot)
            return

        port = _free_port()
        threads = _ELITE_THREADS if slot == "elite" else _FAST_THREADS
        parallel = _ELITE_PARALLEL if slot == "elite" else _FAST_PARALLEL

        args = [
            self._binary or "llama-server",
            "--model",         str(model_path),
            "--host",          "127.0.0.1",
            "--port",          str(port),
            "--ctx-size",      str(spec.ctx),
            "--n-predict",     str(spec.max_out),
            "--cache-type-k",  "q4_0",
            "--cache-type-v",  "q4_0",
            "--threads",       str(threads),
            "--threads-batch", str(threads),
            "--parallel",      str(parallel),
            "--no-mmap",
            *(["--mlock"] if _MLOCK else []),
            *(["--flash-attn"] if _FLASH_ATTN else []),
            "--min-p",         "0.05",   # filter low-confidence noise
            "--log-disable",
        ]
        env = {
            **os.environ,
            "OMP_NUM_THREADS": str(threads),
            "GGML_N_THREADS":  str(threads),
        }
        t0 = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
        except Exception as e:
            logger.error("twin_spawn_failed slot=%s error=%s", slot, e)
            return

        resident = _Resident(spec, proc, port)
        setattr(self, f"_{slot}", resident)
        self._stats["total_swaps"] += 1

        if not await self._wait_health(port, _LOAD_READY_S):
            logger.error("twin_load_timeout slot=%s model=%s", slot, spec.id)
            await self._kill_locked(slot)
            return

        rss_after = _rss_mb()
        if rss_after > _RSS_SOFT_CAP_MB:
            logger.error("twin_rss_over_cap rss_mb=%d cap_mb=%d slot=%s",
                         rss_after, _RSS_SOFT_CAP_MB, slot)
            await self._kill_locked(slot)
            return

        self._swap_history.append({
            "slot": slot, "to": spec.id,
            "duration_ms": round((time.monotonic() - t0) * 1000, 1),
            "weights_mb": spec.weights_mb,
            "rss_mb": rss_after,
            "dual_mode": self._dual_mode,
            "t": time.time(),
        })
        logger.info("twin_loaded slot=%s model=%s duration_ms=%.1f rss_mb=%d",
                    slot, spec.id, (time.monotonic() - t0) * 1000, rss_after)

    async def _kill_locked(self, slot: str) -> None:
        resident: _Resident | None = getattr(self, f"_{slot}", None)
        if resident is None:
            return
        setattr(self, f"_{slot}", None)
        proc = resident.proc
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
            except asyncio.CancelledError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                raise
        gc.collect()
        self._stats["total_evictions"] += 1

    async def _wait_health(self, port: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        url = f"http://127.0.0.1:{port}/health"
        while time.monotonic() < deadline:
            try:
                if self._http is None:
                    return False
                r = await self._http.get(url, timeout=1.0)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.2)
        return False

    # ── completion ──────────────────────────────────────────────────────
    async def _post_completion(
        self, resident: _Resident, prompt: str, timeout_s: float,
    ) -> str:
        if self._http is None:
            return self._stub(prompt, "no_http")
        try:
            r = await self._http.post(
                f"http://127.0.0.1:{resident.port}/completion",
                json={
                    "prompt": prompt[:_MAX_PROMPT_CHARS],
                    "n_predict": resident.spec.max_out,
                    "temperature": 0.0,
                    "stream": False,
                },
                timeout=timeout_s,
            )
            if r.status_code != 200:
                return self._stub(prompt, f"http_{r.status_code}")
            body = r.json()
            text = (body.get("content") or "").strip()
            return self._postprocess(text, resident.spec)
        except (httpx.TimeoutException, asyncio.TimeoutError):
            self._stats["total_timeouts"] += 1
            # Only kill the resident that timed out.
            slot = "fast"
            for s in ("fast", "elite"):
                res = getattr(self, f"_{s}", None)
                if res is resident:
                    slot = s
                    break
            await self._kill_locked(slot)
            return self._stub(prompt, "timeout")
        except Exception as e:
            logger.warning("twin_completion_error error=%s", e)
            self._stats["total_errors"] += 1
            return self._stub(prompt, "error")

    @staticmethod
    def _postprocess(text: str, spec: ModelSpec) -> str:
        if not text:
            return ""
        for marker in ("[end of text]", "<|endoftext|>", "<|im_end|>"):
            text = text.replace(marker, "")
        tokens = text.split()
        if len(tokens) > spec.max_out:
            text = " ".join(tokens[: spec.max_out])
        return text.strip()

    @staticmethod
    def _stub(prompt: str, reason: str) -> str:
        # Always return a valid response that triggers the multi-agent routing.
        return ("⚠️ High traffic detected. Routing you to our multi-agent system. "
                "Too many requests, please wait a moment while we connect you to an available agent.")

    # ── idle watchdog ───────────────────────────────────────────────────
    async def _idle_watchdog(self) -> None:
        if _IDLE_EVICT_ELITE_S <= 0:
            return
        interval = max(5.0, min(60.0, _IDLE_EVICT_ELITE_S / 4.0))
        while not self._shutdown:
            try:
                await asyncio.sleep(interval)
                if self._shutdown or self._inflight > 0 or self._dual_mode:
                    continue
                async with self._lock:
                    if (self._elite is not None and
                            time.monotonic() - self._elite.last_used >= _IDLE_EVICT_ELITE_S):
                        logger.info("twin_idle_evict_elite")
                        await self._kill_locked("elite")
                        await self._ensure_fast_locked()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("twin_idle_watchdog_error error=%s", e)

    # ── introspection ───────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        return {
            "mode": "dual" if self._dual_mode else "single",
            "ram_total_mb": self._total_ram_mb,
            "rss_mb": _rss_mb(),
            "binary": self._binary,
            "models_dir": str(self._models_dir),
            "fast": {
                "spec": self._fast_spec.id if self._fast_spec else None,
                "loaded": self._fast is not None and self._fast.alive,
                "infers": self._fast_infers,
                "max_inflight": self._max_inflight_fast,
                "parallel": _FAST_PARALLEL,
            },
            "elite": {
                "spec": self._elite_spec.id if self._elite_spec else None,
                "loaded": self._elite is not None and self._elite.alive,
                "infers": self._elite_infers,
                "max_inflight": self._max_inflight_elite,
                "parallel": _ELITE_PARALLEL,
            },
            "roster_size": len(MODEL_ROSTER),
            "budget_mb": MAX_RESIDENT_MB,
            "inflight": self._inflight,
            "swap_history": list(self._swap_history),
            "stats": dict(self._stats),
            "uptime_s": round(time.monotonic() - self._started_at, 1),
        }

    def __repr__(self) -> str:
        return (
            f"<TwinEngine mode={'dual' if self._dual_mode else 'single'} "
            f"fast={'up' if self._fast and self._fast.alive else 'down'} "
            f"elite={'up' if self._elite and self._elite.alive else 'down'} "
            f"ram={self._total_ram_mb}MB rss={_rss_mb()}MB>"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Singleton + module-level helpers
# ═══════════════════════════════════════════════════════════════════════════
_ENGINE: TwinEngine | None = None
_ENGINE_LOCK = asyncio.Lock()


async def get_twin_engine() -> TwinEngine | None:
    """Async factory. Returns None if the host can't support it."""
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    async with _ENGINE_LOCK:
        if _ENGINE is None:
            try:
                _ENGINE = TwinEngine()
            except RuntimeError as e:
                logger.warning("twin_unavailable error=%s", e)
                return None
        return _ENGINE


def twin_engine_snapshot() -> dict[str, Any]:
    return _ENGINE.snapshot() if _ENGINE else {"mode": "uninitialised"}


async def execute_local_inference(
    workspace: str, task_type: str, prompt: str, tier: str = "free",
) -> str:
    """Module-level convenience. Never raises. Returns '' if engine unavailable."""
    engine = await get_twin_engine()
    if engine is None:
        return ""
    try:
        return await engine.infer(workspace, task_type, prompt, tier=tier)
    except Exception as e:
        logger.warning("local_inference_error error=%s", e)
        return ""


# ═══════════════════════════════════════════════════════════════════════════
# Backward-compat aliases (keep api/routes_core.py unchanged)
# ═══════════════════════════════════════════════════════════════════════════
get_twin_engine_async      = get_twin_engine
get_hot_swap_engine_async  = get_twin_engine
execute_axelr_hot_swap_engine = execute_local_inference


def get_hot_swap_engine() -> TwinEngine | None:
    """Sync accessor (hot_swap compat). Returns the engine if constructed."""
    return _ENGINE


__all__ = [
    "MAX_RESIDENT_MB", "MODEL_ROSTER", "ROSTER_BY_ID", "ModelSpec",
    "TwinEngine", "get_twin_engine", "twin_engine_snapshot",
    "execute_local_inference", "pick_local_model",
    "get_hot_swap_engine", "get_hot_swap_engine_async",
    "get_twin_engine_async", "execute_axelr_hot_swap_engine",
]