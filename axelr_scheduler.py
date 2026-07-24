# axelr_scheduler.py
# Axelr AI Python Orchestrator Engine (3-2-1 Zero-Cost Architecture)
# Run with: uvicorn axelr_scheduler:app --host 0.0.0.0 --port 5001

import os
import json
import time
import asyncio
try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    import urllib.request as urllib_request
    import urllib.error as urllib_error
    httpx = None
    HAS_HTTPX = False

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List, Dict, Any
from pydantic import BaseModel
import logging

# ============================================================
# LOGGING CONFIGURATION
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("axelr-orchestrator")

# ============================================================
# ENDPOINT CONFIGURATION (Loaded from Environment or Defaults)
# ============================================================
MODEL_ENDPOINTS = {
    "data": {
        "primary": {"url": "http://localhost:11434/api/generate", "timeout": 60},
        "secondary": {"url": "http://localhost:11434/api/generate", "timeout": 45}, # Falls back to same, but we use different models internally
        "backup": {"url": "http://localhost:11434/api/generate", "timeout": 30}
    },
    "design": {
        "primary": {"url": "http://localhost:11434/api/generate", "timeout": 60},
        "secondary": {"url": "http://localhost:11434/api/generate", "timeout": 45},
        "backup": {"url": "http://localhost:11434/api/generate", "timeout": 30}
    },
    "prompt": {
        "primary": {"url": "http://localhost:11434/api/generate", "timeout": 20},
        "backup": {"url": "local_rule_engine", "timeout": 0}
    }
}
app = FastAPI(
    title="Axelr AI Orchestrator Core",
    description="Zero-Cost Multi-Model Routing Engine",
    version="2.1.0"
)

# Enable CORS for local Node.js server cross-talk
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# PYDANTIC SCHEMA DEFINITIONS
# ============================================================
class RouteRequest(BaseModel):
    workspace: str                # "data", "design", or "prompt"
    prompt: str
    history: Optional[List[Dict[str, Any]]] = None
    files: Optional[List[str]] = None
    max_tokens: Optional[int] = 2048
    temperature: Optional[float] = 0.2

class RouteResponse(BaseModel):
    success: bool
    text: str
    provider: str
    model_used: str
    tokens_used: Optional[int] = 0
    latency_ms: Optional[float] = 0.0

# ============================================================
# ASYNCHRONOUS HTTP CLIENT ROUTINE
# ============================================================
async def call_model_endpoint(url: str, payload: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    """Posts payload to external Colab tunnel or local inference node."""
    if not HAS_HTTPX or httpx is None:
        raise RuntimeError("httpx is required for async model endpoint calls but is not installed.")

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException:
            logger.error(f"Timeout reaching model endpoint at {url}")
            raise Exception("Model inference request timed out.")
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error {e.response.status_code} from endpoint {url}: {e.response.text}")
            raise Exception(f"Model HTTP failure ({e.response.status_code})")
        except Exception as e:
            logger.error(f"Network error connecting to endpoint {url}: {str(e)}")
            raise Exception(f"Endpoint unreachable: {str(e)}")

# ============================================================
# PROMPT ENHANCEMENT ENGINE (3-2-1 FAILOVER)
# ============================================================
async def enhance_prompt(user_prompt: str, workspace: str, history: Optional[List[Dict[str, Any]]] = None) -> str:
    """
    Optimizes input prompts prior to primary execution.
    Primary: Co-Dialectic Colab Service
    Secondary: Local Zero-Latency Rule Injector
    Backup: Pass-through raw text
    """
    configs = MODEL_ENDPOINTS["prompt"]
    payload = {"prompt": user_prompt, "workspace": workspace, "history": history or []}

    # 1. Primary: External Optimizer Endpoint
    primary_url = configs["primary"]["url"]
    if primary_url and not primary_url.startswith("http://localhost:8020"):
        try:
            logger.info(f"Dispatching prompt enhancement to primary node: {primary_url}")
            result = await call_model_endpoint(primary_url, payload, configs["primary"]["timeout"])
            enhanced_text = result.get("optimized_prompt") or result.get("text")
            if enhanced_text:
                return enhanced_text
        except Exception as e:
            logger.warning(f"Primary prompt enhancer unavailable ({str(e)}). Failing over to Secondary.")

    # 2. Secondary: Local Zero-Latency System Instruction Injector
    logger.info("Applying Secondary local rule-based system prompt enrichment.")
    if workspace == "design":
        system_instruction = (
            "You are AXELR ARCHITECT — a world-class UI/UX engineer. "
            "Generate production-grade, pixel-perfect, fully responsive HTML/CSS/JS components "
            "utilizing modern Tailwind CSS, flexbox/grid layouts, micro-interactions, and dark mode aesthetics. "
            "Output complete, executable code strictly wrapped inside a single ```html code block.\n\n"
            f"USER SPECIFICATION: {user_prompt}"
        )
    else:
        system_instruction = (
            "You are AXELR DATA — an enterprise data analyst and extraction engine. "
            "Clean, analyze, and transform all raw input into structured insights. "
            "Provide a concise analytical summary followed by raw JSON payload structured inside "
            "[JSON-DATA]...[/JSON-DATA] tags.\n\n"
            f"USER SPECIFICATION: {user_prompt}"
        )
    
    return system_instruction

# ============================================================
# CORE MODEL ROUTING LOGIC
# ============================================================
async def route_to_model(workspace: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Iterates through Primary -> Secondary -> Backup tiers for execution."""
    configs = MODEL_ENDPOINTS.get(workspace)
    if not configs:
        raise ValueError(f"Invalid workspace parameter: {workspace}")

    for tier in ["primary", "secondary", "backup"]:
        cfg = configs[tier]
        url = cfg["url"]

        # Skip unconfigured local placeholder URLs
        if not url or url.startswith("http://localhost:8000") or url.startswith("http://localhost:8010"):
            continue

        logger.info(f"Attempting model execution via [{tier.upper()}] tier at URL: {url}")
        try:
            result = await call_model_endpoint(url, payload, cfg["timeout"])
            return {
                "success": True,
                "text": result.get("text", ""),
                "provider": tier,
                "model_used": result.get("model", "open-source-quantized-7b"),
                "tokens_used": result.get("tokens", len(result.get("text", "").split())),
            }
        except Exception as e:
            logger.warning(f"Tier [{tier.upper()}] execution failed on {url}: {str(e)}")
            continue

    # Final System Fallback
    logger.error(f"CRITICAL: All model execution tiers failed for workspace: {workspace}")
    return {
        "success": False,
        "text": "Axelr Orchestrator Warning: All configured AI model endpoints are currently unreachable. Please check your Colab tunnel connectivity.",
        "provider": "none",
        "model_used": "none",
        "tokens_used": 0,
    }

# ============================================================
# FASTAPI ROUTE ENDPOINTS
# ============================================================
@app.post("/api/route", response_model=RouteResponse)
async def handle_routing(req: RouteRequest):
    """Primary gateway endpoint called by Node.js server.js backend."""
    start_time = time.time()
    
    # Step 1: Prompt Enhancement
    # ensure history is a list when None to satisfy type expectations
    enhanced_prompt = await enhance_prompt(req.prompt, req.workspace, req.history or [])

    # Step 2: Payload Construction
    payload = {
        "prompt": enhanced_prompt,
        "history": req.history or [],
        "files": req.files or [],
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
    }


    # Step 3: Model Execution Routing
    if req.workspace == "prompt":
        result = {
            "success": True,
            "text": enhanced_prompt,
            "provider": "local_enhancer",
            "model_used": "system-rule-injector",
            "tokens_used": len(enhanced_prompt.split()),
        }
        
        
    else:
        result = await route_to_model(req.workspace, payload)

    latency_ms = (time.time() - start_time) * 1000
    return RouteResponse(
        success=result["success"],
        text=result["text"],
        provider=result["provider"],
        model_used=result["model_used"],
        tokens_used=result["tokens_used"],
        latency_ms=round(latency_ms, 2),
    )

@app.get("/api/health")
async def health_check():
    """Health check ping endpoint."""
    return {"status": "operational", "timestamp": time.time(), "engine": "Axelr-Orchestrator-v2"}

@app.get("/api/models/status")
async def model_status():
    """Returns endpoint configurations and connectivity status."""
    status = {}
    for ws, tiers in MODEL_ENDPOINTS.items():
        status[ws] = {}
        for tier, cfg in tiers.items():
            status[ws][tier] = {
                "url": cfg["url"],
                "timeout": cfg["timeout"]
            }
    return status

import base64

def extract_file_content(files: List[Dict]) -> str:
    """Decode base64 files and extract text content (supports CSV, TXT, JSON, PDF, etc.)"""
    extracted = ""
    for f in files:
        filename = f.get("filename", "file")
        content_b64 = f.get("content_base64", "")
        if not content_b64:
            continue
        try:
            data = base64.b64decode(content_b64)
            # For simplicity, try to decode as text (UTF-8)
            text = data.decode("utf-8", errors="ignore")
            extracted += f"\n--- Content of {filename} ---\n{text}\n"
        except:
            extracted += f"\n[Binary file {filename} – content not extractable]\n"
    return extracted