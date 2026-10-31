<<<<<<< HEAD
# core/io_pipeline.py
"""
AXELR — I/O Pipeline (webhook inbound · worker sandbox outbound)
================================================================
Both talk to an external service. Both must never raise. Both retry with
bounded backoff. Both sign or verify what they send. One file.

    §1 Worker sandbox     execute_code_on_worker (outbound, retried)
    §2 Webhook pipeline   inbound HTTP router + SSRF-protected download
"""
from __future__ import annotations
import asyncio
import base64
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import random
import socket
import tempfile
import time
from collections.abc import Awaitable, Callable
from email.utils import parseaddr
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("axelr.io")


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Worker sandbox
# ═══════════════════════════════════════════════════════════════════════════
WORKER_URL = (os.getenv("WORKER_URL") or os.getenv("DATA_WORKER_URL") or "").strip().rstrip("/")
WORKER_API_KEY = (os.getenv("WORKER_API_KEY") or "").strip()
DEFAULT_TIMEOUT_S = float(os.getenv("WORKER_TIMEOUT_SECONDS", "10.0"))
MAX_RETRIES = max(0, int(os.getenv("WORKER_MAX_RETRIES", "2")))
BACKOFF_BASE_S = 0.35
MAX_CODE_BYTES = 64 * 1024

_ALLOWED_LANGUAGES = {
    "python": "python", "py": "python", "python3": "python",
    "javascript": "javascript", "js": "javascript", "node": "javascript",
    "typescript": "javascript", "ts": "javascript",
}

_WORKER_HTTP: httpx.AsyncClient | None = None


def _worker_client() -> httpx.AsyncClient:
    global _WORKER_HTTP
    if _WORKER_HTTP is None:
        _WORKER_HTTP = httpx.AsyncClient(
            timeout=httpx.Timeout(DEFAULT_TIMEOUT_S, connect=5.0, read=DEFAULT_TIMEOUT_S),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
    return _WORKER_HTTP


async def close_worker_client() -> None:
    global _WORKER_HTTP
    if _WORKER_HTTP is not None:
        with contextlib.suppress(Exception):
            await _WORKER_HTTP.aclose()
        _WORKER_HTTP = None


async def execute_code_on_worker(language: str, code: str, timeout: int = 8) -> dict[str, Any]:
    """Execute code in the remote sandbox. Never raises."""
    if not isinstance(code, str) or not code.strip():
        return {"success": False, "output": "", "error": "empty_code"}
    if len(code.encode("utf-8", errors="ignore")) > MAX_CODE_BYTES:
        return {"success": False, "output": "",
                "error": f"code_too_large (max {MAX_CODE_BYTES} bytes)"}
    lang = _ALLOWED_LANGUAGES.get((language or "python").strip().lower(), "python")
    try:
        tmo = int(timeout if timeout is not None else 8)
    except (TypeError, ValueError):
        tmo = 8
    tmo = max(1, min(tmo, 30))

    if not WORKER_URL:
        return {"success": False, "output": "", "error": "worker_unavailable"}

    endpoint = f"{WORKER_URL}/execute"
    headers = {"Content-Type": "application/json"}
    if WORKER_API_KEY:
        headers["Authorization"] = f"Bearer {WORKER_API_KEY}"
    payload = {"language": lang, "code": code, "timeout": tmo}

    last_error = "unknown_error"
    client = _worker_client()
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            resp = await client.post(endpoint, json=payload, headers=headers)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception as e:
                    return {"success": False, "output": "", "error": f"invalid_json: {e}"}
                if not isinstance(data, dict):
                    return {"success": False, "output": "",
                            "error": f"unexpected_payload_type: {type(data).__name__}"}
                return {"success": bool(data.get("success", False)),
                        "output": str(data.get("output", "") or ""),
                        "error": str(data.get("error", "") or "")}
            if resp.status_code in (429, 502, 503, 504):
                last_error = f"http_{resp.status_code}"
            elif 400 <= resp.status_code < 500:
                return {"success": False, "output": "",
                        "error": f"worker_client_error_{resp.status_code}: {resp.text[:200]}"}
            else:
                last_error = f"http_{resp.status_code}"
        except httpx.TimeoutException:
            last_error = "timeout"
        except httpx.ConnectError as e:
            last_error = f"connect_error: {e}"
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
        if attempt <= MAX_RETRIES:
            await asyncio.sleep(BACKOFF_BASE_S * (2 ** (attempt - 1)) + random.uniform(0, 0.15))

    return {"success": False, "output": "", "error": last_error}



# ═══════════════════════════════════════════════════════════════════════════
# §2 — Webhook pipeline
# ═══════════════════════════════════════════════════════════════════════════
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


class ExtractWebhookPayload(BaseModel):
    file_url: str = Field(..., min_length=8, max_length=2000)
    pipeline_id: str = Field(..., min_length=1, max_length=128)
    callback_url: str = Field(..., min_length=8, max_length=2000)
    workspace: str = Field(default="data", max_length=32)
    task_type: str = Field(default="extraction", max_length=32)
    max_tokens: int = Field(default=4096, ge=64, le=16384)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


def _sign(secret: str, timestamp: int, body: bytes) -> str:
    msg = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _verify_inbound(request_body: bytes, *, authorization: str | None,
                    signature_header: str | None, timestamp_header: str | None,
                    shared_token: str, shared_secret: str,
                    max_skew: int = 300) -> None:
    if not shared_token and not shared_secret:
        if _is_prod():
            logger.error("webhook_inbound_auth_unconfigured")
            raise HTTPException(status_code=503, detail="Webhook auth not configured")
        logger.warning("webhook_inbound_auth_disabled env=dev")
        return
    if shared_token and authorization:
        prefix = "bearer "
        if authorization.lower().startswith(prefix) and hmac.compare_digest(
            authorization[len(prefix):].strip(), shared_token,
        ):
            return
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
        raise HTTPException(status_code=401, detail="Signature mismatch")


# Define the blocked IP ranges
BLOCKED_RANGES = [
    ipaddress.ip_network("100.64.0.0/10"),      # CGNAT
    ipaddress.ip_network("192.0.0.0/24"),       # IETF protocol assignments
    ipaddress.ip_network("192.0.2.0/24"),       # TEST-NET-1
    ipaddress.ip_network("198.51.100.0/24"),    # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),     # TEST-NET-3
]


async def _assert_public_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise HTTPException(status_code=400, detail=f"URL scheme not allowed")
    host = parsed.hostname
    if not host:
        raise HTTPException(status_code=400, detail="URL missing host")
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"DNS resolution failed: {e}")
    valid: str | None = None
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except Exception:
            continue
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified
                or any(addr in net for net in BLOCKED_RANGES)):
            raise HTTPException(status_code=400, detail=f"Blocked non-public address: {addr}")
        valid = str(addr); break
    if not valid:
        raise HTTPException(status_code=400, detail="No valid public IP addresses resolved")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    netloc = f"[{valid}]:{port}" if ":" in valid else f"{valid}:{port}"
    return parsed._replace(netloc=netloc).geturl(), host


async def _download_file(client: httpx.AsyncClient, url: str,
                         max_bytes: int = _MAX_DOWNLOAD_BYTES) -> dict[str, Any]:
    verified_url, original_host = await _assert_public_url(url)
    filename = "download"
    mimetype = "application/octet-stream"
    total = 0
    with tempfile.NamedTemporaryFile(delete=False, suffix=".axelr-tmp") as tmpf:
        req = client.build_request("GET", verified_url, headers={"Host": original_host})
        req.extensions["sni_hostname"] = original_host.encode("utf-8")
        async with client.send(req, stream=True, timeout=_DOWNLOAD_TIMEOUT) as resp:
            resp.raise_for_status()
            cd = resp.headers.get("content-disposition", "")
            if cd:
                for part in cd.split(";"):
                    part = part.strip()
                    if part.lower().startswith("filename="):
                        _, fname = parseaddr(part)
                        if fname: filename = fname.strip('"\'')
            ctype = resp.headers.get("content-type")
            if ctype: mimetype = ctype.split(";")[0].strip()
            async for chunk in resp.aiter_bytes(64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    os.unlink(tmpf.name)
                    raise RuntimeError(f"file exceeds {max_bytes} bytes")
                tmpf.write(chunk)
    if total == 0:
        os.unlink(tmpf.name)
        raise RuntimeError("empty file")
    with open(tmpf.name, "rb") as f:
        raw = f.read()
    os.unlink(tmpf.name)
    return {"filename": filename, "mimetype": mimetype,
            "content_base64": base64.b64encode(raw).decode("ascii"), "size": total}


async def _post_callback(client: httpx.AsyncClient, callback_url: str,
                         payload: dict[str, Any], *, secret: str,
                         pipeline_id: str, qstash_token: str = "") -> bool:
    if not secret and _is_prod():
        logger.error("webhook_callback_secret_missing")
        return False
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ts = int(time.time())
    signature = _sign(secret, ts, body) if secret else ""
    headers = {"Content-Type": "application/json", "User-Agent": "Axelr-Webhook/1.0",
               "X-Axelr-Timestamp": str(ts), "X-Axelr-Pipeline-Id": pipeline_id}
    if signature:
        headers["X-Axelr-Signature"] = f"sha256={signature}"
    if qstash_token:
        try:
            publish_url = _QSTASH_PUBLISH.format(url=callback_url)
            h = {"Authorization": f"Bearer {qstash_token}",
                 "Content-Type": "application/json", "Upstash-Retries": "3"}
            for k, v in headers.items():
                if k.lower() == "content-type": continue
                h[f"Upstash-Forward-{k}"] = v
            resp = await client.post(publish_url, headers=h, content=body, timeout=10.0)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.warning("qstash_publish_failed error=%s", e)
    for attempt in range(1, _CALLBACK_RETRIES + 1):
        try:
            resp = await client.post(callback_url, headers=headers,
                                     content=body, timeout=_CALLBACK_TIMEOUT)
            if 200 <= resp.status_code < 300: return True
        except Exception as e:
            logger.warning("callback_error attempt=%s error=%s", attempt, e)
        if attempt < _CALLBACK_RETRIES:
            await asyncio.sleep(min(8, 2 ** attempt))
    return False


async def _process_job(*, route_func: RouteFunc, http_client: httpx.AsyncClient,
                       payload: ExtractWebhookPayload, callback_secret: str,
                       qstash_token: str, job_id: str) -> None:
    started = time.time()
    result: dict[str, Any] = {
        "job_id": job_id, "pipeline_id": payload.pipeline_id,
        "success": False, "text": "", "provider": None, "model": None,
        "elapsed_ms": 0.0, "error": None, "metadata": payload.metadata or {},
    }
    try:
        file_info = await _download_file(http_client, payload.file_url)
        file_info.pop("size", None)
        workspace = payload.workspace if payload.workspace in ("data", "design", "core") else "data"
        task_type = payload.task_type or ("extraction" if workspace == "data" else "frontend")
        ai_result = await route_func(
            workspace=workspace, task_type=task_type,
            prompt=f"Process the attached file for pipeline '{payload.pipeline_id}'.",
            history=[], files=[file_info], max_tokens=payload.max_tokens,
            temp=payload.temperature, tier="business", user=None, context="",
        )
        if ai_result and ai_result.get("success"):
            result.update(success=True,
                          text=str(ai_result.get("text", "")),
                          provider=ai_result.get("provider"),
                          model=ai_result.get("model_used"))
        else:
            result["error"] = "ai_route_failed"
    except HTTPException as e:
        result["error"] = f"download_rejected: {e.detail}"
    except Exception as e:
        logger.exception("webhook_job_failed job_id=%s error=%s", job_id, e)
        result["error"] = f"{type(e).__name__}: {e}"
    result["elapsed_ms"] = round((time.time() - started) * 1000, 2)
    if not await _post_callback(http_client, payload.callback_url, result,
                                secret=callback_secret,
                                pipeline_id=payload.pipeline_id,
                                qstash_token=qstash_token):
        logger.warning("callback_undelivered job_id=%s", job_id)


def make_webhook_router(route_func: RouteFunc, *,
                        http_client: httpx.AsyncClient | None = None) -> APIRouter:
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

    @router.post("/_internal/process-job", status_code=200)
    async def internal_process_job(request: Request,
                                   authorization: str | None = Header(default=None)
                                   ) -> JSONResponse:
        if not authorization or not authorization.startswith("Bearer "):
            return JSONResponse(status_code=403, content={"success": False, "error": "unauthorized"})
        if authorization.split(" ", 1)[1] != qstash_token:
            return JSONResponse(status_code=403, content={"success": False, "error": "invalid_token"})
        job = await request.json()
        await _process_job(route_func=route_func, http_client=client,
                           payload=job["payload"], callback_secret=job["callback_secret"],
                           qstash_token=qstash_token, job_id=job["job_id"])
        return JSONResponse(status_code=200, content={"success": True})

    @router.post("/extract", status_code=202)
    async def webhook_extract(
        request: Request, background_tasks: BackgroundTasks,
        payload: ExtractWebhookPayload,
        authorization: str | None = Header(default=None),
        x_axelr_signature: str | None = Header(default=None),
        x_axelr_timestamp: str | None = Header(default=None),
    ) -> JSONResponse:
        body = await request.body()
        _verify_inbound(body, authorization=authorization,
                        signature_header=x_axelr_signature,
                        timestamp_header=x_axelr_timestamp,
                        shared_token=inbound_token, shared_secret=inbound_secret)
        await _assert_public_url(payload.file_url)
        await _assert_public_url(payload.callback_url)
        job_id = hashlib.sha256(f"{time.time_ns()}|{payload.pipeline_id}".encode()).hexdigest()[:16]
        if qstash_token:
            base = (os.getenv("PUBLIC_API_URL", "").rstrip("/")
                    or str(request.base_url).rstrip("/"))
            process_url = f"{base}{router.prefix}/_internal/process-job"
            job_body = json.dumps({"payload": payload.model_dump(),
                                   "callback_secret": callback_secret,
                                   "job_id": job_id},
                                  separators=(",", ":")).encode("utf-8")
            try:
                h = {"Authorization": f"Bearer {qstash_token}",
                     "Content-Type": "application/json",
                     "Upstash-Retries": "3",
                     "Upstash-Forward-Host": str(request.url.host)}
                await client.post(_QSTASH_PUBLISH.format(url=process_url),
                                  headers=h, content=job_body, timeout=10.0)
                return JSONResponse(status_code=202,
                                    content={"success": True, "job_id": job_id,
                                             "status": "queued"})
            except Exception as e:
                logger.warning("qstash_enqueue_failed_falling_back job_id=%s error=%s", job_id, e)
        background_tasks.add_task(_process_job, route_func=route_func,
                                  http_client=client, payload=payload,
                                  callback_secret=callback_secret,
                                  qstash_token=qstash_token, job_id=job_id)
        return JSONResponse(status_code=202,
                            content={"success": True, "job_id": job_id, "status": "queued"})

    return router


__all__ = [
    # worker
    "execute_code_on_worker", "close_worker_client",
    # webhook
    "ExtractWebhookPayload", "make_webhook_router",
=======
# core/io_pipeline.py
"""
AXELR — I/O Pipeline (webhook inbound · worker sandbox outbound)
================================================================
Both talk to an external service. Both must never raise. Both retry with
bounded backoff. Both sign or verify what they send. One file.

    §1 Worker sandbox     execute_code_on_worker (outbound, retried)
    §2 Webhook pipeline   inbound HTTP router + SSRF-protected download
"""
from __future__ import annotations
import asyncio
import base64
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import random
import socket
import tempfile
import time
from collections.abc import Awaitable, Callable
from email.utils import parseaddr
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("axelr.io")


# ═══════════════════════════════════════════════════════════════════════════
# §1 — Worker sandbox
# ═══════════════════════════════════════════════════════════════════════════
WORKER_URL = (os.getenv("WORKER_URL") or os.getenv("DATA_WORKER_URL") or "").strip().rstrip("/")
WORKER_API_KEY = (os.getenv("WORKER_API_KEY") or "").strip()
DEFAULT_TIMEOUT_S = float(os.getenv("WORKER_TIMEOUT_SECONDS", "10.0"))
MAX_RETRIES = max(0, int(os.getenv("WORKER_MAX_RETRIES", "2")))
BACKOFF_BASE_S = 0.35
MAX_CODE_BYTES = 64 * 1024

_ALLOWED_LANGUAGES = {
    "python": "python", "py": "python", "python3": "python",
    "javascript": "javascript", "js": "javascript", "node": "javascript",
    "typescript": "javascript", "ts": "javascript",
}

_WORKER_HTTP: httpx.AsyncClient | None = None


def _worker_client() -> httpx.AsyncClient:
    global _WORKER_HTTP
    if _WORKER_HTTP is None:
        _WORKER_HTTP = httpx.AsyncClient(
            timeout=httpx.Timeout(DEFAULT_TIMEOUT_S, connect=5.0, read=DEFAULT_TIMEOUT_S),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
    return _WORKER_HTTP


async def close_worker_client() -> None:
    global _WORKER_HTTP
    if _WORKER_HTTP is not None:
        with contextlib.suppress(Exception):
            await _WORKER_HTTP.aclose()
        _WORKER_HTTP = None


async def execute_code_on_worker(language: str, code: str, timeout: int = 8) -> dict[str, Any]:
    """Execute code in the remote sandbox. Never raises."""
    if not isinstance(code, str) or not code.strip():
        return {"success": False, "output": "", "error": "empty_code"}
    if len(code.encode("utf-8", errors="ignore")) > MAX_CODE_BYTES:
        return {"success": False, "output": "",
                "error": f"code_too_large (max {MAX_CODE_BYTES} bytes)"}
    lang = _ALLOWED_LANGUAGES.get((language or "python").strip().lower(), "python")
    try:
        tmo = int(timeout if timeout is not None else 8)
    except (TypeError, ValueError):
        tmo = 8
    tmo = max(1, min(tmo, 30))

    if not WORKER_URL:
        return {"success": False, "output": "", "error": "worker_unavailable"}

    endpoint = f"{WORKER_URL}/execute"
    headers = {"Content-Type": "application/json"}
    if WORKER_API_KEY:
        headers["Authorization"] = f"Bearer {WORKER_API_KEY}"
    payload = {"language": lang, "code": code, "timeout": tmo}

    last_error = "unknown_error"
    client = _worker_client()
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            resp = await client.post(endpoint, json=payload, headers=headers)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception as e:
                    return {"success": False, "output": "", "error": f"invalid_json: {e}"}
                if not isinstance(data, dict):
                    return {"success": False, "output": "",
                            "error": f"unexpected_payload_type: {type(data).__name__}"}
                return {"success": bool(data.get("success", False)),
                        "output": str(data.get("output", "") or ""),
                        "error": str(data.get("error", "") or "")}
            if resp.status_code in (429, 502, 503, 504):
                last_error = f"http_{resp.status_code}"
            elif 400 <= resp.status_code < 500:
                return {"success": False, "output": "",
                        "error": f"worker_client_error_{resp.status_code}: {resp.text[:200]}"}
            else:
                last_error = f"http_{resp.status_code}"
        except httpx.TimeoutException:
            last_error = "timeout"
        except httpx.ConnectError as e:
            last_error = f"connect_error: {e}"
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
        if attempt <= MAX_RETRIES:
            await asyncio.sleep(BACKOFF_BASE_S * (2 ** (attempt - 1)) + random.uniform(0, 0.15))

    return {"success": False, "output": "", "error": last_error}



# ═══════════════════════════════════════════════════════════════════════════
# §2 — Webhook pipeline
# ═══════════════════════════════════════════════════════════════════════════
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


class ExtractWebhookPayload(BaseModel):
    file_url: str = Field(..., min_length=8, max_length=2000)
    pipeline_id: str = Field(..., min_length=1, max_length=128)
    callback_url: str = Field(..., min_length=8, max_length=2000)
    workspace: str = Field(default="data", max_length=32)
    task_type: str = Field(default="extraction", max_length=32)
    max_tokens: int = Field(default=4096, ge=64, le=16384)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


def _sign(secret: str, timestamp: int, body: bytes) -> str:
    msg = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _verify_inbound(request_body: bytes, *, authorization: str | None,
                    signature_header: str | None, timestamp_header: str | None,
                    shared_token: str, shared_secret: str,
                    max_skew: int = 300) -> None:
    if not shared_token and not shared_secret:
        if _is_prod():
            logger.error("webhook_inbound_auth_unconfigured")
            raise HTTPException(status_code=503, detail="Webhook auth not configured")
        logger.warning("webhook_inbound_auth_disabled env=dev")
        return
    if shared_token and authorization:
        prefix = "bearer "
        if authorization.lower().startswith(prefix) and hmac.compare_digest(
            authorization[len(prefix):].strip(), shared_token,
        ):
            return
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


async def _assert_public_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise HTTPException(status_code=400, detail=f"URL scheme not allowed")
    host = parsed.hostname
    if not host:
        raise HTTPException(status_code=400, detail="URL missing host")
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"DNS resolution failed: {e}")
    valid: str | None = None
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except Exception:
            continue
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            raise HTTPException(status_code=400, detail=f"Blocked non-public address: {addr}")
        valid = str(addr); break
    if not valid:
        raise HTTPException(status_code=400, detail="No valid public IP addresses resolved")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    netloc = f"[{valid}]:{port}" if ":" in valid else f"{valid}:{port}"
    return parsed._replace(netloc=netloc).geturl(), host


async def _download_file(client: httpx.AsyncClient, url: str,
                         max_bytes: int = _MAX_DOWNLOAD_BYTES) -> dict[str, Any]:
    verified_url, original_host = await _assert_public_url(url)
    filename = "download"
    mimetype = "application/octet-stream"
    total = 0
    with tempfile.NamedTemporaryFile(delete=False, suffix=".axelr-tmp") as tmpf:
        req = client.build_request("GET", verified_url, headers={"Host": original_host})
        req.extensions["sni_hostname"] = original_host.encode("utf-8")
        async with client.send(req, stream=True, timeout=_DOWNLOAD_TIMEOUT) as resp:
            resp.raise_for_status()
            cd = resp.headers.get("content-disposition", "")
            if cd:
                for part in cd.split(";"):
                    part = part.strip()
                    if part.lower().startswith("filename="):
                        _, fname = parseaddr(part)
                        if fname: filename = fname.strip('"\'')
            ctype = resp.headers.get("content-type")
            if ctype: mimetype = ctype.split(";")[0].strip()
            async for chunk in resp.aiter_bytes(64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    os.unlink(tmpf.name)
                    raise RuntimeError(f"file exceeds {max_bytes} bytes")
                tmpf.write(chunk)
    if total == 0:
        os.unlink(tmpf.name)
        raise RuntimeError("empty file")
    with open(tmpf.name, "rb") as f:
        raw = f.read()
    os.unlink(tmpf.name)
    return {"filename": filename, "mimetype": mimetype,
            "content_base64": base64.b64encode(raw).decode("ascii"), "size": total}


async def _post_callback(client: httpx.AsyncClient, callback_url: str,
                         payload: dict[str, Any], *, secret: str,
                         pipeline_id: str, qstash_token: str = "") -> bool:
    if not secret and _is_prod():
        logger.error("webhook_callback_secret_missing")
        return False
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ts = int(time.time())
    signature = _sign(secret, ts, body) if secret else ""
    headers = {"Content-Type": "application/json", "User-Agent": "Axelr-Webhook/1.0",
               "X-Axelr-Timestamp": str(ts), "X-Axelr-Pipeline-Id": pipeline_id}
    if signature:
        headers["X-Axelr-Signature"] = f"sha256={signature}"
    if qstash_token:
        try:
            publish_url = _QSTASH_PUBLISH.format(url=callback_url)
            h = {"Authorization": f"Bearer {qstash_token}",
                 "Content-Type": "application/json", "Upstash-Retries": "3"}
            for k, v in headers.items():
                if k.lower() == "content-type": continue
                h[f"Upstash-Forward-{k}"] = v
            resp = await client.post(publish_url, headers=h, content=body, timeout=10.0)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.warning("qstash_publish_failed error=%s", e)
    for attempt in range(1, _CALLBACK_RETRIES + 1):
        try:
            resp = await client.post(callback_url, headers=headers,
                                     content=body, timeout=_CALLBACK_TIMEOUT)
            if 200 <= resp.status_code < 300: return True
        except Exception as e:
            logger.warning("callback_error attempt=%s error=%s", attempt, e)
        if attempt < _CALLBACK_RETRIES:
            await asyncio.sleep(min(8, 2 ** attempt))
    return False


async def _process_job(*, route_func: RouteFunc, http_client: httpx.AsyncClient,
                       payload: ExtractWebhookPayload, callback_secret: str,
                       qstash_token: str, job_id: str) -> None:
    started = time.time()
    result: dict[str, Any] = {
        "job_id": job_id, "pipeline_id": payload.pipeline_id,
        "success": False, "text": "", "provider": None, "model": None,
        "elapsed_ms": 0.0, "error": None, "metadata": payload.metadata or {},
    }
    try:
        file_info = await _download_file(http_client, payload.file_url)
        file_info.pop("size", None)
        workspace = payload.workspace if payload.workspace in ("data", "design", "core") else "data"
        task_type = payload.task_type or ("extraction" if workspace == "data" else "frontend")
        ai_result = await route_func(
            workspace=workspace, task_type=task_type,
            prompt=f"Process the attached file for pipeline '{payload.pipeline_id}'.",
            history=[], files=[file_info], max_tokens=payload.max_tokens,
            temp=payload.temperature, tier="business", user=None, context="",
        )
        if ai_result and ai_result.get("success"):
            result.update(success=True,
                          text=str(ai_result.get("text", "")),
                          provider=ai_result.get("provider"),
                          model=ai_result.get("model_used"))
        else:
            result["error"] = "ai_route_failed"
    except HTTPException as e:
        result["error"] = f"download_rejected: {e.detail}"
    except Exception as e:
        logger.exception("webhook_job_failed job_id=%s error=%s", job_id, e)
        result["error"] = f"{type(e).__name__}: {e}"
    result["elapsed_ms"] = round((time.time() - started) * 1000, 2)
    if not await _post_callback(http_client, payload.callback_url, result,
                                secret=callback_secret,
                                pipeline_id=payload.pipeline_id,
                                qstash_token=qstash_token):
        logger.warning("callback_undelivered job_id=%s", job_id)


def make_webhook_router(route_func: RouteFunc, *,
                        http_client: httpx.AsyncClient | None = None) -> APIRouter:
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

    @router.post("/_internal/process-job", status_code=200)
    async def internal_process_job(request: Request,
                                   authorization: str | None = Header(default=None)
                                   ) -> JSONResponse:
        if not authorization or not authorization.startswith("Bearer "):
            return JSONResponse(status_code=403, content={"success": False, "error": "unauthorized"})
        if authorization.split(" ", 1)[1] != qstash_token:
            return JSONResponse(status_code=403, content={"success": False, "error": "invalid_token"})
        job = await request.json()
        await _process_job(route_func=route_func, http_client=client,
                           payload=job["payload"], callback_secret=job["callback_secret"],
                           qstash_token=qstash_token, job_id=job["job_id"])
        return JSONResponse(status_code=200, content={"success": True})

    @router.post("/extract", status_code=202)
    async def webhook_extract(
        request: Request, background_tasks: BackgroundTasks,
        payload: ExtractWebhookPayload,
        authorization: str | None = Header(default=None),
        x_axelr_signature: str | None = Header(default=None),
        x_axelr_timestamp: str | None = Header(default=None),
    ) -> JSONResponse:
        body = await request.body()
        _verify_inbound(body, authorization=authorization,
                        signature_header=x_axelr_signature,
                        timestamp_header=x_axelr_timestamp,
                        shared_token=inbound_token, shared_secret=inbound_secret)
        await _assert_public_url(payload.file_url)
        await _assert_public_url(payload.callback_url)
        job_id = hashlib.sha256(f"{time.time_ns()}|{payload.pipeline_id}".encode()).hexdigest()[:16]
        if qstash_token:
            base = (os.getenv("PUBLIC_API_URL", "").rstrip("/")
                    or str(request.base_url).rstrip("/"))
            process_url = f"{base}{router.prefix}/_internal/process-job"
            job_body = json.dumps({"payload": payload.model_dump(),
                                   "callback_secret": callback_secret,
                                   "job_id": job_id},
                                  separators=(",", ":")).encode("utf-8")
            try:
                h = {"Authorization": f"Bearer {qstash_token}",
                     "Content-Type": "application/json",
                     "Upstash-Retries": "3",
                     "Upstash-Forward-Host": str(request.url.host)}
                await client.post(_QSTASH_PUBLISH.format(url=process_url),
                                  headers=h, content=job_body, timeout=10.0)
                return JSONResponse(status_code=202,
                                    content={"success": True, "job_id": job_id,
                                             "status": "queued"})
            except Exception as e:
                logger.warning("qstash_enqueue_failed_falling_back job_id=%s error=%s", job_id, e)
        background_tasks.add_task(_process_job, route_func=route_func,
                                  http_client=client, payload=payload,
                                  callback_secret=callback_secret,
                                  qstash_token=qstash_token, job_id=job_id)
        return JSONResponse(status_code=202,
                            content={"success": True, "job_id": job_id, "status": "queued"})

    return router


__all__ = [
    # worker
    "execute_code_on_worker", "close_worker_client",
    # webhook
    "ExtractWebhookPayload", "make_webhook_router",
>>>>>>> 79d9e927692298188bf012e464b8c80c2d251657
]