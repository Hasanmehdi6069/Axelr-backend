import asyncio
import hashlib
import hmac
import os
import time
from typing import Any, Literal

import httpx
from fastapi import HTTPException

HEAVY_WORKER_URL = os.getenv("HEAVY_WORKER_URL", "http://localhost:8080")
WORKER_SECRET = os.getenv("HEAVY_WORKER_SECRET", "a-very-secret-key")

class CircuitBreaker:
    def __init__(self, failure_threshold: int, recovery_timeout: int):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.state: Literal["closed", "open", "half_open"] = "closed"
        self.last_failure_time = 0

    async def __aenter__(self):
        if self.state == "open":
            if time.time() - self.last_failure_time > self.recovery_timeout:
                self.state = "half_open"
            else:
                raise HTTPException(status_code=503, detail={"success": False, "code": "WORKER_UNAVAILABLE", "retry_after": 30})

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.failure_count += 1
            if self.state == "half_open" or self.failure_count >= self.failure_threshold:
                self.state = "open"
                self.last_failure_time = time.time()
        else:
            if self.state == "half_open":
                self.state = "closed"
                self.failure_count = 0

_circuit_breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=30)

async def execute_code_on_worker(language: str, code: str, timeout: int) -> dict[str, Any]:
    async with _circuit_breaker:
        async with httpx.AsyncClient(timeout=8.0) as client:
            for attempt in range(2):  # 1 initial + 1 retry
                try:
                    payload = {"language": language, "code": code, "timeout": timeout}
                    body = str(payload).encode()
                    signature = hmac.new(WORKER_SECRET.encode(), body, hashlib.sha256).hexdigest()
                    headers = {"X-HMAC-Signature": signature}

                    response = await client.post(
                        f"{HEAVY_WORKER_URL}/api/execute-code",
                        json=payload,
                        headers=headers,
                    )
                    response.raise_for_status()
                    return response.json()
                except (httpx.RequestError, httpx.HTTPStatusError) as e:
                    if attempt == 1:
                        raise HTTPException(status_code=503, detail={"success": False, "code": "WORKER_UNAVAILABLE", "retry_after": 30}) from e
                    await asyncio.sleep(0.5) # wait before retrying
    return {"success": False, "code": "WORKER_UNAVAILABLE", "retry_after": 30}


async def execute_touch_fix_on_worker(
    full_code: str,
    error_block: str,
    error_message: str,
    language: str,
    tier: str,
    user: dict | None,
) -> str:
    async with _circuit_breaker:
        async with httpx.AsyncClient(timeout=15.0) as client: # Longer timeout for AI
            for attempt in range(2):
                try:
                    payload = {
                        "full_code": full_code,
                        "error_block": error_block,
                        "error_message": error_message,
                        "language": language,
                        "tier": tier,
                        "user": user,
                    }
                    body = str(payload).encode()
                    signature = hmac.new(WORKER_SECRET.encode(), body, hashlib.sha256).hexdigest()
                    headers = {"X-HMAC-Signature": signature}

                    response = await client.post(
                        f"{HEAVY_WORKER_URL}/api/touch-fix",
                        json=payload,
                        headers=headers,
                    )
                    response.raise_for_status()
                    # The worker returns the full code, not a JSON object
                    return response.text
                except (httpx.RequestError, httpx.HTTPStatusError):
                    if attempt == 1:
                        # On failure, we return the original code to not break the user flow
                        return full_code
                    await asyncio.sleep(0.5)
    return full_code