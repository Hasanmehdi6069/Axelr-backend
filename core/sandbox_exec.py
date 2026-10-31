<<<<<<< HEAD
# core/sandbox_exec.py
"""
AXELR — Zero-RAM Sandboxed Test Executor (F43)
=================================================
Stateless handler for `core.code_intel.TestLoop`. Runs code in a child
process with hard rlimits. The child exits; nothing persists in the
parent's heap. Replaces the previous worker-HTTP-only path.

Isolation:
    RLIMIT_AS     128 MB   — hard memory cap
    RLIMIT_CPU    N s      — CPU seconds
    RLIMIT_NPROC  0        — no fork bombs
    RLIMIT_NOFILE 16       — no fd leaks
    new session            — cannot signal the parent
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
from typing import Any
# resource is Unix-only; handle Windows compatibility
try:
    import resource
    HAS_RESOURCE = True
except ImportError:
    HAS_RESOURCE = False
    resource = None

logger = logging.getLogger("axelr.sandbox")

_ALLOWED = {
    "python": ["python3", "-I", "-S", "-B"],
    "py":     ["python3", "-I", "-S", "-B"],
    "javascript": ["node", "--no-warnings", "--jitless"],
    "js":         ["node", "--no-warnings", "--jitless"],
    "typescript": ["node", "--no-warnings", "--jitless"],
    "ts":         ["node", "--no-warnings", "--jitless"],
}


def _limits(memory_mb: int, cpu_s: int):
    mem = memory_mb * 1024 * 1024
    def _apply():
        # Only apply Unix-specific resource limits if the module is available
        if HAS_RESOURCE and resource is not None:
            os.setsid()  # new session — no signal to parent
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
            resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
            resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))
            with contextlib.suppress(Exception):
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return _apply


def _binary_for(lang: str) -> list[str] | None:
    cmd = _ALLOWED.get(lang.lower().strip())
    if not cmd:
        return None
    if not shutil.which(cmd[0]):
        return None
    return cmd


async def run_isolated(
    language: str,
    code: str,
    timeout_s: int = 8,
    *,
    memory_mb: int = 128,
) -> dict[str, Any]:
    """
    Execute `code` in an isolated child process. Never raises.

    Returns:
        {"success": bool, "output": str, "error": str, "exit_code": int}
    """
    if not isinstance(code, str) or not code.strip():
        return {"success": False, "output": "", "error": "empty_code", "exit_code": -1}
    if len(code) > 64 * 1024:
        return {"success": False, "output": "", "error": "code_too_large", "exit_code": -1}

    cmd = _binary_for(language)
    if cmd is None:
        return {"success": False, "output": "", "error": "language_unavailable", "exit_code": -1}

    timeout_s = max(1, min(int(timeout_s or 8), 30))
    memory_mb = max(32, min(int(memory_mb), 256))

    workdir = tempfile.mkdtemp(prefix="axelr-sbx-")
    try:
        ext = ".py" if language.lower() in ("python", "py") else ".js"
        src_path = os.path.join(workdir, f"code{ext}")
        with open(src_path, "w", encoding="utf-8") as f:
            f.write(code)

        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": workdir,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "NODE_OPTIONS": "--no-warnings",
        }

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, src_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                env=env,
                preexec_fn=_limits(memory_mb, timeout_s),
            )
        except Exception as e:
            logger.warning("sandbox_spawn_failed error=%s", e)
            return {"success": False, "output": "", "error": f"spawn_failed: {e}", "exit_code": -1}

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s + 1.0,
            )
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            return {"success": False, "output": "", "error": "timeout", "exit_code": -1}

        out = (stdout or b"").decode("utf-8", errors="replace")[:16_000]
        err = (stderr or b"").decode("utf-8", errors="replace")[:8_000]
        rc = proc.returncode if proc.returncode is not None else -1
        return {
            "success": rc == 0,
            "output": out,
            "error": "" if rc == 0 else (err or f"exit_code_{rc}"),
            "exit_code": rc,
        }
    finally:
        with contextlib.suppress(Exception):
            shutil.rmtree(workdir, ignore_errors=True)


=======
# core/sandbox_exec.py
"""
AXELR — Zero-RAM Sandboxed Test Executor (F43)
=================================================
Stateless handler for `core.code_intel.TestLoop`. Runs code in a child
process with hard rlimits. The child exits; nothing persists in the
parent's heap. Replaces the previous worker-HTTP-only path.

Isolation:
    RLIMIT_AS     128 MB   — hard memory cap
    RLIMIT_CPU    N s      — CPU seconds
    RLIMIT_NPROC  0        — no fork bombs
    RLIMIT_NOFILE 16       — no fd leaks
    new session            — cannot signal the parent
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import resource
import shutil
import tempfile
from typing import Any

logger = logging.getLogger("axelr.sandbox")

_ALLOWED = {
    "python": ["python3", "-I", "-S", "-B"],
    "py":     ["python3", "-I", "-S", "-B"],
    "javascript": ["node", "--no-warnings", "--jitless"],
    "js":         ["node", "--no-warnings", "--jitless"],
    "typescript": ["node", "--no-warnings", "--jitless"],
    "ts":         ["node", "--no-warnings", "--jitless"],
}


def _limits(memory_mb: int, cpu_s: int):
    mem = memory_mb * 1024 * 1024
    def _apply():
        os.setsid()  # new session — no signal to parent
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
        resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))
        with contextlib.suppress(Exception):
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return _apply


def _binary_for(lang: str) -> list[str] | None:
    cmd = _ALLOWED.get(lang.lower().strip())
    if not cmd:
        return None
    if not shutil.which(cmd[0]):
        return None
    return cmd


async def run_isolated(
    language: str,
    code: str,
    timeout_s: int = 8,
    *,
    memory_mb: int = 128,
) -> dict[str, Any]:
    """
    Execute `code` in an isolated child process. Never raises.

    Returns:
        {"success": bool, "output": str, "error": str, "exit_code": int}
    """
    if not isinstance(code, str) or not code.strip():
        return {"success": False, "output": "", "error": "empty_code", "exit_code": -1}
    if len(code) > 64 * 1024:
        return {"success": False, "output": "", "error": "code_too_large", "exit_code": -1}

    cmd = _binary_for(language)
    if cmd is None:
        return {"success": False, "output": "", "error": "language_unavailable", "exit_code": -1}

    timeout_s = max(1, min(int(timeout_s or 8), 30))
    memory_mb = max(32, min(int(memory_mb), 256))

    workdir = tempfile.mkdtemp(prefix="axelr-sbx-")
    try:
        ext = ".py" if language.lower() in ("python", "py") else ".js"
        src_path = os.path.join(workdir, f"code{ext}")
        with open(src_path, "w", encoding="utf-8") as f:
            f.write(code)

        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": workdir,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "NODE_OPTIONS": "--no-warnings",
        }

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, src_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                env=env,
                preexec_fn=_limits(memory_mb, timeout_s),
            )
        except Exception as e:
            logger.warning("sandbox_spawn_failed error=%s", e)
            return {"success": False, "output": "", "error": f"spawn_failed: {e}", "exit_code": -1}

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s + 1.0,
            )
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            return {"success": False, "output": "", "error": "timeout", "exit_code": -1}

        out = (stdout or b"").decode("utf-8", errors="replace")[:16_000]
        err = (stderr or b"").decode("utf-8", errors="replace")[:8_000]
        rc = proc.returncode if proc.returncode is not None else -1
        return {
            "success": rc == 0,
            "output": out,
            "error": "" if rc == 0 else (err or f"exit_code_{rc}"),
            "exit_code": rc,
        }
    finally:
        with contextlib.suppress(Exception):
            shutil.rmtree(workdir, ignore_errors=True)


>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
__all__ = ["run_isolated"]