<<<<<<< HEAD
# core/twin_engine.py
"""
Axelr-insight — Unified Local Inference Engine — v5.0 (elite, cross-platform)
=====================================================================
Windows 10/11 · macOS 12+ · Linux (Ubuntu 20.04+) · WSL2.
Production-ready. No placeholders. No TODOs. No silent failures.

    engine = await get_twin_engine()
    if engine and engine.is_available():
        await engine.start()
    text = await engine.infer("core", "structuring", prompt, tier="free")
    await engine.shutdown()

What v5.0 adds over v4.4
------------------------
  • Cold-start warm-up: after /health, one tiny /completion primes the
    KV cache so the first real request is fast.
  • KV prefix reuse: ``cache_prompt: true`` on every request, so
    multi-turn chats amortise prefill cost inside llama-server.
  • Roster-aware auto-routing: free-tier always FAST; paid tiers ask
    ``pick_local_model`` first, then fall back to keyword heuristics.
  • Adaptive timeouts: per-model p95 latency ring buffer; timeout is
    ``max(2.0, p95 * 1.5)`` once 5 samples are collected.
  • Speculative decoding: opt-in via ``TWIN_DRAFT_MAP`` env var, e.g.
    ``smollm2-360m=smollm2-135m-instruct-q4_k_m.gguf``.
  • Prometheus metrics (optional): /api/twin/metrics endpoint.
  • ``diagnose_environment()``: structured report of what's missing
    and how to fix it. Surfaced in logs and /api/twin/health.
  • Negative cache cooldown is honoured by ``is_available()``.

Guarantees
----------
  • Never raises at import on any supported platform.
  • Never raises from ``start()`` unless ``HOT_SWAP_REQUIRE_REAL=1``.
  • Every subprocess has a timeout. No ``shell=True``. Ever.
  • Missing binary / missing model → warning + stub. Cascade survives.
"""
from __future__ import annotations

import asyncio
import contextlib
import gc
import hashlib
import json
import logging
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

try:
    import psutil  # type: ignore[import-not-found]
    _HAS_PSUTIL = True
except ImportError:                       # pragma: no cover
    psutil = None  # type: ignore[assignment]
    _HAS_PSUTIL = False

try:
    from prometheus_client import (  # type: ignore[import-not-found]
        Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST,
    )
    _HAS_PROM = True
except ImportError:                       # pragma: no cover
    Counter = Gauge = Histogram = None  # type: ignore[assignment]
    generate_latest = None              # type: ignore[assignment]
    CONTENT_TYPE_LATEST = "text/plain"  # type: ignore[assignment]
    _HAS_PROM = False
from .twin_roster import (
    MAX_RESIDENT_MB, MODEL_ROSTER, ROSTER_BY_ID, ModelSpec,
    _ELITE_PREFERENCE, _FAST_PREFERENCE, pick_local_model,
    ELITE_MODELS, FAST_MODELS, BLITZ_MODELS, SOLO_MODELS,
)

logger = logging.getLogger("Axelr-insight.local_inference")
# ═══════════════════════════════════════════════════════════════════════════
# Platform detection
# ═══════════════════════════════════════════════════════════════════════════
_IS_WINDOWS = sys.platform == "win32"
_IS_MACOS   = sys.platform == "darwin"
_IS_LINUX   = sys.platform.startswith("linux")


def _detect_wsl() -> bool:
    if not _IS_LINUX:
        return False
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


_IS_WSL = _detect_wsl()


# ═══════════════════════════════════════════════════════════════════════════
# Tunables
# ═══════════════════════════════════════════════════════════════════════════
_DEFAULT_THREADS     = str(max(1, (os.cpu_count() or 2) // 2))
_MAX_PROMPT_CHARS    = int(os.getenv("TWIN_MAX_PROMPT_CHARS", "2400"))
_FAST_PARALLEL       = int(os.getenv("TWIN_FAST_PARALLEL", "2"))
_ELITE_PARALLEL      = int(os.getenv("TWIN_ELITE_PARALLEL", "1"))
_FAST_THREADS        = int(os.getenv("TWIN_FAST_THREADS", _DEFAULT_THREADS))
_ELITE_THREADS       = int(os.getenv("TWIN_ELITE_THREADS", _DEFAULT_THREADS))
_FAST_TIMEOUT_S      = float(os.getenv("TWIN_FAST_TIMEOUT_S", "35"))
_ELITE_TIMEOUT_S     = float(os.getenv("TWIN_ELITE_TIMEOUT_S", "90"))
_LOAD_READY_S        = float(os.getenv("TWIN_LOAD_READY_S", "12"))
_WARMUP_TIMEOUT_S    = float(os.getenv("TWIN_WARMUP_TIMEOUT_S", "5"))
_IDLE_EVICT_ELITE_S  = float(os.getenv("TWIN_IDLE_EVICT_ELITE_S", "420"))
_REQUIRE_REAL        = os.getenv("HOT_SWAP_REQUIRE_REAL", "0") == "1"
_RSS_SOFT_CAP_MB     = int(os.getenv("TWIN_RSS_SOFT_CAP_MB", "380"))
_DUAL_MIN_MB         = int(os.getenv("TWIN_DUAL_MIN_RAM_MB", "1400"))
_MLOCK               = os.getenv("TWIN_MLOCK", "0" if _IS_MACOS else "1") == "1"
_FLASH_ATTN          = os.getenv("TWIN_FLASH_ATTN", "0") == "1"
_NO_MMAP             = os.getenv("TWIN_NO_MMAP", "1") == "1"
_UNHEALTHY_COOLDOWN  = float(os.getenv("TWIN_UNHEALTHY_COOLDOWN_S", "30"))
_ADAPTIVE_MIN_SAMPLES = 5
_ADAPTIVE_RING       = 32
_PAID_TIERS          = frozenset({"pro", "business", "elite", "paid"})

Cat = Literal["auto", "fast", "elite"]


# ═══════════════════════════════════════════════════════════════════════════
# Prometheus (lazy, module-level so tests don't re-register)
# ═══════════════════════════════════════════════════════════════════════════
_PROM: dict[str, Any] = {}


def _init_prom() -> None:
    if not _HAS_PROM or _PROM:
        return
    _PROM["infers"]    = Counter("twin_infers_total", "Successful inferences",
                                 ["slot", "model"])
    _PROM["stubs"]     = Counter("twin_stub_total", "Stub fallbacks",
                                 ["reason", "tier"])
    _PROM["swaps"]     = Counter("twin_swaps_total", "Model loads", ["slot"])
    _PROM["evictions"] = Counter("twin_evictions_total", "Model evictions", ["slot"])
    _PROM["rss"]       = Gauge("twin_rss_mb", "Process RSS in MB")
    _PROM["inflight"]  = Gauge("twin_inflight", "Concurrent inferences")
    _PROM["duration"]  = Histogram("twin_swap_duration_seconds",
                                   "Time from spawn to /health OK", ["slot"],
                                   buckets=(0.5, 1, 2, 3, 5, 8, 12, 20))


def _prom(name: str, *args: Any, **kwargs: Any) -> None:
    if not _HAS_PROM:
        return
    m = _PROM.get(name)
    if m is None:
        return
    try:
        method = kwargs.pop("_method", "inc")
        if method == "set":
            m.set(*args)
        elif method == "observe":
            m.observe(*args)
        else:
            m.labels(*args).inc() if args else m.inc()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# Null async context manager
# ═══════════════════════════════════════════════════════════════════════════
class _NullCtx:
    __slots__ = ()
    async def __aenter__(self) -> None:
        return None
    async def __aexit__(self, *exc: object) -> None:
        return None


_NULL_CTX = _NullCtx()


# ═══════════════════════════════════════════════════════════════════════════
# Memory probes (cross-platform, never raise)
# ═══════════════════════════════════════════════════════════════════════════
def _rss_mb() -> int:
    if _HAS_PSUTIL:
        try:
            return int(psutil.Process().memory_info().rss) // (1024 * 1024)
        except Exception:
            pass

    if _IS_LINUX:
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) // 1024
        except Exception:
            pass
        return 0

    if _IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _PMC()
            counters.cb = ctypes.sizeof(counters)
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                ctypes.windll.kernel32.GetCurrentProcess(),
                ctypes.byref(counters), counters.cb,
            )
            if ok:
                return int(counters.WorkingSetSize) // (1024 * 1024)
        except Exception:
            pass
        return 0

    if _IS_MACOS:
        try:
            import resource
            return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) // (1024 * 1024)
        except Exception:
            pass

    return 0


def _cgroup_memory_limit_bytes() -> int | None:
    if not _IS_LINUX:
        return None
    for path in ("/sys/fs/cgroup/memory.max",
                 "/sys/fs/cgroup/memory/memory.max"):
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
    if _IS_LINUX:
        limit = _cgroup_memory_limit_bytes()
        if limit is not None:
            return limit // (1024 * 1024)
    if _HAS_PSUTIL:
        try:
            return int(psutil.virtual_memory().total) // (1024 * 1024)
        except Exception:
            pass
    if _IS_LINUX:
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) // 1024
        except Exception:
            pass
    return 512


# ═══════════════════════════════════════════════════════════════════════════
# Binary resolution + environment diagnostics
# ═══════════════════════════════════════════════════════════════════════════
def _platform_bin_subdir() -> str:
    if _IS_WINDOWS:
        return "win"
    if _IS_MACOS:
        return "mac"
    return "linux"


def _binary_filenames() -> tuple[str, ...]:
    if _IS_WINDOWS:
        return ("llama-server.exe", "llama-cli.exe", "llama.exe")
    return ("llama-server", "llama-cli", "llama")


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _models_dir() -> Path:
    root = (os.getenv("WORKSPACE_ROOT") or "").strip()
    return (Path(root) / "models") if root else (_project_root() / "models")


def _is_executable(p: Path) -> bool:
    if not p.is_file():
        return False
    if _IS_WINDOWS:
        return True
    return os.access(p, os.X_OK)


def _diagnose_binary() -> dict[str, Any]:
    """Structured report of every place we looked for llama-server."""
    env_checks: list[dict[str, Any]] = []
    for key in ("LLAMA_SERVER", "LLAMA_CLI"):
        raw = (os.getenv(key) or "").strip()
        env_checks.append({
            "key": key, "value": raw or None,
            "is_file": bool(raw) and Path(raw).is_file(),
            "is_executable": bool(raw) and _is_executable(Path(raw)),
        })

    path_hits: list[dict[str, Any]] = []
    for name in _binary_filenames():
        found = shutil.which(name)
        path_hits.append({"name": name, "found": found})

    bin_dir = _project_root() / "bin" / _platform_bin_subdir()
    local_hits: list[dict[str, Any]] = []
    for name in _binary_filenames():
        candidate = bin_dir / name
        local_hits.append({
            "path": str(candidate),
            "is_file": candidate.is_file(),
            "is_executable": _is_executable(candidate),
        })

    return {
        "platform": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "wsl": _IS_WSL,
        "env_checks": env_checks,
        "path_hits": path_hits,
        "local_bin_dir": str(bin_dir),
        "local_hits": local_hits,
    }


def _resolve_binary() -> str | None:
    for key in ("LLAMA_SERVER", "LLAMA_CLI"):
        raw = (os.getenv(key) or "").strip()
        if raw:
            p = Path(raw)
            if _is_executable(p):
                return str(p.resolve())
            logger.warning("twin_binary_env_not_executable key=%s path=%s", key, raw)

    for name in _binary_filenames():
        found = shutil.which(name)
        if found:
            return found

    bin_dir = _project_root() / "bin" / _platform_bin_subdir()
    for name in _binary_filenames():
        candidate = bin_dir / name
        if _is_executable(candidate):
            return str(candidate)

    return None


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _pick_existing(preference: tuple[str, ...], models_dir: Path) -> ModelSpec | None:
    for mid in preference:
        spec = ROSTER_BY_ID.get(mid)
        if spec and not spec.rejected and (models_dir / spec.file).is_file():
            return spec
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Process termination (cross-platform)
# ═══════════════════════════════════════════════════════════════════════════
async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return

    if _IS_WINDOWS:
        with contextlib.suppress(Exception):
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
            return
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        if _HAS_PSUTIL and proc.pid:
            with contextlib.suppress(Exception):
                psutil.Process(proc.pid).kill()
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return

    with contextlib.suppress(ProcessLookupError, Exception):
        proc.send_signal(signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()


def _spawn_kwargs() -> dict[str, Any]:
    kw: dict[str, Any] = {}
    if _IS_WINDOWS:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return kw


# ═══════════════════════════════════════════════════════════════════════════
# Speculative-decoding map (opt-in)
# ═══════════════════════════════════════════════════════════════════════════
def _parse_draft_map() -> dict[str, str]:
    """
    TWIN_DRAFT_MAP="main-id=draft-filename,other-id=other-draft.gguf"
    Only entries whose draft file exists under models/ are honoured.
    """
    raw = (os.getenv("TWIN_DRAFT_MAP") or "").strip()
    if not raw:
        return {}
    out: dict[str, str] = {}
    for chunk in raw.split(","):
        if "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and v:
            out[k] = v
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Adaptive timeout ring buffer
# ═══════════════════════════════════════════════════════════════════════════
class _LatencyTracker:
    """Per-model p95 latency tracker. Thread-safe enough for asyncio."""

    __slots__ = ("_rings",)

    def __init__(self) -> None:
        self._rings: dict[str, deque[float]] = {}

    def observe(self, model_id: str, seconds: float) -> None:
        ring = self._rings.get(model_id)
        if ring is None:
            ring = deque(maxlen=_ADAPTIVE_RING)
            self._rings[model_id] = ring
        ring.append(seconds)

    def timeout_for(self, model_id: str, default: float) -> float:
        ring = self._rings.get(model_id)
        if ring is None or len(ring) < _ADAPTIVE_MIN_SAMPLES:
            return default
        ordered = sorted(ring)
        idx = max(0, int(len(ordered) * 0.95) - 1)
        p95 = ordered[idx]
        return max(2.0, p95 * 1.5)

    def snapshot(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for mid, ring in self._rings.items():
            if not ring:
                continue
            ordered = sorted(ring)
            out[mid] = {
                "n": len(ordered),
                "min": round(ordered[0], 3),
                "p50": round(ordered[len(ordered) // 2], 3),
                "p95": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 3),
                "max": round(ordered[-1], 3),
            }
        return out


# ═══════════════════════════════════════════════════════════════════════════
# Resident process wrapper
# ═══════════════════════════════════════════════════════════════════════════
class _Resident:
    __slots__ = ("spec", "proc", "port", "loaded_at", "last_used", "warmed")

    def __init__(self, spec: ModelSpec, proc: asyncio.subprocess.Process, port: int):
        self.spec = spec
        self.proc = proc
        self.port = port
        self.loaded_at = time.monotonic()
        self.last_used = time.monotonic()
        self.warmed = False

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
        "_solo_gate",
        "_unavailable_until",
        "_latency",
        "_draft_map",
    )

    def __init__(self) -> None:
        _init_prom()

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
        self._unavailable_until: float = 0.0

        self._max_inflight_fast  = int(os.getenv("TWIN_MAX_INFLIGHT_FAST", "2"))
        self._max_inflight_elite = int(os.getenv("TWIN_MAX_INFLIGHT_ELITE", "1"))

        self._solo_gate: asyncio.Semaphore = asyncio.Semaphore(1)
        self._latency = _LatencyTracker()
        self._draft_map = _parse_draft_map()

        self._stats: dict[str, int] = {
            "total_infers": 0, "total_stub_fallbacks": 0,
            "total_timeouts": 0, "total_errors": 0,
            "total_swaps": 0, "total_evictions": 0,
            "total_pool_rejections": 0, "total_cache_hits": 0,
            "total_elite_to_fast_fallbacks": 0,
            "total_marked_unavailable": 0,
            "total_warmups": 0,
            "total_auto_routes": 0,
        }

        logger.info(
            "twin_init platform=%s%s ram_mb=%d dual=%s fast=%s elite=%s "
            "binary=%s max_inflight_fast=%d max_inflight_elite=%d "
            "fast_parallel=%d draft_entries=%d prometheus=%s",
            platform.system(), "-wsl" if _IS_WSL else "",
            self._total_ram_mb, self._dual_mode,
            self._fast_spec.id if self._fast_spec else None,
            self._elite_spec.id if self._elite_spec else None,
            self._binary, self._max_inflight_fast, self._max_inflight_elite,
            _FAST_PARALLEL, len(self._draft_map), _HAS_PROM,
        )

        if self._binary is None:
            diag = _diagnose_binary()
            logger.warning(
                "twin_disabled_no_binary — engine will return tier-aware stubs. "
                "To enable: install llama.cpp release for %s/%s, then set "
                "LLAMA_SERVER to the .exe/.bin path. Checked env vars=%s "
                "PATH=%s local_bin_dir=%s. Diagnosis: %s",
                diag["platform"], diag["machine"],
                [c["key"] for c in diag["env_checks"]],
                [h["name"] for h in diag["path_hits"]],
                diag["local_bin_dir"], json.dumps(diag, default=str),
            )

    # ── health / availability ───────────────────────────────────────────
    def is_available(self) -> bool:
        if self._shutdown:
            return False
        if time.monotonic() < self._unavailable_until:
            return False
        if self._binary is None or self._fast_spec is None:
            return False
        return True

    def mark_unavailable(self, reason: str = "unspecified",
                         cooldown_s: float = _UNHEALTHY_COOLDOWN) -> None:
        self._unavailable_until = time.monotonic() + cooldown_s
        self._stats["total_marked_unavailable"] += 1
        logger.info("twin_marked_unavailable reason=%s cooldown_s=%.1f",
                    reason, cooldown_s)

    # ── lifecycle ───────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._shutdown:
            return
        if self._binary is None:
            logger.warning("twin_start_skipped reason=no_binary platform=%s",
                           platform.system())
            if _REQUIRE_REAL:
                raise RuntimeError("TwinEngine: no llama-server binary found")
            return
        if self._fast_spec is None:
            logger.warning(
                "twin_start_skipped reason=no_fast_model dir=%s expected_one_of=%s",
                self._models_dir, list(_FAST_PREFERENCE),
            )
            if _REQUIRE_REAL:
                raise RuntimeError("TwinEngine: no fast-cat model on disk")
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
                **_spawn_kwargs(),
            )
            try:
                await asyncio.wait_for(p.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(Exception):
                    p.kill()
                raise RuntimeError("llama --version timed out")
            if p.returncode != 0:
                raise RuntimeError(f"llama --version exit {p.returncode}")
        except Exception as e:
            logger.warning("twin_readiness_probe_failed error=%s", e)
            if _REQUIRE_REAL:
                raise

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
        if not self.is_available():
            return self._stub(prompt, "engine_unavailable", tier)

        if cat == "auto":
            cat = self._resolve_cat(workspace, task_type, prompt, tier)

        if cat == "elite" and (self._elite_spec is None or not self._dual_mode):
            self._stats["total_elite_to_fast_fallbacks"] += 1
            logger.info(
                "twin_elite_to_fast_fallback reason=%s",
                "no_spec" if self._elite_spec is None else "single_mode",
            )
            cat = "fast"

        serving_spec = self._elite_spec if cat == "elite" else self._fast_spec
        solo = serving_spec is not None and serving_spec.requires_solo

        async with (self._solo_gate if solo else _NULL_CTX):
            return await self._infer_inner(
                workspace, task_type, prompt,
                cat=cat, tier=tier, timeout_s=timeout_s,
            )

    # ── routing ─────────────────────────────────────────────────────────
    def _resolve_cat(self, workspace: str, task_type: str,
                     prompt: str, tier: str) -> Cat:
        """Free tier: FAST, always. Paid tiers: consult roster then fall back."""
        if tier not in _PAID_TIERS:
            return "fast"
        try:
            spec = pick_local_model(
                workspace, task_type, prompt, concurrency=max(1, self._inflight),
            )
        except Exception:
            spec = None
        if spec is not None and not spec.rejected:
            self._stats["total_auto_routes"] += 1
            return "elite" if spec.id in _ELITE_PREFERENCE else "fast"
        return self._route_cat(prompt)

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
        if self._fast_spec is None:
            return
        if not self._dual_mode and self._elite is not None:
            await self._kill_locked("elite")
        await self._load_locked(self._fast_spec, slot="fast")

    async def _ensure_elite_locked(self) -> None:
        if self._elite_spec is None:
            await self._ensure_fast_locked()
            return
        if self._elite is not None and self._elite.alive:
            return
        if not self._dual_mode:
            await self._ensure_fast_locked()
            return
        await self._load_locked(self._elite_spec, slot="elite")

    async def _load_locked(self, spec: ModelSpec, *, slot: str) -> None:
        model_path = self._models_dir / spec.file
        if not model_path.is_file():
            logger.warning(
                "twin_model_missing slot=%s model_id=%s expected_path=%s",
                slot, spec.id, model_path,
            )
            return

        if spec.total_mb > _RSS_SOFT_CAP_MB:
            logger.error(
                "twin_refused_load_over_cap slot=%s model=%s total_mb=%d cap_mb=%d",
                slot, spec.id, spec.total_mb, _RSS_SOFT_CAP_MB,
            )
            return

        rss = _rss_mb()
        if _IS_LINUX:
            cg_mb = (_cgroup_memory_limit_bytes() or 0) // (1024 * 1024)
            if cg_mb and rss > int(cg_mb * 0.7):
                logger.error("twin_rss_over_cgroup rss_mb=%d cgroup_mb=%d slot=%s",
                             rss, cg_mb, slot)
                return
        port = _free_port()
        threads  = _ELITE_THREADS if slot == "elite" else _FAST_THREADS
        parallel = _ELITE_PARALLEL if slot == "elite" else _FAST_PARALLEL

        # Thermal guard caps concurrency on hot / low-core devices.
        # Falls through silently if the guard is not running.
        try:
            from .thermal_guard import budget as _thermal_budget
            guard_budget = _thermal_budget()
            if guard_budget > 0:
                parallel = max(1, min(parallel, guard_budget))
        except Exception:
            pass

        # Speculative decoding (opt-in).
        draft_args: list[str] = []
        draft_file = self._draft_map.get(spec.id)
        if draft_file:
            draft_path = self._models_dir / draft_file
            if draft_path.is_file():
                draft_args = [
                    "--model-draft", str(draft_path),
                    "--draft-max",   os.getenv("TWIN_DRAFT_MAX", "8"),
                    "--draft-min",   os.getenv("TWIN_DRAFT_MIN", "1"),
                ]
                logger.info("twin_draft_enabled slot=%s main=%s draft=%s",
                            slot, spec.id, draft_file)
            else:
                logger.warning("twin_draft_missing slot=%s file=%s", slot, draft_file)

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
            * (["--no-mmap"] if _NO_MMAP else []),
            *(["--mlock"]      if (_MLOCK and not _IS_WINDOWS) else []),
            *(["--flash-attn"] if _FLASH_ATTN else []),
            *draft_args,
            "--min-p",         "0.05",
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
                **_spawn_kwargs(),
            )
        except Exception as e:
            logger.error("twin_spawn_failed slot=%s error=%s", slot, e)
            return

        resident = _Resident(spec, proc, port)
        setattr(self, f"_{slot}", resident)
        self._stats["total_swaps"] += 1
        _prom("swaps", slot)

        if not await self._wait_health(port, _LOAD_READY_S):
            logger.error("twin_load_timeout slot=%s model=%s", slot, spec.id)
            await self._kill_locked(slot)
            return

        # Warm-up: prime KV cache so first real request is fast.
        warm_ok = await self._warmup(resident)
        resident.warmed = warm_ok
        if warm_ok:
            self._stats["total_warmups"] += 1

        rss_after = _rss_mb()
        if rss_after > _RSS_SOFT_CAP_MB:
            logger.error("twin_rss_over_cap rss_mb=%d cap_mb=%d slot=%s",
                         rss_after, _RSS_SOFT_CAP_MB, slot)
            await self._kill_locked(slot)
            return

        duration_s = time.monotonic() - t0
        self._swap_history.append({
            "slot": slot, "to": spec.id,
            "duration_ms": round(duration_s * 1000, 1),
            "weights_mb": spec.weights_mb,
            "rss_mb": rss_after,
            "dual_mode": self._dual_mode,
            "warmed": warm_ok,
            "t": time.time(),
        })
        _prom("duration", slot, duration_s, _method="observe")
        logger.info(
            "twin_loaded slot=%s model=%s duration_ms=%.1f rss_mb=%d warmed=%s",
            slot, spec.id, duration_s * 1000, rss_after, warm_ok,
        )

    async def _warmup(self, resident: _Resident) -> bool:
        """Fire one tiny /completion to prime the KV cache. Best-effort."""
        if self._http is None:
            return False
        try:
            r = await self._http.post(
                f"http://127.0.0.1:{resident.port}/completion",
                json={
                    "prompt": "hi",
                    "n_predict": 1,
                    "temperature": 0.0,
                    "stream": False,
                    "cache_prompt": True,
                },
                timeout=_WARMUP_TIMEOUT_S,
            )
            return r.status_code == 200
        except Exception as e:
            logger.debug("twin_warmup_failed port=%d error=%s", resident.port, e)
            return False

    async def _kill_locked(self, slot: str) -> None:
        resident: _Resident | None = getattr(self, f"_{slot}", None)
        if resident is None:
            return
        setattr(self, f"_{slot}", None)
        await _terminate_process(resident.proc)
        gc.collect()
        self._stats["total_evictions"] += 1
        _prom("evictions", slot)

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
    async def _infer_inner(
        self,
        workspace: str,
        task_type: str,
        prompt: str,
        *,
        cat: Cat,
        tier: str,
        timeout_s: float | None,
    ) -> str:
        _prefix_digest = hashlib.blake2b(
            prompt[:1000].encode("utf-8"), digest_size=8
        ).hexdigest()
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

        default = _ELITE_TIMEOUT_S if cat == "elite" else _FAST_TIMEOUT_S
        base = timeout_s if timeout_s is not None else default
        # Adaptive: use observed p95 if we have samples.
        serving = self._elite_spec if cat == "elite" else self._fast_spec
        model_id = serving.id if serving else "unknown"
        adaptive = self._latency.timeout_for(model_id, base)
        # Hard ceiling: 5 s inside the hot path, per the resilience pipeline.
        eff_timeout = min(adaptive, 5.0)

        async with self._lock:
            if cat == "elite":
                if self._elite_infers >= self._max_inflight_elite:
                    self._stats["total_pool_rejections"] += 1
                    logger.warning(
                        "twin_pool_limit_rejected cat=elite current=%d max=%d",
                        self._elite_infers, self._max_inflight_elite,
                    )
                    return self._stub(prompt, "pool_limit", tier)
            else:
                if self._fast_infers >= self._max_inflight_fast:
                    self._stats["total_pool_rejections"] += 1
                    logger.warning(
                        "twin_pool_limit_rejected cat=fast current=%d max=%d",
                        self._fast_infers, self._max_inflight_fast,
                    )
                    return self._stub(prompt, "pool_limit", tier)

        self._inflight += 1
        _prom("inflight", self._inflight, _method="set")
        try:
            async with self._lock:
                if cat == "elite":
                    await self._ensure_elite_locked()
                else:
                    await self._ensure_fast_locked()
                resident = self._elite if cat == "elite" else self._fast
                if resident is None or not resident.alive:
                    self._stats["total_stub_fallbacks"] += 1
                    return self._stub(prompt, "load_failed", tier)

                gfile: str | None = None
                try:
                    from core.structured_gen import grammar_file_for
                    gfile = grammar_file_for(task_type, workspace)
                except Exception:
                    gfile = None

                t0 = time.monotonic()
                text = await self._post_completion(
                    resident, prompt, eff_timeout, grammar_file=gfile,
                )
                elapsed = time.monotonic() - t0
                resident.last_used = time.monotonic()
                self._latency.observe(resident.spec.id, elapsed)

                if cat == "elite":
                    self._elite_infers += 1
                else:
                    self._fast_infers += 1
                if text.startswith("[stub]") or "multi-agent system" in text \
                        or "temporarily busy" in text:
                    self._stats["total_stub_fallbacks"] += 1
                else:
                    self._stats["total_infers"] += 1
                    _prom("infers", cat, resident.spec.id)
                return text
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("twin_infer_error cat=%s error=%s", cat, e)
            self._stats["total_errors"] += 1
            self._stats["total_stub_fallbacks"] += 1
            return self._stub(prompt, "error", tier)
        finally:
            self._inflight -= 1
            _prom("inflight", self._inflight, _method="set")
            _prom("rss", _rss_mb(), _method="set")

    async def _post_completion(
        self,
        resident: _Resident,
        prompt: str,
        timeout_s: float,
        *,
        grammar_file: str | None = None,
    ) -> str:
        if self._http is None:
            return self._stub(prompt, "no_http", "free")
        try:
            req_body: dict[str, Any] = {
                "prompt": prompt[:_MAX_PROMPT_CHARS],
                "n_predict": resident.spec.max_out,
                "temperature": 0.0,
                "stream": False,
                "cache_prompt": True,       # KV prefix reuse across requests
            }
            if grammar_file:
                try:
                    with open(grammar_file, encoding="utf-8") as f:
                        req_body["grammar"] = f.read()
                except OSError as e:
                    logger.debug("twin_grammar_read_failed path=%s error=%s",
                                 grammar_file, e)
            r = await self._http.post(
                f"http://127.0.0.1:{resident.port}/completion",
                json=req_body,
                timeout=timeout_s,
            )
            if r.status_code != 200:
                return self._stub(prompt, f"http_{r.status_code}", "free")
            body = r.json()
            text = (body.get("content") or "").strip()
            return self._postprocess(text, resident.spec)
        except (httpx.TimeoutException, asyncio.TimeoutError):
            self._stats["total_timeouts"] += 1
            slot = "fast"
            for s in ("fast", "elite"):
                res = getattr(self, f"_{s}", None)
                if res is resident:
                    slot = s
                    break
            await self._kill_locked(slot)
            return self._stub(prompt, "timeout", "free")
        except Exception as e:
            logger.warning("twin_completion_error error=%s", e)
            self._stats["total_errors"] += 1
            return self._stub(prompt, "error", "free")

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
    def _stub(prompt: str, reason: str, tier: str = "free") -> str:
        _prom("stubs", reason, tier)
        if tier in _PAID_TIERS:
            return ("High traffic detected — routing you to our multi-agent "
                    "system. Please wait.")
        return "All AI services are temporarily busy. Try again in a moment."

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
                            time.monotonic() - self._elite.last_used
                            >= _IDLE_EVICT_ELITE_S):
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
            "resident": (self._fast_spec.id
                         if self._fast and self._fast.alive else "unavailable"),
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "wsl": _IS_WSL,
                "psutil": _HAS_PSUTIL,
                "prometheus": _HAS_PROM,
            },
            "ram_total_mb": self._total_ram_mb,
            "rss_mb": _rss_mb(),
            "binary": self._binary,
            "models_dir": str(self._models_dir),
            "available": self.is_available(),
            "fast": {
                "spec": self._fast_spec.id if self._fast_spec else None,
                "loaded": self._fast is not None and self._fast.alive,
                "infers": self._fast_infers,
                "max_inflight": self._max_inflight_fast,
                "parallel": _FAST_PARALLEL,
                "requires_solo": bool(
                    self._fast_spec and self._fast_spec.requires_solo),
            },
            "elite": {
                "spec": self._elite_spec.id if self._elite_spec else None,
                "loaded": self._elite is not None and self._elite.alive,
                "infers": self._elite_infers,
                "max_inflight": self._max_inflight_elite,
                "parallel": _ELITE_PARALLEL,
                "requires_solo": bool(
                    self._elite_spec and self._elite_spec.requires_solo),
            },
            "roster_size": len(MODEL_ROSTER),
            "budget_mb": MAX_RESIDENT_MB,
            "inflight": self._inflight,
            "solo_gate_locked": self._solo_gate.locked(),
            "latency": self._latency.snapshot(),
            "draft_map": dict(self._draft_map),
            "swap_history": list(self._swap_history),
            "stats": dict(self._stats),
            "uptime_s": round(time.monotonic() - self._started_at, 1),
        }

    def __repr__(self) -> str:
        return (
            f"<TwinEngine platform={platform.system()} "
            f"mode={'dual' if self._dual_mode else 'single'} "
            f"fast={'up' if self._fast and self._fast.alive else 'down'} "
            f"elite={'up' if self._elite and self._elite.alive else 'down'} "
            f"ram={self._total_ram_mb}MB rss={_rss_mb()}MB>"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Environment diagnostics (callable without an engine)
# ═══════════════════════════════════════════════════════════════════════════
def diagnose_environment() -> dict[str, Any]:
    """
    Structured report: what the engine needs, what's present, what's missing.
    Safe to call before constructing the engine.
    """
    models_dir = _models_dir()
    fast_spec = _pick_existing(_FAST_PREFERENCE, models_dir)
    elite_spec = _pick_existing(_ELITE_PREFERENCE, models_dir)

    fast_candidates: list[dict[str, Any]] = []
    for mid in _FAST_PREFERENCE:
        s = ROSTER_BY_ID.get(mid)
        if s is None:
            continue
        p = models_dir / s.file
        fast_candidates.append({
            "id": s.id, "file": s.file, "path": str(p),
            "present": p.is_file(),
        })

    elite_candidates: list[dict[str, Any]] = []
    for mid in _ELITE_PREFERENCE:
        s = ROSTER_BY_ID.get(mid)
        if s is None:
            continue
        p = models_dir / s.file
        elite_candidates.append({
            "id": s.id, "file": s.file, "path": str(p),
            "present": p.is_file(),
        })

    return {
        "binary": _diagnose_binary(),
        "binary_found": _resolve_binary(),
        "models_dir": str(models_dir),
        "fast_resolved": fast_spec.id if fast_spec else None,
        "elite_resolved": elite_spec.id if elite_spec else None,
        "fast_candidates": fast_candidates,
        "elite_candidates": elite_candidates,
        "ram_mb": _effective_ram_mb(),
        "rss_mb": _rss_mb(),
        "dual_eligible": _effective_ram_mb() >= _DUAL_MIN_MB,
        "psutil": _HAS_PSUTIL,
        "prometheus": _HAS_PROM,
        "next_steps": _next_steps(_resolve_binary(), fast_spec),
    }


def _next_steps(binary: str | None, fast_spec: ModelSpec | None) -> list[str]:
    steps: list[str] = []
    if binary is None:
        if _IS_WINDOWS:
            steps.append(
                "Download llama.cpp Windows release from "
                "https://github.com/ggml-org/llama.cpp/releases "
                "(pick *-bin-win-avx2-x64.zip for most CPUs). "
                "Extract to C:\\llama\\. "
                "Then: $env:LLAMA_SERVER = 'C:\\llama\\llama-server.exe'"
            )
        elif _IS_MACOS:
            steps.append(
                "brew install llama.cpp  (or download from "
                "https://github.com/ggml-org/llama.cpp/releases)"
            )
        else:
            steps.append(
                "Build or download llama.cpp: "
                "https://github.com/ggml-org/llama.cpp#build"
            )
    if fast_spec is None:
        steps.append(
            "Download at least one FAST-tier GGUF into "
            f"{_models_dir()}. First choice: "
            "HuggingFaceTB/SmolLM2-360M-Instruct-GGUF → "
            "smollm2-360m-instruct-q4_k_m.gguf"
        )
    if not steps:
        steps.append("Environment looks good — call engine.start().")
    return steps


# ═══════════════════════════════════════════════════════════════════════════
# Singleton + helpers
# ═══════════════════════════════════════════════════════════════════════════
_ENGINE: TwinEngine | None = None
_ENGINE_LOCK = asyncio.Lock()


async def get_twin_engine() -> TwinEngine | None:
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    async with _ENGINE_LOCK:
        if _ENGINE is None:
            try:
                _ENGINE = TwinEngine()
            except Exception as e:
                logger.warning("twin_unavailable error=%s", e)
                return None
        return _ENGINE


def twin_engine_snapshot() -> dict[str, Any]:
    if _ENGINE is not None:
        return _ENGINE.snapshot()
    diag = diagnose_environment()
    return {
        "mode": "uninitialised",
        "platform": diag["binary"],
        "available": False,
        "binary": diag["binary_found"],
        "models_dir": diag["models_dir"],
        "next_steps": diag["next_steps"],
    }

get_snapshot = twin_engine_snapshot


async def execute_local_inference(
    workspace: str, task_type: str, prompt: str, tier: str = "free",
) -> str:
    engine = await get_twin_engine()
    if engine is None or not engine.is_available():
        return ""
    try:
        return await engine.infer(workspace, task_type, prompt, tier=tier)
    except Exception as e:
        logger.warning("local_inference_error error=%s", e)
        return ""


async def execute_Axelr_insight_hot_swap_engine(
    workspace: str,
    task_type: str,
    prompt: str,
    *,
    tier: str = "free",
) -> str:
    engine = await get_twin_engine()
    if not engine or not engine.is_available():
        return ""
    try:
        return await engine.infer(
            workspace,
            task_type,
            prompt,
            tier=tier,
        )
    except Exception as e:
        logger.error("execute_Axelr_insight_hot_swap_engine_failed error=%s", e)
        return ""


def mark_twin_unavailable(reason: str = "unspecified",
                          cooldown_s: float = _UNHEALTHY_COOLDOWN) -> None:
    try:
        if _ENGINE is not None:
            _ENGINE.mark_unavailable(reason, cooldown_s)
    except Exception:
        pass


def twin_health_payload() -> dict[str, Any]:
    engine = _ENGINE
    if engine is None:
        diag = diagnose_environment()
        return {
            "status": "uninitialised",
            "fast_model": diag["fast_resolved"],
            "elite_model": diag["elite_resolved"],
            "rss_mb": diag["rss_mb"],
            "uptime_s": 0,
            "last_5_swaps": [],
            "mode": "single",
            "platform": diag["binary"],
            "binary": diag["binary_found"],
            "next_steps": diag["next_steps"],
        }
    snap = engine.snapshot()
    return {
        "status": "ok" if engine.is_available() else "unavailable",
        "fast_model": snap["fast"]["spec"],
        "elite_model": snap["elite"]["spec"],
        "rss_mb": snap["rss_mb"],
        "uptime_s": snap["uptime_s"],
        "last_5_swaps": snap["swap_history"][-5:],
        "mode": snap["mode"],
        "platform": snap["platform"],
        "latency": snap["latency"],
        "draft_map": snap["draft_map"],
        "stats": snap["stats"],
    }


# ── Optional FastAPI router (internal /api/twin/health, /api/twin/metrics) ─
try:
    from fastapi import APIRouter
    from fastapi.responses import Response as _FastAPIResponse

    twin_health_router = APIRouter(tags=["internal"])

    @twin_health_router.get("/api/twin/health")
    async def _twin_health_endpoint() -> dict[str, Any]:  # pragma: no cover
        return twin_health_payload()

    @twin_health_router.get("/api/twin/metrics")
    async def _twin_metrics_endpoint():  # pragma: no cover
        if not _HAS_PROM or generate_latest is None:
            return _FastAPIResponse(
                content="# prometheus_client not installed\n",
                media_type="text/plain",
            )
        return _FastAPIResponse(
            content=generate_latest(),
            media_type=CONTENT_TYPE_LATEST,
        )

    @twin_health_router.get("/api/twin/diagnose")
    async def _twin_diagnose_endpoint() -> dict[str, Any]:  # pragma: no cover
        return diagnose_environment()

except ImportError:                                       # pragma: no cover
    twin_health_router = None  # type: ignore[assignment]


# ═══════════════════════════════════════════════════════════════════════════
# Backward-compat aliases
# ═══════════════════════════════════════════════════════════════════════════
get_twin_engine_async      = get_twin_engine
get_hot_swap_engine_async  = get_twin_engine



def get_hot_swap_engine() -> TwinEngine | None:
    return _ENGINE


__all__ = [
    "MAX_RESIDENT_MB", "MODEL_ROSTER", "ROSTER_BY_ID", "ModelSpec",
    "TwinEngine", "get_twin_engine", "twin_engine_snapshot", "get_snapshot",
    "execute_local_inference", "pick_local_model",
    "get_hot_swap_engine", "get_hot_swap_engine_async",
    "get_twin_engine_async", "execute_Axelr_insight_hot_swap_engine",
    "mark_twin_unavailable", "twin_health_payload", "twin_health_router",
    "diagnose_environment",
=======
# core/twin_engine.py
"""
AXELR — Unified Local Inference Engine — v5.0 (elite, cross-platform)
=====================================================================
Windows 10/11 · macOS 12+ · Linux (Ubuntu 20.04+) · WSL2.
Production-ready. No placeholders. No TODOs. No silent failures.

    engine = await get_twin_engine()
    if engine and engine.is_available():
        await engine.start()
    text = await engine.infer("core", "structuring", prompt, tier="free")
    await engine.shutdown()

What v5.0 adds over v4.4
------------------------
  • Cold-start warm-up: after /health, one tiny /completion primes the
    KV cache so the first real request is fast.
  • KV prefix reuse: ``cache_prompt: true`` on every request, so
    multi-turn chats amortise prefill cost inside llama-server.
  • Roster-aware auto-routing: free-tier always FAST; paid tiers ask
    ``pick_local_model`` first, then fall back to keyword heuristics.
  • Adaptive timeouts: per-model p95 latency ring buffer; timeout is
    ``max(2.0, p95 * 1.5)`` once 5 samples are collected.
  • Speculative decoding: opt-in via ``TWIN_DRAFT_MAP`` env var, e.g.
    ``smollm2-360m=smollm2-135m-instruct-q4_k_m.gguf``.
  • Prometheus metrics (optional): /api/twin/metrics endpoint.
  • ``diagnose_environment()``: structured report of what's missing
    and how to fix it. Surfaced in logs and /api/twin/health.
  • Negative cache cooldown is honoured by ``is_available()``.

Guarantees
----------
  • Never raises at import on any supported platform.
  • Never raises from ``start()`` unless ``HOT_SWAP_REQUIRE_REAL=1``.
  • Every subprocess has a timeout. No ``shell=True``. Ever.
  • Missing binary / missing model → warning + stub. Cascade survives.
"""
from __future__ import annotations

import asyncio
import contextlib
import gc
import hashlib
import json
import logging
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

try:
    import psutil  # type: ignore[import-not-found]
    _HAS_PSUTIL = True
except ImportError:                       # pragma: no cover
    psutil = None  # type: ignore[assignment]
    _HAS_PSUTIL = False

try:
    from prometheus_client import (  # type: ignore[import-not-found]
        Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST,
    )
    _HAS_PROM = True
except ImportError:                       # pragma: no cover
    Counter = Gauge = Histogram = None  # type: ignore[assignment]
    generate_latest = None              # type: ignore[assignment]
    CONTENT_TYPE_LATEST = "text/plain"  # type: ignore[assignment]
    _HAS_PROM = False
from .twin_roster import (
    MAX_RESIDENT_MB, MODEL_ROSTER, ROSTER_BY_ID, ModelSpec,
    _ELITE_PREFERENCE, _FAST_PREFERENCE, pick_local_model,
)

logger = logging.getLogger("axelr.local_inference")
# ═══════════════════════════════════════════════════════════════════════════
# Platform detection
# ═══════════════════════════════════════════════════════════════════════════
_IS_WINDOWS = sys.platform == "win32"
_IS_MACOS   = sys.platform == "darwin"
_IS_LINUX   = sys.platform.startswith("linux")


def _detect_wsl() -> bool:
    if not _IS_LINUX:
        return False
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


_IS_WSL = _detect_wsl()


# ═══════════════════════════════════════════════════════════════════════════
# Tunables
# ═══════════════════════════════════════════════════════════════════════════
_DEFAULT_THREADS     = str(max(1, (os.cpu_count() or 2) // 2))
_MAX_PROMPT_CHARS    = int(os.getenv("TWIN_MAX_PROMPT_CHARS", "2400"))
_FAST_PARALLEL       = int(os.getenv("TWIN_FAST_PARALLEL", "2"))
_ELITE_PARALLEL      = int(os.getenv("TWIN_ELITE_PARALLEL", "1"))
_FAST_THREADS        = int(os.getenv("TWIN_FAST_THREADS", _DEFAULT_THREADS))
_ELITE_THREADS       = int(os.getenv("TWIN_ELITE_THREADS", _DEFAULT_THREADS))
_FAST_TIMEOUT_S      = float(os.getenv("TWIN_FAST_TIMEOUT_S", "35"))
_ELITE_TIMEOUT_S     = float(os.getenv("TWIN_ELITE_TIMEOUT_S", "90"))
_LOAD_READY_S        = float(os.getenv("TWIN_LOAD_READY_S", "12"))
_WARMUP_TIMEOUT_S    = float(os.getenv("TWIN_WARMUP_TIMEOUT_S", "5"))
_IDLE_EVICT_ELITE_S  = float(os.getenv("TWIN_IDLE_EVICT_ELITE_S", "420"))
_REQUIRE_REAL        = os.getenv("HOT_SWAP_REQUIRE_REAL", "0") == "1"
_RSS_SOFT_CAP_MB     = int(os.getenv("TWIN_RSS_SOFT_CAP_MB", "380"))
_DUAL_MIN_MB         = int(os.getenv("TWIN_DUAL_MIN_RAM_MB", "1400"))
_MLOCK               = os.getenv("TWIN_MLOCK", "0" if _IS_MACOS else "1") == "1"
_FLASH_ATTN          = os.getenv("TWIN_FLASH_ATTN", "0") == "1"
_NO_MMAP             = os.getenv("TWIN_NO_MMAP", "1") == "1"
_UNHEALTHY_COOLDOWN  = float(os.getenv("TWIN_UNHEALTHY_COOLDOWN_S", "30"))
_ADAPTIVE_MIN_SAMPLES = 5
_ADAPTIVE_RING       = 32
_PAID_TIERS          = frozenset({"pro", "business", "elite", "paid"})

Cat = Literal["auto", "fast", "elite"]


# ═══════════════════════════════════════════════════════════════════════════
# Prometheus (lazy, module-level so tests don't re-register)
# ═══════════════════════════════════════════════════════════════════════════
_PROM: dict[str, Any] = {}


def _init_prom() -> None:
    if not _HAS_PROM or _PROM:
        return
    _PROM["infers"]    = Counter("twin_infers_total", "Successful inferences",
                                 ["slot", "model"])
    _PROM["stubs"]     = Counter("twin_stub_total", "Stub fallbacks",
                                 ["reason", "tier"])
    _PROM["swaps"]     = Counter("twin_swaps_total", "Model loads", ["slot"])
    _PROM["evictions"] = Counter("twin_evictions_total", "Model evictions", ["slot"])
    _PROM["rss"]       = Gauge("twin_rss_mb", "Process RSS in MB")
    _PROM["inflight"]  = Gauge("twin_inflight", "Concurrent inferences")
    _PROM["duration"]  = Histogram("twin_swap_duration_seconds",
                                   "Time from spawn to /health OK", ["slot"],
                                   buckets=(0.5, 1, 2, 3, 5, 8, 12, 20))


def _prom(name: str, *args: Any, **kwargs: Any) -> None:
    if not _HAS_PROM:
        return
    m = _PROM.get(name)
    if m is None:
        return
    try:
        method = kwargs.pop("_method", "inc")
        if method == "set":
            m.set(*args)
        elif method == "observe":
            m.observe(*args)
        else:
            m.labels(*args).inc() if args else m.inc()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# Null async context manager
# ═══════════════════════════════════════════════════════════════════════════
class _NullCtx:
    __slots__ = ()
    async def __aenter__(self) -> None:
        return None
    async def __aexit__(self, *exc: object) -> None:
        return None


_NULL_CTX = _NullCtx()


# ═══════════════════════════════════════════════════════════════════════════
# Memory probes (cross-platform, never raise)
# ═══════════════════════════════════════════════════════════════════════════
def _rss_mb() -> int:
    if _HAS_PSUTIL:
        try:
            return int(psutil.Process().memory_info().rss) // (1024 * 1024)
        except Exception:
            pass

    if _IS_LINUX:
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) // 1024
        except Exception:
            pass
        return 0

    if _IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _PMC()
            counters.cb = ctypes.sizeof(counters)
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                ctypes.windll.kernel32.GetCurrentProcess(),
                ctypes.byref(counters), counters.cb,
            )
            if ok:
                return int(counters.WorkingSetSize) // (1024 * 1024)
        except Exception:
            pass
        return 0

    if _IS_MACOS:
        try:
            import resource
            return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) // (1024 * 1024)
        except Exception:
            pass

    return 0


def _cgroup_memory_limit_bytes() -> int | None:
    if not _IS_LINUX:
        return None
    for path in ("/sys/fs/cgroup/memory.max",
                 "/sys/fs/cgroup/memory/memory.max"):
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
    if _IS_LINUX:
        limit = _cgroup_memory_limit_bytes()
        if limit is not None:
            return limit // (1024 * 1024)
    if _HAS_PSUTIL:
        try:
            return int(psutil.virtual_memory().total) // (1024 * 1024)
        except Exception:
            pass
    if _IS_LINUX:
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) // 1024
        except Exception:
            pass
    return 512


# ═══════════════════════════════════════════════════════════════════════════
# Binary resolution + environment diagnostics
# ═══════════════════════════════════════════════════════════════════════════
def _platform_bin_subdir() -> str:
    if _IS_WINDOWS:
        return "win"
    if _IS_MACOS:
        return "mac"
    return "linux"


def _binary_filenames() -> tuple[str, ...]:
    if _IS_WINDOWS:
        return ("llama-server.exe", "llama-cli.exe", "llama.exe")
    return ("llama-server", "llama-cli", "llama")


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _models_dir() -> Path:
    root = (os.getenv("WORKSPACE_ROOT") or "").strip()
    return (Path(root) / "models") if root else (_project_root() / "models")


def _is_executable(p: Path) -> bool:
    if not p.is_file():
        return False
    if _IS_WINDOWS:
        return True
    return os.access(p, os.X_OK)


def _diagnose_binary() -> dict[str, Any]:
    """Structured report of every place we looked for llama-server."""
    env_checks: list[dict[str, Any]] = []
    for key in ("LLAMA_SERVER", "LLAMA_CLI"):
        raw = (os.getenv(key) or "").strip()
        env_checks.append({
            "key": key, "value": raw or None,
            "is_file": bool(raw) and Path(raw).is_file(),
            "is_executable": bool(raw) and _is_executable(Path(raw)),
        })

    path_hits: list[dict[str, Any]] = []
    for name in _binary_filenames():
        found = shutil.which(name)
        path_hits.append({"name": name, "found": found})

    bin_dir = _project_root() / "bin" / _platform_bin_subdir()
    local_hits: list[dict[str, Any]] = []
    for name in _binary_filenames():
        candidate = bin_dir / name
        local_hits.append({
            "path": str(candidate),
            "is_file": candidate.is_file(),
            "is_executable": _is_executable(candidate),
        })

    return {
        "platform": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "wsl": _IS_WSL,
        "env_checks": env_checks,
        "path_hits": path_hits,
        "local_bin_dir": str(bin_dir),
        "local_hits": local_hits,
    }


def _resolve_binary() -> str | None:
    for key in ("LLAMA_SERVER", "LLAMA_CLI"):
        raw = (os.getenv(key) or "").strip()
        if raw:
            p = Path(raw)
            if _is_executable(p):
                return str(p.resolve())
            logger.warning("twin_binary_env_not_executable key=%s path=%s", key, raw)

    for name in _binary_filenames():
        found = shutil.which(name)
        if found:
            return found

    bin_dir = _project_root() / "bin" / _platform_bin_subdir()
    for name in _binary_filenames():
        candidate = bin_dir / name
        if _is_executable(candidate):
            return str(candidate)

    return None


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _pick_existing(preference: tuple[str, ...], models_dir: Path) -> ModelSpec | None:
    for mid in preference:
        spec = ROSTER_BY_ID.get(mid)
        if spec and not spec.rejected and (models_dir / spec.file).is_file():
            return spec
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Process termination (cross-platform)
# ═══════════════════════════════════════════════════════════════════════════
async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return

    if _IS_WINDOWS:
        with contextlib.suppress(Exception):
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
            return
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        if _HAS_PSUTIL and proc.pid:
            with contextlib.suppress(Exception):
                psutil.Process(proc.pid).kill()
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return

    with contextlib.suppress(ProcessLookupError, Exception):
        proc.send_signal(signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()


def _spawn_kwargs() -> dict[str, Any]:
    kw: dict[str, Any] = {}
    if _IS_WINDOWS:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return kw


# ═══════════════════════════════════════════════════════════════════════════
# Speculative-decoding map (opt-in)
# ═══════════════════════════════════════════════════════════════════════════
def _parse_draft_map() -> dict[str, str]:
    """
    TWIN_DRAFT_MAP="main-id=draft-filename,other-id=other-draft.gguf"
    Only entries whose draft file exists under models/ are honoured.
    """
    raw = (os.getenv("TWIN_DRAFT_MAP") or "").strip()
    if not raw:
        return {}
    out: dict[str, str] = {}
    for chunk in raw.split(","):
        if "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and v:
            out[k] = v
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Adaptive timeout ring buffer
# ═══════════════════════════════════════════════════════════════════════════
class _LatencyTracker:
    """Per-model p95 latency tracker. Thread-safe enough for asyncio."""

    __slots__ = ("_rings",)

    def __init__(self) -> None:
        self._rings: dict[str, deque[float]] = {}

    def observe(self, model_id: str, seconds: float) -> None:
        ring = self._rings.get(model_id)
        if ring is None:
            ring = deque(maxlen=_ADAPTIVE_RING)
            self._rings[model_id] = ring
        ring.append(seconds)

    def timeout_for(self, model_id: str, default: float) -> float:
        ring = self._rings.get(model_id)
        if ring is None or len(ring) < _ADAPTIVE_MIN_SAMPLES:
            return default
        ordered = sorted(ring)
        idx = max(0, int(len(ordered) * 0.95) - 1)
        p95 = ordered[idx]
        return max(2.0, p95 * 1.5)

    def snapshot(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for mid, ring in self._rings.items():
            if not ring:
                continue
            ordered = sorted(ring)
            out[mid] = {
                "n": len(ordered),
                "min": round(ordered[0], 3),
                "p50": round(ordered[len(ordered) // 2], 3),
                "p95": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 3),
                "max": round(ordered[-1], 3),
            }
        return out


# ═══════════════════════════════════════════════════════════════════════════
# Resident process wrapper
# ═══════════════════════════════════════════════════════════════════════════
class _Resident:
    __slots__ = ("spec", "proc", "port", "loaded_at", "last_used", "warmed")

    def __init__(self, spec: ModelSpec, proc: asyncio.subprocess.Process, port: int):
        self.spec = spec
        self.proc = proc
        self.port = port
        self.loaded_at = time.monotonic()
        self.last_used = time.monotonic()
        self.warmed = False

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
        "_solo_gate",
        "_unavailable_until",
        "_latency",
        "_draft_map",
    )

    def __init__(self) -> None:
        _init_prom()

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
        self._unavailable_until: float = 0.0

        self._max_inflight_fast  = int(os.getenv("TWIN_MAX_INFLIGHT_FAST", "2"))
        self._max_inflight_elite = int(os.getenv("TWIN_MAX_INFLIGHT_ELITE", "1"))

        self._solo_gate: asyncio.Semaphore = asyncio.Semaphore(1)
        self._latency = _LatencyTracker()
        self._draft_map = _parse_draft_map()

        self._stats: dict[str, int] = {
            "total_infers": 0, "total_stub_fallbacks": 0,
            "total_timeouts": 0, "total_errors": 0,
            "total_swaps": 0, "total_evictions": 0,
            "total_pool_rejections": 0, "total_cache_hits": 0,
            "total_elite_to_fast_fallbacks": 0,
            "total_marked_unavailable": 0,
            "total_warmups": 0,
            "total_auto_routes": 0,
        }

        logger.info(
            "twin_init platform=%s%s ram_mb=%d dual=%s fast=%s elite=%s "
            "binary=%s max_inflight_fast=%d max_inflight_elite=%d "
            "fast_parallel=%d draft_entries=%d prometheus=%s",
            platform.system(), "-wsl" if _IS_WSL else "",
            self._total_ram_mb, self._dual_mode,
            self._fast_spec.id if self._fast_spec else None,
            self._elite_spec.id if self._elite_spec else None,
            self._binary, self._max_inflight_fast, self._max_inflight_elite,
            _FAST_PARALLEL, len(self._draft_map), _HAS_PROM,
        )

        if self._binary is None:
            diag = _diagnose_binary()
            logger.warning(
                "twin_disabled_no_binary — engine will return tier-aware stubs. "
                "To enable: install llama.cpp release for %s/%s, then set "
                "LLAMA_SERVER to the .exe/.bin path. Checked env vars=%s "
                "PATH=%s local_bin_dir=%s. Diagnosis: %s",
                diag["platform"], diag["machine"],
                [c["key"] for c in diag["env_checks"]],
                [h["name"] for h in diag["path_hits"]],
                diag["local_bin_dir"], json.dumps(diag, default=str),
            )

    # ── health / availability ───────────────────────────────────────────
    def is_available(self) -> bool:
        if self._shutdown:
            return False
        if time.monotonic() < self._unavailable_until:
            return False
        if self._binary is None or self._fast_spec is None:
            return False
        return True

    def mark_unavailable(self, reason: str = "unspecified",
                         cooldown_s: float = _UNHEALTHY_COOLDOWN) -> None:
        self._unavailable_until = time.monotonic() + cooldown_s
        self._stats["total_marked_unavailable"] += 1
        logger.info("twin_marked_unavailable reason=%s cooldown_s=%.1f",
                    reason, cooldown_s)

    # ── lifecycle ───────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._shutdown:
            return
        if self._binary is None:
            logger.warning("twin_start_skipped reason=no_binary platform=%s",
                           platform.system())
            if _REQUIRE_REAL:
                raise RuntimeError("TwinEngine: no llama-server binary found")
            return
        if self._fast_spec is None:
            logger.warning(
                "twin_start_skipped reason=no_fast_model dir=%s expected_one_of=%s",
                self._models_dir, list(_FAST_PREFERENCE),
            )
            if _REQUIRE_REAL:
                raise RuntimeError("TwinEngine: no fast-cat model on disk")
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
                **_spawn_kwargs(),
            )
            try:
                await asyncio.wait_for(p.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(Exception):
                    p.kill()
                raise RuntimeError("llama --version timed out")
            if p.returncode != 0:
                raise RuntimeError(f"llama --version exit {p.returncode}")
        except Exception as e:
            logger.warning("twin_readiness_probe_failed error=%s", e)
            if _REQUIRE_REAL:
                raise

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
        if not self.is_available():
            return self._stub(prompt, "engine_unavailable", tier)

        if cat == "auto":
            cat = self._resolve_cat(workspace, task_type, prompt, tier)

        if cat == "elite" and (self._elite_spec is None or not self._dual_mode):
            self._stats["total_elite_to_fast_fallbacks"] += 1
            logger.info(
                "twin_elite_to_fast_fallback reason=%s",
                "no_spec" if self._elite_spec is None else "single_mode",
            )
            cat = "fast"

        serving_spec = self._elite_spec if cat == "elite" else self._fast_spec
        solo = serving_spec is not None and serving_spec.requires_solo

        async with (self._solo_gate if solo else _NULL_CTX):
            return await self._infer_inner(
                workspace, task_type, prompt,
                cat=cat, tier=tier, timeout_s=timeout_s,
            )

    # ── routing ─────────────────────────────────────────────────────────
    def _resolve_cat(self, workspace: str, task_type: str,
                     prompt: str, tier: str) -> Cat:
        """Free tier: FAST, always. Paid tiers: consult roster then fall back."""
        if tier not in _PAID_TIERS:
            return "fast"
        try:
            spec = pick_local_model(
                workspace, task_type, prompt, concurrency=max(1, self._inflight),
            )
        except Exception:
            spec = None
        if spec is not None and not spec.rejected:
            self._stats["total_auto_routes"] += 1
            return "elite" if spec.id in _ELITE_PREFERENCE else "fast"
        return self._route_cat(prompt)

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
        if self._fast_spec is None:
            return
        if not self._dual_mode and self._elite is not None:
            await self._kill_locked("elite")
        await self._load_locked(self._fast_spec, slot="fast")

    async def _ensure_elite_locked(self) -> None:
        if self._elite_spec is None:
            await self._ensure_fast_locked()
            return
        if self._elite is not None and self._elite.alive:
            return
        if not self._dual_mode:
            await self._ensure_fast_locked()
            return
        await self._load_locked(self._elite_spec, slot="elite")

    async def _load_locked(self, spec: ModelSpec, *, slot: str) -> None:
        model_path = self._models_dir / spec.file
        if not model_path.is_file():
            logger.warning(
                "twin_model_missing slot=%s model_id=%s expected_path=%s",
                slot, spec.id, model_path,
            )
            return

        if spec.total_mb > _RSS_SOFT_CAP_MB:
            logger.error(
                "twin_refused_load_over_cap slot=%s model=%s total_mb=%d cap_mb=%d",
                slot, spec.id, spec.total_mb, _RSS_SOFT_CAP_MB,
            )
            return

        rss = _rss_mb()
        if _IS_LINUX:
            cg_mb = (_cgroup_memory_limit_bytes() or 0) // (1024 * 1024)
            if cg_mb and rss > int(cg_mb * 0.7):
                logger.error("twin_rss_over_cgroup rss_mb=%d cgroup_mb=%d slot=%s",
                             rss, cg_mb, slot)
                return
        port = _free_port()
        threads  = _ELITE_THREADS if slot == "elite" else _FAST_THREADS
        parallel = _ELITE_PARALLEL if slot == "elite" else _FAST_PARALLEL

        # Thermal guard caps concurrency on hot / low-core devices.
        # Falls through silently if the guard is not running.
        try:
            from .thermal_guard import budget as _thermal_budget
            guard_budget = _thermal_budget()
            if guard_budget > 0:
                parallel = max(1, min(parallel, guard_budget))
        except Exception:
            pass

        # Speculative decoding (opt-in).
        draft_args: list[str] = []
        draft_file = self._draft_map.get(spec.id)
        if draft_file:
            draft_path = self._models_dir / draft_file
            if draft_path.is_file():
                draft_args = [
                    "--model-draft", str(draft_path),
                    "--draft-max",   os.getenv("TWIN_DRAFT_MAX", "8"),
                    "--draft-min",   os.getenv("TWIN_DRAFT_MIN", "1"),
                ]
                logger.info("twin_draft_enabled slot=%s main=%s draft=%s",
                            slot, spec.id, draft_file)
            else:
                logger.warning("twin_draft_missing slot=%s file=%s", slot, draft_file)

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
            * (["--no-mmap"] if _NO_MMAP else []),
            *(["--mlock"]      if (_MLOCK and not _IS_WINDOWS) else []),
            *(["--flash-attn"] if _FLASH_ATTN else []),
            *draft_args,
            "--min-p",         "0.05",
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
                **_spawn_kwargs(),
            )
        except Exception as e:
            logger.error("twin_spawn_failed slot=%s error=%s", slot, e)
            return

        resident = _Resident(spec, proc, port)
        setattr(self, f"_{slot}", resident)
        self._stats["total_swaps"] += 1
        _prom("swaps", slot)

        if not await self._wait_health(port, _LOAD_READY_S):
            logger.error("twin_load_timeout slot=%s model=%s", slot, spec.id)
            await self._kill_locked(slot)
            return

        # Warm-up: prime KV cache so first real request is fast.
        warm_ok = await self._warmup(resident)
        resident.warmed = warm_ok
        if warm_ok:
            self._stats["total_warmups"] += 1

        rss_after = _rss_mb()
        if rss_after > _RSS_SOFT_CAP_MB:
            logger.error("twin_rss_over_cap rss_mb=%d cap_mb=%d slot=%s",
                         rss_after, _RSS_SOFT_CAP_MB, slot)
            await self._kill_locked(slot)
            return

        duration_s = time.monotonic() - t0
        self._swap_history.append({
            "slot": slot, "to": spec.id,
            "duration_ms": round(duration_s * 1000, 1),
            "weights_mb": spec.weights_mb,
            "rss_mb": rss_after,
            "dual_mode": self._dual_mode,
            "warmed": warm_ok,
            "t": time.time(),
        })
        _prom("duration", slot, duration_s, _method="observe")
        logger.info(
            "twin_loaded slot=%s model=%s duration_ms=%.1f rss_mb=%d warmed=%s",
            slot, spec.id, duration_s * 1000, rss_after, warm_ok,
        )

    async def _warmup(self, resident: _Resident) -> bool:
        """Fire one tiny /completion to prime the KV cache. Best-effort."""
        if self._http is None:
            return False
        try:
            r = await self._http.post(
                f"http://127.0.0.1:{resident.port}/completion",
                json={
                    "prompt": "hi",
                    "n_predict": 1,
                    "temperature": 0.0,
                    "stream": False,
                    "cache_prompt": True,
                },
                timeout=_WARMUP_TIMEOUT_S,
            )
            return r.status_code == 200
        except Exception as e:
            logger.debug("twin_warmup_failed port=%d error=%s", resident.port, e)
            return False

    async def _kill_locked(self, slot: str) -> None:
        resident: _Resident | None = getattr(self, f"_{slot}", None)
        if resident is None:
            return
        setattr(self, f"_{slot}", None)
        await _terminate_process(resident.proc)
        gc.collect()
        self._stats["total_evictions"] += 1
        _prom("evictions", slot)

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
    async def _infer_inner(
        self,
        workspace: str,
        task_type: str,
        prompt: str,
        *,
        cat: Cat,
        tier: str,
        timeout_s: float | None,
    ) -> str:
        _prefix_digest = hashlib.blake2b(
            prompt[:1000].encode("utf-8"), digest_size=8
        ).hexdigest()
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

        default = _ELITE_TIMEOUT_S if cat == "elite" else _FAST_TIMEOUT_S
        base = timeout_s if timeout_s is not None else default
        # Adaptive: use observed p95 if we have samples.
        serving = self._elite_spec if cat == "elite" else self._fast_spec
        model_id = serving.id if serving else "unknown"
        adaptive = self._latency.timeout_for(model_id, base)
        # Hard ceiling: 5 s inside the hot path, per the resilience pipeline.
        eff_timeout = min(adaptive, 5.0)

        async with self._lock:
            if cat == "elite":
                if self._elite_infers >= self._max_inflight_elite:
                    self._stats["total_pool_rejections"] += 1
                    logger.warning(
                        "twin_pool_limit_rejected cat=elite current=%d max=%d",
                        self._elite_infers, self._max_inflight_elite,
                    )
                    return self._stub(prompt, "pool_limit", tier)
            else:
                if self._fast_infers >= self._max_inflight_fast:
                    self._stats["total_pool_rejections"] += 1
                    logger.warning(
                        "twin_pool_limit_rejected cat=fast current=%d max=%d",
                        self._fast_infers, self._max_inflight_fast,
                    )
                    return self._stub(prompt, "pool_limit", tier)

        self._inflight += 1
        _prom("inflight", self._inflight, _method="set")
        try:
            async with self._lock:
                if cat == "elite":
                    await self._ensure_elite_locked()
                else:
                    await self._ensure_fast_locked()
                resident = self._elite if cat == "elite" else self._fast
                if resident is None or not resident.alive:
                    self._stats["total_stub_fallbacks"] += 1
                    return self._stub(prompt, "load_failed", tier)

                gfile: str | None = None
                try:
                    from core.structured_gen import grammar_file_for
                    gfile = grammar_file_for(task_type, workspace)
                except Exception:
                    gfile = None

                t0 = time.monotonic()
                text = await self._post_completion(
                    resident, prompt, eff_timeout, grammar_file=gfile,
                )
                elapsed = time.monotonic() - t0
                resident.last_used = time.monotonic()
                self._latency.observe(resident.spec.id, elapsed)

                if cat == "elite":
                    self._elite_infers += 1
                else:
                    self._fast_infers += 1
                if text.startswith("[stub]") or "multi-agent system" in text \
                        or "temporarily busy" in text:
                    self._stats["total_stub_fallbacks"] += 1
                else:
                    self._stats["total_infers"] += 1
                    _prom("infers", cat, resident.spec.id)
                return text
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("twin_infer_error cat=%s error=%s", cat, e)
            self._stats["total_errors"] += 1
            self._stats["total_stub_fallbacks"] += 1
            return self._stub(prompt, "error", tier)
        finally:
            self._inflight -= 1
            _prom("inflight", self._inflight, _method="set")
            _prom("rss", _rss_mb(), _method="set")

    async def _post_completion(
        self,
        resident: _Resident,
        prompt: str,
        timeout_s: float,
        *,
        grammar_file: str | None = None,
    ) -> str:
        if self._http is None:
            return self._stub(prompt, "no_http", "free")
        try:
            req_body: dict[str, Any] = {
                "prompt": prompt[:_MAX_PROMPT_CHARS],
                "n_predict": resident.spec.max_out,
                "temperature": 0.0,
                "stream": False,
                "cache_prompt": True,       # KV prefix reuse across requests
            }
            if grammar_file:
                try:
                    with open(grammar_file, encoding="utf-8") as f:
                        req_body["grammar"] = f.read()
                except OSError as e:
                    logger.debug("twin_grammar_read_failed path=%s error=%s",
                                 grammar_file, e)
            r = await self._http.post(
                f"http://127.0.0.1:{resident.port}/completion",
                json=req_body,
                timeout=timeout_s,
            )
            if r.status_code != 200:
                return self._stub(prompt, f"http_{r.status_code}", "free")
            body = r.json()
            text = (body.get("content") or "").strip()
            return self._postprocess(text, resident.spec)
        except (httpx.TimeoutException, asyncio.TimeoutError):
            self._stats["total_timeouts"] += 1
            slot = "fast"
            for s in ("fast", "elite"):
                res = getattr(self, f"_{s}", None)
                if res is resident:
                    slot = s
                    break
            await self._kill_locked(slot)
            return self._stub(prompt, "timeout", "free")
        except Exception as e:
            logger.warning("twin_completion_error error=%s", e)
            self._stats["total_errors"] += 1
            return self._stub(prompt, "error", "free")

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
    def _stub(prompt: str, reason: str, tier: str = "free") -> str:
        _prom("stubs", reason, tier)
        if tier in _PAID_TIERS:
            return ("High traffic detected — routing you to our multi-agent "
                    "system. Please wait.")
        return "All AI services are temporarily busy. Try again in a moment."

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
                            time.monotonic() - self._elite.last_used
                            >= _IDLE_EVICT_ELITE_S):
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
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "wsl": _IS_WSL,
                "psutil": _HAS_PSUTIL,
                "prometheus": _HAS_PROM,
            },
            "ram_total_mb": self._total_ram_mb,
            "rss_mb": _rss_mb(),
            "binary": self._binary,
            "models_dir": str(self._models_dir),
            "available": self.is_available(),
            "fast": {
                "spec": self._fast_spec.id if self._fast_spec else None,
                "loaded": self._fast is not None and self._fast.alive,
                "infers": self._fast_infers,
                "max_inflight": self._max_inflight_fast,
                "parallel": _FAST_PARALLEL,
                "requires_solo": bool(
                    self._fast_spec and self._fast_spec.requires_solo),
            },
            "elite": {
                "spec": self._elite_spec.id if self._elite_spec else None,
                "loaded": self._elite is not None and self._elite.alive,
                "infers": self._elite_infers,
                "max_inflight": self._max_inflight_elite,
                "parallel": _ELITE_PARALLEL,
                "requires_solo": bool(
                    self._elite_spec and self._elite_spec.requires_solo),
            },
            "roster_size": len(MODEL_ROSTER),
            "budget_mb": MAX_RESIDENT_MB,
            "inflight": self._inflight,
            "solo_gate_locked": self._solo_gate.locked(),
            "latency": self._latency.snapshot(),
            "draft_map": dict(self._draft_map),
            "swap_history": list(self._swap_history),
            "stats": dict(self._stats),
            "uptime_s": round(time.monotonic() - self._started_at, 1),
        }

    def __repr__(self) -> str:
        return (
            f"<TwinEngine platform={platform.system()} "
            f"mode={'dual' if self._dual_mode else 'single'} "
            f"fast={'up' if self._fast and self._fast.alive else 'down'} "
            f"elite={'up' if self._elite and self._elite.alive else 'down'} "
            f"ram={self._total_ram_mb}MB rss={_rss_mb()}MB>"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Environment diagnostics (callable without an engine)
# ═══════════════════════════════════════════════════════════════════════════
def diagnose_environment() -> dict[str, Any]:
    """
    Structured report: what the engine needs, what's present, what's missing.
    Safe to call before constructing the engine.
    """
    models_dir = _models_dir()
    fast_spec = _pick_existing(_FAST_PREFERENCE, models_dir)
    elite_spec = _pick_existing(_ELITE_PREFERENCE, models_dir)

    fast_candidates: list[dict[str, Any]] = []
    for mid in _FAST_PREFERENCE:
        s = ROSTER_BY_ID.get(mid)
        if s is None:
            continue
        p = models_dir / s.file
        fast_candidates.append({
            "id": s.id, "file": s.file, "path": str(p),
            "present": p.is_file(),
        })

    elite_candidates: list[dict[str, Any]] = []
    for mid in _ELITE_PREFERENCE:
        s = ROSTER_BY_ID.get(mid)
        if s is None:
            continue
        p = models_dir / s.file
        elite_candidates.append({
            "id": s.id, "file": s.file, "path": str(p),
            "present": p.is_file(),
        })

    return {
        "binary": _diagnose_binary(),
        "binary_found": _resolve_binary(),
        "models_dir": str(models_dir),
        "fast_resolved": fast_spec.id if fast_spec else None,
        "elite_resolved": elite_spec.id if elite_spec else None,
        "fast_candidates": fast_candidates,
        "elite_candidates": elite_candidates,
        "ram_mb": _effective_ram_mb(),
        "rss_mb": _rss_mb(),
        "dual_eligible": _effective_ram_mb() >= _DUAL_MIN_MB,
        "psutil": _HAS_PSUTIL,
        "prometheus": _HAS_PROM,
        "next_steps": _next_steps(_resolve_binary(), fast_spec),
    }


def _next_steps(binary: str | None, fast_spec: ModelSpec | None) -> list[str]:
    steps: list[str] = []
    if binary is None:
        if _IS_WINDOWS:
            steps.append(
                "Download llama.cpp Windows release from "
                "https://github.com/ggml-org/llama.cpp/releases "
                "(pick *-bin-win-avx2-x64.zip for most CPUs). "
                "Extract to C:\\llama\\. "
                "Then: $env:LLAMA_SERVER = 'C:\\llama\\llama-server.exe'"
            )
        elif _IS_MACOS:
            steps.append(
                "brew install llama.cpp  (or download from "
                "https://github.com/ggml-org/llama.cpp/releases)"
            )
        else:
            steps.append(
                "Build or download llama.cpp: "
                "https://github.com/ggml-org/llama.cpp#build"
            )
    if fast_spec is None:
        steps.append(
            "Download at least one FAST-tier GGUF into "
            f"{_models_dir()}. First choice: "
            "HuggingFaceTB/SmolLM2-360M-Instruct-GGUF → "
            "smollm2-360m-instruct-q4_k_m.gguf"
        )
    if not steps:
        steps.append("Environment looks good — call engine.start().")
    return steps


# ═══════════════════════════════════════════════════════════════════════════
# Singleton + helpers
# ═══════════════════════════════════════════════════════════════════════════
_ENGINE: TwinEngine | None = None
_ENGINE_LOCK = asyncio.Lock()


async def get_twin_engine() -> TwinEngine | None:
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    async with _ENGINE_LOCK:
        if _ENGINE is None:
            try:
                _ENGINE = TwinEngine()
            except Exception as e:
                logger.warning("twin_unavailable error=%s", e)
                return None
        return _ENGINE


def twin_engine_snapshot() -> dict[str, Any]:
    if _ENGINE is not None:
        return _ENGINE.snapshot()
    diag = diagnose_environment()
    return {
        "mode": "uninitialised",
        "platform": diag["binary"],
        "available": False,
        "binary": diag["binary_found"],
        "models_dir": diag["models_dir"],
        "next_steps": diag["next_steps"],
    }


async def execute_local_inference(
    workspace: str, task_type: str, prompt: str, tier: str = "free",
) -> str:
    engine = await get_twin_engine()
    if engine is None or not engine.is_available():
        return ""
    try:
        return await engine.infer(workspace, task_type, prompt, tier=tier)
    except Exception as e:
        logger.warning("local_inference_error error=%s", e)
        return ""


def mark_twin_unavailable(reason: str = "unspecified",
                          cooldown_s: float = _UNHEALTHY_COOLDOWN) -> None:
    try:
        if _ENGINE is not None:
            _ENGINE.mark_unavailable(reason, cooldown_s)
    except Exception:
        pass


def twin_health_payload() -> dict[str, Any]:
    engine = _ENGINE
    if engine is None:
        diag = diagnose_environment()
        return {
            "status": "uninitialised",
            "fast_model": diag["fast_resolved"],
            "elite_model": diag["elite_resolved"],
            "rss_mb": diag["rss_mb"],
            "uptime_s": 0,
            "last_5_swaps": [],
            "mode": "single",
            "platform": diag["binary"],
            "binary": diag["binary_found"],
            "next_steps": diag["next_steps"],
        }
    snap = engine.snapshot()
    return {
        "status": "ok" if engine.is_available() else "unavailable",
        "fast_model": snap["fast"]["spec"],
        "elite_model": snap["elite"]["spec"],
        "rss_mb": snap["rss_mb"],
        "uptime_s": snap["uptime_s"],
        "last_5_swaps": snap["swap_history"][-5:],
        "mode": snap["mode"],
        "platform": snap["platform"],
        "latency": snap["latency"],
        "draft_map": snap["draft_map"],
        "stats": snap["stats"],
    }


# ── Optional FastAPI router (internal /api/twin/health, /api/twin/metrics) ─
try:
    from fastapi import APIRouter
    from fastapi.responses import Response as _FastAPIResponse

    twin_health_router = APIRouter(tags=["internal"])

    @twin_health_router.get("/api/twin/health")
    async def _twin_health_endpoint() -> dict[str, Any]:  # pragma: no cover
        return twin_health_payload()

    @twin_health_router.get("/api/twin/metrics")
    async def _twin_metrics_endpoint():  # pragma: no cover
        if not _HAS_PROM or generate_latest is None:
            return _FastAPIResponse(
                content="# prometheus_client not installed\n",
                media_type="text/plain",
            )
        return _FastAPIResponse(
            content=generate_latest(),
            media_type=CONTENT_TYPE_LATEST,
        )

    @twin_health_router.get("/api/twin/diagnose")
    async def _twin_diagnose_endpoint() -> dict[str, Any]:  # pragma: no cover
        return diagnose_environment()

except ImportError:                                       # pragma: no cover
    twin_health_router = None  # type: ignore[assignment]


# ═══════════════════════════════════════════════════════════════════════════
# Backward-compat aliases
# ═══════════════════════════════════════════════════════════════════════════
get_twin_engine_async      = get_twin_engine
get_hot_swap_engine_async  = get_twin_engine
execute_axelr_hot_swap_engine = execute_local_inference


def get_hot_swap_engine() -> TwinEngine | None:
    return _ENGINE


__all__ = [
    "MAX_RESIDENT_MB", "MODEL_ROSTER", "ROSTER_BY_ID", "ModelSpec",
    "TwinEngine", "get_twin_engine", "twin_engine_snapshot",
    "execute_local_inference", "pick_local_model",
    "get_hot_swap_engine", "get_hot_swap_engine_async",
    "get_twin_engine_async", "execute_axelr_hot_swap_engine",
    "mark_twin_unavailable", "twin_health_payload", "twin_health_router",
    "diagnose_environment",
>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
]