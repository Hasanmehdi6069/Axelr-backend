"""Sentinel Agent — unified Blast-Radius-Zero execution engine.

Layered on top of axelr_vultr (unchanged). Sentinel adds:
  - AST-shield + sandboxed fork for arbitrary code
  - Frozen 12-template action registry (no eval, no dynamic import)
  - Atlassian MCP client + keyword-rule proposal engine
  - Human-approval gate for destructive actions
  - WAL-backed autonomous rollback
  - MCP JSON-RPC 2.0 stdio server (Claude/agent-compatible)

Every action, approval, and result lands in one append-only JSONL log,
mirrored to axelr_vultr.auditing so the Insights stream stays canonical.
"""

from __future__ import annotations

from .actions import (
    TEMPLATES,
    dispatch,
    filter_actions,
    get_template,
    list_actions,
)
from .atlassian import (
    RULES,
    ApprovalQueue,
    PendingApproval,
    approve,
    list_pending,
    propose,
    reject,
)
from .engine import (
    AUDIT_PATH,
    MAX_CPU_S,
    MAX_MEMORY_MB,
    NETWORK_ALLOWLIST,
    OUTPUT_CAP_BYTES,
    SANDBOX_ROOT,
    WALL_CLOCK_S,
    ContainmentViolation,
    Engine,
    Snapshot,
    audit,
    minify,
    sha16,
    tail,
)
from .server import handle_line, serve
from .server import main as serve_main

__version__ = "1.0.0"

from .hardening import audit_isolation, detect_isolation, wrap_command
from .wal import MemoryWAL, PersistentWAL, make_wal
from .wal import Snapshot as WalSnapshot

__all__ = [
    "__version__",
    # engine
    "AUDIT_PATH",
    "NETWORK_ALLOWLIST",
    "OUTPUT_CAP_BYTES",
    "SANDBOX_ROOT",
    "WALL_CLOCK_S",
    "MAX_MEMORY_MB",
    "MAX_CPU_S",
    "ContainmentViolation",
    "Engine",
    "Snapshot",
    "minify",
    "sha16",
    "audit",
    "tail",
    # actions
    "TEMPLATES",
    "dispatch",
    "list_actions",
    "filter_actions",
    "get_template",
    # atlassian
    "PendingApproval",
    "ApprovalQueue",
    "approve",
    "reject",
    "list_pending",
    "propose",
    "RULES",
    # server
    "handle_line",
    "serve",
    "serve_main",
    # wal
    "MemoryWAL",
    "PersistentWAL",
    "WalSnapshot",
    "make_wal",
    # hardening
    "audit_isolation",
    "detect_isolation",
    "wrap_command",
]


def health() -> dict:
    """Synchronous package health probe — used by /api/health/ready."""
    from pathlib import Path

    return {
        "version": __version__,
        "sandbox_root": str(SANDBOX_ROOT),
        "sandbox_writable": SANDBOX_ROOT.exists() and os_access_w(SANDBOX_ROOT),
        "audit_path": str(AUDIT_PATH),
        "audit_writable": AUDIT_PATH.parent.exists() and os_access_w(AUDIT_PATH.parent),
        "templates": len(TEMPLATES),
        "network_allowlist": sorted(NETWORK_ALLOWLIST),
        "limits": {"wall_s": WALL_CLOCK_S, "memory_mb": MAX_MEMORY_MB, "cpu_s": MAX_CPU_S},
    }


def os_access_w(p) -> bool:
    import os

    return os.access(str(p), os.W_OK)
