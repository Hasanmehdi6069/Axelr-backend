"""FastAPI routes — 42 elite endpoints for HackGlobal Elite Tier v3.0.0."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .auditing import AuditTimer
from .client import get_client, get_client_pool
from .core import (
    FeatureFlags,
    VultrAPIError,
    VultrCapacityError,
    VultrCircuitBreakerOpenError,
    VultrRateLimitError,
    new_request_id,
    settings,
)
from .core import (
    metrics as core_metrics,
)
from .lifecycle import (
    check_user_quota,
    cleanup_orphans,
    consume_user_quota,
    cost_summary,
    find_orphans,
    get_lifecycle_manager,
    record_provision,
    register_instance,
    unregister_instance,
    wait_for_active,
)
from .schemas import (
    BulkProvisionRequest,
    CleanupRequest,
    CostOptimizationReport,
    CreateInstanceRequest,
    EliteDeploymentRequest,
    EliteDeploymentStatus,
    EngineConfig,
    FeatureFlagsConfig,
    FirewallGroupCreateRequest,
    SnapshotCreateRequest,
    SSHKeyCreateRequest,
    SystemHealthMetrics,
    UpdateInstanceRequest,
)
from .templates import (
    UnknownBlueprintError,
    list_blueprints,
    list_elite_blueprints,
    strict_resolve_blueprint,
)

logger = logging.getLogger("axelr.vultr.routes")
router = APIRouter(prefix="/vultr", tags=["vultr"])
_bearer = HTTPBearer(auto_error=False)


async def get_vultr_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    x_appwrite_session: str | None = Header(None, alias="X-Appwrite-Session"),
) -> dict:
    if credentials is not None:
        try:
            from api.auth import get_current_user
            return await get_current_user(request, credentials)
        except HTTPException:
            pass
        except Exception:
            pass
    if x_appwrite_session:
        try:
            from api.auth import verify_appwrite_session
            user = verify_appwrite_session(x_appwrite_session)
            if user:
                return user
        except Exception:
            pass
    raise HTTPException(401, "Authentication required")


def _err(code: str, message: str, st: int, rid: str | None = None,
         **extra: Any) -> JSONResponse:
    body = {"success": False,
            "error": {"code": code, "message": message, **extra}}
    if rid:
        body["request_id"] = rid
    headers = {"X-Request-Id": rid} if rid else None
    return JSONResponse(status_code=st, content=body, headers=headers)


def _ok(body: dict, st: int = 200, rid: str | None = None) -> JSONResponse:
    if rid:
        body["request_id"] = rid
    headers = {"X-Request-Id": rid} if rid else None
    return JSONResponse(status_code=st, content=body, headers=headers)


def _not_ready(rid: str | None = None) -> JSONResponse | None:
    if not settings.is_configured and not settings.dry_run:
        return _err("NOT_CONFIGURED", "VULTR_API_KEY missing", 503, rid)
    return None


@router.get("/health")
async def vultr_health() -> JSONResponse:
    from .core import health_probe
    report = await health_probe()
    return JSONResponse(status_code=200 if report.get("healthy") else 503,
                        content=report)


@router.get("/health/elite")
async def vultr_health_elite() -> SystemHealthMetrics:
    manager = get_lifecycle_manager()
    return await manager.get_system_health_metrics()


@router.get("/metrics")
async def vultr_metrics() -> dict:
    return {"success": True, "metrics": core_metrics(),
            "spend": cost_summary(24.0),
            "configuration": {
                "region_default": settings.default_region,
                "budget_usd": settings.monthly_budget_usd,
                "daily_limit": settings.daily_provision_limit,
                "dry_run": settings.dry_run,
            }}


@router.get("/metrics/elite")
async def vultr_metrics_elite() -> dict:
    client = get_client()
    manager = get_lifecycle_manager()
    return {
        "success": True,
        "metrics": core_metrics(),
        "spend": cost_summary(24.0),
        "rate_limiter": client.rate_limiter.get_status() if hasattr(client, 'rate_limiter') else {},
        "circuit_breaker": client.circuit_breaker.get_status() if hasattr(client, 'circuit_breaker') else {},
        "cache": client.cache.get_stats() if hasattr(client, 'cache') else {},
        "lifecycle": await manager.get_system_health_metrics(),
        "feature_flags": FeatureFlags.get_all(),
    }


@router.get("/config")
async def vultr_config(user: dict = Depends(get_vultr_user)) -> EngineConfig:
    client = get_client()
    return EngineConfig(
        version="3.1.0",
        mode="elite",
        dry_run=settings.dry_run,
        feature_flags=FeatureFlagsConfig(**FeatureFlags.get_all()),
        rate_limit=client.rate_limiter.config if hasattr(client, 'rate_limiter') else None,
        circuit_breaker=client.circuit_breaker.config if hasattr(client, 'circuit_breaker') else None,
        cache=client.cache.config if hasattr(client, 'cache') else None,
        retry=None,
    )


@router.get("/blueprints")
async def vultr_blueprints(user: dict = Depends(get_vultr_user)) -> dict:
    return {"success": True, "blueprints": list_blueprints()}


@router.get("/blueprints/elite")
async def vultr_blueprints_elite(user: dict = Depends(get_vultr_user)) -> dict:
    return {"success": True, "blueprints": list_elite_blueprints()}


@router.get("/regions")
async def vultr_regions(refresh: bool = Query(False),
                        user: dict = Depends(get_vultr_user)) -> Any:
    if (ready := _not_ready()) is not None:
        return ready
    try:
        regions = await get_client().list_regions(refresh=refresh)
        return {"success": True,
                "regions": [{"id": r.get("id"), "city": r.get("city"),
                             "country": r.get("country"),
                             "continent": r.get("continent"),
                             "options": r.get("options", []),
                             "capacity_score": r.get("capacity_score", 0)}
                            for r in regions]}
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502,
                    upstream_status=e.status)


@router.get("/regions/optimal")
async def vultr_optimal_region(plan_id: str,
                               user: dict = Depends(get_vultr_user)) -> Any:
    if (ready := _not_ready()) is not None:
        return ready
    try:
        manager = get_lifecycle_manager()
        optimal = await manager._select_optimal_region(plan_id)
        return {"success": True, "optimal_region": optimal}
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502,
                    upstream_status=e.status)


@router.get("/plans")
async def vultr_plans(region: str | None = Query(None, max_length=16),
                      plan_type: str | None = Query(None, max_length=16),
                      refresh: bool = Query(False),
                      user: dict = Depends(get_vultr_user)) -> Any:
    if (ready := _not_ready()) is not None:
        return ready
    try:
        plans = await get_client().list_plans(
            region=region, plan_type=plan_type, refresh=refresh)
        return {"success": True,
                "plans": [{"id": p.get("id"),
                           "vcpu_count": p.get("vcpu_count"),
                           "ram_mb": p.get("ram"),
                           "disk_gb": p.get("disk"),
                           "monthly_cost_usd": float(p.get("monthly_cost", 0)),
                           "hourly_cost_usd": round(
                               float(p.get("monthly_cost", 0)) / 730.0, 6),
                           "type": p.get("type", "vc2"),
                           "locations": p.get("locations", []),
                           "available": p.get("available", True)}
                          for p in plans]}
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502,
                    upstream_status=e.status)


@router.get("/cost/{plan_id}")
async def vultr_cost_estimate(plan_id: str,
                              hours: float = Query(730.0, ge=0.1, le=8760),
                              region: str | None = Query(None, max_length=16),
                              user: dict = Depends(get_vultr_user)) -> Any:
    if (ready := _not_ready()) is not None:
        return ready
    try:
        est = await get_client().estimate_cost(plan_id, hours, region=region)
        return {"success": True, "estimate": est}
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502,
                    upstream_status=e.status)


@router.get("/capacity/{region}/{plan_id}")
async def vultr_capacity_check(region: str, plan_id: str,
                               user: dict = Depends(get_vultr_user)) -> Any:
    if (ready := _not_ready()) is not None:
        return ready
    try:
        capacity = await get_client().get_region_capacity(region, plan_id)
        return {"success": True, "capacity": capacity}
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502,
                    upstream_status=e.status)


@router.get("/quotas")
async def vultr_quotas(user: dict = Depends(get_vultr_user)) -> dict:
    user_id = str(user.get("_id", "anon"))
    q = await check_user_quota(user_id, settings.daily_provision_limit)
    return {"success": True, "quota": q}


@router.get("/spend")
async def vultr_spend(window_hours: float = Query(24.0, ge=0.1, le=720),
                      user: dict = Depends(get_vultr_user)) -> dict:
    return {"success": True, "summary": cost_summary(window_hours)}


@router.get("/cost/optimization")
async def vultr_cost_optimization(user: dict = Depends(get_vultr_user)) -> CostOptimizationReport:
    manager = get_lifecycle_manager()
    return await manager.generate_cost_optimization_report()


@router.post("/instances", status_code=status.HTTP_201_CREATED)
async def vultr_create(body: CreateInstanceRequest,
                       user: dict = Depends(get_vultr_user)) -> Any:
    rid = new_request_id("vultr_prov")
    if (ready := _not_ready(rid)) is not None:
        return ready
    user_id = str(user.get("_id", "anon"))

    try:
        bp = strict_resolve_blueprint(
            body.blueprint, region=body.region, plan=body.plan,
            os_id=body.os_id, startup_script=body.startup_script,
            tags=body.tags, default_region=settings.default_region)
    except UnknownBlueprintError as e:
        return _err("UNKNOWN_BLUEPRINT", str(e), 422, rid)

    region = body.region or bp.region_default
    plan = body.plan or bp.plan
    os_id = body.os_id or bp.os_id
    label = body.label or f"axelr-{body.blueprint}-{rid[-6:]}"

    q = await check_user_quota(user_id, settings.daily_provision_limit)
    if q["remaining"] <= 0:
        return _err("QUOTA_EXCEEDED",
                    f"Daily limit {q['limit']} reached", 429, rid,
                    quota=q)

    try:
        est = await get_client().estimate_cost(plan, 730.0, region=region)
    except Exception:
        est = {"over_budget": False, "estimated_usd": 0.0,
               "remaining_budget_usd": 0.0, "monthly_budget_usd": 0.0}
    if est.get("over_budget"):
        return _err("BUDGET_EXCEEDED",
                    f"${est['estimated_usd']:.2f} would exceed "
                    f"${est['monthly_budget_usd']:.2f} budget", 402, rid,
                    estimate=est)

    dry = body.dry_run or settings.dry_run
    if dry:
        return _ok({"success": True, "dry_run": True,
                    "instance": {"id": f"dry-{rid[:8]}", "label": label,
                                 "region": region, "plan": plan,
                                 "status": "pending", "main_ip": None,
                                 "blueprint": bp.name,
                                 "tags": list(bp.tags),
                                 "estimated_monthly_usd": bp.estimated_monthly_usd},
                    "idempotency_key": rid, "estimate": est,
                    "note": "VULTR_DRY_RUN=1 — no API call was made"},
                   201, rid)

    try:
        async with AuditTimer("vultr.provision", user_id=user_id, session_id=rid,
                              blueprint=bp.name, region=region, plan=plan):
            instance = await get_client().create_instance(
                region=region, plan=plan, os_id=os_id, label=label,
                startup_script=bp.startup_script or None,
                ssh_key_ids=body.ssh_key_ids or None,
                tags=list(bp.tags), idempotency_key=rid)
    except VultrRateLimitError as e:
        return _err("RATE_LIMITED", str(e)[:400], 429, rid,
                    retry_after=e.retry_after if hasattr(e, 'retry_after') else None)
    except VultrCircuitBreakerOpenError as e:
        return _err("CIRCUIT_OPEN", str(e)[:400], 503, rid)
    except VultrCapacityError as e:
        return _err("CAPACITY_ERROR", str(e)[:400], 507, rid)
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502, rid,
                    upstream_status=e.status)
    except Exception as e:
        logger.exception("vultr_create_failed err=%s", e)
        return _err("INTERNAL", "Provision failed", 500, rid)

    iid = instance.get("id", "")
    await register_instance(iid, {
        "user_id": user_id, "blueprint": bp.name,
        "region": region, "plan": plan, "request_id": rid})
    await record_provision(iid, plan, bp.estimated_monthly_usd, user_id)
    await consume_user_quota(user_id)
    await get_client().invalidate_spend()

    return _ok({"success": True, "dry_run": False,
                "instance": {"id": iid,
                             "label": instance.get("label", label),
                             "region": instance.get("region", region),
                             "plan": instance.get("plan", plan),
                             "status": instance.get("status", "pending"),
                             "main_ip": instance.get("main_ip"),
                             "blueprint": bp.name,
                             "tags": instance.get("tags", list(bp.tags)),
                             "estimated_monthly_usd": bp.estimated_monthly_usd},
                "idempotency_key": rid, "estimate": est}, 201, rid)


@router.post("/instances/elite", status_code=status.HTTP_201_CREATED)
async def vultr_create_elite(body: EliteDeploymentRequest,
                            user: dict = Depends(get_vultr_user)) -> EliteDeploymentStatus:
    rid = new_request_id("vultr_elite")
    if (ready := _not_ready(rid)) is not None:
        raise HTTPException(status_code=503, detail="Not configured")
    user_id = str(user.get("_id", "anon"))

    manager = get_lifecycle_manager()
    return await manager.deploy_instance(body, user_id, rid)


@router.post("/instances/bulk", status_code=status.HTTP_201_CREATED)
async def vultr_bulk_create(body: BulkProvisionRequest,
                            user: dict = Depends(get_vultr_user)) -> Any:
    rid = new_request_id("vultr_bulk")
    if (ready := _not_ready(rid)) is not None:
        return ready
    user_id = str(user.get("_id", "anon"))

    q = await check_user_quota(user_id, settings.daily_provision_limit)
    if q["remaining"] < len(body.items):
        return _err("QUOTA_EXCEEDED",
                    f"Daily limit {q['limit']} reached — "
                    f"{q['remaining']} remaining, {len(body.items)} requested",
                    429, rid, quota=q)

    created: list[dict] = []
    for i, item in enumerate(body.items):
        try:
            bp = strict_resolve_blueprint(item.blueprint,
                region=item.region, plan=item.plan, os_id=item.os_id,
                startup_script=item.startup_script, tags=item.tags,
                default_region=settings.default_region)
        except UnknownBlueprintError as e:
            await _rollback_bulk(created)
            return _err("UNKNOWN_BLUEPRINT",
                        f"item[{i}]: {e}", 422, rid,
                        rolled_back=len(created))

        region = item.region or bp.region_default
        label = item.label or f"axelr-{item.blueprint}-{rid[-6:]}-{i}"
        try:
            inst = await get_client().create_instance(
                region=region, plan=bp.plan, os_id=bp.os_id,
                label=label, startup_script=bp.startup_script or None,
                tags=list(bp.tags),
                idempotency_key=f"{rid}-{i}")
            iid = inst.get("id", "")
            await register_instance(iid, {"user_id": user_id,
                                          "blueprint": bp.name,
                                          "request_id": rid})
            await record_provision(iid, bp.plan, bp.estimated_monthly_usd, user_id)
            await consume_user_quota(user_id)
            created.append({"id": iid, "blueprint": bp.name,
                            "region": region, "plan": bp.plan})
        except Exception as e:
            await _rollback_bulk(created)
            return _err("BULK_FAILED", f"item[{i}]: {str(e)[:300]}", 502, rid,
                        rolled_back=len(created), original_error=str(e)[:200])
    await get_client().invalidate_spend()
    return _ok({"success": True, "count": len(created), "instances": created},
               201, rid)


async def _rollback_bulk(created: list[dict]) -> None:
    for c in created:
        try:
            await get_client().delete_instance(c["id"])
            await unregister_instance(c["id"])
        except Exception:
            pass


@router.get("/instances")
async def vultr_list(managed_only: bool = Query(True),
                     user: dict = Depends(get_vultr_user)) -> Any:
    if (ready := _not_ready()) is not None:
        return ready
    try:
        instances = await get_client().list_instances()
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502,
                    upstream_status=e.status)
    if managed_only:
        instances = [i for i in instances
                     if "axelr-managed" in (i.get("tags") or [])]
    return {"success": True, "count": len(instances),
            "instances": [{"id": i.get("id"), "label": i.get("label"),
                           "region": i.get("region"), "plan": i.get("plan"),
                           "status": i.get("status"),
                           "main_ip": i.get("main_ip"),
                           "tags": i.get("tags", []),
                           "created_at": i.get("date_created")}
                          for i in instances]}


@router.get("/instances/{instance_id}")
async def vultr_get(instance_id: str,
                    user: dict = Depends(get_vultr_user)) -> Any:
    if (ready := _not_ready()) is not None:
        return ready
    try:
        inst = await get_client().get_instance(instance_id)
    except VultrAPIError as e:
        code = 404 if e.status == 404 else 502
        return _err("VULTR_API_ERROR", str(e)[:400], code,
                    upstream_status=e.status)
    return {"success": True, "instance": inst}


@router.get("/instances/{instance_id}/health")
async def vultr_instance_health(instance_id: str,
                                user: dict = Depends(get_vultr_user)) -> Any:
    if (ready := _not_ready()) is not None:
        return ready
    try:
        manager = get_lifecycle_manager()
        health = await manager._check_instance_health(instance_id)
        return {"success": True, "health": health}
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502,
                    upstream_status=e.status)


@router.patch("/instances/{instance_id}")
async def vultr_update(instance_id: str, body: UpdateInstanceRequest,
                       user: dict = Depends(get_vultr_user)) -> Any:
    rid = new_request_id("vultr_upd")
    if (ready := _not_ready(rid)) is not None:
        return ready
    if body.label is None and body.tags is None:
        return _err("NO_FIELDS", "At least one of label/tags required", 422, rid)
    try:
        inst = await get_client().update_instance(
            instance_id, label=body.label, tags=body.tags)
        return _ok({"success": True, "instance": inst}, 200, rid)
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502, rid,
                    upstream_status=e.status)


@router.post("/instances/{instance_id}/wait")
async def vultr_wait(instance_id: str,
                     timeout_s: int = Query(240, ge=10, le=600),
                     user: dict = Depends(get_vultr_user)) -> Any:
    if (ready := _not_ready()) is not None:
        return ready
    try:
        inst = await wait_for_active(
            get_client(), instance_id,
            timeout_s=min(timeout_s, settings.wait_max_s))
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502,
                    upstream_status=e.status)
    return {"success": True,
            "instance": {"id": inst.get("id", instance_id),
                         "status": inst.get("status", "unknown"),
                         "main_ip": inst.get("main_ip"),
                         "timed_out": bool(inst.get("_timed_out")),
                         "transitions": inst.get("_transitions", [])}}


@router.get("/instances/{instance_id}/bandwidth")
async def vultr_bandwidth(instance_id: str,
                          user: dict = Depends(get_vultr_user)) -> Any:
    if (ready := _not_ready()) is not None:
        return ready
    try:
        bw = await get_client().get_instance_bandwidth(instance_id)
        return {"success": True, "bandwidth": bw}
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502,
                    upstream_status=e.status)


@router.delete("/instances/{instance_id}")
async def vultr_destroy(instance_id: str,
                        confirm: bool = Query(False),
                        user: dict = Depends(get_vultr_user)) -> Any:
    rid = new_request_id("vultr_del")
    if (ready := _not_ready(rid)) is not None:
        return ready
    if not confirm:
        return _err("CONFIRM_REQUIRED",
                    "Set confirm=true. This is irreversible.", 400, rid)
    try:
        async with AuditTimer("vultr.destroy",
                              user_id=str(user.get("_id", "anon")),
                              session_id=rid, instance_id=instance_id):
            await get_client().delete_instance(instance_id)
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502, rid,
                    upstream_status=e.status)
    await unregister_instance(instance_id)
    await get_client().invalidate_spend()
    return _ok({"success": True, "destroyed": instance_id}, 200, rid)


@router.post("/instances/{instance_id}/reboot")
async def vultr_reboot(instance_id: str,
                       user: dict = Depends(get_vultr_user)) -> Any:
    if (ready := _not_ready()) is not None:
        return ready
    try:
        await get_client().reboot_instance(instance_id)
        return {"success": True, "action": "reboot",
                "instance_id": instance_id}
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502,
                    upstream_status=e.status)


@router.post("/instances/{instance_id}/halt")
async def vultr_halt(instance_id: str,
                     user: dict = Depends(get_vultr_user)) -> Any:
    if (ready := _not_ready()) is not None:
        return ready
    try:
        await get_client().halt_instance(instance_id)
        return {"success": True, "action": "halt",
                "instance_id": instance_id}
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502,
                    upstream_status=e.status)


@router.post("/instances/{instance_id}/start")
async def vultr_start(instance_id: str,
                      user: dict = Depends(get_vultr_user)) -> Any:
    if (ready := _not_ready()) is not None:
        return ready
    try:
        await get_client().start_instance(instance_id)
        return {"success": True, "action": "start",
                "instance_id": instance_id}
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502,
                    upstream_status=e.status)


@router.post("/instances/{instance_id}/recovery")
async def vultr_trigger_recovery(instance_id: str,
                                 user: dict = Depends(get_vultr_user)) -> Any:
    if (ready := _not_ready()) is not None:
        return ready
    try:
        manager = get_lifecycle_manager()
        await manager._attempt_recovery(instance_id)
        return {"success": True, "action": "recovery_triggered",
                "instance_id": instance_id}
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502,
                    upstream_status=e.status)


@router.get("/snapshots")
async def vultr_snapshots(user: dict = Depends(get_vultr_user)) -> Any:
    if (ready := _not_ready()) is not None:
        return ready
    try:
        snapshots = await get_client().list_snapshots()
        return {"success": True, "snapshots": snapshots}
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502,
                    upstream_status=e.status)


@router.post("/snapshots", status_code=status.HTTP_201_CREATED)
async def vultr_create_snapshot(body: SnapshotCreateRequest,
                               user: dict = Depends(get_vultr_user)) -> Any:
    rid = new_request_id("vultr_snap")
    if (ready := _not_ready(rid)) is not None:
        return ready
    try:
        snapshot = await get_client().create_snapshot(
            instance_id=body.instance_id, description=body.description)
        return _ok({"success": True, "snapshot": snapshot}, 201, rid)
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502, rid,
                    upstream_status=e.status)


@router.get("/firewalls")
async def vultr_firewalls(user: dict = Depends(get_vultr_user)) -> Any:
    if (ready := _not_ready()) is not None:
        return ready
    try:
        firewalls = await get_client().list_firewall_groups()
        return {"success": True, "firewalls": firewalls}
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502,
                    upstream_status=e.status)


@router.post("/firewalls", status_code=status.HTTP_201_CREATED)
async def vultr_create_firewall(body: FirewallGroupCreateRequest,
                                user: dict = Depends(get_vultr_user)) -> Any:
    rid = new_request_id("vultr_fw")
    if (ready := _not_ready(rid)) is not None:
        return ready
    try:
        fw = await get_client().create_firewall_group(description=body.description)
        return _ok({"success": True, "firewall": fw}, 201, rid)
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502, rid,
                    upstream_status=e.status)


@router.get("/ssh-keys")
async def vultr_ssh_keys(user: dict = Depends(get_vultr_user)) -> Any:
    if (ready := _not_ready()) is not None:
        return ready
    try:
        keys = await get_client().list_ssh_keys()
        return {"success": True, "ssh_keys": keys}
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502,
                    upstream_status=e.status)


@router.post("/ssh-keys", status_code=status.HTTP_201_CREATED)
async def vultr_create_ssh_key(body: SSHKeyCreateRequest,
                               user: dict = Depends(get_vultr_user)) -> Any:
    rid = new_request_id("vultr_ssh")
    if (ready := _not_ready(rid)) is not None:
        return ready
    try:
        key = await get_client().create_ssh_key(name=body.name, key=body.key)
        return _ok({"success": True, "ssh_key": key}, 201, rid)
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502, rid,
                    upstream_status=e.status)


@router.post("/cleanup")
async def vultr_cleanup(body: CleanupRequest,
                        user: dict = Depends(get_vultr_user)) -> Any:
    rid = new_request_id("vultr_clean")
    if (ready := _not_ready(rid)) is not None:
        return ready
    user_id = str(user.get("_id", "anon"))
    try:
        orphans = await find_orphans(get_client(), max_age_hours=body.max_age_hours)
        if body.confirm:
            destroyed = await cleanup_orphans(get_client(), orphans)
            await get_client().invalidate_spend()
            return _ok({"success": True, "orphans_found": len(orphans),
                        "destroyed": len(destroyed)}, 200, rid)
        return _ok({"success": True, "orphans_found": len(orphans),
                    "orphans": orphans, "confirm_required": True}, 200, rid)
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502, rid,
                    upstream_status=e.status)


@router.get("/cache/stats")
async def vultr_cache_stats(user: dict = Depends(get_vultr_user)) -> dict:
    client = get_client()
    return {"success": True, "cache_stats": client.cache.get_stats() if hasattr(client, 'cache') else {}}


@router.post("/cache/invalidate")
async def vultr_cache_invalidate(user: dict = Depends(get_vultr_user)) -> dict:
    client = get_client()
    if hasattr(client, 'cache'):
        client.cache.invalidate_all()
    return {"success": True, "cache_invalidated": True}


@router.get("/pool/status")
async def vultr_pool_status(user: dict = Depends(get_vultr_user)) -> dict:
    pool = get_client_pool()
    return {"success": True, "pool_status": pool.get_status() if hasattr(pool, 'get_status') else {}}
