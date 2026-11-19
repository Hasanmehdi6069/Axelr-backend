"""Thin CLI wrapper so the Switch agent can call Appwrite via shell.

The Switch agent runs `python backend/appwrite_backend.py get-incident <id>`
and reads JSON from stdout. This keeps the agent harness simple and lets
the same engine be reused by Sentinel, PayPal Agent, etc.
"""
from __future__ import annotations

import asyncio
import json
import sys

from axelr_insights_engine import get_engine, init


async def main() -> None:
    await init()
    db = get_engine()
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""

    if cmd == "get-incident" and len(sys.argv) > 2:
        row = await db.read("audit_events", sys.argv[2])
        print(json.dumps(row, default=str))
    elif cmd == "update-incident" and len(sys.argv) > 3:
        row_id, kv = sys.argv[2], sys.argv[3]
        key, val = kv.split("=", 1)
        await db.update("audit_events", row_id, {key: val})
        print(json.dumps({"ok": True}))
    else:
        print(json.dumps({"error": "usage: get-incident <id> | update-incident <id> key=value"}))
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
