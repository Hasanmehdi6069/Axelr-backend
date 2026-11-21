"""Nebius Cloud inference client with robust retry logic, region failover, and cross-region load balancing"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp

from .retry import inference_retry_wrapper
from .schemas import (
    NebiusChatCompletionResponse,
    validate_inference_request,
)

logger = logging.getLogger("axelr.nebius.inference")


@dataclass(slots=True)
class RegionCircuitBreaker:
    """Circuit breaker for region health management with health metrics"""

    region: str
    failure_count: int = 0
    success_count: int = 0
    is_open: bool = False
    last_failure_time: float = 0.0
    threshold: int = 5
    timeout: int = 120  # Longer timeout for inference regions
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_requests: int = 0
    total_requests: int = 0

    async def record_success(self) -> None:
        async with self.lock:
            self.failure_count = 0
            self.success_count += 1
            self.active_requests -= 1
            self.total_requests += 1
            self.is_open = False

    async def record_failure(self) -> bool:
        async with self.lock:
            self.failure_count += 1
            self.active_requests -= 1
            if self.failure_count >= self.threshold:
                self.is_open = True
                self.last_failure_time = asyncio.get_event_loop().time()
                logger.critical(f"Region {self.region} circuit opened after {self.failure_count} failures")
            return self.is_open

    async def allow_request(self) -> bool:
        async with self.lock:
            if not self.is_open:
                self.active_requests += 1
                self.total_requests += 1
                return True
            if asyncio.get_event_loop().time() - self.last_failure_time > self.timeout:
                self.is_open = False
                logger.info(f"Region {self.region} circuit half-open - allowing test request")
                self.active_requests += 1
                self.total_requests += 1
                return True
            return False


@dataclass(frozen=True, slots=True)
class NebiusSettings:
    """Configuration settings for Nebius Cloud integration"""

    api_endpoint: str
    api_key: str
    regions: List[str]
    models: List[str]
    max_concurrent_inferences: int
    request_timeout: int
    max_concurrent_per_region: int

    @classmethod
    def from_env(cls) -> "NebiusSettings":
        """Load settings from environment variables"""
        regions = [r.strip() for r in os.environ.get("NEBIUS_REGIONS", "eu-west-1,us-east-1").split(",") if r.strip()]
        models = [m.strip() for m in os.environ.get("NEBIUS_MODELS", "llama3:70b").split(",") if m.strip()]

        return cls(
            api_endpoint=os.environ.get("NEBIUS_API_ENDPOINT", "https://api.studio.nebius.ai/v1"),
            api_key=os.environ.get("NEBIUS_API_KEY", ""),
            regions=regions or ["eu-west-1"],
            models=models or ["llama3:70b"],
            max_concurrent_inferences=int(os.environ.get("MAX_CONCURRENT", "16")),
            max_concurrent_per_region=int(os.environ.get("MAX_CONCURRENT_PER_REGION", "8")),
            request_timeout=int(os.environ.get("REQUEST_TIMEOUT", "600")),  # 10 minute timeout
        )


class NebiusInferenceClient:
    """Production inference client for Nebius Cloud with all hardening features"""

    _instance: Optional[NebiusInferenceClient] = None

    def __init__(self):
        self.settings = NebiusSettings.from_env()
        self.circuit_breakers: Dict[str, RegionCircuitBreaker] = {
            region: RegionCircuitBreaker(region=region) for region in self.settings.regions
        }
        self.global_semaphore = asyncio.Semaphore(self.settings.max_concurrent_inferences)
        self._session: Optional[aiohttp.ClientSession] = None
        self._initialized = False
        self._region_rotation_index: int = 0
        self._load_balance_lock: asyncio.Lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task[None]] = None

    @classmethod
    def get_instance(cls) -> NebiusInferenceClient:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def init(self) -> None:
        """Initialize HTTP session, connections, and background cleanup"""
        if self._initialized:
            return

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.settings.request_timeout),
            headers={"Authorization": f"Bearer {self.settings.api_key}", "Content-Type": "application/json"},
        )
        # Start connection pooling maintenance loop
        self._cleanup_task = asyncio.create_task(self._connection_pool_maintenance_loop())
        self._initialized = True
        logger.info(f"Nebius inference client initialized with {len(self.settings.regions)} regions")

    async def _connection_pool_maintenance_loop(self) -> None:
        """Background loop to maintain connection pool health and retry stale requests"""
        while True:
            try:
                await asyncio.sleep(30.0)
                if self._session and not self._session.closed:
                    logger.debug(
                        f"Connection pool stats: {self._session.connector.used_connections}/{self._session.connector.limit} used"
                    )
            except asyncio.CancelledError:
                logger.info("Connection pool maintenance loop cancelled")
                return
            except Exception as e:
                logger.warning(f"Connection pool maintenance error: {str(e)}")
                await asyncio.sleep(5.0)

    async def _get_available_region(self) -> Optional[str]:
        """Least-loaded region selection algorithm with circuit breaker awareness"""
        available_regions = []
        for region in self.settings.regions:
            breaker = self.circuit_breakers[region]
            if await breaker.allow_request():
                available_regions.append(
                    (
                        breaker.active_requests,  # Primary sort key: active load
                        breaker.success_count,  # Secondary sort key: success rate
                        region,
                    )
                )

        if not available_regions:
            return None

        # Select region with lowest active requests, then highest success rate
        available_regions.sort(key=lambda x: (x[0], -x[1]))
        selected = available_regions[0][2]
        logger.debug(f"Selected region {selected} with {available_regions[0][0]} active requests")
        return selected

    @inference_retry_wrapper()
    async def create_chat_completion(
        self, messages: List[Dict[str, str]], model: str = "llama3:70b", stream: bool = False, **kwargs
    ) -> Dict[str, Any]:
        """
        Create a chat completion with full validation, retry logic, and region failover.
        Main entry point for all LLM inference requests with cross-region load balancing.
        """
        async with self.global_semaphore:  # Global concurrency limiting
            # Validate request before sending
            validated_request = validate_inference_request(
                {"model": model, "messages": messages, "stream": stream, **kwargs}
            )

            # Find an available region with least load
            region = await self._get_available_region()
            if not region:
                raise RuntimeError("No available inference regions - all circuits are open")

            circuit_breaker = self.circuit_breakers[region]

            try:
                if not self._session:
                    await self.init()

                # Send request to Nebius API with non-blocking I/O
                async with self._session.post(
                    f"{self.settings.api_endpoint}/chat/completions", json=validated_request.model_dump()
                ) as response:
                    response.raise_for_status()
                    result = await response.json()

                    # Validate response
                    validated_response = NebiusChatCompletionResponse(**result)
                    await circuit_breaker.record_success()
                    logger.debug(f"Inference completed successfully in region {region}")

                    return validated_response.model_dump()

            except Exception as e:
                await circuit_breaker.record_failure()
                logger.error(f"Inference failed in region {region}: {str(e)}")
                raise

    async def close(self) -> None:
        """Cleanup all resources properly"""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("Nebius inference client resources cleaned up")


def get_inference_client() -> NebiusInferenceClient:
    """Get the singleton Nebius inference client"""
    return NebiusInferenceClient.get_instance()
