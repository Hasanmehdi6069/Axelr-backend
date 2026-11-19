from __future__ import annotations

import asyncio
import base64
import gc
import hashlib
import hmac
import json
import logging
import os
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

logger = logging.getLogger("axelr.qstash.webhook")
router = APIRouter()

CHUNK_SIZE = 1024 * 1024
MAX_TOKEN_BATCH = 1024
MAX_PAYLOAD_SIZE = 5 * 1024 * 1024


try:
    from opentelemetry import trace
    _tracer = trace.get_tracer("axelr.qstash")
except Exception:
    _tracer = None


class QStashTaskPayload(BaseModel):
    task_id: str = Field(..., description="Unique task identifier")
    task_type: str = Field(
        ..., pattern="^(embedding|inference|storage|persistence|health)$",
    )
    payload_stream_url: Optional[str] = Field(None)
    token_chunk: Optional[list[int]] = Field(None)
    metadata: dict = Field(default_factory=dict)

    @property
    def is_write_operation(self) -> bool:
        return self.task_type in ("storage", "persistence")


async def verify_qstash_signature(request: Request) -> bool:
    key = (os.getenv("UPSTASH_QSTASH_CURRENT_SIGNING_KEY") or "").strip()
    if not key:
        env = (os.getenv("ENVIRONMENT") or "development").lower()
        if env == "production":
            logger.error("qstash_signing_key_missing_in_prod")
            return False
        logger.warning("qstash_signing_key_missing_dev_mode")
        return True
    try:
        body = await request.body()
        sig_header = request.headers.get("upstash-signature", "")
        if not sig_header:
            return False
        provided = sig_header.split("=", 1)[-1].strip()
        expected = base64.b64encode(
            hmac.new(key.encode(), body, hashlib.sha256).digest()
        ).decode()
        return hmac.compare_digest(provided, expected)
    except Exception as e:
        logger.warning("qstash_signature_verify_failed error=%s", e)
        return False


async def stream_token_chunks(stream_url: str) -> AsyncIterator[list[int]]:
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(stream_url) as response:
            if response.status != 200:
                raise RuntimeError(f"stream_url returned {response.status}")
            buffer = bytearray()
            async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                buffer.extend(chunk)
                while len(buffer) >= MAX_TOKEN_BATCH * 4:
                    token_batch = [
                        int.from_bytes(buffer[i:i + 4], byteorder="little")
                        for i in range(0, MAX_TOKEN_BATCH * 4, 4)
                    ]
                    yield token_batch
                    del token_batch
                    buffer = buffer[MAX_TOKEN_BATCH * 4:]
                    gc.collect()
            if buffer:
                token_batch = [
                    int.from_bytes(buffer[i:i + 4], byteorder="little")
                    for i in range(0, len(buffer) - 3, 4)
                ]
                yield token_batch
                del token_batch
                gc.collect()
            del buffer
            gc.collect()


async def _process_token_chunk(token_chunk: list[int], task_id: str) -> None:
    logger.debug("processing_token_chunk task_id=%s size=%d",
                 task_id, len(token_chunk))
    await asyncio.sleep(0.01)
    del token_chunk
    gc.collect()


async def _process_simple_task(payload: QStashTaskPayload) -> None:
    logger.debug("processing_simple_task task_id=%s type=%s",
                 payload.task_id, payload.task_type)
    if payload.task_type == "health":
        return
    await asyncio.sleep(0.05)
    gc.collect()


async def execute_ai_task(payload: QStashTaskPayload) -> dict:
    task_id = payload.task_id
    span_cm = (
        _tracer.start_as_current_span(f"ai_task.{task_id}")
        if _tracer is not None else _null_context()
    )
    try:
        with span_cm as span:
            try:
                if payload.payload_stream_url:
                    async for token_chunk in stream_token_chunks(payload.payload_stream_url):
                        await _process_token_chunk(token_chunk, task_id=task_id)
                        gc.collect()
                elif payload.token_chunk:
                    await _process_token_chunk(payload.token_chunk, task_id=task_id)
                    del payload.token_chunk
                    gc.collect()
                else:
                    await _process_simple_task(payload)
                return {"status": "completed", "task_id": task_id,
                        "memory_released": True}
            except Exception as e:
                if span is not None:
                    span.record_exception(e)
                    span.set_attribute("task.status", "failure")
                raise
    finally:
        gc.collect()
        gc.collect()


class _null_context:
    def __enter__(self): return None
    def __exit__(self, *a): return False


@router.post("/api/qstash/webhook", tags=["qstash", "webhooks"])
async def qstash_webhook(
    request: Request, verified: bool = Depends(verify_qstash_signature),
) -> Response:
    if not verified:
        client_ip = request.client.host if request.client else "unknown"
        logger.warning("invalid_qstash_signature client_ip=%s", client_ip)
        raise HTTPException(status_code=403, detail="Invalid request signature")

    try:
        raw = await request.body()
        if len(raw) > MAX_PAYLOAD_SIZE:
            raise HTTPException(413, "Payload too large")
        payload = QStashTaskPayload(**json.loads(raw))
        logger.info("qstash_task_received task_id=%s type=%s",
                    payload.task_id, payload.task_type)
        result = await execute_ai_task(payload)
        del payload
        gc.collect()
        return Response(
            content=json.dumps(result),
            media_type="application/json",
            status_code=200,
        )
    except json.JSONDecodeError as e:
        logger.error("invalid_json_payload error=%s", e)
        raise HTTPException(400, "Invalid JSON payload")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("qstash_task_failed error=%s", e)
        raise HTTPException(500, f"Task processing failed: {str(e)[:200]}")


@router.post("/api/qstash/dlq", tags=["qstash", "webhooks"])
async def qstash_dlq(request: Request) -> Response:
    raw = await request.body()
    logger.error("qstash_dlq_received payload=%s", raw.decode('utf-8', errors='replace')[:500])
    return Response(status_code=200, content=json.dumps({"status": "received"}))


def init_instrumentation(app) -> None:
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app)
        logger.info("opentelemetry_instrumentation_initialized")
    except Exception:
        logger.info("opentelemetry_unavailable_skipping")


__all__ = ["router", "init_instrumentation"]
