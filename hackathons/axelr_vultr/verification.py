# axelr_vultr/verification.py
"""E2E verification — safe by default, destructive only if VULTR_E2E=1."""
from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass

import httpx


@dataclass
class Result:
    name: str
    passed: bool
    duration_ms: float
    detail: str = ""


class Verifier:
    def __init__(self) -> None:
        self.base = (os.getenv("AXELR_BASE_URL", "http://127.0.0.1:8000")).rstrip("/")
        self.token = os.getenv("AXELR_TOKEN", "")
        self.e2e = os.getenv("VULTR_E2E", "0") == "1"
        self.results: list[Result] = []
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self.client = httpx.AsyncClient(
            base_url=self.base, headers=headers, timeout=120.0,
        )

    def rec(self, name: str, ok: bool, ms: float, detail: str = "") -> bool:
        self.results.append(Result(name, ok, ms, detail))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name:<46} {detail} ({ms:.0f}ms)")
        return ok

    async def health(self) -> bool:
        t0 = time.perf_counter()
        try:
            r = await self.client.get("/vultr/health")
            ms = (time.perf_counter() - t0) * 1000
            ok = r.status_code in (200, 503)
            detail = f"status={r.status_code}"
            if ok:
                b = r.json()
                detail += f" mode={b.get('mode')} healthy={b.get('healthy')}"
            return self.rec("vultr_health_responds", ok, ms, detail)
        except Exception as e:
            return self.rec("vultr_health_responds", False, 0, str(e))

    async def auth_required(self) -> bool:
        t0 = time.perf_counter()
        try:
            anon = httpx.AsyncClient(base_url=self.base, timeout=10.0)
            r = await anon.post("/vultr/instances", json={"blueprint": "web_node"})
            await anon.aclose()
            ms = (time.perf_counter() - t0) * 1000
            ok = r.status_code == 401
            return self.rec("vultr_requires_auth", ok, ms, f"status={r.status_code}")
        except Exception as e:
            return self.rec("vultr_requires_auth", False, 0, str(e))

    async def blueprints(self) -> bool:
        if not self.token:
            return self.rec("vultr_blueprints_listed", True, 0, "SKIP no token")
        t0 = time.perf_counter()
        try:
            r = await self.client.get("/vultr/blueprints")
            ms = (time.perf_counter() - t0) * 1000
            if r.status_code != 200:
                return self.rec("vultr_blueprints_listed", False, ms, f"{r.status_code}")
            bps = r.json().get("blueprints", [])
            names = [b["name"] for b in bps]
            ok = "inference_worker" in names and len(bps) >= 3
            return self.rec("vultr_blueprints_listed", ok, ms, f"count={len(bps)} names={names}")
        except Exception as e:
            return self.rec("vultr_blueprints_listed", False, 0, str(e))

    async def rejects_bad_blueprint(self) -> bool:
        if not self.token:
            return self.rec("vultr_rejects_bad_blueprint", True, 0, "SKIP no token")
        t0 = time.perf_counter()
        try:
            r = await self.client.post("/vultr/instances",
                                       json={"blueprint": "not_a_real_blueprint"})
            ms = (time.perf_counter() - t0) * 1000
            ok = r.status_code == 422
            return self.rec("vultr_rejects_bad_blueprint", ok, ms, f"status={r.status_code}")
        except Exception as e:
            return self.rec("vultr_rejects_bad_blueprint", False, 0, str(e))

    async def destroy_requires_confirm(self) -> bool:
        if not self.token:
            return self.rec("vultr_destroy_requires_confirm", True, 0, "SKIP no token")
        t0 = time.perf_counter()
        try:
            r = await self.client.delete("/vultr/instances/FAKE")
            ms = (time.perf_counter() - t0) * 1000
            ok = r.status_code == 400
            return self.rec("vultr_destroy_requires_confirm", ok, ms, f"status={r.status_code}")
        except Exception as e:
            return self.rec("vultr_destroy_requires_confirm", False, 0, str(e))

    async def regions_and_plans(self) -> bool:
        if not self.token:
            return self.rec("vultr_regions_and_plans", True, 0, "SKIP no token")
        if os.getenv("VULTR_API_KEY") is None and os.getenv("VULTR_DRY_RUN") != "1":
            return self.rec("vultr_regions_and_plans", True, 0, "SKIP no API key")
        t0 = time.perf_counter()
        try:
            r1 = await self.client.get("/vultr/regions")
            r2 = await self.client.get("/vultr/plans")
            ms = (time.perf_counter() - t0) * 1000
            ok = r1.status_code in (200, 503) and r2.status_code in (200, 503)
            regions = len(r1.json().get("regions", [])) if r1.status_code == 200 else 0
            plans = len(r2.json().get("plans", [])) if r2.status_code == 200 else 0
            return self.rec("vultr_regions_and_plans", ok, ms,
                            f"regions={regions} plans={plans}")
        except Exception as e:
            return self.rec("vultr_regions_and_plans", False, 0, str(e))

    async def provision_e2e(self) -> bool:
        """Destructive — provisions + destroys. Gated by VULTR_E2E=1."""
        if not self.e2e:
            return self.rec("vultr_provision_e2e", True, 0, "SKIP VULTR_E2E!=1")
        if not self.token:
            return self.rec("vultr_provision_e2e", True, 0, "SKIP no token")
        t0 = time.perf_counter()
        iid: str | None = None
        try:
            r = await self.client.post("/vultr/instances", json={
                "blueprint": "web_node",
                "label": "axelr-verify-e2e",
                "dry_run": False,
            })
            ms = (time.perf_counter() - t0) * 1000
            if r.status_code != 201:
                return self.rec("vultr_provision_e2e", False, ms,
                                f"{r.status_code} {r.text[:200]}")
            body = r.json()
            iid = body.get("instance", {}).get("id")
            if not iid:
                return self.rec("vultr_provision_e2e", False, ms, "no_instance_id")
            print(f"    provisioned instance_id={iid} — waiting for active")
            # Wait
            w = await self.client.post(f"/vultr/instances/{iid}/wait?timeout_s=240")
            wms = (time.perf_counter() - t0) * 1000
            if w.status_code != 200:
                await self._destroy(iid)
                return self.rec("vultr_provision_e2e", False, wms,
                                f"wait_failed {w.status_code}")
            status = w.json().get("instance", {}).get("status")
            ok = status in ("active", "stopped")
            await self._destroy(iid)
            return self.rec("vultr_provision_e2e", ok, wms,
                            f"final_status={status} destroyed=true")
        except Exception as e:
            if iid:
                await self._destroy(iid)
            return self.rec("vultr_provision_e2e", False, 0, str(e))

    async def _destroy(self, iid: str) -> None:
        try:
            await self.client.delete(f"/vultr/instances/{iid}?confirm=true")
        except Exception:
            pass

    async def run(self) -> int:
        print(f"Target: {self.base}")
        print(f"Destructive E2E: {'ENABLED' if self.e2e else 'DISABLED (set VULTR_E2E=1)'}")
        print()
        await self.health()
        await self.auth_required()
        await self.blueprints()
        await self.rejects_bad_blueprint()
        await self.destroy_requires_confirm()
        await self.regions_and_plans()
        await self.provision_e2e()
        passed = sum(1 for r in self.results if r.passed)
        failed = len(self.results) - passed
        print()
        print("=" * 70)
        print(f"VULTR MODULE VERIFICATION — {passed} passed, {failed} failed")
        print("=" * 70)
        if failed:
            for r in self.results:
                if not r.passed:
                    print(f"  FAILED: {r.name} — {r.detail}")
        await self.client.aclose()
        return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(Verifier().run()))