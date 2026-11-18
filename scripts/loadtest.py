"""Async load test — measures p50/p95/p99, RPS, error rate.

No external dependencies (stdlib only). Drives N concurrent workers against
a target endpoint at a configured rate. Prints a reproducible report.

Usage:
    python loadtest.py --url http://localhost:8000/vultr/health --rps 100 --duration 60
    python loadtest.py --url http://localhost:8000/vultr/metrics \
                       --token $AXELR_TOKEN --rps 200 --duration 30 --concurrency 50

Honest note: this measures *one process* against *one server*. Do not
extrapolate to multi-worker Gunicorn — rerun with your production stack.
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from dataclasses import dataclass, field

import urllib.request
from urllib.error import HTTPError, URLError


@dataclass
class Stats:
    latencies_ms: list[float] = field(default_factory=list)
    errors: int = 0
    success: int = 0

    def report(self, duration_s: float) -> dict:
        n = len(self.latencies_ms)
        if n == 0:
            return {"total": 0, "rps": 0, "errors": self.errors}
        s = sorted(self.latencies_ms)
        return {
            "total_requests": n + self.errors,
            "success": self.success,
            "errors": self.errors,
            "error_rate_pct": round(100 * self.errors / max(n + self.errors, 1), 2),
            "duration_s": round(duration_s, 2),
            "rps": round((n + self.errors) / duration_s, 2),
            "latency_ms": {
                "min": round(s[0], 2), "mean": round(statistics.mean(s), 2),
                "p50": round(s[int(0.50 * n)], 2),
                "p95": round(s[int(0.95 * (n - 1))], 2),
                "p99": round(s[int(0.99 * (n - 1))], 2),
                "max": round(s[-1], 2),
            },
        }


async def _one_request(url: str, headers: dict) -> float:
    """Single GET via urllib in a thread (stdlib only, no httpx dep)."""
    def _do() -> float:
        req = urllib.request.Request(url, headers=headers)
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
            return (time.perf_counter() - t0) * 1000
        except (HTTPError, URLError):
            return -1.0
    return await asyncio.to_thread(_do)


async def _worker(url: str, headers: dict, interval: float,
                  stop_at: float, stats: Stats, sem: asyncio.Semaphore) -> None:
    next_tick = time.monotonic()
    while time.monotonic() < stop_at:
        async with sem:
            ms = await _one_request(url, headers)
        if ms < 0:
            stats.errors += 1
        else:
            stats.latencies_ms.append(ms)
            stats.success += 1
        next_tick += interval
        sleep_s = next_tick - time.monotonic()
        if sleep_s > 0:
            await asyncio.sleep(sleep_s)


async def run(url: str, rps: int, duration: int, concurrency: int,
              token: str | None) -> dict:
    headers = {"accept": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    stats = Stats()
    sem = asyncio.Semaphore(concurrency)
    interval = 1.0 / rps
    stop_at = time.monotonic() + duration
    workers = [
        asyncio.create_task(_worker(url, headers, interval, stop_at, stats, sem))
        for _ in range(concurrency)
    ]
    t0 = time.monotonic()
    await asyncio.gather(*workers, return_exceptions=True)
    actual = time.monotonic() - t0
    return stats.report(actual)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--rps", type=int, default=100)
    p.add_argument("--duration", type=int, default=30)
    p.add_argument("--concurrency", type=int, default=20)
    p.add_argument("--token", default=None)
    args = p.parse_args()
    print(f"Target: {args.url}")
    print(f"Target rate: {args.rps} rps  |  Duration: {args.duration}s  "
          f"|  Concurrency: {args.concurrency}")
    print()
    report = asyncio.run(run(args.url, args.rps, args.duration,
                             args.concurrency, args.token))
    import json
    print(json.dumps(report, indent=2))
    if report.get("error_rate_pct", 100) > 5:
        print(f"\n⚠ error rate {report['error_rate_pct']}% exceeds 5% — "
              "investigate before shipping")
        raise SystemExit(1)


if __name__ == "__main__":
    main()