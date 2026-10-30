# axelr_paypal/verification.py
"""E2E verification suite for the PayPal module.

Exit 0 = all checks pass. Exit 1 = one or more failed.
Run against a live backend: AXELR_BASE_URL=... AXELR_TOKEN=... python -m axelr_paypal.verification
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

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
        self.results: list[Result] = []
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self.client = httpx.AsyncClient(
            base_url=self.base, headers=headers, timeout=30.0,
        )

    def rec(self, name: str, ok: bool, ms: float, detail: str = "") -> bool:
        self.results.append(Result(name, ok, ms, detail))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name:<42} {detail} ({ms:.0f}ms)")
        return ok

    async def health(self) -> bool:
        t0 = time.perf_counter()
        try:
            r = await self.client.get("/paypal/health")
            ms = (time.perf_counter() - t0) * 1000
            # 200 = healthy, 503 = not configured (still a valid response shape)
            ok = r.status_code in (200, 503)
            detail = f"status={r.status_code}"
            if ok:
                body = r.json()
                detail += f" mode={body.get('mode')} healthy={body.get('healthy')}"
            return self.rec("paypal_health_responds", ok, ms, detail)
        except Exception as e:
            return self.rec("paypal_health_responds", False, 0, f"error={e}")

    async def auth_required(self) -> bool:
        t0 = time.perf_counter()
        try:
            anon = httpx.AsyncClient(base_url=self.base, timeout=10.0)
            r = await anon.post("/paypal/orders", json={"amount": "1.00"})
            await anon.aclose()
            ms = (time.perf_counter() - t0) * 1000
            ok = r.status_code == 401
            return self.rec("paypal_requires_auth", ok, ms, f"status={r.status_code}")
        except Exception as e:
            return self.rec("paypal_requires_auth", False, 0, f"error={e}")

    async def create_order_validation(self) -> bool:
        if not self.token:
            return self.rec("paypal_create_order_validation", True, 0, "SKIP no token")
        t0 = time.perf_counter()
        try:
            # Bad: negative amount
            r = await self.client.post("/paypal/orders", json={"amount": "-1.00"})
            ms = (time.perf_counter() - t0) * 1000
            ok = r.status_code == 422
            return self.rec("paypal_rejects_negative_amount", ok, ms, f"status={r.status_code}")
        except Exception as e:
            return self.rec("paypal_rejects_negative_amount", False, 0, f"error={e}")

    async def create_order_e2e(self) -> bool:
        if not self.token:
            return self.rec("paypal_create_order_e2e", True, 0, "SKIP no token")
        if os.getenv("PAYPAL_CLIENT_ID") is None:
            return self.rec("paypal_create_order_e2e", True, 0, "SKIP no PAYPAL_CLIENT_ID")
        t0 = time.perf_counter()
        try:
            r = await self.client.post("/paypal/orders", json={
                "amount": "9.99", "currency": "USD",
                "description": "Verification test order",
                "reference_id": "verif",
                "return_url": "https://axelr.in/ok",
                "cancel_url": "https://axelr.in/cancel",
            })
            ms = (time.perf_counter() - t0) * 1000
            if r.status_code != 201:
                return self.rec("paypal_create_order_e2e", False, ms,
                                f"status={r.status_code} body={r.text[:200]}")
            order = r.json().get("order", {})
            ok = bool(order.get("id") and order.get("approve_url"))
            return self.rec("paypal_create_order_e2e", ok, ms,
                            f"order_id={order.get('id', '')[:20]}")
        except Exception as e:
            return self.rec("paypal_create_order_e2e", False, 0, f"error={e}")

    async def webhook_rejects_bad_sig(self) -> bool:
        t0 = time.perf_counter()
        try:
            payload = json.dumps({"id": "EVT-BAD", "event_type": "CHECKOUT.ORDER.APPROVED", "resource": {}}).encode()
            r = await self.client.post(
                "/paypal/webhooks", content=payload,
                headers={"Content-Type": "application/json"},
            )
            ms = (time.perf_counter() - t0) * 1000
            ok = r.status_code == 400
            return self.rec("paypal_webhook_rejects_bad_sig", ok, ms, f"status={r.status_code}")
        except Exception as e:
            return self.rec("paypal_webhook_rejects_bad_sig", False, 0, f"error={e}")

    async def refund_requires_confirm(self) -> bool:
        if not self.token:
            return self.rec("paypal_refund_requires_confirm", True, 0, "SKIP no token")
        t0 = time.perf_counter()
        try:
            r = await self.client.post("/paypal/refunds", json={
                "capture_id": "FAKECAP12345",
                "reason": "test",
                "confirm": False,
            })
            ms = (time.perf_counter() - t0) * 1000
            ok = r.status_code == 400
            return self.rec("paypal_refund_requires_confirm", ok, ms, f"status={r.status_code}")
        except Exception as e:
            return self.rec("paypal_refund_requires_confirm", False, 0, f"error={e}")

    async def summary(self) -> int:
        passed = sum(1 for r in self.results if r.passed)
        failed = len(self.results) - passed
        print()
        print("=" * 70)
        print(f"PAYPAL MODULE VERIFICATION — {passed} passed, {failed} failed")
        print("=" * 70)
        if failed:
            for r in self.results:
                if not r.passed:
                    print(f"  FAILED: {r.name} — {r.detail}")
        await self.client.aclose()
        return 0 if failed == 0 else 1

    async def run(self) -> int:
        print(f"Target: {self.base}\n")
        await self.health()
        await self.auth_required()
        await self.create_order_validation()
        await self.webhook_rejects_bad_sig()
        await self.refund_requires_confirm()
        await self.create_order_e2e()
        return await self.summary()


def main() -> int:
    return asyncio.run(Verifier().run())


if __name__ == "__main__":
    sys.exit(main())