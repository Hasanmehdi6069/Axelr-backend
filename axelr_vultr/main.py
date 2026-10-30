"""Appwrite Function entry + local demo runner.

Honest note: same `sys.exit(1)`-on-import bug as paypal. Fixed — the
dependency check runs only inside `_local_demo()`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Awaitable, Callable

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("axelr.vultr.fn")


class _MockRequest:
    def __init__(self, body: dict | None = None, headers: dict | None = None):
        self.bodyJson = body or {}
        self.headers = headers or {}


class _MockResponse:
    def json(self, data: dict, status_code: int = 200) -> dict:
        return {"body": json.dumps(data), "statusCode": status_code}


class AppwriteContext:
    def __init__(self, body: dict | None = None, headers: dict | None = None):
        self.req = _MockRequest(body, headers)
        self.res = _MockResponse()
        self.log = lambda m: logger.info(m)
        self.error = lambda m: logger.error(m)


async def _act_health(_body: dict) -> tuple[dict, int]:
    from .core import health_probe
    r = await health_probe()
    return r, 200 if r.get("healthy") else 503


async def _act_list_blueprints(_body: dict) -> tuple[dict, int]:
    from .templates import list_blueprints
    return {"success": True, "blueprints": list_blueprints()}, 200


async def _act_provision(body: dict) -> tuple[dict, int]:
    from .client import get_client
    from .core import VultrAPIError, new_request_id, settings
    from .lifecycle import register_instance
    from .schemas import CreateInstanceRequest
    from .templates import resolve_blueprint

    if not settings.is_configured and not settings.dry_run:
        return {"success": False, "error": {"code": "NOT_CONFIGURED"}}, 503
    try:
        req = CreateInstanceRequest(**{k: v for k, v in body.items() if k != "action"})
    except Exception as e:
        return {"success": False,
                "error": {"code": "VALIDATION", "message": str(e)[:300]}}, 422

    bp = resolve_blueprint(
        req.blueprint, region=req.region, plan=req.plan,
        os_id=req.os_id, startup_script=req.startup_script,
        tags=req.tags, default_region=settings.default_region,
    )
    if req.dry_run or settings.dry_run:
        return {
            "success": True, "dry_run": True,
            "blueprint": bp.name, "region": bp.region_default,
            "plan": bp.plan, "estimated_monthly_usd": bp.estimated_monthly_usd,
        }, 201
    idem = new_request_id("axelr_fn")
    try:
        inst = await get_client().create_instance(
            region=bp.region_default, plan=bp.plan, os_id=bp.os_id,
            label=req.label or f"axelr-{bp.name}-{idem[-6:]}",
            startup_script=bp.startup_script or None,
            tags=list(bp.tags), idempotency_key=idem,
        )
    except VultrAPIError as e:
        return {"success": False,
                "error": {"code": "VULTR", "message": str(e)[:400]}}, 502
    iid = inst.get("id", "")
    if iid:
        await register_instance(iid, {
            "user_id": body.get("user_id", "function"),
            "blueprint": bp.name, "region": bp.region_default, "plan": bp.plan,
        })
    return {
        "success": True,
        "instance": {
            "id": iid, "status": inst.get("status", "pending"),
            "label": inst.get("label"), "region": inst.get("region"),
            "plan": inst.get("plan"),
        },
        "idempotency_key": idem,
    }, 201


async def _act_list_instances(body: dict) -> tuple[dict, int]:
    from .client import get_client
    from .core import VultrAPIError, settings
    if not settings.is_configured and not settings.dry_run:
        return {"success": False, "error": {"code": "NOT_CONFIGURED"}}, 503
    try:
        insts = await get_client().list_instances()
    except VultrAPIError as e:
        return {"success": False,
                "error": {"code": "VULTR", "message": str(e)[:400]}}, 502
    if body.get("managed_only", True):
        insts = [i for i in insts if "axelr-managed" in (i.get("tags") or [])]
    return {"success": True, "count": len(insts), "instances": insts}, 200


async def _act_destroy(body: dict) -> tuple[dict, int]:
    from .client import get_client
    from .core import VultrAPIError, settings
    from .lifecycle import unregister_instance
    if not settings.is_configured and not settings.dry_run:
        return {"success": False, "error": {"code": "NOT_CONFIGURED"}}, 503
    if not body.get("confirm"):
        return {"success": False, "error": {"code": "CONFIRM_REQUIRED"}}, 400
    iid = body.get("instance_id")
    if not iid:
        return {"success": False, "error": {"code": "MISSING_INSTANCE_ID"}}, 400
    try:
        await get_client().delete_instance(iid)
        await unregister_instance(iid)
        return {"success": True, "destroyed": iid}, 200
    except VultrAPIError as e:
        return {"success": False,
                "error": {"code": "VULTR", "message": str(e)[:400]}}, 502


async def _act_orphans(body: dict) -> tuple[dict, int]:
    from .client import get_client
    from .core import settings
    from .lifecycle import find_orphans
    if not settings.is_configured and not settings.dry_run:
        return {"success": False, "error": {"code": "NOT_CONFIGURED"}}, 503
    max_age = float(body.get("max_age_hours", 24.0))
    orphans = await find_orphans(get_client(), max_age_hours=max_age)
    return {"success": True, "count": len(orphans), "orphans": orphans}, 200


_ACTIONS: dict[str, Callable[[dict], Awaitable[tuple[dict, int]]]] = {
    "health": _act_health,
    "list_blueprints": _act_list_blueprints,
    "provision": _act_provision,
    "list_instances": _act_list_instances,
    "destroy": _act_destroy,
    "orphans": _act_orphans,
}


async def main(context: AppwriteContext) -> dict[str, Any]:
    body = context.req.bodyJson or {}
    action = body.get("action", "health")
    handler = _ACTIONS.get(action)
    if handler is None:
        return context.res.json({
            "success": False,
            "error": {"code": "UNKNOWN_ACTION", "message": f"action={action}"},
            "available_actions": sorted(_ACTIONS),
        }, 400)
    try:
        result, st = await handler(body)
        return context.res.json(result, st)
    except Exception as e:
        logger.exception("vultr_fn_unhandled action=%s err=%s", action, e)
        return context.res.json({
            "success": False,
            "error": {"code": "INTERNAL", "message": str(e)[:300]},
        }, 500)


async def _local_demo() -> None:
    from .core import init, shutdown
    await init()
    try:
        print("=== health ===")
        print(await main(AppwriteContext({"action": "health"})))
        print("\n=== list_blueprints ===")
        print(await main(AppwriteContext({"action": "list_blueprints"})))
        print("\n=== provision (dry_run) ===")
        print(await main(AppwriteContext({
            "action": "provision", "blueprint": "web_node", "dry_run": True,
        })))
        if os.getenv("VULTR_API_KEY"):
            print("\n=== list_instances (live) ===")
            print(await main(AppwriteContext({"action": "list_instances"})))
        else:
            print("\n(skipping live list — VULTR_API_KEY not set)")
    finally:
        await shutdown()


if __name__ == "__main__":
    asyncio.run(_local_demo())