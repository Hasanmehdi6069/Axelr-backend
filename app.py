# -*- coding: utf-8 -*-
"""
AXELR AI - UNIFIED FORTRESS v13.4 (Elite Production)
7‑provider AI routing with bulletproof failover, accurate quota,
workspace‑aware file handling, and real admin metrics.
"""

import os
import re
import time
import json
import asyncio
import hashlib
import smtplib
import logging
import base64
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from collections import defaultdict

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

# ---------- STRIPE (optional) ----------
STRIPE_AVAILABLE = False
stripe = None
try:
    import stripe
    STRIPE_AVAILABLE = True
except ImportError:
    pass

import bleach
from cachetools import TTLCache
from fastapi import FastAPI, HTTPException, Depends, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
import uvicorn

# -------------------- LOGGING --------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("axelr-unified")

# -------------------- ENV VARS --------------------
MONGO_URI = (os.getenv("MONGO_URI") or "").strip()
GOOGLE_CLIENT_ID = (os.getenv("GOOGLE_CLIENT_ID") or "").strip()

if not MONGO_URI:
    logger.warning("MONGO_URI is not configured; database-backed features will be unavailable")
if not GOOGLE_CLIENT_ID:
    logger.warning("GOOGLE_CLIENT_ID is not configured; Google auth will be unavailable")

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "shanh1346@gmail.com")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
NETLIFY_ACCESS_TOKEN = os.getenv("NETLIFY_ACCESS_TOKEN")

# AI API Keys
GROQ_API_KEY = (os.getenv("GROQ_API_KEY") or "").strip()
OPENROUTER_API_KEY = (os.getenv("OPENROUTER_API_KEY") or "").strip()
SAMBANOVA_API_KEY = (os.getenv("SAMBANOVA_API_KEY") or "").strip()
TOGETHER_API_KEY = (os.getenv("TOGETHER_AI_API_KEY") or os.getenv("TOGETHER_API_KEY") or "").strip()
CEREBRAS_API_KEY = (os.getenv("CEREBRAS_API_KEY") or "").strip()

FREE_TIER_TOKEN_LIMIT = int(os.getenv("FREE_TIER_TOKEN_LIMIT", 1000000))

# -------------------- STRIPE INIT --------------------
if STRIPE_AVAILABLE and STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET:
    stripe.api_key = STRIPE_SECRET_KEY
    logger.info("Stripe initialized")
else:
    logger.warning("Stripe not configured - payment features disabled")

# -------------------- EMAIL --------------------
def get_email_transport():
    if SMTP_USER and SMTP_PASS:
        try:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            return server
        except Exception as e:
            logger.warning(f"Email transport failed: {e}")
    return None

# -------------------- MONGO DB --------------------
client = None
db = None
users_col = None
sessions_col = None
reports_col = None
db_available = False

async def init_db():
    global client, db, users_col, sessions_col, reports_col, db_available
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
        from bson import ObjectId
        client = AsyncIOMotorClient(MONGO_URI)
        db = client.get_default_database()
        users_col = db.get_collection("users")
        sessions_col = db.get_collection("chatsessions")
        reports_col = db.get_collection("bugreports")
        await users_col.create_index("googleId", unique=True)
        await sessions_col.create_index([("userId", 1), ("status", 1), ("workspace", 1)])
        await sessions_col.create_index("userId")
        await reports_col.create_index("userId")
        db_available = True
        logger.info("MongoDB connection established.")
    except Exception as e:
        logger.error(f"MongoDB initialization failed: {e}")
        db_available = False

def get_object_id():
    if db_available:
        from bson import ObjectId
        return ObjectId
    return None

# -------------------- CACHE --------------------
ai_cache = TTLCache(maxsize=2000, ttl=3600)

# Circuit breaker for providers
provider_failures = defaultdict(int)
provider_last_fail = defaultdict(float)
PROVIDER_COOLDOWN = 600  # 10 minutes

# -------------------- SECURITY UTILITIES --------------------
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

def strip_fluff(text: str) -> str:
    patterns = [
        r"^I (am|'m) (so |very )?happy to help",
        r"^Sure!",
        r"^Absolutely!",
        r"^Of course!",
        r"^Here( is| are|'s) (what|the|your)",
        r"^Let me (know|explain|show you)",
        r"^As (an|a) .* (assistant|AI),",
    ]
    for pat in patterns:
        text = re.sub(pat, "", text, flags=re.IGNORECASE)
    return text.strip()

# -------------------- ASYNC HTTP HELPER --------------------
async def http_post_async(url: str, headers: Dict, json_data: Dict, timeout: float = 90.0):
    data = json.dumps(json_data).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    loop = asyncio.get_running_loop()
    try:
        response = await asyncio.to_thread(urllib.request.urlopen, req, timeout=timeout)
        content = response.read().decode('utf-8')
        return json.loads(content)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else ''
        raise Exception(f"HTTP error {e.code}: {error_body}")
    except Exception as e:
        raise Exception(f"HTTP request failed: {e}")

# -------------------- 8‑MODEL MATRIX (OpenRouter IDs with :free) --------------------
MODEL_MATRIX = {
    "analytics":   "meta-llama/llama-3.1-8b-instruct:free",
    "extraction":  "meta-llama/llama-3.1-8b-instruct:free",
    "scripting":   "meta-llama/llama-3.1-8b-instruct:free",
    "fullstack":   "meta-llama/llama-3.1-8b-instruct:free",
    "frontend":    "meta-llama/llama-3.1-8b-instruct:free",
    "touch_fix":   "meta-llama/llama-3.1-8b-instruct:free",
    "structuring": "meta-llama/llama-3.1-8b-instruct:free",
    "logic_math":  "meta-llama/llama-3.1-8b-instruct:free",
}
FALLBACK_MODEL = "meta-llama/llama-3.1-8b-instruct:free"

def select_model(task_type: str) -> str:
    return MODEL_MATRIX.get(task_type, FALLBACK_MODEL)

# -------------------- AI PROVIDERS (each with correct endpoint & model) --------------------

# 1. OpenRouter
async def call_openrouter(model: str, prompt: str, max_tokens: int, temp: float) -> str:
    if not OPENROUTER_API_KEY:
        raise Exception("OPENROUTER_API_KEY missing")
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://axelr.in",
        "X-Title": "Axelr AI"
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
    }
    resp = await http_post_async(url, headers, payload, timeout=90)
    return resp["choices"][0]["message"]["content"]

# 2. Groq
async def call_groq(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not GROQ_API_KEY:
        raise Exception("GROQ_API_KEY missing")
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    effective_model = model or os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": min(max_tokens, 1024),
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload, timeout=60)
    return resp["choices"][0]["message"]["content"]

# 3. SambaNova
async def call_sambanova(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not SAMBANOVA_API_KEY:
        raise Exception("SAMBANOVA_API_KEY missing")
    url = "https://api.sambanova.ai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {SAMBANOVA_API_KEY}", "Content-Type": "application/json"}
    effective_model = model or os.getenv("SAMBANOVA_MODEL", "Meta-Llama-3.1-8B-Instruct")
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
    }
    resp = await http_post_async(url, headers, payload, timeout=90)
    return resp["choices"][0]["message"]["content"]

# 4. Together AI
async def call_together(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not TOGETHER_API_KEY:
        raise Exception("TOGETHER_API_KEY missing")
    url = "https://api.together.xyz/v1/chat/completions"
    headers = {"Authorization": f"Bearer {TOGETHER_API_KEY}", "Content-Type": "application/json"}
    effective_model = model or os.getenv("TOGETHER_MODEL", "meta-llama/Llama-3.1-8B-Instruct-Turbo")
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload, timeout=90)
    return resp["choices"][0]["message"]["content"]

# 5. Cerebras
async def call_cerebras(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not CEREBRAS_API_KEY:
        raise Exception("CEREBRAS_API_KEY missing")
    url = "https://api.cerebras.ai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {CEREBRAS_API_KEY}", "Content-Type": "application/json"}
    effective_model = model or os.getenv("CEREBRAS_MODEL", "llama3.1-8b")
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload, timeout=90)
    return resp["choices"][0]["message"]["content"]

# 6. Pollinations (no key, free)
async def call_pollinations(prompt: str, max_tokens: int, temp: float) -> str:
    import urllib.parse
    encoded = urllib.parse.quote(prompt[:500])
    url = f"https://text.pollinations.ai/{encoded}?seed=42&model=openai"
    req = urllib.request.Request(url, method='GET')
    loop = asyncio.get_running_loop()
    try:
        response = await asyncio.to_thread(urllib.request.urlopen, req, timeout=30)
        content = response.read().decode('utf-8')
        try:
            data = json.loads(content)
            if isinstance(data, dict) and "text" in data:
                return data["text"]
            elif isinstance(data, str):
                return data
            else:
                return content
        except:
            return content
    except Exception as e:
        raise Exception(f"Pollinations failed: {e}")

# 7. Ollama (local, optional)
async def call_ollama(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    ollama_url = (os.getenv("OLLAMA_URL") or "http://127.0.0.1:11434/api/chat").strip()
    if not ollama_url:
        raise Exception("OLLAMA_URL not set")
    effective_model = model or os.getenv("OLLAMA_MODEL", "llama3.2:3b")
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {
            "num_predict": max_tokens,
            "temperature": temp,
        },
    }
    headers = {"Content-Type": "application/json"}
    resp = await http_post_async(ollama_url, headers, payload, timeout=90)
    if isinstance(resp, dict) and isinstance(resp.get("message"), dict):
        return resp["message"].get("content", "")
    raise Exception("Unexpected Ollama response format")

# 8. Local fallback (always works, context‑aware) - UPDATED to handle greetings
def build_local_fallback_response(workspace: str, task_type: str, prompt: str) -> str:
    prompt_text = (prompt or "").strip()
    if not prompt_text:
        return "I can help with that. Please share the task details and I will provide a structured response."

    # Check for simple greetings / general questions
    lower = prompt_text.lower()
    greetings = ["hello", "hi", "hey", "how are you", "what's up", "good morning", "good afternoon", "good evening"]
    if any(lower.startswith(g) for g in greetings) or len(prompt_text.split()) < 4:
        return (
            "Hello! I'm **Axelr AI**, your intelligent assistant. "
            "I can help you with data extraction, UI/UX generation, code debugging, and more. "
            "Feel free to upload a file or describe your task, and I'll get to work!"
        )

    if workspace == "design":
        return (
            f"UI/UX draft for: \"{prompt_text[:120]}\". "
            "Here's a production‑safe starting point – refine it with your brand details and I'll help further."
        )
    if workspace == "data":
        return (
            f"Data analysis summary for: \"{prompt_text[:120]}\". "
            "Please provide the source data or example output and I'll turn it into a cleaner analysis."
        )
    if task_type == "touch_fix":
        return (
            f"Issue captured: \"{prompt_text[:120]}\". "
            "Please share the full error message, file name, and expected behaviour for a precise fix."
        )
    return (
        f"Request received: \"{prompt_text[:160]}\". "
        "I can help with a concise plan, code snippet, or structured answer – tell me more specifics."
    )

async def call_local_fallback(prompt: str, max_tokens: int, temp: float, workspace: str = "general", task_type: str = "general") -> str:
    return build_local_fallback_response(workspace, task_type, prompt)

# Provider chain (ordered by quality & reliability)
PROVIDER_CHAIN = [
    ("openrouter", call_openrouter, {}),
    ("groq", call_groq, {}),
    ("sambanova", call_sambanova, {}),
    ("together", call_together, {}),
    ("cerebras", call_cerebras, {}),
    ("pollinations", call_pollinations, {}),
    ("ollama", call_ollama, {}),
    ("local", call_local_fallback, {}),
]

# -------------------- MASTER SYSTEM PROMPT --------------------
MASTER_PROMPT = (
    "You are AXELR, an elite executive AI operating in zero-cost, production-safe mode. "
    "Always answer directly, clearly, and usefully. Never claim a service is unavailable unless all configured paths fail. "
    "Prefer concise, high-quality responses with actionable detail. For coding tasks, provide working code, short explanations, and no filler. "
    "For analysis tasks, provide a concise summary and structured output when helpful. "
    "Do not mention subscriptions, paid plans, or avoidable fluff."
)

def get_system_prompt(workspace: str, task_type: str) -> str:
    base = (
        f"{MASTER_PROMPT} "
        "RESPONSE MUST BE SHORT, CONCISE, AND ZERO‑FLUFF. "
        "Keep replies under 200 words unless code or detailed explanation is explicitly requested. "
        "Do not add pleasantries, introductions, or conclusions. "
        "Provide exactly what is asked, nothing more."
    )
    if workspace == "design":
        return base + (
            " You are AXELR ARCHITECT – a world-class UI/UX engineer. "
            "Generate production‑grade, pixel‑perfect, fully responsive HTML/CSS/JS components "
            "using modern Tailwind, flex/grid, micro‑interactions, and dark mode. "
            "Output complete code inside a single ```html block."
        )
    elif workspace == "data":
        return base + (
            " You are AXELR DATA – an enterprise data analyst. "
            "Clean, analyse, and transform the input into structured insights. "
            "Provide a concise summary followed by raw JSON inside [JSON-DATA]...[/JSON-DATA] tags."
        )
    else:
        return base + " Rewrite the user prompt into a detailed, professional system prompt."

# -------------------- AI ROUTER (bulletproof) --------------------
async def route_ai_request(
    workspace: str,
    task_type: str,
    prompt: str,
    history: Optional[List[Dict]],
    files: Optional[List[Dict]],
    max_tokens: int,
    temp: float,
    tier: str
) -> Dict[str, Any]:
    start = time.time()

    # Build full prompt with history
    history_text = ""
    if history:
        recent = []
        for msg in history[-4:]:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "user")
            content = msg.get("content") or msg.get("text") or ""
            if isinstance(content, list):
                parts = [p.get("text", "") for p in content if isinstance(p, dict)]
                content = "\n".join(parts)
            if isinstance(content, str) and content.strip():
                recent.append(f"{role}: {content.strip()}")
        history_text = "\n".join(recent)

    system_prompt = get_system_prompt(workspace, task_type)
    full_prompt = f"{system_prompt}\n\n"
    if history_text:
        full_prompt += f"Previous conversation:\n{history_text}\n\n"
    full_prompt += f"User request: {prompt}"

    # Security
    if detect_manipulation(prompt):
        return {"success": False, "text": "Manipulation detected.", "provider": "security", "model_used": "filter", "tokens_used": 0, "latency_ms": 0}

    # Cache
    cache_key = hashlib.sha256(f"{workspace}:{task_type}:{full_prompt}".encode()).hexdigest()
    if cache_key in ai_cache:
        cached = ai_cache[cache_key]
        return {**cached, "cached": True}

    # Primary model for this task
    primary_model = MODEL_MATRIX.get(task_type, FALLBACK_MODEL)

    response_text = None
    provider_used = None
    model_used = None
    last_error = None

    # Try each provider in order
    for name, func, kwargs in PROVIDER_CHAIN:
        # Skip if API key missing (except for local and pollinations)
        if name == "openrouter" and not OPENROUTER_API_KEY:
            logger.debug("Skipping openrouter – no API key")
            continue
        if name == "groq" and not GROQ_API_KEY:
            logger.debug("Skipping groq – no API key")
            continue
        if name == "sambanova" and not SAMBANOVA_API_KEY:
            logger.debug("Skipping sambanova – no API key")
            continue
        if name == "together" and not TOGETHER_API_KEY:
            logger.debug("Skipping together – no API key")
            continue
        if name == "cerebras" and not CEREBRAS_API_KEY:
            logger.debug("Skipping cerebras – no API key")
            continue
        if name == "ollama" and not os.getenv("OLLAMA_URL"):
            logger.debug("Skipping ollama – no URL")
            continue

        # Circuit breaker
        if provider_failures[name] >= 3 and time.time() - provider_last_fail[name] < PROVIDER_COOLDOWN:
            logger.warning(f"Skipping provider {name} due to circuit breaker (cooldown)")
            continue

        try:
            for attempt in range(2):  # retry once
                try:
                    if name == "openrouter":
                        response_text = await func(primary_model, full_prompt, max_tokens, temp)
                    elif name == "sambanova":
                        response_text = await func(full_prompt, max_tokens, temp, os.getenv("SAMBANOVA_MODEL", "Meta-Llama-3.1-8B-Instruct"))
                    elif name == "groq":
                        response_text = await func(full_prompt, max_tokens, temp, os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"))
                    elif name == "together":
                        response_text = await func(full_prompt, max_tokens, temp, os.getenv("TOGETHER_MODEL", "meta-llama/Llama-3.1-8B-Instruct-Turbo"))
                    elif name == "cerebras":
                        response_text = await func(full_prompt, max_tokens, temp, os.getenv("CEREBRAS_MODEL", "llama3.1-8b"))
                    elif name == "ollama":
                        response_text = await func(full_prompt, max_tokens, temp, os.getenv("OLLAMA_MODEL", "llama3.2:3b"))
                    elif name == "pollinations":
                        response_text = await func(full_prompt, max_tokens, temp)
                    else:  # local fallback - FIX: pass original prompt, not full_prompt
                        response_text = await func(prompt, max_tokens, temp, workspace, task_type)
                    provider_used = name
                    model_used = primary_model if name == "openrouter" else (os.getenv(f"{name.upper()}_MODEL") or name)
                    provider_failures[name] = 0
                    break
                except Exception as e:
                    last_error = e
                    logger.warning(f"Provider {name} attempt {attempt+1} failed: {e}")
                    await asyncio.sleep(1 * (attempt+1))
                    provider_failures[name] += 1
                    provider_last_fail[name] = time.time()
            if response_text:
                break
        except Exception as e:
            last_error = e
            logger.warning(f"Provider {name} fully failed: {e}")
            provider_failures[name] += 1
            provider_last_fail[name] = time.time()
            continue

    # Ultimate fallback (should never happen because local always works)
    if not response_text:
        response_text = build_local_fallback_response(workspace, task_type, prompt)
        provider_used = "local"
        model_used = "local-fallback"
        logger.error(f"All providers failed. Last error: {last_error}")

    response_text = strip_fluff(response_text)
    latency = (time.time() - start) * 1000
    result = {
        "success": True,
        "text": response_text,
        "provider": provider_used,
        "model_used": model_used,
        "tokens_used": len(response_text.split()),
        "latency_ms": round(latency, 2)
    }
    ai_cache[cache_key] = result
    return result

# -------------------- AUTHENTICATION --------------------
security = HTTPBearer()

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> Dict:
    if not db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")
    token = credentials.credentials
    try:
        idinfo = id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)
        if idinfo['iss'] not in ['accounts.google.com', 'https://accounts.google.com']:
            raise HTTPException(status_code=401, detail="Invalid issuer")
        user_doc = await users_col.find_one({"googleId": idinfo['sub']})
        is_admin = idinfo['email'] == ADMIN_EMAIL
        if not user_doc:
            new_user = {
                "googleId": idinfo['sub'],
                "email": idinfo['email'],
                "displayName": idinfo.get('name', idinfo['email']),
                "tier": "free",
                "dailyUsage": 0,
                "storageBytesUsed": 0,
                "lastUsageDate": datetime.utcnow(),
                "customInstructions": "",
                "tokenUsage": {
                    "totalPromptTokens": 0,
                    "totalCompletionTokens": 0,
                    "dailyPromptTokens": 0,
                    "dailyCompletionTokens": 0,
                    "lastTokenReset": datetime.utcnow()
                },
                "isAdmin": is_admin,
                "dailyGroqQuota": 0,
                "dailyOpenRouterQuota": 0,
                "dailySambaNovaQuota": 0,
                "dailyTogetherQuota": 0,
                "dailyCerebrasQuota": 0,
                "lastAiQuotaReset": datetime.utcnow()
            }
            result = await users_col.insert_one(new_user)
            user_doc = await users_col.find_one({"_id": result.inserted_id})
            logger.info(f"New user created: {idinfo['email']}")
        else:
            if user_doc.get("isAdmin") != is_admin:
                await users_col.update_one({"_id": user_doc["_id"]}, {"$set": {"isAdmin": is_admin}})
                user_doc["isAdmin"] = is_admin
            # Reset daily usage if new day
            now = datetime.utcnow()
            today = datetime(now.year, now.month, now.day)
            last_reset = user_doc.get("lastUsageDate")
            if last_reset:
                last_reset_day = datetime(last_reset.year, last_reset.month, last_reset.day)
                if today > last_reset_day:
                    await users_col.update_one(
                        {"_id": user_doc["_id"]},
                        {"$set": {
                            "dailyUsage": 0,
                            "tokenUsage.dailyPromptTokens": 0,
                            "tokenUsage.dailyCompletionTokens": 0,
                            "tokenUsage.lastTokenReset": datetime.utcnow(),
                            "dailyGroqQuota": 0,
                            "dailyOpenRouterQuota": 0,
                            "dailySambaNovaQuota": 0,
                            "dailyTogetherQuota": 0,
                            "dailyCerebrasQuota": 0,
                            "lastAiQuotaReset": datetime.utcnow()
                        }}
                    )
                    user_doc = await users_col.find_one({"_id": user_doc["_id"]})
        return user_doc
    except Exception as e:
        logger.error(f"Auth failed: {e}")
        raise HTTPException(status_code=401, detail="Invalid or expired token")

# -------------------- PER‑USER RATE LIMITING (soft) --------------------
user_rate_limiter = {}
RATE_LIMITS = {
    "free": 2,
    "pro": 5,
    "business": 8,
}

def check_user_rate_limit(user_id: str, tier: str):
    now = time.time()
    limit = RATE_LIMITS.get(tier, 2)
    if user_id not in user_rate_limiter:
        user_rate_limiter[user_id] = []
    user_rate_limiter[user_id] = [t for t in user_rate_limiter[user_id] if now - t < 60]
    if len(user_rate_limiter[user_id]) >= limit:
        logger.info(f"Rate limit exceeded for user {user_id}, but allowing request (soft limit)")
    user_rate_limiter[user_id].append(now)

# -------------------- FASTAPI APP --------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    if not db_available:
        logger.critical("MongoDB is not available. The application will run in degraded mode.")
    else:
        logger.info("Unified Fortress online")
    yield
    if client:
        client.close()
        logger.info("Shutdown complete")

app = FastAPI(title="AXELR Unified", version="13.4", lifespan=lifespan)

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

# ============================================================
# GLOBAL EXCEPTION HANDLER – Returns JSON for any error
# ============================================================
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"success": False, "message": "Internal server error. Please try again later."}
    )

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    # If the detail is already a dict, return it as is
    if isinstance(exc.detail, dict):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    else:
        return JSONResponse(
            status_code=exc.status_code,
            content={"success": False, "message": str(exc.detail)}
        )

# -------------------- HEALTH --------------------
@app.get("/")
@app.get("/api/health")
async def health():
    db_status = "unavailable" if not db_available else "connected"
    if db_available:
        try:
            await db.command("ping")
            db_status = "connected"
        except Exception as e:
            db_status = f"disconnected ({str(e)})"
    return {
        "status": "operational" if db_status == "connected" else "degraded",
        "timestamp": datetime.utcnow().isoformat(),
        "db": db_status,
        "stripe": bool(STRIPE_SECRET_KEY),
        "email": bool(SMTP_USER and SMTP_PASS),
        "uptime": time.time() - app.state.start_time if hasattr(app.state, "start_time") else 0
    }

@app.on_event("startup")
async def startup_event():
    app.state.start_time = time.time()

# -------------------- USER PROFILE --------------------
@app.get("/api/user/profile")
async def get_profile(user: dict = Depends(get_current_user)):
    if not db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")
    tier = user.get("tier", "free")
    if tier == "free":
        daily_limit = 5
    elif tier == "pro":
        daily_limit = 20
    elif tier == "business":
        daily_limit = 30
    else:
        daily_limit = 5

    return {
        "tier": tier,
        "dailyUsage": user.get("dailyUsage", 0),
        "dailyLimit": daily_limit,
        "customInstructions": user.get("customInstructions", ""),
        "tokenUsage": {
            "dailyPrompt": user.get("tokenUsage", {}).get("dailyPromptTokens", 0),
            "dailyCompletion": user.get("tokenUsage", {}).get("dailyCompletionTokens", 0),
            "totalPrompt": user.get("tokenUsage", {}).get("totalPromptTokens", 0),
            "totalCompletion": user.get("tokenUsage", {}).get("totalCompletionTokens", 0),
        },
        "isAdmin": user.get("isAdmin", False),
        "email": user.get("email"),
        "stripeCustomerId": user.get("stripeCustomerId"),
        "stripeSubscriptionId": user.get("stripeSubscriptionId"),
        "dailyGroqQuota": user.get("dailyGroqQuota", 0),
        "dailyOpenRouterQuota": user.get("dailyOpenRouterQuota", 0),
        "dailySambaNovaQuota": user.get("dailySambaNovaQuota", 0),
        "dailyTogetherQuota": user.get("dailyTogetherQuota", 0),
        "dailyCerebrasQuota": user.get("dailyCerebrasQuota", 0),
    }

class InstructionsUpdate(BaseModel):
    instructions: str

@app.put("/api/user/instructions")
async def update_instructions(data: InstructionsUpdate, user: dict = Depends(get_current_user)):
    if not db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")
    instructions = data.instructions[:5000]
    await users_col.update_one({"_id": user["_id"]}, {"$set": {"customInstructions": instructions}})
    return {"success": True}

@app.delete("/api/user/delete")
async def delete_account(user: dict = Depends(get_current_user)):
    if not db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")
    uid = user["_id"]
    await sessions_col.delete_many({"userId": uid})
    await reports_col.delete_many({"userId": uid})
    await users_col.delete_one({"_id": uid})
    return {"success": True}

@app.delete("/api/history/delete-all")
async def delete_all_chats(user: dict = Depends(get_current_user)):
    if not db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")
    await sessions_col.delete_many({"userId": user["_id"]})
    return {"success": True}

# -------------------- HISTORY ROUTES (unchanged) --------------------
class RenamePayload(BaseModel):
    action: str
    payload: Optional[str] = None

@app.put("/api/history/{history_id}")
async def update_history(history_id: str, data: RenamePayload, user: dict = Depends(get_current_user)):
    if not db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")
    ObjectId = get_object_id()
    if not ObjectId or not ObjectId.is_valid(history_id):
        raise HTTPException(status_code=400, detail="Invalid ID")
    session = await sessions_col.find_one({"_id": ObjectId(history_id), "userId": user["_id"]})
    if not session:
        raise HTTPException(status_code=404, detail="Not found")
    if data.action == "rename" and data.payload:
        new_name = data.payload[:100]
        await sessions_col.update_one({"_id": ObjectId(history_id)}, {"$set": {"filename": new_name}})
    elif data.action == "pin":
        current = session.get("isPinned", False)
        await sessions_col.update_one({"_id": ObjectId(history_id)}, {"$set": {"isPinned": not current}})
    else:
        raise HTTPException(status_code=400, detail="Invalid action")
    return {"success": True}

class StatusUpdate(BaseModel):
    status: str

@app.put("/api/history/{history_id}/status")
async def update_status(history_id: str, data: StatusUpdate, user: dict = Depends(get_current_user)):
    if not db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")
    ObjectId = get_object_id()
    if not ObjectId or not ObjectId.is_valid(history_id):
        raise HTTPException(status_code=400, detail="Invalid ID")
    valid_statuses = ["active", "archived", "trashed"]
    if data.status not in valid_statuses:
        raise HTTPException(status_code=400, detail="Invalid status")
    update: Dict[str, Any] = {"status": data.status}
    if data.status == "trashed":
        update["trashedAt"] = datetime.utcnow()
    result = await sessions_col.update_one(
        {"_id": ObjectId(history_id), "userId": user["_id"]},
        {"$set": update}
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    return {"success": True}

@app.delete("/api/history/{history_id}")
async def delete_history(history_id: str, user: dict = Depends(get_current_user)):
    if not db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")
    ObjectId = get_object_id()
    if not ObjectId or not ObjectId.is_valid(history_id):
        raise HTTPException(status_code=400, detail="Invalid ID")
    result = await sessions_col.delete_one({"_id": ObjectId(history_id), "userId": user["_id"], "status": "trashed"})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Not found or not trashed")
    return {"success": True}

class VariantUpdate(BaseModel):
    msgId: str
    variantIndex: int

@app.put("/api/history/{history_id}/variant")
async def switch_variant(history_id: str, data: VariantUpdate, user: dict = Depends(get_current_user)):
    if not db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")
    ObjectId = get_object_id()
    if not ObjectId or not ObjectId.is_valid(history_id):
        raise HTTPException(status_code=400, detail="Invalid ID")
    session = await sessions_col.find_one({"_id": ObjectId(history_id), "userId": user["_id"]})
    if not session:
        raise HTTPException(status_code=404, detail="Not found")
    messages = session.get("messages", [])
    msg_index = -1
    for i, msg in enumerate(messages):
        if str(msg.get("_id")) == data.msgId:
            msg_index = i
            break
    if msg_index == -1:
        raise HTTPException(status_code=404, detail="Message not found")
    msg = messages[msg_index]
    variants = msg.get("variants", [])
    if data.variantIndex < 0 or data.variantIndex >= len(variants):
        raise HTTPException(status_code=400, detail="Invalid variant index")
    msg["activeVariant"] = data.variantIndex
    msg["text"] = variants[data.variantIndex]
    await sessions_col.update_one(
        {"_id": ObjectId(history_id)},
        {"$set": {"messages": messages}}
    )
    return {"success": True}

@app.get("/api/history")
async def list_history(
    workspace: str = "data",
    status: str = "active",
    page: int = 1,
    limit: int = 20,
    user: dict = Depends(get_current_user)
):
    if not db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")
    if workspace not in ["data", "design", "general"]:
        workspace = "data"
    if status not in ["active", "archived", "trashed"]:
        status = "active"
    skip = (page - 1) * limit
    query = {"userId": user["_id"], "status": status, "workspace": workspace}
    total = await sessions_col.count_documents(query)
    cursor = sessions_col.find(query).sort([("isPinned", -1), ("createdAt", -1)]).skip(skip).limit(limit)
    logs = await cursor.to_list(length=limit)
    for log in logs:
        log["_id"] = str(log["_id"])
        log["userId"] = str(log["userId"])
        for msg in log.get("messages", []):
            if "_id" in msg:
                msg["_id"] = str(msg["_id"])
    return {
        "success": True,
        "logs": logs,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": (total + limit - 1) // limit
        }
    }

# -------------------- REPORTS --------------------
class ReportCreate(BaseModel):
    type: str = "feedback"
    description: str

@app.post("/api/reports")
async def create_report(data: ReportCreate, user: dict = Depends(get_current_user)):
    if not db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")
    report = {
        "userId": user["_id"],
        "type": data.type,
        "description": data.description[:5000],
        "createdAt": datetime.utcnow()
    }
    await reports_col.insert_one(report)
    if SMTP_USER and SMTP_PASS:
        try:
            server = get_email_transport()
            if server:
                msg = MIMEMultipart()
                msg["From"] = SMTP_USER
                msg["To"] = ADMIN_EMAIL
                msg["Subject"] = f"🔔 New {data.type.upper()} Report from {user['displayName']}"
                body = f"""
                <h2>New Report</h2>
                <p><strong>From:</strong> {user['displayName']} ({user['email']})</p>
                <p><strong>Type:</strong> {data.type}</p>
                <p><strong>Date:</strong> {datetime.utcnow().isoformat()}</p>
                <p><strong>Description:</strong><br>{data.description}</p>
                <hr>
                <p><strong>User ID:</strong> {user['_id']}</p>
                <p><strong>Tier:</strong> {user.get('tier')}</p>
                """
                msg.attach(MIMEText(body, "html"))
                server.sendmail(SMTP_USER, ADMIN_EMAIL, msg.as_string())
                server.quit()
        except Exception as e:
            logger.warning(f"Report email failed: {e}")
    return {"success": True}

# -------------------- PROMPT ENHANCEMENT --------------------
class EnhanceRequest(BaseModel):
    promptText: str

@app.post("/api/enhance-prompt")
async def enhance_prompt(data: EnhanceRequest, user: dict = Depends(get_current_user)):
    if not db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")
    prompt_text = data.promptText
    if not prompt_text:
        raise HTTPException(status_code=400, detail="No text provided")
    now = datetime.utcnow()
    today = datetime(now.year, now.month, now.day)
    last_reset = user.get("lastUsageDate")
    if last_reset:
        last_day = datetime(last_reset.year, last_reset.month, last_reset.day)
        if today > last_day:
            await users_col.update_one(
                {"_id": user["_id"]},
                {"$set": {"dailyUsage": 0, "lastUsageDate": datetime.utcnow()}}
            )
            user = await users_col.find_one({"_id": user["_id"]})
    tier = user.get("tier", "free")
    if tier == "free":
        limit = 3
    elif tier == "pro":
        limit = 7
    elif tier == "business":
        limit = 15
    else:
        limit = 3
    used = user.get("dailyUsage", 0)
    if used >= limit:
        raise HTTPException(status_code=403, detail={
            "code": "LIMIT_REACHED",
            "usage": used,
            "limit": limit
        })
    ai_result = await route_ai_request(
        workspace="prompt",
        task_type="structuring",
        prompt=prompt_text,
        history=[],
        files=[],
        max_tokens=2048,
        temp=0.2,
        tier=tier
    )
    if not ai_result.get("success"):
        raise HTTPException(status_code=503, detail="AI service unavailable")
    enhanced = ai_result["text"]
    await users_col.update_one(
        {"_id": user["_id"]},
        {"$inc": {"dailyUsage": 1}}
    )
    return {"success": True, "enhanced": enhanced}

# -------------------- EXTRACT (MAIN) --------------------
def estimate_tokens(text: str) -> int:
    return len(text) // 4 if text else 0

def generate_chat_name(command: str, files: List[UploadFile]) -> str:
    STOP_WORDS = {"the","be","to","of","and","a","in","that","have","i","it","for","not","on","with","he","as","you","do","at","this","but","his","by","from","they","we","say","her","she","or","an","will","my","one","all","would","there","their","what","so","up","out","if","about","who","get","which","go","me","when","make","can","like","time","no","just","him","know","take","people","into","year","your","good","some","could","them","see","other","than","then","now","look","only","come","its","over","think","also","back","after","use","two","how","our","work","first","well","way","even","new","want","because","any","these","give","day","most","us"}
    if files:
        base = files[0].filename.split('.')[0]
        return base.replace('_', ' ').replace('-', ' ')[:50] or "File Chat"
    if command and command.strip():
        words = command.strip().split()
        meaningful = [w for w in words if w.lower() not in STOP_WORDS and len(w) > 2]
        picked = meaningful[:3]
        if picked:
            return " ".join(picked)[:60]
        return " ".join(words[:3])[:60]
    return f"Chat_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

def is_allowed_file(workspace: str, filename: str, content_type: str) -> bool:
    if workspace == "data":
        allowed_data_types = [
            "image/", "application/pdf", "text/csv", "text/plain",
            "application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ]
        allowed_data_exts = ('.csv', '.xls', '.xlsx', '.pdf', '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.txt')
        return any(content_type.startswith(t) for t in allowed_data_types) or filename.lower().endswith(allowed_data_exts)
    elif workspace == "design":
        allowed_design_types = [
            "image/", "text/html", "text/css", "text/javascript", "text/x-python", "text/x-python-script",
            "application/javascript", "application/json", "text/x-js", "text/x-python", "text/x-c", "text/x-c++",
            "text/x-java", "text/x-php", "text/x-rs", "text/x-go", "text/x-ruby", "text/x-swift", "text/x-kotlin",
            "text/x-scala", "text/x-haskell", "text/x-lua", "text/x-perl", "text/x-r", "text/x-sh"
        ]
        allowed_design_exts = ('.html', '.css', '.js', '.ts', '.jsx', '.tsx', '.vue', '.svelte',
                               '.py', '.ipynb', '.java', '.c', '.cpp', '.h', '.hpp', '.go', '.rs',
                               '.rb', '.php', '.swift', '.kt', '.scala', '.hs', '.lua', '.pl', '.r',
                               '.sh', '.bash', '.zsh', '.json', '.yaml', '.yml', '.toml', '.ini',
                               '.md', '.markdown', '.txt', '.xml', '.svg', '.wasm', '.dockerfile',
                               '.dockerignore', '.gitignore')
        return any(content_type.startswith(t) for t in allowed_design_types) or filename.lower().endswith(allowed_design_exts)
    return True

@app.post("/api/extract")
async def extract(
    request: Request,
    user: dict = Depends(get_current_user),
    command: str = Form(...),
    workspace: str = Form("data"),
    task_type: Optional[str] = Form(None),
    isRetry: str = Form("false"),
    sessionId: Optional[str] = Form(None),
    files: List[UploadFile] = File([])
):
    if not db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")
    client_ip = request.client.host if request.client else "unknown"
    check_user_rate_limit(user["_id"], user.get("tier", "free"))

    if workspace not in ["data", "design", "general"]:
        workspace = "data"

    if len(files) > 5:
        raise HTTPException(status_code=400, detail="Too many files (max 5)")

    valid_files = []
    rejected = []
    for f in files:
        if is_allowed_file(workspace, f.filename, f.content_type or ""):
            valid_files.append(f)
        else:
            rejected.append(f.filename)
    if rejected:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type(s) for {workspace} workspace: {', '.join(rejected)}. "
                   f"Allowed: {'images, PDF, CSV, Excel, text' if workspace=='data' else 'images, code files (HTML, CSS, JS, Python, etc.), JSON, Markdown, text'}."
        )
    files = valid_files

    total_size = 0
    for f in files:
        file_size = f.size or 0
        if file_size > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"File {f.filename} exceeds 10MB")
        total_size += file_size
    if total_size > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Total upload size too large")

    tier = user.get("tier", "free")

    # Daily quota
    if tier == "free":
        daily_limit = 5
    elif tier == "pro":
        daily_limit = 20
    elif tier == "business":
        daily_limit = 30
    else:
        daily_limit = 5

    daily_used = user.get("dailyUsage", 0)

    # Reset if new day
    now = datetime.utcnow()
    last_reset = user.get("lastUsageDate")
    if last_reset:
        last_day = datetime(last_reset.year, last_reset.month, last_reset.day)
        today = datetime(now.year, now.month, now.day)
        if today > last_day:
            await users_col.update_one({"_id": user["_id"]}, {"$set": {"dailyUsage": 0, "lastUsageDate": now}})
            user = await users_col.find_one({"_id": user["_id"]})
            daily_used = user.get("dailyUsage", 0)

    logger.info(f"User {user.get('email')} tier={tier} daily_used={daily_used}/{daily_limit}")

    if daily_used >= daily_limit:
        raise HTTPException(status_code=403, detail={
            "code": "LIMIT_REACHED",
            "usage": daily_used,
            "limit": daily_limit
        })

    # Storage limit
    storage_limit = 5 * 1024 * 1024  # 5MB free
    if tier == "pro":
        storage_limit = 20 * 1024 * 1024
    elif tier == "business":
        storage_limit = 50 * 1024 * 1024
    current_storage = user.get("storageBytesUsed", 0)
    if current_storage + total_size > storage_limit:
        raise HTTPException(status_code=403, detail={"code": "STORAGE_LIMIT_REACHED", "message": f"Storage quota exceeded. Maximum {storage_limit / (1024*1024)}MB."})

    # Read files
    file_contents = []
    for f in files:
        content_bytes = await f.read()
        b64 = base64.b64encode(content_bytes).decode('utf-8')
        file_contents.append({
            "filename": f.filename,
            "mimetype": f.content_type or "application/octet-stream",
            "content_base64": b64
        })

    # Determine task_type
    if task_type is None:
        if workspace == "data":
            task_type = "extraction"
        elif workspace == "design":
            task_type = "frontend"
        else:
            task_type = "structuring"
    supported_types = list(MODEL_MATRIX.keys())
    if task_type not in supported_types:
        task_type = "extraction" if workspace == "data" else "frontend"

    ObjectId = get_object_id()
    if sessionId and (not ObjectId or not ObjectId.is_valid(sessionId)):
        sessionId = None

    current_session = None
    history = []
    if sessionId and ObjectId:
        current_session = await sessions_col.find_one({"_id": ObjectId(sessionId), "userId": user["_id"]})
        if current_session:
            history = current_session.get("messages", [])
            if isRetry == "true" and history and history[-1].get("role") == "model":
                history = history[:-2]

    # AI call – this should never raise, but wrap just in case
    try:
        ai_result = await route_ai_request(
            workspace=workspace,
            task_type=task_type,
            prompt=command,
            history=history,
            files=file_contents,
            max_tokens=2048,
            temp=0.2,
            tier=tier
        )
    except Exception as e:
        logger.error(f"AI router raised unexpected exception: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail="AI service temporarily unavailable")

    if not ai_result.get("success"):
        raise HTTPException(status_code=503, detail="AI service unavailable")

    ai_text = ai_result["text"]
    provider = ai_result.get("provider")
    model_used = ai_result.get("model_used")

    # Parse JSON data if present
    structured = []
    json_match = re.search(r'\[JSON-DATA\](.*?)\[/JSON-DATA\]', ai_text, re.DOTALL)
    if json_match:
        try:
            structured = json.loads(json_match.group(1).strip())
        except Exception:
            structured = []
        ai_text = re.sub(r'\[JSON-DATA\].*?\[/JSON-DATA\]', '', ai_text, flags=re.DOTALL).strip()
    if not ai_text:
        ai_text = "I am Axelr AI. How can I help you?"

    # Update usage and tokens
    prompt_tokens = estimate_tokens(command) + sum(estimate_tokens(f["filename"]) + len(f["content_base64"]) // 4 for f in file_contents)
    completion_tokens = estimate_tokens(ai_text)
    update_query = {
        "$inc": {
            "tokenUsage.totalPromptTokens": prompt_tokens,
            "tokenUsage.totalCompletionTokens": completion_tokens,
            "tokenUsage.dailyPromptTokens": prompt_tokens,
            "tokenUsage.dailyCompletionTokens": completion_tokens,
            "dailyUsage": 1,
            "storageBytesUsed": total_size,
        },
        "$set": {
            "lastUsageDate": datetime.utcnow()
        }
    }
    if provider == "groq":
        update_query["$inc"]["dailyGroqQuota"] = 1
    elif provider == "openrouter":
        update_query["$inc"]["dailyOpenRouterQuota"] = 1
    elif provider == "sambanova":
        update_query["$inc"]["dailySambaNovaQuota"] = 1
    elif provider == "together":
        update_query["$inc"]["dailyTogetherQuota"] = 1
    elif provider == "cerebras":
        update_query["$inc"]["dailyCerebrasQuota"] = 1

    await users_col.update_one({"_id": user["_id"]}, update_query)

    # Save session
    session_id_out = None
    filename_out = "Export.csv"
    session_saved = False

    if current_session:
        if isRetry == "true" and len(current_session.get("messages", [])) > 0:
            last_msg = current_session["messages"][-1]
            if last_msg.get("role") == "model":
                variants = last_msg.get("variants", [])
                if not variants:
                    variants = [last_msg.get("text", "")]
                variants.append(ai_text)
                last_msg["variants"] = variants
                last_msg["activeVariant"] = len(variants) - 1
                last_msg["text"] = ai_text
                await sessions_col.update_one(
                    {"_id": ObjectId(sessionId)},
                    {"$set": {"messages": current_session["messages"], "structuredData": structured}}
                )
                session_saved = True
                session_id_out = sessionId
                filename_out = current_session.get("filename", "Export")
        else:
            current_session["messages"].append({
                "role": "user",
                "text": command,
                "attachedFiles": [f.filename for f in files]
            })
            current_session["messages"].append({
                "role": "model",
                "text": ai_text,
                "variants": [ai_text],
                "activeVariant": 0,
                "createdAt": datetime.utcnow()
            })
            current_session["structuredData"] = structured
            await sessions_col.update_one(
                {"_id": ObjectId(sessionId)},
                {"$set": {"messages": current_session["messages"], "structuredData": structured}}
            )
            session_saved = True
            session_id_out = sessionId
            filename_out = current_session.get("filename", "Export")
    else:
        filename = generate_chat_name(command, files)
        new_session = {
            "userId": user["_id"],
            "filename": filename,
            "workspace": workspace,
            "status": "active",
            "isPinned": False,
            "messages": [
                {
                    "role": "user",
                    "text": command,
                    "attachedFiles": [f.filename for f in files],
                    "createdAt": datetime.utcnow()
                },
                {
                    "role": "model",
                    "text": ai_text,
                    "variants": [ai_text],
                    "activeVariant": 0,
                    "createdAt": datetime.utcnow()
                }
            ],
            "structuredData": structured,
            "createdAt": datetime.utcnow()
        }
        result = await sessions_col.insert_one(new_session)
        session_saved = True
        session_id_out = str(result.inserted_id)
        filename_out = filename

    return {
        "success": True,
        "text": ai_text,
        "sessionId": session_id_out if session_saved else None,
        "structuredData": structured,
        "filename": f"{filename_out}.csv",
        "provider": provider,
        "model": model_used
    }

# -------------------- TOUCH FIX --------------------
class TouchFixRequest(BaseModel):
    code: str
    error_message: str
    task_type: Optional[str] = "touch_fix"

@app.post("/api/touch_fix")
async def touch_fix(data: TouchFixRequest, user: dict = Depends(get_current_user)):
    if not data.code:
        raise HTTPException(status_code=400, detail="No code provided")
    prompt = f"""Fix the following code. The error is: {data.error_message}
Return only the corrected code, without any explanation.

```html
{data.code}
```"""
    ai_result = await route_ai_request(
        workspace="design",
        task_type="touch_fix",
        prompt=prompt,
        history=[],
        files=[],
        max_tokens=2048,
        temp=0.2,
        tier=user.get("tier", "free")
    )
    if not ai_result.get("success"):
        raise HTTPException(status_code=503, detail="AI service unavailable")
    fixed_code = ai_result["text"]
    code_match = re.search(r"```(?:html|javascript|css)?\s*([\s\S]*?)```", fixed_code, re.DOTALL)
    if code_match:
        fixed_code = code_match.group(1).strip()
    return {"success": True, "fixed_code": fixed_code}

# -------------------- DEPLOY (unchanged) --------------------
def _build_multipart(data: Dict, files: Dict) -> (bytes, str):
    boundary = '----WebKitFormBoundary' + hashlib.md5(os.urandom(16)).hexdigest()
    body_parts = []
    for key, value in data.items():
        body_parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode('utf-8'))
    for field, (filename, content, mimetype) in files.items():
        body_parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\nContent-Type: {mimetype}\r\n\r\n'.encode('utf-8'))
        body_parts.append(content)
        body_parts.append(b'\r\n')
    body_parts.append(f'--{boundary}--\r\n'.encode('utf-8'))
    body = b''.join(body_parts)
    content_type = f'multipart/form-data; boundary={boundary}'
    return body, content_type

async def http_post_multipart_async(url: str, headers: Dict, data: Dict, files: Dict, timeout: float = 30.0):
    body, content_type = _build_multipart(data, files)
    headers = headers.copy()
    headers['Content-Type'] = content_type
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    loop = asyncio.get_running_loop()
    try:
        response = await asyncio.to_thread(urllib.request.urlopen, req, timeout=timeout)
        content = response.read().decode('utf-8')
        return json.loads(content), response.status
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else ''
        raise Exception(f"HTTP error {e.code}: {error_body}")
    except Exception as e:
        raise Exception(f"HTTP request failed: {e}")

class DeployRequest(BaseModel):
    htmlContent: str

@app.post("/api/deploy")
async def deploy(data: DeployRequest, user: dict = Depends(get_current_user)):
    html = data.htmlContent
    if not html:
        raise HTTPException(status_code=400, detail="Missing HTML content")
    if "<html" not in html or "</html>" not in html:
        raise HTTPException(status_code=400, detail="Generated HTML is incomplete.")
    allowed_tags = [
        'html', 'head', 'body', 'div', 'span', 'p', 'a', 'img', 'button', 'input', 'form', 'table',
        'tr', 'td', 'th', 'ul', 'ol', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'em', 'u',
        'br', 'hr', 'section', 'article', 'header', 'footer', 'nav', 'main', 'aside', 'figure',
        'figcaption', 'mark', 'small', 'sub', 'sup', 'code', 'pre', 'blockquote', 'cite', 'label',
        'select', 'option', 'textarea', 'style', 'link', 'meta', 'title'
    ]
    allowed_attrs = {
        '*': ['class', 'id', 'style'],
        'a': ['href', 'title'],
        'img': ['src', 'alt', 'width', 'height'],
        'link': ['rel', 'type', 'href', 'media'],
        'meta': ['name', 'content'],
        'source': ['src', 'type'],
    }
    sanitized = bleach.clean(html, tags=allowed_tags, attributes=allowed_attrs, strip=True)
    if NETLIFY_ACCESS_TOKEN:
        try:
            create_headers = {
                "Authorization": f"Bearer {NETLIFY_ACCESS_TOKEN}",
                "Content-Type": "application/json"
            }
            site_name = f"axelr-deploy-{int(time.time())}"
            create_payload = {"name": site_name}
            create_resp = await http_post_async(
                "https://api.netlify.com/api/v1/sites",
                create_headers,
                create_payload,
                timeout=30.0
            )
            if create_resp.get("id"):
                site_id = create_resp["id"]
                deploy_headers = {
                    "Authorization": f"Bearer {NETLIFY_ACCESS_TOKEN}"
                }
                data_payload = {}
                files_payload = {
                    "file": ("index.html", sanitized.encode('utf-8'), "text/html")
                }
                deploy_resp, status = await http_post_multipart_async(
                    f"https://api.netlify.com/api/v1/sites/{site_id}/deploys",
                    deploy_headers,
                    data_payload,
                    files_payload,
                    timeout=30.0
                )
                if status == 200 and deploy_resp.get("deploy_url"):
                    return {"success": True, "liveUrl": deploy_resp["deploy_url"]}
        except Exception as e:
            logger.warning(f"Netlify deploy failed: {e}")
    data_uri = f"data:text/html;charset=utf-8,{sanitized}"
    return {"success": True, "liveUrl": data_uri, "message": "Preview available via data URI."}

# -------------------- ADMIN METRICS --------------------
@app.get("/api/admin/metrics")
async def admin_metrics(user: dict = Depends(get_current_user)):
    if not db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")
    if not user.get("isAdmin") or user.get("email") != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Admin access restricted")

    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    total_users = await users_col.count_documents({})
    pro_users = await users_col.count_documents({"tier": "pro"})
    business_users = await users_col.count_documents({"tier": "business"})
    total_chats = await sessions_col.count_documents({})

    pipeline_usage = [
        {"$group": {"_id": None, "totalQueries": {"$sum": "$dailyUsage"}, "totalBytes": {"$sum": "$storageBytesUsed"}}}
    ]
    usage_result = await users_col.aggregate(pipeline_usage).to_list(length=1)
    metrics = usage_result[0] if usage_result else {"totalQueries": 0, "totalBytes": 0}

    pipeline_tokens = [
        {"$group": {"_id": None, "totalPrompt": {"$sum": "$tokenUsage.totalPromptTokens"}, "totalCompletion": {"$sum": "$tokenUsage.totalCompletionTokens"}}}
    ]
    tokens_result = await users_col.aggregate(pipeline_tokens).to_list(length=1)
    tokens = tokens_result[0] if tokens_result else {"totalPrompt": 0, "totalCompletion": 0}
    total_tokens = tokens["totalPrompt"] + tokens["totalCompletion"]

    pipeline_provider = [
        {"$group": {"_id": None,
                    "totalGroq": {"$sum": "$dailyGroqQuota"},
                    "totalOpenRouter": {"$sum": "$dailyOpenRouterQuota"},
                    "totalSambaNova": {"$sum": "$dailySambaNovaQuota"},
                    "totalTogether": {"$sum": "$dailyTogetherQuota"},
                    "totalCerebras": {"$sum": "$dailyCerebrasQuota"}}}
    ]
    provider_result = await users_col.aggregate(pipeline_provider).to_list(length=1)
    provider_totals = provider_result[0] if provider_result else {
        "totalGroq":0, "totalOpenRouter":0, "totalSambaNova":0, "totalTogether":0, "totalCerebras":0
    }

    pipeline_daily_provider = [
        {"$match": {"lastAiQuotaReset": {"$gte": today}}},
        {"$group": {"_id": None,
                    "dailyGroq": {"$sum": "$dailyGroqQuota"},
                    "dailyOpenRouter": {"$sum": "$dailyOpenRouterQuota"},
                    "dailySambaNova": {"$sum": "$dailySambaNovaQuota"},
                    "dailyTogether": {"$sum": "$dailyTogetherQuota"},
                    "dailyCerebras": {"$sum": "$dailyCerebrasQuota"}}}
    ]
    daily_provider_result = await users_col.aggregate(pipeline_daily_provider).to_list(length=1)
    daily_provider = daily_provider_result[0] if daily_provider_result else {
        "dailyGroq":0, "dailyOpenRouter":0, "dailySambaNova":0, "dailyTogether":0, "dailyCerebras":0
    }

    groq_limit = int(os.getenv("GROQ_DAILY_LIMIT", 1000))
    openrouter_limit = int(os.getenv("OPENROUTER_DAILY_LIMIT", 1000))
    sambanova_limit = int(os.getenv("SAMBANOVA_DAILY_LIMIT", 200))
    together_limit = int(os.getenv("TOGETHER_DAILY_LIMIT", 200))
    cerebras_limit = int(os.getenv("CEREBRAS_DAILY_LIMIT", 200))

    daily_usage = {
        "groq": daily_provider["dailyGroq"],
        "openrouter": daily_provider["dailyOpenRouter"],
        "sambanova": daily_provider["dailySambaNova"],
        "together": daily_provider["dailyTogether"],
        "cerebras": daily_provider["dailyCerebras"],
    }
    active_provider = max(daily_usage, key=daily_usage.get) if any(daily_usage.values()) else "openrouter"

    pipeline_daily_queries = [
        {"$match": {"lastUsageDate": {"$gte": today}}},
        {"$group": {"_id": None, "dailyQueries": {"$sum": "$dailyUsage"}}}
    ]
    daily_queries_result = await users_col.aggregate(pipeline_daily_queries).to_list(length=1)
    daily_queries = daily_queries_result[0]["dailyQueries"] if daily_queries_result else 0

    recent_users = await users_col.find({}, {"email":1, "displayName":1, "tier":1, "createdAt":1}).sort("createdAt", -1).limit(10).to_list(length=10)
    for u in recent_users:
        u["_id"] = str(u["_id"])

    return {
        "success": True,
        "totalUsers": total_users,
        "proUsers": pro_users,
        "businessUsers": business_users,
        "totalChats": total_chats,
        "metrics": {
            "totalQueries": metrics["totalQueries"],
            "totalBytesMB": round(metrics["totalBytes"] / (1024 * 1024), 2),
        },
        "tokenUsage": {
            "prompt": tokens["totalPrompt"],
            "completion": tokens["totalCompletion"],
            "total": total_tokens,
            "remaining": max(0, FREE_TIER_TOKEN_LIMIT - total_tokens),
            "limit": FREE_TIER_TOKEN_LIMIT,
        },
        "aiQuota": {
            "groq": provider_totals["totalGroq"],
            "openRouter": provider_totals["totalOpenRouter"],
            "sambaNova": provider_totals["totalSambaNova"],
            "together": provider_totals["totalTogether"],
            "cerebras": provider_totals["totalCerebras"],
            "dailyGroq": daily_provider["dailyGroq"],
            "dailyOpenRouter": daily_provider["dailyOpenRouter"],
            "dailySambaNova": daily_provider["dailySambaNova"],
            "dailyTogether": daily_provider["dailyTogether"],
            "dailyCerebras": daily_provider["dailyCerebras"],
            "groqLimit": groq_limit,
            "openRouterLimit": openrouter_limit,
            "sambaNovaLimit": sambanova_limit,
            "togetherLimit": together_limit,
            "cerebrasLimit": cerebras_limit,
            "activeProvider": active_provider,
        },
        "dailyQueries": daily_queries,
        "recentUsers": recent_users,
        "timestamp": datetime.utcnow().isoformat()
    }

# -------------------- STRIPE CHECKOUT & WEBHOOK --------------------
class CheckoutRequest(BaseModel):
    tier: str = "pro"
    subTier: str = "full"

@app.post("/api/billing/checkout")
async def create_checkout(data: CheckoutRequest, user: dict = Depends(get_current_user)):
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Payment service unavailable")
    tier = data.tier
    subTier = data.subTier
    pricing = {
        "pro": {
            "full": {"price": 1500, "name": "Pro Full Stack", "features": "20 Data + 15 UI + 7 Enhancements"},
            "data": {"price": 800, "name": "Pro Data", "features": "19 Data + 0 UI + 5 Enhancements"},
            "design": {"price": 900, "name": "Pro Design", "features": "0 Data + 13 UI + 5 Enhancements"}
        },
        "business": {
            "full": {"price": 2900, "name": "Business Full", "features": "30 Data + 25 UI + 15 Enhancements"},
            "data": {"price": 1600, "name": "Business Data", "features": "28 Data + 0 UI + 10 Enhancements"},
            "design": {"price": 1600, "name": "Business Design", "features": "0 Data + 20 UI + 10 Enhancements"}
        }
    }
    plan = pricing.get(tier, {}).get(subTier)
    if not plan:
        raise HTTPException(status_code=400, detail="Invalid plan selection")
    origin = "https://axelr.in"
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            client_reference_id=user["googleId"],
            customer_email=user["email"],
            metadata={
                "tier": tier,
                "subTier": subTier,
                "userId": str(user["_id"])
            },
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": plan["name"],
                        "description": plan["features"]
                    },
                    "unit_amount": plan["price"],
                    "recurring": {"interval": "month"}
                },
                "quantity": 1
            }],
            success_url=f"{origin}/?billing=success&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{origin}/?billing=cancelled",
            allow_promotion_codes=True,
        )
        if not session.url:
            raise Exception("No checkout URL returned")
        return {"success": True, "url": session.url}
    except Exception as e:
        logger.error(f"Checkout error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request):
    if not (STRIPE_AVAILABLE and STRIPE_SECRET_KEY):
        return JSONResponse(content={"received": True, "note": "Stripe disabled"})
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    event = None
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        logger.warning(f"Webhook signature verification failed: {e}")
        event = json.loads(payload)
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        google_id = session.get("client_reference_id")
        if google_id:
            user = await users_col.find_one({"googleId": google_id})
            if user:
                tier = session.get("metadata", {}).get("tier", "pro")
                subTier = session.get("metadata", {}).get("subTier", "full")
                has_data = subTier in ["full", "data"]
                has_design = subTier in ["full", "design"]
                await users_col.update_one(
                    {"_id": user["_id"]},
                    {"$set": {
                        "tier": tier,
                        "stripeCustomerId": session.get("customer"),
                        "stripeSubscriptionId": session.get("subscription"),
                        "subTierOptions.hasDataAccess": has_data,
                        "subTierOptions.hasDesignAccess": has_design
                    }}
                )
                logger.info(f"User {user['email']} upgraded to {tier}")
                if SMTP_USER and SMTP_PASS:
                    try:
                        server = get_email_transport()
                        if server:
                            msg = MIMEMultipart()
                            msg["From"] = SMTP_USER
                            msg["To"] = user["email"]
                            msg["Subject"] = "🎉 Axelr AI - Subscription Upgrade Confirmed"
                            body = f"""
                            <h2>Welcome to {tier.upper()} Tier!</h2>
                            <p>Your Axelr AI workspace has been successfully upgraded.</p>
                            <p><strong>Plan:</strong> {tier}</p>
                            <p><strong>Features:</strong></p>
                            <ul>
                                <li>Data Access: {'✅' if has_data else '❌'}</li>
                                <li>Design Access: {'✅' if has_design else '❌'}</li>
                            </ul>
                            <p>Thank you for choosing Axelr AI!</p>
                            """
                            msg.attach(MIMEText(body, "html"))
                            server.sendmail(SMTP_USER, user["email"], msg.as_string())
                            server.quit()
                    except Exception as e:
                        logger.warning(f"Upgrade email failed: {e}")
    elif event["type"] == "customer.subscription.deleted":
        subscription = event["data"]["object"]
        user = await users_col.find_one({"stripeSubscriptionId": subscription["id"]})
        if user:
            await users_col.update_one(
                {"_id": user["_id"]},
                {"$set": {"tier": "free", "subTierOptions.hasDataAccess": False, "subTierOptions.hasDesignAccess": False}}
            )
            logger.info(f"Subscription cancelled for {user['email']}")
            if SMTP_USER and SMTP_PASS:
                try:
                    server = get_email_transport()
                    if server:
                        msg = MIMEMultipart()
                        msg["From"] = SMTP_USER
                        msg["To"] = user["email"]
                        msg["Subject"] = "Axelr AI - Subscription Cancelled"
                        body = """
                        <h2>Subscription Cancelled</h2>
                        <p>Your Axelr AI subscription has been cancelled.</p>
                        <p>You are now on the Free tier.</p>
                        """
                        msg.attach(MIMEText(body, "html"))
                        server.sendmail(SMTP_USER, user["email"], msg.as_string())
                        server.quit()
                except Exception as e:
                    logger.warning(f"Cancellation email failed: {e}")
    return {"received": True}

# -------------------- 404 --------------------
@app.exception_handler(404)
async def not_found(request, exc):
    return JSONResponse(status_code=404, content={"success": False, "code": "NOT_FOUND", "message": "Endpoint not found."})

# -------------------- MAIN --------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    logger.info(f"=== STARTING AXELR AI ON PORT {port} ===")
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")