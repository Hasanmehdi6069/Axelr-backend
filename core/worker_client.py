# core/worker_client.py
"""
AXELR AI — Remote sandbox execution client.
============================================
Public API:
    await execute_code_on_worker(language, code, timeout=8) -> dict

Return shape (always, never raises):
    {"success": bool, "output": str, "error": str}

Design:
    * Posts to DATA_WORKER_URL (or WORKER_URL) if configured.
    * Falls back to a structured "worker_unavailable" response if not.
    * Bounded retries with exponential backoff + jitter.
    * Never propagates exceptions to the caller — the sandbox is optional.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from typing import Any

import httpx

logger = logging.getLogger("axelr.code")


WORKER_URL = (
    os.getenv("WORKER_URL")
    or os.getenv("DATA_WORKER_URL")
    or ""
).strip().rstrip("/")

WORKER_API_KEY = (os.getenv("WORKER_API_KEY") or "").strip()

DEFAULT_TIMEOUT_S = float(os.getenv("WORKER_TIMEOUT_SECONDS", "10.0"))
MAX_RETRIES       = max(0, int(os.getenv("WORKER_MAX_RETRIES", "2")))
BACKOFF_BASE_S    = 0.35


# HTTP client (lazy, module-scoped)
_HTTP: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _HTTP
    if _HTTP is None:
        _HTTP = httpx.AsyncClient(
            timeout=httpx.Timeout(DEFAULT_TIMEOUT_S, connect=5.0, read=DEFAULT_TIMEOUT_S),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
    return _HTTP


async def close_worker_client() -> None:
    """Optional graceful shutdown helper. Safe to call multiple times."""
    global _HTTP
    if _HTTP is not None:
        try:
            await _HTTP.aclose()
        except Exception:
            pass
        _HTTP = None


# Payload validation
_ALLOWED_LANGUAGES = {
    "python": "python",
    "py": "python",
    "python3": "python",
    "javascript": "javascript",
    "js": "javascript",
    "node": "javascript",
    "typescript": "javascript",
    "ts": "javascript",
}

MAX_CODE_BYTES = 64 * 1024  # 64 KB hard cap


def _normalize_language(language: str) -> str:
    lang = (language or "python").strip().lower()
    return _ALLOWED_LANGUAGES.get(lang, "python")


def _normalize_timeout(timeout: int | float | None) -> int:
    try:
        t = int(timeout if timeout is not None else 8)
    except (TypeError, ValueError):
        t = 8
    return max(1, min(t, 30))


async def execute_code_on_worker(
    language: str,
    code: str,
    timeout: int = 8,
) -> dict[str, Any]:
    """
    Execute `code` in the remote sandbox.

    Always returns a dict — never raises:
        {"success": bool, "output": str, "error": str}
    """
    # ---- 1. Validate input ---------------------------------------------------
    if not isinstance(code, str) or not code.strip():
        return {"success": False, "output": "", "error": "empty_code"}

    if len(code.encode("utf-8", errors="ignore")) > MAX_CODE_BYTES:
        return {
            "success": False,
            "output": "",
            "error": f"code_too_large (max {MAX_CODE_BYTES} bytes)",
        }

    lang = _normalize_language(language)
    tmo = _normalize_timeout(timeout)

    # ---- 2. No worker configured → structured failure ------------------------
    if not WORKER_URL:
        logger.debug("worker_client_disabled reason=no_worker_url")
        return {"success": False, "output": "", "error": "worker_unavailable"}

    # ---- 3. Build request ----------------------------------------------------
    endpoint = f"{WORKER_URL}/execute"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if WORKER_API_KEY:
        headers["Authorization"] = f"Bearer {WORKER_API_KEY}"

    payload = {"language": lang, "code": code, "timeout": tmo}

    # ---- 4. Retry loop -------------------------------------------------------
    last_error: str = "unknown_error"
    client = _client()

    for attempt in range(1, MAX_RETRIES + 2):  # 1-based, includes final attempt
        try:
            resp = await client.post(endpoint, json=payload, headers=headers)

            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception as e:
                    last_error = f"invalid_json: {e}"
                    logger.warning("worker_invalid_json", error=str(e))
                    break  # do not retry malformed responses

                if not isinstance(data, dict):
                    return {
                        "success": False,
                        "output": "",
                        "error": f"unexpected_payload_type: {type(data).__name__}",
                    }

                return {
                    "success": bool(data.get("success", False)),
                    "output":  str(data.get("output", "") or ""),
                    "error":   str(data.get("error",  "") or ""),
                }

            if resp.status_code in (429, 502, 503, 504):
                last_error = f"http_{resp.status_code}"
                logger.warning(
                    "worker_transient_error",
                    status=resp.status_code,
                    attempt=attempt,
                )
            elif 400 <= resp.status_code < 500:
                # Client error — do NOT retry
                return {
                    "success": False,
                    "output": "",
                    "error": f"worker_client_error_{resp.status_code}: {resp.text[:200]}",
                }
            else:
                last_error = f"http_{resp.status_code}"
                logger.warning(
                    "worker_server_error",
                    status=resp.status_code,
                    attempt=attempt,
                )

        except httpx.TimeoutException:
            last_error = "timeout"
            logger.warning("worker_timeout", attempt=attempt, timeout_s=DEFAULT_TIMEOUT_S)
        except httpx.ConnectError as e:
            last_error = f"connect_error: {e}"
            logger.warning("worker_connect_error", error=str(e), attempt=attempt)
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            logger.warning("worker_unexpected_error", error=str(e), attempt=attempt)

        # Backoff before next attempt (unless this was the last one)
        if attempt <= MAX_RETRIES:
            jitter = random.uniform(0, 0.15)
            delay = BACKOFF_BASE_S * (2 ** (attempt - 1)) + jitter
            await asyncio.sleep(delay)

    # ---- 5. All attempts exhausted ------------------------------------------
    logger.error("worker_all_attempts_failed", error=last_error, retries=MAX_RETRIES)
    return {"success": False, "output": "", "error": last_error}


__all__ = ["execute_code_on_worker", "close_worker_client"]