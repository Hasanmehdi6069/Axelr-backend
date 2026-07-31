# -*- coding: utf-8 -*-
"""
AXELR AI - Unified Fortress (v9.0)
Single-file production backend merging:
- Node.js server logic (auth, DB, routes, billing, webhooks, email)
- Python orchestrator logic (AI routing, multi-model failover, caching)
Deploys as a single FastAPI container on port 8080.
"""

import os
import re
import time
import json
import asyncio
import hashlib
import smtplib
import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple
from contextlib import asynccontextmanager
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import httpx
import stripe
import bleach
from cachetools import TTLCache
from fastapi import FastAPI, HTTPException, Depends, Request, UploadFile, File, Form, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
import uvicorn

# -------------------- LOGGING --------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("axelr-unified")

# -------------------- ENV VARS --------------------
MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI environment variable is required")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
if not GOOGLE_CLIENT_ID:
    raise RuntimeError("GOOGLE_CLIENT_ID environment variable is required")
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "")  # not used anymore (internal)

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "shanh1346@gmail.com")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
VERCEL_TOKEN = os.getenv("VERCEL_TOKEN")
VERCEL_PROJECT_ID = os.getenv("VERCEL_PROJECT_ID")
NETLIFY_TOKEN = os.getenv("NETLIFY_TOKEN")
NETLIFY_SITE_ID = os.getenv("NETLIFY_SITE_ID")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
FREE_TIER_TOKEN_LIMIT = int(os.getenv("FREE_TIER_TOKEN_LIMIT", 1000000))

# -------------------- STRIPE INIT --------------------
stripe_client = None
if STRIPE_SECRET_KEY:
    stripe_client = stripe.Stripe(STRIPE_SECRET_KEY)
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
client = AsyncIOMotorClient(MONGO_URI)
db = client.get_default_database()
users_col = db.get_collection("users")
sessions_col = db.get_collection("chatsessions")
reports_col = db.get_collection("bugreports")

# Indexes
async def init_indexes():
    await users_col.create_index("googleId", unique=True)
    await sessions_col.create_index([("userId", 1), ("status", 1), ("workspace", 1)])
    await sessions_col.create_index("userId")
    await reports_col.create_index("userId")

# -------------------- CACHE --------------------
# SHA256 caching engine – cache AI responses per prompt+workspace
ai_cache = TTLCache(maxsize=1000, ttl=3600)  # 1 hour

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
    """Server-side fluff stripper: remove common filler phrases."""
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

# -------------------- AI ROUTER --------------------
async def call_groq(prompt: str, max_tokens: int, temp: float) -> str:
    if not GROQ_API_KEY:
        raise Exception("GROQ_API_KEY not configured")
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    # Use a reliable model
    model = "llama-3.1-70b-versatile"
    effective_max = min(max_tokens, 1024)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": effective_max,
        "temperature": temp,
        "stream": False
    }
    async with httpx.AsyncClient(timeout=90.0) as http_client:
        try:
            resp = await http_client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            error_body = e.response.text if e.response else "No response"
            logger.error(f"Groq API error {e.response.status_code}: {error_body}")
            try:
                detail = json.loads(error_body).get("error", {}).get("message", error_body)
            except:
                detail = error_body
            raise Exception(f"Groq API error {e.response.status_code}: {detail}")
        return resp.json()["choices"][0]["message"]["content"]

async def call_openrouter(model: str, prompt: str, max_tokens: int, temp: float) -> str:
    if not OPENROUTER_API_KEY:
        raise Exception("OPENROUTER_API_KEY not configured")
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
    async with httpx.AsyncClient(timeout=90.0) as http_client:
        try:
            resp = await http_client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            error_body = e.response.text if e.response else "No response"
            logger.error(f"OpenRouter API error {e.response.status_code}: {error_body}")
            try:
                detail = json.loads(error_body).get("error", {}).get("message", error_body)
            except:
                detail = error_body
            raise Exception(f"OpenRouter API error {e.response.status_code}: {detail}")
        return resp.json()["choices"][0]["message"]["content"]

async def call_with_retries(provider_func, *args, retries=3, delay=1.0, **kwargs):
    last_exception = None
    for attempt in range(retries):
        try:
            return await provider_func(*args, **kwargs)
        except Exception as e:
            last_exception = e
            logger.warning(f"Provider attempt {attempt+1} failed: {e}")
            if attempt < retries - 1:
                await asyncio.sleep(delay * (2 ** attempt))
            else:
                raise last_exception
    raise last_exception

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

async def route_ai_request(workspace: str, prompt: str, history: Optional[List[Dict]], files: Optional[List[Dict]], max_tokens: int, temp: float, tier: str) -> Dict[str, Any]:
    """Core AI routing – replaces the Python orchestrator's /api/route."""
    start = time.time()
    # Build full prompt
    history_text = ""
    if history:
        history_text = "\n".join([f"{m['role']}: {m['content']}" for m in history[-4:]])
    system_prompt = get_system_prompt(workspace)
    full_prompt = f"{system_prompt}\n\n"
    if history_text:
        full_prompt += f"Previous conversation:\n{history_text}\n\n"
    full_prompt += f"User request: {prompt}"

    # Check manipulation
    if detect_manipulation(prompt):
        return {
            "success": False,
            "text": "We have detected manipulative content in your request. Please adhere to our terms of service.",
            "provider": "security",
            "model_used": "security-filter",
            "tokens_used": 0,
            "latency_ms": 0
        }

    # Caching (optional)
    cache_key = hashlib.sha256(f"{workspace}:{full_prompt}".encode()).hexdigest()
    if cache_key in ai_cache:
        cached = ai_cache[cache_key]
        return {
            "success": True,
            "text": cached["text"],
            "provider": cached["provider"],
            "model_used": cached["model_used"],
            "tokens_used": cached.get("tokens_used", 0),
            "latency_ms": 0,
            "cached": True
        }

    response_text = None
    provider = None
    model_used = None

    # Choose provider based on workspace
    if workspace == "design":
        # Primary: Groq
        try:
            response_text = await call_with_retries(call_groq, full_prompt, max_tokens, temp, retries=2)
            provider = "groq"
            model_used = "llama-3.1-70b-versatile"
        except Exception as e:
            logger.warning(f"Groq design failed: {e}")
            # Fallback: OpenRouter coder
            try:
                response_text = await call_with_retries(
                    call_openrouter,
                    "qwen/qwen-2.5-coder-32b:free",
                    full_prompt,
                    max_tokens,
                    temp,
                    retries=2
                )
                provider = "openrouter-fallback"
                model_used = "qwen-2.5-coder-32b:free"
            except Exception as e2:
                logger.error(f"All design providers failed: {e2}")
                raise HTTPException(status_code=503, detail="All AI providers are currently unavailable. Please try again later.")
    elif workspace == "data":
        # Primary: OpenRouter deepseek
        try:
            model = "deepseek/deepseek-r1-distill-llama-70b:free"
            response_text = await call_with_retries(
                call_openrouter,
                model,
                full_prompt,
                max_tokens,
                temp,
                retries=2
            )
            provider = "openrouter"
            model_used = model
        except Exception as e:
            logger.warning(f"OpenRouter data failed: {e}")
            # Fallback: Groq
            try:
                response_text = await call_with_retries(call_groq, full_prompt, max_tokens, temp, retries=2)
                provider = "groq-fallback"
                model_used = "llama-3.1-70b-versatile"
            except Exception as e2:
                logger.error(f"All data providers failed: {e2}")
                raise HTTPException(status_code=503, detail="All AI providers are currently unavailable. Please try again later.")
    else:  # prompt enhancement
        try:
            model = "qwen/qwen-2.5-coder-32b:free"
            response_text = await call_with_retries(
                call_openrouter,
                model,
                f"You are an expert prompt engineer. Rewrite this user prompt into a detailed, professional system prompt:\n\n{prompt}",
                max_tokens,
                temp,
                retries=2
            )
            provider = "openrouter"
            model_used = model
        except Exception as e:
            logger.warning(f"OpenRouter prompt enhancement failed: {e}")
            response_text = f"Please provide a detailed response to: {prompt}"
            provider = "local-fallback"
            model_used = "rule-engine"

    # Strip fluff from response
    if response_text:
        response_text = strip_fluff(response_text)

    latency = (time.time() - start) * 1000
    result = {
        "success": True,
        "text": response_text,
        "provider": provider,
        "model_used": model_used,
        "tokens_used": len(response_text.split()),
        "latency_ms": round(latency, 2)
    }
    # Cache successful responses
    if response_text:
        ai_cache[cache_key] = result
    return result

# -------------------- AUTHENTICATION --------------------
security = HTTPBearer()

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> Dict:
    token = credentials.credentials
    try:
        # Verify Google ID token
        idinfo = id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)
        if idinfo['iss'] not in ['accounts.google.com', 'https://accounts.google.com']:
            raise HTTPException(status_code=401, detail="Invalid issuer")
        # Get or create user
        user_doc = await users_col.find_one({"googleId": idinfo['sub']})
        is_admin = idinfo['email'] == ADMIN_EMAIL
        if not user_doc:
            # Create new user
            new_user = {
                "googleId": idinfo['sub'],
                "email": idinfo['email'],
                "displayName": idinfo.get('name', idinfo['email']),
                "tier": "free",
                "dailyUsage": 0,
                "dailyUiUxUsage": 0,
                "storageBytesUsed": 0,
                "lastUsageDate": datetime.utcnow(),
                "customInstructions": "",
                "subTierOptions": {"hasDataAccess": False, "hasDesignAccess": False},
                "quotas": {
                    "dailyExtractionsUsed": 0,
                    "dailyGenerationsUsed": 0,
                    "dailyEnhancementsUsed": 0,
                    "monthlyEnhancementsLimit": 3,
                    "lastQuotaReset": datetime.utcnow()
                },
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
                "lastAiQuotaReset": datetime.utcnow()
            }
            result = await users_col.insert_one(new_user)
            user_doc = await users_col.find_one({"_id": result.inserted_id})
            logger.info(f"New user created: {idinfo['email']}")
        else:
            # Update admin flag if needed
            if user_doc.get("isAdmin") != is_admin:
                await users_col.update_one({"_id": user_doc["_id"]}, {"$set": {"isAdmin": is_admin}})
                user_doc["isAdmin"] = is_admin
            # Reset daily quotas if new day
            now = datetime.utcnow()
            today = datetime(now.year, now.month, now.day)
            last_reset = user_doc["quotas"]["lastQuotaReset"]
            if last_reset:
                last_reset_day = datetime(last_reset.year, last_reset.month, last_reset.day)
                if today > last_reset_day:
                    await users_col.update_one(
                        {"_id": user_doc["_id"]},
                        {"$set": {
                            "dailyUsage": 0,
                            "dailyUiUxUsage": 0,
                            "quotas.dailyExtractionsUsed": 0,
                            "quotas.dailyGenerationsUsed": 0,
                            "quotas.dailyEnhancementsUsed": 0,
                            "quotas.lastQuotaReset": datetime.utcnow(),
                            "tokenUsage.dailyPromptTokens": 0,
                            "tokenUsage.dailyCompletionTokens": 0,
                            "tokenUsage.lastTokenReset": datetime.utcnow(),
                            "dailyGroqQuota": 0,
                            "dailyOpenRouterQuota": 0,
                            "lastAiQuotaReset": datetime.utcnow()
                        }}
                    )
                    # refresh doc
                    user_doc = await users_col.find_one({"_id": user_doc["_id"]})
        return user_doc
    except Exception as e:
        logger.error(f"Auth failed: {e}")
        raise HTTPException(status_code=401, detail="Invalid or expired token")

# -------------------- FASTAPI APP --------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_indexes()
    logger.info("Unified Fortress online")
    yield
    # Shutdown
    client.close()
    logger.info("Shutdown complete")

app = FastAPI(title="AXELR Unified", version="9.0", lifespan=lifespan)

# CORS
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

# -------------------- RATE LIMITING (simple) --------------------
# In-memory rate limiter per IP (for global endpoint)
rate_limiter = {}
RATE_LIMIT_WINDOW = 15 * 60  # 15 minutes
RATE_LIMIT_MAX = 200

def check_rate_limit(client_ip: str):
    now = time.time()
    key = client_ip
    if key not in rate_limiter:
        rate_limiter[key] = []
    # Clean old entries
    rate_limiter[key] = [t for t in rate_limiter[key] if now - t < RATE_LIMIT_WINDOW]
    if len(rate_limiter[key]) >= RATE_LIMIT_MAX:
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
    rate_limiter[key].append(now)

# -------------------- HEALTH --------------------
@app.get("/")
@app.get("/api/health")
async def health():
    db_status = "connected"
    try:
        await db.command("ping")
    except:
        db_status = "disconnected"
    return {
        "status": "operational" if db_status == "connected" else "degraded",
        "timestamp": datetime.utcnow().isoformat(),
        "db": db_status,
        "stripe": stripe_client is not None,
        "email": SMTP_USER is not None,
        "uptime": time.time() - app.start_time if hasattr(app, "start_time") else 0
    }

@app.on_event("startup")
async def startup_event():
    app.start_time = time.time()

# -------------------- USER PROFILE --------------------
@app.get("/api/user/profile")
async def get_profile(user: dict = Depends(get_current_user)):
    return {
        "tier": user.get("tier"),
        "dailyUsage": user.get("dailyUsage", 0),
        "dailyUiUxUsage": user.get("dailyUiUxUsage", 0),
        "customInstructions": user.get("customInstructions", ""),
        "quotas": user.get("quotas", {}),
        "subTierOptions": user.get("subTierOptions", {}),
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
    }

@app.put("/api/user/instructions")
async def update_instructions(instructions: str = Form(...), user: dict = Depends(get_current_user)):
    # Actually we expect JSON body, but we can use Form if needed; better to use Pydantic
    # We'll define a model later, but for simplicity, use request body.
    pass  # We'll implement below with Pydantic

# Actually, we need a proper Pydantic model for requests. Let's define them here.
class InstructionsUpdate(BaseModel):
    instructions: str

@app.put("/api/user/instructions")
async def update_instructions(data: InstructionsUpdate, user: dict = Depends(get_current_user)):
    instructions = data.instructions[:5000]
    await users_col.update_one({"_id": user["_id"]}, {"$set": {"customInstructions": instructions}})
    return {"success": True}

@app.delete("/api/user/delete")
async def delete_account(user: dict = Depends(get_current_user)):
    uid = user["_id"]
    await sessions_col.delete_many({"userId": uid})
    await reports_col.delete_many({"userId": uid})
    await users_col.delete_one({"_id": uid})
    return {"success": True}

@app.delete("/api/history/delete-all")
async def delete_all_chats(user: dict = Depends(get_current_user)):
    await sessions_col.delete_many({"userId": user["_id"]})
    return {"success": True}

# -------------------- HISTORY ROUTES --------------------
class RenamePayload(BaseModel):
    action: str
    payload: Optional[str] = None

@app.put("/api/history/{history_id}")
async def update_history(history_id: str, data: RenamePayload, user: dict = Depends(get_current_user)):
    if not ObjectId.is_valid(history_id):
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
    if not ObjectId.is_valid(history_id):
        raise HTTPException(status_code=400, detail="Invalid ID")
    valid_statuses = ["active", "archived", "trashed"]
    if data.status not in valid_statuses:
        raise HTTPException(status_code=400, detail="Invalid status")
    update = {"status": data.status}
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
    if not ObjectId.is_valid(history_id):
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
    if not ObjectId.is_valid(history_id):
        raise HTTPException(status_code=400, detail="Invalid ID")
    session = await sessions_col.find_one({"_id": ObjectId(history_id), "userId": user["_id"]})
    if not session:
        raise HTTPException(status_code=404, detail="Not found")
    # Locate message by id
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
    # Update the message
    msg["activeVariant"] = data.variantIndex
    msg["text"] = variants[data.variantIndex]
    # Update the whole messages array
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
    if workspace not in ["data", "design", "general"]:
        workspace = "data"
    if status not in ["active", "archived", "trashed"]:
        status = "active"
    skip = (page - 1) * limit
    query = {"userId": user["_id"], "status": status, "workspace": workspace}
    total = await sessions_col.count_documents(query)
    cursor = sessions_col.find(query).sort([("isPinned", -1), ("createdAt", -1)]).skip(skip).limit(limit)
    logs = await cursor.to_list(length=limit)
    # Convert ObjectId to string
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

# -------------------- REPORTS (BUG/HELP) --------------------
class ReportCreate(BaseModel):
    type: str = "feedback"
    description: str

@app.post("/api/reports")
async def create_report(data: ReportCreate, user: dict = Depends(get_current_user)):
    report = {
        "userId": user["_id"],
        "type": data.type,
        "description": data.description[:5000],
        "createdAt": datetime.utcnow()
    }
    result = await reports_col.insert_one(report)
    # Send email to admin if configured
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
    prompt_text = data.promptText
    if not prompt_text:
        raise HTTPException(status_code=400, detail="No text provided")
    # Check quota
    now = datetime.utcnow()
    today = datetime(now.year, now.month, now.day)
    last_reset = user.get("quotas", {}).get("lastQuotaReset")
    if last_reset:
        last_day = datetime(last_reset.year, last_reset.month, last_reset.day)
        if today > last_day:
            await users_col.update_one(
                {"_id": user["_id"]},
                {"$set": {
                    "quotas.dailyEnhancementsUsed": 0,
                    "quotas.lastQuotaReset": datetime.utcnow()
                }}
            )
            user = await users_col.find_one({"_id": user["_id"]})

    # Determine limit based on tier
    tier = user.get("tier", "free")
    if tier == "free":
        limit = 3
    elif tier == "pro":
        has_data = user.get("subTierOptions", {}).get("hasDataAccess", False)
        has_design = user.get("subTierOptions", {}).get("hasDesignAccess", False)
        limit = 7 if (has_data and has_design) else 5
    elif tier == "business":
        has_data = user.get("subTierOptions", {}).get("hasDataAccess", False)
        has_design = user.get("subTierOptions", {}).get("hasDesignAccess", False)
        limit = 15 if (has_data and has_design) else 10
    else:
        limit = 3

    used = user.get("quotas", {}).get("dailyEnhancementsUsed", 0)
    if used >= limit:
        raise HTTPException(status_code=403, detail={
            "code": "LIMIT_REACHED",
            "usage": used,
            "limit": limit
        })

    # Call AI router with workspace='prompt'
    ai_result = await route_ai_request(
        workspace="prompt",
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

    # Increment usage
    await users_col.update_one(
        {"_id": user["_id"]},
        {"$inc": {
            "quotas.dailyEnhancementsUsed": 1,
            "dailyUsage": 1
        }}
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

def clean_assistant_message(text: str) -> str:
    if not text:
        return ""
    return re.sub(r'\|.*\|.*\n', '', text).strip()

@app.post("/api/extract")
async def extract(
    request: Request,
    user: dict = Depends(get_current_user),
    command: str = Form(...),
    workspace: str = Form("data"),
    isRetry: str = Form("false"),
    sessionId: Optional[str] = Form(None),
    files: List[UploadFile] = File([])
):
    # Rate limit per IP
    client_ip = request.client.host
    check_rate_limit(client_ip)

    # Validate workspace
    if workspace not in ["data", "design", "general"]:
        workspace = "data"
    # Validate sessionId if provided
    if sessionId and not ObjectId.is_valid(sessionId):
        sessionId = None

    # Validate files
    if len(files) > 5:
        raise HTTPException(status_code=400, detail="Too many files")
    total_size = 0
    for f in files:
        if f.size > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"File {f.filename} exceeds 10MB")
        total_size += f.size
    if total_size > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Total upload size too large")

    # ----- QUOTA CHECKS -----
    tier = user.get("tier", "free")
    has_data = user.get("subTierOptions", {}).get("hasDataAccess", False)
    has_design = user.get("subTierOptions", {}).get("hasDesignAccess", False)
    is_design = workspace == "design"

    if tier == "free":
        data_limit = 5
        ui_limit = 0
    elif tier == "pro":
        if has_data and has_design:
            data_limit = 20
            ui_limit = 15
        elif has_data:
            data_limit = 19
            ui_limit = 0
        elif has_design:
            data_limit = 0
            ui_limit = 13
        else:
            data_limit = 0
            ui_limit = 0
    elif tier == "business":
        if has_data and has_design:
            data_limit = 30
            ui_limit = 25
        elif has_data:
            data_limit = 28
            ui_limit = 0
        elif has_design:
            data_limit = 0
            ui_limit = 20
        else:
            data_limit = 0
            ui_limit = 0
    else:
        data_limit = 5
        ui_limit = 0

    if is_design:
        if tier != "free" and not has_design:
            raise HTTPException(status_code=403, detail={"code": "SUB_TIER_RESTRICTION", "message": "UI generation not included in your plan."})
        limit = ui_limit
        quota_field = "quotas.dailyGenerationsUsed"
    else:
        if tier != "free" and not has_data:
            raise HTTPException(status_code=403, detail={"code": "SUB_TIER_RESTRICTION", "message": "Data extraction not included in your plan."})
        limit = data_limit
        quota_field = "quotas.dailyExtractionsUsed"

    # Get current usage
    current_usage = user.get("quotas", {}).get(quota_field.split('.')[-1], 0) if quota_field.startswith("quotas.") else 0
    # Actually quota_field is string like "quotas.dailyGenerationsUsed", we need to get the value
    quota_parts = quota_field.split('.')
    if len(quota_parts) == 2:
        current_usage = user.get(quota_parts[0], {}).get(quota_parts[1], 0)
    else:
        current_usage = user.get(quota_field, 0)
    if current_usage >= limit:
        raise HTTPException(status_code=403, detail={"code": "LIMIT_REACHED", "usage": current_usage, "limit": limit})

    # Storage quota
    storage_limit = 5 * 1024 * 1024  # free
    if tier == "pro":
        storage_limit = 20 * 1024 * 1024
    elif tier == "business":
        storage_limit = 50 * 1024 * 1024
    current_storage = user.get("storageBytesUsed", 0)
    if current_storage + total_size > storage_limit:
        raise HTTPException(status_code=403, detail={"code": "STORAGE_LIMIT_REACHED", "message": f"Storage quota exceeded. Maximum {storage_limit / (1024*1024)}MB."})

    # ----- READ FILES -----
    file_contents = []
    for f in files:
        content_bytes = await f.read()
        b64 = content_bytes.hex()  # Actually we need base64
        import base64
        b64 = base64.b64encode(content_bytes).decode('utf-8')
        file_contents.append({
            "filename": f.filename,
            "mimetype": f.content_type or "application/octet-stream",
            "content_base64": b64
        })

    # ----- PREPARE SESSION -----
    current_session = None
    history = []
    if sessionId:
        current_session = await sessions_col.find_one({"_id": ObjectId(sessionId), "userId": user["_id"]})
        if current_session:
            history = current_session.get("messages", [])
            if isRetry == "true" and history and history[-1].get("role") == "model":
                history = history[:-2]  # remove last user+model

    # ----- AI CALL -----
    ai_result = await route_ai_request(
        workspace=workspace,
        prompt=command,
        history=history,
        files=file_contents,
        max_tokens=2048,
        temp=0.2,
        tier=tier
    )
    if not ai_result.get("success"):
        raise HTTPException(status_code=503, detail="AI service unavailable")

    ai_text = ai_result["text"]
    provider = ai_result.get("provider")
    model_used = ai_result.get("model_used")

    # ----- PROCESS JSON-DATA -----
    structured = []
    json_match = re.search(r'\[JSON-DATA\](.*?)\[/JSON-DATA\]', ai_text, re.DOTALL)
    if json_match:
        try:
            structured = json.loads(json_match.group(1).strip())
        except:
            structured = []
        ai_text = re.sub(r'\[JSON-DATA\].*?\[/JSON-DATA\]', '', ai_text, flags=re.DOTALL).strip()
    if not ai_text:
        ai_text = "I am Axelr AI. How can I help you?"

    # ----- INCREMENT QUOTAS AND STORAGE -----
    prompt_tokens = estimate_tokens(command) + sum(estimate_tokens(f["filename"]) + len(f["content_base64"]) // 4 for f in file_contents)
    completion_tokens = estimate_tokens(ai_text)
    update_query = {
        "$inc": {
            "tokenUsage.totalPromptTokens": prompt_tokens,
            "tokenUsage.totalCompletionTokens": completion_tokens,
            "tokenUsage.dailyPromptTokens": prompt_tokens,
            "tokenUsage.dailyCompletionTokens": completion_tokens,
            quota_field: 1,
            "dailyUsage": 1,
            "storageBytesUsed": total_size,
        }
    }
    if provider in ["groq", "groq-fallback"]:
        update_query["$inc"]["dailyGroqQuota"] = 1
    elif provider in ["openrouter", "openrouter-fallback"]:
        update_query["$inc"]["dailyOpenRouterQuota"] = 1
    await users_col.update_one({"_id": user["_id"]}, update_query)

    # ----- SAVE SESSION -----
    session_id_out = None
    filename_out = "Export.csv"
    session_saved = False

    if current_session:
        # Update existing
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
                # Update the messages array
                await sessions_col.update_one(
                    {"_id": ObjectId(sessionId)},
                    {"$set": {"messages": current_session["messages"], "structuredData": structured}}
                )
                session_saved = True
                session_id_out = sessionId
                filename_out = current_session.get("filename", "Export")
        else:
            # Append new messages
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
        # Create new session
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

    # Return response
    return {
        "success": True,
        "text": ai_text,
        "sessionId": session_id_out if session_saved else None,
        "structuredData": structured,
        "filename": f"{filename_out}.csv",
        "provider": provider,
        "model": model_used
    }

# -------------------- DEPLOY --------------------
class DeployRequest(BaseModel):
    htmlContent: str

@app.post("/api/deploy")
async def deploy(data: DeployRequest, user: dict = Depends(get_current_user)):
    html = data.htmlContent
    if not html:
        raise HTTPException(status_code=400, detail="Missing HTML content")
    if "<html" not in html or "</html>" not in html:
        raise HTTPException(status_code=400, detail="Generated HTML is incomplete.")

    # Sanitize with bleach
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

    # Attempt Vercel deployment
    if VERCEL_TOKEN and VERCEL_PROJECT_ID:
        try:
            async with httpx.AsyncClient(timeout=30.0) as http_client:
                # Prepare multipart form data
                files_payload = {"file": ("index.html", sanitized.encode('utf-8'), "text/html")}
                response = await http_client.post(
                    f"https://api.vercel.com/v1/deployments?projectId={VERCEL_PROJECT_ID}",
                    headers={"Authorization": f"Bearer {VERCEL_TOKEN}"},
                    files=files_payload
                )
                if response.status_code == 200:
                    result = response.json()
                    if result.get("url"):
                        return {"success": True, "liveUrl": f"https://{result['url']}"}
        except Exception as e:
            logger.warning(f"Vercel deploy failed: {e}")

    # Attempt Netlify
    if NETLIFY_TOKEN and NETLIFY_SITE_ID:
        try:
            async with httpx.AsyncClient(timeout=30.0) as http_client:
                files_payload = {"file": ("index.html", sanitized.encode('utf-8'), "text/html")}
                response = await http_client.post(
                    f"https://api.netlify.com/api/v1/sites/{NETLIFY_SITE_ID}/deploys",
                    headers={"Authorization": f"Bearer {NETLIFY_TOKEN}"},
                    files=files_payload
                )
                if response.status_code == 200:
                    result = response.json()
                    if result.get("deploy_url"):
                        return {"success": True, "liveUrl": result["deploy_url"]}
        except Exception as e:
            logger.warning(f"Netlify deploy failed: {e}")

    # Fallback: data URI
    data_uri = f"data:text/html;charset=utf-8,{sanitized}"
    return {"success": True, "liveUrl": data_uri, "message": "Preview available via data URI."}

# -------------------- ADMIN METRICS --------------------
@app.get("/api/admin/metrics")
async def admin_metrics(user: dict = Depends(get_current_user)):
    if not user.get("isAdmin") or user.get("email") != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Admin access restricted")
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    total_users = await users_col.count_documents({})
    pro_users = await users_col.count_documents({"tier": "pro"})
    business_users = await users_col.count_documents({"tier": "business"})
    total_chats = await sessions_col.count_documents({})

    # Aggregations
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

    # AI quota
    pipeline_ai = [
        {"$group": {"_id": None, "totalGroq": {"$sum": "$dailyGroqQuota"}, "totalOpenRouter": {"$sum": "$dailyOpenRouterQuota"}}}
    ]
    ai_result = await users_col.aggregate(pipeline_ai).to_list(length=1)
    ai_quotas = ai_result[0] if ai_result else {"totalGroq": 0, "totalOpenRouter": 0}

    # Daily usage (today)
    pipeline_daily = [
        {"$match": {"lastUsageDate": {"$gte": today}}},
        {"$group": {"_id": None, "dailyQueries": {"$sum": "$dailyUsage"}}}
    ]
    daily_result = await users_col.aggregate(pipeline_daily).to_list(length=1)
    daily_queries = daily_result[0]["dailyQueries"] if daily_result else 0

    pipeline_daily_ai = [
        {"$match": {"lastAiQuotaReset": {"$gte": today}}},
        {"$group": {"_id": None, "dailyGroq": {"$sum": "$dailyGroqQuota"}, "dailyOpenRouter": {"$sum": "$dailyOpenRouterQuota"}}}
    ]
    daily_ai_result = await users_col.aggregate(pipeline_daily_ai).to_list(length=1)
    daily_ai = daily_ai_result[0] if daily_ai_result else {"dailyGroq": 0, "dailyOpenRouter": 0}

    groq_limit = int(os.getenv("GROQ_DAILY_LIMIT", 1000))
    openrouter_limit = int(os.getenv("OPENROUTER_DAILY_LIMIT", 1000))

    recent_users = await users_col.find({}, {"email": 1, "displayName": 1, "tier": 1, "createdAt": 1}).sort("createdAt", -1).limit(10).to_list(length=10)
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
            "totalBytesMB": metrics["totalBytes"] / (1024 * 1024),
        },
        "tokenUsage": {
            "prompt": tokens["totalPrompt"],
            "completion": tokens["totalCompletion"],
            "total": total_tokens,
            "remaining": max(0, FREE_TIER_TOKEN_LIMIT - total_tokens),
            "limit": FREE_TIER_TOKEN_LIMIT,
        },
        "aiQuota": {
            "groq": ai_quotas["totalGroq"],
            "openRouter": ai_quotas["totalOpenRouter"],
            "dailyGroq": daily_ai["dailyGroq"],
            "dailyOpenRouter": daily_ai["dailyOpenRouter"],
            "groqLimit": groq_limit,
            "openRouterLimit": openrouter_limit,
        },
        "dailyQueries": daily_queries,
        "recentUsers": recent_users,
        "timestamp": datetime.utcnow().isoformat()
    }

# -------------------- STRIPE CHECKOUT --------------------
class CheckoutRequest(BaseModel):
    tier: str = "pro"
    subTier: str = "full"

@app.post("/api/billing/checkout")
async def create_checkout(data: CheckoutRequest, user: dict = Depends(get_current_user)):
    if not stripe_client:
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
    origin = "https://axelr.in"  # Could be dynamic
    try:
        session = stripe_client.checkout.Session.create(
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

# -------------------- STRIPE WEBHOOK --------------------
@app.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request):
    if not stripe_client:
        return JSONResponse(content={"received": True, "note": "Stripe disabled"})
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    event = None
    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe_client.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
        else:
            event = json.loads(payload)
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
                # Send email notification
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
    port = int(os.getenv("PORT", 8080))
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")
    if __name__ == '__main__':
    # Hardcoded to Port 3000 to match SnapDeploy's free tier load balancer routing
    app.run(host='0.0.0.0', port=3000)
