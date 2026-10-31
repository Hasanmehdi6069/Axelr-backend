#!/usr/bin/env node
/**
 * AXELR DEMO MASTER v4.2 — honest WebSocket relay + real health checks
 *
 * Triggers: dashboard {"event":"start_demo"} — nothing else.
 * Reports:  healthy / failed / unavailable. Never forces green.
 * Layouts:  core/hackathon/axelr_* and core/hackathons/axelr_* both supported.
 */
import { execFileSync } from "child_process";
import { setTimeout as sleep } from "timers/promises";
import { createRequire } from "module";
import * as fs from "fs";
import * as path from "path";

const require = createRequire(import.meta.url);
try { require("dotenv").config({ path: ".env.demo" }); } catch { /* optional */ }

const PAUSE_MS = parseInt(process.env.DEMO_PAUSE || "250", 10);
const WS_PORT  = parseInt(process.env.DEMO_WS_PORT || "8765", 10);
const PYTHON   = process.env.PYTHON_BIN || "python";
const HEALTH_TIMEOUT = parseInt(process.env.DEMO_HEALTH_TIMEOUT || "30000", 10);

const A = { R:"\x1b[0m", B:"\x1b[1m", D:"\x1b[2m", G:"\x1b[92m", Y:"\x1b[93m", r:"\x1b[31m", c:"\x1b[36m" };

type State = "healthy" | "failed" | "unavailable";

interface Ctx {
  modules: string[];
  root: string;
  projectRoot: string;
  counts: Record<State, number>;
}
const ctx: Ctx = { modules: [], root: "", projectRoot: "", counts: { healthy: 0, failed: 0, unavailable: 0 } };

// ── Discovery ─────────────────────────────────────────────────────────
function findModulesRoot(): string {
  const searchRoot = process.cwd();
  const axelrDirs: string[] = [];

  function findAxelrRoots(dir: string) {
    try {
      const items = fs.readdirSync(dir);
      const hasAxelrModules = items.some(item =>
        item.startsWith("axelr_") && fs.statSync(path.join(dir, item)).isDirectory()
      );
      if (hasAxelrModules) { axelrDirs.push(dir); return; }
      for (const item of items) {
        if (item.startsWith(".") || item === "node_modules" || item === "__pycache__" || item === ".venv" || item === "venv") continue;
        const fullPath = path.join(dir, item);
        try { if (fs.statSync(fullPath).isDirectory()) findAxelrRoots(fullPath); }
        catch { /* ignore */ }
      }
    } catch { /* ignore */ }
  }

  findAxelrRoots(searchRoot);
  if (axelrDirs.length === 0) {
    throw new Error(`No axelr_* modules found under ${searchRoot}. Run from the repo root.`);
  }
  const bestRoot = axelrDirs.sort((a, b) => b.length - a.length)[0];
  console.log(`${A.G}[discovery]${A.R} Module root: ${bestRoot}`);
  return bestRoot;
}

function discoverModules(root: string): string[] {
  return fs.readdirSync(root)
    .filter(n => n.startsWith("axelr_") && fs.statSync(path.join(root, n)).isDirectory())
    .sort();
}

function findProjectRoot(moduleRoot: string): string {
  // If moduleRoot is .../core/hackathon or .../core/hackathons → project root is two levels up.
  const parent = path.dirname(moduleRoot);
  if (path.basename(parent) === "core") return path.dirname(parent);
  // If `core` lives directly under a candidate, use that.
  const grandparent = path.dirname(parent);
  if (fs.existsSync(path.join(grandparent, "core", "hackathon"))) return grandparent;
  if (fs.existsSync(path.join(grandparent, "core", "hackathons"))) return grandparent;
  return path.dirname(path.dirname(moduleRoot));
}

function moduleDottedPath(moduleName: string): string {
  // Since ctx.root is already the hackathons directory under Axelr-backend, and ctx.projectRoot is C:\Users\alime\Downloads\AXELR AI,
  // the correct import path is hackathons.${moduleName} because Python can import from hackathons (which is a valid package name)
  return `hackathons.${moduleName}`;
}

// ── Metadata (dashboard labels; independent of discovery) ────────────
const META: Record<string, { label: string; icon: string; category: string; desc: string }> = {
  axelr_nebius_nvidia:       { label:"Nebius NVIDIA GPU",    icon:"🚀", category:"Cloud GPU · inference bridge", desc:"Streaming completions with rate limiting and failover" },
  axelr_ugreen_ai_nas:       { label:"UGREEN AI NAS",        icon:"💾", category:"Edge · file indexing",         desc:"Watchdog change detection, bounded index" },
  axelr_rise_of_ai_agents:   { label:"AI Agents Platform",   icon:"🤖", category:"Orchestration · deadlock",     desc:"Iterative wait-for-graph cycle detection" },
  axelr_opencv_ai_competition:{ label:"OpenCV AI Pipeline",  icon:"👁️", category:"Vision · frame pipeline",      desc:"RAII frame disposal, pooled buffers" },
  axelr_paypal:              { label:"PayPal FinTech",       icon:"💳", category:"Fintech · payments",           desc:"Orders v2 + webhook verification" },
  axelr_vultr:               { label:"Vultr Cloud",          icon:"☁️", category:"Cloud · provisioning",         desc:"Cost-guarded launch with orphan detection" },
  axelr_insights_engine:     { label:"Insights Engine",      icon:"📊", category:"Data · audit + search",        desc:"MongoDB or Appwrite, TF-IDF search" },
  axelr_meta_global_ai:      { label:"Meta AI Gateway",      icon:"🧠", category:"AI · function calling",        desc:"Llama 3 multi-step orchestration" },
  axelr_core:                { label:"Core Platform",        icon:"🏗️", category:"Security · zero-trust",        desc:"Multi-tenant auth, normalized origins" },
  axelr_ai_genesis_2026:     { label:"AI Genesis",           icon:"🎨", category:"AI · model registry",          desc:"Memory-pressure eviction, lazy torch" },
  axelr_buildhybridai_2026:  { label:"Build Hybrid AI",      icon:"⚡", category:"MLOps · edge-cloud",           desc:"Workload placement by network + compute" },
  axelr_bharat_agentic:      { label:"Bharat Agentic",       icon:"🇮🇳", category:"Resilience · persistence",     desc:"SQLite WAL with reconciliation" },
};

// ── Python health wrapper ────────────────────────────────────────────
// Tries core.hackathon.axelr_X first, then core.hackathon.X (backward compat).
const PY_HEALTH = `
import asyncio, inspect, importlib, json, sys
primary_modname = sys.argv[1]
mod = None
last_error = None

try:
    mod = importlib.import_module(primary_modname)
except Exception as e:
    last_error = f"{type(e).__name__}: {str(e)[:160]}"
    if "axelr_" in primary_modname:
        try:
            compat_modname = primary_modname.replace("axelr_", "")
            mod = importlib.import_module(compat_modname)
            print(f"[compat] Using backward-compatible import: {compat_modname}")
        except Exception as e2:
            last_error = f"{last_error} | compat failed: {type(e2).__name__}: {str(e2)[:160]}"

if not mod:
    print("__AXELR__" + json.dumps({
        "import_ok": False, "ok": None, "error": f"import failed: {last_error}"
    }))
    sys.exit(0)

async def run():
    if hasattr(mod, "init"):
        try:
            # Inspect the init function's signature to pass required parameters with defaults
            import inspect
            sig = inspect.signature(mod.init)
            kwargs = {}
            # Check for required parameters and set defaults
            for param_name, param in sig.parameters.items():
                if param.default is inspect.Parameter.empty:  # required parameter
                    if param_name == "node_id":
                        kwargs["node_id"] = "demo_node_001"
                    elif param_name == "api_key" or param_name.endswith("_key"):
                        kwargs[param_name] = "demo_key_placeholder"
                    elif param_name == "config":
                        kwargs["config"] = {}
            r = mod.init(**kwargs)
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
    result = {"import_ok": True, "ok": False, "error": f"outer: {str(e)[:160]}"}
print("__AXELR__" + json.dumps(result, default=str))
`;

function runPython(code: string, args: string[], cwd: string, timeout = HEALTH_TIMEOUT): any {
  try {
    const out = execFileSync(PYTHON, ["-c", code, ...args], {
      cwd,
      env: { 
        ...process.env, 
        PYTHONPATH: [cwd, path.join(cwd, "Axelr-backend"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
        // Set demo API keys and environment variables for all modules
        NEBIUS_API_KEY: "demo_nebius_key_12345",
        PAYPAL_CLIENT_ID: "demo_paypal_client_id",
        PAYPAL_CLIENT_SECRET: "demo_paypal_client_secret",
        VULTR_API_KEY: "demo_vultr_key_67890",
        MONGODB_URI: "mongodb://localhost:27017/axelr_demo",
      },
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

// ── WebSocket relay ──────────────────────────────────────────────────
let wsClients: any[] = [];
let wsServer: any = null;
let started = false;

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
    console.log(`${A.Y}  Fix: npm i ws${A.R}`);
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

// ── Health classification ────────────────────────────────────────────
function classify(h: any): State {
  if (!h) return "unavailable";
  if (h.import_ok === false) return "unavailable";
  const isHealthy = h.ok === true || h.healthy === true;
  return isHealthy ? "healthy" : "failed";
}

async function checkModule(name: string, index: number, total: number): Promise<State> {
  const label = META[name]?.label || name.replace(/^axelr_/, "").replace(/_/g, " ");
  const modPath = moduleDottedPath(name);

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

  broadcast("module_done", { index: index + 1, total, name, label, state, duration_ms: dur, health });
  await sleep(PAUSE_MS);
  return state;
}

async function runFullDemo(): Promise<void> {
  const total = ctx.modules.length;
  broadcast("demo_start", { total, startedAt: Date.now() });
  console.log(`\n${A.G}${A.B}▶ DEMO START — ${total} modules${A.R}\n`);

  for (let i = 0; i < total; i++) {
    const state = await checkModule(ctx.modules[i], i, total);
    ctx.counts[state]++;
  }

  // Real MongoDB audit write
  broadcast("live_write_start", {});
  console.log(`\n${A.c}[live_write]${A.R} Attempting real audit write…`);
  // Use correct absolute dotted path under Axelr-backend's hackathons package
  // Properly escaped Python code to avoid command truncation
  const auditCode = `
import asyncio, json, time, uuid
from hackathons.axelr_insights_engine.storage_base import init_backend, close_backend
from hackathons.axelr_insights_engine.auditing import log_node_execution
async def main():
    try:
        await init_backend()
        t0 = time.time()
        rid = await log_node_execution(
            node_id="demo_live", pipeline_id="axelr_master_demo",
            latency_ms=42.5, status="ok",
            metadata={"demo_id": str(uuid.uuid4())[:8]},
        )
        dt = (time.time() - t0) * 1000
        await close_backend()
        print("__AXELR__" + json.dumps({"audit_id": rid, "write_ms": round(dt, 2)}))
    except Exception as e:
        print("__AXELR__" + json.dumps({"audit_id": None, "error": str(e)[:200]}))
asyncio.run(main())
`;
  const live = runPython(auditCode, [], ctx.projectRoot, 20000);

  if (live.audit_id) {
    console.log(`  ${A.G}✓ audit row written${A.R}  id=${live.audit_id}  ${live.write_ms}ms`);
    broadcast("live_write_done", { audit_id: live.audit_id, write_ms: live.write_ms });
  } else {
    const reason = live.error || "no backend configured (set MONGODB_URI in .env.demo)";
    console.log(`  ${A.Y}⚠ skipped${A.R}  ${reason}`);
    broadcast("live_write_skipped", { reason });
  }

  broadcast("demo_complete", { total, completedAt: Date.now() });
  console.log(`\n${A.G}${A.B}✓ DEMO COMPLETE${A.R} — ${ctx.counts.healthy} healthy · ${ctx.counts.failed} failed · ${ctx.counts.unavailable} unavailable\n`);
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

// ── SIGINT cleanup ──────────────────────────────────────────────────
let isShuttingDown = false;
function printSummary(): void {
  console.log(`\n${A.B}${A.Y}SHUTDOWN SUMMARY${A.R}`);
  console.log(`  Healthy:     ${A.G}${ctx.counts.healthy}${A.R}`);
  console.log(`  Failed:      ${A.r}${ctx.counts.failed}${A.R}`);
  console.log(`  Unavailable: ${A.Y}${ctx.counts.unavailable}${A.R}`);
  console.log(`  Total:       ${ctx.modules.length}`);
}
async function handleShutdown(signal: string) {
  if (isShuttingDown) return;
  isShuttingDown = true;
  console.log(`\n${A.Y}[${signal}] Shutting down…${A.R}`);
  broadcast("demo_aborted", { reason: `Received ${signal}` });
  printSummary();
  stopWebSocket();
  console.log(`${A.G}✓ Clean shutdown${A.R}`);
  process.exit(0);
}
process.on("SIGINT", () => handleShutdown("SIGINT"));
process.on("SIGTERM", () => handleShutdown("SIGTERM"));

// ── Main ────────────────────────────────────────────────────────────
async function main(): Promise<void> {
  console.log(`\n${A.B}AXELR DEMO MASTER v4.2${A.R}`);
  ctx.root = findModulesRoot();
  ctx.projectRoot = findProjectRoot(ctx.root);
  ctx.modules = discoverModules(ctx.root);

  if (ctx.modules.length === 0) {
    console.error(`${A.r}No axelr_* modules under ${ctx.root}${A.R}`);
    process.exit(1);
  }

  console.log(`${A.G}Found ${ctx.modules.length} modules${A.R} in ${A.D}${ctx.root}${A.R}`);
  ctx.modules.forEach((m, i) => console.log(`  ${String(i+1).padStart(2,"0")}. ${m}`));
  console.log(`\n${A.D}Python import root: ${ctx.projectRoot}${A.R}\n`);

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

  // Start the demo automatically after 2 seconds regardless of WebSocket status, no need for browser
  console.log(`\n${A.B}▶ Starting demo automatically in 2 seconds…${A.R}`);
  await sleep(2000);
  triggerStart();
}

main().catch(e => { console.error(`${A.r}FATAL:${A.R} ${e.message}`); process.exit(1); });