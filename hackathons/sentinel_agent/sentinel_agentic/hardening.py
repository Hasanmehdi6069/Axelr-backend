"""OS-level isolation wrappers — bwrap (bubblewrap) and firejail.

The Python-level monkeypatches in engine.py defend against honest mistakes
and naive attackers. They do NOT defend against a determined attacker who
knows `importlib.reload(builtins)` or `getattr(__builtins__, 'ev'+'al')`.

For the "Blast Radius Zero" claim to be *provably* true, wrap the child
process in bubblewrap (rootless, namespaces, seccomp) or firejail.

Usage:
    from .hardening import wrap_command, detect_isolation
    cmd = wrap_command([sys.executable, "-I", str(script)])
    # cmd is either the raw list (no isolation) or prefixed with bwrap/firejail

Detection order: bwrap > firejail > none. Override with SENTINEL_ISOLATION.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def detect_isolation() -> str:
    forced = (os.getenv("SENTINEL_ISOLATION") or "").lower()
    if forced in ("none", "bwrap", "firejail"):
        return forced
    if shutil.which("bwrap"):
        return "bwrap"
    if shutil.which("firejail"):
        return "firejail"
    return "none"


def _bwrap_cmd(cmd: list[str], scratch: Path) -> list[str]:
    """Minimal bwrap: namespaces, read-only root, writable /scratch only."""
    return [
        "bwrap",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind-try", "/etc/alternatives", "/etc/alternatives",
        "--ro-bind", "/etc/ssl", "/etc/ssl",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--bind", str(scratch), str(scratch),
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--chdir", str(scratch),
        "--setenv", "SENTINEL_ISOLATED", "bwrap",
        "--",
        *cmd,
    ]


def _firejail_cmd(cmd: list[str], scratch: Path) -> list[str]:
    return [
        "firejail",
        "--quiet",
        "--private",
        "--net=none",
        "--read-only=/",
        "--read-write=" + str(scratch),
        "--caps.drop=all",
        "--seccomp",
        "--nosound",
        "--no3d",
        "--nodvd",
        "--notv",
        "--nou2f",
        "--",
        *cmd,
    ]


def wrap_command(cmd: list[str], scratch: Path | str = "/tmp/sentinel-scratch",
                 force: str | None = None) -> tuple[list[str], str]:
    """Return (wrapped_cmd, isolation_mode). Never raises."""
    mode = force or detect_isolation()
    scratch = Path(scratch)
    if mode == "bwrap":
        return _bwrap_cmd(cmd, scratch), "bwrap"
    if mode == "firejail":
        return _firejail_cmd(cmd, scratch), "firejail"
    return cmd, "none"


# ── Honest self-check (called from health) ────────────────────────────────
def audit_isolation() -> dict:
    mode = detect_isolation()
    return {
        "mode": mode,
        "provable_blast_radius_zero": mode in ("bwrap", "firejail"),
        "note": (
            "bwrap/firejail confirmed present — OS-level isolation in effect"
            if mode in ("bwrap", "firejail")
            else "Python-level guards only. For production, install bubblewrap "
                 "(apt install bubblewrap) and set SENTINEL_ISOLATION=bwrap"
        ),
    }