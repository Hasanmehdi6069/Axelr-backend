import logging
import time
from collections.abc import AsyncGenerator

import httpx

logger = logging.getLogger("AxelrGateway")
logger.setLevel(logging.INFO)

class CircuitBreakerOpenException(Exception):
    """Raised when calls to a provider are temporarily suspended."""

class ProviderNode:
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 12.0,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0
    ):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout_seconds
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        
        # State tracking
        self.failure_count = 0
        self.last_failure_time = 0.0
        self.state = "CLOSED"  # CLOSED, OPEN, HALF-OPEN

    def is_available(self) -> bool:
        if not self.api_key or not self.base_url:
            return False
            
        now = time.time()
        if self.state == "OPEN":
            if now - self.last_failure_time > self.cooldown_seconds:
                logger.info(f"Provider [{self.name}] transitioning to HALF-OPEN.")
                self.state = "HALF-OPEN"
                return True
            return False
        return True

    def record_success(self):
        self.failure_count = 0
        self.state = "CLOSED"

    def record_failure(self, status_code: int | None = None):
        self.failure_count += 1
        self.last_failure_time = time.time()
        
        # Immediate trip for auth failures or missing configs
        if status_code in (401, 403) or self.failure_count >= self.failure_threshold:
            self.state = "OPEN"
            logger.warning(
                f"Provider [{self.name}] circuit OPENED. Status: {status_code}, Failures: {self.failure_count}."
            )

class ProviderRouter:
    def __init__(self, providers: list[ProviderNode]):
        self.providers = providers

    def get_healthy_providers(self) -> list[ProviderNode]:
        return [p for p in self.providers if p.is_available()]

    async def execute_stream(
        self,
        messages: list[dict[str, str]],
        workspace: str = "core"
    ) -> AsyncGenerator[str, None]:
        healthy_pool = self.get_healthy_providers()
        
        if not healthy_pool:
            logger.error("All AI inference providers currently unavailable.")
            yield "System alert: AI execution engines are currently saturated or under maintenance. Please retry in a few moments."
            return

        client_headers = {"Content-Type": "application/json"}

        for provider in healthy_pool:
            logger.info(f"Attempting inference via [{provider.name}] on model [{provider.model}]")
            headers = {
                **client_headers,
                "Authorization": f"Bearer {provider.api_key}"
            }
            payload = {
                "model": provider.model,
                "messages": messages,
                "stream": True,
                "temperature": 0.3 if workspace == "data" else 0.7
            }

            try:
                async with httpx.AsyncClient(timeout=provider.timeout) as client:
                    async with client.stream(
                        "POST",
                        f"{provider.base_url}/chat/completions",
                        json=payload,
                        headers=headers
                    ) as response:
                        
                        if response.status_code != 200:
                            logger.warning(
                                f"Provider [{provider.name}] failed with HTTP {response.status_code}"
                            )
                            provider.record_failure(status_code=response.status_code)
                            continue  # Cascade to next provider

                        # Stream tokens incrementally
                        stream_successful = False
                        async for line in response.aiter_lines():
                            if line.startswith("data: "):
                                data = line[6:].strip()
                                if data == "[DONE]":
                                    break
                                yield data
                                stream_successful = True
                        
                        if stream_successful:
                            provider.record_success()
                            return

            except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as err:
                logger.error(f"Network error on provider [{provider.name}]: {err!s}")
                provider.record_failure()
                continue
            except Exception as unhandled_err:
                logger.error(f"Unexpected fault on provider [{provider.name}]: {unhandled_err!s}")
                provider.record_failure()
                continue

        # Final local safety fallback
        yield "Execution fallback: Upstream providers could not fulfill the streaming session. Verification logs captured."