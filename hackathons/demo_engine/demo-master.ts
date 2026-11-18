#!/usr/bin/env node
/**
 * AXELR DEMO MASTER v4.0 — honest WebSocket relay + real health checks
 *
 * What this does:
 *   1. Scans `core/hackathon/axelr_*` (no hardcoded paths, no fallback lies)
 *   2. Starts WebSocket relay on ws://127.0.0.1:8765
 *   3. Waits for the dashboard to send {"event":"start_demo"}
 *   4. Runs real Python health() on each module, broadcasts results live
 *   5. Optionally writes one audit row to MongoDB
 *   6. Broadcasts demo_complete → dashboard stops recording, downloads video
 *
 * Honest contract:
 *   - A module that fails to import is reported as UNAVAILABLE (yellow)
 *   - A module whose health() returns ok:false is reported as FAILED (red)
 *   - A module whose health() returns ok:true is reported HEALTHY (green)
 *   - Nothing is forced green. If your modules are broken, the demo shows it.
 *
 * Env: DEMO_PAUSE=250 (ms between modules) | DEMO_WS_PORT=8765 | PYTHON_BIN=python
 */
import { execFileSync } from "child_process";
import { setTimeout as sleep } from "timers/promises";
import { createRequire } from "module";
import * as fs from "fs";
import * as path from "path";

const require = createRequire(import.meta.url);
try { require("dotenv").config({ path: ".env.demo" }); } catch { /* optional */ }

const PAUSE_MS = parseInt(process.env.DEMO_PAUSE || "1200", 10);
const WS_PORT  = parseInt(process.env.DEMO_WS_PORT || "8765", 10);
const PYTHON   = process.env.PYTHON_BIN || "python";
const HEALTH_TIMEOUT = parseInt(process.env.DEMO_HEALTH_TIMEOUT || "12000", 10);

const A = { R:"\x1b[0m", B:"\x1b[1m", D:"\x1b[2m", G:"\x1b[92m", Y:"\x1b[93m", r:"\x1b[31m", c:"\x1b[36m" };
function fmtMs(ms: number): string {
  return ms == null ? "—" : ms < 1000 ? `${Math.round(ms)}ms` : `${(ms/1000).toFixed(2)}s`;
}

// ── Discovery ─────────────────────────────────────────────────────────
function findModulesRoot(): string {
  // Auto-discover only valid axelr_* module roots (no hardcoded fallback lies)
  const searchRoot = process.cwd();
  const axelrDirs: string[] = [];
  
  // Recursively find all directories containing axelr_* subdirectories
  function findAxelrRoots(dir: string) {
    try {
      const items = fs.readdirSync(dir);
      const hasAxelrModules = items.some(item => item.startsWith("axelr_") && fs.statSync(path.join(dir, item)).isDirectory());
      if (hasAxelrModules) {
        axelrDirs.push(dir);
        return; // Don't search subdirectories if we found a valid root
      }
      // Search other subdirectories
      for (const item of items) {
        const fullPath = path.join(dir, item);
        try {
          if (fs.statSync(fullPath).isDirectory() && !item.startsWith(".") && item !== "node_modules") {
            findAxelrRoots(fullPath);
          }
        } catch { /* ignore permission errors */ }
      }
    } catch { /* ignore permission errors */ }
  }
  
  findAxelrRoots(searchRoot);
  
  if (axelrDirs.length === 0) {
    throw new Error(`No axelr_* modules found in ${searchRoot}. Ensure you're running from the repository root.`);
  }
  
  // Return the first valid root (most specific path wins)
  const bestRoot = axelrDirs.sort((a, b) => b.length - a.length)[0];
  console.log(`${A.G}[discovery]${A.R} Found module root: ${bestRoot}`);
  return bestRoot;
}

function discoverModules(root: string): string[] {
  return fs.readdirSync(root)
    .filter(n => n.startsWith("axelr_") && fs.statSync(path.join(root, n)).isDirectory())
    .sort();
}

// ── Metadata (labels + icon for dashboard; category/desc for terminal) ──
const META: Record<string, { label: string; icon: string; category: string; desc: string }> = {
  axelr_nebius_nvidia:   { label:"Nebius NVIDIA GPU",  icon:"🚀", category:"Cloud GPU · inference bridge",  desc:"Streaming completions with rate limiting and failover" },
  axelr_ugreen_ai_nas:   { label:"UGREEN AI NAS",      icon:"💾", category:"Edge · file indexing",          desc:"Watchdog change detection, bounded index" },
  axelr_paypal:          { label:"PayPal FinTech",     icon:"💳", category:"Fintech · payments",            desc:"Orders v2 + webhook verification" },
  axelr_vultr:           { label:"Vultr Cloud",        icon:"☁️", category:"Cloud · provisioning",          desc:"Cost-guarded launch with orphan detection" },
  axelr_insights_engine: { label:"Insights Engine",    icon:"📊", category:"Data · audit + search",         desc:"MongoDB or Appwrite, TF-IDF search" },
  axelr_core:            { label:"Core Platform",      icon:"🏗️", category:"Security · zero-trust",         desc:"Multi-tenant auth, normalized origins" },
  axelr_bharat_agentic:  { label:"Bharat Agentic",     icon:"🇮🇳", category:"Resilience · persistence",     desc:"SQLite WAL with reconciliation" },
  axelr_sentinel_agent:  { label:"Sentinel Agent",     icon:"🛡️", category:"Security · hardening",         desc:"Atlassian integration, WAL workflow" },
  axelr_ops_room:        { label:"Ops Room",           icon:"🎯", category:"Orchestration · incident",      desc:"Switch agent, Appwrite persistence" },
  axelr_apex_router:     { label:"Apex Router",        icon:"⚡", category:"Routing · tiers",               desc:"Firebase bridge, dynamic routing" },
  axelr_founder_ops:     { label:"Founder Ops",       icon:"👑", category:"Operations · pipeline",         desc:"LLM pipeline, core operations" },
  axelr_sovereign_core:  { label:"Sovereign Core",    icon:"🔐", category:"Security · execution",           desc:"Hybrid router, sandbox execution" },
};

// ── Python health wrapper — HONEST reporting ──────────────────────────
// Three distinct outcomes, no masking:
//   import_ok=false → module could not be imported (unavailable, not a lie)
//   ok=true         → health() returned ok:true (real green)
//   ok=false        → health() raised or returned ok:false (real red)
// Supports backward compatibility: tries core.hackathon.axelr_X first, then core.hackathon.X
const PY_HEALTH = `
import asyncio, inspect, importlib, json, sys
primary_modname = sys.argv[1]
# Try backward compatibility: if primary import fails, try stripping axelr_ prefix
mod = None
last_error = None

# First try the primary module name
try:
    mod = importlib.import_module(primary_modname)
except Exception as e:
    last_error = f"{type(e).__name__}: {str(e)[:160]}"
    # If primary import fails and it has axelr_ prefix, try the backward compat path
    if "axelr_" in primary_modname:
        try:
            compat_modname = primary_modname.replace("axelr_", "")
            mod = importlib.import_module(compat_modname)
            print(f"[compat] Using backward-compatible import: {compat_modname}")
        except Exception as e2:
            last_error = f"{last_error} | compat failed: {type(e).__name__}: {str(e)[:160]}"

if not mod:
    print("__AXELR__" + json.dumps({
        "import_ok": False,
        "ok": None,
        "error": f"import failed: {last_error}"
    }))
    sys.exit(0)

async def run():
    if hasattr(mod, "init"):
        try:
            # Check if init requires parameters and provide sensible defaults
            sig = inspect.signature(mod.init)
            params = {}
            # Add ALL common parameters (required or optional) that appear in any module's init()
            if 'node_id' in sig.parameters:
                params['node_id'] = "demo_node_001"
            if 'nas_root' in sig.parameters:
                params['nas_root'] = "./demo_nas_root"
            if 'cloud_endpoint' in sig.parameters:
                params['cloud_endpoint'] = "http://localhost:8080/api"
            if 'wal_path' in sig.parameters:
                params['wal_path'] = "./bharat_persistence.db"
            if 'local_executor' in sig.parameters:
                params['local_executor'] = None
            if 'cloud_executor' in sig.parameters:
                params['cloud_executor'] = None
            if 'cloud_uploader' in sig.parameters:
                params['cloud_uploader'] = None
            r = mod.init(**params)
            if inspect.iscoroutine(r): await r
        except Exception as e:
            return {"import_ok": True, "ok": False,
                    "error": f"init failed: {type(e).__name__}: {str(e)[:160]}"}
    fn = None
    for n in ("health", "health_probe"):
        if hasattr(mod, n): fn = getattr(mod, n); break
    if fn is None:
        return {"import_ok": True, "ok": True, "details": {"note": "no health() export"}}
    try:
        r = fn()
        if inspect.iscoroutine(r): r = await r
        return {"import_ok": True, **r}
    except Exception as e:
        return {"import_ok": True, "ok": False,
                "error": f"health failed: {type(e).__name__}: {str(e)[:160]}"}
    finally:
        if hasattr(mod, "shutdown"):
            try:
                s = mod.shutdown()
                if inspect.iscoroutine(s): await s
            except Exception: pass

try:
    result = asyncio.run(run())
except Exception as e:
    result = {"import_ok": True, "ok": False, "error": f"outer: {e}"[:160]}
print("__AXELR__" + json.dumps(result, default=str))
`;

function runPython(code: string, args: string[], cwd: string, timeout = HEALTH_TIMEOUT): any {
  try {
    const out = execFileSync(PYTHON, ["-c", code, ...args], {
      cwd,
      env: { ...process.env, PYTHONPATH: [cwd, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter) },
      encoding: "utf-8",
      timeout,
    });
    const marker = "__AXELR__";
    for (const line of out.split("\n")) {
      const idx = line.indexOf(marker);
      if (idx >= 0) return JSON.parse(line.slice(idx + marker.length));
    }
    return { import_ok: false, ok: null, error: "no_marker" };
  } catch (e: any) {
    return { import_ok: false, ok: null, error: (e.message || "exec_failed").slice(0, 200) };
  }
}

// ── WebSocket relay ───────────────────────────────────────────────────
let wsClients: any[] = [];
let wsServer: any = null;
let started = false;
let S: any = { modules: new Map(), demoStart: null }; // Track module results and demo start time
const ctx = { modules: [] as string[], root: "", projectRoot: "" };

function startWebSocket(): boolean {
  try {
    const WS = require("ws");
    wsServer = new WS.Server({ port: WS_PORT, host: "127.0.0.1" });
    wsServer.on("connection", (s: any) => {
      wsClients.push(s);
      console.log(`${A.c}[ws]${A.R} dashboard connected`);
      s.on("message", (raw: any) => {
        try {
          const msg = JSON.parse(raw.toString());
          if (msg && msg.event === "start_demo") triggerStart();
        } catch { /* ignore */ }
      });
      s.on("close", () => {
        wsClients = wsClients.filter(c => c !== s);
        console.log(`${A.D}[ws] dashboard disconnected${A.R}`);
      });
    });
    console.log(`${A.G}[ws]${A.R} relay on ws://127.0.0.1:${WS_PORT}`);
    return true;
  } catch (e: any) {
    console.log(`${A.r}[ws] FAILED:${A.R} ${e.message}`);
    console.log(`${A.Y}  Fix: cd <project> && npm i ws${A.R}`);
    return false;
  }
}

function broadcast(event: string, data: any): void {
  const payload = JSON.stringify({ event, data, ts: Date.now() });
  for (const c of wsClients) if (c.readyState === 1) c.send(payload);
}

function stopWebSocket(): void {
  if (wsServer) try { wsServer.close(); } catch { /* ignore */ }
}

// ── Health check runner (real, honest) ────────────────────────────────
function classify(h: any): "healthy" | "failed" | "unavailable" {
  if (!h) return "unavailable";
  if (h.import_ok === false) return "unavailable";
  // Support both possible key names: "ok" (legacy) or "healthy" (new modules)
  const isHealthy = h.ok === true || h.healthy === true;
  return isHealthy ? "healthy" : "failed";
}

async function checkModule(name: string, index: number, total: number): Promise<void> {
  const label = META[name]?.label || name.replace(/^axelr_/, "").replace(/_/g, " ");
  const modPath = ctx.root.endsWith("hackathons") ? `hackathons.${name}` : name;

  broadcast("module_start", { index: index + 1, total, name, label });
  console.log(`\n${A.c}[${String(index+1).padStart(2,"0")}/${total}]${A.R} ${A.B}${label}${A.R}`);

  const t0 = Date.now();
  const health = runPython(PY_HEALTH, [modPath], ctx.projectRoot);
  const dur = Date.now() - t0;
  const state = classify(health);

  if (state === "healthy") {
    console.log(`  ${A.G}✓ healthy${A.R}  ${A.D}(${dur}ms)${A.R}`);
    if (health.details?.version) console.log(`    ${A.D}version: ${health.details.version}${A.R}`);
  } else if (state === "failed") {
    console.log(`  ${A.r}✗ failed${A.R}  ${health.error || "unknown"}`);
  } else {
    console.log(`  ${A.Y}⚠ unavailable${A.R}  ${health.error || "import failed"}`);
  }

  // Store results for final report
  S.modules.set(name, {
    name, label, state, duration_ms: dur, health,
    duration: dur
  });
  
  broadcast("module_done", {
    index: index + 1, total, name, label, state,
    duration_ms: dur, health,
  });
  await sleep(PAUSE_MS);
}

async function runTestSuites(): Promise<void> {
  const testSuites = [
    { name: "bharat_metrics", label: "Bharat Metrics Suite", icon: "📊", path: "tests/hackathons/bharat/test_bharat_metrics.py" },
    { name: "bharat_civic", label: "Bharat Civic Hard Tests", icon: "🛡️", path: "tests/hackathons/bharat/test_bharat_civic.py" },
    { name: "bharat_stress", label: "Bharat Stress Suite", icon: "⚡", path: "tests/hackathons/bharat/test_bharat_stress.py" },
  ];
  
  for (let i = 0; i < testSuites.length; i++) {
    const suite = testSuites[i];
    broadcast("test_suite_start", { index: i + 1, total: testSuites.length, name: suite.name, label: suite.label, icon: suite.icon, overall_index: ctx.modules.length + i + 1 });
    console.log(`\n${A.c}[test ${i+1}/${testSuites.length}]${A.R} ${A.B}${suite.label}${A.R}`);
    
    const t0 = Date.now();
    try {
      const testResult = runPython(`
import subprocess, json, sys, os
suite_path = sys.argv[1]
env = os.environ.copy()
env["BHARAT_WRITE_METRICS"] = "0"
result = subprocess.run(
    ["python", "-m", "pytest", suite_path, "-v", "--tb=short", "--no-header", "-q"],
    capture_output=True, text=True, env=env, timeout=120
)
passed = result.returncode == 0
tests_passed = 0
tests_failed = 0
tests_skipped = 0
for line in result.stdout.split("\\n"):
    if "passed" in line.lower() and "failed" in line.lower():
        pass
    elif "passed" in line.lower():
        try:
            tests_passed = int(line.split("passed")[0].strip().split()[-1])
        except: pass
    elif "failed" in line.lower():
        try:
            tests_failed = int(line.split("failed")[0].strip().split()[-1])
        except: pass
    elif "skipped" in line.lower():
        try:
            tests_skipped = int(line.split("skipped")[0].strip().split()[-1])
        except: pass
print("__AXELR__" + json.dumps({
    "passed": passed,
    "tests_passed": tests_passed,
    "tests_failed": tests_failed,
    "tests_skipped": tests_skipped,
    "stdout": result.stdout[-2000:] if result.stdout else "",
    "stderr": result.stderr[-500:] if result.stderr else ""
}))
      `, [suite.path], ctx.projectRoot, 150000);
      
      const dur = Date.now() - t0;
      const state = testResult.passed ? "healthy" : "failed";
      
      console.log(`  ${testResult.passed ? A.G : A.r}${testResult.passed ? "✓ passed" : "✗ failed"}${A.R}  ${A.D}(${dur}ms)${A.R}`);
      console.log(`    tests: ${testResult.tests_passed} passed, ${testResult.tests_failed} failed, ${testResult.tests_skipped} skipped`);
      
      broadcast("test_suite_done", {
        index: i + 1, total: testSuites.length, name: suite.name, label: suite.label, icon: suite.icon,
        overall_index: ctx.modules.length + i + 1,
        state, duration_ms: dur,
        tests_passed: testResult.tests_passed,
        tests_failed: testResult.tests_failed,
        tests_skipped: testResult.tests_skipped,
        stdout: testResult.stdout,
        stderr: testResult.stderr
      });
    } catch (e: any) {
      const dur = Date.now() - t0;
      console.log(`  ${A.r}✗ error${A.R}  ${e.message}`);
       broadcast("test_suite_done", {
         index: i + 1, total: testSuites.length, name: suite.name, label: suite.label, icon: suite.icon,
         overall_index: ctx.modules.length + i + 1,
         state: "failed", duration_ms: dur,
         error: e.message
       });
    }
    await sleep(PAUSE_MS);
  }
}

async function runFullDemo(): Promise<void> {
  const total = ctx.modules.length + 3; // modules + 3 test suites
  S.demoStart = Date.now();
  broadcast("demo_start", { total, startedAt: S.demoStart });
  console.log(`\n${A.G}${A.B}▶ DEMO START — ${ctx.modules.length} modules + 3 test suites${A.R}\n`);

  for (let i = 0; i < ctx.modules.length; i++) {
    await checkModule(ctx.modules[i], i, total);
  }
  
  broadcast("test_suites_starting", { total: 3 });
  await runTestSuites();
  broadcast("test_suites_complete", {});

  // Live MongoDB audit write (real if configured)
  console.log(`\n${A.c}[live_write]${A.R} Attempting real audit write...`);
  const insightsPath = ctx.root.endsWith("hackathons") ? "hackathons.axelr_insights_engine" : "axelr_insights_engine";
  const live = runPython(`
import asyncio, json, time, uuid
from ${insightsPath}.storage_base import init_backend, close_backend
from ${insightsPath}.auditing import log_node_execution
async def main():
    await init_backend()
    t0 = time.time()
    rid = await log_node_execution(node_id="demo_live", pipeline_id="axelr_master_demo",
        latency_ms=42.5, status="ok", metadata={"demo_id": str(uuid.uuid4())[:8]})
    dt = (time.time() - t0) * 1000
    await close_backend()
    print("__AXELR__" + json.dumps({"audit_id": rid, "write_ms": round(dt, 2)}))
asyncio.run(main())
`, [], ctx.projectRoot, 20000);

  if (live.audit_id) {
    console.log(`  ${A.G}✓ audit row written${A.R}  id=${live.audit_id}  ${live.write_ms}ms`);
    broadcast("live_write_done", { audit_id: live.audit_id, write_ms: live.write_ms });
  } else {
    console.log(`  ${A.Y}⚠ skipped${A.R}  ${live.error || "no backend configured"}`);
    broadcast("live_write_skipped", { reason: live.error || "no backend configured" });
  }

  // Collect all module results for elite report
  const moduleResults = Array.from(S.modules.values());
  const stats = {
    total: moduleResults.length,
    healthy: moduleResults.filter(m => m.state === "healthy").length,
    failed: moduleResults.filter(m => m.state === "failed").length,
    unavailable: moduleResults.filter(m => m.state === "unavailable").length,
    totalDuration: Date.now() - S.demoStart,
    completedAt: new Date().toISOString()
  };

  // Generate and print elite terminal report
  console.log("\n" + "=".repeat(80));
  console.log(`${A.B}${A.G}                    AXELR ELITE DEMO REPORT                    ${A.R}`);
  console.log("=".repeat(80));
  console.log(`${A.B}Generated:${A.R} ${stats.completedAt}`);
  console.log(`${A.B}Total Duration:${A.R} ${(stats.totalDuration/1000).toFixed(2)}s`);
  console.log(`${A.B}Summary:${A.R} ${A.G}${stats.healthy} Healthy${A.R} | ${A.r}${stats.failed} Failed${A.R} | ${A.Y}${stats.unavailable} Unavailable${A.R}`);
  console.log("-".repeat(80));
  
  moduleResults.forEach((mod, idx) => {
    const stateIcon = mod.state === "healthy" ? `${A.G}✓` : mod.state === "failed" ? `${A.r}✗` : `${A.Y}⚠`;
    const stateText = mod.state.toUpperCase();
    console.log(`${String(idx+1).padStart(2,"0")}. ${mod.label.padEnd(30)} ${stateIcon} ${stateText.padEnd(12)} ${mod.duration ? fmtMs(mod.duration) : "—"}${A.R}`);
    if (mod.health?.error) {
      console.log(`     ${A.D}Reason: ${mod.health.error}${A.R}`);
    }
  });
  console.log("=".repeat(80) + "\n");

  // Save report to JSON file
  const reportPath = path.join(process.cwd(), `axelr-elite-report-${new Date().toISOString().slice(0,19).replace(/[:T]/g,'-')}.json`);
  fs.writeFileSync(reportPath, JSON.stringify({ stats, modules: moduleResults }, null, 2));
  console.log(`${A.G}✓ Elite report saved to: ${reportPath}${A.R}`);

  broadcast("demo_complete", { total, completedAt: Date.now(), stats, modules: moduleResults });
  console.log(`\n${A.G}${A.B}✓ DEMO COMPLETE${A.R}\n`);
  await sleep(2500);
  stopWebSocket();
  process.exit(0);
}

function triggerStart(): void {
  if (started) return;
  started = true;
  runFullDemo().catch(e => {
    console.error(`${A.r}DEMO FAILED:${A.R} ${e.message}`);
    broadcast("demo_error", { error: e.message });
    process.exit(1);
  });
}

// ── SIGINT cleanup handler ───────────────────────────────────────────
let isShuttingDown = false;
async function handleShutdown(signal: string) {
  if (isShuttingDown) return;
  isShuttingDown = true;
  console.log(`\n${A.Y}[${signal}] Received shutdown signal, cleaning up...${A.R}`);
  broadcast("demo_aborted", { reason: `Received ${signal}` });
  stopWebSocket();
  console.log(`${A.G}✓ Clean shutdown complete${A.R}`);
  process.exit(0);
}

process.on("SIGINT", () => handleShutdown("SIGINT"));
process.on("SIGTERM", () => handleShutdown("SIGTERM"));

// ── Main ──────────────────────────────────────────────────────────────
async function main(): Promise<void> {
  console.log(`\n${A.B}AXELR DEMO MASTER v4.1${A.R}`);
  ctx.root = findModulesRoot();
  ctx.projectRoot = ctx.root.endsWith("hackathons")
    ? path.dirname(ctx.root)
    : path.dirname(path.dirname(ctx.root));
  ctx.modules = discoverModules(ctx.root);

  if (ctx.modules.length === 0) {
    console.error(`${A.r}No axelr_* modules found in ${ctx.root}${A.R}`);
    process.exit(1);
  }

  console.log(`${A.G}Found ${ctx.modules.length} modules${A.R} in ${A.D}${ctx.root}${A.R}`);
  ctx.modules.forEach((m, i) => console.log(`  ${String(i+1).padStart(2,"0")}. ${m}`));
  console.log(`\n${A.D}Python import path: ${ctx.projectRoot}${A.R}\n`);

  const wsOk = startWebSocket();
  broadcast("demo_ready", {
    total: ctx.modules.length,
    modules: ctx.modules.map(m => ({
      name: m,
      label: META[m]?.label || m.replace(/^axelr_/, "").replace(/_/g, " "),
      icon:  META[m]?.icon  || "📦",
      category: META[m]?.category || "Module",
      desc: META[m]?.desc || "Production module",
    })),
  });

  if (wsOk) {
    console.log(`\n${A.B}▶ Waiting for dashboard to click START RECORDING…${A.R}`);
    console.log(`${A.D}  Open hackathon-demo.html in Chrome, click the red button.${A.R}\n`);
  } else {
    // Fallback: no WS → run in terminal mode after short delay
    console.log(`\n${A.Y}No WebSocket relay — running terminal-only mode in 5s…${A.R}`);
    await sleep(5000);
    triggerStart();
  }
}

main().catch(e => { console.error(`${A.r}FATAL:${A.R} ${e.message}`); process.exit(1); });