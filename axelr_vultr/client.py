# axelr_vultr/client.py
"""Async Vultr REST client — instances, regions, plans, account, snapshots."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .core import (
    VultrAPIError, _get_http, limiter, retry, settings,
)

logger = logging.getLogger("axelr.vultr.client")


class VultrClient:
    """Async Vultr v2 client with rate limiting and retry baked in."""

    def __init__(self) -> None:
        self._account_cache: dict[str, Any] | None = None
        self._plans_cache: dict[str, dict[str, Any]] = {}
        self._regions_cache: list[dict[str, Any]] | None = None
        self._cache_lock = asyncio.Lock()

    # ── Low-level request ───────────────────────────────────────────────

    async def _request(
        self, method: str, path: str, *,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not settings.is_configured:
            raise VultrAPIError(503, "vultr_not_configured")
        await limiter().acquire(cost=1.0)

        async def _do() -> dict[str, Any]:
            headers = {"Authorization": f"Bearer {settings.api_key}"}
            if idempotency_key:
                headers["X-Request-Id"] = idempotency_key
            http = _get_http()
            try:
                r = await http.request(method, path, headers=headers, json=body)
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                raise VultrAPIError(504, f"network:{e}") from e
            if r.status_code >= 400:
                try:
                    payload = r.json()
                except Exception:
                    payload = r.text[:400]
                raise VultrAPIError(r.status_code, payload)
            if r.status_code == 204 or not r.content:
                return {}
            try:
                return r.json()
            except Exception as e:
                raise VultrAPIError(502, f"invalid_json:{e}") from e
        return await retry(_do)

    # ── Account ─────────────────────────────────────────────────────────

    async def get_account(self) -> dict[str, Any]:
        result = await self._request("GET", "/account")
        return result.get("account", result)

    # ── Regions ─────────────────────────────────────────────────────────

    async def list_regions(self, refresh: bool = False) -> list[dict[str, Any]]:
        async with self._cache_lock:
            if self._regions_cache is not None and not refresh:
                return self._regions_cache
            result = await self._request("GET", "/regions")
            self._regions_cache = result.get("regions", [])
            return self._regions_cache

    # ── Plans ───────────────────────────────────────────────────────────

    async def list_plans(
        self, *, region: str | None = None,
        plan_type: str | None = None,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        cache_key = f"{region or 'all'}::{plan_type or 'all'}"
        async with self._cache_lock:
            if not refresh and cache_key in self._plans_cache:
                return list(self._plans_cache[cache_key].values()) if isinstance(
                    self._plans_cache[cache_key], dict
                ) else self._plans_cache[cache_key]

        params: list[str] = []
        if region:
            params.append(f"region={region}")
        if plan_type:
            params.append(f"type={plan_type}")
        path = "/plans" + (f"?{'&'.join(params)}" if params else "")
        result = await self._request("GET", path)
        plans = result.get("plans", [])
        async with self._cache_lock:
            self._plans_cache[cache_key] = {p["id"]: p for p in plans}
        return plans

    async def get_plan(self, plan_id: str, *, region: str | None = None) -> dict[str, Any]:
        plans = await self.list_plans(region=region)
        for p in plans:
            if p.get("id") == plan_id:
                return p
        raise VultrAPIError(404, f"plan_not_found:{plan_id}")

    # ── OS ──────────────────────────────────────────────────────────────

    async def list_os(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "/os")
        return result.get("os", [])

    # ── Instances — list / get ──────────────────────────────────────────

    async def list_instances(self, *, page_size: int = 100) -> list[dict[str, Any]]:
        """Paginated list of all instances."""
        out: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(50):  # hard cap 5000 instances — well above any sane account
            path = f"/instances?per_page={page_size}"
            if cursor:
                path += f"&cursor={cursor}"
            result = await self._request("GET", path)
            out.extend(result.get("instances", []))
            meta = result.get("meta", {}) or {}
            links = meta.get("links", {}) or {}
            next_url = links.get("next")
            if not next_url:
                break
            cursor = meta.get("cursor", "") or ""
            if not cursor:
                break
        return out

    async def get_instance(self, instance_id: str) -> dict[str, Any]:
        result = await self._request("GET", f"/instances/{instance_id}")
        return result.get("instance", result)

    # ── Instances — mutating ────────────────────────────────────────────

    async def create_instance(
        self, *,
        region: str,
        plan: str,
        os_id: int,
        label: str,
        hostname: str | None = None,
        startup_script: str | None = None,
        ssh_key_ids: list[str] | None = None,
        tags: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "region": region,
            "plan": plan,
            "os_id": os_id,
            "label": label[:64],
            "hostname": (hostname or label)[:64],
            "backups": "disabled",
            "ddos_protection": False,
            "enable_ipv6": True,
        }
        if startup_script:
            body["user_data"] = startup_script
        if ssh_key_ids:
            body["sshkey_id"] = ssh_key_ids
        if tags:
            body["tags"] = tags
        logger.info(
            "vultr_create_instance region=%s plan=%s os_id=%s label=%s",
            region, plan, os_id, label,
        )
        result = await self._request(
            "POST", "/instances", body=body, idempotency_key=idempotency_key,
        )
        return result.get("instance", result)

    async def delete_instance(self, instance_id: str) -> bool:
        logger.info("vultr_delete_instance instance_id=%s", instance_id)
        await self._request("DELETE", f"/instances/{instance_id}")
        return True

    async def reboot_instance(self, instance_id: str) -> bool:
        await self._request("POST", f"/instances/{instance_id}/reboot", body={})
        return True

    async def halt_instance(self, instance_id: str) -> bool:
        await self._request("POST", f"/instances/{instance_id}/halt", body={})
        return True

    async def start_instance(self, instance_id: str) -> bool:
        await self._request("POST", f"/instances/{instance_id}/start", body={})
        return True

    async def reinstall_instance(self, instance_id: str, os_id: int) -> bool:
        await self._request(
            "POST", f"/instances/{instance_id}/reinstall",
            body={"os_id": os_id},
        )
        return True

    # ── Cost helpers ────────────────────────────────────────────────────

    @staticmethod
    def _hourly(monthly: float) -> float:
        # Vultr prices monthly assuming 730 hours.
        return round(monthly / 730.0, 6)

    async def estimate_cost(
        self, plan_id: str, hours: float, *,
        region: str | None = None,
    ) -> dict[str, Any]:
        plan = await self.get_plan(plan_id, region=region)
        monthly = float(plan.get("monthly_cost", 0.0))
        hourly = self._hourly(monthly)
        # Vultr bills hourly up to the monthly cap.
        estimated = min(hourly * hours, monthly)
        remaining = max(0.0, settings.monthly_budget_usd - await self.current_spend())
        return {
            "plan_id": plan_id,
            "hours": hours,
            "hourly_cost_usd": hourly,
            "monthly_cost_usd": monthly,
            "estimated_usd": round(estimated, 4),
            "over_budget": estimated > remaining,
            "monthly_budget_usd": settings.monthly_budget_usd,
            "remaining_budget_usd": round(remaining, 2),
        }

    async def current_spend(self) -> float:
        """Sum `pending_charges` from Vultr account if available."""
        try:
            account = await self.get_account()
            return float(account.get("pending_charges") or 0.0)
        except Exception:
            return 0.0


_client: VultrClient | None = None


def get_client() -> VultrClient:
    global _client
    if _client is None:
        _client = VultrClient()
    return _client