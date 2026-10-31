"""Live-endpoint verification suite.

Runs against a *running* deployment. Exit 0 iff every check passes.

Honest notes
------------
* Requires `BASE` (default http://127.0.0.1:8080) and either `TOKEN` (admin
  bearer JWT) OR `APPWRITE_SESSION` (Appwrite session secret) env vars. 
  Without either, auth-gated checks will fail.
* The latency-report check depends on the bulk-insert check above it
  having inserted ≥100 events into `verify_pipeline_001` within 60 min.
  That's why both use the same pipeline_id and window.
* This is an integration smoke test, not a unit suite. Unit tests live
  in `tests.py`.
"""
from __future__ import annotations

import json
import logging
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("insights.verify")


@dataclass
class Check:
    name: str
    passed: bool
    duration_ms: float
    detail: str
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "duration_ms": round(self.duration_ms, 2),
            "detail": self.detail,
            "ts": self.ts.isoformat(),
        }


class Verifier:
    def __init__(self, base_url: str | None = None,
                 token: str | None = None) -> None:
        self.base = (base_url or os.getenv("BASE", "http://127.0.0.1:8080")).rstrip("/")
        self.token = token or os.getenv("TOKEN", "")         # Bearer JWT
        self.session = os.getenv("APPWRITE_SESSION", "")     # Appwrite session secret
        
        self.headers = {"Content-Type": "application/json"}
        if self.token:
            self.headers["Authorization"] = f"Bearer {self.token}"
        if self.session:
            self.headers["X-Appwrite-Session"] = self.session
            
        self.results: list[Check] = []
        self.client = httpx.Client(
            base_url=self.base, timeout=30.0,
            headers=self.headers,
        )

    def _record(self, name: str, ok: bool, ms: float, detail: str = "") -> None:
        self.results.append(Check(name, ok, ms, detail))
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name:<45} {detail}  ({ms:.0f}ms)")

    def _get(self, path: str, **kw: Any) -> httpx.Response:
        return self.client.get(path, headers=self.headers, **kw)

    def _post(self, path: str, body: dict) -> httpx.Response:
        return self.client.post(path, headers=self.headers, json=body)

    # ── 1. Health ──

    def check_health(self) -> bool:
        print("\n[1] Health & infrastructure")
        ok_all = True
        t0 = time.perf_counter()
        try:
            r = self.client.get("/insights/health")
            ms = (time.perf_counter() - t0) * 1000
            ok = r.status_code == 200
            self._record("health_200", ok, ms, f"status={r.status_code}")
            ok_all &= ok
            if ok:
                d = r.json()
                for key in ("search_backend", "storage_backend"):
                    present = bool(d.get(key))
                    self._record(f"{key}_set", present, 0, str(d.get(key)))
                    ok_all &= present
                bh = d.get("backend_health") or {}
                self._record(
                    "backend_healthy", bool(bh.get("healthy")),
                    0, str(bh.get("reason") or bh.get("backend")),
                )
                ok_all &= bool(bh.get("healthy"))
            self._record("cold_start_within_5s", ms < 5000, ms, f"{ms:.0f}ms")
            ok_all &= ms < 5000
        except Exception as e:
            self._record(
                "health_reachable", False,
                (time.perf_counter() - t0) * 1000, str(e)[:100],
            )
            ok_all = False
        return ok_all

    # ── 2. Auth ──

    def check_auth(self) -> bool:
        print("\n[2] Authentication & authorization")
        ok_all = True
        t0 = time.perf_counter()
        r = self.client.post("/insights/search", json={"query": "x"})
        self._record(
            "anon_search_401", r.status_code == 401,
            (time.perf_counter() - t0) * 1000, f"status={r.status_code}",
        )
        ok_all &= r.status_code == 401

        t0 = time.perf_counter()
        r = self._post("/insights/search", {"query": "provider failover", "top_k": 3})
        ok = r.status_code == 200
        self._record(
            "authed_search_200", ok,
            (time.perf_counter() - t0) * 1000, f"status={r.status_code}",
        )
        ok_all &= ok
        if ok:
            hits = r.json().get("hits", [])
            self._record(
                "search_hits_list", isinstance(hits, list), 0, f"n={len(hits)}",
            )
            ok_all &= isinstance(hits, list)
        return ok_all

    # ── 3. Data operations ──

    def check_data(self) -> bool:
        print("\n[3] Data operations")
        ok_all = True

        t0 = time.perf_counter()
        r = self._post("/insights/audit/transaction", {
            "user_id": "verify_u1", "session_id": "verify_s1",
            "action": "integration_test", "details": {"phase": "verify"},
        })
        ok = r.status_code == 201
        self._record(
            "transaction_logged", ok,
            (time.perf_counter() - t0) * 1000, f"status={r.status_code}",
        )
        ok_all &= ok

        t0 = time.perf_counter()
        r = self._post("/insights/audit/node-execution", {
            "node_id": "verify_node_001", "pipeline_id": "verify_pipeline_001",
            "latency_ms": 42.5, "status": "ok",
        })
        ok = r.status_code == 201
        self._record(
            "node_execution_logged", ok,
            (time.perf_counter() - t0) * 1000, f"status={r.status_code}",
        )
        ok_all &= ok

        events = [{
            "kind": "node_execution", "node_id": f"bulk_{i}",
            "pipeline_id": "verify_pipeline_001",
            "latency_ms": 10.0 + (i % 50),
            "status": "ok" if i % 10 else "error",
        } for i in range(100)]
        t0 = time.perf_counter()
        r = self._post("/insights/audit/batch", {"events": events})
        dur = (time.perf_counter() - t0) * 1000
        ok = r.status_code == 202
        self._record("bulk_accepted", ok, dur, f"status={r.status_code}")
        ok_all &= ok
        if ok:
            n = r.json().get("inserted", 0)
            self._record("bulk_inserted_all", n == 100, 0, f"n={n}/100")
            ok_all &= n == 100
            self._record("bulk_under_3s", dur < 3000, dur, f"{dur:.0f}ms")
            ok_all &= dur < 3000
        return ok_all

    # ── 4. Analytics ──

    def check_analytics(self) -> bool:
        print("\n[4] Analytics & reporting")
        ok_all = True

        t0 = time.perf_counter()
        r = self._get(
            "/insights/audit/latency",
            params={"pipeline_id": "verify_pipeline_001", "window_minutes": 60},
        )
        ok = r.status_code == 200
        self._record(
            "latency_200", ok,
            (time.perf_counter() - t0) * 1000, f"status={r.status_code}",
        )
        ok_all &= ok
        if ok:
            d = r.json()
            c = d.get("count", 0)
            self._record("latency_count_ge_100", c >= 100, 0, f"count={c}")
            ok_all &= c >= 100
            for k in ("p50", "p95"):
                present = d.get(k) is not None
                self._record(f"{k}_present", present, 0, str(d.get(k)))
                ok_all &= present

        t0 = time.perf_counter()
        r = self._get("/insights/audit/stats", params={"window_minutes": 60})
        ok = r.status_code == 200
        self._record(
            "stats_200", ok,
            (time.perf_counter() - t0) * 1000, f"status={r.status_code}",
        )
        ok_all &= ok
        if ok:
            d = r.json()
            for key in ("total_events", "error_rate", "avg_latency_ms",
                        "p95_latency_ms"):
                present = key in d
                self._record(f"stats_field_{key}", present, 0, str(d.get(key)))
                ok_all &= present
        return ok_all

    # ── 5. Errors ──

    def check_errors(self) -> bool:
        print("\n[5] Error handling")
        ok_all = True

        r = self._get("/insights/audit/latency", params={"window_minutes": 99999})
        ok = r.status_code == 422
        self._record("invalid_window_422", ok, 0, f"status={r.status_code}")
        ok_all &= ok

        r = self._get("/insights/audit/pipelines/nonexistent_xyz/state")
        ok = r.status_code in (403, 404)
        self._record(
            "missing_resource_403or404", ok, 0, f"status={r.status_code}",
        )
        ok_all &= ok
        return ok_all

    # ── Report ──

    def report(self) -> tuple[int, dict[str, Any]]:
        passed = sum(1 for r in self.results if r.passed)
        failed = len(self.results) - passed
        durations = [r.duration_ms for r in self.results if r.duration_ms]
        summary = {
            "summary": {
                "total": len(self.results),
                "passed": passed,
                "failed": failed,
                "success_rate": (
                    round(passed / len(self.results), 4)
                    if self.results else 0.0
                ),
                "target": self.base,
                "ts": datetime.now(timezone.utc).isoformat(),
            },
            "checks": [r.as_dict() for r in self.results],
            "performance": {
                "avg_ms": round(statistics.mean(durations), 2) if durations else 0,
                "total_ms": round(sum(durations), 2),
            },
        }
        print("\n" + "=" * 78)
        print(
            f"  ✅ {passed} passed   ❌ {failed} failed   "
            f"({summary['summary']['success_rate']*100:.1f}%)"
        )
        print(f"  ⏱  total {summary['performance']['total_ms']:.0f}ms")
        print("=" * 78)
        if failed:
            print("\n  Failed:")
            for r in self.results:
                if not r.passed:
                    print(f"    - {r.name}: {r.detail}")
        return (0 if failed == 0 else 1), summary

    def run(self, export_path: str | None = None) -> int:
        ok = True
        for check in (self.check_health, self.check_auth, self.check_data,
                      self.check_analytics, self.check_errors):
            try:
                ok &= check()
            except Exception:
                logger.exception("check crashed: %s", check.__name__)
                ok = False
        code, summary = self.report()
        if export_path:
            with open(export_path, "w") as f:
                json.dump(summary, f, indent=2)
            print(f"\n  Results written to {export_path}")
        self.client.close()
        return code


def main() -> int:
    return Verifier().run(export_path=os.getenv("VERIFICATION_EXPORT_PATH"))


if __name__ == "__main__":
    sys.exit(main())