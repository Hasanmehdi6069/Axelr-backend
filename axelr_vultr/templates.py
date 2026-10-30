# axelr_vultr/templates.py
"""Blueprint system — pre-baked Axelr deployment templates.

A blueprint is a named bundle of: plan, region default, OS, startup script,
tags, firewall intent, and cost estimate. Users override any field with
`POST /vultr/instances` and a `blueprint` name plus overrides.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any

logger = logging.getLogger("axelr.vultr.templates")

# Ubuntu 22.04 x64 is the current Vultr OS id for most regions.
OS_UBUNTU_22_04 = 1743


# ── Startup scripts ─────────────────────────────────────────────────────────
# Scripts run once as root at boot. They're idempotent — safe to re-run.

_STARTUP_MINIMAL = """#!/bin/bash
# Axelr inference worker — minimal health endpoint (30s boot)
set -euo pipefail
exec > >(tee -a /var/log/axelr-boot.log) 2>&1
echo "=== Axelr worker boot $(date -u) ==="
apt-get update -qq
apt-get install -y -qq python3
mkdir -p /opt/axelr
cat > /opt/axelr/health.py << 'PYEOF'
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, os
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "status": "ok",
            "service": "axelr-inference-worker",
            "hostname": os.uname().nodename,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", 8080), H).serve_forever()
PYEOF
nohup python3 /opt/axelr/health.py > /var/log/axelr-health.log 2>&1 &
echo "=== Axelr health endpoint listening on :8080 ==="
"""

_STARTUP_FULL = """#!/bin/bash
# Axelr inference worker — full install (5 min boot)
set -euo pipefail
exec > >(tee -a /var/log/axelr-boot.log) 2>&1
echo "=== Axelr full worker boot $(date -u) ==="
apt-get update -qq
apt-get install -y -qq python3 curl ca-certificates
# Install ollama
curl -fsSL https://ollama.com/install.sh | sh
# Start ollama in background
nohup ollama serve > /var/log/ollama.log 2>&1 &
sleep 8
# Pull a small model for demo
ollama pull tinyllama || true
# Health sidecar for fast probes
mkdir -p /opt/axelr
cat > /opt/axelr/health.py << 'PYEOF'
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, os, urllib.request
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            req = urllib.request.Request("http://127.0.0.1:11434/api/tags")
            urllib.request.urlopen(req, timeout=2)
            ollama_ok = True
        except Exception:
            ollama_ok = False
        body = json.dumps({
            "status": "ok",
            "service": "axelr-inference-worker",
            "ollama_ready": ollama_ok,
            "hostname": os.uname().nodename,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", 8080), H).serve_forever()
PYEOF
nohup python3 /opt/axelr/health.py > /var/log/axelr-health.log 2>&1 &
echo "=== Axelr full worker ready ==="
"""

_STARTUP_SANDBOX = """#!/bin/bash
# Axelr sandbox — isolated code runner
set -euo pipefail
exec > >(tee -a /var/log/axelr-boot.log) 2>&1
apt-get update -qq
apt-get install -y -qq python3 python3-pip nodejs npm git
pip3 install -q httpx pydantic fastapi uvicorn
mkdir -p /opt/sandbox
cat > /opt/sandbox/runner.py << 'PYEOF'
from fastapi import FastAPI
import subprocess, sys, tempfile, os
app = FastAPI()
@app.get("/health")
async def health():
    return {"status": "ok", "service": "axelr-sandbox"}
@app.post("/run")
async def run(body: dict):
    code = body.get("code", "")
    lang = body.get("language", "python")
    with tempfile.NamedTemporaryFile(mode="w", suffix=f".{lang}", delete=False) as f:
        f.write(code)
        path = f.name
    try:
        cmd = ["python3", path] if lang == "python" else ["node", path]
        r = subprocess.run(cmd, capture_output=True, timeout=10, text=True)
        return {"stdout": r.stdout[:4096], "stderr": r.stderr[:4096], "rc": r.returncode}
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    finally:
        os.unlink(path)
PYEOF
cd /opt/sandbox
nohup uvicorn runner:app --host 0.0.0.0 --port 8080 > /var/log/axelr-sandbox.log 2>&1 &
echo "=== Axelr sandbox ready on :8080 ==="
"""

_STARTUP_BATCH = """#!/bin/bash
# Axelr batch processor — long-running job runner
set -euo pipefail
exec > >(tee -a /var/log/axelr-boot.log) 2>&1
apt-get update -qq
apt-get install -y -qq python3 python3-pip
pip3 install -q httpx redis pydantic
mkdir -p /opt/batch
cat > /opt/batch/worker.py << 'PYEOF'
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, time, os
START = time.time()
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "status": "ok", "service": "axelr-batch",
            "uptime_s": round(time.time() - START, 2),
            "hostname": os.uname().nodename,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", 8080), H).serve_forever()
PYEOF
nohup python3 /opt/batch/worker.py > /var/log/axelr-batch.log 2>&1 &
echo "=== Axelr batch ready on :8080 ==="
"""

_STARTUP_WEB = """#!/bin/bash
# Axelr web node — lightweight proxy / static host
set -euo pipefail
exec > >(tee -a /var/log/axelr-boot.log) 2>&1
apt-get update -qq
apt-get install -y -qq python3
mkdir -p /opt/web
cat > /opt/web/server.py << 'PYEOF'
from http.server import HTTPServer, SimpleHTTPRequestHandler
import os
os.chdir("/opt/web")
HTTPServer(("0.0.0.0", 8080), SimpleHTTPRequestHandler).serve_forever()
PYEOF
echo "<html><body><h1>Axelr web node ready</h1></body></html>" > /opt/web/index.html
nohup python3 /opt/web/server.py > /var/log/axelr-web.log 2>&1 &
echo "=== Axelr web ready on :8080 ==="
"""


# ── Blueprint model ─────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Blueprint:
    name: str
    description: str
    plan: str
    region_default: str
    os_id: int
    startup_script: str
    estimated_monthly_usd: float
    tags: tuple[str, ...] = ()
    hardware_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "plan": self.plan,
            "region_default": self.region_default,
            "os_id": self.os_id,
            "estimated_monthly_usd": self.estimated_monthly_usd,
            "tags": list(self.tags),
            "hardware_hint": self.hardware_hint,
        }


# ── Pre-baked blueprints ────────────────────────────────────────────────────

BLUEPRINTS: dict[str, Blueprint] = {
    "inference_worker": Blueprint(
        name="inference_worker",
        description="CPU inference node running a health endpoint + optional ollama. "
                    "Ready in ~30s (minimal) or ~5min (full).",
        plan="vc2-2c-4gb",  # 2 vCPU, 4GB — $18/mo
        region_default="ewr",
        os_id=OS_UBUNTU_22_04,
        startup_script=_STARTUP_MINIMAL,
        estimated_monthly_usd=18.0,
        tags=("axelr-managed", "axelr-role-inference"),
        hardware_hint="2 vCPU / 4GB / 80GB SSD",
    ),
    "sandbox": Blueprint(
        name="sandbox",
        description="Isolated code execution sandbox with FastAPI runner. "
                    "Python + Node. 10s per-job timeout enforced.",
        plan="vc2-1c-2gb",
        region_default="ewr",
        os_id=OS_UBUNTU_22_04,
        startup_script=_STARTUP_SANDBOX,
        estimated_monthly_usd=12.0,
        tags=("axelr-managed", "axelr-role-sandbox"),
        hardware_hint="1 vCPU / 2GB / 55GB SSD",
    ),
    "batch_processor": Blueprint(
        name="batch_processor",
        description="Long-running batch worker with uptime tracking. "
                    "Ideal for async job queues.",
        plan="vc2-1c-2gb",
        region_default="ewr",
        os_id=OS_UBUNTU_22_04,
        startup_script=_STARTUP_BATCH,
        estimated_monthly_usd=12.0,
        tags=("axelr-managed", "axelr-role-batch"),
        hardware_hint="1 vCPU / 2GB / 55GB SSD",
    ),
    "web_node": Blueprint(
        name="web_node",
        description="Lightweight static host / reverse proxy.",
        plan="vc2-1c-1gb",
        region_default="ewr",
        os_id=OS_UBUNTU_22_04,
        startup_script=_STARTUP_WEB,
        estimated_monthly_usd=6.0,
        tags=("axelr-managed", "axelr-role-web"),
        hardware_hint="1 vCPU / 1GB / 25GB SSD",
    ),
}


# ── Public API ──────────────────────────────────────────────────────────────

def list_blueprints() -> list[dict[str, Any]]:
    return [bp.to_dict() for bp in BLUEPRINTS.values()]


def get_blueprint(name: str) -> Blueprint | None:
    return BLUEPRINTS.get(name)


def resolve_blueprint(
    name: str,
    *,
    region: str | None = None,
    plan: str | None = None,
    os_id: int | None = None,
    startup_script: str | None = None,
    tags: list[str] | None = None,
    default_region: str = "ewr",
) -> Blueprint:
    """Resolve a blueprint, merging overrides. Falls back to `custom` if unknown."""
    base = BLUEPRINTS.get(name)
    if base is None:
        logger.info("blueprint_unknown name=%s using_custom", name)
        base = Blueprint(
            name="custom",
            description="User-defined configuration",
            plan="vc2-1c-1gb",
            region_default=default_region,
            os_id=OS_UBUNTU_22_04,
            startup_script="",
            estimated_monthly_usd=6.0,
            tags=("axelr-managed",),
            hardware_hint="1 vCPU / 1GB",
        )
    merged_tags = list(base.tags)
    if tags:
        for t in tags:
            if t not in merged_tags:
                merged_tags.append(t)
    return replace(
        base,
        region_default=region or base.region_default,
        plan=plan or base.plan,
        os_id=os_id or base.os_id,
        startup_script=startup_script if startup_script is not None else base.startup_script,
        tags=tuple(merged_tags),
    )