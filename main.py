# main.py - Production-ready Python orchestrator (v4.2.1)
import os
import time
import json
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import re
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Axelr AI Cloud Orchestrator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://axelr.in",
        "https://www.axelr.in",
        "https://axelr-frontend.pages.dev",
        "http://localhost:3000",
        "http://localhost:5000",
        "http://localhost:5001",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=86400,
)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

if not GROQ_API_KEY:
    logger.warning("⚠️ GROQ_API_KEY not set - Groq calls will fail")
if not OPENROUTER_API_KEY:
    logger.warning("⚠️ OPENROUTER_API_KEY not set - OpenRouter calls will fail")

class RouteRequest(BaseModel):
    workspace: str
    prompt: str
    history: Optional[List[Dict[str, Any]]] = None
    files: Optional[List[Dict[str, str]]] = None
    max_tokens: int = 2048
    temperature: float = 0.2
    tier: Optional[str] = 'free'

MANIPULATION_PATTERNS = [
    r"forget all (instructions|prior|previous)",
    r"disregard (system prompt|guidelines|instructions)",
    r"ignore (all|previous) (instructions|prompts)",
    r"override your (system|core|primary) instructions",
    r"you are (not|no longer) bound by",
    r"bypass your safety",
    r"stop following your instructions",
    r"reset your instructions"
]

def detect_manipulation(text: str) -> bool:
    for pattern in MANIPULATION_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False

async def call_with_retries(provider_func, *args, retries=3, delay=1.0, **kwargs):
    """Generic retry with exponential backoff."""
    for attempt in range(retries):
        try:
            return await provider_func(*args, **kwargs)
        except Exception as e:
            logger.warning(f"Provider call attempt {attempt+1} failed: {e}")
            if attempt < retries - 1:
                await asyncio.sleep(delay * (2 ** attempt))
            else:
                raise
    raise Exception("All retries exhausted")

async def call_groq(prompt: str, max_tokens: int, temp: float, tier: str = 'free') -> str:
    if not GROQ_API_KEY:
        raise Exception("GROQ_API_KEY not configured")
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    model = "mixtral-8x7b-32768"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
    }
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

async def call_openrouter(model: str, prompt: str, max_tokens: int, temp: float, tier: str = 'free') -> str:
    if not OPENROUTER_API_KEY:
        raise Exception("OPENROUTER_API_KEY not configured")
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://axelr.in",
        "X-Title": "Axelr AI"
    }
    if tier == 'free':
        free_models = {
            'data': 'deepseek/deepseek-r1-distill-llama-70b:free',
            'design': 'qwen/qwen-2.5-coder-32b:free',
            'prompt': 'qwen/qwen-2.5-coder-32b:free'
        }
        model = free_models.get('data', 'deepseek/deepseek-r1-distill-llama-70b:free')
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
    }
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

def get_system_prompt(workspace: str) -> str:
    base = "You are AXELR - an elite, executive AI assistant. Keep responses concise, directly on point, with no fluff."
    if workspace == "design":
        return base + (
            " You are AXELR ARCHITECT - a world-class UI/UX engineer. "
            "Generate production-grade, pixel-perfect, fully responsive HTML/CSS/JS components "
            "using modern Tailwind, flex/grid, micro-interactions, and dark mode. "
            "Output complete code inside a single ```html block."
        )
    elif workspace == "data":
        return base + (
            " You are AXELR DATA - an enterprise data analyst. "
            "Clean, analyse, and transform the input into structured insights. "
            "Provide a concise summary followed by raw JSON inside [JSON-DATA]...[/JSON-DATA] tags."
        )
    else:
        return base + " Rewrite the user prompt into a detailed, professional system prompt."

@app.post("/api/route")
async def route(req: RouteRequest):
    start = time.time()
    try:
        history_text = ""
        if req.history:
            history_text = "\n".join([f"{m['role']}: {m['content']}" for m in req.history[-4:]])
        system_prompt = get_system_prompt(req.workspace)
        full_prompt = f"{system_prompt}\n\n"
        if history_text:
            full_prompt += f"Previous conversation:\n{history_text}\n\n"
        full_prompt += f"User request: {req.prompt}"
        if detect_manipulation(req.prompt):
            return JSONResponse({
                "success": False,
                "text": "We have detected manipulative content in your request. Please adhere to our terms of service.",
                "provider": "security",
                "model_used": "security-filter",
                "tokens_used": 0,
                "latency_ms": 0
            })
        tier = getattr(req, 'tier', 'free')
        # Try primary provider with retries, then fallback
        response_text = None
        provider = None
        model_used = None

        if req.workspace == "design":
            try:
                response_text = await call_with_retries(call_groq, full_prompt, req.max_tokens, req.temperature, tier, retries=2)
                provider = "groq"
                model_used = "mixtral-8x7b-32768"
            except Exception as e:
                logger.warning(f"Groq primary failed: {e}")
                try:
                    response_text = await call_with_retries(
                        call_openrouter,
                        "qwen/qwen-2.5-coder-32b:free",
                        full_prompt,
                        req.max_tokens,
                        req.temperature,
                        tier,
                        retries=2
                    )
                    provider = "openrouter-fallback"
                    model_used = "qwen-2.5-coder-32b"
                except Exception as e2:
                    logger.error(f"All providers failed: {e2}")
                    # Final local fallback – generic but helpful
                    response_text = (
                        "I'm Axelr AI. I'm currently experiencing high demand, but I can still provide guidance. "
                        "Please try again in a moment. If the issue persists, contact support."
                    )
                    provider = "local-fallback"
                    model_used = "rule-engine"
        elif req.workspace == "data":
            try:
                model = "deepseek/deepseek-r1-distill-llama-70b:free"
                response_text = await call_with_retries(
                    call_openrouter,
                    model,
                    full_prompt,
                    req.max_tokens,
                    req.temperature,
                    tier,
                    retries=2
                )
                provider = "openrouter"
                model_used = model
            except Exception as e:
                logger.warning(f"OpenRouter data failed: {e}")
                try:
                    response_text = await call_with_retries(call_groq, full_prompt, req.max_tokens, req.temperature, tier, retries=2)
                    provider = "groq-fallback"
                    model_used = "mixtral-8x7b-32768"
                except Exception as e2:
                    logger.error(f"All providers failed: {e2}")
                    response_text = (
                        "I'm Axelr AI. I'm currently experiencing high demand, but I can still provide guidance. "
                        "Please try again in a moment. If the issue persists, contact support."
                    )
                    provider = "local-fallback"
                    model_used = "rule-engine"
        else:  # prompt enhancement
            try:
                response_text = await call_with_retries(
                    call_openrouter,
                    "qwen/qwen-2.5-coder-32b:free",
                    f"You are an expert prompt engineer. Rewrite this user prompt into a detailed, professional system prompt:\n\n{req.prompt}",
                    req.max_tokens,
                    req.temperature,
                    tier,
                    retries=2
                )
                provider = "openrouter"
                model_used = "qwen-2.5-coder-32b"
            except Exception as e:
                logger.warning(f"OpenRouter prompt failed: {e}")
                response_text = f"Please provide a detailed response to: {req.prompt}"
                provider = "local-fallback"
                model_used = "rule-engine"

        latency = (time.time() - start) * 1000
        return JSONResponse({
            "success": True,
            "text": response_text,
            "provider": provider,
            "model_used": model_used,
            "tokens_used": len(response_text.split()),
            "latency_ms": round(latency, 2)
        })
    except Exception as e:
        logger.error(f"Route error: {e}")
        return JSONResponse({
            "success": False,
            "text": "Our AI engines are currently experiencing high demand. Please try again in a few moments.",
            "provider": "none",
            "model_used": "none",
            "tokens_used": 0,
            "latency_ms": 0
        })

@app.get("/health")
async def health():
    return {"status": "operational", "engine": "axelr-cloud-orchestrator", "version": "4.2.1"}

@app.get("/api/route")
async def route_get():
    return {"message": "POST to /api/route for AI processing"}

if __name__ == "__main__":
    import uvicorn
    import asyncio
    port = int(os.getenv("PORT", 5001))
    uvicorn.run(app, host="0.0.0.0", port=port)