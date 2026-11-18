"""Axelr Ops Room — incident triage with a living runbook.

Unique twist: the agent writes a persistent runbook to Appwrite for every
incident. Other rooms can query the runbook, so incident memory is
durable and cross-room — not lost in chat scroll.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from switch_client import SwitchAgentClient
from axelr_insights_engine import init, get_engine

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("workflow")

ROOM_ID = os.getenv("SWITCH_ROOM_ID", "")


async def main() -> None:
    await init()
    db = get_engine()

    switch = SwitchAgentClient()
    if not switch._agent_id:
        await switch.register("axelr-triage")

    pending: dict[str, dict[str, Any]] = {}

    async for frame in switch.events(rooms=[ROOM_ID]):
        event = frame.get("data", {})
        payload = event.get("payload", {})
        text = (payload.get("text") or "").strip()
        author = payload.get("author", {}).get("name", "unknown")
        room_id = event.get("room_id", ROOM_ID)

        if not text:
            continue
        log.info("message author=%s text=%.60s", author, text)

        # ── New incident ────────────────────────────────────────────
        if text.lower().startswith("incident"):
            incident_id = _extract_id(text) or f"INC-{int(time.time())}"
            # Persist incident
            await db.create(
                "audit_events",
                {
                    "kind": "incident",
                    "ts": _now_iso(),
                    "status": "open",
                    "payload": json.dumps({"text": text, "room": room_id}),
                },
                row_id=incident_id,
            )
            pending[incident_id] = {"room_id": room_id, "stage": "triaging"}

            # Unique twist: write a runbook row
            await db.create(
                "axelr_insights",
                {
                    "user_id": "ops",
                    "insight_type": "runbook",
                    "content": f"Runbook for {incident_id}: pending approval",
                    "timestamp": int(time.time() * 1000),
                    "system_origin": "axelr-ops-room",
                },
                row_id=f"rb-{incident_id}",
            )

            await switch.post_message(
                room_id,
                f"**Incident {incident_id} received.**\n"
                f"Likely cause: upstream dependency regression.\n"
                f"First step: check deploy diff.\n"
                f"Rollback plan: redeploy previous tag.\n\n"
                f"Reply `APPROVED` to mitigate.",
            )

        # ── Approval ────────────────────────────────────────────────
        elif text.upper() in ("APPROVED", "REJECTED"):
            incident_id = _latest_pending(pending, room_id)
            if not incident_id:
                await switch.post_message(room_id, "No incident awaiting approval.")
                continue
            status = "mitigated" if text.upper() == "APPROVED" else "held"
            await db.update("audit_events", incident_id, {"status": status})
            # Update runbook
            await db.upsert(
                "axelr_insights",
                f"rb-{incident_id}",
                {
                    "user_id": "ops",
                    "insight_type": "runbook",
                    "content": f"Runbook for {incident_id}: {status}",
                    "timestamp": int(time.time() * 1000),
                    "system_origin": "axelr-ops-room",
                },
            )
            await switch.post_message(
                room_id,
                f"**{incident_id} → {status}.** Runbook updated.",
            )
            pending.pop(incident_id, None)

    await switch.close()


def _extract_id(text: str) -> str | None:
    for t in text.split():
        if t.startswith("INC-"):
            return t
    return None

def _latest_pending(pending: dict[str, dict], room_id: str) -> str | None:
    for iid, meta in reversed(list(pending.items())):
        if meta["room_id"] == room_id:
            return iid
    return None

def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")

if __name__ == "__main__":
    asyncio.run(main())