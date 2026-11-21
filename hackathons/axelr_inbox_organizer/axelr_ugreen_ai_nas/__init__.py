"""AXELR UGREEN NAS Skill — Inbox Organizer.

UGOS Skill manifest, public API, and optional aiohttp route helpers.
Runs entirely on-device: Apex Router local tier (Ollama + Gemma) only.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("axelr.ugreen_ai_nas")

__version__ = "2.0.0"

# ─── UGOS Skill manifest ────────────────────────────────────────────────

SKILL_MANIFEST: dict[str, Any] = {
    "id": "axelr.inbox-organizer",
    "name": "Inbox Organizer",
    "version": __version__,
    "vendor": "AXELR",
    "track": "automate-your-workflow",
    "description": (
        "Monitors a NAS inbox folder, extracts document metadata with Apex "
        "Router's local Ollama + Gemma tier, and files invoices, receipts, "
        "emails, and scans into a Year/Month/Category tree with a 50-entry "
        "undo log and a daily 'What's new' summary."
    ),
    "runtime": {
        "entry": "axelr.ugreen_ai_nas:init_module",
        "language": "python",
        "python": ">=3.10",
        "client_side": True,
        "offline": True,
        "requires_network_for_inference": False,
    },
    "permissions": [
        {"scope": "filesystem.watch", "path": "${inbox}", "reason": "detect new documents"},
        {"scope": "filesystem.read", "path": "${inbox}", "reason": "extract text for metadata"},
        {"scope": "filesystem.write", "path": "${library}", "reason": "file documents into tree"},
        {"scope": "filesystem.write", "path": "${state_dir}", "reason": "persist undo log and dedupe state"},
        {"scope": "filesystem.write", "path": "${summary_dir}", "reason": "write daily summaries"},
        {"scope": "local_inference", "endpoint": "http://127.0.0.1:11434", "reason": "Apex Router local tier only"},
    ],
    "triggers": [
        {"type": "file.created", "path": "${inbox}", "recursive": True},
        {"type": "file.modified", "path": "${inbox}", "recursive": True},
        {"type": "schedule.daily", "time": "18:00", "action": "render_summary"},
    ],
    "config": {
        "inbox": "inbox",
        "library": "library",
        "state_dir": "_skill",
        "summary_dir": "_summary",
        "apex_endpoint": "http://127.0.0.1:11434",
        "apex_model": "gemma3:4b",
        "debounce_s": 2.0,
        "undo_log_size": 50,
    },
    "network": {
        "cloud_calls": False,
        "allowed_hosts": ["127.0.0.1", "localhost", "::1"],
    },
}

PERMISSIONS: tuple[str, ...] = tuple(p["scope"] for p in SKILL_MANIFEST["permissions"])


def manifest() -> dict[str, Any]:
    """Return a deep-ish copy of the skill manifest for the UGOS host."""
    import copy

    return copy.deepcopy(SKILL_MANIFEST)


# ─── Public API ─────────────────────────────────────────────────────────

from .core import (  # noqa: E402
    DocumentMetadata,
    DocumentSkill,
    ProcessingResult,
    Settings,
    UndoEntry,
    compute_hash,
    extract_text,
    get_skill,
    health,
    init,
    is_healthy,
    shutdown,
)

__all__ = [
    "SKILL_MANIFEST",
    "PERMISSIONS",
    "manifest",
    "DocumentMetadata",
    "DocumentSkill",
    "ProcessingResult",
    "Settings",
    "UndoEntry",
    "compute_hash",
    "extract_text",
    "get_skill",
    "health",
    "init",
    "init_module",
    "is_healthy",
    "register_routes",
    "shutdown",
]


# ─── aiohttp wiring (optional — the Skill SDK may call this directly) ───


async def init_module(app: Any, root: Optional[str] = None) -> None:
    """UGOS entrypoint: initialize the skill and attach cleanup."""
    nas_root = root or os.getenv("NAS_ROOT") or os.getenv("SKILL_ROOT", "")
    if not nas_root:
        raise RuntimeError("NAS_ROOT or SKILL_ROOT env required")
    Path(nas_root).mkdir(parents=True, exist_ok=True)
    await init(nas_root)
    if hasattr(app, "on_cleanup"):
        app.on_cleanup.append(_on_cleanup)
    logger.info("skill_module_initialized root=%s", nas_root)


async def _on_cleanup(_app: Any) -> None:
    await shutdown()


def register_routes(app: Any, prefix: str = "/skill/inbox-organizer") -> None:
    """Register read-only summary + health routes on an aiohttp app."""
    try:
        from aiohttp import web
    except ImportError:
        logger.warning("aiohttp missing — routes not registered")
        return

    async def _today(request: Any) -> Any:
        skill = get_skill()
        if skill is None:
            return web.json_response({"ok": False, "reason": "not_initialized"}, status=503)
        return web.Response(text=await skill.whats_new(), content_type="text/markdown")

    async def _day(request: Any) -> Any:
        skill = get_skill()
        if skill is None:
            return web.json_response({"ok": False, "reason": "not_initialized"}, status=503)
        raw = request.match_info.get("day", "")
        try:
            import time as _t

            when = _t.mktime(_t.strptime(raw, "%Y-%m-%d"))
        except ValueError:
            return web.json_response({"error": "invalid day, use YYYY-MM-DD"}, status=400)
        return web.Response(text=await skill.whats_new(when), content_type="text/markdown")

    async def _health(request: Any) -> Any:
        return web.json_response(health())

    app.router.add_get(f"{prefix}/summary/today", _today)
    app.router.add_get(f"{prefix}/summary/{{day}}", _day)
    app.router.add_get(f"{prefix}/health", _health)
    logger.info("routes_registered prefix=%s", prefix)
