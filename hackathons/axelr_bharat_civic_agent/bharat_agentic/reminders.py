"""Mock SMS / WhatsApp providers + tool registration.

Swap `MockSMSProvider` / `MockWhatsAppProvider` for Twilio / WhatsApp
Business API adapters — dispatch policy, approval gating, and receipt
recording stay identical.

Every provider call is wrapped in a strict timeout so a hung real provider
cannot block the approval state machine.
"""
from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
import time
from typing import Any, Dict, List, Optional

from .errors import BharatAgentError
from .models import ApprovalState, ReminderChannel
from .sentinel import Sentinel

logger = logging.getLogger("bharat_agentic.reminders")

_PROVIDER_TIMEOUT_SECONDS = 20.0
_PHONE_RE = __import__("re").compile(r"^\+?[1-9]\d{6,14}$")


def _validate_phone(to: str) -> str:
    to = (to or "").strip()
    if not _PHONE_RE.match(to):
        raise BharatAgentError(
            f"invalid phone number: {to!r} (expected E.164-ish, "
            "e.g. +919876543210)")
    return to


class MockSMSProvider:
    name = "mock-sms"

    async def send(self, to: str, body: str) -> Dict[str, Any]:
        await asyncio.sleep(0.002)
        return {
            "provider": self.name,
            "message_id": f"mock-sms-{random.randint(10**9, 10**10)}",
            "to": to,
            "delivered": True,
            "latency_ms": round(random.uniform(35, 140), 2),
        }


class MockWhatsAppProvider:
    name = "mock-whatsapp"

    async def send(self, to: str, body: str) -> Dict[str, Any]:
        await asyncio.sleep(0.003)
        return {
            "provider": self.name,
            "message_id": f"mock-wa-{random.randint(10**9, 10**10)}",
            "to": to,
            "delivered": True,
            "latency_ms": round(random.uniform(50, 180), 2),
        }


_PROVIDERS = {
    ReminderChannel.SMS: MockSMSProvider(),
    ReminderChannel.WHATSAPP: MockWhatsAppProvider(),
}


def _init_receipts_db(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS reminder_receipts (
                reminder_id TEXT PRIMARY KEY,
                approval_id TEXT NOT NULL,
                case_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                to_addr TEXT NOT NULL,
                status TEXT NOT NULL,
                provider_message_id TEXT,
                delivered INTEGER NOT NULL,
                latency_ms REAL NOT NULL,
                recorded_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_receipt_case
                ON reminder_receipts(case_id);
        """)


def _record_receipt(
    db_path: str, reminder_id: str, approval_id: str, case_id: str,
    channel: str, to: str, status: ApprovalState,
    message_id: Optional[str], delivered: bool, latency_ms: float,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO reminder_receipts VALUES "
            "(?,?,?,?,?,?,?,?,?,?)",
            (reminder_id, approval_id, case_id, channel, to,
             status.value, message_id, int(delivered), latency_ms,
             time.time()))


def register_reminder_tools(sentinel: Sentinel, db_path: str) -> None:
    """Register ``sms_send`` and ``whatsapp_send`` tools on the sandbox."""
    _init_receipts_db(db_path)

    async def _handle(
        channel: ReminderChannel, payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        to = _validate_phone(payload.get("to", ""))
        provider = _PROVIDERS[channel]
        try:
            result = await asyncio.wait_for(
                provider.send(to, payload["body"]),
                timeout=_PROVIDER_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            raise BharatAgentError(
                f"provider {channel.value} timed out after "
                f"{_PROVIDER_TIMEOUT_SECONDS}s") from exc
        _record_receipt(
            db_path=db_path,
            reminder_id=payload["reminder_id"],
            approval_id=payload["approval_id"],
            case_id=payload["case_id"],
            channel=channel.value,
            to=to,
            status=ApprovalState.EXECUTED,
            message_id=result["message_id"],
            delivered=bool(result["delivered"]),
            latency_ms=float(result["latency_ms"]))
        logger.info(
            "reminder_sent channel=%s case=%s reminder=%s provider_msg=%s",
            channel.value, payload["case_id"], payload["reminder_id"],
            result["message_id"])
        return result

    sentinel.executor.register(
        "sms_send", lambda p: _handle(ReminderChannel.SMS, p))
    sentinel.executor.register(
        "whatsapp_send", lambda p: _handle(ReminderChannel.WHATSAPP, p))


def _list_receipts_sync(db_path: str, case_id: str) -> List[Dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM reminder_receipts WHERE case_id=? "
            "ORDER BY recorded_at DESC", (case_id,)).fetchall()
        return [dict(r) for r in rows]


async def list_receipts(db_path: str, case_id: str) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(_list_receipts_sync, db_path, case_id)
