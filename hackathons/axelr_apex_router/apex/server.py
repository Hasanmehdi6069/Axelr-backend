"""Apex Router — HTTP server + live dashboard (FastAPI + Uvicorn).

Endpoints:
  POST /route              run one routing decision
  GET  /health             liveness + tier availability
  GET  /telemetry          aggregate JSON for the dashboard
  POST /telemetry/reset    truncate the log
  GET  /                   live dashboard HTML

Start:  python -m apex.server
        # or: uvicorn apex.server:create_app --factory --port 8080
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import tiers as _tiers
from .router import ApexRouter
from .telemetry import aggregate
from .telemetry import reset as reset_telemetry
from .telemetry import stop as stop_telemetry


class _AppState:
    router: Optional[ApexRouter] = None


state = _AppState()


# ─── Schemas ──────────────────────────────────────────────────────────────
class RouteRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    taskType: Optional[str] = None
    maxTokens: int = Field(default=512, ge=1, le=8192)


class RouteResponse(BaseModel):
    text: str
    taskType: str
    tier: str
    confidence: float
    latencyMs: int
    costUsd: float
    policy: str
    escalated: bool
    cascade: list[dict]
    simulated: bool
    exhausted: bool = False


# ─── App factory ──────────────────────────────────────────────────────────
def create_app(policy: Optional[str] = None, config_path: Optional[str] = None) -> FastAPI:
    app = FastAPI(title="Apex Router", version="1.0.0")

    @app.on_event("startup")
    async def _startup() -> None:
        state.router = await ApexRouter.create(config_path=config_path, policy=policy)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        if state.router:
            await state.router.close()
        await stop_telemetry()

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "policy": state.router.policy if state.router else None,
                "tiers": {
                    name: tier.is_available() for name, tier in (state.router._tiers.items() if state.router else [])
                },
                "nebius_module_loaded": _tiers._NEBIUS_AVAILABLE,
            }
        )

    @app.post("/route", response_model=RouteResponse)
    async def route(req: RouteRequest) -> RouteResponse:
        if state.router is None:
            raise HTTPException(503, "router not initialized")
        result = await state.router.route(req.prompt, taskType=req.taskType, maxTokens=req.maxTokens)
        return RouteResponse(**result.to_dict())

    @app.get("/telemetry")
    async def telemetry(recent: int = 25) -> JSONResponse:
        agg = await aggregate(recent_n=max(1, min(recent, 200)))
        return JSONResponse(agg.to_dict())

    @app.post("/telemetry/reset")
    async def telemetry_reset() -> JSONResponse:
        reset_telemetry()
        return JSONResponse({"reset": True})

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> HTMLResponse:
        return HTMLResponse(DASHBOARD_HTML)

    return app


# ─── Dashboard HTML ───────────────────────────────────────────────────────
DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><title>Apex Router — Live Telemetry</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background:#0b0f17; color:#e6edf3; margin:0; padding:24px; }
  h1 { font-size:20px; margin:0 0 20px; font-weight:600; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
          gap:16px; margin-bottom:16px; }
  .card { background:#161b22; border:1px solid #30363d; border-radius:10px;
          padding:16px; }
  .card + .card { margin-top:16px; }
  .label { font-size:11px; color:#8b949e; text-transform:uppercase;
           letter-spacing:.08em; }
  .value { font-size:28px; font-weight:600; margin-top:6px; }
  .tier-row { display:flex; align-items:center; gap:12px; margin:10px 0; }
  .tier-name { width:90px; font-size:13px; text-transform:capitalize; }
  .bar { flex:1; height:14px; background:#21262d; border-radius:6px;
         overflow:hidden; }
  .bar > span { display:block; height:100%; transition:width .4s ease; }
  .bar.firebase > span { background:#3fb950; }
  .bar.local    > span { background:#58a6ff; }
  .bar.nebius   > span { background:#f78166; }
  .pct { width:70px; text-align:right; font-size:13px; color:#8b949e; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th, td { padding:7px 8px; text-align:left; border-bottom:1px solid #21262d; }
  th { color:#8b949e; font-weight:500; }
  .pill { padding:2px 8px; border-radius:10px; font-size:11px;
          text-transform:capitalize; }
  .pill.firebase { background:#1f6f3f; color:#aff5c3; }
  .pill.local    { background:#1f4a7d; color:#b8dcff; }
  .pill.nebius   { background:#7d3a2b; color:#ffd0c4; }
  .cascade { color:#8b949e; font-size:11px; }
  .controls { margin-bottom:16px; display:flex; gap:12px; align-items:center; }
  button { background:#21262d; color:#e6edf3; border:1px solid #30363d;
           border-radius:6px; padding:8px 14px; font-size:13px; cursor:pointer; }
  button:hover { background:#30363d; }
  input[type=text] { flex:1; background:#0d1117; border:1px solid #30363d;
                     border-radius:6px; padding:10px; color:#e6edf3;
                     font-size:13px; }
</style></head><body>
<h1>Apex Router — Live Telemetry</h1>

<div class="controls">
  <input type="text" id="promptInput"
         placeholder="Send a test prompt (e.g. 'Receipt: 2x Coffee $8. Total $8.')"
         onkeypress="if(event.key==='Enter') sendPrompt()">
  <button onclick="sendPrompt()">Send</button>
  <button onclick="resetTelemetry()">Reset</button>
</div>

<div class="grid">
  <div class="card"><div class="label">Total Requests</div><div class="value" id="total">—</div></div>
  <div class="card"><div class="label">Total Cost</div><div class="value" id="cost">—</div></div>
  <div class="card"><div class="label">p95 Latency</div><div class="value" id="p95">—</div></div>
  <div class="card"><div class="label">Escalation Rate</div><div class="value" id="esc">—</div></div>
</div>

<div class="card">
  <div class="label">Tier Breakdown</div>
  <div id="tiers"><div class="cascade">Waiting for first request…</div></div>
</div>

<div class="card">
  <div class="label">Recent Requests</div>
  <table>
    <thead><tr><th>Time</th><th>Task</th><th>Tier</th><th>Conf.</th>
               <th>Latency</th><th>Cost</th><th>Cascade</th></tr></thead>
    <tbody id="recent"></tbody>
  </table>
</div>

<script>
const fmtPct = (n) => (n * 100).toFixed(1) + '%';
const fmtMs  = (n) => n + 'ms';
const fmtUsd = (n) => '$' + Number(n).toFixed(6);

async function sendPrompt() {
  const input = document.getElementById('promptInput');
  const prompt = input.value.trim();
  if (!prompt) return;
  input.disabled = true;
  try {
    await fetch('/route', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt }),
    });
    input.value = '';
  } finally {
    input.disabled = false;
    input.focus();
    tick();
  }
}

async function resetTelemetry() {
  await fetch('/telemetry/reset', { method: 'POST' });
  tick();
}

async function tick() {
  let d;
  try { d = await (await fetch('/telemetry')).json(); }
  catch { return; }

  document.getElementById('total').textContent = d.total;
  document.getElementById('cost').textContent  = '$' + Number(d.totalCostUsd).toFixed(4);
  document.getElementById('p95').textContent   = fmtMs(d.p95LatencyMs);
  document.getElementById('esc').textContent   = fmtPct(d.escalationRate);

  const tiers = document.getElementById('tiers');
  const tierEntries = Object.entries(d.byTier);
  tiers.innerHTML = tierEntries.length
    ? tierEntries.map(([name, n]) => {
        const pct = d.total ? (n / d.total * 100) : 0;
        return `<div class="tier-row">
          <span class="tier-name">${name}</span>
          <div class="bar ${name}"><span style="width:${pct}%"></span></div>
          <span class="pct">${pct.toFixed(1)}% (${n})</span>
        </div>`;
      }).join('')
    : '<div class="cascade">No requests logged yet.</div>';

  document.getElementById('recent').innerHTML = (d.recent || []).map((e) => {
    const cascade = (e.cascade || []).map((c) =>
      `${c.tier}:${c.confidence}`).join(' → ');
    return `<tr>
      <td>${new Date(e.timestamp).toLocaleTimeString()}</td>
      <td>${e.taskType}</td>
      <td><span class="pill ${e.tier}">${e.tier}</span></td>
      <td>${e.confidence}</td>
      <td>${fmtMs(e.latencyMs)}</td>
      <td>${fmtUsd(e.costUsd)}</td>
      <td class="cascade">${cascade}</td>
    </tr>`;
  }).join('');
}
tick();
setInterval(tick, 1500);
</script>
</body></html>"""


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(create_app(), host="0.0.0.0", port=port)
