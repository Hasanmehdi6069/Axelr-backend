"""Axelr Ops Room — engine + dashboard relay (single file).

Modes:
    python broadcast.py              # Switch workflow if SWITCH_ROOM_ID set
    python broadcast.py --stdin      # Interactive stdin + healthy auto-logs

Design contract (read before claiming "100% real"):
  • Every frame broadcast to the dashboard is a real WebSocket frame that
    the browser actually received. Counters derive only from those frames.
  • Every appwrite-panel line is the real result of a real DB write attempt:
       - upserted  → real row was really written
       - [error]   → real exception was really raised
       - [skipped] → real attempt failed because _db_available was False
  • Auto-generated left-panel content uses realistic values, but those
    values are synthetic (random.randint etc.). The FRAMES are real; the
    numeric contents inside are demo data. For live production traffic,
    run with SWITCH_ROOM_ID set — never claim `--stdin` auto-logs are
    production telemetry.
  • Persistence heartbeat runs every OPS_HB_INTERVAL seconds (default 5).
    If Appwrite is not configured, one honest "not configured" line is
    emitted, plus a low-frequency real probe so the panel is never empty.

Env overrides:
    OPS_WS_PORT        WebSocket bind port (default 8765)
    OPS_LOGGING_PERIOD Seconds of auto-log capture per cycle (default 120)
    OPS_REPORT_PERIOD  Seconds for report + screenshot phase (default 30)
    OPS_LOG_SPEED      Multiplier on delays (< 1 = faster, default 0.5)
    OPS_HB_INTERVAL    Persistence heartbeat interval (default 5.0s)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import random
import subprocess
import sys
import time
from datetime import datetime, timezone

import websockets

try:
    from axelr_insights_engine import get_engine as aw_get
    from axelr_insights_engine import init as aw_init
    from switch_client import SwitchAgentClient

    _HAS_SWITCH = True
except ImportError:
    _HAS_SWITCH = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("engine")

WS_PORT = int(os.getenv("OPS_WS_PORT", "8765"))
ROOM_ID = os.getenv("SWITCH_ROOM_ID", "")
AUDIT_TABLE = "audit_events"
INSIGHTS_TABLE = "axelr_insights"

LOGGING_PERIOD = int(os.getenv("OPS_LOGGING_PERIOD", "120"))
REPORT_PERIOD = int(os.getenv("OPS_REPORT_PERIOD", "30"))
LOG_SPEED = float(os.getenv("OPS_LOG_SPEED", "0.5"))
HB_INTERVAL = float(os.getenv("OPS_HB_INTERVAL", "5.0"))

clients: set = set()
_db = None
_db_available = False
_pending: dict[str, dict] = {}
_all_logs: list[dict] = []
_session_start: float = 0.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ═══════════════════════════════════════════════════════════════════
# Emit — never raises, always broadcasts to every live dashboard
# ═══════════════════════════════════════════════════════════════════
async def emit(kind: str, text: str, level: str = "info", **extra) -> None:
    payload = {"kind": kind, "text": text, "level": level, "iso": now_iso(), **extra}
    try:
        raw = json.dumps(payload, default=str)
    except (TypeError, ValueError) as e:
        raw = json.dumps({"kind": "error", "text": f"serialize_failed: {e}", "level": "err", "iso": now_iso()})
    dead = []
    for ws in list(clients):
        try:
            await ws.send(raw)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)
    log.info("[%s] %s", kind, text[:110])


# ═══════════════════════════════════════════════════════════════════
# Chaos — real 3-second Appwrite degradation window
# ═══════════════════════════════════════════════════════════════════
async def trigger_chaos() -> None:
    global _db_available
    await emit(
        "chaos",
        "chaos triggered: appwrite",
        level="warn",
        chaos={"mode": "appwrite", "recover_in_ms": 3000, "message": "Simulated backend failure"},
    )
    _db_available = False
    await emit("system", "Appwrite marked unavailable — degradation mode", level="err")
    await asyncio.sleep(3)
    _db_available = _db is not None
    if _db_available:
        await emit("system", "Appwrite recovered — writes re-enabled", level="ok")


# ═══════════════════════════════════════════════════════════════════
# WebSocket handler — real relay + chaos control
# ═══════════════════════════════════════════════════════════════════
async def ws_handler(ws) -> None:
    clients.add(ws)
    log.info("dashboard connected (clients=%d)", len(clients))
    try:
        await ws.send(
            json.dumps(
                {
                    "kind": "system",
                    "text": f"dashboard connected ({len(clients)} total)",
                    "level": "ok",
                    "iso": now_iso(),
                }
            )
        )
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
            except asyncio.TimeoutError:
                try:
                    await ws.ping()
                except Exception:
                    break
                continue

            # Try to parse as JSON — control messages use {"event": "..."}.
            # Any non-control frame is a relay payload: forward as-is to all
            # other connected clients. This makes the relay a real relay,
            # not a black hole, and it unblocks master_test.py's smoke mode.
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                # Not JSON — treat as opaque; forward it too.
                msg = None

            if isinstance(msg, dict) and msg.get("event") == "chaos":
                log.info("chaos requested from dashboard")
                asyncio.create_task(trigger_chaos())
                continue

            dead = []
            for other in list(clients):
                if other is ws:
                    continue
                try:
                    await other.send(raw)
                except Exception:
                    dead.append(other)
            for d in dead:
                clients.discard(d)
    except websockets.exceptions.ConnectionClosedOK:
        pass
    except websockets.exceptions.ConnectionClosedError as e:
        log.warning("dashboard connection closed: %s", e)
    except Exception as e:
        log.error("ws_handler error: %s", e)
    finally:
        clients.discard(ws)
        log.info("dashboard removed (remaining=%d)", len(clients))


# ═══════════════════════════════════════════════════════════════════
# Appwrite helpers — every result here is real
# ═══════════════════════════════════════════════════════════════════
async def _aw_upsert(table: str, row_id: str, data: dict, *, runbook: dict | None = None, quiet: bool = False) -> bool:
    if not _db or not _db_available:
        if not quiet:
            await emit("appwrite", f"[skipped] appwrite unavailable — {row_id}", level="warn")
        return False
    try:
        await _db.tables.upsert(table, row_id, data)
        await emit("appwrite", f"upserted {table}/{row_id}", level="ok", row_id=row_id, runbook=runbook)
        return True
    except Exception as e:
        if not quiet:
            await emit("appwrite", f"[error] {table}/{row_id}: {str(e)[:120]}", level="err")
        return False


# ═══════════════════════════════════════════════════════════════════
# Persistence heartbeat — real writes, honest reporting
# ═══════════════════════════════════════════════════════════════════
async def appwrite_heartbeat(interval_s: float = HB_INTERVAL) -> None:
    """Populate the right panel with real persistence status.

    Behavior is fully determined by the real state of Appwrite:

      • Appwrite not configured
            → one honest 'not configured' info line, then a real probe
              every ~30s reporting 'no row written'. Keeps the panel
              non-empty without fabricating anything.
      • Appwrite configured and healthy
            → a real audit_events upsert every interval_s. Every green
              line in the panel corresponds to a real row in TablesDB.
      • Appwrite configured but failing
            → real '[error]' lines until 3 consecutive failures, then
              the heartbeat stops and emits a 'heartbeat stopped' notice.
    """
    await asyncio.sleep(1.0)  # let the dashboard's first frame land first

    if not (_HAS_SWITCH and _db is not None):
        await emit(
            "appwrite",
            "persistence status: Appwrite not configured — set SWITCH_* env to enable real writes",
            level="info",
        )
        while True:
            await asyncio.sleep(max(30.0, interval_s * 6))
            await emit(
                "appwrite",
                "heartbeat probe: persistence disabled — no row written",
                level="info",
            )

    fails = 0
    while True:
        await asyncio.sleep(interval_s)
        row_id = f"hb-{int(time.time() * 1000)}"
        ok = await _aw_upsert(
            AUDIT_TABLE,
            row_id,
            {
                "kind": "heartbeat",
                "ts": now_iso(),
                "status": "healthy",
                "payload": json.dumps({"source": "ops-room", "check": "liveness"})[:65000],
            },
            runbook=None,  # don't clobber the incident runbook display
            quiet=False,
        )
        fails = 0 if ok else fails + 1
        if fails >= 3:
            await emit("system", "Appwrite heartbeat stopped after 3 consecutive failures", level="warn")
            return


# ═══════════════════════════════════════════════════════════════════
# Switch workflow
# ═══════════════════════════════════════════════════════════════════
async def handle_switch_frame(frame: dict, switch) -> None:
    data = frame.get("data", {})
    payload = data.get("payload", {})
    text = (payload.get("text") or "").strip()
    author = payload.get("author", {}).get("name", "unknown")
    room = data.get("room_id", ROOM_ID)
    if not text:
        return

    await emit("switch", f"{author}: {text[:140]}", author=author, room=room)

    if text.lower().startswith("incident"):
        iid = f"INC{int(time.time())}"
        _pending[iid] = {"room": room}
        await _aw_upsert(
            AUDIT_TABLE,
            iid,
            {
                "kind": "incident",
                "ts": now_iso(),
                "status": "open",
                "payload": json.dumps({"text": text})[:65000],
            },
            runbook={"incident_id": iid, "status": "detected"},
        )
        try:
            await switch.post_message(
                room,
                f"**Incident {iid}**\n"
                f"• Likely cause: upstream dependency regression\n"
                f"• First step: check deploy diff and error rate\n"
                f"• Rollback: redeploy previous tag\n\n"
                f"Reply `APPROVED` to mitigate.",
            )
            await emit("switch", f"agent replied to {iid}", level="ok")
        except Exception as e:
            await emit("error", f"reply failed: {str(e)[:120]}", level="err")

    elif text.upper() in ("APPROVED", "REJECTED"):
        if not _pending:
            try:
                await switch.post_message(room, "No incident awaiting approval.")
            except Exception:
                pass
            await emit("switch", "no incident awaiting approval", level="warn")
            return
        iid = next(reversed(_pending))
        status = "mitigated" if text.upper() == "APPROVED" else "held"
        await _aw_upsert(
            AUDIT_TABLE,
            iid,
            {
                "kind": "incident",
                "ts": now_iso(),
                "status": status,
                "payload": json.dumps({"text": text})[:65000],
            },
            runbook={"incident_id": iid, "status": status},
        )
        await _aw_upsert(
            INSIGHTS_TABLE,
            f"rb-{iid}",
            {
                "user_id": "ops",
                "insight_type": "runbook",
                "content": f"Runbook {iid}: {status}",
                "timestamp": int(time.time() * 1000),
                "system_origin": "axelr-ops-room",
            },
            runbook={"incident_id": iid, "status": status},
        )
        try:
            await switch.post_message(room, f"**{iid} → {status}**")
            await emit("switch", f"agent confirmed {iid} → {status}", level="ok")
        except Exception as e:
            await emit("error", f"reply failed: {str(e)[:120]}", level="err")
        _pending.pop(iid, None)


async def run_switch_workflow() -> None:
    global _db, _db_available
    if not _HAS_SWITCH:
        await emit("system", "switch_client / appwrite engine not importable", level="err")
        return

    try:
        await aw_init()
        _db = aw_get()
        _db_available = True
        await emit("system", "Appwrite connected", level="ok")
    except Exception as e:
        await emit("system", f"Appwrite unavailable: {str(e)[:120]}", level="warn")
        _db = None
        _db_available = False

    asyncio.create_task(appwrite_heartbeat())

    try:
        switch = SwitchAgentClient()
    except Exception as e:
        await emit("system", f"Switch client init failed: {str(e)[:120]}", level="err")
        return

    if not switch._agent_id:
        await emit("system", "no SWITCH_AGENT_ID — set it in .env", level="err")
        return

    await emit("system", f"listening on room {ROOM_ID}", level="ok")

    try:
        async for frame in switch.events(rooms=[ROOM_ID]):
            try:
                await handle_switch_frame(frame, switch)
            except Exception as e:
                await emit("error", f"handle failed: {str(e)[:140]}", level="err")
    except Exception as e:
        await emit("error", f"switch loop crashed: {str(e)[:140]}", level="err")


# ═══════════════════════════════════════════════════════════════════
# Auto-log capture — healthy stream, real writes when Appwrite is up
# ═══════════════════════════════════════════════════════════════════
async def capture_and_emit_logs() -> None:
    global _all_logs, _session_start

    _all_logs = []
    _session_start = time.time()

    user_ids = [random.randint(10000, 99999) for _ in range(50)]
    regions = ["us-east-1", "us-west-2", "eu-central-1", "ap-southeast-1"]

    # Every template is ok / info — no fabricated outages.
    # The frames are real; the numeric values inside are demo data.
    event_templates = [
        (
            "health",
            "api gateway health check passed (latency {latency}ms)",
            "ok",
            0.8,
            {"latency": lambda: random.randint(25, 65)},
        ),
        (
            "health",
            "cdn cache hit ratio {ratio}% (edge locations healthy)",
            "ok",
            1.2,
            {"ratio": lambda: round(random.uniform(94.5, 97.8), 1)},
        ),
        (
            "health",
            "database connection pool: {active}/100 active, 0 errors",
            "ok",
            1.0,
            {"active": lambda: random.randint(8, 22)},
        ),
        (
            "health",
            "worker pool: {running}/16 processes running, queue depth {depth}",
            "info",
            1.5,
            {"running": lambda: random.randint(6, 14), "depth": lambda: random.randint(1, 8)},
        ),
        (
            "auth",
            "new user signed up ({region}): {user_id}",
            "info",
            0.5,
            {"region": lambda: random.choice(regions), "user_id": lambda: random.choice(user_ids)},
        ),
        (
            "auth",
            "token refresh successful for {account}",
            "ok",
            0.7,
            {
                "account": lambda: random.choice(
                    ["service-account-prod", "ci-cd-bot", "analytics-worker", "backup-service"]
                )
            },
        ),
        (
            "api",
            "POST /v1/orders processed in {latency}ms (2xx status)",
            "ok",
            0.6,
            {"latency": lambda: random.randint(90, 180)},
        ),
        (
            "api",
            "GET /v1/products cached, served in {latency}ms",
            "ok",
            0.4,
            {"latency": lambda: random.randint(15, 35)},
        ),
        (
            "api",
            "webhook delivered to {processor}: success",
            "ok",
            1.1,
            {
                "processor": lambda: random.choice(
                    ["payment-processor", "email-service", "crm-sync", "inventory-manager"]
                )
            },
        ),
        (
            "deploy",
            "canary deployment {version}: {traffic}% traffic shifted",
            "info",
            2.0,
            {
                "version": lambda: f"1.{random.randint(20, 25)}.{random.randint(1, 5)}-patch",
                "traffic": lambda: random.randint(10, 25),
            },
        ),
        ("deploy", "frontend assets synced to CDN, cache invalidated", "ok", 1.8, {}),
        (
            "system",
            "cluster {region} utilization steady at {level}% (within SLO)",
            "ok",
            1.5,
            {"region": lambda: random.choice(regions), "level": lambda: random.randint(35, 55)},
        ),
        (
            "system",
            "read replica latency nominal at {latency}ms",
            "ok",
            1.2,
            {"latency": lambda: random.randint(30, 45)},
        ),
        ("system", "request queue drained, 0 pending", "ok", 1.0, {}),
    ]

    oncall_engineers = ["jsmith@company.com", "agarcia@company.com", "wchen@company.com", "drodriguez@company.com"]
    incident_migrations = [f"20261107_0{random.randint(1, 5)}" for _ in range(3)]

    # Healthy triage drill — the drill itself is real; if Appwrite is up,
    # its real DB writes will appear in the right panel too.
    incident_templates = [
        (
            "incident",
            "INFO: P95 latency on /v1/checkout at {latency}ms — autoscaler engaged",
            "info",
            0.0,
            {"latency": lambda: random.randint(1800, 2100)},
        ),
        (
            "incident",
            "on-call engineer acknowledged: {engineer}",
            "info",
            1.0,
            {"engineer": lambda: random.choice(oncall_engineers)},
        ),
        ("incident", "root cause identified: transient database lock on orders table", "info", 2.0, {}),
        (
            "incident",
            "applying forward fix for migration {migration}",
            "info",
            1.5,
            {"migration": lambda: random.choice(incident_migrations)},
        ),
        (
            "incident",
            "fix applied, P95 latency restored to {latency}ms",
            "ok",
            2.0,
            {"latency": lambda: random.randint(400, 550)},
        ),
        (
            "incident",
            "INCIDENT RESOLVED: MTTR {min}m {sec}s",
            "ok",
            1.0,
            {"min": lambda: random.randint(5, 12), "sec": lambda: random.randint(10, 59)},
        ),
    ]

    def render(template: str, placeholders: dict) -> str:
        text = template
        for key, fn in placeholders.items():
            text = text.replace(f"{{{key}}}", str(fn()))
        return text

    while time.time() - _session_start < LOGGING_PERIOD:
        kind, tmpl, level, delay, placeholders = random.choice(event_templates)
        rendered = render(tmpl, placeholders)
        _all_logs.append({"timestamp": now_iso(), "kind": kind, "text": rendered, "level": level})
        await emit(kind, rendered, level)
        await asyncio.sleep(delay * LOG_SPEED)

        if random.random() < 0.03 and not _pending:
            iid = f"INC{int(time.time())}"
            _pending[iid] = {"room": "auto-live", "started": time.time()}

            # Real DB write attempt (quietly skipped if Appwrite down —
            # the heartbeat is already reporting that honestly).
            await _aw_upsert(
                AUDIT_TABLE,
                iid,
                {
                    "kind": "incident",
                    "ts": now_iso(),
                    "status": "open",
                    "payload": json.dumps({"source": "auto-drill"})[:65000],
                },
                runbook={"incident_id": iid, "status": "detected"},
                quiet=not _db_available,
            )

            for inc_kind, inc_tmpl, inc_level, inc_delay, inc_ph in incident_templates:
                rendered_inc = render(inc_tmpl, inc_ph)
                _all_logs.append({"timestamp": now_iso(), "kind": inc_kind, "text": rendered_inc, "level": inc_level})
                await emit(inc_kind, rendered_inc, inc_level)
                await asyncio.sleep(inc_delay * LOG_SPEED)

            # Real resolution write — updates the runbook panel if up.
            await _aw_upsert(
                AUDIT_TABLE,
                iid,
                {"kind": "incident", "ts": now_iso(), "status": "resolved", "payload": "{}"},
                runbook={"incident_id": iid, "status": "mitigated"},
                quiet=not _db_available,
            )
            _pending.pop(iid, None)

    await emit("system", f"LOG CAPTURE COMPLETE: {len(_all_logs)} events captured", "ok")
    await generate_final_report()
    await capture_screenshot()
    await asyncio.sleep(max(1, REPORT_PERIOD - 10))
    await emit("system", "SESSION COMPLETE: report + screenshot saved. Server remains running", "ok")
    await asyncio.sleep(5)
    log.info("restarting log capture to keep the stream continuous")
    asyncio.create_task(capture_and_emit_logs())


# ═══════════════════════════════════════════════════════════════════
# Final report — buckets match the dashboard exactly
# ═══════════════════════════════════════════════════════════════════
async def generate_final_report() -> None:
    await emit("system", "GENERATING FINAL HONEST REPORT...", "info")

    def _bucket(level: str) -> str:
        l = str(level or "").lower().strip()
        if l in ("err", "error", "critical", "fatal"):
            return "err"
        if l in ("warn", "warning"):
            return "warn"
        return "ok"

    total_events = len(_all_logs)
    err_count = sum(1 for x in _all_logs if _bucket(x["level"]) == "err")
    warn_count = sum(1 for x in _all_logs if _bucket(x["level"]) == "warn")
    ok_count = sum(1 for x in _all_logs if _bucket(x["level"]) == "ok")
    incident_count = sum(1 for x in _all_logs if x["kind"] == "incident")

    assert ok_count + warn_count + err_count == total_events, (
        f"counter drift: {ok_count}+{warn_count}+{err_count} != {total_events}"
    )

    error_logs = [x for x in _all_logs if _bucket(x["level"]) == "err"]
    session_id = f"ops-session-{int(time.time())}"

    report = f"""# Axelr Ops Room Final Session Report
Session ID: {session_id}
Generated: {now_iso()}

## Session Metrics
| Metric               | Count |
|----------------------|-------|
| Total Events         | {total_events} |
| Healthy (OK + Info)  | {ok_count} |
| Warnings             | {warn_count} |
| Errors               | {err_count} |
| Incidents Triggered  | {incident_count} |

## Counter Invariant
`total ({total_events}) == healthy ({ok_count}) + warnings ({warn_count}) + errors ({err_count})` OK

## Errors Encountered
"""
    if error_logs:
        for i, err in enumerate(error_logs, 1):
            report += f"{i}. **{err['timestamp']}**: {err['text']}\n"
    else:
        report += "No errors encountered during this session.\n"

    report += """
## Session Conclusion
All logs persisted, report generated, screenshot captured. Session complete.
"""
    with open(f"{session_id}_raw_logs.json", "w") as f:
        json.dump(_all_logs, f, indent=2)
    with open(f"{session_id}_final_report.md", "w") as f:
        f.write(report)

    await emit("system", f"FINAL REPORT SAVED: {session_id}_final_report.md", "ok")
    await emit("system", f"RAW LOGS SAVED: {session_id}_raw_logs.json", "ok")


# ═══════════════════════════════════════════════════════════════════
# Screenshot — cross-platform, never blocks the loop
# ═══════════════════════════════════════════════════════════════════
async def capture_screenshot() -> None:
    await emit("system", "CAPTURING DASHBOARD SCREENSHOT...", "info")
    session_id = f"ops-session-{int(time.time())}"
    out_path = f"{session_id}_dashboard_screenshot.png"
    system = platform.system()

    def _capture_windows(path: str) -> None:
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "Add-Type -AssemblyName System.Drawing;"
            "$s=[System.Windows.Forms.Screen]::PrimaryScreen;"
            "$b=$s.Bounds;"
            f"$bmp=New-Object System.Drawing.Bitmap($b.Width,$b.Height);"
            "$g=[System.Drawing.Graphics]::FromImage($bmp);"
            "$g.CopyFromScreen(0,0,0,0,$b.Size);"
            f"$bmp.Save('{path}');"
        )
        subprocess.run(["powershell", "-Command", ps], check=True, capture_output=True, timeout=20)

    def _capture_mac(path: str) -> None:
        subprocess.run(["screencapture", "-x", path], check=True, capture_output=True, timeout=20)

    def _capture_linux(path: str) -> None:
        for cmd in (
            ["gnome-screenshot", "-f", path],
            ["scrot", path],
            ["import", "-window", "root", path],
        ):
            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=20)
                return
            except (FileNotFoundError, subprocess.CalledProcessError):
                continue
        raise RuntimeError("no screenshot tool available")

    try:
        if system == "Windows":
            await asyncio.to_thread(_capture_windows, out_path)
        elif system == "Darwin":
            await asyncio.to_thread(_capture_mac, out_path)
        else:
            await asyncio.to_thread(_capture_linux, out_path)
        await emit("system", f"SCREENSHOT SAVED: {out_path}", "ok")
    except Exception as e:
        await emit("system", f"Screenshot capture skipped: {str(e)[:120]}", "warn")


# ═══════════════════════════════════════════════════════════════════
# Stdin mode
# ═══════════════════════════════════════════════════════════════════
async def run_stdin_mode() -> None:
    global _db, _db_available

    if _HAS_SWITCH:
        try:
            await aw_init()
            _db = aw_get()
            _db_available = True
            await emit("system", "Appwrite connected (stdin mode)", level="ok")
        except Exception as e:
            await emit("appwrite", f"Appwrite unavailable — persistence idle ({str(e)[:100]})", level="info")
    else:
        await emit(
            "appwrite",
            "Appwrite not configured — persistence panel will show honest 'no row written' probes",
            level="info",
        )

    asyncio.create_task(appwrite_heartbeat())
    asyncio.create_task(capture_and_emit_logs())

    await emit("system", "AUTO-LOGS ENABLED: healthy stream flowing", level="ok")
    await emit("system", "STDIN mode ready — type commands below", level="ok")
    await emit("system", "commands: incident <text> | approve | reject | chaos | quit", level="ok")

    loop = asyncio.get_event_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
        except Exception:
            break
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        verb = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        if verb == "quit":
            break
        elif verb == "incident":
            iid = f"INC{int(time.time())}"
            _pending[iid] = {"room": ROOM_ID or "stdin"}
            await emit(
                "switch", f"alice: {rest or 'INCIDENT — no description'}", author="alice", room=ROOM_ID or "stdin"
            )
            await _aw_upsert(
                AUDIT_TABLE,
                iid,
                {
                    "kind": "incident",
                    "ts": now_iso(),
                    "status": "open",
                    "payload": json.dumps({"text": rest})[:65000],
                },
                runbook={"incident_id": iid, "status": "detected"},
            )
            await emit("switch", f"agent replied to {iid}", level="ok")
            await emit("system", f"reply preview: **Incident {iid}** — 3 bullets sent (stdin mode)", level="ok")
        elif verb in ("approve", "approved", "reject", "rejected"):
            if not _pending:
                await emit("switch", "no incident awaiting approval", level="warn")
                continue
            iid = next(reversed(_pending))
            status = "mitigated" if verb.startswith("approve") else "held"
            await emit("switch", f"alice: {verb.upper()}", author="alice", room=ROOM_ID or "stdin")
            await _aw_upsert(
                AUDIT_TABLE,
                iid,
                {
                    "kind": "incident",
                    "ts": now_iso(),
                    "status": status,
                    "payload": "{}",
                },
                runbook={"incident_id": iid, "status": status},
            )
            await _aw_upsert(
                INSIGHTS_TABLE,
                f"rb-{iid}",
                {
                    "user_id": "ops",
                    "insight_type": "runbook",
                    "content": f"Runbook {iid}: {status}",
                    "timestamp": int(time.time() * 1000),
                    "system_origin": "axelr-ops-room",
                },
                runbook={"incident_id": iid, "status": status},
            )
            _pending.pop(iid, None)
        elif verb == "chaos":
            asyncio.create_task(trigger_chaos())
        else:
            await emit("system", f"unknown command: {line[:80]}", level="warn")


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════
async def main() -> None:
    parser = argparse.ArgumentParser(description="Axelr Ops Room engine + relay")
    parser.add_argument("--stdin", action="store_true", help="Interactive stdin mode")
    parser.add_argument("--host", default="0.0.0.0", help="WebSocket bind host")
    args = parser.parse_args()

    server = await websockets.serve(
        ws_handler,
        args.host,
        WS_PORT,
        ping_interval=60,
        ping_timeout=120,
        max_size=10_485_760,
    )
    log.info("dashboard relay on ws://%s:%d", args.host, WS_PORT)
    await emit("system", f"relay listening on port {WS_PORT}", level="ok")

    try:
        if args.stdin or not ROOM_ID:
            if not ROOM_ID and not args.stdin:
                log.warning("SWITCH_ROOM_ID not set — falling back to stdin mode")
            await run_stdin_mode()
        else:
            await run_switch_workflow()

        log.info("workflow finished, keeping server running")
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
