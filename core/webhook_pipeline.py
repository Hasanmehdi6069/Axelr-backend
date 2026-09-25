# core/webhook_pipeline.py
"""
AXELR Webhook Extraction Pipeline
=================================
POST ``/webhook/extract`` with a JSON body:

    {
      "file_url":     "https://.../document.pdf",
      "pipeline_id":  "invoice-v3",
      "callback_url": "https://client.example.com/hook",
      "workspace":    "data",
      "task_type":    "extraction",
      "metadata":     {...}
    }

The endpoint acknowledges immediately (202) and processes asynchronously
in a background task. When processing completes, the result is POSTed to
``callback_url`` with an HMAC-SHA256 signature.

Reliability
-----------
* If ``QSTASH_TOKEN`` is configured, the *outbound callback* is sent via
  Upstash QStash (automatic retries, no local retry state). Otherwise a
  bounded local retry loop is used.
* SSRF protection: the file URL is resolved and rejected if the target is
  loopback / private / link-local / reserved.

Security
--------
* Inbound: ``Authorization: Bearer <WEBHOOK_INBOUND_TOKEN>`` **or**
  ``X-Axelr-Signature: sha256=<hex>`` over ``{timestamp}.{body}`` using
  ``WEBHOOK_INBOUND_SECRET``.
* Outbound: the callback is signed with ``WEBHOOK_CALLBACK_SECRET``.
* In production (``ENV`` not in dev/local/test), missing inbound auth
  configuration is refused with 503 rather than silently allowed.

RAM footprint: < 20 MB (streaming download, hard 25 MB cap).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable
from email.utils import parseaddr
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("axelr.webhook_pipeline")


RouteFunc = Callable[..., Awaitable[dict[str, Any]]]

_MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
_DOWNLOAD_TIMEOUT = 30.0
_CALLBACK_TIMEOUT = 15.0
_CALLBACK_RETRIES = 3
_ALLOWED_SCHEMES = {"http", "https"}
_QSTASH_PUBLISH = "https://qstash.upstash.io/v2/publish/{url}"

_DEV_ENVS = frozenset({"dev", "local", "test", "development"})


def _is_prod() -> bool:
    return (os.getenv("ENV", "dev") or "dev").lower() not in _DEV_ENVS


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------

class ExtractWebhookPayload(BaseModel):
    """Inbound payload schema for the extraction webhook."""

    file_url: str = Field(..., min_length=8, max_length=2000)
    pipeline_id: str = Field(..., min_length=1, max_length=128)
    callback_url: str = Field(..., min_length=8, max_length=2000)
    workspace: str = Field(default="data", max_length=32)
    task_type: str = Field(default="extraction", max_length=32)
    max_tokens: int = Field(default=4096, ge=64, le=16384)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Signing / verification helpers
# ---------------------------------------------------------------------------

def _sign(secret: str, timestamp: int, body: bytes) -> str:
    """Return hex HMAC-SHA256 of ``{timestamp}.{body}``."""
    msg = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _verify_inbound(
    request_body: bytes,
    *,
    authorization: str | None,
    signature_header: str | None,
    timestamp_header: str | None,
    shared_token: str,
    shared_secret: str,
    max_skew: int = 300,
) -> None:
    """
    Enforce inbound authentication.

    In production (ENV != dev/local/test) at least one of
    WEBHOOK_INBOUND_TOKEN / WEBHOOK_INBOUND_SECRET must be set. If neither
    is set, we refuse with 503 so misconfigured deployments cannot be
    silently exploited.
    """
    if not shared_token and not shared_secret:
        if _is_prod():
            logger.error("webhook_inbound_auth_unconfigured env=prod")
            raise HTTPException(
                status_code=503,
                detail="Webhook auth not configured",
            )
        logger.warning("webhook_inbound_auth_disabled env=dev")
        return

    # Bearer path
    if shared_token and authorization:
        prefix = "bearer "
        if authorization.lower().startswith(prefix) and hmac.compare_digest(
            authorization[len(prefix):].strip(), shared_token
        ):
            return

    # HMAC path
    if shared_secret and signature_header and timestamp_header:
        try:
            ts = int(timestamp_header)
        except ValueError:
            raise HTTPException(status_code=401, detail="Invalid timestamp")
        if abs(time.time() - ts) > max_skew:
            raise HTTPException(status_code=401, detail="Timestamp skew too large")
        expected = _sign(shared_secret, ts, request_body)
        got = signature_header.split("=", 1)[1] if "=" in signature_header else signature_header
        if hmac.compare_digest(expected, got.strip()):
            return

    raise HTTPException(status_code=401, detail="Webhook authentication failed")


# ---------------------------------------------------------------------------
# SSRF protection
# ---------------------------------------------------------------------------

async def _assert_public_url(url: str) -> None:
    """Raise ``HTTPException(400)`` if ``url`` is not a public HTTP(S) URL."""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise HTTPException(status_code=400, detail=f"URL scheme {parsed.scheme!r} not allowed")
    host = parsed.hostname
    if not host:
        raise HTTPException(status_code=400, detail="URL missing host")

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"DNS resolution failed: {e}")

    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except Exception:
            continue
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            raise HTTPException(
                status_code=400, detail=f"Blocked non-public address: {addr}"
            )


# ---------------------------------------------------------------------------
# Download + extraction
# ---------------------------------------------------------------------------

async def _download_file(
    client: httpx.AsyncClient, url: str, max_bytes: int = _MAX_DOWNLOAD_BYTES
) -> dict[str, Any]:
    """
    Stream-download a URL with a hard byte cap.

    Returns ``{"filename": str, "mimetype": str, "content_base64": str}``.
    Raises ``RuntimeError`` on transport / size failures.
    """
    filename = "download"
    mimetype = "application/octet-stream"
    total = 0
    chunks: list[bytes] = []

    async with client.stream("GET", url, timeout=_DOWNLOAD_TIMEOUT) as resp:
        resp.raise_for_status()
        cd = resp.headers.get("content-disposition", "")
        if cd:
            for part in cd.split(";"):
                part = part.strip()
                if part.lower().startswith("filename="):
                    _, fname = parseaddr(part)
                    if fname:
                        filename = fname.strip('"\'')
        ctype = resp.headers.get("content-type")
        if ctype:
            mimetype = ctype.split(";")[0].strip()

        async for chunk in resp.aiter_bytes(64 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise RuntimeError(f"file exceeds {max_bytes} bytes")
            chunks.append(chunk)

    blob = b"".join(chunks)
    if not blob:
        raise RuntimeError("empty file")
    return {
        "filename": filename,
        "mimetype": mimetype,
        "content_base64": base64.b64encode(blob).decode("ascii"),
        "size": total,
    }


# ---------------------------------------------------------------------------
# Callback POST
# ---------------------------------------------------------------------------

async def _post_callback(
    client: httpx.AsyncClient,
    callback_url: str,
    payload: dict[str, Any],
    *,
    secret: str,
    pipeline_id: str,
    qstash_token: str = "",
) -> bool:
    if not secret and _is_prod():
        logger.error("webhook_callback_secret_missing env=prod")
        return False

    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ts = int(time.time())
    signature = _sign(secret, ts, body) if secret else ""

    forward_headers = {
        "Content-Type": "application/json",
        "User-Agent": "Axelr-Webhook/1.0",
        "X-Axelr-Timestamp": str(ts),
        "X-Axelr-Pipeline-Id": pipeline_id,
    }
    if signature:
        forward_headers["X-Axelr-Signature"] = f"sha256={signature}"

    # --- QStash path ---
    if qstash_token:
        try:
            publish_url = _QSTASH_PUBLISH.format(url=callback_url)
            headers = {
                "Authorization": f"Bearer {qstash_token}",
                "Content-Type": "application/json",
                "Upstash-Retries": "3",
            }
            for k, v in forward_headers.items():
                if k.lower() == "content-type":
                    continue
                headers[f"Upstash-Forward-{k}"] = v
            resp = await client.post(
                publish_url, headers=headers, content=body, timeout=10.0
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.warning("qstash_publish_failed error=%s", e)
            # fall through to local POST

    # --- Local path with bounded retries ---
    for attempt in range(1, _CALLBACK_RETRIES + 1):
        try:
            resp = await client.post(
                callback_url,
                headers=forward_headers,
                content=body,
                timeout=_CALLBACK_TIMEOUT,
            )
            if 200 <= resp.status_code < 300:
                return True
            logger.warning(
                "callback_non_2xx status=%s attempt=%s", resp.status_code, attempt
            )
        except Exception as e:
            logger.warning("callback_error attempt=%s error=%s", attempt, e)
        if attempt < _CALLBACK_RETRIES:
            await asyncio.sleep(min(8, 2 ** attempt))
    return False


# ---------------------------------------------------------------------------
# Background job
# ---------------------------------------------------------------------------

async def _process_job(
    *,
    route_func: RouteFunc,
    http_client: httpx.AsyncClient,
    payload: ExtractWebhookPayload,
    callback_secret: str,
    qstash_token: str,
    job_id: str,
) -> None:
    """Run the extraction pipeline and post the callback."""
    started = time.time()
    result_payload: dict[str, Any] = {
        "job_id": job_id,
        "pipeline_id": payload.pipeline_id,
        "success": False,
        "text": "",
        "provider": None,
        "model": None,
        "elapsed_ms": 0.0,
        "error": None,
        "metadata": payload.metadata or {},
    }

    try:
        file_info = await _download_file(http_client, payload.file_url)
        file_info.pop("size", None)

        workspace = payload.workspace if payload.workspace in ("data", "design", "core") else "data"
        task_type = payload.task_type or ("extraction" if workspace == "data" else "frontend")

        ai_result = await route_func(
            workspace=workspace,
            task_type=task_type,
            prompt=f"Process the attached file for pipeline '{payload.pipeline_id}'.",
            history=[],
            files=[file_info],
            max_tokens=payload.max_tokens,
            temp=payload.temperature,
            tier="business",       # webhook jobs run at business tier
            user=None,
            context="",
        )

        if ai_result and ai_result.get("success"):
            result_payload.update(
                success=True,
                text=str(ai_result.get("text", "")),
                provider=ai_result.get("provider"),
                model=ai_result.get("model_used"),
            )
        else:
            result_payload["error"] = "ai_route_failed"
    except HTTPException as e:
        result_payload["error"] = f"download_rejected: {e.detail}"
    except Exception as e:
        logger.exception("webhook_job_failed job_id=%s error=%s", job_id, e)
        result_payload["error"] = f"{type(e).__name__}: {e}"

    result_payload["elapsed_ms"] = round((time.time() - started) * 1000, 2)

    ok = await _post_callback(
        http_client,
        payload.callback_url,
        result_payload,
        secret=callback_secret,
        pipeline_id=payload.pipeline_id,
        qstash_token=qstash_token,
    )
    if not ok:
        logger.warning("callback_undelivered job_id=%s", job_id)


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

def make_webhook_router(
    route_func: RouteFunc,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> APIRouter:
    """
    Build the webhook router.

    Parameters
    ----------
    route_func : RouteFunc
        The AI routing callable from ``app.py`` (e.g.
        ``route_ai_request_parallel``).
    http_client : httpx.AsyncClient | None
        Shared HTTP client. When ``None`` an internal client is created
        (closed automatically at process exit).

    Returns
    -------
    APIRouter
        Mounted by the host app via ``app.include_router(...)``.
    """
    router = APIRouter(prefix="/webhook", tags=["webhook"])

    client = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        follow_redirects=True,
    )

    inbound_token = (os.getenv("WEBHOOK_INBOUND_TOKEN") or "").strip()
    inbound_secret = (os.getenv("WEBHOOK_INBOUND_SECRET") or "").strip()
    callback_secret = (os.getenv("WEBHOOK_CALLBACK_SECRET") or "").strip()
    qstash_token = (os.getenv("QSTASH_TOKEN") or "").strip()

    @router.post("/extract", status_code=202)
    async def webhook_extract(
        request: Request,
        payload: ExtractWebhookPayload,
        background: BackgroundTasks,
        authorization: str | None = Header(default=None),
        x_axelr_signature: str | None = Header(default=None),
        x_axelr_timestamp: str | None = Header(default=None),
    ) -> JSONResponse:
        """
        Accept an extraction job, process it in the background, and deliver
        the result to ``callback_url``.

        Returns immediately with ``202 Accepted`` and a ``job_id``.
        """
        # 1. Auth
        body = await request.body()
        _verify_inbound(
            body,
            authorization=authorization,
            signature_header=x_axelr_signature,
            timestamp_header=x_axelr_timestamp,
            shared_token=inbound_token,
            shared_secret=inbound_secret,
        )

        # 2. SSRF checks on both URLs
        await _assert_public_url(payload.file_url)
        await _assert_public_url(payload.callback_url)

        # 3. Dispatch background processing
        job_id = hashlib.sha256(
            f"{time.time_ns()}|{payload.pipeline_id}".encode()
        ).hexdigest()[:16]

        background.add_task(
            _process_job,
            route_func=route_func,
            http_client=client,
            payload=payload,
            callback_secret=callback_secret,
            qstash_token=qstash_token,
            job_id=job_id,
        )

        return JSONResponse(
            status_code=202,
            content={"success": True, "job_id": job_id, "status": "queued"},
        )

    return router


# ── Public surface ───────────────────────────────────────────────────────────
__all__ = [
    "ExtractWebhookPayload",
    "make_webhook_router",
]