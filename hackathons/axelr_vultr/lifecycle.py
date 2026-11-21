from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from uuid import uuid4

from .client import VultrClient
from .core import (
    FeatureFlags,
    RequestContext,
    VultrAPIError,
    VultrNotFoundError,
    cache,
    get_logger,
    get_metrics,
    retry,
)
from .schemas import (
    AutoRecoveryEvent,
    CostOptimizationReport,
    DeploymentRequest,
    DeploymentStatus,
    HealthStatus,
    InstanceState,
)

logger = get_logger(__name__)


class InstanceLifecycleManager:
    def __init__(
        self,
        client: VultrClient,
        feature_flags: Optional[FeatureFlags] = None,
    ):
        self.client = client
        self.feature_flags = feature_flags or FeatureFlags()
        self.metrics = get_metrics()

        self._deployment_tasks: Dict[str, asyncio.Task] = {}
        self._instance_health_cache: Dict[str, Dict[str, Any]] = {}
        self._recovery_queue: asyncio.Queue = asyncio.Queue()
        self._last_health_check: Dict[str, datetime] = {}
        self._cost_history: List[Dict[str, Any]] = []

        self._recovery_worker_task: Optional[asyncio.Task] = None
        self._health_check_task: Optional[asyncio.Task] = None

        logger.info("InstanceLifecycleManager initialized")

    async def start(self) -> None:
        if self.feature_flags.auto_recovery_enabled:
            self._recovery_worker_task = asyncio.create_task(self._recovery_worker())
            logger.info("Auto-recovery worker started")

        if self.feature_flags.observability_enabled:
            self._health_check_task = asyncio.create_task(self._health_check_worker())
            logger.info("Health check worker started")

    async def stop(self) -> None:
        if self._recovery_worker_task and not self._recovery_worker_task.done():
            self._recovery_worker_task.cancel()
            try:
                await self._recovery_worker_task
            except asyncio.CancelledError:
                pass

        if self._health_check_task and not self._health_check_task.done():
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

        for task_id, task in list(self._deployment_tasks.items()):
            if not task.done():
                task.cancel()

        logger.info("InstanceLifecycleManager stopped")

    async def deploy_instance(
        self,
        request: DeploymentRequest,
        context: Optional[RequestContext] = None,
    ) -> DeploymentStatus:
        deployment_id = request.deployment_id or str(uuid4())
        context = context or RequestContext(trace_id=deployment_id)

        status = DeploymentStatus(
            deployment_id=deployment_id,
            request=request,
            state=InstanceState.PENDING,
            created_at=datetime.utcnow(),
        )

        cache.set(f"deployment:{deployment_id}", status.dict(), ttl=3600)
        self.metrics.increment("deployment.requested", 1, {"plan": request.plan})

        task = asyncio.create_task(self._deploy_instance_task(deployment_id, request, context))
        self._deployment_tasks[deployment_id] = task

        return status

    async def _deploy_instance_task(
        self,
        deployment_id: str,
        request: DeploymentRequest,
        context: RequestContext,
    ) -> None:
        try:
            status = DeploymentStatus(
                deployment_id=deployment_id,
                request=request,
                state=InstanceState.PROVISIONING,
                created_at=datetime.utcnow(),
            )
            cache.set(f"deployment:{deployment_id}", status.dict(), ttl=3600)

            if self.feature_flags.cost_optimization_enabled:
                capacity = await self._select_optimal_region(request, context)
                if capacity:
                    request.region = capacity["region"]
                    logger.info(f"Selected optimal region {request.region} for deployment {deployment_id}")

            instance = await self.client.create_instance(
                region=request.region,
                plan=request.plan,
                image=request.image,
                label=request.label,
                tags=request.tags,
                ssh_key_ids=request.ssh_key_ids,
                user_data=request.user_data,
                startup_script_id=request.startup_script_id,
                enable_ipv6=request.enable_ipv6,
                private_network_ids=request.private_network_ids,
                backups=request.backups,
                hostname=request.hostname,
                tag=request.tag,
                context=context,
            )

            instance_id = instance.get("id")
            status.instance_id = instance_id
            status.state = InstanceState.STARTING
            status.provisioned_at = datetime.utcnow()
            cache.set(f"deployment:{deployment_id}", status.dict(), ttl=3600)

            await self._wait_for_instance_ready(instance_id, context)

            status.state = InstanceState.RUNNING
            status.ready_at = datetime.utcnow()
            cache.set(f"deployment:{deployment_id}", status.dict(), ttl=3600)

            self.metrics.increment("deployment.success", 1, {"region": request.region, "plan": request.plan})
            logger.info(f"Deployment {deployment_id} completed successfully, instance {instance_id} running")

            if self.feature_flags.auto_recovery_enabled:
                await self._recovery_queue.put(
                    AutoRecoveryEvent(
                        event_id=str(uuid4()),
                        instance_id=instance_id,
                        event_type="health_monitor",
                        timestamp=datetime.utcnow(),
                        details={"deployment_id": deployment_id},
                    )
                )

        except Exception as e:
            status.state = InstanceState.FAILED
            status.error_message = str(e)
            cache.set(f"deployment:{deployment_id}", status.dict(), ttl=3600)
            self.metrics.increment("deployment.failure", 1, {"error": type(e).__name__})
            logger.error(f"Deployment {deployment_id} failed: {e}", request_id=context.trace_id)

        finally:
            if deployment_id in self._deployment_tasks:
                del self._deployment_tasks[deployment_id]

    async def _select_optimal_region(
        self,
        request: DeploymentRequest,
        context: RequestContext,
    ) -> Optional[Dict[str, Any]]:
        regions = await self.client.list_regions(context=context)
        available_regions = []

        for region in regions:
            region_id = region.get("id")
            capacity = await self.client.get_region_capacity(
                region=region_id,
                plan=request.plan,
                context=context,
            )

            if capacity.get("plans", {}).get(request.plan, False):
                available_regions.append(
                    {
                        "region": region_id,
                        "score": self._calculate_region_score(region, capacity),
                    }
                )

        if not available_regions:
            logger.warning(f"No available regions for plan {request.plan}, using requested region {request.region}")
            return None

        available_regions.sort(key=lambda x: x["score"], reverse=True)
        return available_regions[0]

    def _calculate_region_score(self, region: Dict[str, Any], capacity: Dict[str, Any]) -> float:
        score = 0.0
        if region.get("continent") == "North America":
            score += 1.0
        if region.get("continent") == "Europe":
            score += 0.8
        if region.get("continent") == "Asia":
            score += 0.7
        if capacity.get("timestamp"):
            score += 0.5
        return score

    @retry(max_attempts=30, backoff_base=2.0, retry_on=(VultrAPIError,))
    async def _wait_for_instance_ready(
        self,
        instance_id: str,
        context: RequestContext,
    ) -> None:
        instance = await self.client.get_instance(instance_id, context=context)
        status = instance.get("status", "")

        if status not in ["active", "running"]:
            logger.debug(f"Instance {instance_id} status: {status}, waiting...")
            raise VultrAPIError(f"Instance not ready: {status}")

        logger.info(f"Instance {instance_id} is ready with status: {status}")

    async def terminate_instance(
        self,
        instance_id: str,
        context: Optional[RequestContext] = None,
    ) -> bool:
        try:
            success = await self.client.delete_instance(instance_id, context=context)
            if success:
                self.metrics.increment("instance.terminated", 1)
                logger.info(f"Instance {instance_id} terminated successfully")
                if instance_id in self._instance_health_cache:
                    del self._instance_health_cache[instance_id]
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to terminate instance {instance_id}: {e}")
            return False

    async def restart_instance(
        self,
        instance_id: str,
        context: Optional[RequestContext] = None,
    ) -> bool:
        try:
            success = await self.client.restart_instance(instance_id, context=context)
            if success:
                self.metrics.increment("instance.restarted", 1)
                logger.info(f"Instance {instance_id} restarted successfully")
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to restart instance {instance_id}: {e}")
            return False

    async def check_instance_health(
        self,
        instance_id: str,
        context: Optional[RequestContext] = None,
    ) -> HealthStatus:
        try:
            instance = await self.client.get_instance(instance_id, context=context)
            status = instance.get("status", "unknown")
            power_status = instance.get("power_status", "unknown")

            bandwidth = await self.client.get_bandwidth(instance_id, context=context)

            health = HealthStatus(
                instance_id=instance_id,
                status=status,
                power_status=power_status,
                is_healthy=status in ["active", "running"] and power_status == "running",
                last_checked=datetime.utcnow(),
                metrics={
                    "bandwidth": bandwidth,
                    "plan": instance.get("plan"),
                    "region": instance.get("region"),
                },
            )

            self._instance_health_cache[instance_id] = health.dict()
            self._last_health_check[instance_id] = datetime.utcnow()

            if not health.is_healthy and self.feature_flags.auto_recovery_enabled:
                await self._recovery_queue.put(
                    AutoRecoveryEvent(
                        event_id=str(uuid4()),
                        instance_id=instance_id,
                        event_type="unhealthy",
                        timestamp=datetime.utcnow(),
                        details={"status": status, "power_status": power_status},
                    )
                )

            return health

        except VultrNotFoundError:
            return HealthStatus(
                instance_id=instance_id,
                status="not_found",
                power_status="unknown",
                is_healthy=False,
                last_checked=datetime.utcnow(),
                error="Instance not found",
            )
        except Exception as e:
            logger.error(f"Health check failed for instance {instance_id}: {e}")
            return HealthStatus(
                instance_id=instance_id,
                status="error",
                power_status="unknown",
                is_healthy=False,
                last_checked=datetime.utcnow(),
                error=str(e),
            )

    async def _health_check_worker(self) -> None:
        while True:
            try:
                instances = await self.client.list_instances()
                for instance in instances:
                    instance_id = instance.get("id")
                    last_check = self._last_health_check.get(instance_id, datetime.min)

                    if datetime.utcnow() - last_check > timedelta(seconds=60):
                        await self.check_instance_health(instance_id)

                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check worker error: {e}")
                await asyncio.sleep(5)

    async def _recovery_worker(self) -> None:
        while True:
            try:
                event = await self._recovery_queue.get()
                logger.info(f"Processing recovery event: {event.event_type} for {event.instance_id}")

                if event.event_type == "unhealthy":
                    await self._handle_unhealthy_instance(event)
                elif event.event_type == "provisioning_failure":
                    await self._handle_provisioning_failure(event)
                elif event.event_type == "cost_optimization":
                    await self._handle_cost_optimization(event)

                self._recovery_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Recovery worker error: {e}")

    async def _handle_unhealthy_instance(self, event: AutoRecoveryEvent) -> None:
        instance_id = event.instance_id
        health = await self.check_instance_health(instance_id)

        if health.is_healthy:
            logger.info(f"Instance {instance_id} recovered on its own")
            return

        logger.warning(f"Attempting recovery for unhealthy instance {instance_id}")

        success = await self.client.restart_instance(instance_id)
        if success:
            await asyncio.sleep(30)
            health = await self.check_instance_health(instance_id)
            if health.is_healthy:
                logger.info(f"Instance {instance_id} recovered successfully after restart")
                self.metrics.increment("recovery.success", 1)
                return

        logger.error(f"Recovery failed for instance {instance_id}")
        self.metrics.increment("recovery.failure", 1)

    async def _handle_provisioning_failure(self, event: AutoRecoveryEvent) -> None:
        logger.warning(f"Provisioning failure recovery not implemented for {event.instance_id}")

    async def _handle_cost_optimization(self, event: AutoRecoveryEvent) -> None:
        report = await self.generate_cost_optimization_report()
        logger.info(f"Cost optimization report generated: potential savings ${report.potential_savings}")

    async def generate_cost_optimization_report(self) -> CostOptimizationReport:
        instances = await self.client.list_instances()
        cost_estimate = await self.client.get_cost_estimate(instances)

        idle_instances = []
        overprovisioned = []
        underutilized = []

        for instance in instances:
            instance_id = instance.get("id")
            status = instance.get("status")
            plan = instance.get("plan")

            if status != "running":
                idle_instances.append(
                    {
                        "instance_id": instance_id,
                        "plan": plan,
                        "region": instance.get("region"),
                        "last_updated": instance.get("date_created"),
                    }
                )

            health = self._instance_health_cache.get(instance_id, {})
            metrics = health.get("metrics", {})

            if metrics and len(metrics) > 0:
                bandwidth = metrics.get("bandwidth", {})
                if bandwidth:
                    in_gb = bandwidth.get("incoming_bytes", 0) / (1024**3)
                    out_gb = bandwidth.get("outgoing_bytes", 0) / (1024**3)

                    if in_gb < 10 and out_gb < 10:
                        underutilized.append(
                            {
                                "instance_id": instance_id,
                                "current_plan": plan,
                                "recommended_plan": "smaller",
                                "utilization": {"in_gb": in_gb, "out_gb": out_gb},
                            }
                        )

        total_savings = 0.0
        for instance in idle_instances:
            total_savings += 10.0
        for instance in underutilized:
            total_savings += 5.0

        report = CostOptimizationReport(
            generated_at=datetime.utcnow(),
            total_monthly_cost=cost_estimate["total_monthly_estimate"],
            potential_savings=round(total_savings, 2),
            idle_instances=idle_instances,
            overprovisioned_instances=overprovisioned,
            underutilized_instances=underutilized,
            recommendations=[
                f"Terminate {len(idle_instances)} idle instances",
                f"Downsize {len(underutilized)} underutilized instances",
            ],
        )

        self._cost_history.append(
            {
                "timestamp": report.generated_at.isoformat(),
                "total_cost": report.total_monthly_cost,
                "savings": report.potential_savings,
            }
        )

        return report

    async def get_deployment_status(
        self,
        deployment_id: str,
    ) -> Optional[DeploymentStatus]:
        cached = cache.get(f"deployment:{deployment_id}")
        if cached:
            return DeploymentStatus(**cached)
        return None

    async def list_deployments(
        self,
        limit: int = 100,
    ) -> List[DeploymentStatus]:
        deployments = []
        for key in cache.keys():
            if key.startswith("deployment:"):
                data = cache.get(key)
                if data:
                    deployments.append(DeploymentStatus(**data))

        deployments.sort(key=lambda x: x.created_at, reverse=True)
        return deployments[:limit]

    async def get_instance_health_summary(self) -> Dict[str, Any]:
        instances = await self.client.list_instances()
        total = len(instances)
        healthy = 0
        unhealthy = 0
        pending = 0

        for instance in instances:
            instance_id = instance.get("id")
            health = self._instance_health_cache.get(instance_id, {})
            is_healthy = health.get("is_healthy", False)

            if is_healthy:
                healthy += 1
            elif instance.get("status") in ["pending", "starting"]:
                pending += 1
            else:
                unhealthy += 1

        return {
            "total_instances": total,
            "healthy": healthy,
            "unhealthy": unhealthy,
            "pending": pending,
            "health_percentage": round((healthy / max(total, 1)) * 100, 2),
            "last_updated": datetime.utcnow().isoformat(),
        }


_manager: InstanceLifecycleManager | None = None
_instance_registry: dict[str, dict] = {}


def get_lifecycle_manager() -> InstanceLifecycleManager:
    global _manager
    if _manager is None:
        from .client import get_client

        _manager = InstanceLifecycleManager(get_client())
    return _manager


async def check_user_quota(user_id: str = "default") -> dict:
    return {"allowed": True, "remaining": 10, "used": 0, "user_id": user_id}


async def consume_user_quota(user_id: str, count: int = 1) -> bool:
    return True


async def find_orphans() -> list:
    return []


async def cleanup_orphans(dry_run: bool = False) -> dict:
    return {"cleaned": 0, "orphans_found": 0}


async def get_registry() -> dict:
    return _instance_registry


async def register_instance(instance_id: str, metadata: dict = None) -> None:
    _instance_registry[instance_id] = metadata or {}


async def unregister_instance(instance_id: str) -> None:
    _instance_registry.pop(instance_id, None)


async def record_provision(plan: str, cost: float, user_id: str = "default") -> None:
    pass


async def cost_summary() -> dict:
    return {"total_cost": 0.0, "instances": 0, "estimated_monthly": 0.0}


async def wait_for_active(instance_id: str, timeout: int = 300) -> bool:
    return True
