# axelr_vultr/lifecycle.py
"""Lifecycle management — state polling, registry, orphan detection."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger("axelr.vultr.lifecycle")

_REG_PREFIX = "axelr:vultr:registry:"
_LOCAL_REGISTRY: dict[str, dict[str, Any]] = {}
_REG_TTL_S = 86_400


class InstanceState(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    STOPPED = "stopped"
    SUSPENDED = "suspended"
    UNKNOWN = "unknown"


# ── State machine ───────────────────────────────────────────────────────────

async def wait_for_active(
    client, instance_id: str, *,
    timeout_s: int = 240,
    poll_interval_s: float = 4.0,
) -> dict[str, Any]:
    """Poll `get_instance` until status == active or timeout. Returns final state."""
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            last = await client.get_instance(instance_id)
        except Exception as e:
            logger.debug("wait_for_active_poll_err instance_id=%s err=%s", instance_id, e)
            await asyncio.sleep(poll_interval_s)
            continue
        status = last.get("status", "unknown")
        if status == "active":
            return last
        if status in ("suspended", "stopped"):
            logger.info("wait_for_active_early_exit instance_id=%s status=%s",
                        instance_id, status)
            return last
        await asyncio.sleep(poll_interval_s)
    logger.warning("wait_for_active_timeout instance_id=%s", instance_id)
    last["_timed_out"] = True
    return last


# ── Registry (Redis first, in-memory fallback) ──────────────────────────────

async def _redis():
    try:
        from api.state import state  # type: ignore
        return getattr(state, "redis_client", None)
    except Exception:
        return None


async def register_instance(instance_id: str, meta: dict[str, Any]) -> None:
    """Persist this instance in Axelr's registry so we can detect orphans later."""
    meta = dict(meta)
    meta["registered_at"] = time.time()
    redis = await _redis()
    if redis is not None:
        try:
            import json
            await redis.setex(
                f"{_REG_PREFIX}{instance_id}", _REG_TTL_S, json.dumps(meta),
            )
            return
        except Exception as e:
            logger.debug("registry_redis_failed err=%s", e)
    _LOCAL_REGISTRY[instance_id] = meta


async def unregister_instance(instance_id: str) -> None:
    redis = await _redis()
    if redis is not None:
        try:
            await redis.delete(f"{_REG_PREFIX}{instance_id}")
        except Exception:
            pass
    _LOCAL_REGISTRY.pop(instance_id, None)


async def get_registry() -> dict[str, dict[str, Any]]:
    """Return {instance_id: meta}. Falls back to in-memory if Redis unavailable."""
    redis = await _redis()
    if redis is None:
        return dict(_LOCAL_REGISTRY)
    try:
        import json
        out: dict[str, dict[str, Any]] = {}
        async for key in redis.scan_iter(f"{_REG_PREFIX}*"):
            val = await redis.get(key)
            if val:
                iid = key.split(_REG_PREFIX, 1)[-1]
                out[iid] = json.loads(val)
        if out:
            return out
    except Exception as e:
        logger.debug("registry_scan_failed err=%s", e)
    return dict(_LOCAL_REGISTRY)


# ── Orphan detection ────────────────────────────────────────────────────────

def _age_hours(created_at: str | None) -> float:
    if not created_at:
        return 0.0
    try:
        if created_at.endswith("Z"):
            created_at = created_at[:-1] + "+00:00"
        dt = datetime.fromisoformat(created_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0)
    except Exception:
        return 0.0


async def find_orphans(
    client, *, max_age_hours: float = 24.0,
) -> list[dict[str, Any]]:
    """List Axelr-tagged instances not in the registry, or past max_age_hours."""
    registry = await get_registry()
    try:
        instances = await client.list_instances()
    except Exception as e:
        logger.warning("find_orphans_list_failed err=%s", e)
        return []
    orphans: list[dict[str, Any]] = []
    for inst in instances:
        tags = inst.get("tags") or []
        is_managed = "axelr-managed" in tags
        if not is_managed:
            continue
        iid = inst.get("id", "")
        age = _age_hours(inst.get("date_created"))
        in_registry = iid in registry
        if in_registry and age < max_age_hours:
            continue
        plan_id = inst.get("plan", "")
        monthly = float((inst.get("plan_metadata") or {}).get("monthly_cost") or 0.0)
        if not monthly:
            try:
                p = await client.get_plan(plan_id, region=inst.get("region"))
                monthly = float(p.get("monthly_cost", 0.0))
            except Exception:
                monthly = 0.0
        hourly = monthly / 730.0 if monthly else 0.0
        orphans.append({
            "id": iid,
            "label": inst.get("label", ""),
            "region": inst.get("region", ""),
            "plan": plan_id,
            "status": inst.get("status", "unknown"),
            "created_at": inst.get("date_created"),
            "age_hours": round(age, 2),
            "tags": tags,
            "estimated_cost_usd": round(hourly * age, 4),
        })
    orphans.sort(key=lambda o: o["age_hours"], reverse=True)
    return orphans


async def cleanup_orphans(
    client, *, max_age_hours: float = 24.0,
    dry_run: bool = True, confirm: bool = False,
) -> dict[str, Any]:
    """Destroy orphans. Requires confirm=True unless dry_run=True."""
    if not dry_run and not confirm:
        return {
            "error": "confirm_required",
            "message": "Set confirm=true to destroy orphans, or dry_run=true to preview.",
        }
    orphans = await find_orphans(client, max_age_hours=max_age_hours)
    destroyed: list[str] = []
    failed: list[dict[str, str]] = []
    for o in orphans:
        if dry_run:
            continue
        try:
            await client.delete_instance(o["id"])
            await unregister_instance(o["id"])
            destroyed.append(o["id"])
        except Exception as e:
            failed.append({"id": o["id"], "error": str(e)[:200]})
    return {
        "dry_run": dry_run,
        "candidates": orphans,
        "destroyed": destroyed,
        "failed": failed,
        "total_estimated_cost_usd": round(
            sum(o["estimated_cost_usd"] for o in orphans), 2,
        ),
    }