# axelr_vultr/routes.py
"""FastAPI routes for the Vultr module — 11 endpoints, all structured."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import (
    APIRouter, Depends, Header, HTTPException, Query, Request, status,
)
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .auditing import AuditTimer
from .client import get_client
from .core import VultrAPIError, new_request_id, settings
from .lifecycle import (
    cleanup_orphans, find_orphans, register_instance,
    unregister_instance, wait_for_active,
)
from .schemas import (
    CleanupRequest, CostEstimate, CreateInstanceRequest, InstanceResponse,
)
from .templates import get_blueprint, list_blueprints, resolve_blueprint

logger = logging.getLogger("axelr.vultr.routes")
router = APIRouter(prefix="/vultr", tags=["vultr"])
_bearer = HTTPBearer(auto_error=False)


# ── Auth ────────────────────────────────────────────────────────────────────

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


def _err(code: str, message: str, st: int, **extra: Any) -> JSONResponse:
    return JSONResponse(
        status_code=st,
        content={"success": False, "error": {"code": code, "message": message, **extra}},
    )


# ── Health ──────────────────────────────────────────────────────────────────

@router.get("/health")
async def vultr_health() -> JSONResponse:
    from .core import health_probe
    report = await health_probe()
    code = 200 if report.get("healthy") else 503
    return JSONResponse(status_code=code, content=report)


# ── Blueprints ──────────────────────────────────────────────────────────────

@router.get("/blueprints")
async def vultr_blueprints(user: dict = Depends(get_vultr_user)) -> dict[str, Any]:
    return {"success": True, "blueprints": list_blueprints()}


# ── Regions ─────────────────────────────────────────────────────────────────

@router.get("/regions")
async def vultr_regions(
    refresh: bool = Query(False),
    user: dict = Depends(get_vultr_user),
) -> Any:
    if not settings.is_configured and not settings.dry_run:
        return _err("NOT_CONFIGURED", "VULTR_API_KEY missing", 503)
    try:
        regions = await get_client().list_regions(refresh=refresh)
        return {
            "success": True,
            "regions": [
                {
                    "id": r.get("id"), "city": r.get("city"),
                    "country": r.get("country"), "continent": r.get("continent"),
                    "options": r.get("options", []),
                }
                for r in regions
            ],
        }
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502, upstream_status=e.status)


# ── Plans ───────────────────────────────────────────────────────────────────

@router.get("/plans")
async def vultr_plans(
    region: str | None = Query(None, max_length=16),
    plan_type: str | None = Query(None, max_length=16),
    refresh: bool = Query(False),
    user: dict = Depends(get_vultr_user),
) -> Any:
    if not settings.is_configured and not settings.dry_run:
        return _err("NOT_CONFIGURED", "VULTR_API_KEY missing", 503)
    try:
        plans = await get_client().list_plans(
            region=region, plan_type=plan_type, refresh=refresh,
        )
        return {
            "success": True,
            "plans": [
                {
                    "id": p.get("id"),
                    "vcpu_count": p.get("vcpu_count"),
                    "ram_mb": p.get("ram"),
                    "disk_gb": p.get("disk"),
                    "monthly_cost_usd": float(p.get("monthly_cost", 0)),
                    "hourly_cost_usd": round(float(p.get("monthly_cost", 0)) / 730.0, 6),
                    "type": p.get("type", "vc2"),
                    "locations": p.get("locations", []),
                }
                for p in plans
            ],
        }
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502, upstream_status=e.status)


# ── Cost estimate ───────────────────────────────────────────────────────────

@router.get("/cost/{plan_id}")
async def vultr_cost_estimate(
    plan_id: str,
    hours: float = Query(730.0, ge=0.1, le=8760),
    region: str | None = Query(None, max_length=16),
    user: dict = Depends(get_vultr_user),
) -> Any:
    if not settings.is_configured and not settings.dry_run:
        return _err("NOT_CONFIGURED", "VULTR_API_KEY missing", 503)
    try:
        est = await get_client().estimate_cost(plan_id, hours, region=region)
        return {"success": True, "estimate": est}
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502, upstream_status=e.status)


# ── Provision ───────────────────────────────────────────────────────────────

@router.post("/instances", status_code=status.HTTP_201_CREATED)
async def vultr_create(
    body: CreateInstanceRequest, user: dict = Depends(get_vultr_user),
) -> Any:
    if not settings.is_configured and not settings.dry_run:
        return _err("NOT_CONFIGURED", "VULTR_API_KEY missing", 503)

    bp = resolve_blueprint(
        body.blueprint,
        region=body.region, plan=body.plan, os_id=body.os_id,
        startup_script=body.startup_script, tags=body.tags,
        default_region=settings.default_region,
    )
    region = body.region or bp.region_default
    plan = body.plan or bp.plan
    os_id = body.os_id or bp.os_id
    label = body.label or f"axelr-{body.blueprint}-{new_request_id('').split('_')[-1]}"
    idem = new_request_id("axelr_prov")
    req_id = new_request_id("vultr")

    # Cost guard
    try:
        est = await get_client().estimate_cost(plan, 730.0, region=region)
    except Exception:
        est = {"over_budget": False, "estimated_usd": 0.0, "remaining_budget_usd": 0.0}
    if est.get("over_budget"):
        return _err(
            "BUDGET_EXCEEDED",
            f"Monthly cost ${est['estimated_usd']:.2f} would exceed budget "
            f"${est['monthly_budget_usd']:.2f} "
            f"(remaining ${est['remaining_budget_usd']:.2f})",
            402,
            estimate=est,
        )

    dry = body.dry_run or settings.dry_run
    if dry:
        # Return a plausible dry-run response — no API call, no charge.
        return {
            "success": True, "dry_run": True,
            "instance": {
                "id": f"dry-{idem[:8]}",
                "label": label, "region": region, "plan": plan,
                "status": "pending", "main_ip": None,
                "blueprint": bp.name, "tags": list(bp.tags),
                "estimated_monthly_usd": bp.estimated_monthly_usd,
            },
            "idempotency_key": idem,
            "estimate": est,
            "note": "VULTR_DRY_RUN=1 — no API call was made",
        }

    try:
        async with AuditTimer(
            "vultr.provision",
            user_id=str(user.get("_id", "anon")),
            session_id=req_id,
            blueprint=bp.name, region=region, plan=plan,
        ):
            instance = await get_client().create_instance(
                region=region, plan=plan, os_id=os_id, label=label,
                startup_script=bp.startup_script or None,
                ssh_key_ids=body.ssh_key_ids or None,
                tags=list(bp.tags),
                idempotency_key=idem,
            )
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502,
                    upstream_status=e.status, idempotency_key=idem)
    except Exception as e:
        logger.exception("vultr_create_failed err=%s", e)
        return _err("INTERNAL", "Provision failed", 500, idempotency_key=idem)

    iid = instance.get("id", "")
    await register_instance(iid, {
        "user_id": str(user.get("_id", "anon")),
        "blueprint": bp.name, "region": region, "plan": plan,
        "request_id": req_id,
    })

    return {
        "success": True, "dry_run": False,
        "instance": {
            "id": iid,
            "label": instance.get("label", label),
            "region": instance.get("region", region),
            "plan": instance.get("plan", plan),
            "status": instance.get("status", "pending"),
            "main_ip": instance.get("main_ip"),
            "blueprint": bp.name,
            "tags": instance.get("tags", list(bp.tags)),
            "estimated_monthly_usd": bp.estimated_monthly_usd,
        },
        "idempotency_key": idem,
        "estimate": est,
    }


# ── List / get ──────────────────────────────────────────────────────────────

@router.get("/instances")
async def vultr_list(
    managed_only: bool = Query(True),
    user: dict = Depends(get_vultr_user),
) -> Any:
    if not settings.is_configured and not settings.dry_run:
        return _err("NOT_CONFIGURED", "VULTR_API_KEY missing", 503)
    try:
        instances = await get_client().list_instances()
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502, upstream_status=e.status)
    if managed_only:
        instances = [i for i in instances if "axelr-managed" in (i.get("tags") or [])]
    return {
        "success": True,
        "count": len(instances),
        "instances": [
            {
                "id": i.get("id"), "label": i.get("label"),
                "region": i.get("region"), "plan": i.get("plan"),
                "status": i.get("status"), "main_ip": i.get("main_ip"),
                "tags": i.get("tags", []), "created_at": i.get("date_created"),
            }
            for i in instances
        ],
    }


@router.get("/instances/{instance_id}")
async def vultr_get(
    instance_id: str, user: dict = Depends(get_vultr_user),
) -> Any:
    if not settings.is_configured and not settings.dry_run:
        return _err("NOT_CONFIGURED", "VULTR_API_KEY missing", 503)
    try:
        inst = await get_client().get_instance(instance_id)
    except VultrAPIError as e:
        code = 404 if e.status == 404 else 502
        return _err("VULTR_API_ERROR", str(e)[:400], code, upstream_status=e.status)
    return {"success": True, "instance": inst}


@router.post("/instances/{instance_id}/wait")
async def vultr_wait(
    instance_id: str,
    timeout_s: int = Query(240, ge=10, le=600),
    user: dict = Depends(get_vultr_user),
) -> Any:
    if not settings.is_configured and not settings.dry_run:
        return _err("NOT_CONFIGURED", "VULTR_API_KEY missing", 503)
    try:
        inst = await wait_for_active(
            get_client(), instance_id, timeout_s=min(timeout_s, settings.wait_max_s),
        )
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502, upstream_status=e.status)
    return {
        "success": True,
        "instance": {
            "id": inst.get("id", instance_id),
            "status": inst.get("status", "unknown"),
            "main_ip": inst.get("main_ip"),
            "timed_out": bool(inst.get("_timed_out")),
        },
    }


# ── Destroy ─────────────────────────────────────────────────────────────────

@router.delete("/instances/{instance_id}")
async def vultr_destroy(
    instance_id: str,
    confirm: bool = Query(False),
    user: dict = Depends(get_vultr_user),
) -> Any:
    if not settings.is_configured and not settings.dry_run:
        return _err("NOT_CONFIGURED", "VULTR_API_KEY missing", 503)
    if not confirm:
        return _err(
            "CONFIRM_REQUIRED",
            "Set confirm=true to destroy the instance. This is irreversible.",
            400,
        )
    try:
        async with AuditTimer(
            "vultr.destroy",
            user_id=str(user.get("_id", "anon")),
            session_id=new_request_id("vultr_del"),
            instance_id=instance_id,
        ):
            await get_client().delete_instance(instance_id)
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502, upstream_status=e.status)
    await unregister_instance(instance_id)
    return {"success": True, "destroyed": instance_id}


# ── Reboot / halt / start ──────────────────────────────────────────────────

@router.post("/instances/{instance_id}/reboot")
async def vultr_reboot(
    instance_id: str, user: dict = Depends(get_vultr_user),
) -> Any:
    if not settings.is_configured and not settings.dry_run:
        return _err("NOT_CONFIGURED", "VULTR_API_KEY missing", 503)
    try:
        await get_client().reboot_instance(instance_id)
        return {"success": True, "action": "reboot", "instance_id": instance_id}
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502, upstream_status=e.status)


@router.post("/instances/{instance_id}/halt")
async def vultr_halt(
    instance_id: str, user: dict = Depends(get_vultr_user),
) -> Any:
    if not settings.is_configured and not settings.dry_run:
        return _err("NOT_CONFIGURED", "VULTR_API_KEY missing", 503)
    try:
        await get_client().halt_instance(instance_id)
        return {"success": True, "action": "halt", "instance_id": instance_id}
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502, upstream_status=e.status)


@router.post("/instances/{instance_id}/start")
async def vultr_start(
    instance_id: str, user: dict = Depends(get_vultr_user),
) -> Any:
    if not settings.is_configured and not settings.dry_run:
        return _err("NOT_CONFIGURED", "VULTR_API_KEY missing", 503)
    try:
        await get_client().start_instance(instance_id)
        return {"success": True, "action": "start", "instance_id": instance_id}
    except VultrAPIError as e:
        return _err("VULTR_API_ERROR", str(e)[:400], 502, upstream_status=e.status)


# ── Orphans ────────────────────────────────────────────────────────────────

@router.get("/orphans")
async def vultr_orphans(
    max_age_hours: float = Query(24.0, ge=0.1, le=720),
    user: dict = Depends(get_vultr_user),
) -> Any:
    if not settings.is_configured and not settings.dry_run:
        return _err("NOT_CONFIGURED", "VULTR_API_KEY missing", 503)
    try:
        orphans = await find_orphans(get_client(), max_age_hours=max_age_hours)
    except Exception as e:
        logger.exception("orphan_scan_failed err=%s", e)
        return _err("INTERNAL", "Orphan scan failed", 500)
    return {
        "success": True, "count": len(orphans), "orphans": orphans,
        "total_estimated_cost_usd": round(
            sum(o["estimated_cost_usd"] for o in orphans), 2,
        ),
    }


@router.post("/orphans/cleanup")
async def vultr_cleanup(
    body: CleanupRequest, user: dict = Depends(get_vultr_user),
) -> Any:
    if not settings.is_configured and not settings.dry_run:
        return _err("NOT_CONFIGURED", "VULTR_API_KEY missing", 503)
    try:
        result = await cleanup_orphans(
            get_client(),
            max_age_hours=body.max_age_hours,
            dry_run=body.dry_run, confirm=body.confirm,
        )
    except Exception as e:
        logger.exception("orphan_cleanup_failed err=%s", e)
        return _err("INTERNAL", "Orphan cleanup failed", 500)
    if "error" in result:
        return _err("CONFIRM_REQUIRED", result["message"], 400)
    return {"success": True, **result}