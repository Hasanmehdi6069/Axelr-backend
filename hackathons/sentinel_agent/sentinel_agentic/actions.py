"""Frozen action registry — 12 templates. No eval, no dynamic import.

Categories:
  safe         — read-only (sandbox.read_file, vultr.list_instances)
  reversible   — WAL-backed rollback available
  destructive  — requires approval_token; NO rollback

The Vultr templates import axelr_vultr directly when available (same process,
no HTTP hop). Set AXELR_VULTR_BASE to force the FastAPI route instead.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.request import Request, urlopen

from .engine import (
    ContainmentViolation, Engine, audit, minify, sha16,
)

Params = dict[str, Any]
Receipt = dict[str, Any]
AsyncFn = Callable[[Params, Engine], Awaitable[dict[str, Any]]]

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Atlassian transport (urllib, stdlib only) ──────────────────────────────
async def _http_json(method: str, url: str,
                     body: dict | None = None,
                     headers: dict | None = None) -> dict:
    def _do() -> dict:
        req = Request(url, method=method, headers=headers or {})
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("content-type", "application/json")
        with urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    return await asyncio.to_thread(_do)


def _atl_headers() -> dict:
    return {"authorization": f"Bearer {os.getenv('ATLASSIAN_TOKEN', '')}"}


def _atl_base() -> str:
    return os.getenv("ATLASSIAN_BASE", "https://api.atlassian.com")


async def _atlassian(method: str, path: str, body: dict | None = None) -> dict:
    if os.getenv("SENTINEL_DEMO") == "1":
        rid = f"DEMO-{(abs(hash((method, path, str(body)))) % 9000) + 1000}"
        return {"id": rid, "key": rid, "title": (body or {}).get("title", "demo")}
    return await _http_json(method, _atl_base() + path, body, _atl_headers())


# ── Vultr transport (direct import preferred) ──────────────────────────────
async def _vultr_http(method: str, path: str, body: dict | None = None) -> dict:
    base = os.environ["AXELR_VULTR_BASE"]
    return await _http_json(method, base + path, body,
                            {"authorization": f"Bearer {os.getenv('AXELR_TOKEN', '')}"})


async def _vultr_provision(p: Params) -> Receipt:
    if os.getenv("SENTINEL_DEMO") == "1":
        return {"instance_id": f"dry-{sha16(str(p))[:8]}",
                "blueprint": p.get("blueprint")}
    if os.getenv("AXELR_VULTR_BASE"):
        r = await _vultr_http("POST", "/vultr/instances", {
            "blueprint": p.get("blueprint", "web_node"),
            "region": p.get("region"), "dry_run": False})
        return {"instance_id": (r.get("instance") or {}).get("id"),
                "blueprint": p.get("blueprint")}
    from axelr_vultr.client import get_client
    from axelr_vultr.core import new_request_id
    from axelr_vultr.lifecycle import register_instance
    from axelr_vultr.templates import resolve_blueprint
    bp = resolve_blueprint(p.get("blueprint", "web_node"), region=p.get("region"))
    idem = new_request_id("sentinel")
    inst = await get_client().create_instance(
        region=bp.region_default, plan=bp.plan, os_id=bp.os_id,
        label=p.get("label") or f"axelr-{bp.name}-{idem[-6:]}",
        startup_script=bp.startup_script or None,
        tags=list(bp.tags), idempotency_key=idem,
    )
    iid = inst.get("id", "")
    if iid:
        await register_instance(iid, {"user_id": "sentinel", "blueprint": bp.name})
    return {"instance_id": iid, "blueprint": bp.name,
            "region": bp.region_default, "plan": bp.plan}


async def _vultr_destroy(p: Params) -> Receipt:
    if os.getenv("SENTINEL_DEMO") == "1":
        return {"instance_id": p.get("instance_id"), "destroyed": _now()}
    if os.getenv("AXELR_VULTR_BASE"):
        await _vultr_http("DELETE",
            f"/vultr/instances/{p['instance_id']}?confirm=true")
    else:
        from axelr_vultr.client import get_client
        from axelr_vultr.lifecycle import unregister_instance
        await get_client().delete_instance(p["instance_id"])
        await unregister_instance(p["instance_id"])
    return {"instance_id": p.get("instance_id"), "destroyed": _now()}


async def _vultr_reboot(p: Params) -> Receipt:
    if os.getenv("SENTINEL_DEMO") == "1":
        return {"instance_id": p.get("instance_id"), "action": "reboot"}
    if os.getenv("AXELR_VULTR_BASE"):
        await _vultr_http("POST", f"/vultr/instances/{p['instance_id']}/reboot")
    else:
        from axelr_vultr.client import get_client
        await get_client().reboot_instance(p["instance_id"])
    return {"instance_id": p.get("instance_id"), "action": "reboot"}


# ── Sandbox async helpers ──────────────────────────────────────────────────
async def _sandbox_write(p: Params, engine: Engine) -> dict:
    snap = await engine.wal_snapshot(p["path"])
    from pathlib import Path as _P
    await asyncio.to_thread(
        lambda: _P(snap["path"]).write_text(p["content"], encoding="utf-8"))
    return {"receipt": {"path": p["path"], "existed": snap["existed"]}}


async def _sandbox_run(p: Params, engine: Engine) -> dict:
    engine.validate(p["script"])                       # Phase 1
    r = await engine.run(p["script"])                  # Phase 2
    m = minify(r["stdout"], r["stderr"])               # Phase 3
    if not r["ok"]:
        err = RuntimeError(
            f"sandbox_exit_{r['exit_code']}"
            + ("_timeout" if r["timed_out"] else ""))
        err.code = "SANDBOX_FAILURE"
        err.output = m
        raise err
    return {"receipt": {"exit_code": r["exit_code"],
                        "timed_out": r["timed_out"],
                        "duration_ms": r["duration_ms"]},
            "output": m}


async def _sandbox_read(p: Params, engine: Engine) -> dict:
    from pathlib import Path as _P
    abs_path = engine.assert_path(p["path"])
    text = await asyncio.to_thread(
        lambda: _P(abs_path).read_text(encoding="utf-8"))
    return {"receipt": {"path": p["path"], "bytes": len(text)},
            "output": {"text": text[:4096],
                       "truncated": len(text) > 4096,
                       "original_bytes": len(text)}}


# ── 12 frozen templates ────────────────────────────────────────────────────
TEMPLATES: dict[str, dict[str, Any]] = {
    # ── Jira (4) ──────────────────────────────────────────────────────
    "jira.create_issue": {
        "category": "reversible",
        "description": "Create a Jira issue (Jira REST v3).",
        "validate": lambda p: bool(p.get("project_key") and p.get("summary")),
        "dryRun": lambda p: {"would": "create_issue",
                             "project": p["project_key"],
                             "summary": p["summary"]},
        "execute": lambda p, e: _atlassian("POST", "/rest/api/3/issue", {
            "fields": {"project": {"key": p["project_key"]},
                       "summary": p["summary"],
                       "issuetype": {"name": p.get("type", "Task")},
                       "description": p.get("description", "")}}),
        "rollback": lambda r: _atlassian("DELETE",
            f"/rest/api/3/issue/{r.get('key') or r.get('id')}"),
    },
    "jira.transition_issue": {
        "category": "reversible",
        "description": "Transition a Jira issue status.",
        "validate": lambda p: bool(p.get("issue_key") and p.get("transition_id")),
        "dryRun": lambda p: {"would": "transition",
                             "issue": p["issue_key"],
                             "to": p["transition_id"]},
        "execute": lambda p, e: _atlassian("POST",
            f"/rest/api/3/issue/{p['issue_key']}/transitions",
            {"transition": {"id": p["transition_id"]}}),
        "rollback": lambda r: _atlassian("POST",
            f"/rest/api/3/issue/{r['issue_key']}/transitions",
            {"transition": {"id": r.get("prev_status", "")}}),
    },
    "jira.add_comment": {
        "category": "reversible",
        "description": "Comment on a Jira issue.",
        "validate": lambda p: bool(p.get("issue_key") and p.get("body")),
        "dryRun": lambda p: {"would": "comment",
                             "issue": p["issue_key"],
                             "bytes": len(p["body"])},
        "execute": lambda p, e: _atlassian("POST",
            f"/rest/api/3/issue/{p['issue_key']}/comment",
            {"body": {"type": "doc", "version": 1,
                      "content": [{"type": "paragraph",
                                   "content": [{"type": "text",
                                                "text": p["body"]}]}]}}),
        "rollback": lambda r: _atlassian("DELETE",
            f"/rest/api/3/issue/{r['issue_key']}/comment/{r.get('id', '')}"),
    },
    "jira.delete_issue": {
        "category": "destructive",
        "description": "Permanently delete a Jira issue. No rollback.",
        "validate": lambda p: bool(p.get("issue_key")),
        "dryRun": lambda p: {"would": "delete_issue",
                             "issue": p["issue_key"], "irreversible": True},
        "execute": lambda p, e: _atlassian("DELETE",
            f"/rest/api/3/issue/{p['issue_key']}"),
        "rollback": None,
    },
    # ── Confluence (2) ────────────────────────────────────────────────
    "confluence.create_page": {
        "category": "reversible",
        "description": "Create a Confluence page (storage format body).",
        "validate": lambda p: bool(p.get("space_id") and p.get("title")
                                   and p.get("body")),
        "dryRun": lambda p: {"would": "create_page",
                             "space": p["space_id"], "title": p["title"]},
        "execute": lambda p, e: _atlassian("POST", "/wiki/api/v2/pages", {
            "spaceId": p["space_id"], "status": "current",
            "title": p["title"],
            "body": {"representation": "storage", "value": p["body"]}}),
        "rollback": lambda r: _atlassian("DELETE",
            f"/wiki/api/v2/pages/{r['id']}"),
    },
    "confluence.add_label": {
        "category": "reversible",
        "description": "Tag a Confluence page.",
        "validate": lambda p: bool(p.get("page_id") and p.get("label")),
        "dryRun": lambda p: {"would": "add_label",
                             "page": p["page_id"], "label": p["label"]},
        "execute": lambda p, e: _atlassian("POST",
            f"/wiki/api/v2/pages/{p['page_id']}/labels",
            [{"name": p["label"]}]),
        "rollback": lambda r: _atlassian("DELETE",
            f"/wiki/api/v2/pages/{r['page_id']}/labels/{r['label']}"),
    },
    # ── Sandbox (3) ───────────────────────────────────────────────────
    "sandbox.write_file": {
        "category": "safe",
        "description": "Write a file inside /scratch only (WAL-protected).",
        "validate": lambda p: bool(p.get("path")) and isinstance(p.get("content"), str),
        "dryRun": lambda p: {"would": "write",
                             "path": p["path"], "bytes": len(p["content"])},
        "execute": _sandbox_write,
        "rollback": lambda r: {"restored": "via_wal"},
    },
    "sandbox.run_script": {
        "category": "reversible",
        "description": "Run Python in the isolated child (30s/256MB/allowlist).",
        "validate": lambda p: isinstance(p.get("script"), str),
        "dryRun": lambda p: {"would": "run",
                             "script_hash": sha16(p["script"])},
        "execute": _sandbox_run,
        "rollback": lambda r: {"restored": "no_fs_touch"},
    },
    "sandbox.read_file": {
        "category": "safe",
        "description": "Read a file inside /scratch only.",
        "validate": lambda p: bool(p.get("path")),
        "dryRun": lambda p: {"would": "read", "path": p["path"]},
        "execute": _sandbox_read,
        "rollback": lambda r: {"restored": "read_only"},
    },
    # ── Vultr (3) ─────────────────────────────────────────────────────
    "vultr.provision_instance": {
        "category": "reversible",
        "description": "Provision a Vultr instance via axelr_vultr.",
        "validate": lambda p: bool(p.get("blueprint")),
        "dryRun": lambda p: {"would": "provision",
                             "blueprint": p["blueprint"],
                             "region": p.get("region")},
        "execute": lambda p, e: _vultr_provision(p),
        "rollback": lambda r: _vultr_destroy({"instance_id": r["instance_id"]}),
    },
    "vultr.destroy_instance": {
        "category": "destructive",
        "description": "Destroy a Vultr instance. Requires approval_token.",
        "validate": lambda p: bool(p.get("instance_id")),
        "dryRun": lambda p: {"would": "destroy",
                             "instance_id": p["instance_id"],
                             "irreversible": True},
        "execute": lambda p, e: _vultr_destroy(p),
        "rollback": None,
    },
    "vultr.reboot_instance": {
        "category": "reversible",
        "description": "Reboot a Vultr instance (graceful).",
        "validate": lambda p: bool(p.get("instance_id")),
        "dryRun": lambda p: {"would": "reboot",
                             "instance_id": p["instance_id"]},
        "execute": lambda p, e: _vultr_reboot(p),
        "rollback": lambda r: _vultr_reboot({"instance_id": r["instance_id"]}),
    },
}

for _spec in TEMPLATES.values():
    _spec.setdefault("requires_approval", _spec["category"] == "destructive")


# ── Public API ────────────────────────────────────────────────────────────
def list_actions() -> list[dict[str, Any]]:
    return [{"name": name, "category": spec["category"],
             "description": spec["description"],
             "requiresApproval": spec["requires_approval"]}
            for name, spec in TEMPLATES.items()]


def filter_actions(category: str | None = None,
                   substring: str | None = None) -> list[str]:
    names = list(TEMPLATES)
    if category:
        names = [n for n in names if TEMPLATES[n]["category"] == category]
    if substring:
        names = [n for n in names if substring.lower() in n.lower()]
    return names


def get_template(name: str) -> dict[str, Any] | None:
    return TEMPLATES.get(name)


async def dispatch(engine: Engine, args: Params) -> dict[str, Any]:
    """Execute a pre-registered template with WAL-backed auto-rollback."""
    action = args.get("action", "")
    params = args.get("params", {}) or {}
    dry_run = args.get("dryRun", True)
    approval = args.get("approval_token")
    spec = TEMPLATES.get(action)
    if spec is None:
        return {"ok": False,
                "error": {"code": "UNKNOWN_ACTION", "action": action}}
    try:
        if not spec["validate"](params):
            return {"ok": False,
                    "error": {"code": "INVALID_PARAMS", "action": action}}
    except Exception as e:
        return {"ok": False,
                "error": {"code": "INVALID_PARAMS", "message": str(e)}}

    entry = {"action": action, "category": spec["category"],
             "dry_run": dry_run, "params": params,
             "approval": "***" if approval else None}
    try:
        if dry_run:
            preview = spec["dryRun"](params)
            await audit({**entry, "phase": "dry_run", "preview": preview})
            return {"ok": True, "dryRun": True, "preview": preview}

        if spec["requires_approval"]:
            if not approval:
                await audit({**entry, "phase": "pending_approval"})
                return {"ok": False,
                        "error": {"code": "APPROVAL_REQUIRED", "action": action}}
            from .atlassian import _verify
            claims = _verify(str(approval))
            if not claims:
                await audit({**entry, "phase": "approval_rejected",
                             "reason": "invalid_or_expired_token"})
                return {"ok": False,
                        "error": {"code": "APPROVAL_INVALID", "action": action}}
            params["proposal_id"] = claims["pid"]

        import time as _time
        t0 = _time.monotonic()
        result = await spec["execute"](params, engine)
        latency_ms = round((_time.monotonic() - t0) * 1000, 2)
        await audit({**entry, "phase": "executed", "latency_ms": latency_ms,
                     "receipt": result.get("receipt"),
                     "output": result.get("output")})
        return {"ok": True, "dryRun": False, "result": result,
                "latencyMs": latency_ms}
    except ContainmentViolation as e:
        await audit({**entry, "phase": "containment_violation",
                     "error": str(e), "why": e.why, "detail": e.detail})
        rb = await engine.rollback(workflow_id=args.get("corr_id"))
        return {"ok": False,
                "error": {"code": e.code, "why": e.why, "detail": e.detail,
                          "rolledBack": True,
                          "restoredFiles": rb["restored"]}}
    except Exception as e:
        await audit({**entry, "phase": "error", "error": str(e),
                     "code": getattr(e, "code", "EXECUTION_ERROR")})
        rb = await engine.rollback(workflow_id=args.get("corr_id"))
        return {"ok": False,
                "error": {"code": getattr(e, "code", "EXECUTION_ERROR"),
                          "message": str(e),
                          "rolledBack": True,
                          "restoredFiles": rb["restored"],
                          "output": getattr(e, "output", None)}}