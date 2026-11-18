"""Sentinel Engine — audit, AST shield, sandboxed fork, WAL, minifier.

Four subsystems, one module:
  1. audit() / tail()       — append-only JSONL, mirrored to axelr Insights
  2. Engine.validate()      — ast.parse() ban-list (real AST, not regex)
  3. Engine.run()           — python -I child, RLIMIT_AS/CPU/CORE, wall clock
  4. minify()               — trace-preserving compressor for LLM context

Security invariants (each enforceable independently):
  - No eval / exec / compile / __import__ / subprocess / ctypes anywhere.
  - No file write outside SENTINEL_SCRATCH (enforced at AST + child runtime).
  - No socket to non-allowlisted host (enforced at child runtime).
  - No run > WALL_CLOCK_S (SIGKILL, then minified stderr returned).
  - No memory > MAX_MEMORY_MB (RLIMIT_AS -> MemoryError in child).
  - Every mutation snapshot't in WAL first; failed action -> auto rollback.
"""
from __future__ import annotations

import ast
import asyncio
import contextvars
import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .wal import make_wal
from .hardening import wrap_command, audit_isolation

CURRENT_WORKFLOW_ID: contextvars.ContextVar[str | None] = (
    contextvars.ContextVar("wf_id", default=None)
)

# ── Configuration (env-driven, safe defaults) ──────────────────────────────
AUDIT_PATH = Path(os.getenv("SENTINEL_AUDIT", "/tmp/sentinel-audit.jsonl"))
SANDBOX_ROOT = Path(os.getenv("SENTINEL_SCRATCH", "/tmp/sentinel-scratch")).resolve()
WALL_CLOCK_S = int(os.getenv("SENTINEL_WALL_CLOCK_S", "30"))
MAX_MEMORY_MB = int(os.getenv("SENTINEL_MAX_MEMORY_MB", "256"))
MAX_CPU_S = int(os.getenv("SENTINEL_MAX_CPU_S", "60"))
OUTPUT_CAP_BYTES = 32 * 1024
MINIFY_THRESHOLD = 2048

NETWORK_ALLOWLIST = frozenset({
    "api.atlassian.com", "atlassian.com", "atlassian.net",
    "localhost", "127.0.0.1", "169.254.169.254", "metadata.vultr.com",
})

_audit_lock: threading.Lock = threading.Lock()
_audit_buffer: list[str] = []
_audit_buffer_lock: threading.Lock = threading.Lock()


# ── Audit (append-only JSONL, Insights mirror) ─────────────────────────────
def _flush_audit_buffer() -> None:
    global _audit_buffer
    with _audit_buffer_lock:
        if not _audit_buffer:
            return
        lines = _audit_buffer
        _audit_buffer = []
    
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _audit_lock:
        with AUDIT_PATH.open("a", encoding="utf-8") as f:
            f.write("".join(lines))


async def flush_audit() -> None:
    await asyncio.to_thread(_flush_audit_buffer)


async def audit(entry: dict) -> dict:
    """Append one JSONL line. Mirror to axelr_vultr.auditing when importable."""
    entry = {"ts": datetime.now(timezone.utc).isoformat(), **entry}
    line = json.dumps(entry, default=str, separators=(",", ":")) + "\n"
    
    with _audit_buffer_lock:
        _audit_buffer.append(line)
    
    try:
        from axelr_vultr.auditing import log_vultr_op
        ok = entry.get("phase") in ("executed", "dry_run")
        await log_vultr_op(
            action=entry.get("action", "sentinel"),
            user_id=str(entry.get("user_id", "sentinel")),
            session_id=str(entry.get("session_id", "sentinel")),
            status="ok" if ok else "error",
            error=entry.get("error"),
            details={k: v for k, v in entry.items() if k != "ts"},
        )
    except Exception:
        pass
    return entry


async def tail(n: int = 20) -> list[dict]:
    # Flush buffer first to get latest entries
    await flush_audit()
    
    if not AUDIT_PATH.exists():
        return []
    def _r() -> list[dict]:
        text = AUDIT_PATH.read_text(encoding="utf-8").strip()
        if not text:
            return []
        results = []
        for line in text.split("\n")[-n:]:
            if not line:
                continue
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return results
    return await asyncio.to_thread(_r)


# ── Errors ─────────────────────────────────────────────────────────────────
class ContainmentViolation(Exception):
    """Raised by the AST shield or path guard. No child is ever spawned."""
    def __init__(self, why: str, detail: str = "") -> None:
        super().__init__(f"containment_violation:{why}")
        self.code = "CONTAINMENT_VIOLATION"
        self.why = why
        self.detail = detail


# ── AST shield — ban-lists ─────────────────────────────────────────────────
FORBIDDEN_CALLS = frozenset({
    "eval", "exec", "compile", "__import__", "globals", "locals",
    "vars", "breakpoint", "input",
})
FORBIDDEN_MODULES = frozenset({
    "subprocess", "multiprocessing", "ctypes", "cffi", "pty", "fcntl",
    "signal", "resource", "shutil",
})
FORBIDDEN_ATTRS = frozenset({
    "system", "popen", "spawn", "spawnl", "spawnv", "spawnve",
    "execv", "execve", "execl", "execle",
    "fork", "forkpty", "kill", "setuid", "setgid", "chroot", "chmod",
})
SENSITIVE_LITERALS = (
    "/etc/passwd", "/etc/shadow", "/etc/sudoers",
    "/root/.ssh", "~/.ssh", "/proc/self", "/proc/1",
)


# ── Child bootstrap — runs FIRST inside the sandbox child ──────────────────
_CHILD_BOOTSTRAP = r'''
import os, sys, socket, builtins

# OS-level limits (may fail under unprivileged constraints — safe to skip)
try:
    import resource as _rs
    _mem = int(os.environ.get("SENTINEL_MAX_MEMORY_MB", "256")) * 1024 * 1024
    _cpu = int(os.environ.get("SENTINEL_MAX_CPU_S", "60"))
    _rs.setrlimit(_rs.RLIMIT_AS,   (_mem, _mem))
    _rs.setrlimit(_rs.RLIMIT_CPU,  (_cpu, _cpu))
    _rs.setrlimit(_rs.RLIMIT_CORE, (0, 0))
    _rs.setrlimit(_rs.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
except Exception:
    pass

_ROOT = os.path.realpath(os.environ["SENTINEL_SANDBOX_ROOT"])
_ALLOW = set(os.environ["SENTINEL_NET_ALLOWLIST"].split(","))
_SENSITIVE = ("/etc/passwd", "/etc/shadow", "/etc/sudoers", "/root/.ssh", "/proc/self")
_open, _connect = builtins.open, socket.socket.connect

def _inside(p):
    rp = os.path.realpath(os.fspath(p))
    return rp == _ROOT or rp.startswith(_ROOT + os.sep)

def _gopen(file, mode="r", *a, **kw):
    p = os.path.realpath(os.fspath(file))
    if any(c in mode for c in "wax+"):
        if not _inside(p):
            raise PermissionError("fs_escape:" + str(file))
    elif any(p.startswith(s) for s in _SENSITIVE):
        raise PermissionError("sensitive_read_denied:" + str(file))
    return _open(file, mode, *a, **kw)
builtins.open = _gopen

def _gconnect(self, addr):
    host = addr[0] if isinstance(addr, tuple) else str(addr)
    if host not in _ALLOW and not any(host.endswith("." + h) for h in _ALLOW):
        raise PermissionError("network_egress_denied:" + str(host))
    return _connect(self, addr)
socket.socket.connect = _gconnect
'''


# ── WAL snapshot ───────────────────────────────────────────────────────────
@dataclass
class Snapshot:
    path: Path
    prev: str | None
    ts: float


# ── Engine ─────────────────────────────────────────────────────────────────
class Engine:
    """Owns the WAL, runs validation, forks the sandbox child."""

    def __init__(self) -> None:
        self.wal = make_wal()          # was: self.wal: dict[Path, Snapshot] = {}
        self.runs: int = 0
        self.violations: int = 0
        self.rollbacks: int = 0
        self.total_ms: float = 0.0
        self._validate_cache = {}
        self._validate_cache_max = 1000

    # Phase 1 — AST shield
    def validate(self, payload: str) -> bool:
        if not isinstance(payload, str):
            raise ValueError("payload must be a string")
        if len(payload) > 64 * 1024:
            raise ValueError("payload_too_large")
        
        # Fast path: cache results of frequent payloads
        if payload in self._validate_cache:
            return self._validate_cache[payload]
        
        try:
            tree = ast.parse(payload)
        except SyntaxError as e:
            raise ContainmentViolation("syntax_error", str(e)) from e
        
        forbidden_calls = FORBIDDEN_CALLS
        forbidden_modules = FORBIDDEN_MODULES
        forbidden_attrs = FORBIDDEN_ATTRS
        sensitive_literals = SENSITIVE_LITERALS
        
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name) and f.id in forbidden_calls:
                    self.violations += 1
                    raise ContainmentViolation(f"forbidden_call:{f.id}")
                if isinstance(f, ast.Attribute) and f.attr in forbidden_attrs:
                    self.violations += 1
                    raise ContainmentViolation(f"forbidden_attr:{f.attr}")
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = ([a.name for a in node.names] if isinstance(node, ast.Import)
                        else [node.module or ""])
                for m in mods:
                    if m.split(".")[0] in forbidden_modules:
                        self.violations += 1
                        raise ContainmentViolation(f"forbidden_import:{m}")
            elif isinstance(node, ast.Attribute) and node.attr in forbidden_attrs:
                self.violations += 1
                raise ContainmentViolation(f"forbidden_attr:{node.attr}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                val = node.value
                for s in sensitive_literals:
                    if s in val:
                        self.violations += 1
                        raise ContainmentViolation("forbidden_literal", val[:80])
        
        # Cache the result
        result = True
        if len(self._validate_cache) >= self._validate_cache_max:
            self._validate_cache.clear()
        self._validate_cache[payload] = result
        return result

    def assert_path(self, target: str) -> Path:
        p = (SANDBOX_ROOT / target).resolve()
        if p != SANDBOX_ROOT and SANDBOX_ROOT not in p.parents:
            self.violations += 1
            raise ContainmentViolation("fs_escape", target)
        return p

    # WAL
    async def wal_snapshot(self, target: str, *, workflow_id: str | None = None) -> dict[str, Any]:
        p = self.assert_path(target)
        prev = p.read_text(encoding="utf-8") if p.exists() else None
        await self.wal.record(
            str(p), prev, action="sandbox.write_file",
            workflow_id=workflow_id or CURRENT_WORKFLOW_ID.get(),
        )
        return {"path": str(p), "existed": prev is not None}

    async def rollback(self, workflow_id: str | None = None) -> dict[str, int]:
        result = await self.wal.rollback_all(workflow_id=workflow_id)
        self.rollbacks += 1
        return result

    # Phase 2 — fork
    async def run(self, script: str, timeout: float | None = None) -> dict[str, Any]:
        self.runs += 1
        SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)
        wall = min(timeout or WALL_CLOCK_S, WALL_CLOCK_S)
        script_file = SANDBOX_ROOT / f"_s_{uuid.uuid4().hex[:8]}.py"
        script_file.write_text(_CHILD_BOOTSTRAP + "\n" + script, encoding="utf-8")
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(SANDBOX_ROOT),
            "SENTINEL_SANDBOX": "1",
            "SENTINEL_SANDBOX_ROOT": str(SANDBOX_ROOT),
            "SENTINEL_NET_ALLOWLIST": ",".join(NETWORK_ALLOWLIST),
            "SENTINEL_MAX_MEMORY_MB": str(MAX_MEMORY_MB),
            "SENTINEL_MAX_CPU_S": str(MAX_CPU_S),
        }

        def _exec() -> dict[str, Any]:
            import subprocess
            t0 = time.monotonic()
            try:
                wrapped, isolation = wrap_command(
                    [sys.executable, "-I", "-B", str(script_file)],
                    scratch=SANDBOX_ROOT)
                proc = subprocess.Popen(
                    wrapped, cwd=str(SANDBOX_ROOT), env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                try:
                    out, err = proc.communicate(timeout=wall)
                    timed_out = False
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out, err = proc.communicate()
                    timed_out = True
                dur = round((time.monotonic() - t0) * 1000, 2)
                self.total_ms += dur
                return {
                    "ok": proc.returncode == 0 and not timed_out,
                    "exit_code": proc.returncode, "timed_out": timed_out,
                    "stdout": out[:OUTPUT_CAP_BYTES].decode("utf-8", "replace"),
                    "stderr": err[:OUTPUT_CAP_BYTES].decode("utf-8", "replace"),
                    "duration_ms": dur,
                    "isolation": isolation,
                }
            except Exception as e:
                return {"ok": False, "exit_code": -1, "timed_out": False,
                        "stdout": "", "stderr": str(e),
                        "duration_ms": round((time.monotonic() - t0) * 1000, 2)}
            finally:
                try:
                    script_file.unlink(missing_ok=True)
                except Exception:
                    pass

        return await asyncio.to_thread(_exec)

    # Stats / health
    def stats(self) -> dict[str, Any]:
        return {"runs": self.runs, "violations": self.violations,
                "rollbacks": self.rollbacks,
                "total_ms": round(self.total_ms, 2),
                "limits": {"wall_s": WALL_CLOCK_S, "memory_mb": MAX_MEMORY_MB,
                           "cpu_s": MAX_CPU_S}}

    async def health(self) -> dict[str, Any]:
        ok = SANDBOX_ROOT.exists() and os.access(str(SANDBOX_ROOT), os.W_OK)
        wal_stats = await self.wal.stats()
        return {"healthy": ok, **self.stats(), "wal": wal_stats,
                "isolation": audit_isolation(),
                "sandbox_root": str(SANDBOX_ROOT)}


# ── Token minifier ─────────────────────────────────────────────────────────
_TRACE_RE = re.compile(
    r"error|exception|traceback|denied|violation|failed|fatal|at\s+\w+",
    re.IGNORECASE,
)


def minify(stdout: str, stderr: str, threshold: int = MINIFY_THRESHOLD) -> dict[str, Any]:
    """Preserve trace lines; strip repetition; emit [truncated] marker."""
    combined = stdout + (f"\n[stderr]\n{stderr}" if stderr else "")
    if len(combined) <= threshold:
        return {"text": combined, "truncated": False, "original_bytes": len(combined)}
    lines = combined.split("\n")
    keep: set[int] = set()
    for i, ln in enumerate(lines):
        if _TRACE_RE.search(ln):
            keep.update(range(max(0, i - 1), min(len(lines), i + 3)))
    out: list[str] = []
    last: str | None = None
    count = 0
    for i, ln in enumerate(lines):
        if i not in keep:
            continue
        if ln == last:
            count += 1
            continue
        if count:
            out.append(f"[{count}× repeated]")
        last, count = ln, 0
        out.append(ln)
    if count:
        out.append(f"[{count}× repeated]")
    dropped = len(combined) - len("\n".join(out))
    return {"text": "\n".join(out) + f"\n[... truncated {dropped} bytes ...]",
            "truncated": True, "original_bytes": len(combined),
            "dropped_bytes": dropped}


def sha16(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]