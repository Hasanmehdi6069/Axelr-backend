# api/routes_hooks.py
"""
AXELR — Hooks (F45 edge webhook + F47 collaborative streaming)
================================================================
Two endpoints:

    POST /api/hooks/verify        — CLI-triggered anti-hallucination verify
    GET  /api/collab/{room}/sse   — multiplexed token stream for N clients
    POST /api/warmup              — pre-submit DB warmup signal (F44)

F45 is stateless: the payload carries the source text + claim to verify.
F47 uses Redis pub/sub as the fan-out bus; each subscriber holds one
short-lived socket. Nothing persists.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import time
from typing import Any, AsyncIterator

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .state import state

logger = logging.getLogger("axelr.hooks")
router = APIRouter(tags=["hooks"])

_SOURCE_MAX = 200_000
_CLAIM_MAX = 4_000


# ═══════════════════════════════════════════════════════════════════════════
# F45 · CLI-triggered anti-hallucination verify
# ═══════════════════════════════════════════════════════════════════════════
class VerifyRequest(BaseModel):
    source: str = Field(..., min_length=1, max_length=_SOURCE_MAX)
    claim: str = Field(..., min_length=1, max_length=_CLAIM_MAX)
    normalize: bool = True


def _normalize(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    return text.strip().casefold()


@router.post("/api/hooks/verify")
async def hooks_verify(payload: VerifyRequest):
    """
    Deterministic substring check — the smallest possible anti-hallucination
    primitive. Returns whether the claim is literally grounded in source.
    """
    src = payload.source if not payload.normalize else _normalize(payload.source)
    claim = payload.claim if not payload.normalize else _normalize(payload.claim)
    grounded = claim in src
    idx = src.find(claim) if grounded else -1
    return {
        "success": True,
        "grounded": grounded,
        "source_offset": idx,
        "claim_length": len(claim),
        "checked_at": time.time(),
    }


# ═══════════════════════════════════════════════════════════════════════════
# F47 · Collaborative streaming via Redis pub/sub
# ═══════════════════════════════════════════════════════════════════════════
def _room_key(room: str) -> str:
    return f"axelr:collab:{room}"


@router.get("/api/collab/{room}/sse")
async def collab_sse(room: str, request: Request):
    """
    Subscribe to a room's token bus. Every token published via
    `publish_token` is fanned out to all subscribers of the same room.
    """
    if not re.match(r"^[a-zA-Z0-9_\-]{1,64}$", room):
        raise HTTPException(status_code=400, detail="Invalid room id")

    if state.redis_client is None:
        raise HTTPException(status_code=503, detail="Collaboration bus unavailable")

    pubsub = state.redis_client.pubsub()
    channel = _room_key(room)
    await pubsub.subscribe(channel)

    async def _stream() -> AsyncIterator[bytes]:
        try:
            yield f"event: open\ndata: {json.dumps({'room': room})}\n\n".encode()
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(
                        pubsub.get_message(ignore_subscribe_messages=True,
                                           timeout=1.0),
                        timeout=2.0,
                    )
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                if not msg or msg.get("type") != "message":
                    continue
                data = msg.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8", "replace")
                yield f"event: token\ndata: {data}\n\n".encode()
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(channel)
            with contextlib.suppress(Exception):
                await pubsub.aclose()

    return StreamingResponse(
        _stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache",
                 "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


async def publish_token(room: str, token: str) -> None:
    """Publish one token to the room bus. Best-effort; never raises."""
    if state.redis_client is None:
        return
    with contextlib.suppress(Exception):
        await state.redis_client.publish(
            _room_key(room), json.dumps({"t": token, "ts": time.time()}),
        )


# ═══════════════════════════════════════════════════════════════════════════
# F44 · Pre-submit warmup signal
# ═══════════════════════════════════════════════════════════════════════════
# FIX: the previous version accepted `x_warmup_token` but never validated
# it — any caller could trigger a DB ping. Now the token is enforced only
# when WARMUP_TOKEN is configured; if unset, the endpoint is open (dev
# mode) so local work is unblocked.
@router.post("/api/warmup", include_in_schema=False)
async def warmup_signal(x_warmup_token: str | None = Header(default=None)):
    from core.warmup import signal_warmup
    expected = (os.getenv("WARMUP_TOKEN") or "").strip()
    if expected and x_warmup_token != expected:
        raise HTTPException(status_code=403, detail="Invalid warmup token")
    ok = await signal_warmup(state.redis_client)
    return {"success": ok}


__all__ = ["router", "publish_token"]