# api/routes_indexer.py
"""
AXELR — Repo Indexer Offload (F27)
====================================
The API process must never hold chunk text arrays. Two endpoints:

    POST /api/repo/enqueue   → write job to Redis list; return job_id
    GET  /api/repo/job/{id}  → check status

The actual indexing runs inside a QStash-triggered worker hitting
`POST /api/repo/_worker/run` which imports the heavy modules lazily.
On the API container the modules are never imported → ~40 MB saved
during indexing bursts.

Auth on _worker/run is a shared token (`INDEXER_WORKER_TOKEN`).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from .auth import get_current_user
from .state import get_redis_cache, set_redis_cache, state

logger = logging.getLogger("axelr.indexer")
router = APIRouter(tags=["indexer"])

_JOB_TTL_S = 86_400
_JOB_PREFIX = "axelr:repo:job"
_QUEUE_KEY = "axelr:repo:queue"
_QSTASH_PUBLISH = "https://qstash.upstash.io/v2/publish/{url}"


class RepoEnqueueRequest(BaseModel):
    repo_url: str
    branch: str | None = None
    force: bool = False


def _job_key(job_id: str) -> str:
    return f"{_JOB_PREFIX}:{job_id}"


@router.post("/api/repo/enqueue")
async def enqueue_repo_index(
    data: RepoEnqueueRequest, user: dict = Depends(get_current_user),
):
    """Return a job_id immediately. The heavy work runs off-container."""
    if state.redis_client is None:
        raise HTTPException(status_code=503, detail="Queue unavailable")

    job_id = hashlib.sha256(
        f"{time.time_ns()}|{data.repo_url}|{data.branch or ''}|{secrets.token_hex(8)}"
        .encode()
    ).hexdigest()[:16]

    record: dict[str, Any] = {
        "job_id": job_id,
        "repo_url": data.repo_url,
        "branch": data.branch,
        "force": data.force,
        "user_id": str(user["_id"]),
        "status": "queued",
        "created_at": time.time(),
    }
    await set_redis_cache(_job_key(job_id), record, ttl=_JOB_TTL_S)

    try:
        await state.redis_client.lpush(_QUEUE_KEY, job_id)
        await state.redis_client.expire(_QUEUE_KEY, _JOB_TTL_S)
    except Exception as e:
        logger.warning("repo_enqueue_redis_failed error=%s", e)
        raise HTTPException(status_code=503, detail="Queue write failed")

    # Optionally push to QStash for edge-triggered execution.
    qstash = (os.getenv("QSTASH_TOKEN") or "").strip()
    public_url = (os.getenv("PUBLIC_API_URL") or "").rstrip("/")
    if qstash and public_url:
        try:
            from .config import get_http_client
            body = json.dumps({"job_id": job_id}).encode()
            headers = {
                "Authorization": f"Bearer {qstash}",
                "Content-Type": "application/json",
                "Upstash-Retries": "3",
            }
            await get_http_client().post(
                _QSTASH_PUBLISH.format(url=f"{public_url}/api/repo/_worker/run"),
                headers=headers, content=body, timeout=8.0,
            )
        except Exception as e:
            logger.debug("repo_qstash_publish_failed error=%s", e)

    return {"success": True, "job_id": job_id, "status": "queued"}


@router.get("/api/repo/job/{job_id}")
async def get_repo_job(
    job_id: str, user: dict = Depends(get_current_user),
):
    record = await get_redis_cache(_job_key(job_id))
    if not record:
        raise HTTPException(status_code=404, detail="Job not found")
    if record.get("user_id") != str(user["_id"]):
        raise HTTPException(status_code=403, detail="Not your job")
    return {"success": True, **record}


@router.post("/api/repo/_worker/run")
async def repo_worker_run(
    payload: dict,
    x_worker_token: str | None = Header(default=None),
):
    """Worker-only endpoint. Loads heavy modules lazily, then releases them."""
    expected = (os.getenv("INDEXER_WORKER_TOKEN") or "").strip()
    if not expected or x_worker_token != expected:
        raise HTTPException(status_code=403, detail="Worker token invalid")

    job_id = str(payload.get("job_id") or "")
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id required")

    record = await get_redis_cache(_job_key(job_id))
    if not record:
        raise HTTPException(status_code=404, detail="Job not found")

    record["status"] = "running"
    record["started_at"] = time.time()
    await set_redis_cache(_job_key(job_id), record, ttl=_JOB_TTL_S)

    try:
        # Lazy import — heavy modules exist only for the duration of this call
        from core.memory import RepoIndexer  # noqa: WPS433
        indexer = RepoIndexer(redis_client=state.redis_client)
        try:
            stats = await indexer.index_repo(
                record["repo_url"],
                record.get("branch"),
                force=bool(record.get("force")),
            )
            record["status"] = "done"
            record["stats"] = stats.to_dict()
        finally:
            await indexer.close()
    except Exception as e:
        logger.warning("repo_worker_failed job_id=%s error=%s", job_id, e)
        record["status"] = "failed"
        record["error"] = str(e)[:300]

    record["finished_at"] = time.time()
    await set_redis_cache(_job_key(job_id), record, ttl=_JOB_TTL_S)
    return {"success": True, "job_id": job_id, "status": record["status"]}


__all__ = ["router"]