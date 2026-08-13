# -*- coding: utf-8 -*-
"""
AXELR AI - ELITE PRODUCTION v22.1
Provider Chain (priority):
  Tier 1 (Official): Gemini → Groq → Cloudflare → OpenRouter → Cerebras → Mistral → HuggingFace → GitHub Models → Nrouter
  Tier 2 (Gateways): Pollinations.ai → Puter → FreeTheAi → KeylessAI → FreeFlow → BazaarLink → Glama → ChubVenus → Neets.ai
  Tier 3 (Web fallbacks): VoidAI → Qoder → FreeGPT4 → OmniGPT → Text Cortex
  Ultimate fallback: local

Zero‑cost, permanent free tiers, automatic failover, 429 handling, circuit breakers.
All HTTP calls use httpx with appropriate timeouts (8s default).
Each provider may have multiple free models; the router tries each model sequentially.
"""
import os, re, time, json, asyncio, hashlib, smtplib, logging, base64, ssl
import urllib.request, urllib.error, urllib.parse
from datetime import datetime
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from collections import defaultdict

import httpx
from dotenv import load_dotenv

# Disable SSL for development (remove in production)
ssl._create_default_https_context = ssl._create_unverified_context

load_dotenv(override=True)

# ---------- Stripe (optional) ----------
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("axelr-unified")

# ---------- ENV VARS ----------
MONGO_URI = (os.getenv("MONGO_URI") or "").strip()
GOOGLE_CLIENT_ID = (os.getenv("GOOGLE_CLIENT_ID") or "").strip()
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "shanh1346@gmail.com")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
NETLIFY_ACCESS_TOKEN = os.getenv("NETLIFY_ACCESS_TOKEN")

# ---------- AI KEYS ----------
GROQ_API_KEY = (os.getenv("GROQ_API_KEY") or "").strip()
CLOUDFLARE_API_TOKEN = (os.getenv("CLOUDFLARE_API_TOKEN") or "").strip()
CLOUDFLARE_ACCOUNT_ID = (os.getenv("CLOUDFLARE_ACCOUNT_ID") or "").strip()
OPENROUTER_API_KEY = (os.getenv("OPENROUTER_API_KEY") or "").strip()
HF_API_KEY = (os.getenv("HUGGINGFACE_API_KEY") or "").strip()
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()
CEREBRAS_API_KEY = (os.getenv("CEREBRAS_API_KEY") or "").strip()
MISTRAL_API_KEY = (os.getenv("MISTRAL_API_KEY") or "").strip()
GITHUB_MODELS_TOKEN = (os.getenv("GITHUB_MODELS_TOKEN") or "").strip()
NROUTER_API_KEY = (os.getenv("NROUTER_API_KEY") or "").strip()
TEXT_CORTEX_API_KEY = (os.getenv("TEXT.CORTEX_API_KEY") or "").strip()   # <-- NEW
# Optional keys for additional providers (may be empty)
POLLINATIONS_KEY = (os.getenv("POLLINATIONS_KEY") or "").strip()

# ---------- MODEL LISTS (read from env, with defaults) ----------
# OpenRouter free models (verified as of Aug 2026)
OPENROUTER_MODELS_STR = os.getenv(
    "OPENROUTER_MODELS",
    "nvidia/nemotron-3.5-lightning:free,"
    "fish-audio/s2.1-pro-free:free,"
    "poolside/laguna-s-2.1:free,"
    "nvidia/nemotron-3-ultra-550b-a55b:free,"
    "cohere/north-mini-code:free,"
    "openai/gpt-oss-20b:free,"
    "openrouter/free-gpt-3.5-turbo:free,"
    "openrouter/free,"
    "nvidia/nemotron-3-embed-1b:free,"
    "nvidia/llama-nemotron-embed-vl-1b-v2:free"
)
OPENROUTER_MODELS = [m.strip() for m in OPENROUTER_MODELS_STR.split(",") if m.strip()]

# HuggingFace free models
HF_MODELS_STR = os.getenv(
    "HUGGINGFACE_MODELS",
    "google/gemma-2-9b-it,"
    "meta-llama/Llama-3.2-3B-Instruct,"
    "mistralai/Mistral-7B-Instruct-v0.3"
)
HF_MODELS = [m.strip() for m in HF_MODELS_STR.split(",") if m.strip()]

# Groq free models (check current availability)
GROQ_MODELS_STR = os.getenv("GROQ_MODELS",
                            "llama-3.1-8b-instant",
                            "canopylabs/orpheus-v1-english",
                            "meta-llama/llama-prompt-guard-2-86m"
                            "qwen/qwen3.6-27b"
                            "openai/gpt-oss-120b"
                            )
GROQ_MODELS = [m.strip() for m in GROQ_MODELS_STR.split(",") if m.strip()]

# Mistral free models
MISTRAL_MODELS_STR = os.getenv("MISTRAL_MODELS", "open-mistral-7b,mistral-small-latest,mistral-tiny")
MISTRAL_MODELS = [m.strip() for m in MISTRAL_MODELS_STR.split(",") if m.strip()]

# Gemini (only one known free model)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

# Cloudflare – single model (use environment or default)
CLOUDFLARE_MODEL = os.getenv("CLOUDFLARE_MODEL", "@cf/meta/llama-3.1-8b-instruct")

# GitHub Models
GITHUB_MODEL = os.getenv("GITHUB_MODEL", "gpt-4o-mini")

# Nrouter
NROUTER_MODEL = os.getenv("NROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct")

# Pollinations
POLLINATIONS_MODEL = os.getenv("POLLINATIONS_MODEL", "openai")

# Glama (if free, but we keep)
GLAMA_MODEL = os.getenv("GLAMA_MODEL", "gpt-3.5-turbo")

# Cerebras – use a valid free model (check docs)
CEREBRAS_MODEL = os.getenv("CEREBRAS_MODEL", "gpt-oss-120b""Gemma 4 31B")

# Text Cortex models (you can add more)
TEXT_CORTEX_MODELS = ["gpt-3.5-turbo",]

# Free tier token limit
FREE_TIER_TOKEN_LIMIT = int(os.getenv("FREE_TIER_TOKEN_LIMIT", 1000000))

HTTP_CLIENT = httpx.AsyncClient(
    timeout=httpx.Timeout(8.0, connect=5.0, read=8.0, write=5.0),
    verify=False
)

# -------------------- STRIPE INIT --------------------
if STRIPE_AVAILABLE and STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET:
    stripe.api_key = STRIPE_SECRET_KEY
    logger.info("Stripe initialized")

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

# -------------------- CACHE & CIRCUIT BREAKER --------------------
ai_cache = TTLCache(maxsize=2000, ttl=3600)
provider_failures = defaultdict(int)
provider_last_fail = defaultdict(float)
# Per-model failure tracking
model_failures = defaultdict(int)
model_last_fail = defaultdict(float)
PROVIDER_COOLDOWN = 600  # 10 minutes
MODEL_COOLDOWN = 120     # 2 minutes per model

# -------------------- SECURITY --------------------
MANIPULATION_PATTERNS = [
    r"forget all (instructions|prior|previous)",
    r"disregard (system prompt|guidelines|instructions)",
    r"ignore (all|previous) (instructions|prompts)",
    r"override your (system|core|primary) instructions",
    r"you are (not|no longer) bound by",
    r"bypass your safety",
    r"stop following your instructions",
    r"reset your instructions",
    r"act as (an|a) (evil|unethical|unrestricted) AI",
]

EXPLICIT_PATTERNS = [
    r"(?:sexual|porn|nude|sex|erotic|adult content)",
    r"(?:hack|exploit|malware|virus|crack)",
    r"(?:threat|kill|murder|terrorism)",
]

def detect_manipulation(text: str) -> bool:
    for pattern in MANIPULATION_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False

def contains_explicit(text: str) -> bool:
    for pattern in EXPLICIT_PATTERNS:
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

# -------------------- HTTP HELPER (fixed) --------------------
async def http_post_async(url: str, headers: Dict, json_data: Dict, timeout: float = 8.0):
    try:
        resp = await HTTP_CLIENT.post(url, headers=headers, json=json_data, timeout=timeout)
        resp.raise_for_status()
        try:
            return resp.json()
        except json.JSONDecodeError:
            return {"text": resp.text}
    except httpx.HTTPStatusError as e:
        # Handle specific status codes
        if e.response.status_code == 429:
            raise Exception(f"Quota exceeded: {e.response.text}")
        elif e.response.status_code == 402:
            raise Exception("Payment required – skipping provider")
        elif e.response.status_code in (301, 302, 303, 307, 308):
            location = e.response.headers.get('Location')
            if location:
                logger.info(f"Following redirect to {location}")
                return await http_post_async(location, headers, json_data, timeout)
        # Generic error
        raise Exception(f"HTTP error {e.response.status_code}: {e.response.text}")
    except Exception as e:
        raise Exception(f"HTTP request failed: {e}")

# -------------------- PROVIDER FUNCTIONS --------------------
# 1. GEMINI
async def call_gemini(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not GEMINI_API_KEY:
        raise Exception("GEMINI_API_KEY missing")
    model_name = model or GEMINI_MODEL
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temp,
            "maxOutputTokens": max_tokens,
            "topP": 0.95,
            "topK": 40
        }
    }
    resp = await http_post_async(url, headers, payload)
    try:
        return resp["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise Exception(f"Gemini unexpected response: {resp}")

# 2. GROQ
async def call_groq(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not GROQ_API_KEY:
        raise Exception("GROQ_API_KEY missing")
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    effective_model = model or GROQ_MODELS[0]
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 3. CLOUDFLARE
async def call_cloudflare(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not CLOUDFLARE_API_TOKEN or not CLOUDFLARE_ACCOUNT_ID:
        raise Exception("Cloudflare credentials missing")
    url = f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run/{model or CLOUDFLARE_MODEL}"
    headers = {"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
    }
    resp = await http_post_async(url, headers, payload)
    return resp.get("result", {}).get("response", "")

# 4. OPENROUTER
async def call_openrouter(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not OPENROUTER_API_KEY:
        raise Exception("OPENROUTER_API_KEY missing")
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://axelr.in",
        "X-Title": "Axelr AI"
    }
    effective_model = model or OPENROUTER_MODELS[0]
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 5. CEREBRAS
async def call_cerebras(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not CEREBRAS_API_KEY:
        raise Exception("CEREBRAS_API_KEY missing")
    url = "https://api.cerebras.ai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {CEREBRAS_API_KEY}",
        "Content-Type": "application/json"
    }
    effective_model = model or CEREBRAS_MODEL
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 6. MISTRAL
async def call_mistral(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not MISTRAL_API_KEY:
        raise Exception("MISTRAL_API_KEY missing")
    url = "https://api.mistral.ai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {MISTRAL_API_KEY}",
        "Content-Type": "application/json"
    }
    effective_model = model or MISTRAL_MODELS[0]
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 7. HUGGINGFACE
async def call_huggingface(prompt: str, max_tokens: int, temp: float, model: str) -> str:
    if not HF_API_KEY:
        raise Exception("HF_API_KEY missing")
    url = f"https://api-inference.huggingface.co/models/{model}"
    headers = {"Authorization": f"Bearer {HF_API_KEY}"}
    payload = {
        "inputs": prompt,
        "parameters": {
            "max_new_tokens": max_tokens,
            "temperature": temp,
            "return_full_text": False,
        }
    }
    resp = await http_post_async(url, headers, payload)
    if isinstance(resp, list):
        return resp[0].get("generated_text", "")
    return resp.get("generated_text", "")

# 8. GITHUB MODELS
async def call_github_models(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not GITHUB_MODELS_TOKEN:
        raise Exception("GITHUB_MODELS_TOKEN missing")
    url = "https://models.inference.ai.azure.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GITHUB_MODELS_TOKEN}",
        "Content-Type": "application/json"
    }
    effective_model = model or GITHUB_MODEL
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 9. NROUTER
async def call_nrouter(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not NROUTER_API_KEY:
        raise Exception("NROUTER_API_KEY missing")
    url = "https://api.nrouter.io/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {NROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    effective_model = model or NROUTER_MODEL
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 10. POLLINATIONS
async def call_pollinations(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    encoded_prompt = urllib.parse.quote(prompt)
    url = f"https://text.pollinations.ai/{encoded_prompt}?model={model or POLLINATIONS_MODEL}&temperature={temp}&max_tokens={max_tokens}"
    try:
        resp = await HTTP_CLIENT.get(url)
        resp.raise_for_status()
        return resp.text.strip()
    except Exception as e:
        raise Exception(f"Pollinations error: {e}")

# 11. PUTER
async def call_puter(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = "https://api.puter.com/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model or "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 12. FreeTheAi
async def call_freetheai(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = "https://api.freetheai.com/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model or "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 13. KeylessAI
async def call_keylessai(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = "https://api.keyless.ai/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model or "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 14. FreeFlow
async def call_freeflow(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = "https://freeflow.llm/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model or "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 15. BazaarLink
async def call_bazaarlink(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = "https://api.bazaarlink.io/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model or "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 16. Glama
async def call_glama(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = "https://api.glama.ai/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    effective_model = model or GLAMA_MODEL
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 17. Chub Venus
async def call_chubvenus(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = "https://api.chub.ai/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model or "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 18. Neets.ai
async def call_neets(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = "https://api.neets.ai/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model or "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 19. VoidAI
async def call_voidai(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = "https://api.voidai.com/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model or "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 20. Qoder
async def call_qoder(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = "https://api.qoder.com/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model or "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 21. FreeGPT4
async def call_freegpt4(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = "https://api.freegpt4.io/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model or "gpt-4",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 22. OmniGPT
async def call_omnigpt(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = "https://api.omnigpt.io/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model or "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 23. TEXT CORTEX (NEW)
async def call_text_cortex(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not TEXT_CORTEX_API_KEY:
        raise Exception("TEXT_CORTEX_API_KEY missing")
    # Using official endpoint (verify from docs)
    url = "https://api.textcortex.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {TEXT_CORTEX_API_KEY}",
        "Content-Type": "application/json"
    }
    effective_model = model or TEXT_CORTEX_MODELS[0]
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 24. LOCAL FALLBACK
async def call_local_fallback(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None, workspace: str = "general", task_type: str = "general") -> str:
    return build_local_fallback_response(workspace, task_type, prompt)

def build_local_fallback_response(workspace: str, task_type: str, prompt: str) -> str:
    prompt_text = (prompt or "").strip()
    if not prompt_text:
        return "I'm sorry, all AI services are temporarily unavailable. Please try again in a few minutes."
    if workspace == "design":
        return f"Design concept for: \"{prompt_text[:120]}\".\nHere's a starting point – refine it and I'll assist further."
    if workspace == "data":
        return f"Data analysis for: \"{prompt_text[:120]}\".\nPlease provide the source data or example output for a more precise analysis."
    if task_type == "touch_fix":
        return f"Debugging: \"{prompt_text[:120]}\".\nPlease share the full error, file name, and expected behaviour."
    return f"Request received: \"{prompt_text[:160]}\".\nI can help with a concise plan, code snippet, or structured answer – tell me more specifics."

# -------------------- PROVIDER CHAIN --------------------
PROVIDER_CHAIN = [
    ("gemini", call_gemini),
    ("groq", call_groq),
    ("cloudflare", call_cloudflare),
    ("openrouter", call_openrouter),
    ("cerebras", call_cerebras),
    ("mistral", call_mistral),
    ("huggingface", call_huggingface),
    ("github_models", call_github_models),
    ("nrouter", call_nrouter),
    ("pollinations", call_pollinations),
    ("puter", call_puter),
    ("freetheai", call_freetheai),
    ("keylessai", call_keylessai),
    ("freeflow", call_freeflow),
    ("bazaarlink", call_bazaarlink),
    ("glama", call_glama),
    ("chubvenus", call_chubvenus),
    ("neets", call_neets),
    ("voidai", call_voidai),
    ("qoder", call_qoder),
    ("freegpt4", call_freegpt4),
    ("omnigpt", call_omnigpt),
    ("text_cortex", call_text_cortex),   # <-- NEW
    ("local", call_local_fallback),
]

# PROVIDER_MODELS: map provider name to list of model names
PROVIDER_MODELS = {
    "gemini": [GEMINI_MODEL],
    "groq": GROQ_MODELS,
    "cloudflare": [CLOUDFLARE_MODEL],
    "openrouter": OPENROUTER_MODELS,
    "cerebras": [CEREBRAS_MODEL],
    "mistral": MISTRAL_MODELS,
    "huggingface": HF_MODELS,
    "github_models": [GITHUB_MODEL],
    "nrouter": [NROUTER_MODEL],
    "pollinations": [POLLINATIONS_MODEL],
    "puter": ["gpt-3.5-turbo"],
    "freetheai": ["gpt-3.5-turbo"],
    "keylessai": ["gpt-3.5-turbo"],
    "freeflow": ["gpt-3.5-turbo"],
    "bazaarlink": ["gpt-3.5-turbo"],
    "glama": [GLAMA_MODEL],
    "chubvenus": ["gpt-3.5-turbo"],
    "neets": ["gpt-3.5-turbo"],
    "voidai": ["gpt-3.5-turbo"],
    "qoder": ["gpt-3.5-turbo"],
    "freegpt4": ["gpt-4"],
    "omnigpt": ["gpt-3.5-turbo"],
    "text_cortex": TEXT_CORTEX_MODELS,
    "local": [],
}

provider_health = {p: {"status": "unknown", "last_check": None, "daily_usage": 0} for p, _ in PROVIDER_CHAIN}

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

# -------------------- WORKSPACE-SPECIFIC PRIORITY --------------------
WORKSPACE_PRIORITY = {
    "data": ["gemini", "groq", "cloudflare", "mistral", "openrouter"],
    "design": ["cloudflare", "groq", "gemini", "openrouter", "mistral"],
    "general": ["gemini", "groq", "cloudflare", "openrouter", "mistral"],
    "prompt": ["gemini", "openrouter", "groq"],
    "touch_fix": ["groq", "mistral", "gemini"],
}

def get_provider_order(workspace: str) -> List[str]:
    """Return ordered list of provider names for the given workspace."""
    provider_names = [name for name, _ in PROVIDER_CHAIN if name != "local"]
    priority = WORKSPACE_PRIORITY.get(workspace, WORKSPACE_PRIORITY["general"])
    ordered = []
    for name in priority:
        if name in provider_names and name not in ordered:
            ordered.append(name)
    # Append remaining providers in original order
    for name in provider_names:
        if name not in ordered:
            ordered.append(name)
    ordered.append("local")
    return ordered

# -------------------- AI ROUTER (with per-model fallback) --------------------
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

    # Security checks
    if detect_manipulation(prompt):
        return {
            "success": False,
            "text": "⚠️ WARNING: Manipulation attempt detected. Your action has been logged. Please stay within operational parameters.",
            "provider": "security",
            "model_used": "filter",
            "tokens_used": 0,
            "latency_ms": 0
        }
    if contains_explicit(prompt):
        return {
            "success": False,
            "text": "🚫 Your request violates our content policy. Please revise your input.",
            "provider": "security",
            "model_used": "blocked",
            "tokens_used": 0,
            "latency_ms": 0
        }

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

    cache_key = hashlib.sha256(f"{workspace}:{task_type}:{full_prompt}".encode()).hexdigest()
    if cache_key in ai_cache:
        cached = ai_cache[cache_key]
        return {**cached, "cached": True}

    response_text = None
    provider_used = None
    model_used = None
    last_error = None

    provider_names = [name for name, _ in PROVIDER_CHAIN if name != "local"]
    provider_order = get_provider_order(workspace)
    provider_func_map = dict(PROVIDER_CHAIN)

    for provider_name in provider_order:
        if provider_name == "local":
            continue
        func = provider_func_map.get(provider_name)
        if not func:
            continue
        # Skip if API key missing (for those that require keys)
        if provider_name == "gemini" and not GEMINI_API_KEY:
            continue
        if provider_name == "groq" and not GROQ_API_KEY:
            continue
        if provider_name == "cloudflare" and (not CLOUDFLARE_API_TOKEN or not CLOUDFLARE_ACCOUNT_ID):
            continue
        if provider_name == "openrouter" and not OPENROUTER_API_KEY:
            continue
        if provider_name == "cerebras" and not CEREBRAS_API_KEY:
            continue
        if provider_name == "mistral" and not MISTRAL_API_KEY:
            continue
        if provider_name == "huggingface" and not HF_API_KEY:
            continue
        if provider_name == "github_models" and not GITHUB_MODELS_TOKEN:
            continue
        if provider_name == "nrouter" and not NROUTER_API_KEY:
            continue
        if provider_name == "text_cortex" and not TEXT_CORTEX_API_KEY:
            continue

        # Check provider-level circuit breaker
        if provider_failures[provider_name] >= 3 and time.time() - provider_last_fail[provider_name] < PROVIDER_COOLDOWN:
            logger.warning(f"Skipping {provider_name} (provider circuit breaker)")
            continue

        models = PROVIDER_MODELS.get(provider_name, [])
        if not models:
            continue

        # Try each model for this provider
        provider_success = False
        for model in models:
            model_key = (provider_name, model)
            # Check model-level circuit breaker
            if model_failures[model_key] >= 3 and time.time() - model_last_fail[model_key] < MODEL_COOLDOWN:
                logger.warning(f"Skipping {provider_name}/{model} (model circuit breaker)")
                continue

            for attempt in range(2):
                try:
                    resp_text = await func(full_prompt, max_tokens, temp, model)
                    if resp_text:
                        response_text = resp_text
                        provider_used = provider_name
                        model_used = model
                        provider_success = True
                        # Reset failures on success
                        provider_failures[provider_name] = 0
                        model_failures[model_key] = 0
                        logger.info(f"Provider {provider_name} with model {model} succeeded.")
                        break
                except Exception as e:
                    last_error = e
                    error_msg = str(e).lower()
                    if "quota" in error_msg or "429" in error_msg:
                        logger.warning(f"{provider_name}/{model} quota exceeded, skipping model")
                        model_failures[model_key] += 1
                        model_last_fail[model_key] = time.time()
                        break  # skip to next model
                    elif "payment required" in error_msg or "402" in error_msg:
                        logger.warning(f"{provider_name}/{model} requires payment, skipping")
                        model_failures[model_key] += 1
                        model_last_fail[model_key] = time.time()
                        break
                    logger.warning(f"{provider_name}/{model} attempt {attempt+1} failed: {e}")
                    await asyncio.sleep(2 ** attempt)
                    model_failures[model_key] += 1
                    model_last_fail[model_key] = time.time()
            if provider_success:
                break  # break out of model loop

        if provider_success:
            break  # break out of provider loop
        else:
            provider_failures[provider_name] += 1
            provider_last_fail[provider_name] = time.time()
            logger.warning(f"All models for provider {provider_name} failed; marking provider cooldown")

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

    if provider_used and provider_used in provider_health:
        provider_health[provider_used]["status"] = "active"
        provider_health[provider_used]["last_check"] = datetime.utcnow().isoformat()
        provider_health[provider_used]["daily_usage"] = provider_health[provider_used].get("daily_usage", 0) + 1

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
                "dailyCloudflareQuota": 0,
                "dailyGeminiQuota": 0,
                "dailyOpenRouterQuota": 0,
                "dailyGroqQuota": 0,
                "dailyHuggingFaceQuota": 0,
                "dailyCerebrasQuota": 0,
                "dailyMistralQuota": 0,
                "dailyGithubQuota": 0,
                "dailyNrouterQuota": 0,
                "dailyTextCortexQuota": 0,   # <-- NEW
                "lastAiQuotaReset": datetime.utcnow()
            }
            result = await users_col.insert_one(new_user)
            user_doc = await users_col.find_one({"_id": result.inserted_id})
            logger.info(f"New user created: {idinfo['email']}")
        else:
            if user_doc.get("isAdmin") != is_admin:
                await users_col.update_one({"_id": user_doc["_id"]}, {"$set": {"isAdmin": is_admin}})
                user_doc["isAdmin"] = is_admin
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
                            "dailyCloudflareQuota": 0,
                            "dailyGeminiQuota": 0,
                            "dailyOpenRouterQuota": 0,
                            "dailyGroqQuota": 0,
                            "dailyHuggingFaceQuota": 0,
                            "dailyCerebrasQuota": 0,
                            "dailyMistralQuota": 0,
                            "dailyGithubQuota": 0,
                            "dailyNrouterQuota": 0,
                            "dailyTextCortexQuota": 0,
                            "lastAiQuotaReset": datetime.utcnow()
                        }}
                    )
                    user_doc = await users_col.find_one({"_id": user_doc["_id"]})
        return user_doc
    except Exception as e:
        logger.error(f"Auth failed: {e}")
        raise HTTPException(status_code=401, detail="Invalid or expired token")

# -------------------- PER‑USER RATE LIMITING --------------------
user_rate_limiter = {}
RATE_LIMITS = {"free": 2, "pro": 5, "business": 8}

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

app = FastAPI(title="AXELR Unified", version="22.1", lifespan=lifespan)

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

@app.on_event("startup")
async def startup_event():
    app.state.start_time = time.time()

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

# -------------------- DIAGNOSTICS ROUTE --------------------
@app.get("/api/v1/diagnose")
async def diagnose_providers():
    """Concurrently ping all providers with their first model to check real-time availability."""
    results = {}
    test_prompt = "Say 'OK'"
    tasks = {}
    for provider_name, func in PROVIDER_CHAIN:
        if provider_name == "local":
            continue
        if provider_name == "gemini" and not GEMINI_API_KEY:
            results[provider_name] = {"status": "skipped", "reason": "No API key"}
            continue
        if provider_name == "groq" and not GROQ_API_KEY:
            results[provider_name] = {"status": "skipped", "reason": "No API key"}
            continue
        if provider_name == "cloudflare" and (not CLOUDFLARE_API_TOKEN or not CLOUDFLARE_ACCOUNT_ID):
            results[provider_name] = {"status": "skipped", "reason": "Missing credentials"}
            continue
        if provider_name == "openrouter" and not OPENROUTER_API_KEY:
            results[provider_name] = {"status": "skipped", "reason": "No API key"}
            continue
        if provider_name == "cerebras" and not CEREBRAS_API_KEY:
            results[provider_name] = {"status": "skipped", "reason": "No API key"}
            continue
        if provider_name == "mistral" and not MISTRAL_API_KEY:
            results[provider_name] = {"status": "skipped", "reason": "No API key"}
            continue
        if provider_name == "huggingface" and not HF_API_KEY:
            results[provider_name] = {"status": "skipped", "reason": "No API key"}
            continue
        if provider_name == "github_models" and not GITHUB_MODELS_TOKEN:
            results[provider_name] = {"status": "skipped", "reason": "No API key"}
            continue
        if provider_name == "nrouter" and not NROUTER_API_KEY:
            results[provider_name] = {"status": "skipped", "reason": "No API key"}
            continue
        if provider_name == "text_cortex" and not TEXT_CORTEX_API_KEY:
            results[provider_name] = {"status": "skipped", "reason": "No API key"}
            continue

        models = PROVIDER_MODELS.get(provider_name, [])
        if not models:
            results[provider_name] = {"status": "skipped", "reason": "No models configured"}
            continue
        model = models[0]
        tasks[provider_name] = asyncio.create_task(
            _probe_provider(provider_name, func, test_prompt, model)
        )

    for name, task in tasks.items():
        try:
            result = await task
            results[name] = result
        except Exception as e:
            results[name] = {"status": "error", "error": str(e)[:100]}

    return {"providers": results}

async def _probe_provider(name: str, func, prompt: str, model: Optional[str]) -> Dict:
    try:
        start = time.time()
        resp = await func(prompt, 5, 0.0, model)
        latency = (time.time() - start) * 1000
        if resp and "OK" in resp:
            return {"status": "healthy", "latency_ms": round(latency, 2)}
        else:
            return {"status": "unhealthy", "response": resp[:50] if resp else "empty"}
    except Exception as e:
        return {"status": "error", "error": str(e)[:100]}

# -------------------- ALL ORIGINAL ENDPOINTS (with fixes) --------------------
# 1. User profile
@app.get("/api/user/profile")
async def get_profile(user: dict = Depends(get_current_user)):
    if not db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")
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
        "dailyCloudflareQuota": user.get("dailyCloudflareQuota", 0),
        "dailyGeminiQuota": user.get("dailyGeminiQuota", 0),
        "dailyOpenRouterQuota": user.get("dailyOpenRouterQuota", 0),
        "dailyGroqQuota": user.get("dailyGroqQuota", 0),
        "dailyHuggingFaceQuota": user.get("dailyHuggingFaceQuota", 0),
        "dailyCerebrasQuota": user.get("dailyCerebrasQuota", 0),
        "dailyMistralQuota": user.get("dailyMistralQuota", 0),
        "dailyGithubQuota": user.get("dailyGithubQuota", 0),
        "dailyNrouterQuota": user.get("dailyNrouterQuota", 0),
        "dailyTextCortexQuota": user.get("dailyTextCortexQuota", 0),
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
        {"$inc": {
            "quotas.dailyEnhancementsUsed": 1,
            "dailyUsage": 1
        }}
    )
    return {"success": True, "enhanced": enhanced}

# ---------- extract (main) ----------
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
            "application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/msword", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ]
        allowed_data_exts = ('.csv', '.xls', '.xlsx', '.pdf', '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.txt', '.doc', '.docx')
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
                   f"Allowed: {'images, PDF, CSV, Excel, Word, text' if workspace=='data' else 'images, code files (HTML, CSS, JS, Python, etc.), JSON, Markdown, text'}."
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
    has_data = user.get("subTierOptions", {}).get("hasDataAccess", False)
    has_design = user.get("subTierOptions", {}).get("hasDesignAccess", False)
    is_design = workspace == "design"

    if tier == "free":
        data_limit = 5
        ui_limit = 3
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
        limit = ui_limit
        quota_field = "quotas.dailyGenerationsUsed"
    else:
        limit = data_limit
        quota_field = "quotas.dailyExtractionsUsed"

    quota_parts = quota_field.split('.')
    if len(quota_parts) == 2:
        current_usage = user.get(quota_parts[0], {}).get(quota_parts[1], 0)
    else:
        current_usage = user.get(quota_field, 0)

    if current_usage > limit:
        await users_col.update_one({"_id": user["_id"]}, {"$set": {quota_field: limit}})
        current_usage = limit

    logger.info(f"User {user.get('email')} tier={tier} workspace={workspace} usage={current_usage}/{limit}")

    if current_usage >= limit:
        raise HTTPException(status_code=403, detail={"code": "LIMIT_REACHED", "usage": current_usage, "limit": limit})

    storage_limit = 5 * 1024 * 1024
    if tier == "pro":
        storage_limit = 20 * 1024 * 1024
    elif tier == "business":
        storage_limit = 50 * 1024 * 1024
    current_storage = user.get("storageBytesUsed", 0)
    if current_storage + total_size > storage_limit:
        raise HTTPException(status_code=403, detail={"code": "STORAGE_LIMIT_REACHED", "message": f"Storage quota exceeded. Maximum {storage_limit / (1024*1024)}MB."})

    file_contents = []
    for f in files:
        content_bytes = await f.read()
        b64 = base64.b64encode(content_bytes).decode('utf-8')
        file_contents.append({
            "filename": f.filename,
            "mimetype": f.content_type or "application/octet-stream",
            "content_base64": b64
        })

    if task_type is None:
        if workspace == "data":
            task_type = "extraction"
        elif workspace == "design":
            task_type = "frontend"
        else:
            task_type = "structuring"
    supported_types = ["extraction", "frontend", "structuring", "touch_fix"]
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
    if not ai_result.get("success"):
        raise HTTPException(status_code=503, detail="AI service unavailable")

    ai_text = ai_result["text"]
    provider = ai_result.get("provider")
    model_used = ai_result.get("model_used")

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

    if provider != "local":
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
            },
            "$set": {
                "lastUsageDate": datetime.utcnow()
            }
        }
        # Track provider usage
        if provider == "gemini":
            update_query["$inc"]["dailyGeminiQuota"] = 1
        elif provider == "groq":
            update_query["$inc"]["dailyGroqQuota"] = 1
        elif provider == "cloudflare":
            update_query["$inc"]["dailyCloudflareQuota"] = 1
        elif provider == "openrouter":
            update_query["$inc"]["dailyOpenRouterQuota"] = 1
        elif provider == "cerebras":
            update_query["$inc"]["dailyCerebrasQuota"] = 1
        elif provider == "mistral":
            update_query["$inc"]["dailyMistralQuota"] = 1
        elif provider == "huggingface":
            update_query["$inc"]["dailyHuggingFaceQuota"] = 1
        elif provider == "github_models":
            update_query["$inc"]["dailyGithubQuota"] = 1
        elif provider == "nrouter":
            update_query["$inc"]["dailyNrouterQuota"] = 1
        elif provider == "text_cortex":
            update_query["$inc"]["dailyTextCortexQuota"] = 1
        # Other providers are not tracked individually
        await users_col.update_one({"_id": user["_id"]}, update_query)
    else:
        logger.info(f"Local fallback used for user {user['email']}")

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
                "canRegenerate": True,   # <-- ADDED: flag for frontend
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
                    "canRegenerate": True,   # <-- ADDED
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

# ---------- touch_fix ----------
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

# ---------- deploy ----------
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

# ---------- TEST EMAIL ENDPOINT ----------
@app.get("/api/test-email")
async def test_email(user: dict = Depends(get_current_user)):
    if not user.get("isAdmin"):
        raise HTTPException(status_code=403, detail="Admin only")
    try:
        server = get_email_transport()
        if not server:
            return {"success": False, "error": "SMTP not configured"}
        msg = MIMEText("This is a test email from Axelr AI.")
        msg["Subject"] = "Test Email"
        msg["From"] = SMTP_USER
        msg["To"] = ADMIN_EMAIL
        server.sendmail(SMTP_USER, ADMIN_EMAIL, msg.as_string())
        server.quit()
        return {"success": True, "message": f"Test email sent to {ADMIN_EMAIL}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ---------- admin metrics ----------
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
                    "totalGemini": {"$sum": "$dailyGeminiQuota"},
                    "totalCloudflare": {"$sum": "$dailyCloudflareQuota"},
                    "totalHuggingFace": {"$sum": "$dailyHuggingFaceQuota"},
                    "totalCerebras": {"$sum": "$dailyCerebrasQuota"},
                    "totalMistral": {"$sum": "$dailyMistralQuota"},
                    "totalGithub": {"$sum": "$dailyGithubQuota"},
                    "totalNrouter": {"$sum": "$dailyNrouterQuota"},
                    "totalTextCortex": {"$sum": "$dailyTextCortexQuota"}}}
    ]
    provider_result = await users_col.aggregate(pipeline_provider).to_list(length=1)
    provider_totals = provider_result[0] if provider_result else {
        "totalGroq":0, "totalOpenRouter":0, "totalGemini":0,
        "totalCloudflare":0, "totalHuggingFace":0, "totalCerebras":0, "totalMistral":0,
        "totalGithub":0, "totalNrouter":0, "totalTextCortex":0
    }

    pipeline_daily_provider = [
        {"$match": {"lastAiQuotaReset": {"$gte": today}}},
        {"$group": {"_id": None,
                    "dailyGroq": {"$sum": "$dailyGroqQuota"},
                    "dailyOpenRouter": {"$sum": "$dailyOpenRouterQuota"},
                    "dailyGemini": {"$sum": "$dailyGeminiQuota"},
                    "dailyCloudflare": {"$sum": "$dailyCloudflareQuota"},
                    "dailyHuggingFace": {"$sum": "$dailyHuggingFaceQuota"},
                    "dailyCerebras": {"$sum": "$dailyCerebrasQuota"},
                    "dailyMistral": {"$sum": "$dailyMistralQuota"},
                    "dailyGithub": {"$sum": "$dailyGithubQuota"},
                    "dailyNrouter": {"$sum": "$dailyNrouterQuota"},
                    "dailyTextCortex": {"$sum": "$dailyTextCortexQuota"}}}
    ]
    daily_provider_result = await users_col.aggregate(pipeline_daily_provider).to_list(length=1)
    daily_provider = daily_provider_result[0] if daily_provider_result else {
        "dailyGroq":0, "dailyOpenRouter":0, "dailyGemini":0,
        "dailyCloudflare":0, "dailyHuggingFace":0, "dailyCerebras":0, "dailyMistral":0,
        "dailyGithub":0, "dailyNrouter":0, "dailyTextCortex":0
    }

    provider_status = {}
    for p, health in provider_health.items():
        status = health.get("status", "unknown")
        if health.get("last_check"):
            try:
                last_check = datetime.fromisoformat(health["last_check"])
                if (datetime.utcnow() - last_check).total_seconds() > 3600:
                    status = "inactive"
            except:
                pass
        provider_status[p] = {
            "status": status,
            "daily_usage": health.get("daily_usage", 0),
            "last_check": health.get("last_check")
        }

    groq_limit = int(os.getenv("GROQ_DAILY_LIMIT", 1000000))
    openrouter_limit = int(os.getenv("OPENROUTER_DAILY_LIMIT", 1000000))
    gemini_limit = int(os.getenv("GEMINI_DAILY_LIMIT", 1500))
    cloudflare_limit = int(os.getenv("CLOUDFLARE_DAILY_LIMIT", 1000000))
    huggingface_limit = int(os.getenv("HUGGINGFACE_DAILY_LIMIT", 1000000))
    cerebras_limit = int(os.getenv("CEREBRAS_DAILY_LIMIT", 1000000))
    mistral_limit = int(os.getenv("MISTRAL_DAILY_LIMIT", 1000000))
    github_limit = int(os.getenv("GITHUB_DAILY_LIMIT", 1000000))
    nrouter_limit = int(os.getenv("NROUTER_DAILY_LIMIT", 1000000))
    text_cortex_limit = int(os.getenv("TEXT_CORTEX_DAILY_LIMIT", 100))

    daily_usage = {
        "groq": daily_provider["dailyGroq"],
        "openrouter": daily_provider["dailyOpenRouter"],
        "gemini": daily_provider["dailyGemini"],
        "cloudflare": daily_provider["dailyCloudflare"],
        "huggingface": daily_provider["dailyHuggingFace"],
        "cerebras": daily_provider["dailyCerebras"],
        "mistral": daily_provider["dailyMistral"],
        "github": daily_provider["dailyGithub"],
        "nrouter": daily_provider["dailyNrouter"],
        "text_cortex": daily_provider["dailyTextCortex"],
    }
    active_provider = max(daily_usage, key=daily_usage.get) if any(daily_usage.values()) else "gemini"

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
            "gemini": provider_totals["totalGemini"],
            "cloudflare": provider_totals["totalCloudflare"],
            "huggingFace": provider_totals["totalHuggingFace"],
            "cerebras": provider_totals["totalCerebras"],
            "mistral": provider_totals["totalMistral"],
            "github": provider_totals["totalGithub"],
            "nrouter": provider_totals["totalNrouter"],
            "textCortex": provider_totals["totalTextCortex"],
            "dailyGroq": daily_provider["dailyGroq"],
            "dailyOpenRouter": daily_provider["dailyOpenRouter"],
            "dailyGemini": daily_provider["dailyGemini"],
            "dailyCloudflare": daily_provider["dailyCloudflare"],
            "dailyHuggingFace": daily_provider["dailyHuggingFace"],
            "dailyCerebras": daily_provider["dailyCerebras"],
            "dailyMistral": daily_provider["dailyMistral"],
            "dailyGithub": daily_provider["dailyGithub"],
            "dailyNrouter": daily_provider["dailyNrouter"],
            "dailyTextCortex": daily_provider["dailyTextCortex"],
            "groqLimit": groq_limit,
            "openRouterLimit": openrouter_limit,
            "geminiLimit": gemini_limit,
            "cloudflareLimit": cloudflare_limit,
            "huggingFaceLimit": huggingface_limit,
            "cerebrasLimit": cerebras_limit,
            "mistralLimit": mistral_limit,
            "githubLimit": github_limit,
            "nrouterLimit": nrouter_limit,
            "textCortexLimit": text_cortex_limit,
            "activeProvider": active_provider,
        },
        "providerStatus": provider_status,
        "dailyQueries": daily_queries,
        "recentUsers": recent_users,
        "timestamp": datetime.utcnow().isoformat()
    }

# ---------- stripe & webhook ----------
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

# ---------- 404 ----------
@app.exception_handler(404)
async def not_found(request, exc):
    return JSONResponse(status_code=404, content={"success": False, "code": "NOT_FOUND", "message": "Endpoint not found."})

# ---------- MAIN ----------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    logger.info(f"=== STARTING AXELR AI v22.1 ON PORT {port} ===")
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")