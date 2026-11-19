"""Switch agent client — wire-correct.

Critical fix vs. previous version: `websockets.connect` does NOT accept
`extra_headers` for query params. Query params MUST be in the URL.
Retries with exponential backoff. Emits events as an async iterator.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any, AsyncIterator
from urllib.parse import urlencode

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger("switch_client")


class SwitchAgentClient:
    def __init__(
        self,
        bridge_url: str | None = None,
        registration_token: str | None = None,
        agent_id: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self._bridge = (bridge_url or os.getenv("SWITCH_BRIDGE_URL", "http://localhost:7312")).rstrip("/")
        self._registration_token = registration_token or os.getenv("SWITCH_REGISTRATION_TOKEN", "")
        self._agent_id = agent_id or os.getenv("SWITCH_AGENT_ID", "")
        self._api_key = api_key or os.getenv("SWITCH_AGENT_API_KEY", "")
        self._connection_id = str(uuid.uuid4())
        self._cursor = 0
        self._http = httpx.AsyncClient(base_url=self._bridge, timeout=30.0)

    async def register(self, name: str, connection_model: str = "always_on", addressing: str = "anyone") -> dict[str, Any]:
        if not self._registration_token:
            raise RuntimeError("SWITCH_REGISTRATION_TOKEN is required for registration")
        headers = {"Authorization": f"Bearer {self._registration_token}"}
        payload = {
            "name": name,
            "description": "Axelr incident triage agent",
            "connector_type": "python",
            "integration_profile": {
                "connection_model": connection_model,
                "message_exchange": True,
                "pre_invocation_mediation": [],
                "post_invocation_mediation": [],
                "event_reporting": [],
            },
            "addressing": addressing,
            "overwrite": True,
        }
        r = await self._http.post("/agents", json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
        self._agent_id = data["id"]
        self._api_key = data["api_key"]
        logger.info("registered agent_id=%s", self._agent_id)
        return data

    async def events(self, rooms: list[str]) -> AsyncIterator[dict[str, Any]]:
        ws_base = f"{self._bridge.replace('http', 'ws')}/agents/{self._agent_id}/connection/ws"
        params = {
            "connection_id": self._connection_id,
            "rooms": ",".join(rooms),
            "scope": "single",
        }
        ws_url = f"{ws_base}?{urlencode(params)}"          # ← THE FIX
        headers = {"Authorization": f"Bearer {self._api_key}"}
        backoff = 1.0

        while True:
            try:
                async with websockets.connect(
                    ws_url,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    logger.info("socket_open conn=%s", self._connection_id)
                    backoff = 1.0
                    async for raw in ws:
                        try:
                            frame = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        kind = frame.get("type")
                        if kind == "ping":
                            await ws.send(json.dumps({"type": "pong", "cursor": self._cursor}))
                            continue
                        if kind == "connection_state":
                            self._cursor = frame.get("cursor", 0)
                            logger.info("connection_state cursor=%s", self._cursor)
                            continue
                        if kind in ("gap", "evicted", "subscription_changed", "room_released"):
                            if kind == "evicted":
                                break
                            continue
                        seq = frame.get("sequence") or frame.get("id")
                        if seq:
                            self._cursor = max(self._cursor, int(seq))
                        yield frame
            except ConnectionClosed:
                logger.warning("socket_closed backoff=%.1fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            except Exception as e:
                logger.error("socket_error err=%s", e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def post_message(self, room_id: str, content: str) -> dict[str, Any]:
        r = await self._http.post(
            f"/agents/{self._agent_id}/message",
            json={"room_id": room_id, "content": content},
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        r.raise_for_status()
        return r.json()

    async def close(self) -> None:
        await self._http.aclose()
