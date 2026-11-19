from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import aiohttp

from .core import (
    FeatureFlags,
    RequestContext,
    RequestOptimizer,
    VultrAPIError,
    VultrAuthError,
    VultrNotFoundError,
    VultrRateLimitError,
    VultrServerError,
    VultrTimeoutError,
    ZeroTrustVerifier,
    cache,
    circuit_breaker,
    get_logger,
    get_metrics,
    rate_limiter,
    retry,
)

logger = get_logger(__name__)


class VultrClient:
    BASE_URL = "https://api.vultr.com/v2"

    def __init__(
        self,
        api_key: str,
        timeout: int = 30,
        max_connections: int = 100,
        http2_enabled: bool = True,
    ):
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.max_connections = max_connections
        self.http2_enabled = http2_enabled

        self._session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(max_connections)
        self._optimizer = RequestOptimizer()
        self._security = ZeroTrustVerifier()
        self._metrics = get_metrics()
        self._feature_flags = FeatureFlags()

        self._instance_types_cache: Dict[str, Any] = {}
        self._regions_cache: Dict[str, Any] = {}
        self._plans_cache: Dict[str, Any] = {}
        self._last_capacity_check: Dict[str, datetime] = {}

        logger.info(f"VultrClient initialized with http2={http2_enabled}, max_conns={max_connections}")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=self.max_connections,
                ttl_dns_cache=300,
                use_dns_cache=True,
                force_close=False,
                enable_cleanup_closed=True,
            )
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "User-Agent": "Axelr-Vultr-Engine/3.0.0",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            logger.info("VultrClient session closed")

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        context: Optional[RequestContext] = None,
        use_cache: bool = True,
        cache_ttl: int = 60,
    ) -> Dict[str, Any]:
        request_id = context.request_id if context else "unknown"
        full_url = f"{self.BASE_URL}{path}"

        cache_key = None
        if use_cache and method.upper() == "GET":
            cache_key = f"vultr:{method}:{path}:{json.dumps(params or {})}"
            cached = cache.get(cache_key)
            if cached is not None:
                self._metrics.increment("cache.hit", 1, {"endpoint": path})
                return cached
            self._metrics.increment("cache.miss", 1, {"endpoint": path})

        if self._feature_flags.ai_optimization_enabled:
            optimized = self._optimizer.optimize(path, params or {})
            if optimized.get("batching"):
                logger.debug(f"AI optimization applied for {path}: batching={optimized['batching']}")

        @retry(max_attempts=5, backoff_base=1.0, retry_on=(VultrServerError, VultrTimeoutError))
        @rate_limiter(endpoint=path)
        @circuit_breaker(endpoint=path)
        async def _execute() -> Dict[str, Any]:
            async with self._semaphore:
                session = await self._get_session()

                start_time = datetime.utcnow()
                try:
                    if params:
                        full_url_with_params = f"{full_url}?{urlencode(params)}"
                    else:
                        full_url_with_params = full_url

                    async with session.request(
                        method=method,
                        url=full_url_with_params,
                        json=json_data,
                        allow_redirects=True,
                    ) as response:
                        latency = (datetime.utcnow() - start_time).total_seconds()

                        self._metrics.observe("request.latency", latency, {
                            "method": method,
                            "endpoint": path,
                            "status": str(response.status),
                        })

                        response_text = await response.text()

                        if self._feature_flags.zero_trust_enabled:
                            self._security.verify_response(response_text, context)

                        if response.status == 429:
                            retry_after = int(response.headers.get("Retry-After", "60"))
                            raise VultrRateLimitError(f"Rate limited, retry after {retry_after}s", retry_after=retry_after)

                        if response.status == 404:
                            raise VultrNotFoundError(f"Resource not found: {path}")

                        if response.status in (401, 403):
                            raise VultrAuthError(f"Authentication failed: {response.status}")

                        if response.status >= 500:
                            raise VultrServerError(f"Server error: {response.status}", status_code=response.status)

                        if response.status >= 400:
                            raise VultrAPIError(f"API error: {response.status} - {response_text[:200]}", status_code=response.status)

                        try:
                            result = json.loads(response_text)
                        except json.JSONDecodeError as e:
                            raise VultrAPIError(f"Invalid JSON response: {e}", status_code=response.status)

                        if cache_key and method.upper() == "GET":
                            cache.set(cache_key, result, ttl=cache_ttl)

                        self._metrics.increment("request.success", 1, {
                            "method": method,
                            "endpoint": path,
                        })

                        return result

                except asyncio.TimeoutError:
                    latency = (datetime.utcnow() - start_time).total_seconds()
                    self._metrics.increment("request.timeout", 1, {"endpoint": path})
                    raise VultrTimeoutError(f"Request timed out after {self.timeout}s")
                except aiohttp.ClientError as e:
                    latency = (datetime.utcnow() - start_time).total_seconds()
                    self._metrics.increment("request.error", 1, {"endpoint": path, "error": type(e).__name__})
                    raise VultrAPIError(f"Client error: {e}")

        try:
            return await _execute()
        except Exception as e:
            self._metrics.increment("request.failure", 1, {
                "method": method,
                "endpoint": path,
                "error": type(e).__name__,
            })
            logger.error(f"Vultr request failed: {method} {path} - {e}", request_id=request_id)
            raise

    async def list_instances(
        self,
        context: Optional[RequestContext] = None,
        per_page: int = 500,
    ) -> List[Dict[str, Any]]:
        all_instances = []
        cursor = None

        while True:
            params = {"per_page": per_page}
            if cursor:
                params["cursor"] = cursor

            response = await self._request(
                "GET",
                "/instances",
                params=params,
                context=context,
                cache_ttl=30,
            )

            instances = response.get("instances", [])
            all_instances.extend(instances)

            links = response.get("links", {})
            cursor = links.get("next")

            if not cursor or len(instances) == 0:
                break

        return all_instances

    async def get_instance(
        self,
        instance_id: str,
        context: Optional[RequestContext] = None,
    ) -> Dict[str, Any]:
        response = await self._request(
            "GET",
            f"/instances/{instance_id}",
            context=context,
            cache_ttl=10,
        )
        return response.get("instance", {})

    async def create_instance(
        self,
        region: str,
        plan: str,
        image: str,
        label: Optional[str] = None,
        tags: Optional[List[str]] = None,
        ssh_key_ids: Optional[List[str]] = None,
        user_data: Optional[str] = None,
        startup_script_id: Optional[str] = None,
        enable_ipv6: bool = True,
        private_network_ids: Optional[List[str]] = None,
        backups: str = "disabled",
        hostname: Optional[str] = None,
        tag: Optional[str] = None,
        context: Optional[RequestContext] = None,
    ) -> Dict[str, Any]:
        data = {
            "region": region,
            "plan": plan,
            "image": image,
            "enable_ipv6": enable_ipv6,
            "backups": backups,
        }

        if label:
            data["label"] = label
        if tags:
            data["tags"] = tags
        if ssh_key_ids:
            data["ssh_key_ids"] = ssh_key_ids
        if user_data:
            data["user_data"] = user_data
        if startup_script_id:
            data["startup_script_id"] = startup_script_id
        if private_network_ids:
            data["private_network_ids"] = private_network_ids
        if hostname:
            data["hostname"] = hostname
        if tag:
            data["tag"] = tag

        response = await self._request(
            "POST",
            "/instances",
            json_data=data,
            context=context,
            use_cache=False,
        )

        instance = response.get("instance", {})
        logger.info(f"Created instance {instance.get('id')} in {region} with plan {plan}")

        if self._feature_flags.cost_optimization_enabled:
            self._metrics.increment("provisioning.cost", 1, {
                "region": region,
                "plan": plan,
            })

        return instance

    async def delete_instance(
        self,
        instance_id: str,
        context: Optional[RequestContext] = None,
    ) -> bool:
        try:
            await self._request(
                "DELETE",
                f"/instances/{instance_id}",
                context=context,
                use_cache=False,
            )
            logger.info(f"Deleted instance {instance_id}")
            return True
        except VultrNotFoundError:
            logger.warning(f"Instance {instance_id} not found for deletion")
            return False

    async def start_instance(
        self,
        instance_id: str,
        context: Optional[RequestContext] = None,
    ) -> bool:
        try:
            await self._request(
                "POST",
                f"/instances/{instance_id}/start",
                context=context,
                use_cache=False,
            )
            logger.info(f"Started instance {instance_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to start instance {instance_id}: {e}")
            return False

    async def stop_instance(
        self,
        instance_id: str,
        context: Optional[RequestContext] = None,
    ) -> bool:
        try:
            await self._request(
                "POST",
                f"/instances/{instance_id}/halt",
                context=context,
                use_cache=False,
            )
            logger.info(f"Stopped instance {instance_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to stop instance {instance_id}: {e}")
            return False

    async def restart_instance(
        self,
        instance_id: str,
        context: Optional[RequestContext] = None,
    ) -> bool:
        try:
            await self._request(
                "POST",
                f"/instances/{instance_id}/reboot",
                context=context,
                use_cache=False,
            )
            logger.info(f"Restarted instance {instance_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to restart instance {instance_id}: {e}")
            return False

    async def list_plans(
        self,
        context: Optional[RequestContext] = None,
    ) -> List[Dict[str, Any]]:
        all_plans = []
        cursor = None

        while True:
            params = {"per_page": 500}
            if cursor:
                params["cursor"] = cursor

            response = await self._request(
                "GET",
                "/plans",
                params=params,
                context=context,
                cache_ttl=3600,
            )

            plans = response.get("plans", [])
            all_plans.extend(plans)

            links = response.get("links", {})
            cursor = links.get("next")

            if not cursor or len(plans) == 0:
                break

        self._plans_cache = {p["id"]: p for p in all_plans}
        return all_plans

    async def list_regions(
        self,
        context: Optional[RequestContext] = None,
    ) -> List[Dict[str, Any]]:
        all_regions = []
        cursor = None

        while True:
            params = {"per_page": 500}
            if cursor:
                params["cursor"] = cursor

            response = await self._request(
                "GET",
                "/regions",
                params=params,
                context=context,
                cache_ttl=3600,
            )

            regions = response.get("regions", [])
            all_regions.extend(regions)

            links = response.get("links", {})
            cursor = links.get("next")

            if not cursor or len(regions) == 0:
                break

        self._regions_cache = {r["id"]: r for r in all_regions}
        return all_regions

    async def list_images(
        self,
        context: Optional[RequestContext] = None,
        per_page: int = 500,
    ) -> List[Dict[str, Any]]:
        all_images = []
        cursor = None

        while True:
            params = {"per_page": per_page}
            if cursor:
                params["cursor"] = cursor

            response = await self._request(
                "GET",
                "/images",
                params=params,
                context=context,
                cache_ttl=1800,
            )

            images = response.get("images", [])
            all_images.extend(images)

            links = response.get("links", {})
            cursor = links.get("next")

            if not cursor or len(images) == 0:
                break

        return all_images

    async def list_ssh_keys(
        self,
        context: Optional[RequestContext] = None,
    ) -> List[Dict[str, Any]]:
        all_keys = []
        cursor = None

        while True:
            params = {"per_page": 500}
            if cursor:
                params["cursor"] = cursor

            response = await self._request(
                "GET",
                "/ssh-keys",
                params=params,
                context=context,
                cache_ttl=300,
            )

            keys = response.get("ssh_keys", [])
            all_keys.extend(keys)

            links = response.get("links", {})
            cursor = links.get("next")

            if not cursor or len(keys) == 0:
                break

        return all_keys

    async def get_region_capacity(
        self,
        region: str,
        plan: Optional[str] = None,
        context: Optional[RequestContext] = None,
    ) -> Dict[str, Any]:
        cache_key = f"capacity:{region}:{plan or 'all'}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        params = {}
        if plan:
            params["plan"] = plan

        response = await self._request(
            "GET",
            f"/regions/{region}/availability",
            params=params,
            context=context,
            cache_ttl=60,
        )

        availability = response.get("availability", [])
        result = {
            "region": region,
            "plans": {a["plan"]: a["available"] for a in availability},
            "timestamp": datetime.utcnow().isoformat(),
        }

        cache.set(cache_key, result, ttl=60)
        self._last_capacity_check[region] = datetime.utcnow()

        return result

    async def get_bandwidth(
        self,
        instance_id: str,
        context: Optional[RequestContext] = None,
    ) -> Dict[str, Any]:
        response = await self._request(
            "GET",
            f"/instances/{instance_id}/bandwidth",
            context=context,
            cache_ttl=60,
        )
        return response.get("bandwidth", {})

    async def get_instance_neighbors(
        self,
        instance_id: str,
        context: Optional[RequestContext] = None,
    ) -> List[str]:
        response = await self._request(
            "GET",
            f"/instances/{instance_id}/neighbors",
            context=context,
            cache_ttl=300,
        )
        return response.get("neighbors", [])

    async def list_firewall_groups(
        self,
        context: Optional[RequestContext] = None,
    ) -> List[Dict[str, Any]]:
        all_groups = []
        cursor = None

        while True:
            params = {"per_page": 500}
            if cursor:
                params["cursor"] = cursor

            response = await self._request(
                "GET",
                "/firewalls",
                params=params,
                context=context,
                cache_ttl=300,
            )

            groups = response.get("firewalls", [])
            all_groups.extend(groups)

            links = response.get("links", {})
            cursor = links.get("next")

            if not cursor or len(groups) == 0:
                break

        return all_groups

    async def create_firewall_group(
        self,
        description: str,
        context: Optional[RequestContext] = None,
    ) -> Dict[str, Any]:
        data = {"description": description}
        response = await self._request(
            "POST",
            "/firewalls",
            json_data=data,
            context=context,
            use_cache=False,
        )
        return response.get("firewall_group", {})

    async def get_api_status(
        self,
        context: Optional[RequestContext] = None,
    ) -> Dict[str, Any]:
        try:
            start_time = datetime.utcnow()
            await self._request(
                "GET",
                "/regions",
                params={"per_page": 1},
                context=context,
                cache_ttl=0,
            )
            latency = (datetime.utcnow() - start_time).total_seconds()
            return {
                "status": "healthy",
                "latency_ms": round(latency * 1000, 2),
                "timestamp": datetime.utcnow().isoformat(),
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat(),
            }

    async def get_cost_estimate(
        self,
        instances: Optional[List[Dict[str, Any]]] = None,
        context: Optional[RequestContext] = None,
    ) -> Dict[str, Any]:
        if instances is None:
            instances = await self.list_instances(context=context)

        total_monthly = 0.0
        running_count = 0
        stopped_count = 0

        plans = await self.list_plans(context=context)
        plan_prices = {p["id"]: float(p.get("monthly_cost", 0)) for p in plans}

        for instance in instances:
            plan = instance.get("plan")
            status = instance.get("status", "stopped")

            if status == "running":
                running_count += 1
                total_monthly += plan_prices.get(plan, 0)
            else:
                stopped_count += 1

        return {
            "total_monthly_estimate": round(total_monthly, 2),
            "running_instances": running_count,
            "stopped_instances": stopped_count,
            "total_instances": len(instances),
            "per_instance_avg": round(total_monthly / max(running_count, 1), 2) if running_count > 0 else 0,
        }


class VultrClientPool:
    def __init__(
        self,
        api_key: str,
        pool_size: int = 5,
        **client_kwargs,
    ):
        self.api_key = api_key
        self.pool_size = pool_size
        self.client_kwargs = client_kwargs
        self._clients: List[VultrClient] = []
        self._index = 0
        self._lock = asyncio.Lock()

        logger.info(f"VultrClientPool initialized with size={pool_size}")

    async def get_client(self) -> VultrClient:
        async with self._lock:
            if len(self._clients) < self.pool_size:
                client = VultrClient(self.api_key, **self.client_kwargs)
                self._clients.append(client)

            client = self._clients[self._index % len(self._clients)]
            self._index += 1
            return client

    async def close_all(self) -> None:
        for client in self._clients:
            await client.close()
        self._clients.clear()
        logger.info("All VultrClient pool connections closed")


_client: VultrClient | None = None

def get_client() -> VultrClient:
    global _client
    if _client is None:
        from .core import settings
        _client = VultrClient(settings.vultr_api_key)
    return _client


_pool: VultrClientPool | None = None

def get_client_pool() -> VultrClientPool:
    global _pool
    if _pool is None:
        from .core import settings
        _pool = VultrClientPool(settings.vultr_api_key)
    return _pool
