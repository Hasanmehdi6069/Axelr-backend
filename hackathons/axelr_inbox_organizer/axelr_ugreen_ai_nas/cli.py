"""Single-command driver for the AXELR Inbox Organizer engine.

Makes the reference engine feel like one unified skill: init, file, watch,
summary, undo, health, verify. Every subcommand is self-contained.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

from .apex_client import ApexRouterClient, CloudCallForbidden
from .core import init, shutdown


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
    )


def _root(args: argparse.Namespace) -> Path:
    raw = args.root or os.getenv("NAS_ROOT") or os.getenv("SKILL_ROOT")
    if not raw:
        raise SystemExit("error: --root, NAS_ROOT, or SKILL_ROOT required")
    return Path(raw).expanduser().resolve()


async def _init(args: argparse.Namespace) -> int:
    skill = await init(_root(args))
    print(json.dumps(skill.health(), indent=2))
    await shutdown()
    return 0


async def _file(args: argparse.Namespace) -> int:
    skill = await init(_root(args))
    result = await skill.process_now(args.path)
    await shutdown()
    if result is None:
        print(json.dumps({"ok": False, "reason": "skipped"}))
        return 0
    print(json.dumps({
        "ok": result.error is None,
        "source": result.source_name,
        "destination": result.destination,
        "category": result.metadata.category if result.metadata else None,
        "document_type": result.metadata.document_type if result.metadata else None,
        "elapsed_ms": round(result.elapsed_ms, 1),
        "error": result.error,
    }, indent=2))
    return 0 if result.error is None else 1


async def _watch(args: argparse.Namespace) -> int:
    skill = await init(_root(args))
    print(f"watching {skill.inbox} → {skill.library} (Ctrl-C stops)")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await shutdown()
    return 0


async def _summary(args: argparse.Namespace) -> int:
    skill = await init(_root(args))
    when: Optional[float] = None
    if args.date:
        try:
            when = time.mktime(time.strptime(args.date, "%Y-%m-%d"))
        except ValueError:
            await shutdown()
            print(f"error: --date must be YYYY-MM-DD, got {args.date!r}", file=sys.stderr)
            return 2
    print(await skill.whats_new(when))
    await shutdown()
    return 0


async def _undo(args: argparse.Namespace) -> int:
    skill = await init(_root(args))
    entry = await skill.undo_last()
    await shutdown()
    if entry is None:
        print(json.dumps({"ok": False, "reason": "nothing_to_undo"}))
        return 0
    print(json.dumps({
        "ok": True, "op_id": entry.op_id,
        "restored_from": entry.dst, "restored_to": entry.src,
    }, indent=2))
    return 0


async def _health(args: argparse.Namespace) -> int:
    skill = await init(_root(args))
    snapshot = skill.health()
    snapshot["apex_router"] = await skill.client.health()
    await shutdown()
    print(json.dumps(snapshot, indent=2))
    return 0 if snapshot.get("ok") else 1


async def _verify(args: argparse.Namespace) -> int:
    """Prove the zero-cloud claim: public endpoints must be rejected."""
    endpoint = args.endpoint or os.getenv("APEX_ROUTER_ENDPOINT", "http://127.0.0.1:11434")
    model = args.model or os.getenv("APEX_ROUTER_MODEL", "gemma3:4b")
    try:
        client = ApexRouterClient(endpoint=endpoint, model=model)
    except CloudCallForbidden as exc:
        print(json.dumps({"ok": False, "reason": "endpoint_not_local", "detail": str(exc)}))
        return 2
    try:
        ApexRouterClient(endpoint="http://8.8.8.8:11434", model=model)
        print(json.dumps({"ok": False, "reason": "guard_failed",
                          "detail": "public host was accepted"}))
        await client.close()
        return 3
    except CloudCallForbidden:
        pass
    probe = await client.health()
    await client.close()
    print(json.dumps({
        "ok": probe.get("ok", False),
        "endpoint": endpoint, "model": model,
        "guard": "rejects_public_host", "apex_probe": probe,
    }, indent=2))
    return 0 if probe.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="axelr", description="AXELR Inbox Organizer — unified engine CLI")
    p.add_argument("--root", help="NAS root (overrides NAS_ROOT / SKILL_ROOT)")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="initialize folders").set_defaults(fn=_init)
    f = sub.add_parser("file", help="file a single document now")
    f.add_argument("path")
    f.set_defaults(fn=_file)
    sub.add_parser("watch", help="run the watcher in foreground").set_defaults(fn=_watch)
    s = sub.add_parser("summary", help="print daily summary")
    s.add_argument("--date", help="YYYY-MM-DD (default: today)")
    s.set_defaults(fn=_summary)
    sub.add_parser("undo", help="undo the last move").set_defaults(fn=_undo)
    sub.add_parser("health", help="print health JSON").set_defaults(fn=_health)
    v = sub.add_parser("verify", help="prove Apex Router is local")
    v.add_argument("--endpoint")
    v.add_argument("--model")
    v.set_defaults(fn=_verify)
    return p


def main() -> int:
    args = build_parser().parse_args()
    _setup_logging(args.log_level)
    try:
        return asyncio.run(args.fn(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())