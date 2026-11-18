"""ELITE MASTER TEST SUITE — Axelr Ops Room
=========================================
Modes:
  smoke    2-client relay verification (fastest sanity check)
  demo     scripted incident lifecycle + chaos + recovery
  stress   2500 incidents / 60 dashboards / 20% chaos
  extreme  5000 incidents / 100 dashboards / 35% chaos + live screen
  full     smoke → demo → stress, sequentially

This is a *test tool*. It intentionally exercises the dashboard's red/yellow
paths (real error levels, real chaos events) so you can verify the UI handles
them. If you want a 100% healthy stream, run `python broadcast.py --stdin`
instead — its auto-log generator never fabricates warn/err.

Examples:
  python master_test.py --mode smoke
  python master_test.py --mode demo
  python master_test.py --mode stress --incidents 40 --dashboards 4 --timeout 300
  python master_test.py --mode extreme --incidents 20 --dashboards 3 --no-clear
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import websockets
from websockets.exceptions import ConnectionClosed, ConnectionClosedError


# ═══════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════

def _setup_logging(log_file: str, verbose: bool) -> logging.Logger:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)-5s %(message)s"
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    logging.basicConfig(
        level=level,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, mode="w", encoding="utf-8"),
        ],
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    return logging.getLogger("master")


# ═══════════════════════════════════════════════════════════════════════════
# TEMPLATES / SERVICES / CHAOS
# ═══════════════════════════════════════════════════════════════════════════

SERVICES = [
    "api-gateway", "user-auth", "payment-processor", "search-service",
    "recommendation-engine", "streaming-ingest", "analytics-pipeline",
    "notification-service", "billing-system", "inventory-manager",
]

INCIDENT_TEMPLATES = [
    "PG-SOFT-{id}: PostgreSQL primary node {id} connection pool drained - {conns} active connections",
    "REDIS-OOM-{id}: Redis cluster {id} out of memory - evicting {keys} keys",
    "MONGO-SLOW-{id}: MongoDB query latency >{latency}s on collection {coll}",
    "NET-PART-{id}: AZ {az} network partition - {svcs} services unreachable",
    "DNS-FAIL-{id}: DNS resolution failing for {domain} - {fails} consecutive failures",
    "LB-5XX-{id}: Load balancer {lb} reporting {pct}% 5XX errors",
    "K8S-POD-{id}: {pods} pods crashing in deployment {deploy}",
    "K8S-NODE-{id}: Node {node} disk pressure - {pct}% usage",
    "K8S-CPU-{id}: Cluster CPU throttling - {pct}% utilization across {nodes} nodes",
    "APP-OOM-{id}: JVM {app} OOMKill - heap size {heap}GB",
    "APP-LAT-{id}: {app} P95 latency {latency}s (SLO violation)",
    "AUTH-FAIL-{id}: Auth service {id} rejecting {reqs}rps - token expiration bug",
    "PAY-FAIL-{id}: Payment processor {processor} {fails}% decline rate",
    "RATELIMIT-{id}: API rate limits exhausted for {key} - {reqs} blocked in {window}s",
    "CACHE-MISS-{id}: CDN cache miss rate {pct}% - origin flooded",
    "WARM-FAIL-{id}: Cache warmup failed - {keys} keys not populated",
    "WAF-BLOCK-{id}: WAF blocking {reqs}rps from {asn} - false positive storm",
    "CVE-2024-{id}: Log4j vulnerability exploitation attempts detected - {attempts} attempts",
    "pod eviction on node-{node} - {service} unresponsive",
    "database connection pool exhausted - {pool_connections} connections used",
    "5xx error rate spiking to {pct}% on {service}",
    "API latency increased to {latency}ms for {endpoint}",
    "node {node} disk at {pct}% utilization - critical",
    "Kubernetes pod {pod} in CrashLoopBackOff - restarted {count}x",
    "CDN cache miss ratio {pct}% - origin servers overwhelmed",
    "Redis cluster failover - {shard} primary down",
    "Message queue lag {lag} messages - consumers not keeping up",
    "SSL certificate expiring in {days} days - {domain}",
    "GPU utilization {pct}% on worker {node} - ML jobs queued",
    "S3 API throttling - {rps} requests/sec hitting limits",
    "Network partition between zone {zone1} and {zone2}",
    "PostgreSQL transaction log size {size}GB - disk filling",
    "Elasticsearch cluster health RED - {shards} unassigned shards",
]

CHAOS_EVENTS = [
    {"mode": "appwrite",   "duration": (5000, 15000),  "message": "Appwrite database outage - network partition"},
    {"mode": "switch",     "duration": (3000, 8000),   "message": "Switch bridge websocket disconnect"},
    {"mode": "network",    "duration": (2000, 6000),   "message": "Inter-AZ network latency spike to {latency}ms"},
    {"mode": "disk",       "duration": (10000, 30000), "message": "Disk I/O wait {wait}ms - storage array degradation"},
    {"mode": "rate_limit", "duration": (4000, 10000),  "message": "API rate limits triggered - {blocked} requests blocked"},
]

DEMO_SCRIPT = [
    (2.0, {"kind": "system",   "level": "ok",   "text": "System initialized, waiting for incidents..."}),
    (1.5, {"kind": "switch",   "level": "err",  "text": "INC-1234: Production database connection pool exhausted",
           "runbook": {"incident_id": "INC1234", "status": "detected"}}),
    (2.0, {"kind": "switch",   "level": "info", "text": "Switch agent: Analyzing incident patterns from past runbooks...",
           "runbook": {"incident_id": "INC1234", "status": "analyzing"}}),
    (1.5, {"kind": "switch",   "level": "info", "text": "Switch agent: Found similar incident — recommend restarting connection pool",
           "runbook": {"incident_id": "INC1234", "status": "suggested_fix"}}),
    (2.0, {"kind": "switch",   "level": "ok",   "text": "Human engineer: Approved — executing connection pool restart",
           "runbook": {"incident_id": "INC1234", "status": "executing"}}),
    (1.5, {"kind": "switch",   "level": "ok",   "text": "INC-1234: Resolved — Database connections restored",
           "runbook": {"incident_id": "INC1234", "status": "resolved"}}),
    (1.0, {"kind": "appwrite", "level": "ok",   "text": "appwrite: upserted audit_events/rowINC1234 status=resolved",
           "runbook": {"incident_id": "INC1234", "status": "mitigated"}}),
    (2.0, {"kind": "chaos",    "level": "warn", "text": "CHAOS appwrite: simulated backend outage for INC-1234",
           "chaos": {"mode": "appwrite", "recover_in_ms": 3000, "message": "simulated failure"}}),
    (4.0, {"kind": "system",   "level": "ok",   "text": "Appwrite recovered — writes re-enabled, all systems green"}),
]


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def render_incident(incident_id: str) -> str:
    template = random.choice(INCIDENT_TEMPLATES)
    params = {
        "id": incident_id.split("-")[-1] if "-" in incident_id else incident_id,
        "conns": random.randint(500, 5000),
        "latency": round(random.uniform(5, 30), 1),
        "coll": f"collection_{random.randint(1, 100)}",
        "az": f"us-east-{random.choice(['1a', '1b', '1c'])}",
        "svcs": random.randint(5, 25),
        "domain": f"api-{random.randint(1, 20)}.axelr.com",
        "fails": random.randint(100, 1000),
        "lb": f"lb-{random.randint(1, 10)}",
        "pct": round(random.uniform(10, 99.9), 1),
        "pods": random.randint(3, 50),
        "deploy": f"deploy-{random.randint(1, 100)}",
        "node": f"ip-10-0-0-{random.randint(1, 255)}",
        "nodes": random.randint(10, 100),
        "app": f"service-{random.randint(1, 50)}",
        "service": random.choice(SERVICES),
        "heap": random.randint(8, 64),
        "reqs": random.randint(1000, 50000),
        "processor": random.choice(["stripe", "adyen", "paypal"]),
        "keys": random.randint(10000, 1000000),
        "asn": f"AS{random.randint(1000, 9999)}",
        "attempts": random.randint(10, 1000),
        "window": random.randint(60, 3600),
        "blocked": random.randint(1000, 100000),
        "wait": random.randint(100, 1000),
        "pool_connections": random.randint(800, 1024),
        "pod": f"{random.choice(SERVICES)}-{random.randint(1000, 9999)}",
        "count": random.randint(5, 50),
        "rps": random.randint(10000, 50000),
        "days": random.randint(1, 14),
        "zone1": f"us-east-{random.randint(1, 2)}",
        "zone2": f"us-west-{random.randint(1, 2)}",
        "size": round(random.uniform(50, 500), 1),
        "shards": random.randint(10, 50),
        "lag": random.randint(100000, 10000000),
        "shard": f"shard-{random.randint(0, 15)}",
        "key": f"token_{random.randint(100, 999)}",
        "endpoint": f"/api/v1/{random.choice(['users', 'orders', 'products'])}/list",
    }
    try:
        return template.format(**params)
    except (KeyError, IndexError):
        return template


def banner(text: str) -> None:
    line = "=" * 80
    print(f"\n{line}\n{text}\n{line}", flush=True)


# ═══════════════════════════════════════════════════════════════════════════
# CONFIG + PROFILES
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Config:
    mode: str = "demo"
    host: str = "localhost"
    port: int = 8765
    incidents: int = 0
    dashboards: int = 1
    chaos: float = 0.0
    min_interval: float = 0.5
    max_interval: float = 1.5
    stage_min: float = 0.5
    stage_max: float = 2.0
    reconnect_storm: bool = False
    clear_screen: bool = False
    verbose: bool = False
    timeout: float = 0.0
    log_file: str = "master_test.log"
    report_file: str = "master_test_report.json"

    @property
    def uri(self) -> str:
        return f"ws://{self.host}:{self.port}"


PROFILES = {
    "smoke":   dict(incidents=0,    dashboards=2,   chaos=0.0,
                    min_interval=0, max_interval=0),
    "demo":    dict(incidents=0,    dashboards=1,   chaos=1.0,
                    min_interval=0, max_interval=0),
    "stress":  dict(incidents=2500, dashboards=60,  chaos=0.20,
                    min_interval=0.05, max_interval=0.30,
                    reconnect_storm=True, stage_min=0.5, stage_max=2.0),
    "extreme": dict(incidents=5000, dashboards=100, chaos=0.35,
                    min_interval=0.02, max_interval=0.15,
                    reconnect_storm=True, stage_min=0.2, stage_max=1.5,
                    clear_screen=True),
    "full":    dict(incidents=2500, dashboards=60,  chaos=0.20,
                    min_interval=0.05, max_interval=0.30,
                    reconnect_storm=True, stage_min=0.5, stage_max=2.0),
}


def apply_profile(cfg: Config) -> Config:
    for k, v in PROFILES[cfg.mode].items():
        setattr(cfg, k, v)
    return cfg


def apply_env(cfg: Config) -> Config:
    env = os.environ
    if env.get("OPS_WS_HOST"):                            cfg.host = env["OPS_WS_HOST"]
    if env.get("OPS_WS_PORT"):                            cfg.port = int(env["OPS_WS_PORT"])
    if env.get("STRESS_TEST_MAX_INCIDENTS"):              cfg.incidents = int(env["STRESS_TEST_MAX_INCIDENTS"])
    if env.get("STRESS_TEST_MAX_CONCURRENT_DASHBOARDS"):  cfg.dashboards = int(env["STRESS_TEST_MAX_CONCURRENT_DASHBOARDS"])
    if env.get("STRESS_TEST_CHAOS_FREQUENCY"):            cfg.chaos = float(env["STRESS_TEST_CHAOS_FREQUENCY"])
    if env.get("STRESS_TEST_INCIDENT_MIN_INTERVAL"):      cfg.min_interval = float(env["STRESS_TEST_INCIDENT_MIN_INTERVAL"])
    if env.get("STRESS_TEST_INCIDENT_MAX_INTERVAL"):      cfg.max_interval = float(env["STRESS_TEST_INCIDENT_MAX_INTERVAL"])
    if env.get("STRESS_TEST_ENABLE_RECONNECT_STORM"):
        cfg.reconnect_storm = env["STRESS_TEST_ENABLE_RECONNECT_STORM"].lower() == "true"
    return cfg


# ═══════════════════════════════════════════════════════════════════════════
# MASTER TESTER
# ═══════════════════════════════════════════════════════════════════════════

class MasterTester:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = logging.getLogger("master")
        self.start_time = 0.0
        self.running = True

        self.incidents_generated = 0
        self.incidents_resolved = 0
        self.events_sent = 0
        self.failed_events = 0
        self.chaos_triggered = 0
        self.reconnect_count = 0
        self.connection_failures = 0
        self.active_connections = 0
        self.peak_connections = 0

        self.sender: Optional[Any] = None
        self.websockets: List[Any] = []
        self.incident_tasks: Dict[str, asyncio.Task] = {}
        self.lock = asyncio.Lock()
        self.message_queue: deque = deque(maxlen=200_000)
        self.lifecycle_times: List[float] = []
        self.mps_history: deque = deque(maxlen=3600)
        self.conn_history: deque = deque(maxlen=3600)

        self.smoke_passed = False
        self.demo_verified = False

    async def _ensure_sender(self) -> bool:
        if self.sender is not None and not self.sender.closed:
            return True
        try:
            self.sender = await websockets.connect(self.cfg.uri)
            self.log.info(f"sender connected → {self.cfg.uri}")
            return True
        except Exception as e:
            self.log.debug(f"sender connect failed: {e}")
            self.sender = None
            return False

    async def send(self, payload: Dict[str, Any]) -> bool:
        if not await self._ensure_sender():
            self.failed_events += 1
            return False
        payload.setdefault("ts", time.time())
        payload.setdefault("iso", now_iso())
        try:
            await self.sender.send(json.dumps(payload))
            self.events_sent += 1
            return True
        except Exception as e:
            self.log.debug(f"send failed: {e}")
            self.failed_events += 1
            self.sender = None
            return False

    async def dashboard_worker(self, dashboard_id: int) -> None:
        lo, hi = ((0.05, 0.3) if self.cfg.mode in ("stress", "extreme") else (0.5, 1.5))
        while self.running:
            ws = None
            try:
                ws = await websockets.connect(self.cfg.uri)
                async with self.lock:
                    self.websockets.append(ws)
                    self.active_connections += 1
                    self.peak_connections = max(self.peak_connections, self.active_connections)
                self.log.debug(f"dashboard#{dashboard_id} connected (active={self.active_connections})")

                async for raw in ws:
                    try:
                        self.message_queue.append(json.loads(raw))
                    except json.JSONDecodeError:
                        pass

            except (ConnectionRefusedError, OSError):
                async with self.lock:
                    self.connection_failures += 1
                await asyncio.sleep(random.uniform(lo, hi))
            except (ConnectionClosedError, ConnectionClosed):
                if self.cfg.reconnect_storm and self.running:
                    async with self.lock:
                        self.reconnect_count += 1
                    await asyncio.sleep(random.uniform(lo, hi))
                else:
                    break
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log.debug(f"dashboard#{dashboard_id} error: {e}")
                await asyncio.sleep(1.0)
            finally:
                if ws is not None:
                    async with self.lock:
                        if ws in self.websockets:
                            self.websockets.remove(ws)
                        self.active_connections = max(0, self.active_connections - 1)

    async def run_lifecycle(self, incident_id: str) -> None:
        t0 = time.time()
        text = render_incident(incident_id)
        stages = [
            {"kind": "switch",   "level": "err",  "text": f"{incident_id}: {text}",
             "runbook": {"incident_id": incident_id, "status": "detected"}},
            {"kind": "switch",   "level": "info", "text": f"Switch agent {incident_id}: Scanning runbook history for similar incidents...",
             "runbook": {"incident_id": incident_id, "status": "analyzing"}},
            {"kind": "switch",   "level": "info", "text": f"Switch agent {incident_id}: Found {random.randint(3,15)} past incidents — correlating signals",
             "runbook": {"incident_id": incident_id, "status": "correlating"}},
            {"kind": "switch",   "level": "info", "text": f"Switch agent {incident_id}: Runbook {random.randint(100,999)} matched — recommending remediation",
             "runbook": {"incident_id": incident_id, "status": "runbook_matched"}},
            {"kind": "switch",   "level": "info", "text": f"Human engineer {incident_id}: Reviewing recommendation — validating runbook steps",
             "runbook": {"incident_id": incident_id, "status": "human_review"}},
            {"kind": "switch",   "level": "ok",   "text": f"Human engineer {incident_id}: Approved — executing automated remediation",
             "runbook": {"incident_id": incident_id, "status": "executing"}},
            {"kind": "switch",   "level": "info", "text": f"Switch agent {incident_id}: Remediation steps running — {random.randint(3,10)} tasks in progress",
             "runbook": {"incident_id": incident_id, "status": "remediation_running"}},
            {"kind": "switch",   "level": "ok",   "text": f"{incident_id}: RESOLVED — all metrics back to baseline",
             "runbook": {"incident_id": incident_id, "status": "resolved"}},
            {"kind": "appwrite", "level": "ok",   "text": f"appwrite: upserted audit_events/row{incident_id} status=resolved",
             "runbook": None},
        ]
        if random.random() < self.cfg.chaos:
            asyncio.create_task(self.trigger_chaos(
                random.choice(CHAOS_EVENTS),
                random.uniform(1, len(stages) * 2),
            ))
            async with self.lock:
                self.chaos_triggered += 1
        for st in stages:
            if not self.running:
                break
            await self.send(st)
            await asyncio.sleep(random.uniform(self.cfg.stage_min, self.cfg.stage_max))
        async with self.lock:
            self.incidents_resolved += 1
            self.lifecycle_times.append(time.time() - t0)

    async def trigger_chaos(self, chaos: Dict[str, Any], delay: float) -> None:
        await asyncio.sleep(delay)
        if not self.running:
            return
        msg = chaos["message"].format(
            latency=random.randint(500, 2000),
            wait=random.randint(100, 500),
            blocked=random.randint(1000, 100000),
        )
        await self.send({
            "kind": "chaos", "level": "warn",
            "text": f"CHAOS {chaos['mode']}: {msg}",
            "chaos": {"mode": chaos["mode"],
                      "recover_in_ms": random.randint(*chaos["duration"]),
                      "message": msg},
        })
        self.log.warning(f"⚡ chaos: {chaos['mode']} — {msg}")

    async def connection_storm(self) -> None:
        self.log.info(f"🌪️  spawning {self.cfg.dashboards} dashboards")
        tasks = []
        for i in range(self.cfg.dashboards):
            tasks.append(asyncio.create_task(self.dashboard_worker(i)))
            await asyncio.sleep(random.uniform(0.01, 0.05))
        await asyncio.gather(*tasks, return_exceptions=True)

    async def incident_firehose(self) -> None:
        self.log.info(f"🔥 firehose: {self.cfg.incidents} incidents")
        while self.incidents_generated < self.cfg.incidents and self.running:
            incident_id = f"INC{self.incidents_generated + 1:06d}"
            task = asyncio.create_task(self.run_lifecycle(incident_id))
            async with self.lock:
                self.incident_tasks[incident_id] = task
                self.incidents_generated += 1
            if len(self.incident_tasks) > 500:
                for k in [k for k, t in self.incident_tasks.items() if t.done()]:
                    del self.incident_tasks[k]
            await asyncio.sleep(random.uniform(self.cfg.min_interval, self.cfg.max_interval))
        await asyncio.gather(*self.incident_tasks.values(), return_exceptions=True)
        self.log.info("✅ all incidents generated — draining")

    async def _stats_collector(self) -> None:
        while self.running:
            await asyncio.sleep(1)
            elapsed = time.time() - self.start_time if self.start_time else 0
            self.mps_history.append(self.events_sent / elapsed if elapsed else 0)
            self.conn_history.append(self.active_connections)

    async def stats_reporter(self) -> None:
        while self.running or self.incidents_resolved < self.cfg.incidents:
            elapsed = time.time() - self.start_time if self.start_time else 0
            mps = self.events_sent / elapsed if elapsed else 0
            avg = (sum(self.lifecycle_times) / len(self.lifecycle_times)) if self.lifecycle_times else 0
            if self.cfg.clear_screen and sys.stdout.isatty():
                print("\033c", end="")
            print("=" * 100)
            print(f"🔥 AXELR OPS ROOM — MODE {self.cfg.mode.upper()} — LIVE")
            print("=" * 100)
            print(f"  Elapsed:               {elapsed:>10.2f}s")
            print(f"  Events sent:           {self.events_sent:>10,}")
            print(f"  Avg MPS:               {mps:>10.2f}")
            print(f"  Active dashboards:     {self.active_connections:>6}/{self.cfg.dashboards} (peak {self.peak_connections})")
            print(f"  Incidents:             {self.incidents_resolved:>6,}/{self.cfg.incidents:,} resolved")
            print(f"  Chaos events:          {self.chaos_triggered:>10}")
            print(f"  Reconnections:         {self.reconnect_count:>10}")
            print(f"  Conn. failures:        {self.connection_failures:>10}")
            print(f"  Send failures:         {self.failed_events:>10}")
            print(f"  Avg lifecycle:         {avg:>10.2f}s")
            print(f"  Inbox queue:           {len(self.message_queue):>10,}")
            print("=" * 100)
            await asyncio.sleep(1.0)

    async def run_smoke(self) -> None:
        banner("SMOKE — verifying relay forwards frames between clients")
        try:
            async with websockets.connect(self.cfg.uri) as a, websockets.connect(self.cfg.uri) as b:
                await asyncio.sleep(0.3)
                probe = {"kind": "switch", "level": "ok",
                         "text": f"smoke-ping-{random.randint(1000, 9999)}", "ts": time.time()}
                await a.send(json.dumps(probe))
                try:
                    raw = await asyncio.wait_for(b.recv(), timeout=3.0)
                    got = json.loads(raw)
                    if got.get("text") == probe["text"]:
                        self.log.info(f"✅ SMOKE PASS — B received {got.get('kind')}/{got.get('level')}/{got.get('text')}")
                        self.smoke_passed = True
                    else:
                        self.log.error(f"❌ SMOKE FAIL — unexpected payload: {got}")
                except asyncio.TimeoutError:
                    self.log.error("❌ SMOKE FAIL — B never received the frame (relay not forwarding)")
        except Exception as e:
            self.log.error(f"❌ SMOKE FAIL — cannot connect: {e}")

    async def run_demo(self) -> None:
        banner("DEMO — scripted incident → chaos → recovery")
        if not await self._ensure_sender():
            self.log.error("❌ cannot connect sender — is broadcast.py running?")
            return
        recv = asyncio.create_task(self.dashboard_worker(-1))
        await asyncio.sleep(1.0)

        for delay, payload in DEMO_SCRIPT:
            await asyncio.sleep(delay)
            await self.send(payload)
            self.log.info(f"→ {payload.get('kind'):8s} {payload.get('text', '')}")

        await asyncio.sleep(2.0)
        received = len(self.message_queue)
        self.demo_verified = received >= len(DEMO_SCRIPT)
        if self.demo_verified:
            self.log.info(f"✅ DEMO VERIFIED — {received} frames reached the dashboard")
        else:
            self.log.warning(f"⚠️  DEMO incomplete — {received}/{len(DEMO_SCRIPT)} frames reached the dashboard")

        self.running = False
        recv.cancel()
        try:
            await recv
        except asyncio.CancelledError:
            pass

    async def run_stress_like(self) -> None:
        banner(f"{self.cfg.mode.upper()} — {self.cfg.incidents} incidents × {self.cfg.dashboards} dashboards")
        if not await self._ensure_sender():
            self.log.error("❌ cannot connect sender — is broadcast.py running?")
            return
        if self.cfg.incidents > 100:
            est = self.cfg.incidents * 9 * (self.cfg.stage_min + self.cfg.stage_max) / 2
            self.log.warning(f"⏱️  estimated runtime ≈ {est/60:.1f} min "
                             f"(9 stages × ~{(self.cfg.stage_min + self.cfg.stage_max) / 2:.2f}s)")

        tasks = [
            asyncio.create_task(self.connection_storm()),
            asyncio.create_task(self.incident_firehose()),
            asyncio.create_task(self._stats_collector()),
            asyncio.create_task(self.stats_reporter()),
        ]
        try:
            while self.incidents_resolved < self.cfg.incidents and self.running:
                await asyncio.sleep(1)
        finally:
            self.running = False
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run_full(self) -> None:
        banner("FULL — smoke → demo → stress")
        await self.run_smoke()
        if not self.smoke_passed:
            self.log.error("❌ FULL aborted — smoke failed")
            return
        await self.run_demo()
        if not self.demo_verified:
            self.log.error("❌ FULL aborted — demo failed")
            return
        self.running = True
        self.incidents_generated = 0
        self.incidents_resolved = 0
        self.events_sent = 0
        await self.run_stress_like()

    async def shutdown(self) -> None:
        self.running = False
        for t in list(self.incident_tasks.values()):
            t.cancel()
        for ws in list(self.websockets):
            try: await ws.close()
            except Exception: pass
        if self.sender is not None:
            try: await self.sender.close()
            except Exception: pass

    def final_report(self) -> Dict[str, Any]:
        elapsed = time.time() - self.start_time if self.start_time else 0
        mps = self.events_sent / elapsed if elapsed else 0
        avg = (sum(self.lifecycle_times) / len(self.lifecycle_times)) if self.lifecycle_times else 0
        return {
            "mode": self.cfg.mode,
            "test_duration_seconds": round(elapsed, 2),
            "total_events_sent": self.events_sent,
            "average_mps": round(mps, 2),
            "peak_mps": round(max(self.mps_history) if self.mps_history else 0, 2),
            "peak_connections": self.peak_connections,
            "max_configured_dashboards": self.cfg.dashboards,
            "incidents_generated": self.incidents_generated,
            "incidents_resolved": self.incidents_resolved,
            "total_incidents_configured": self.cfg.incidents,
            "chaos_events_triggered": self.chaos_triggered,
            "total_reconnections": self.reconnect_count,
            "total_connection_failures": self.connection_failures,
            "total_send_failures": self.failed_events,
            "average_incident_lifecycle_seconds": round(avg, 2),
            "inbox_queue_size": len(self.message_queue),
            "smoke_passed": self.smoke_passed,
            "demo_verified": self.demo_verified,
            "config": {
                "host": self.cfg.host, "port": self.cfg.port,
                "chaos_frequency": self.cfg.chaos,
                "min_interval": self.cfg.min_interval,
                "max_interval": self.cfg.max_interval,
                "reconnect_storm": self.cfg.reconnect_storm,
            },
        }


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def parse_args() -> Config:
    p = argparse.ArgumentParser(
        description="Elite master test suite for Axelr Ops Room",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="See module docstring for examples.",
    )
    p.add_argument("--mode", choices=list(PROFILES.keys()), default="demo")
    p.add_argument("--host");   p.add_argument("--port", type=int)
    p.add_argument("--incidents", type=int);   p.add_argument("--dashboards", type=int)
    p.add_argument("--chaos", type=float)
    p.add_argument("--min-interval", type=float); p.add_argument("--max-interval", type=float)
    p.add_argument("--stage-min", type=float);    p.add_argument("--stage-max", type=float)
    p.add_argument("--reconnect-storm",    dest="reconnect_storm",  action="store_true",  default=None)
    p.add_argument("--no-reconnect-storm", dest="reconnect_storm",  action="store_false")
    p.add_argument("--clear",              dest="clear_screen",     action="store_true",  default=None)
    p.add_argument("--no-clear",           dest="clear_screen",     action="store_false")
    p.add_argument("--timeout", type=float, default=0.0,
                   help="hard timeout for the whole run, in seconds (0 = none)")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--log-file", default="master_test.log")
    p.add_argument("--report-file", default="master_test_report.json")
    ns = p.parse_args()

    cfg = apply_profile(Config(mode=ns.mode))
    cfg = apply_env(cfg)
    for f in ("host", "port", "incidents", "dashboards", "chaos",
              "min_interval", "max_interval", "stage_min", "stage_max",
              "reconnect_storm", "clear_screen"):
        v = getattr(ns, f, None)
        if v is not None:
            setattr(cfg, f, v)
    cfg.verbose = ns.verbose
    cfg.timeout = ns.timeout
    cfg.log_file = ns.log_file
    cfg.report_file = ns.report_file
    return cfg


async def _main_async(cfg: Config) -> None:
    log = _setup_logging(cfg.log_file, cfg.verbose)
    log.info(f"config: {cfg}")
    tester = MasterTester(cfg)
    tester.start_time = time.time()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT,  lambda: (log.warning("SIGINT — shutting down"),  stop.set()))
        loop.add_signal_handler(signal.SIGTERM, lambda: (log.warning("SIGTERM — shutting down"), stop.set()))
    except NotImplementedError:
        pass

    if   cfg.mode == "smoke":  runner = asyncio.create_task(tester.run_smoke())
    elif cfg.mode == "demo":   runner = asyncio.create_task(tester.run_demo())
    elif cfg.mode == "full":   runner = asyncio.create_task(tester.run_full())
    else:                      runner = asyncio.create_task(tester.run_stress_like())

    stopper = asyncio.create_task(stop.wait())
    to_task = asyncio.create_task(asyncio.sleep(cfg.timeout)) if cfg.timeout > 0 else None

    try:
        watch = [runner, stopper] + ([to_task] if to_task else [])
        done, _ = await asyncio.wait(watch, return_when=asyncio.FIRST_COMPLETED)
        if to_task and to_task in done and not runner.done():
            log.error(f"timeout after {cfg.timeout}s — forcing shutdown")
    except asyncio.CancelledError:
        pass

    for t in (runner, stopper, to_task):
        if t and not t.done(): t.cancel()
    await tester.shutdown()

    report = tester.final_report()
    banner("FINAL REPORT")
    for k, v in report.items():
        if k != "config":
            print(f"  {k:35s} {v}")
    print()
    for k, v in report["config"].items():
        print(f"    config.{k:28s} {v}")
    print("═" * 80)

    try:
        with open(cfg.report_file, "w") as f:
            json.dump(report, f, indent=2)
        log.info(f"📝 report → {cfg.report_file}")
    except Exception as e:
        log.error(f"report write failed: {e}")
    log.info(f"📝 logs   → {cfg.log_file}")


def main() -> None:
    cfg = parse_args()
    try:
        asyncio.run(_main_async(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()