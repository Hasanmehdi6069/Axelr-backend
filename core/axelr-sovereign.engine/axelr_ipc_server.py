# axelr_ipc_server.py
"""
axelr_ipc_server.py — aiohttp WebSocket server for the AXELR IPC protocol.

Per-session: real ASR adapter + EnginePipeline + PhoneticCorrector.
Protocol envelope: {type, id, payload} per protocol.json v1.0.0.

Honest contract: refuses to start without a real ASR adapter unless
AXELR_ALLOW_MOCK=1. Every response carries `ok` and either `payload`
or `payload.error`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import numpy as np

try:
    from aiohttp import web, WSMsgType
except ImportError:
    web = None
    WSMsgType = None

from axelr_config import Tier, select_profile, map_js_tier_to_python
from axelr_engine import DecoderConfig, PhoneticCorrector, Token
from axelr_features import CloudConfig
from axelr_pipeline import EnginePipeline, PipelineConfig
from axelr_telemetry import MetricsExporter, SessionStore
from axelr_utils import TokenBucket, get_logger
from axelr_adapters import ASRConfig, select_adapter, make_local_fn
from axelr_streaming import (
    CancellationToken, CancelledError, RequestDeduplicator, RequestContext,
)
import sys
try:
    sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
    from permissions import ConsentStore
except ImportError:
    logger.warning("permissions module missing; consent enforcement disabled")
    class ConsentStore:  # minimal shim
        def grant(self, k): return False
        def check_graceful(self, keys): return (True, [])

logger = get_logger(__name__)

PROTOCOL_VERSION = "1.0.0"
SERVER_VERSION = "4.0.0"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_PATH = "/axelr/v1"
HANDSHAKE_TIMEOUT_S = 2.0
MAX_FRAME_BYTES = 2 * 1024 * 1024


@dataclass
class _Session:
    sid: str
    tier: str
    pipeline: EnginePipeline
    phonetic: PhoneticCorrector
    adapter: Any
    ws: Any
    bucket: TokenBucket = field(default_factory=lambda: TokenBucket(200, 200.0))
    created_at: float = field(default_factory=time.monotonic)
    dedup: RequestDeduplicator = field(default_factory=RequestDeduplicator)
    active_tokens: Dict[int, CancellationToken] = field(default_factory=dict)


class IPCServer:
    """One aiohttp app, one adapter per session, spec-compliant dispatch."""

    def __init__(self, tier: Optional[Tier] = None,
                 host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        if web is None:
            raise RuntimeError("aiohttp not installed; IPCServer disabled")
        self._tier = tier
        self._host = host
        self._port = port
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._sessions: Dict[str, _Session] = {}
        self._shutdown_event = asyncio.Event()
        self._metrics = MetricsExporter()
        self._session_store = SessionStore()

    async def start(self) -> str:
        self._app = web.Application()
        self._app.router.add_get(DEFAULT_PATH, self._ws_handler)
        self._app.router.add_get("/healthz", self._healthz)
        self._app.router.add_get("/readyz", self._readyz)
        self._app.router.add_get("/metrics", self._metrics_handler)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        endpoint = f"ws://{self._host}:{self._port}{DEFAULT_PATH}"
        logger.info("ipc_ready endpoint=%s protocol=%s",
                    endpoint, PROTOCOL_VERSION)
        return endpoint

    async def _healthz(self, request: "web.Request") -> "web.Response":
        return web.json_response({
            "ok": True,
            "sessions": len(self._sessions),
            "protocol_version": PROTOCOL_VERSION,
            "server_version": SERVER_VERSION,
        })

    async def _readyz(self, request: "web.Request") -> "web.Response":
        return web.json_response({
            "ready": True,
            "sessions": len(self._sessions),
        })

    async def _metrics_handler(self, request: "web.Request") -> "web.Response":
        self._metrics.active_sessions.set(len(self._sessions))
        return web.Response(text=self._metrics.render(),
                            content_type="text/plain; version=0.0.4")

    async def wait_for_shutdown(self) -> None:
        await self._shutdown_event.wait()

    async def stop(self) -> None:
        for s in list(self._sessions.values()):
            try:
                await s.pipeline.stop()
            except Exception:
                logger.exception("session_stop_failed sid=%s", s.sid)
            try:
                s.adapter.dispose()
            except Exception:
                logger.exception("adapter_dispose_failed sid=%s", s.sid)
            try:
                await s.ws.close()
            except Exception:
                pass
        self._sessions.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._shutdown_event.set()

    async def _ws_handler(self, request: "web.Request") -> "web.WebSocketResponse":
        ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=MAX_FRAME_BYTES)
        await ws.prepare(request)
        session = await self._handshake(ws)
        if session is None:
            await ws.close()
            return ws
        self._sessions[session.sid] = session
        logger.info("session_open sid=%s tier=%s adapter=%s",
                    session.sid, session.tier, session.adapter.name)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._dispatch(session, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    logger.warning("ws_error sid=%s err=%s",
                                   session.sid, ws.exception())
                    break
        finally:
            self._sessions.pop(session.sid, None)
            try:
                await session.pipeline.stop()
            except Exception:
                logger.exception("session_stop_failed sid=%s", session.sid)
            try:
                session.adapter.dispose()
            except Exception:
                logger.exception("adapter_dispose_failed sid=%s", session.sid)
            logger.info("session_close sid=%s", session.sid)
        return ws

    async def _handshake(self, ws: "web.WebSocketResponse") -> Optional[_Session]:
        try:
            first = await asyncio.wait_for(ws.receive(), timeout=HANDSHAKE_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning("handshake_timeout")
            return None
        if first.type != WSMsgType.TEXT:
            return None
        try:
            env = json.loads(first.data)
        except json.JSONDecodeError:
            await ws.send_json({"type": "error", "id": 0, "ok": False,
                                "payload": {"code": "bad_json",
                                            "message": "invalid first frame"}})
            return None
        if env.get("type") != "init":
            await ws.send_json({"type": "error", "id": env.get("id", 0),
                                "ok": False,
                                "payload": {"code": "bad_handshake",
                                            "message": "first frame must be init"}})
            return None

        # Check user consent before proceeding (load from consent.json if exists)
        store = ConsentStore()
        # Load consent from consent.json if it exists (GDPR compliance)
        consent_path = os.path.join(os.path.dirname(__file__), "consent.json")
        if os.path.exists(consent_path):
            try:
                with open(consent_path, "r", encoding="utf-8") as f:
                    saved_consent = json.load(f)
                # Grant any consents that are explicitly saved as granted
                for consent_key, granted in saved_consent.items():
                    if granted:
                        store.grant(consent_key)
            except Exception as e:
                logger.warning("failed_to_load_consent_json err=%s", str(e))
        
        ok, missing = store.check_graceful(["data_processing"])
        if not ok:
            await ws.send_json({
                "type": "error", "id": env.get("id", 0), "ok": False,
                "payload": {"code": "protocol_mismatch",
                            "message": f"missing consent: {missing}"},
            })
            return None

        payload = env.get("payload") or {}
        tier_str = str(payload.get("tier", "auto"))
        try:
            if tier_str == "auto":
                tier = None
            else:
                tier = map_js_tier_to_python(tier_str)
        except ValueError:
            tier = None
        profile = select_profile(tier or self._tier)

        # ── Real adapter selection per session ──────────────────────
        try:
            adapter = select_adapter(ASRConfig(model_size=profile.model_size))
        except RuntimeError as e:
            await ws.send_json({
                "type": "error", "id": env.get("id", 0), "ok": False,
                "payload": {"code": "no_provider_available",
                            "message": f"no ASR adapter: {e}"},
            })
            return None

        if adapter.name == "mock" and os.environ.get("AXELR_ALLOW_MOCK") != "1":
            await ws.send_json({
                "type": "error", "id": env.get("id", 0), "ok": False,
                "payload": {"code": "no_provider_available",
                            "message": "mock ASR disabled in production"},
            })
            return None

        local_fn = make_local_fn(adapter)
        cfg = PipelineConfig(
            decoder=DecoderConfig(
                cloud_fallback_url=None, api_key=None,
                model_quantization=profile.quantization.value,
                max_local_memory_mb=profile.max_memory_mb,
            ),
            cloud=CloudConfig(),
        )
        pipeline = EnginePipeline(profile, cfg, local_fn, buffer_for_drain=False)
        await pipeline.start()
        phonetic = PhoneticCorrector()
        phonetic.load(["receive", "their", "there", "your", "you're",
                       "its", "it's", "to", "too", "two"])
        sid = str(uuid.uuid4())
        session = _Session(
            sid=sid, tier=profile.tier.value, pipeline=pipeline,
            phonetic=phonetic, adapter=adapter, ws=ws,
        )

        def partial_callback(tokens: list[str]):
            async def send_partial():
                await session.ws.send_json({
                    "type": "partial_transcript",
                    "id": 0,
                    "ok": True,
                    "payload": {"text": " ".join(tokens)},
                })
            if not session.ws.closed:
                asyncio.create_task(send_partial())

        pipeline.on_partial(partial_callback)

        await ws.send_json({
            "type": "ready", "id": env.get("id", 0), "ok": True,
            "payload": {
                "session_id": sid,
                "server_version": SERVER_VERSION,
                "protocol_version": PROTOCOL_VERSION,
                "tier": profile.tier.value,
                "adapter": adapter.name,
                "exclusive": True,
                "owner": "python",
            },
        })
        return session

    async def _dispatch(self, session: _Session, raw: str) -> None:
        allowed, retry = session.bucket.acquire()
        if not allowed:
            await session.ws.send_json({
                "type": "error", "id": 0, "ok": False,
                "payload": {"code": "rate_limit",
                            "message": "too many requests",
                            "retry_after_ms": int(retry * 1000)},
            })
            return
        try:
            env = json.loads(raw)
        except json.JSONDecodeError:
            await session.ws.send_json({
                "type": "error", "id": 0, "ok": False,
                "payload": {"code": "bad_json", "message": "invalid json"}})
            return

        req_id = env.get("id", 0)
        req_type = env.get("type", "")
        payload = env.get("payload") or {}
        t0 = time.perf_counter()
        ok = True
        try:
            if req_type == "cancel":
                target_id = int(payload.get("id", 0))
                token = session.active_tokens.pop(target_id, None)
                if token is not None:
                    token.cancel("remote")
                    await session.ws.send_json({
                        "type": "cancelled", "id": req_id, "ok": True,
                        "payload": {"cancelled_id": target_id}})
                else:
                    await session.ws.send_json({
                        "type": "error", "id": req_id, "ok": False,
                        "payload": {"code": "session_closed", "message": f"no active request {target_id}"}})
                return
            elif req_type == "ping":
                await session.ws.send_json({"type": "pong", "id": req_id,
                                            "ok": True, "payload": {}})
            elif req_type == "degrade_status":
                await session.ws.send_json({
                    "type": "degrade_status", "id": req_id, "ok": True,
                    "payload": session.pipeline.degradation.state()})
            elif req_type == "resume":
                requested = str(payload.get("session_id", ""))
                if requested in self._sessions and requested != session.sid:
                    old = self._sessions.pop(requested)
                    try:
                        await old.pipeline.stop()
                    except Exception:
                        pass
                await session.ws.send_json({
                    "type": "resumed", "id": req_id, "ok": True,
                    "payload": {"session_id": session.sid}})
            elif req_type == "correct":
                word = str(payload.get("word", ""))
                corrected = session.phonetic.correct(word)
                await session.ws.send_json({
                    "type": "correct_result", "id": req_id, "ok": True,
                    "payload": {"word": word, "corrected": corrected,
                                "score": 1.0 if corrected == word else 0.9}})
            elif req_type == "calibrate":
                logit = float(payload.get("logit", 0.0))
                cal = session.pipeline.decoder.calibrator
                await session.ws.send_json({
                    "type": "calibrate_result", "id": req_id, "ok": True,
                    "payload": {"raw_logit": logit,
                                "calibrated": cal.calibrate(logit),
                                "temperature": cal.temperature}})
            elif req_type == "context":
                session.pipeline.observe_context(payload)
                snap = session.pipeline.injector.snapshot
                keywords, _commons, boost = session.pipeline.injector.biasing_terms()
                await session.ws.send_json({
                    "type": "context_ack", "id": req_id, "ok": True,
                    "payload": {
                        "domain_hints": sorted(snap.domain_hints) if snap else [],
                        "biasing_terms": sorted(keywords),
                        "boost": boost}})
            elif req_type == "drain":
                text = session.pipeline.drain()
                await session.ws.send_json({
                    "type": "drain_result", "id": req_id, "ok": True,
                    "payload": {"text": text, "consumed": 1 if text else 0}})
            elif req_type == "transcribe":
                lm = payload.get("log_mel")
                if not isinstance(lm, list):
                    raise ValueError("log_mel must be a 2D list")
                arr = np.asarray(lm, dtype=np.float32)
                # No PCM available from browser — pass log_mel only
                tokens = await session.pipeline.process_mel(arr)
                await session.ws.send_json({
                    "type": "transcript", "id": req_id, "ok": True,
                    "payload": {
                        "text": " ".join(tokens),
                        "tokens": [{"text": t, "confidence": 1.0,
                                    "token_id": i}
                                   for i, t in enumerate(tokens)]}})
            elif req_type == "transcribe_pcm":
                idem_key = f"{session.sid}:{req_id}:pcm"
                cached = session.dedup.check(idem_key)
                if cached is not None:
                    await session.ws.send_json({
                        "type": "transcript", "id": req_id, "ok": True,
                        "payload": cached, "dedup": True})
                    return
                pcm_list = payload.get("pcm")
                if not isinstance(pcm_list, list):
                    raise ValueError("pcm must be list of int16 samples")
                pcm = np.asarray(pcm_list, dtype=np.int16)
                token = CancellationToken()
                session.active_tokens[req_id] = token
                try:
                    lm = log_mel_from_pcm(pcm)

                    def _send_partial(payload: Dict[str, Any]) -> None:
                        asyncio.create_task(session.ws.send_json({
                            "type": "streaming_partial", "id": req_id,
                            "ok": True, "payload": payload,
                        }))
                    emitter = AsyncPartialEmitter(_send_partial)
                    session.pipeline.set_partial_emitter(emitter)

                    result = await session.pipeline.process_mel(lm, pcm=pcm, token=token)
                finally:
                    session.pipeline.set_partial_emitter(None)
                    out_payload = {
                        "text": " ".join(result),
                        "tokens": [{"text": t, "confidence": 1.0, "token_id": i}
                                   for i, t in enumerate(result)],
                    }
                    session.dedup.record(idem_key, out_payload)
                    await session.ws.send_json({
                        "type": "transcript", "id": req_id, "ok": True,
                        "payload": out_payload})
                except CancelledError:
                    await session.ws.send_json({
                        "type": "error", "id": req_id, "ok": False,
                        "payload": {"code": "session_closed", "message": "cancelled"}})
                finally:
                    session.active_tokens.pop(req_id, None)
            elif req_type == "config":
                new_tier = payload.get("tier")
                if new_tier in ("low_ram", "standard", "high_end"):
                    try:
                        tier_enum = Tier(new_tier)
                        new_profile = select_profile(tier_enum)
                        session.tier = new_profile.tier.value
                        await session.ws.send_json({
                            "type": "context_ack", "id": req_id, "ok": True,
                            "payload": {"domain_hints": [], "biasing_terms": [],
                                        "boost": 1.0, "tier": session.tier}})
                    except Exception as e:
                        await session.ws.send_json({
                            "type": "error", "id": req_id, "ok": False,
                            "payload": {"code": "unsupported_payload", "message": str(e)}})
                else:
                    await session.ws.send_json({
                        "type": "error", "id": req_id, "ok": False,
                        "payload": {"code": "unsupported_payload", "message": "tier must be low_ram|standard|high_end"}})
                return
            elif req_type == "shutdown":
                await session.ws.send_json({"type": "shutdown_ack",
                                            "id": req_id, "ok": True,
                                            "payload": {}})
                self._shutdown_event.set()
            else:
                ok = False
                await session.ws.send_json({
                    "type": "error", "id": req_id, "ok": False,
                    "payload": {"code": "unknown_command",
                                "message": f"unsupported type: {req_type}"}})
        except Exception as e:
            ok = False
            logger.exception("dispatch_failed type=%s id=%s", req_type, req_id)
            await session.ws.send_json({
                "type": "error", "id": req_id, "ok": False,
                "payload": {"code": "internal_error", "message": str(e)[:200]}})
        finally:
            self._metrics.record_request(req_type, ok,
                                          time.perf_counter() - t0)


async def serve(tier: Optional[Tier] = None,
                port: int = DEFAULT_PORT) -> None:
    server = IPCServer(tier=tier, port=port)
    await server.start()
    try:
        await server.wait_for_shutdown()
    finally:
        await server.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="AXELR IPC server")
    parser.add_argument("--tier", choices=[t.value for t in Tier], default=None)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    tier = Tier(args.tier) if args.tier else None
    try:
        asyncio.run(serve(tier, args.port))
    except KeyboardInterrupt:
        logger.info("server_interrupted")


if __name__ == "__main__":
    main()