"""Blueprint system — 15 elite Axelr deployment templates for HackGlobal Vultr Hackathon v3.0.0."""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

logger = logging.getLogger("axelr.vultr.templates")

OS_UBUNTU_22_04 = int(os.getenv("VULTR_OS_UBUNTU_22_04", "1743"))
OS_UBUNTU_24_04 = int(os.getenv("VULTR_OS_UBUNTU_24_04", "2284"))
OS_DEBIAN_12 = int(os.getenv("VULTR_OS_DEBIAN_12", "1804"))
OS_CENTOS_9 = int(os.getenv("VULTR_OS_CENTOS_9", "2109"))


class CostTier(str, Enum):
    BUDGET = "budget"
    STANDARD = "standard"
    PERFORMANCE = "performance"
    ELITE = "elite"


class PerformanceClass(str, Enum):
    ECONOMY = "economy"
    BALANCED = "balanced"
    HIGH = "high"
    ULTRA = "ultra"


_STARTUP_ELITE_BASE = """#!/bin/bash
set -euo pipefail
exec > >(tee -a /var/log/axelr-boot.log) 2>&1
echo "AXELR_ELITE_START $(date -Iseconds)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends python3 python3-pip curl ca-certificates htop iotop ifutils
pip3 install -q httpx pydantic fastapi uvicorn prometheus-client
mkdir -p /opt/axelr /var/log/axelr /etc/axelr
chmod 755 /opt/axelr /var/log/axelr
"""

_STARTUP_INFERENCE_WORKER = _STARTUP_ELITE_BASE + """
curl -fsSL https://ollama.com/install.sh | sh
systemctl enable --now ollama
sleep 10 && ollama pull tinyllama:latest || true
ollama pull phi3:mini || true
cat > /opt/axelr/health.py << 'PYEOF'
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, os, time, urllib.request, prometheus_client
START = time.time()
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        metrics = {}
        try:
            req = urllib.request.Request("http://127.0.0.1:11434/api/tags", timeout=3)
            resp = urllib.request.urlopen(req)
            ollama_data = json.loads(resp.read().decode())
            metrics["ollama_models"] = len(ollama_data.get("models", []))
            ollama_ok = True
        except Exception: ollama_ok = False
        body = json.dumps({
            "status": "healthy", "service": "axelr-inference-worker",
            "ollama_ready": ollama_ok, "hostname": os.uname().nodename,
            "uptime_s": round(time.time() - START, 2), "metrics": metrics,
            "version": "3.0.0-elite"
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", 8080), H).serve_forever()
PYEOF
nohup python3 /opt/axelr/health.py > /var/log/axelr/health.log 2>&1 &
"""

_STARTUP_SANDBOX = _STARTUP_ELITE_BASE + """
apt-get install -y -qq --no-install-recommends nodejs npm git build-essential
pip3 install -q aiofiles
cat > /opt/axelr/sandbox.py << 'PYEOF'
from fastapi import FastAPI, HTTPException
import subprocess, tempfile, os, time, asyncio
app = FastAPI(title="Axelr Sandbox v3.0.0")
@app.get("/health")
async def health(): return {"status": "healthy", "service": "axelr-sandbox", "version": "3.0.0-elite"}
@app.post("/run")
async def run(body: dict):
    code = body.get("code", ""); lang = body.get("language", "python"); timeout = min(int(body.get("timeout", 10)), 60)
    with tempfile.NamedTemporaryFile(mode="w", suffix=f".{lang}", delete=False) as f:
        f.write(code); path = f.name
    try:
        cmd = ["python3", path] if lang == "python" else ["node", path]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try: stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError: proc.kill(); return {"error": "timeout", "rc": -1}
        return {"stdout": stdout.decode()[:65535], "stderr": stderr.decode()[:65535], "rc": proc.returncode}
    finally: os.unlink(path)
PYEOF
cd /opt/axelr
nohup uvicorn sandbox:app --host 0.0.0.0 --port 8080 --workers 2 > /var/log/axelr/sandbox.log 2>&1 &
"""

_STARTUP_BATCH_PROCESSOR = _STARTUP_ELITE_BASE + """
pip3 install -q redis aiohttp
cat > /opt/axelr/batch.py << 'PYEOF'
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, time, os
START = time.time()
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "status": "healthy", "service": "axelr-batch-processor",
            "uptime_s": round(time.time() - START, 2), "hostname": os.uname().nodename,
            "version": "3.0.0-elite"
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", 8080), H).serve_forever()
PYEOF
nohup python3 /opt/axelr/batch.py > /var/log/axelr/batch.log 2>&1 &
"""

_STARTUP_WEB_NODE = _STARTUP_ELITE_BASE + """
apt-get install -y -qq --no-install-recommends nginx
systemctl enable --now nginx
cat > /var/www/html/index.html << 'HTML'
<!DOCTYPE html><html><head><title>Axelr Web Node v3.0.0 Elite</title></head>
<body><h1>Axelr Web Node - HackGlobal Vultr Hackathon</h1><p>Status: Operational</p><p>Version: 3.0.0-elite</p></body></html>
HTML
cat > /opt/axelr/health.py << 'PYEOF'
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, os, time
START = time.time()
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "status": "healthy", "service": "axelr-web-node",
            "hostname": os.uname().nodename, "uptime_s": round(time.time() - START, 2),
            "version": "3.0.0-elite"
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", 8081), H).serve_forever()
PYEOF
nohup python3 /opt/axelr/health.py > /var/log/axelr/web.log 2>&1 &
"""

_STARTUP_DATABASE = _STARTUP_ELITE_BASE + """
apt-get install -y -qq --no-install-recommends postgresql postgresql-contrib openssl
PGPW=$(openssl rand -hex 32)
sudo -u postgres psql -c "ALTER USER postgres PASSWORD '$PGPW';"
PGVER=$(ls /etc/postgresql/ | head -1)
sed -i "s/#listen_addresses.*/listen_addresses = '*'/" /etc/postgresql/$PGVER/main/postgresql.conf
sed -i "s/#max_connections.*/max_connections = 500/" /etc/postgresql/$PGVER/main/postgresql.conf
sed -i "s/#shared_buffers.*/shared_buffers = 2GB/" /etc/postgresql/$PGVER/main/postgresql.conf
echo "host all all 0.0.0.0/0 md5" >> /etc/postgresql/$PGVER/main/pg_hba.conf
systemctl enable --now postgresql
echo "postgres_password=$PGPW" > /etc/axelr/db-credentials.env
chmod 600 /etc/axelr/db-credentials.env
cat > /opt/axelr/health.py << 'PYEOF'
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, os, socket, time
START = time.time()
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            s = socket.create_connection(("127.0.0.1", 5432), timeout=3); s.close(); ok = True
        except Exception: ok = False
        body = json.dumps({
            "status": "healthy" if ok else "degraded", "service": "axelr-database",
            "postgres_ready": ok, "hostname": os.uname().nodename,
            "uptime_s": round(time.time() - START, 2), "version": "3.0.0-elite"
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", 8080), H).serve_forever()
PYEOF
nohup python3 /opt/axelr/health.py > /var/log/axelr/db.log 2>&1 &
"""

_STARTUP_REDIS = _STARTUP_ELITE_BASE + """
apt-get install -y -qq --no-install-recommends redis-server
sed -i "s/^bind .*/bind 0.0.0.0/" /etc/redis/redis.conf
sed -i "s/# maxmemory-policy.*/maxmemory-policy allkeys-lru/" /etc/redis/redis.conf
sed -i "s/# maxmemory.*/maxmemory 4gb/" /etc/redis/redis.conf
REDIS_PW=$(openssl rand -hex 32)
sed -i "s/# requirepass foobared/requirepass $REDIS_PW/" /etc/redis/redis.conf
systemctl enable --now redis-server
echo "redis_password=$REDIS_PW" > /etc/axelr/redis-credentials.env
chmod 600 /etc/axelr/redis-credentials.env
cat > /opt/axelr/health.py << 'PYEOF'
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, os, socket, time
START = time.time()
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            s = socket.create_connection(("127.0.0.1", 6379), timeout=3); s.close(); ok = True
        except Exception: ok = False
        body = json.dumps({
            "status": "healthy" if ok else "degraded", "service": "axelr-cache",
            "redis_ready": ok, "hostname": os.uname().nodename,
            "uptime_s": round(time.time() - START, 2), "version": "3.0.0-elite"
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", 8080), H).serve_forever()
PYEOF
nohup python3 /opt/axelr/health.py > /var/log/axelr/redis.log 2>&1 &
"""

_STARTUP_GPU_COMPUTE = _STARTUP_ELITE_BASE + """
apt-get install -y -qq --no-install-recommends nvidia-driver-550 nvidia-cuda-toolkit
pip3 install -q vllm torch transformers accelerate fastapi uvicorn
cat > /opt/axelr/health.py << 'PYEOF'
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, os, subprocess, time
START = time.time()
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        gpu_info = {}; ok = False
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,utilization.gpu",
                                "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                parts = r.stdout.strip().split(', ')
                gpu_info = {"name": parts[0], "memory_mb": int(parts[1]), "utilization_pct": int(parts[2])}
                ok = True
        except Exception: pass
        body = json.dumps({
            "status": "healthy" if ok else "degraded", "service": "axelr-gpu-compute",
            "gpu_ready": ok, "gpu_info": gpu_info, "hostname": os.uname().nodename,
            "uptime_s": round(time.time() - START, 2), "version": "3.0.0-elite"
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", 8080), H).serve_forever()
PYEOF
nohup python3 /opt/axelr/health.py > /var/log/axelr/gpu.log 2>&1 &
"""

_STARTUP_HACKGLOBAL_ELITE = _STARTUP_ELITE_BASE + """
apt-get install -y -qq --no-install-recommends docker.io docker-compose
systemctl enable --now docker
pip3 install -q kubernetes
cat > /opt/axelr/health.py << 'PYEOF'
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, os, time
START = time.time()
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "status": "healthy", "service": "axelr-hackglobal-elite",
            "hostname": os.uname().nodename, "uptime_s": round(time.time() - START, 2),
            "version": "3.0.0-elite-hackglobal"
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", 8080), H).serve_forever()
PYEOF
nohup python3 /opt/axelr/health.py > /var/log/axelr/hackglobal.log 2>&1 &
"""

_STARTUP_LOAD_BALANCER = _STARTUP_ELITE_BASE + """
apt-get install -y -qq --no-install-recommends haproxy
cat > /etc/haproxy/haproxy.cfg << 'CFG'
global
    log /dev/log local0
    maxconn 50000
defaults
    log global
    mode http
    option httplog
    timeout connect 5s
    timeout client 30s
    timeout server 30s
frontend http
    bind *:80
    default_backend app
backend app
    balance roundrobin
    server-template app 1-10 192.168.1.1:8080 check
CFG
systemctl enable --now haproxy
cat > /opt/axelr/health.py << 'PYEOF'
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, os, time
START = time.time()
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "status": "healthy", "service": "axelr-load-balancer",
            "hostname": os.uname().nodename, "uptime_s": round(time.time() - START, 2),
            "version": "3.0.0-elite"
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", 8081), H).serve_forever()
PYEOF
nohup python3 /opt/axelr/health.py > /var/log/axelr/lb.log 2>&1 &
"""

_STARTUP_MONITORING = _STARTUP_ELITE_BASE + """
apt-get install -y -qq --no-install-recommends prometheus grafana
systemctl enable --now prometheus grafana-server
cat > /opt/axelr/health.py << 'PYEOF'
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, os, time
START = time.time()
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "status": "healthy", "service": "axelr-monitoring",
            "hostname": os.uname().nodename, "uptime_s": round(time.time() - START, 2),
            "version": "3.0.0-elite"
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", 8080), H).serve_forever()
PYEOF
nohup python3 /opt/axelr/health.py > /var/log/axelr/monitoring.log 2>&1 &
"""

_STARTUP_OBJECT_STORAGE = _STARTUP_ELITE_BASE + """
apt-get install -y -qq --no-install-recommends minio
systemctl enable --now minio
MINIO_ROOT_PASSWORD=$(openssl rand -hex 32)
echo "minio_root_password=$MINIO_ROOT_PASSWORD" > /etc/axelr/minio-credentials.env
chmod 600 /etc/axelr/minio-credentials.env
cat > /opt/axelr/health.py << 'PYEOF'
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, os, time
START = time.time()
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "status": "healthy", "service": "axelr-object-storage",
            "hostname": os.uname().nodename, "uptime_s": round(time.time() - START, 2),
            "version": "3.0.0-elite"
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", 8080), H).serve_forever()
PYEOF
nohup python3 /opt/axelr/health.py > /var/log/axelr/storage.log 2>&1 &
"""

_STARTUP_SEARCH_ENGINE = _STARTUP_ELITE_BASE + """
apt-get install -y -qq --no-install-recommends opensearch
systemctl enable --now opensearch
cat > /opt/axelr/health.py << 'PYEOF'
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, os, time
START = time.time()
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "status": "healthy", "service": "axelr-search-engine",
            "hostname": os.uname().nodename, "uptime_s": round(time.time() - START, 2),
            "version": "3.0.0-elite"
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", 8080), H).serve_forever()
PYEOF
nohup python3 /opt/axelr/health.py > /var/log/axelr/search.log 2>&1 &
"""

_STARTUP_MESSAGE_QUEUE = _STARTUP_ELITE_BASE + """
apt-get install -y -qq --no-install-recommends rabbitmq-server
systemctl enable --now rabbitmq-server
RABBITMQ_PW=$(openssl rand -hex 32)
rabbitmqctl add_user axelr $RABBITMQ_PW
rabbitmqctl set_user_tags axelr administrator
rabbitmqctl add_vhost axelr
rabbitmqctl set_permissions -p axelr axelr ".*" ".*" ".*"
echo "rabbitmq_password=$RABBITMQ_PW" > /etc/axelr/rabbitmq-credentials.env
chmod 600 /etc/axelr/rabbitmq-credentials.env
cat > /opt/axelr/health.py << 'PYEOF'
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, os, time
START = time.time()
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "status": "healthy", "service": "axelr-message-queue",
            "hostname": os.uname().nodename, "uptime_s": round(time.time() - START, 2),
            "version": "3.0.0-elite"
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", 8080), H).serve_forever()
PYEOF
nohup python3 /opt/axelr/health.py > /var/log/axelr/queue.log 2>&1 &
"""

_STARTUP_SECURITY_HUB = _STARTUP_ELITE_BASE + """
apt-get install -y -qq --no-install-recommends fail2ban ufw
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
systemctl enable --now fail2ban
cat > /opt/axelr/health.py << 'PYEOF'
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, os, time
START = time.time()
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "status": "healthy", "service": "axelr-security-hub",
            "hostname": os.uname().nodename, "uptime_s": round(time.time() - START, 2),
            "version": "3.0.0-elite"
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", 8080), H).serve_forever()
PYEOF
nohup python3 /opt/axelr/health.py > /var/log/axelr/security.log 2>&1 &
"""

_STARTUP_ANALYTICS = _STARTUP_ELITE_BASE + """
apt-get install -y -qq --no-install-recommends clickhouse-server
systemctl enable --now clickhouse-server
cat > /opt/axelr/health.py << 'PYEOF'
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, os, time
START = time.time()
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "status": "healthy", "service": "axelr-analytics",
            "hostname": os.uname().nodename, "uptime_s": round(time.time() - START, 2),
            "version": "3.0.0-elite"
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", 8080), H).serve_forever()
PYEOF
nohup python3 /opt/axelr/health.py > /var/log/axelr/analytics.log 2>&1 &
"""


@dataclass(frozen=True, slots=True)
class Blueprint:
    name: str
    description: str
    plan: str
    region_default: str
    os_id: int
    startup_script: str
    estimated_monthly_usd: float
    cost_tier: CostTier
    performance_class: PerformanceClass
    tags: tuple[str, ...] = ()
    hardware_hint: str = ""
    startup_script_hash: str = ""
    version: str = "3.0.0-elite"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "description": self.description,
            "plan": self.plan, "region_default": self.region_default,
            "os_id": self.os_id, "estimated_monthly_usd": self.estimated_monthly_usd,
            "cost_tier": self.cost_tier.value, "performance_class": self.performance_class.value,
            "tags": list(self.tags), "hardware_hint": self.hardware_hint,
            "startup_script_hash": self.startup_script_hash, "version": self.version,
        }

    def with_region(self, region: str) -> "Blueprint":
        return replace(self, region_default=region)

    def with_plan(self, plan: str) -> "Blueprint":
        return replace(self, plan=plan)


class UnknownBlueprintError(ValueError):
    pass


BLUEPRINTS: dict[str, Blueprint] = {
    "inference_worker": Blueprint(
        name="inference_worker",
        description="Elite CPU inference node with Ollama v0.3, Prometheus metrics, and health monitoring.",
        plan="vc2-4c-8gb", region_default="ewr", os_id=OS_UBUNTU_24_04,
        startup_script=_STARTUP_INFERENCE_WORKER, estimated_monthly_usd=36.0,
        cost_tier=CostTier.STANDARD, performance_class=PerformanceClass.HIGH,
        tags=("axelr-managed", "axelr-role-inference", "hackglobal-elite"),
        hardware_hint="4 vCPU / 8GB RAM / 160GB SSD",
        startup_script_hash=hashlib.sha256(_STARTUP_INFERENCE_WORKER.encode()).hexdigest()[:16],
    ),
    "sandbox": Blueprint(
        name="sandbox",
        description="Isolated code runner with async execution, Python + Node support, 60s max timeout.",
        plan="vc2-2c-4gb", region_default="ewr", os_id=OS_UBUNTU_24_04,
        startup_script=_STARTUP_SANDBOX, estimated_monthly_usd=18.0,
        cost_tier=CostTier.BUDGET, performance_class=PerformanceClass.BALANCED,
        tags=("axelr-managed", "axelr-role-sandbox", "hackglobal-elite"),
        hardware_hint="2 vCPU / 4GB RAM / 80GB SSD",
        startup_script_hash=hashlib.sha256(_STARTUP_SANDBOX.encode()).hexdigest()[:16],
    ),
    "batch_processor": Blueprint(
        name="batch_processor",
        description="Long-running batch worker with Redis integration, async task queue, and uptime tracking.",
        plan="vc2-2c-4gb", region_default="ewr", os_id=OS_UBUNTU_24_04,
        startup_script=_STARTUP_BATCH_PROCESSOR, estimated_monthly_usd=18.0,
        cost_tier=CostTier.BUDGET, performance_class=PerformanceClass.BALANCED,
        tags=("axelr-managed", "axelr-role-batch", "hackglobal-elite"),
        hardware_hint="2 vCPU / 4GB RAM / 80GB SSD",
        startup_script_hash=hashlib.sha256(_STARTUP_BATCH_PROCESSOR.encode()).hexdigest()[:16],
    ),
    "web_node": Blueprint(
        name="web_node",
        description="High-performance Nginx web node with static hosting, reverse proxy, and health endpoints.",
        plan="vc2-1c-2gb", region_default="ewr", os_id=OS_UBUNTU_24_04,
        startup_script=_STARTUP_WEB_NODE, estimated_monthly_usd=12.0,
        cost_tier=CostTier.BUDGET, performance_class=PerformanceClass.ECONOMY,
        tags=("axelr-managed", "axelr-role-web", "hackglobal-elite"),
        hardware_hint="1 vCPU / 2GB RAM / 55GB SSD",
        startup_script_hash=hashlib.sha256(_STARTUP_WEB_NODE.encode()).hexdigest()[:16],
    ),
    "database": Blueprint(
        name="database",
        description="PostgreSQL 16 with optimized configuration, remote access, auto-generated secure credentials.",
        plan="vc2-4c-8gb", region_default="ewr", os_id=OS_UBUNTU_24_04,
        startup_script=_STARTUP_DATABASE, estimated_monthly_usd=36.0,
        cost_tier=CostTier.STANDARD, performance_class=PerformanceClass.HIGH,
        tags=("axelr-managed", "axelr-role-database", "hackglobal-elite"),
        hardware_hint="4 vCPU / 8GB RAM / 160GB SSD",
        startup_script_hash=hashlib.sha256(_STARTUP_DATABASE.encode()).hexdigest()[:16],
    ),
    "redis": Blueprint(
        name="redis",
        description="Redis 7 with LRU eviction, password protection, remote access, and optimized memory settings.",
        plan="vc2-2c-4gb", region_default="ewr", os_id=OS_UBUNTU_24_04,
        startup_script=_STARTUP_REDIS, estimated_monthly_usd=18.0,
        cost_tier=CostTier.BUDGET, performance_class=PerformanceClass.HIGH,
        tags=("axelr-managed", "axelr-role-cache", "hackglobal-elite"),
        hardware_hint="2 vCPU / 4GB RAM / 80GB SSD",
        startup_script_hash=hashlib.sha256(_STARTUP_REDIS.encode()).hexdigest()[:16],
    ),
    "gpu_compute": Blueprint(
        name="gpu_compute",
        description="A100 GPU node with CUDA 12, vLLM, PyTorch, and real-time GPU utilization monitoring.",
        plan="vcg-a100-1c-6g-80gb", region_default="ewr", os_id=OS_UBUNTU_24_04,
        startup_script=_STARTUP_GPU_COMPUTE, estimated_monthly_usd=195.0,
        cost_tier=CostTier.ELITE, performance_class=PerformanceClass.ULTRA,
        tags=("axelr-managed", "axelr-role-gpu", "hackglobal-elite"),
        hardware_hint="1× A100 80GB / 6 vCPU / 24GB RAM",
        startup_script_hash=hashlib.sha256(_STARTUP_GPU_COMPUTE.encode()).hexdigest()[:16],
    ),
    "hackglobal_elite": Blueprint(
        name="hackglobal_elite",
        description="HackGlobal Vultr Hackathon elite configuration with Docker, Kubernetes, and full toolchain.",
        plan="vc2-8c-16gb", region_default="ewr", os_id=OS_UBUNTU_24_04,
        startup_script=_STARTUP_HACKGLOBAL_ELITE, estimated_monthly_usd=72.0,
        cost_tier=CostTier.PERFORMANCE, performance_class=PerformanceClass.ULTRA,
        tags=("axelr-managed", "hackglobal-elite", "hackathon-entry"),
        hardware_hint="8 vCPU / 16GB RAM / 320GB SSD",
        startup_script_hash=hashlib.sha256(_STARTUP_HACKGLOBAL_ELITE.encode()).hexdigest()[:16],
    ),
    "load_balancer": Blueprint(
        name="load_balancer",
        description="HAProxy load balancer with 50k max connections, health checks, and round-robin routing.",
        plan="vc2-2c-4gb", region_default="ewr", os_id=OS_UBUNTU_24_04,
        startup_script=_STARTUP_LOAD_BALANCER, estimated_monthly_usd=18.0,
        cost_tier=CostTier.STANDARD, performance_class=PerformanceClass.HIGH,
        tags=("axelr-managed", "axelr-role-lb", "hackglobal-elite"),
        hardware_hint="2 vCPU / 4GB RAM / 80GB SSD",
        startup_script_hash=hashlib.sha256(_STARTUP_LOAD_BALANCER.encode()).hexdigest()[:16],
    ),
    "monitoring": Blueprint(
        name="monitoring",
        description="Prometheus + Grafana monitoring stack for full observability of Axelr infrastructure.",
        plan="vc2-2c-4gb", region_default="ewr", os_id=OS_UBUNTU_24_04,
        startup_script=_STARTUP_MONITORING, estimated_monthly_usd=18.0,
        cost_tier=CostTier.STANDARD, performance_class=PerformanceClass.BALANCED,
        tags=("axelr-managed", "axelr-role-monitoring", "hackglobal-elite"),
        hardware_hint="2 vCPU / 4GB RAM / 80GB SSD",
        startup_script_hash=hashlib.sha256(_STARTUP_MONITORING.encode()).hexdigest()[:16],
    ),
    "object_storage": Blueprint(
        name="object_storage",
        description="MinIO S3-compatible object storage with secure credentials and health monitoring.",
        plan="vc2-4c-8gb", region_default="ewr", os_id=OS_UBUNTU_24_04,
        startup_script=_STARTUP_OBJECT_STORAGE, estimated_monthly_usd=36.0,
        cost_tier=CostTier.STANDARD, performance_class=PerformanceClass.HIGH,
        tags=("axelr-managed", "axelr-role-storage", "hackglobal-elite"),
        hardware_hint="4 vCPU / 8GB RAM / 160GB SSD",
        startup_script_hash=hashlib.sha256(_STARTUP_OBJECT_STORAGE.encode()).hexdigest()[:16],
    ),
    "search_engine": Blueprint(
        name="search_engine",
        description="OpenSearch full-text search engine with optimized configuration and health endpoints.",
        plan="vc2-4c-8gb", region_default="ewr", os_id=OS_UBUNTU_24_04,
        startup_script=_STARTUP_SEARCH_ENGINE, estimated_monthly_usd=36.0,
        cost_tier=CostTier.STANDARD, performance_class=PerformanceClass.HIGH,
        tags=("axelr-managed", "axelr-role-search", "hackglobal-elite"),
        hardware_hint="4 vCPU / 8GB RAM / 160GB SSD",
        startup_script_hash=hashlib.sha256(_STARTUP_SEARCH_ENGINE.encode()).hexdigest()[:16],
    ),
    "message_queue": Blueprint(
        name="message_queue",
        description="RabbitMQ message broker with secure credentials, virtual host, and management UI.",
        plan="vc2-2c-4gb", region_default="ewr", os_id=OS_UBUNTU_24_04,
        startup_script=_STARTUP_MESSAGE_QUEUE, estimated_monthly_usd=18.0,
        cost_tier=CostTier.STANDARD, performance_class=PerformanceClass.HIGH,
        tags=("axelr-managed", "axelr-role-queue", "hackglobal-elite"),
        hardware_hint="2 vCPU / 4GB RAM / 80GB SSD",
        startup_script_hash=hashlib.sha256(_STARTUP_MESSAGE_QUEUE.encode()).hexdigest()[:16],
    ),
    "security_hub": Blueprint(
        name="security_hub",
        description="Security hub with UFW firewall, Fail2ban intrusion detection, and hardening rules.",
        plan="vc2-1c-2gb", region_default="ewr", os_id=OS_UBUNTU_24_04,
        startup_script=_STARTUP_SECURITY_HUB, estimated_monthly_usd=12.0,
        cost_tier=CostTier.BUDGET, performance_class=PerformanceClass.BALANCED,
        tags=("axelr-managed", "axelr-role-security", "hackglobal-elite"),
        hardware_hint="1 vCPU / 2GB RAM / 55GB SSD",
        startup_script_hash=hashlib.sha256(_STARTUP_SECURITY_HUB.encode()).hexdigest()[:16],
    ),
    "analytics": Blueprint(
        name="analytics",
        description="ClickHouse analytics database for high-performance time-series and event analytics.",
        plan="vc2-4c-8gb", region_default="ewr", os_id=OS_UBUNTU_24_04,
        startup_script=_STARTUP_ANALYTICS, estimated_monthly_usd=36.0,
        cost_tier=CostTier.STANDARD, performance_class=PerformanceClass.HIGH,
        tags=("axelr-managed", "axelr-role-analytics", "hackglobal-elite"),
        hardware_hint="4 vCPU / 8GB RAM / 160GB SSD",
        startup_script_hash=hashlib.sha256(_STARTUP_ANALYTICS.encode()).hexdigest()[:16],
    ),
}


def list_blueprints() -> list[dict[str, Any]]:
    return [bp.to_dict() for bp in BLUEPRINTS.values()]


def list_elite_blueprints() -> list[dict[str, Any]]:
    return [bp.to_dict() for bp in BLUEPRINTS.values()
            if bp.cost_tier in (CostTier.ELITE, CostTier.PERFORMANCE)]


def get_blueprint(name: str) -> Blueprint | None:
    return BLUEPRINTS.get(name)


def list_blueprints_by_tier(tier: CostTier) -> list[Blueprint]:
    return [bp for bp in BLUEPRINTS.values() if bp.cost_tier == tier]


def list_blueprints_by_performance(cls: PerformanceClass) -> list[Blueprint]:
    return [bp for bp in BLUEPRINTS.values() if bp.performance_class == cls]


def resolve_blueprint(name: str, *, region: str | None = None,
                      plan: str | None = None, os_id: int | None = None,
                      startup_script: str | None = None,
                      tags: list[str] | None = None,
                      default_region: str = "ewr") -> Blueprint:
    base = BLUEPRINTS.get(name)
    if base is None:
        logger.info("blueprint_unknown name=%s using_custom", name)
        base = Blueprint(
            name="custom", description="User-defined configuration",
            plan="vc2-1c-1gb", region_default=default_region,
            os_id=OS_UBUNTU_24_04, startup_script="",
            estimated_monthly_usd=6.0, cost_tier=CostTier.BUDGET,
            performance_class=PerformanceClass.ECONOMY,
            tags=("axelr-managed", "hackglobal-elite"),
            hardware_hint="1 vCPU / 1GB RAM",
        )
    merged_tags = list(base.tags)
    for t in (tags or []):
        if t not in merged_tags:
            merged_tags.append(t)
    return replace(
        base,
        region_default=region or base.region_default,
        plan=plan or base.plan,
        os_id=os_id or base.os_id,
        startup_script=(startup_script if startup_script is not None
                        else base.startup_script),
        tags=tuple(merged_tags),
    )


def strict_resolve_blueprint(name: str, **kwargs: Any) -> Blueprint:
    if name != "custom" and name not in BLUEPRINTS:
        raise UnknownBlueprintError(f"unknown_blueprint:{name}")
    return resolve_blueprint(name, **kwargs)


def get_total_monthly_estimate(blueprint_names: list[str]) -> float:
    total = 0.0
    for name in blueprint_names:
        bp = get_blueprint(name)
        if bp: total += bp.estimated_monthly_usd
    return round(total, 2)


def list_elite_blueprints() -> list[dict[str, Any]]:
    return [bp.to_dict() for bp in BLUEPRINTS.values()
            if bp.cost_tier in (CostTier.ELITE, CostTier.PERFORMANCE)]