"""Atlassian MCP bridge — poll, propose, approve, execute.

Three concerns, one module:
  1. RULES            — keyword -> proposal mapping (extensible)
  2. propose()        — poll Confluence via MCP REST, generate proposals
  3. ApprovalQueue    — in-memory queue of pending destructive actions

Webhook signature verification is included for future Atlassian event
subscriptions (X-Atlassian-Webhook-Signature, HMAC-SHA256).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.request import Request, urlopen

from .engine import Engine, audit


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

_SECRET = os.getenv("SENTINEL_APPROVAL_SECRET", "dev-secret").encode()

_recently_approved: dict[str, float] = {}
_APPROVAL_TTL_S = 300.0


def _make_approval_token(proposal_id: str, action: str) -> str:
    action_hash = hashlib.sha256(action.encode()).hexdigest()[:8]
    return f"queue|{proposal_id}|{action_hash}"


def verify_approval_token(token: str, expected_action: str) -> bool:
    if not token.startswith("queue|"):
        return False
    parts = token.split("|")
    if len(parts) != 3:
        return False
    _, pid, action_hash = parts
    ts = _recently_approved.get(pid)
    if ts is None or (time.time() - ts) > _APPROVAL_TTL_S:
        _recently_approved.pop(pid, None)
        return False
    expected = hashlib.sha256(expected_action.encode()).hexdigest()[:8]
    return action_hash == expected


def _sign(proposal_id: str, approver: str) -> str:
    payload = json.dumps({"pid": proposal_id, "by": approver,
                         "exp": int(time.time()) + 3600},
                        separators=(",", ":")).encode()
    sig = hmac.new(_SECRET, payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + b"." + sig).decode()

def _verify(token: str) -> dict | None:
    try:
        blob = base64.urlsafe_b64decode(token.encode())
        payload, sig = blob.rsplit(b".", 1)
        if not hmac.compare_digest(
            hmac.new(_SECRET, payload, hashlib.sha256).digest(), sig):
            return None
        data = json.loads(payload)
        return None if data["exp"] < time.time() else data
    except Exception:
        return None


# ── Rules (keyword -> proposal) ────────────────────────────────────────────
RuleFn = "callable"

DEFAULT_RULES: list[tuple[re.Pattern, "RuleFn"]] = [
    (re.compile(r"\bblocker\b", re.IGNORECASE), lambda page: {
        "action": "jira.create_issue", "category": "reversible",
        "params": {"project_key": os.getenv("JIRA_PROJECT", "ENG"),
                   "summary": f"Blocker in: {page['title']}",
                   "description": f"Page {page['id']} mentions blocker. "
                                  f"Auto-proposed by Sentinel."},
        "reason": f'Page "{page["title"]}" matches /blocker/',
    }),
    (re.compile(r"action item:", re.IGNORECASE), lambda page: {
        "action": "jira.create_issue", "category": "reversible",
        "params": {"project_key": os.getenv("JIRA_PROJECT", "ENG"),
                   "summary": f"Action from: {page['title']}",
                   "description": f"Action item extracted from {page['id']}."},
        "reason": f'Page "{page["title"]}" matches /action item:/',
    }),
    (re.compile(r"tracking\s+blocker", re.IGNORECASE), lambda page: {
        "action": "confluence.add_label", "category": "reversible",
        "params": {"page_id": page["id"], "label": "blocker"},
        "reason": f'Page "{page["title"]}" flagged for label sync',
    }),
]

def _load_rules() -> list[tuple[re.Pattern, "RuleFn"]]:
    """Allow rule overrides via SENTINEL_RULES JSON env var."""
    raw = os.getenv("SENTINEL_RULES")
    if not raw:
        return DEFAULT_RULES
    try:
        overrides = json.loads(raw)
        rules: list = []
        for spec in overrides:
            pat = re.compile(spec["pattern"], re.IGNORECASE)
            action = spec["action"]
            params = spec.get("params", {})
            reason = spec.get("reason", "custom_rule")
            rules.append((pat, lambda page, a=action, p=params, r=reason: {
                "action": a, "category": "reversible",
                "params": {**p, "page_id": page["id"],
                           "summary": p.get("summary", page["title"])},
                "reason": r,
            }))
        return rules or DEFAULT_RULES
    except Exception:
        return DEFAULT_RULES


RULES = _load_rules()


# ── Confluence MCP client ──────────────────────────────────────────────────
async def _list_pages(max_pages: int) -> list[dict[str, Any]]:
    if os.getenv("SENTINEL_DEMO") == "1":
        return [
            {"id": "p-101", "title": "Q3 Launch Blocker",
             "body": "There is a blocker on the payments migration."},
            {"id": "p-102", "title": "Daily Standup 12 Nov",
             "body": "Action item: update runbook. Owner: platform team."},
            {"id": "p-103", "title": "Architecture Notes",
             "body": "Nothing urgent. Just design musing."},
        ][:max_pages]

    base = os.getenv("ATLASSIAN_BASE", "https://api.atlassian.com")
    token = os.getenv("ATLASSIAN_TOKEN", "")
    space = os.getenv("CONFLUENCE_SPACE_ID", "")
    if not token or not space:
        raise RuntimeError("atlassian_not_configured")

    url = f"{base}/wiki/api/v2/spaces/{space}/pages?limit={max_pages}"

    def _do() -> dict:
        req = Request(url, headers={"authorization": f"Bearer {token}"})
        with urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    data = await asyncio.to_thread(_do)
    return [{"id": str(p["id"]), "title": p["title"],
             "body": (p.get("body") or {}).get("storage", {}).get("value", "")}
            for p in data.get("results", [])]


# ── Proposal generator ────────────────────────────────────────────────────
async def propose(engine: Engine,
                  args: dict[str, Any] | None = None) -> dict[str, Any]:
    args = args or {}
    max_pages = min(int(args.get("max_pages", 5)), 20)
    pages = await _list_pages(max_pages)
    proposals: list[dict[str, Any]] = []
    for page in pages:
        for pat, rule in RULES:
            if pat.search(page["body"]) or pat.search(page["title"]):
                p = rule(page)
                p["page_id"] = page["id"]
                p["page_title"] = page["title"]
                p["requiresApproval"] = p.get("category") == "destructive"
                p["proposal_id"] = f"prop-{hash((page['id'], p['action'])) % 100000}"
                proposals.append(p)
    await audit({"kind": "proposals_generated",
                 "pages": len(pages), "proposals": len(proposals)})
    return {"pages": len(pages), "proposals": proposals}


# ── Approval queue (in-memory) ────────────────────────────────────────────
@dataclass
class PendingApproval:
    proposal_id: str
    action: str
    params: dict[str, Any]
    category: str
    reason: str
    created_at: str = field(default_factory=_now)
    approved_by: str | None = None


class ApprovalQueue:
    def __init__(self) -> None:
        self._pending: dict[str, PendingApproval] = {}
        self._lock = asyncio.Lock()

    async def add(self, p: PendingApproval) -> str:
        async with self._lock:
            self._pending[p.proposal_id] = p
        return p.proposal_id

    async def list(self) -> list[PendingApproval]:
        async with self._lock:
            return list(self._pending.values())

    async def approve(self, proposal_id: str, approver: str = "human") -> PendingApproval:
        async with self._lock:
            p = self._pending.pop(proposal_id, None)
            if p is None:
                raise KeyError(f"proposal_not_found:{proposal_id}")
            p.approved_by = approver
        await audit({"kind": "approval_granted",
                     "proposal_id": proposal_id, "approver": approver})
        return p

    async def reject(self, proposal_id: str, reason: str = "") -> None:
        async with self._lock:
            self._pending.pop(proposal_id, None)
        await audit({"kind": "approval_rejected",
                     "proposal_id": proposal_id, "reason": reason})


_QUEUE = ApprovalQueue()


async def list_pending() -> list[dict[str, Any]]:
    items = await _QUEUE.list()
    return [{"proposal_id": p.proposal_id, "action": p.action,
             "category": p.category, "reason": p.reason,
             "created_at": p.created_at}
            for p in items]


_SHARED: Engine | None = None

def _shared_engine() -> Engine:
    global _SHARED
    if _SHARED is None:
        _SHARED = Engine()
    return _SHARED


async def approve(proposal_id: str, approver: str = "human",
                  engine: Engine | None = None) -> dict[str, Any]:
    from .actions import dispatch
    try:
        p = await _QUEUE.approve(proposal_id, approver)
    except KeyError as e:
        return {"ok": False, "error": {"code": "NOT_FOUND", "message": str(e)}}
    _recently_approved[p.proposal_id] = time.time()
    eng = engine or _shared_engine()
    p.params["proposal_id"] = p.proposal_id
    token = _make_approval_token(p.proposal_id, p.action)
    r = await dispatch(eng, {"action": p.action, "params": p.params,
                             "dryRun": False,
                             "approval_token": token})
    return {"ok": r.get("ok", False), "result": r, "approver": approver}


async def reject(proposal_id: str, reason: str = "") -> dict[str, Any]:
    try:
        await _QUEUE.reject(proposal_id, reason)
        return {"ok": True, "proposal_id": proposal_id, "rejected": True}
    except Exception as e:
        return {"ok": False, "error": {"code": "REJECT_FAILED", "message": str(e)}}


# ── Webhook signature (for future Atlassian event subscriptions) ──────────
def verify_webhook(body: bytes, signature_hex: str,
                   secret: str | None = None) -> bool:
    """Verify X-Atlassian-Webhook-Signature (HMAC-SHA256)."""
    secret = secret or os.getenv("ATLASSIAN_WEBHOOK_SECRET", "")
    if not secret or not signature_hex:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_hex)
