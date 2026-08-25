# -*- coding: utf-8 -*-
"""
AXELR AI - ELITE PRODUCTION v24.0
=================================
5‑Tier Provider Chain (priority) – all free/community tiers:
  Tier 1 (Primary Elite): Gemini → Groq → OpenRouter → Cloudflare → ModelScope → Ollama Cloud → Nara Router
  Tier 2 (Highly Recommended): Mistral → HuggingFace → GitHub Models → Zhipu → Teamorouter → OVHcloud → SiliconFlow → Agnes AI → Bifrost → FreeGPT4‑WEB‑API → Bazaarlink → Requesty
  Tier 3 (Useful Fallbacks): Nrouter → Puter → FreeTheAi → Omni GPT Gateway → OpenCode Zen → FreeFlow → Qoder → Manifest
  Tier 4 (Limited/Unstable): KeylessAI → Glama → ChubVenus → BlockRun → AnyAPI → Aymo → ZeroTwoAI → AI Hub MIX → AISure
  Tier 5 (Ultimate Fallback): Local (graceful message)

Zero‑cost, permanent free tiers, automatic failover, 429 handling, circuit breakers.
"""

import os, re, time, json, asyncio, hashlib, smtplib, logging, base64, ssl
import urllib.request, urllib.error, urllib.parse
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from collections import defaultdict
import secrets
import bcrypt
from jose import JWTError, jwt
from dotenv import load_dotenv
import bleach
from cachetools import TTLCache
from fastapi import FastAPI, HTTPException, Depends, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
import uvicorn
import httpx
from httpx import TimeoutException, ConnectError
import redis.asyncio as aioredis

# ---------- DISABLE SSL FOR DEV (remove in production) ----------
ssl._create_default_https_context = ssl._create_unverified_context
load_dotenv(override=True)

# ---------- FASTAPI APP ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await init_redis()
    if not db_available:
        logger.critical("MongoDB is not available. The application will run in degraded mode.")
    else:
        logger.info("Unified Fortress online")
    app.state.start_time = time.time()
    # Validate all providers on startup (async task)
    asyncio.create_task(validate_all_providers())
    yield
    if client:
        client.close()
        logger.info("Shutdown complete")

app = FastAPI(title="AXELR Unified", version="24.0", lifespan=lifespan)

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

# ---------- STRIPE (optional) ----------
STRIPE_AVAILABLE = False
stripe = None
try:
    import stripe
    STRIPE_AVAILABLE = True
except ImportError:
    pass

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("axelr-unified")

# ---------- ENV VARS ----------
MONGO_URI = (os.getenv("MONGO_URI") or "").strip()
GOOGLE_CLIENT_ID = (os.getenv("GOOGLE_CLIENT_ID") or "").strip()
if not GOOGLE_CLIENT_ID:
    GOOGLE_CLIENT_ID = "474929925590-kfpurq4aou35pkscf6gbr963vf4hfa7g.apps.googleusercontent.com"
    logger.warning("GOOGLE_CLIENT_ID not set. Using default (frontend).")
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
MISTRAL_API_KEY = (os.getenv("MISTRAL_API_KEY") or "").strip()
GITHUB_MODELS_TOKEN = (os.getenv("GITHUB_MODELS_TOKEN") or "").strip()
NROUTER_API_KEY = (os.getenv("NROUTER_API_KEY") or "").strip()
TEXT_CORTEX_API_KEY = (os.getenv("TEXT.CORTEX_API_KEY") or "").strip()
NARAROUTER_API_KEY = (os.getenv("NARAROUTER_API_KEY") or "").strip()
BAZAARLINK_API_KEY = (os.getenv("BAZAARLINK_API-KEY") or "").strip()
SILICONFLOW_API_KEY = (os.getenv("SILICONFLOW_API_KEY") or "").strip()
AGNES_API_KEY = (os.getenv("AGNES_API_KEY") or "").strip()
OLLAMA_API_KEY = (os.getenv("OLLAMA_API_KEY") or "").strip()
ANYAPI_API_KEY = (os.getenv("ANYAPI_API_KEY") or "").strip()
MODELSCOPE_API_KEY = (os.getenv("MODELSCOPE_API_KEY") or "").strip()
OVHCLOUD_API_KEY = (os.getenv("OVHCLOUD_API_KEY") or "").strip()
REQUESTY_API_KEY = (os.getenv("REQUESTY_API_KEY") or "").strip()
MANIFEST_API_KEY = (os.getenv("MANIFEST_API_KEY") or "").strip()
GLAMA_API_KEY = (os.getenv("GLAMA_API_KEY") or "").strip()
ZHIPU_API_KEY = (os.getenv("ZHIPU_API_KEY") or "").strip()
TEAMOROUTER_API_KEY = (os.getenv("TEAMOROUTER_API_KEY") or "").strip()

# GitHub OAuth
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "").strip()
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "").strip()
GITHUB_REDIRECT_URI = os.getenv("GITHUB_REDIRECT_URI", "https://axelr-backend.onrender.com/api/auth/github/callback")

# WebAuthn
RP_ID = os.getenv("RP_ID", "axelr.in")
RP_NAME = os.getenv("RP_NAME", "AXELR AI")
ORIGIN = os.getenv("ORIGIN", "https://axelr.in")

# JWT
SECRET_KEY = os.getenv("JWT_SECRET", "your-super-secret-key")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

# ---------- MODEL LISTS ----------
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.7-flash")
GROQ_MODELS_STR = os.getenv("GROQ_MODELS", "openai/gpt-oss-120b")
GROQ_MODELS = [m.strip() for m in GROQ_MODELS_STR.split(",") if m.strip()]
OPENROUTER_MODELS_STR = os.getenv(
    "OPENROUTER_MODELS",
    "openrouter/free,nvidia/nemotron-3.5-lightning:free,cohere/north-mini-code:free,openai/gpt-oss-20b:free"
)
OPENROUTER_MODELS = [m.strip() for m in OPENROUTER_MODELS_STR.split(",") if m.strip()]
CLOUDFLARE_MODEL = os.getenv("CLOUDFLARE_MODEL", "@cf/meta/llama-3.1-8b-instruct")
MODELSCOPE_MODELS_STR = os.getenv("MODELSCOPE_MODELS", "qwen-max,deepseek-v3,glm-4")
MODELSCOPE_MODELS = [m.strip() for m in MODELSCOPE_MODELS_STR.split(",") if m.strip()]
OLLAMA_MODELS_STR = os.getenv("OLLAMA_MODELS", "llama3.1:70b,mistral-7b,gemma2-27b")
OLLAMA_MODELS = [m.strip() for m in OLLAMA_MODELS_STR.split(",") if m.strip()]
NARA_MODELS_STR = os.getenv("NARA_MODELS", "minimax-m3,deepseek-v3")
NARA_MODELS = [m.strip() for m in NARA_MODELS_STR.split(",") if m.strip()]
MISTRAL_MODELS_STR = os.getenv("MISTRAL_MODELS", "open-mistral-7b,mistral-small-latest")
MISTRAL_MODELS = [m.strip() for m in MISTRAL_MODELS_STR.split(",") if m.strip()]
HF_MODELS_STR = os.getenv("HUGGINGFACE_MODELS", "meta-llama/Llama-3.2-3B-Instruct,mistralai/Mistral-7B-Instruct-v0.3")
HF_MODELS = [m.strip() for m in HF_MODELS_STR.split(",") if m.strip()]
GITHUB_MODEL = os.getenv("GITHUB_MODEL", "gpt-4o-mini")
OVHCLOUD_MODELS_STR = os.getenv("OVHCLOUD_MODELS", "llama-3.3-70b-instruct,mistral-7b-instruct")
OVHCLOUD_MODELS = [m.strip() for m in OVHCLOUD_MODELS_STR.split(",") if m.strip()]
SILICONFLOW_MODELS_STR = os.getenv("SILICONFLOW_MODELS", "deepseek-ai/DeepSeek-V3,Qwen/Qwen2.5-7B-Instruct")
SILICONFLOW_MODELS = [m.strip() for m in SILICONFLOW_MODELS_STR.split(",") if m.strip()]
AGNES_MODEL = os.getenv("AGNES_MODEL", "agnes-2.5-flash")
BIFROST_MODELS = ["llama3.1:70b", "mistral:7b"]
FREEGPT4_MODELS = ["gpt-4"]
BAZAARLINK_MODEL = os.getenv("BAZAARLINK_MODEL", "auto:free")
REQUESTY_MODEL = os.getenv("REQUESTY_MODEL", "auto:free")
NROUTER_MODEL = os.getenv("NROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct")
PUTER_MODEL = "gpt-3.5-turbo"
FREETHEAI_MODEL = "gpt-3.5-turbo"
OMNIGPT_MODELS = ["gpt-3.5-turbo"]
OPENDODE_MODELS = ["qwen3-coder"]
FREEFLOW_MODEL = "gpt-3.5-turbo"
QODER_MODEL = "qwen3-coder"
MANIFEST_MODEL = "auto:free"
KEYLESS_MODEL = "gpt-3.5-turbo"
GLAMA_MODEL = os.getenv("GLAMA_MODEL", "gpt-3.5-turbo")
CHUBVENUS_MODEL = "gpt-3.5-turbo"
BLOCKRUN_MODELS = ["deepseek-v4-flash"]
ANYAPI_MODEL = "claude-sonnet-4.6"
AYMO_MODELS = ["gemini-flash", "deepseek-v3.2", "qwen3"]
ZEROTWO_MODELS = ["gpt-5-mini", "gemini-flash-lite"]
AIHUBMIX_MODELS = ["gpt-5.5", "gemini-3", "glm-5.1", "kimi", "minimax"]
AISURE_MODEL = "gpt-4o"
ZHIPU_MODEL = os.getenv("ZHIPU_MODEL", "glm-4")
TEAMOROUTER_MODEL = os.getenv("TEAMOROUTER_MODEL", "teamorouter-free")
FREE_TIER_TOKEN_LIMIT = int(os.getenv("FREE_TIER_TOKEN_LIMIT", 1000000))

# ---------- HTTP CLIENT ----------
HTTP_CLIENT = httpx.AsyncClient(
    timeout=httpx.Timeout(12.0, connect=8.0, read=12.0, write=8.0),
    verify=False,
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=50)
)

async def http_post_async(url: str, headers: Dict, json_data: Dict, timeout: float = 8.0):
    try:
        resp = await HTTP_CLIENT.post(url, headers=headers, json=json_data, timeout=timeout)
        resp.raise_for_status()
        try:
            return resp.json()
        except json.JSONDecodeError:
            return {"text": resp.text}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            raise Exception(f"Quota exceeded: {e.response.text}")
        elif e.response.status_code == 402:
            raise Exception("Payment required – skipping provider")
        elif e.response.status_code in (301, 302, 303, 307, 308):
            location = e.response.headers.get('Location')
            if location:
                logger.info(f"Following redirect to {location}")
                return await http_post_async(location, headers, json_data, timeout)
        raise Exception(f"HTTP error {e.response.status_code}: {e.response.text}")
    except Exception as e:
        raise Exception(f"HTTP request failed: {e}")

async def http_post_with_retry(url: str, headers: Dict, json_data: Dict, timeout: float = 12.0, max_retries: int = 3) -> Dict:
    last_error = None
    for attempt in range(max_retries):
        try:
            resp = await HTTP_CLIENT.post(url, headers=headers, json=json_data, timeout=timeout)
            resp.raise_for_status()
            try:
                return resp.json()
            except json.JSONDecodeError:
                return {"text": resp.text}
        except (TimeoutException, ConnectError) as e:
            last_error = e
            wait_time = (2 ** attempt) + (0.1 * attempt)
            logger.warning(f"HTTP attempt {attempt+1} failed for {url}: {e}. Retrying in {wait_time:.2f}s")
            await asyncio.sleep(wait_time)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                raise Exception(f"Quota exceeded: {e.response.text}")
            elif e.response.status_code == 402:
                raise Exception("Payment required – skipping provider")
            elif e.response.status_code in (301, 302, 303, 307, 308):
                location = e.response.headers.get('Location')
                if location:
                    logger.info(f"Following redirect to {location}")
                    return await http_post_with_retry(location, headers, json_data, timeout, 1)
            raise Exception(f"HTTP error {e.response.status_code}: {e.response.text[:200]}")
        except Exception as e:
            last_error = e
            logger.warning(f"HTTP attempt {attempt+1} failed: {e}")
            await asyncio.sleep(0.5)
    raise Exception(f"HTTP request failed after {max_retries} attempts: {last_error}")

# ---------- JWT HELPERS ----------
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None

# ---------- MONGO DB ----------
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
        await users_col.create_index("githubId", unique=True, sparse=True)
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

# ---------- REDIS ----------
REDIS_URL = os.getenv("REDIS_URL")
redis_client = None

async def init_redis():
    global redis_client
    if REDIS_URL:
        try:
            redis_client = await aioredis.from_url(REDIS_URL, decode_responses=True, max_connections=10)
            await redis_client.ping()
            logger.info("Redis connected successfully")
        except Exception as e:
            logger.warning(f"Redis connection failed: {e}")
            redis_client = None
    else:
        logger.info("Redis not configured, using memory-only cache")

# ---------- CACHE & CIRCUIT BREAKER ----------
ai_cache = TTLCache(maxsize=2000, ttl=3600)
provider_failures = defaultdict(int)
provider_last_fail = defaultdict(float)
model_failures = defaultdict(int)
model_last_fail = defaultdict(float)
PROVIDER_COOLDOWN = 600
MODEL_COOLDOWN = 120

# ---------- SECURITY ----------
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

# ---------- EMAIL ----------
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

# ---------- PROVIDER FUNCTIONS ----------
# (All provider functions remain unchanged – they are correct as given)
# 1. GEMINI
async def call_gemini(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not GEMINI_API_KEY:
        raise Exception("GEMINI_API_KEY missing")
    model_name = model or GEMINI_MODEL
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temp, "maxOutputTokens": max_tokens, "topP": 0.95, "topK": 40}
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
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
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
    payload = {"messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens, "temperature": temp}
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

# 5. MODELSCOPE
async def call_modelscope(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not MODELSCOPE_API_KEY:
        raise Exception("MODELSCOPE_API_KEY missing")
    url = "https://api.modelscope.cn/v1/chat/completions"
    headers = {"Authorization": f"Bearer {MODELSCOPE_API_KEY}", "Content-Type": "application/json"}
    effective_model = model or MODELSCOPE_MODELS[0]
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 6. OLLAMA CLOUD
async def call_ollama_cloud(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not OLLAMA_API_KEY:
        raise Exception("OLLAMA_API_KEY missing")
    url = "https://api.ollama.ai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OLLAMA_API_KEY}", "Content-Type": "application/json"}
    effective_model = model or OLLAMA_MODELS[0]
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 7. NARA ROUTER
async def call_nara_router(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not NARAROUTER_API_KEY:
        raise Exception("NARAROUTER_API_KEY missing")
    url = "https://api.nrouter.io/v1/chat/completions"
    headers = {"Authorization": f"Bearer {NARAROUTER_API_KEY}", "Content-Type": "application/json"}
    effective_model = model or NARA_MODELS[0]
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 8. MISTRAL
async def call_mistral(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not MISTRAL_API_KEY:
        raise Exception("MISTRAL_API_KEY missing")
    url = "https://api.mistral.ai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {MISTRAL_API_KEY}", "Content-Type": "application/json"}
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

# 9. HUGGINGFACE
async def call_huggingface(prompt: str, max_tokens: int, temp: float, model: str) -> str:
    if not HF_API_KEY:
        raise Exception("HF_API_KEY missing")
    url = f"https://api-inference.huggingface.co/models/{model}"
    headers = {"Authorization": f"Bearer {HF_API_KEY}"}
    payload = {
        "inputs": prompt,
        "parameters": {"max_new_tokens": max_tokens, "temperature": temp, "return_full_text": False}
    }
    resp = await http_post_async(url, headers, payload)
    if isinstance(resp, dict):
        if "generated_text" in resp:
            return resp["generated_text"]
        if "text" in resp:
            return resp["text"]
    elif isinstance(resp, list):
        if resp and isinstance(resp[0], dict):
            return resp[0].get("generated_text", "")
        elif resp and isinstance(resp[0], str):
            return resp[0]
    if isinstance(resp, dict):
        for key, value in resp.items():
            if isinstance(value, str) and len(value) > 10:
                return value
    return ""

# 10. GITHUB MODELS
async def call_github_models(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not GITHUB_MODELS_TOKEN:
        raise Exception("GITHUB_MODELS_TOKEN missing")
    url = "https://models.inference.ai.azure.com/v1/chat/completions"
    params = {"api-version": "2024-05-01-preview"}
    full_url = url + "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": f"Bearer {GITHUB_MODELS_TOKEN}", "Content-Type": "application/json"}
    effective_model = model or GITHUB_MODEL
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(full_url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 11. OVHCLOUD
async def call_ovhcloud(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not OVHCLOUD_API_KEY:
        raise Exception("OVHCLOUD_API_KEY missing")
    url = "https://endpoints.ai.cloud.ovh.net/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OVHCLOUD_API_KEY}", "Content-Type": "application/json"}
    effective_model = model or OVHCLOUD_MODELS[0]
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 12. SILICONFLOW
async def call_siliconflow(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not SILICONFLOW_API_KEY:
        raise Exception("SILICONFLOW_API_KEY missing")
    url = "https://api.siliconflow.cn/v1/chat/completions"
    headers = {"Authorization": f"Bearer {SILICONFLOW_API_KEY}", "Content-Type": "application/json"}
    effective_model = model or SILICONFLOW_MODELS[0]
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 13. AGNES AI
async def call_agnes_ai(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not AGNES_API_KEY:
        raise Exception("AGNES_API_KEY missing")
    url = "https://api.agnes.ai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {AGNES_API_KEY}", "Content-Type": "application/json"}
    effective_model = model or AGNES_MODEL
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 14. BIFROST
async def call_bifrost(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = os.getenv("BIFROST_URL", "http://localhost:8080/v1/chat/completions")
    headers = {"Content-Type": "application/json"}
    effective_model = model or BIFROST_MODELS[0]
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 15. FREEGPT4-WEB-API
async def call_freegpt4_api(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = os.getenv("FREEGPT4_URL", "http://localhost:5500/")
    encoded = urllib.parse.quote(prompt)
    full_url = f"{url}?text={encoded}"
    try:
        resp = await HTTP_CLIENT.get(full_url)
        resp.raise_for_status()
        return resp.text.strip()
    except Exception as e:
        raise Exception(f"FreeGPT4 error: {e}")

# 16. BAZAARLINK
async def call_bazaarlink(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not BAZAARLINK_API_KEY:
        raise Exception("BAZAARLINK_API_KEY missing")
    url = "https://api.bazaarlink.io/v1/chat/completions"
    headers = {"Authorization": f"Bearer {BAZAARLINK_API_KEY}", "Content-Type": "application/json"}
    effective_model = model or BAZAARLINK_MODEL
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 17. REQUESTY
async def call_requesty(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not REQUESTY_API_KEY:
        raise Exception("REQUESTY_API_KEY missing")
    url = "https://api.requesty.ai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {REQUESTY_API_KEY}", "Content-Type": "application/json"}
    effective_model = model or REQUESTY_MODEL
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 18. NROUTER
async def call_nrouter(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not NROUTER_API_KEY:
        raise Exception("NROUTER_API_KEY missing")
    url = "https://api.nrouter.io/v1/chat/completions"
    headers = {"Authorization": f"Bearer {NROUTER_API_KEY}", "Content-Type": "application/json"}
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

# 19. PUTER
async def call_puter(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = "https://api.puter.com/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model or PUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 20. FREETHEAI
async def call_freetheai(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = "https://api.freetheai.com/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model or FREETHEAI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 21. OMNI GPT GATEWAY
async def call_omnigpt_gateway(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = os.getenv("OMNIGPT_URL", "https://api.omnigpt.io/v1/chat/completions")
    headers = {"Content-Type": "application/json"}
    effective_model = model or OMNIGPT_MODELS[0]
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 22. OPENDODE ZEN
async def call_opencode_zen(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = "https://api.opencode.zen/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    effective_model = model or OPENDODE_MODELS[0]
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 23. FREEFLOW
async def call_freeflow(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = "https://freeflow.llm/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model or FREEFLOW_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 24. QODER
async def call_qoder(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = "https://api.qoder.com/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    effective_model = model or QODER_MODEL
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 25. MANIFEST
async def call_manifest(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not MANIFEST_API_KEY:
        raise Exception("MANIFEST_API_KEY missing")
    url = "https://api.manifest.build/v1/chat/completions"
    headers = {"Authorization": f"Bearer {MANIFEST_API_KEY}", "Content-Type": "application/json"}
    effective_model = model or MANIFEST_MODEL
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 26. KEYLESSAI
async def call_keylessai(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = "https://api.keyless.ai/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model or KEYLESS_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 27. GLAMA
async def call_glama(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not GLAMA_API_KEY:
        raise Exception("GLAMA_API_KEY missing")
    url = "https://api.glama.ai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GLAMA_API_KEY}", "Content-Type": "application/json"}
    effective_model = model or GLAMA_MODEL
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 28. CHUB VENUS
async def call_chubvenus(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = "https://api.chub.ai/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model or CHUBVENUS_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 29. BLOCKRUN
async def call_blockrun(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = "https://api.blockrun.com/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    effective_model = model or BLOCKRUN_MODELS[0]
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 30. ANYAPI
async def call_anyapi(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not ANYAPI_API_KEY:
        raise Exception("ANYAPI_API_KEY missing")
    baseurl = os.getenv("BASEURL", "https://api.anyapi.ai/v1")
    url = f"{baseurl}/chat/completions"
    headers = {"Authorization": f"Bearer {ANYAPI_API_KEY}", "Content-Type": "application/json"}
    effective_model = model or ANYAPI_MODEL
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 31. AYMO
async def call_aymo(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = "https://api.aymo.ai/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    effective_model = model or AYMO_MODELS[0]
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 32. ZEROTWOAI
async def call_zerotwo(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = "https://api.zerotwo.ai/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    effective_model = model or ZEROTWO_MODELS[0]
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 33. AI HUB MIX
async def call_aihubmix(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = "https://api.aihubmix.com/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    effective_model = model or AIHUBMIX_MODELS[0]
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 34. AISURE
async def call_aisure(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    url = "https://api.aisure.ai/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    effective_model = model or AISURE_MODEL
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 35. ZHIPU AI
async def call_zhipu(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not ZHIPU_API_KEY:
        raise Exception("ZHIPU_API_KEY missing")
    url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    headers = {"Authorization": f"Bearer {ZHIPU_API_KEY}", "Content-Type": "application/json"}
    effective_model = model or ZHIPU_MODEL
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 36. TEAMOROUTER
async def call_teamorouter(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    if not TEAMOROUTER_API_KEY:
        raise Exception("TEAMOROUTER_API_KEY missing")
    url = "https://api.teamorouter.io/v1/chat/completions"
    headers = {"Authorization": f"Bearer {TEAMOROUTER_API_KEY}", "Content-Type": "application/json"}
    effective_model = model or TEAMOROUTER_MODEL
    payload = {
        "model": effective_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temp,
        "stream": False
    }
    resp = await http_post_async(url, headers, payload)
    return resp["choices"][0]["message"]["content"]

# 37. LOCAL FALLBACK
async def call_local_fallback(prompt: str, max_tokens: int, temp: float, model: Optional[str] = None) -> str:
    return build_local_fallback_response("general", "general", prompt)

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

# ---------- PROVIDER MAPS ----------
PROVIDER_FUNC_MAP = {
    "gemini": call_gemini,
    "groq": call_groq,
    "cloudflare": call_cloudflare,
    "openrouter": call_openrouter,
    "modelscope": call_modelscope,
    "ollama_cloud": call_ollama_cloud,
    "nara_router": call_nara_router,
    "mistral": call_mistral,
    "huggingface": call_huggingface,
    "github_models": call_github_models,
    "ovhcloud": call_ovhcloud,
    "siliconflow": call_siliconflow,
    "agnes_ai": call_agnes_ai,
    "bifrost": call_bifrost,
    "freegpt4_api": call_freegpt4_api,
    "bazaarlink": call_bazaarlink,
    "requesty": call_requesty,
    "nrouter": call_nrouter,
    "puter": call_puter,
    "freetheai": call_freetheai,
    "omnigpt_gateway": call_omnigpt_gateway,
    "opencode_zen": call_opencode_zen,
    "freeflow": call_freeflow,
    "qoder": call_qoder,
    "manifest": call_manifest,
    "keylessai": call_keylessai,
    "glama": call_glama,
    "chubvenus": call_chubvenus,
    "blockrun": call_blockrun,
    "anyapi": call_anyapi,
    "aymo": call_aymo,
    "zerotwo": call_zerotwo,
    "aihubmix": call_aihubmix,
    "aisure": call_aisure,
    "zhipu": call_zhipu,
    "teamorouter": call_teamorouter,
}

PROVIDER_KEY_CHECK = {
    "gemini": bool(GEMINI_API_KEY),
    "groq": bool(GROQ_API_KEY),
    "cloudflare": bool(CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID),
    "openrouter": bool(OPENROUTER_API_KEY),
    "modelscope": bool(MODELSCOPE_API_KEY),
    "ollama_cloud": bool(OLLAMA_API_KEY),
    "nara_router": bool(NARAROUTER_API_KEY),
    "mistral": bool(MISTRAL_API_KEY),
    "huggingface": bool(HF_API_KEY),
    "github_models": bool(GITHUB_MODELS_TOKEN),
    "ovhcloud": bool(OVHCLOUD_API_KEY),
    "siliconflow": bool(SILICONFLOW_API_KEY),
    "agnes_ai": bool(AGNES_API_KEY),
    "bifrost": True,
    "freegpt4_api": True,
    "bazaarlink": bool(BAZAARLINK_API_KEY),
    "requesty": bool(REQUESTY_API_KEY),
    "nrouter": bool(NROUTER_API_KEY),
    "puter": True,
    "freetheai": True,
    "omnigpt_gateway": True,
    "opencode_zen": True,
    "freeflow": True,
    "qoder": True,
    "manifest": bool(MANIFEST_API_KEY),
    "keylessai": True,
    "glama": bool(GLAMA_API_KEY),
    "chubvenus": True,
    "blockrun": True,
    "anyapi": bool(ANYAPI_API_KEY),
    "aymo": True,
    "zerotwo": True,
    "aihubmix": True,
    "aisure": True,
    "zhipu": bool(ZHIPU_API_KEY),
    "teamorouter": bool(TEAMOROUTER_API_KEY),
}

# ---------- PROVIDER CHAIN ----------
PROVIDER_CHAIN_ENTRIES = [
    ("gemini", call_gemini, [GEMINI_MODEL]),
    ("groq", call_groq, GROQ_MODELS),
    ("openrouter", call_openrouter, OPENROUTER_MODELS),
    ("cloudflare", call_cloudflare, [CLOUDFLARE_MODEL]),
    ("modelscope", call_modelscope, MODELSCOPE_MODELS),
    ("ollama_cloud", call_ollama_cloud, OLLAMA_MODELS),
    ("nara_router", call_nara_router, NARA_MODELS),
    ("mistral", call_mistral, MISTRAL_MODELS),
    ("huggingface", call_huggingface, HF_MODELS),
    ("github_models", call_github_models, [GITHUB_MODEL]),
    ("zhipu", call_zhipu, [ZHIPU_MODEL]),
    ("teamorouter", call_teamorouter, [TEAMOROUTER_MODEL]),
    ("ovhcloud", call_ovhcloud, OVHCLOUD_MODELS),
    ("siliconflow", call_siliconflow, SILICONFLOW_MODELS),
    ("agnes_ai", call_agnes_ai, [AGNES_MODEL]),
    ("bifrost", call_bifrost, BIFROST_MODELS),
    ("freegpt4_api", call_freegpt4_api, FREEGPT4_MODELS),
    ("bazaarlink", call_bazaarlink, [BAZAARLINK_MODEL]),
    ("requesty", call_requesty, [REQUESTY_MODEL]),
    ("nrouter", call_nrouter, [NROUTER_MODEL]),
    ("freetheai", call_freetheai, [FREETHEAI_MODEL]),
    ("omnigpt_gateway", call_omnigpt_gateway, OMNIGPT_MODELS),
    ("opencode_zen", call_opencode_zen, OPENDODE_MODELS),
    ("freeflow", call_freeflow, [FREEFLOW_MODEL]),
    ("qoder", call_qoder, [QODER_MODEL]),
    ("manifest", call_manifest, [MANIFEST_MODEL]),
    ("keylessai", call_keylessai, [KEYLESS_MODEL]),
    ("glama", call_glama, [GLAMA_MODEL]),
    ("chubvenus", call_chubvenus, [CHUBVENUS_MODEL]),
    ("blockrun", call_blockrun, BLOCKRUN_MODELS),
    ("anyapi", call_anyapi, [ANYAPI_MODEL]),
    ("aymo", call_aymo, AYMO_MODELS),
    ("zerotwo", call_zerotwo, ZEROTWO_MODELS),
    ("aihubmix", call_aihubmix, AIHUBMIX_MODELS),
    ("aisure", call_aisure, [AISURE_MODEL]),
    ("local", call_local_fallback, []),
]

PROVIDER_CHAIN = [(name, func) for name, func, _ in PROVIDER_CHAIN_ENTRIES]
PROVIDER_MODELS = {name: models for name, _, models in PROVIDER_CHAIN_ENTRIES}
provider_health = {p: {"status": "unknown", "last_check": None, "daily_usage": 0} for p, _ in PROVIDER_CHAIN}

# ---------- MASTER PROMPT ----------
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

# ---------- WORKSPACE PRIORITY ----------
WORKSPACE_PRIORITY = {
    "data": [
        "gemini", "modelscope", "groq", "openrouter", "ollama_cloud", "nara_router",
        "mistral", "ovhcloud", "siliconflow", "zhipu", "teamorouter", "nrouter",
        "bazaarlink", "requesty", "qoder", "manifest",
        "keylessai", "anyapi", "aymo", "zerotwo", "aihubmix", "aisure"
    ],
    "design": [
        "cloudflare", "groq", "gemini", "openrouter", "modelscope", "nara_router",
        "agnes_ai", "siliconflow", "zhipu", "teamorouter", "bifrost",
        "freegpt4_api", "ovhcloud", "nrouter", "puter", "omnigpt_gateway",
        "opencode_zen", "qoder", "keylessai", "glama", "chubvenus", "blockrun", "anyapi"
    ],
    "general": [
        "gemini", "modelscope", "groq", "openrouter", "ollama_cloud", "nara_router",
        "mistral", "huggingface", "github_models", "zhipu", "teamorouter",
        "ovhcloud", "siliconflow", "nrouter", "bazaarlink", "requesty",
        "qoder", "freeflow", "manifest", "keylessai", "glama", "chubvenus",
        "anyapi", "aymo", "zerotwo", "aihubmix", "aisure"
    ],
    "prompt": [
        "gemini", "openrouter", "modelscope", "groq", "nara_router", "ollama_cloud",
        "zhipu", "teamorouter", "requesty", "bazaarlink"
    ],
    "touch_fix": [
        "groq", "mistral", "github_models", "zhipu", "teamorouter", "nara_router",
        "ovhcloud", "qoder", "opencode_zen"
    ],
}

def get_provider_order(workspace: str) -> List[str]:
    provider_names = [name for name, _ in PROVIDER_CHAIN if name != "local"]
    priority = WORKSPACE_PRIORITY.get(workspace, WORKSPACE_PRIORITY["general"])
    ordered = []
    for name in priority:
        if name in provider_names and name not in ordered:
            ordered.append(name)
    for name in provider_names:
        if name not in ordered:
            ordered.append(name)
    ordered.append("local")
    return ordered

# ---------- AI ROUTER (sequential) ----------
async def route_ai_request(
    workspace: str,
    task_type: str,
    prompt: str,
    history: Optional[List[Dict]],
    files: Optional[List[Dict]],
    max_tokens: int,
    temp: float,
    tier: str,
    user: Optional[Dict] = None
) -> Dict[str, Any]:
    start = time.time()
    if detect_manipulation(prompt):
        return {"success": False, "text": "⚠️ Manipulation attempt detected.", "provider": "security", "model_used": "filter", "tokens_used": 0, "latency_ms": 0}
    if contains_explicit(prompt):
        return {"success": False, "text": "🚫 Content policy violation.", "provider": "security", "model_used": "blocked", "tokens_used": 0, "latency_ms": 0}

    history_text = ""
    if history:
        recent = []
        for msg in history[-4:]:
            if not isinstance(msg, dict): continue
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

    normalized_prompt = ' '.join(prompt.lower().split())
    cache_key = hashlib.sha256(f"{workspace}:{task_type}:{normalized_prompt}:{history_text}".encode()).hexdigest()
    if cache_key in ai_cache:
        cached = ai_cache[cache_key]
        return {**cached, "cached": True}

    response_text = None
    provider_used = None
    model_used = None
    last_error = None

    provider_order = get_provider_order(workspace)
    provider_func_map = dict(PROVIDER_CHAIN)

    for provider_name in provider_order:
        if provider_name == "local":
            continue
        func = provider_func_map.get(provider_name)
        if not func:
            continue

        # Skip if mandatory keys missing
        if provider_name == "gemini" and not GEMINI_API_KEY: continue
        if provider_name == "groq" and not GROQ_API_KEY: continue
        if provider_name == "cloudflare" and (not CLOUDFLARE_API_TOKEN or not CLOUDFLARE_ACCOUNT_ID): continue
        if provider_name == "openrouter" and not OPENROUTER_API_KEY: continue
        if provider_name == "modelscope" and not MODELSCOPE_API_KEY: continue
        if provider_name == "ollama_cloud" and not OLLAMA_API_KEY: continue
        if provider_name == "nara_router" and not NARAROUTER_API_KEY: continue
        if provider_name == "mistral" and not MISTRAL_API_KEY: continue
        if provider_name == "huggingface" and not HF_API_KEY: continue
        if provider_name == "github_models" and not GITHUB_MODELS_TOKEN: continue
        if provider_name == "zhipu" and not ZHIPU_API_KEY: continue
        if provider_name == "teamorouter" and not TEAMOROUTER_API_KEY: continue
        if provider_name == "ovhcloud" and not OVHCLOUD_API_KEY: continue
        if provider_name == "siliconflow" and not SILICONFLOW_API_KEY: continue
        if provider_name == "agnes_ai" and not AGNES_API_KEY: continue
        if provider_name == "bazaarlink" and not BAZAARLINK_API_KEY: continue
        if provider_name == "requesty" and not REQUESTY_API_KEY: continue
        if provider_name == "nrouter" and not NROUTER_API_KEY: continue
        if provider_name == "glama" and not GLAMA_API_KEY: continue
        if provider_name == "anyapi" and not ANYAPI_API_KEY: continue
        if provider_name == "manifest" and not MANIFEST_API_KEY: continue
        if provider_name == "puter" and (user is None or not user.get("puter_enabled", False)):
            continue

        # Circuit breaker
        if provider_failures[provider_name] >= 3 and time.time() - provider_last_fail[provider_name] < PROVIDER_COOLDOWN:
            logger.warning(f"Skipping {provider_name} (circuit breaker)")
            continue

        models = PROVIDER_MODELS.get(provider_name, [])
        if not models:
            continue

        provider_success = False
        for model in models:
            model_key = (provider_name, model)
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
                        break
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
                break

        if provider_success:
            break
        else:
            provider_failures[provider_name] += 1
            provider_last_fail[provider_name] = time.time()
            logger.warning(f"All models for provider {provider_name} failed; marking cooldown")

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

# ---------- PARALLEL ROUTER (guest & faster) ----------
async def route_ai_request_parallel(
    workspace: str,
    task_type: str,
    prompt: str,
    history: Optional[List[Dict]],
    files: Optional[List[Dict]],
    max_tokens: int,
    temp: float,
    tier: str,
    user: Optional[Dict] = None
) -> Dict[str, Any]:
    start = time.time()
    if detect_manipulation(prompt):
        return {"success": False, "text": "⚠️ Manipulation attempt detected.", "provider": "security", "model_used": "filter", "tokens_used": 0, "latency_ms": 0}
    if contains_explicit(prompt):
        return {"success": False, "text": "🚫 Content policy violation.", "provider": "security", "model_used": "blocked", "tokens_used": 0, "latency_ms": 0}

    history_text = ""
    if history:
        recent = []
        for msg in history[-4:]:
            if not isinstance(msg, dict): continue
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

    normalized_prompt = ' '.join(prompt.lower().split())
    cache_key = hashlib.sha256(f"{workspace}:{task_type}:{normalized_prompt}:{history_text}".encode()).hexdigest()
    if cache_key in ai_cache:
        cached = ai_cache[cache_key]
        return {**cached, "cached": True}

    provider_order = get_provider_order(workspace)
    candidate_providers = []
    for p in provider_order:
        if p == "local": continue
        if not _is_provider_ready(p, user): continue
        candidate_providers.append(p)
        if len(candidate_providers) >= 3: break

    if not candidate_providers:
        response_text = build_local_fallback_response(workspace, task_type, prompt)
        return {"success": True, "text": response_text, "provider": "local", "model_used": "local-fallback", "tokens_used": len(response_text.split()), "latency_ms": 0}

    provider_func_map = dict(PROVIDER_CHAIN)
    tasks = []
    for p in candidate_providers:
        func = provider_func_map.get(p)
        if not func: continue
        models = PROVIDER_MODELS.get(p, [])
        if not models: continue
        model = models[0]
        tasks.append(_execute_provider_with_timeout(p, func, full_prompt, model, max_tokens, temp, user))

    done, pending = await asyncio.wait(tasks, timeout=6.0, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()

    best_response = None
    best_score = -1
    for task in done:
        try:
            result = task.result()
            if result and result.get("text"):
                score = _compute_quality_score(result["text"], workspace)
                if score > best_score:
                    best_score = score
                    best_response = result
        except Exception as e:
            logger.warning(f"Provider task failed: {e}")

    if best_response and best_score > 20:
        response_text = strip_fluff(best_response["text"])
        latency = (time.time() - start) * 1000
        result = {
            "success": True,
            "text": response_text,
            "provider": best_response.get("provider", "unknown"),
            "model_used": best_response.get("model", "unknown"),
            "tokens_used": len(response_text.split()),
            "latency_ms": round(latency, 2)
        }
        ai_cache[cache_key] = result
        return result

    response_text = await _sequential_provider_fallback(workspace, task_type, full_prompt, max_tokens, temp, user)
    latency = (time.time() - start) * 1000
    result = {
        "success": True,
        "text": strip_fluff(response_text),
        "provider": "fallback",
        "model_used": "sequential",
        "tokens_used": len(response_text.split()),
        "latency_ms": round(latency, 2)
    }
    ai_cache[cache_key] = result
    return result

async def _execute_provider_with_timeout(provider_name, func, prompt, model, max_tokens, temp, user):
    try:
        if provider_name == "puter" and (user is None or not user.get("puter_enabled", False)):
            return {"text": "", "provider": provider_name, "model": model, "error": "Puter disabled"}
        response = await asyncio.wait_for(func(prompt, max_tokens, temp, model), timeout=4.0)
        if response and len(response.strip()) > 10:
            return {"text": response, "provider": provider_name, "model": model}
        return {"text": "", "provider": provider_name, "model": model, "error": "Empty response"}
    except asyncio.TimeoutError:
        return {"text": "", "provider": provider_name, "model": model, "error": "Timeout"}
    except Exception as e:
        return {"text": "", "provider": provider_name, "model": model, "error": str(e)}

def _is_provider_ready(provider_name: str, user: Optional[Dict] = None) -> bool:
    if provider_name == "gemini" and not GEMINI_API_KEY: return False
    if provider_name == "groq" and not GROQ_API_KEY: return False
    if provider_name == "cloudflare" and (not CLOUDFLARE_API_TOKEN or not CLOUDFLARE_ACCOUNT_ID): return False
    if provider_name == "openrouter" and not OPENROUTER_API_KEY: return False
    if provider_name == "modelscope" and not MODELSCOPE_API_KEY: return False
    if provider_name == "ollama_cloud" and not OLLAMA_API_KEY: return False
    if provider_name == "nara_router" and not NARAROUTER_API_KEY: return False
    if provider_name == "mistral" and not MISTRAL_API_KEY: return False
    if provider_name == "huggingface" and not HF_API_KEY: return False
    if provider_name == "github_models" and not GITHUB_MODELS_TOKEN: return False
    if provider_name == "zhipu" and not ZHIPU_API_KEY: return False
    if provider_name == "teamorouter" and not TEAMOROUTER_API_KEY: return False
    if provider_name == "ovhcloud" and not OVHCLOUD_API_KEY: return False
    if provider_name == "siliconflow" and not SILICONFLOW_API_KEY: return False
    if provider_name == "agnes_ai" and not AGNES_API_KEY: return False
    if provider_name == "bazaarlink" and not BAZAARLINK_API_KEY: return False
    if provider_name == "requesty" and not REQUESTY_API_KEY: return False
    if provider_name == "nrouter" and not NROUTER_API_KEY: return False
    if provider_name == "glama" and not GLAMA_API_KEY: return False
    if provider_name == "anyapi" and not ANYAPI_API_KEY: return False
    if provider_name == "manifest" and not MANIFEST_API_KEY: return False
    if provider_name == "puter" and (user is None or not user.get("puter_enabled", False)): return False
    if provider_failures[provider_name] >= 3 and time.time() - provider_last_fail[provider_name] < PROVIDER_COOLDOWN:
        return False
    return True

def _compute_quality_score(text: str, workspace: str) -> int:
    if not text: return 0
    score = 0
    if len(text) >= 20: score += 10
    if len(text) >= 100: score += 10
    if len(text) >= 500: score += 10
    if "```" in text: score += 15
    if workspace == "data" and "[JSON-DATA]" in text: score += 20
    hallucination_patterns = [r"I am (not|unable)", r"I don't have access", r"I apologize", r"as an AI"]
    for pattern in hallucination_patterns:
        if not re.search(pattern, text, re.IGNORECASE): score += 5
    if re.search(r"\d+", text): score += 10
    if re.search(r"(step|first|then|finally)", text, re.IGNORECASE): score += 10
    return min(score, 100)

async def _sequential_provider_fallback(workspace: str, task_type: str, prompt: str, max_tokens: int, temp: float, user: Optional[Dict] = None) -> str:
    provider_order = get_provider_order(workspace)
    provider_func_map = dict(PROVIDER_CHAIN)
    for provider_name in provider_order:
        if provider_name == "local": continue
        func = provider_func_map.get(provider_name)
        if not func: continue
        if not _is_provider_ready(provider_name, user): continue
        models = PROVIDER_MODELS.get(provider_name, [])
        for model in models:
            try:
                response = await asyncio.wait_for(func(prompt, max_tokens, temp, model), timeout=8.0)
                if response and len(response.strip()) > 10:
                    return response
            except Exception as e:
                logger.debug(f"{provider_name}/{model} failed: {e}")
                continue
    return build_local_fallback_response(workspace, task_type, prompt)

# ---------- PROVIDER VALIDATION (startup) ----------
async def validate_all_providers():
    """Test each provider with a simple prompt and log health status."""
    test_prompt = "Say OK"
    results = {}
    for name, func in PROVIDER_CHAIN:
        if name == "local":
            continue
        if not PROVIDER_KEY_CHECK.get(name, False):
            results[name] = "skipped (no key or not configured)"
            continue
        models = PROVIDER_MODELS.get(name, [])
        if not models:
            results[name] = "skipped (no models)"
            continue
        try:
            start = time.time()
            resp = await asyncio.wait_for(func(test_prompt, 5, 0.0, models[0]), timeout=5.0)
            latency = (time.time() - start) * 1000
            if resp and len(resp.strip()) > 0:
                results[name] = f"healthy ({latency:.0f}ms)"
            else:
                results[name] = "unhealthy (empty response)"
        except Exception as e:
            results[name] = f"error: {str(e)[:80]}"
    logger.info("Provider validation results: " + json.dumps(results, indent=2))
    return results

# ---------- AUTH ----------
security = HTTPBearer()

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    if not db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")
    token = credentials.credentials

    # Try Google OAuth
    try:
        idinfo = id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)
        if idinfo['iss'] not in ['accounts.google.com', 'https://accounts.google.com']:
            raise HTTPException(status_code=401, detail="Invalid issuer")
        user_doc = await users_col.find_one({"googleId": idinfo['sub']})
        if not user_doc:
            user_doc = await _create_user_from_google(idinfo)
        else:
            user_doc = await _reset_quotas_if_needed(user_doc)
        return user_doc
    except Exception as e:
        logger.debug(f"Google OAuth failed: {e}")

    # Try JWT
    try:
        payload = decode_token(token)
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid token")
        user_doc = await users_col.find_one({"email": payload.get("sub")})
        if not user_doc:
            raise HTTPException(status_code=401, detail="User not found")
        return user_doc
    except Exception as e:
        logger.error(f"Auth failed: {e}")
        raise HTTPException(status_code=401, detail="Invalid or expired token")

async def _create_user_from_google(idinfo: dict) -> dict:
    is_admin = idinfo['email'] == ADMIN_EMAIL
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
        "dailyMistralQuota": 0,
        "dailyGithubQuota": 0,
        "dailyNrouterQuota": 0,
        "dailyTextCortexQuota": 0,
        "dailyModelscopeQuota": 0,
        "dailyOllamaQuota": 0,
        "dailyNaraQuota": 0,
        "dailyOvhcloudQuota": 0,
        "dailySiliconflowQuota": 0,
        "dailyAgnesQuota": 0,
        "dailyBazaarlinkQuota": 0,
        "dailyRequestyQuota": 0,
        "dailyManifestQuota": 0,
        "dailyZhipuQuota": 0,
        "dailyTeamorouterQuota": 0,
        "lastAiQuotaReset": datetime.utcnow(),
        "puter_enabled": False,
        "preferences": {"defaultWorkspace": "data"}
    }
    result = await users_col.insert_one(new_user)
    user_doc = await users_col.find_one({"_id": result.inserted_id})
    logger.info(f"New user created: {idinfo['email']}")
    return user_doc

async def _reset_quotas_if_needed(user_doc: dict) -> dict:
    now = datetime.utcnow()
    today = datetime(now.year, now.month, now.day)
    last_reset = user_doc.get("quotas", {}).get("lastQuotaReset")
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
                    "dailyMistralQuota": 0,
                    "dailyGithubQuota": 0,
                    "dailyNrouterQuota": 0,
                    "dailyTextCortexQuota": 0,
                    "dailyModelscopeQuota": 0,
                    "dailyOllamaQuota": 0,
                    "dailyNaraQuota": 0,
                    "dailyOvhcloudQuota": 0,
                    "dailySiliconflowQuota": 0,
                    "dailyAgnesQuota": 0,
                    "dailyBazaarlinkQuota": 0,
                    "dailyRequestyQuota": 0,
                    "dailyManifestQuota": 0,
                    "dailyZhipuQuota": 0,
                    "dailyTeamorouterQuota": 0,
                    "lastAiQuotaReset": datetime.utcnow()
                }}
            )
            user_doc = await users_col.find_one({"_id": user_doc["_id"]})
    return user_doc

# ============================================================
# AUTH ROUTES (Google, GitHub, Email, Passkey)
# ============================================================

# ---------- GITHUB OAUTH ----------
@app.get("/api/auth/github")
async def github_login():
    if not GITHUB_CLIENT_ID:
        raise HTTPException(status_code=503, detail="GitHub OAuth not configured")
    params = {
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": GITHUB_REDIRECT_URI,
        "scope": "user:email",
        "response_type": "code",
        "state": secrets.token_urlsafe(16)
    }
    url = f"https://github.com/login/oauth/authorize?{urllib.parse.urlencode(params)}"
    return RedirectResponse(url=url)

@app.get("/api/auth/github/callback")
async def github_callback(code: str, state: Optional[str] = None):
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        raise HTTPException(status_code=503, detail="GitHub OAuth not configured")
    token_url = "https://github.com/login/oauth/access_token"
    headers = {"Accept": "application/json"}
    data = {
        "client_id": GITHUB_CLIENT_ID,
        "client_secret": GITHUB_CLIENT_SECRET,
        "code": code,
        "redirect_uri": GITHUB_REDIRECT_URI,
        "state": state
    }
    try:
        resp = await HTTP_CLIENT.post(token_url, headers=headers, json=data, timeout=10.0)
        resp.raise_for_status()
        token_data = resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="Failed to obtain access token")
    except Exception as e:
        logger.error(f"GitHub token exchange failed: {e}")
        raise HTTPException(status_code=503, detail="GitHub authentication service unavailable")

    user_info_url = "https://api.github.com/user"
    user_headers = {"Authorization": f"Bearer {access_token}"}
    try:
        user_resp = await HTTP_CLIENT.get(user_info_url, headers=user_headers, timeout=10.0)
        user_resp.raise_for_status()
        user_data = user_resp.json()
    except Exception as e:
        logger.error(f"GitHub user info fetch failed: {e}")
        raise HTTPException(status_code=503, detail="Failed to fetch GitHub profile")

    email_url = "https://api.github.com/user/emails"
    try:
        email_resp = await HTTP_CLIENT.get(email_url, headers=user_headers, timeout=10.0)
        email_resp.raise_for_status()
        emails = email_resp.json()
        primary_email = next((e["email"] for e in emails if e.get("primary")), user_data.get("email"))
    except Exception:
        primary_email = user_data.get("email") or f"{user_data['id']}@github.user"

    if not db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")

    github_id = str(user_data["id"])
    user_doc = await users_col.find_one({"githubId": github_id})
    if not user_doc:
        new_user = {
            "githubId": github_id,
            "email": primary_email,
            "displayName": user_data.get("name") or user_data.get("login") or primary_email,
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
            "isAdmin": primary_email == ADMIN_EMAIL,
            "puter_enabled": False,
            "preferences": {"defaultWorkspace": "data"},
            "createdAt": datetime.utcnow()
        }
        result = await users_col.insert_one(new_user)
        user_doc = await users_col.find_one({"_id": result.inserted_id})
        logger.info(f"New GitHub user created: {primary_email}")
    else:
        await users_col.update_one({"_id": user_doc["_id"]}, {"$set": {"lastUsageDate": datetime.utcnow()}})
        user_doc = await users_col.find_one({"_id": user_doc["_id"]})
        user_doc = await _reset_quotas_if_needed(user_doc)

    token = create_access_token({"sub": user_doc["email"]})
    frontend_url = "https://axelr.in"
    redirect_url = f"{frontend_url}/?auth=github&token={token}"
    return RedirectResponse(url=redirect_url)

# ---------- WEBAUTHN (optional) ----------
WEBAUTHN_AVAILABLE = False
webauthn_challenges = {}

try:
    from webauthn import generate_registration_options, verify_registration_response
    from webauthn import generate_authentication_options, verify_authentication_response
    from webauthn.helpers.structs import (
        RegistrationCredential, AuthenticationCredential,
        AuthenticatorSelectionCriteria, UserVerificationRequirement,
        PublicKeyCredentialDescriptor
    )
    WEBAUTHN_AVAILABLE = True
    logger.info("WebAuthn loaded successfully – passkey features enabled")
except ImportError as e:
    logger.warning(f"WebAuthn module not available – passkey features disabled: {e}")
except Exception as e:
    logger.warning(f"WebAuthn initialization failed – passkey features disabled: {e}")

if WEBAUTHN_AVAILABLE:
    class WebAuthnRegistrationBeginRequest(BaseModel):
        email: str

    class WebAuthnRegistrationFinishRequest(BaseModel):
        email: str
        credential: dict

    class WebAuthnLoginBeginRequest(BaseModel):
        email: str

    class WebAuthnLoginFinishRequest(BaseModel):
        email: str
        credential: dict

    async def get_user_for_webauthn(email: str) -> dict:
        if not db_available:
            raise HTTPException(status_code=503, detail="Database unavailable")
        user = await users_col.find_one({"email": email})
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return user

    async def store_webauthn_credential(user_id: str, credential_id: bytes, public_key: bytes, sign_count: int):
        await users_col.update_one(
            {"_id": user_id},
            {"$push": {"webauthnCredentials": {
                "credentialId": credential_id.hex(),
                "publicKey": public_key.hex(),
                "signCount": sign_count,
                "transports": []
            }}}
        )

    async def get_webauthn_credential(user_id: str, credential_id: bytes):
        user = await users_col.find_one({"_id": user_id})
        if not user:
            return None
        for cred in user.get("webauthnCredentials", []):
            if cred["credentialId"] == credential_id.hex():
                return cred
        return None

    @app.post("/api/auth/webauthn/register/begin")
    async def webauthn_register_begin(data: WebAuthnRegistrationBeginRequest):
        user = await get_user_for_webauthn(data.email)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        options = generate_registration_options(
            rp_id=RP_ID,
            rp_name=RP_NAME,
            user_id=user["_id"].binary,
            user_name=user.get("displayName", data.email),
            user_display_name=user.get("displayName", data.email),
            authenticator_selection=AuthenticatorSelectionCriteria(
                user_verification=UserVerificationRequirement.PREFERRED,
                resident_key="preferred"
            ),
        )
        webauthn_challenges[options.challenge] = data.email
        return options.model_dump()

    @app.post("/api/auth/webauthn/register/finish")
    async def webauthn_register_finish(data: WebAuthnRegistrationFinishRequest):
        user = await get_user_for_webauthn(data.email)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        challenge = webauthn_challenges.pop(data.credential.get("challenge"), None)
        if not challenge:
            raise HTTPException(status_code=400, detail="Invalid or expired challenge")
        try:
            credential = RegistrationCredential(**data.credential)
            verification = verify_registration_response(
                credential=credential,
                expected_challenge=challenge,
                expected_rp_id=RP_ID,
                expected_origin=ORIGIN,
            )
        except Exception as e:
            logger.error(f"WebAuthn registration verification failed: {e}")
            raise HTTPException(status_code=400, detail="Registration verification failed")
        await store_webauthn_credential(user["_id"], verification.credential_id, verification.credential_public_key, verification.sign_count)
        return {"success": True, "message": "Passkey registered successfully"}

    @app.post("/api/auth/webauthn/login/begin")
    async def webauthn_login_begin(data: WebAuthnLoginBeginRequest):
        user = await get_user_for_webauthn(data.email)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        credentials = user.get("webauthnCredentials", [])
        if not credentials:
            raise HTTPException(status_code=400, detail="No passkeys registered for this user")
        allowed_credentials = [PublicKeyCredentialDescriptor(id=bytes.fromhex(c["credentialId"])) for c in credentials]
        options = generate_authentication_options(
            rp_id=RP_ID,
            challenge=secrets.token_urlsafe(32),
            allow_credentials=allowed_credentials,
            user_verification=UserVerificationRequirement.PREFERRED,
        )
        webauthn_challenges[options.challenge] = data.email
        return options.model_dump()

@app.get("/api/ready")
async def readiness():
    if not db_available:
        raise HTTPException(503, "Database unavailable")
    # Check if any provider is healthy (optional)
    if not any(h["status"] == "healthy" for h in provider_health.values()):
        raise HTTPException(503, "No AI provider available")
    return {"status": "ready"}
    @app.post("/api/auth/webauthn/login/finish")
    async def webauthn_login_finish(data: WebAuthnLoginFinishRequest):
        user = await get_user_for_webauthn(data.email)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        challenge = webauthn_challenges.pop(data.credential.get("challenge"), None)
        if not challenge:
            raise HTTPException(status_code=400, detail="Invalid or expired challenge")
        try:
            credential = AuthenticationCredential(**data.credential)
            stored_cred = await get_webauthn_credential(user["_id"], bytes.fromhex(credential.id))
            if not stored_cred:
                raise HTTPException(status_code=400, detail="Credential not found")
            verification = verify_authentication_response(
                credential=credential,
                expected_challenge=challenge,
                expected_rp_id=RP_ID,
                expected_origin=ORIGIN,
                credential_public_key=bytes.fromhex(stored_cred["publicKey"]),
                credential_current_sign_count=stored_cred["signCount"],
            )
            await users_col.update_one(
                {"_id": user["_id"], "webauthnCredentials.credentialId": credential.id},
                {"$set": {"webauthnCredentials.$.signCount": verification.new_sign_count}}
            )
        except Exception as e:
            logger.error(f"WebAuthn login verification failed: {e}")
            raise HTTPException(status_code=400, detail="Authentication failed")
        token = create_access_token({"sub": user["email"]})
        return {"success": True, "token": token}
# ---------- EMAIL / PASSWORD AUTH ----------
class EmailLoginRequest(BaseModel):
    email: str
    password: str

@app.post("/api/auth/email")
async def email_login(data: EmailLoginRequest):
    if not db_available:
        raise HTTPException(503, "Database unavailable")
    user = await users_col.find_one({"email": data.email})
    if not user:
        raise HTTPException(401, "User not found")
    if not verify_password(data.password, user.get("password_hash", "")):
        raise HTTPException(401, "Invalid password")
    token = create_access_token({"sub": user["email"]})
    return {"success": True, "token": token}

@app.post("/api/auth/email/register")
async def email_register(data: EmailLoginRequest):
    if not db_available:
        raise HTTPException(503, "Database unavailable")
    existing = await users_col.find_one({"email": data.email})
    if existing:
        raise HTTPException(400, "Email already registered")
    hashed = hash_password(data.password)
    new_user = {
        "email": data.email,
        "displayName": data.email.split('@')[0],
        "password_hash": hashed,
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
        "isAdmin": data.email == ADMIN_EMAIL,
        "puter_enabled": False,
        "preferences": {"defaultWorkspace": "data"},
        "createdAt": datetime.utcnow()
    }
    result = await users_col.insert_one(new_user)
    user_doc = await users_col.find_one({"_id": result.inserted_id})
    token = create_access_token({"sub": user_doc["email"]})
    return {"success": True, "token": token}

# ---------- FASTAPI APP ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await init_redis()
    if not db_available:
        logger.critical("MongoDB is not available. The application will run in degraded mode.")
    else:
        logger.info("Unified Fortress online")
    app.state.start_time = time.time()
    # Validate all providers on startup (async task)
    asyncio.create_task(validate_all_providers())
    yield
    if client:
        client.close()
        logger.info("Shutdown complete")

app = FastAPI(title="AXELR Unified", version="24.0", lifespan=lifespan)

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

# ---------- RATE LIMITING ----------
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

# ---------- GUEST SESSIONS ----------
guest_sessions = {}

class GuestSession(BaseModel):
    sessionId: str
    expiresIn: int

@app.post("/api/guest/session")
async def create_guest_session():
    session_id = secrets.token_urlsafe(16)
    guest_sessions[session_id] = {
        "expires": datetime.utcnow() + timedelta(hours=1),
        "messages": [],
        "structured": None,
        "created_at": datetime.utcnow()
    }
    return {"sessionId": session_id, "expiresIn": 3600}

@app.get("/api/guest/session/{session_id}")
async def get_guest_session(session_id: str):
    if session_id not in guest_sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    session = guest_sessions[session_id]
    if datetime.utcnow() > session["expires"]:
        del guest_sessions[session_id]
        raise HTTPException(status_code=404, detail="Session expired")
    return {
        "sessionId": session_id,
        "expiresIn": int((session["expires"] - datetime.utcnow()).total_seconds()),
        "messageCount": len(session.get("messages", [])),
        "hasStructured": session.get("structured") is not None
    }

@app.post("/api/guest/extract")
async def guest_extract(
    command: str = Form(...),
    workspace: str = Form("data"),
    sessionId: Optional[str] = Form(None),
    files: List[UploadFile] = File([])
):
    if not sessionId or sessionId not in guest_sessions:
        new_session = secrets.token_urlsafe(16)
        guest_sessions[new_session] = {
            "expires": datetime.utcnow() + timedelta(hours=1),
            "messages": [],
            "structured": None,
            "created_at": datetime.utcnow()
        }
        sessionId = new_session

    session = guest_sessions[sessionId]
    if datetime.utcnow() > session["expires"]:
        del guest_sessions[sessionId]
        raise HTTPException(status_code=403, detail="Session expired")

    message_count = len(session.get("messages", []))
    if message_count >= 5:
        raise HTTPException(status_code=403, detail={
            "code": "GUEST_LIMIT_REACHED",
            "message": "Guest sessions limited to 5 messages. Sign in for unlimited access.",
            "limit": 5,
            "used": message_count
        })

    valid_files = []
    for f in files:
        if is_allowed_file(workspace, f.filename, f.content_type or ""):
            valid_files.append(f)

    file_contents = []
    for f in valid_files:
        content_bytes = await f.read()
        b64 = base64.b64encode(content_bytes).decode('utf-8')
        file_contents.append({
            "filename": f.filename,
            "mimetype": f.content_type or "application/octet-stream",
            "content_base64": b64
        })

    if workspace == "data":
        task_type = "extraction"
    elif workspace == "design":
        task_type = "frontend"
    else:
        task_type = "structuring"

    ai_result = await route_ai_request_parallel(
        workspace=workspace,
        task_type=task_type,
        prompt=command,
        history=session.get("messages", []),
        files=file_contents,
        max_tokens=2048,
        temp=0.2,
        tier="free",
        user=None
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

    session["messages"].append({
        "role": "user",
        "text": command,
        "attachedFiles": [f.filename for f in valid_files]
    })
    session["messages"].append({
        "role": "model",
        "text": ai_text,
        "variants": [ai_text],
        "activeVariant": 0,
        "canRegenerate": True,
        "createdAt": datetime.utcnow().isoformat()
    })
    session["structured"] = structured

    return {
        "success": True,
        "text": ai_text,
        "sessionId": sessionId,
        "structuredData": structured,
        "filename": f"Export_{datetime.utcnow().strftime('%Y%m%d')}.csv",
        "provider": provider,
        "model": model_used,
        "remaining": 5 - len(session.get("messages", [])) // 2
    }

# ---------- ENDPOINTS ----------
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

@app.get("/api/v1/diagnose")
async def diagnose_providers():
    results = {}
    test_prompt = "Say 'OK'"
    tasks = {}
    for provider_name, func in PROVIDER_CHAIN:
        if provider_name == "local":
            continue
        if not PROVIDER_KEY_CHECK.get(provider_name, False):
            results[provider_name] = {"status": "skipped", "reason": "Not configured"}
            continue
        models = PROVIDER_MODELS.get(provider_name, [])
        if not models:
            results[provider_name] = {"status": "skipped", "reason": "No models"}
            continue
        model = models[0]
        tasks[provider_name] = asyncio.create_task(_probe_provider(provider_name, func, test_prompt, model))
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
        if resp and len(resp.strip()) > 0:
            return {"status": "healthy", "latency_ms": round(latency, 2)}
        else:
            return {"status": "unhealthy", "response": resp[:50] if resp else "empty"}
    except Exception as e:
        return {"status": "error", "error": str(e)[:100]}

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
        "dailyMistralQuota": user.get("dailyMistralQuota", 0),
        "dailyGithubQuota": user.get("dailyGithubQuota", 0),
        "dailyNrouterQuota": user.get("dailyNrouterQuota", 0),
        "dailyTextCortexQuota": user.get("dailyTextCortexQuota", 0),
        "puter_enabled": user.get("puter_enabled", False),
        "preferences": user.get("preferences", {}),
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

@app.get("/api/test-email")
async def test_email(user: dict = Depends(get_current_user)):
    if not user.get("isAdmin") or user.get("email") != ADMIN_EMAIL:
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
        tier=tier,
        user=user
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

class RefactorRequest(BaseModel):
    code: str
    task_type: Optional[str] = "refactor"

@app.post("/api/refactor")
async def refactor_code(data: RefactorRequest, user: dict = Depends(get_current_user)):
    if not data.code:
        raise HTTPException(status_code=400, detail="No code provided")
    prompt = f"""Refactor the following code for better readability, performance, and accessibility.
Return only the refactored code, without any explanation.

```html
{data.code}
```"""
    ai_result = await route_ai_request(
        workspace="design",
        task_type="refactor",
        prompt=prompt,
        history=[],
        files=[],
        max_tokens=2048,
        temp=0.2,
        tier=user.get("tier", "free"),
        user=user
    )
    if not ai_result.get("success"):
        raise HTTPException(status_code=503, detail="AI service unavailable")
    refactored_code = ai_result["text"]
    code_match = re.search(r"```(?:html|javascript|css)?\s*([\s\S]*?)```", refactored_code, re.DOTALL)
    if code_match:
        refactored_code = code_match.group(1).strip()
    return {"success": True, "refactored_code": refactored_code}

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
        tier=tier,
        user=user
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
        if provider == "gemini":
            update_query["$inc"]["dailyGeminiQuota"] = 1
        elif provider == "groq":
            update_query["$inc"]["dailyGroqQuota"] = 1
        elif provider == "cloudflare":
            update_query["$inc"]["dailyCloudflareQuota"] = 1
        elif provider == "openrouter":
            update_query["$inc"]["dailyOpenRouterQuota"] = 1
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
                "canRegenerate": True,
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
                    "canRegenerate": True,
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
        tier=user.get("tier", "free"),
        user=user
    )
    if not ai_result.get("success"):
        raise HTTPException(status_code=503, detail="AI service unavailable")
    fixed_code = ai_result["text"]
    code_match = re.search(r"```(?:html|javascript|css)?\s*([\s\S]*?)```", fixed_code, re.DOTALL)
    if code_match:
        fixed_code = code_match.group(1).strip()
    return {"success": True, "fixed_code": fixed_code}

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

class CodeRequest(BaseModel):
    code: str

class TextRequest(BaseModel):
    text: str

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

    pipeline_usage = [{"$group": {"_id": None, "totalQueries": {"$sum": "$dailyUsage"}, "totalBytes": {"$sum": "$storageBytesUsed"}}}]
    usage_result = await users_col.aggregate(pipeline_usage).to_list(length=1)
    metrics = usage_result[0] if usage_result else {"totalQueries": 0, "totalBytes": 0}

    pipeline_tokens = [{"$group": {"_id": None, "totalPrompt": {"$sum": "$tokenUsage.totalPromptTokens"}, "totalCompletion": {"$sum": "$tokenUsage.totalCompletionTokens"}}}]
    tokens_result = await users_col.aggregate(pipeline_tokens).to_list(length=1)
    tokens = tokens_result[0] if tokens_result else {"totalPrompt": 0, "totalCompletion": 0}
    total_tokens = tokens["totalPrompt"] + tokens["totalCompletion"]

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

    pipeline_provider = [
        {"$group": {"_id": None,
                    "totalGroq": {"$sum": "$dailyGroqQuota"},
                    "totalOpenRouter": {"$sum": "$dailyOpenRouterQuota"},
                    "totalGemini": {"$sum": "$dailyGeminiQuota"},
                    "totalCloudflare": {"$sum": "$dailyCloudflareQuota"},
                    "totalHuggingFace": {"$sum": "$dailyHuggingFaceQuota"},
                    "totalMistral": {"$sum": "$dailyMistralQuota"},
                    "totalGithub": {"$sum": "$dailyGithubQuota"},
                    "totalNrouter": {"$sum": "$dailyNrouterQuota"},
                    "totalTextCortex": {"$sum": "$dailyTextCortexQuota"}}}
    ]
    provider_result = await users_col.aggregate(pipeline_provider).to_list(length=1)
    provider_totals = provider_result[0] if provider_result else {}

    pipeline_daily_provider = [
        {"$match": {"lastAiQuotaReset": {"$gte": today}}},
        {"$group": {"_id": None,
                    "dailyGroq": {"$sum": "$dailyGroqQuota"},
                    "dailyOpenRouter": {"$sum": "$dailyOpenRouterQuota"},
                    "dailyGemini": {"$sum": "$dailyGeminiQuota"},
                    "dailyCloudflare": {"$sum": "$dailyCloudflareQuota"},
                    "dailyHuggingFace": {"$sum": "$dailyHuggingFaceQuota"},
                    "dailyMistral": {"$sum": "$dailyMistralQuota"},
                    "dailyGithub": {"$sum": "$dailyGithubQuota"},
                    "dailyNrouter": {"$sum": "$dailyNrouterQuota"},
                    "dailyTextCortex": {"$sum": "$dailyTextCortexQuota"}}}
    ]
    daily_provider_result = await users_col.aggregate(pipeline_daily_provider).to_list(length=1)
    daily_provider = daily_provider_result[0] if daily_provider_result else {}

    daily_usage = {
        "groq": daily_provider.get("dailyGroq", 0),
        "openrouter": daily_provider.get("dailyOpenRouter", 0),
        "gemini": daily_provider.get("dailyGemini", 0),
        "cloudflare": daily_provider.get("dailyCloudflare", 0),
        "huggingface": daily_provider.get("dailyHuggingFace", 0),
        "mistral": daily_provider.get("dailyMistral", 0),
        "github": daily_provider.get("dailyGithub", 0),
        "nrouter": daily_provider.get("dailyNrouter", 0),
        "text_cortex": daily_provider.get("dailyTextCortex", 0),
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
            "groq": provider_totals.get("totalGroq", 0),
            "openRouter": provider_totals.get("totalOpenRouter", 0),
            "gemini": provider_totals.get("totalGemini", 0),
            "cloudflare": provider_totals.get("totalCloudflare", 0),
            "huggingFace": provider_totals.get("totalHuggingFace", 0),
            "mistral": provider_totals.get("totalMistral", 0),
            "github": provider_totals.get("totalGithub", 0),
            "nrouter": provider_totals.get("totalNrouter", 0),
            "textCortex": provider_totals.get("totalTextCortex", 0),
            "dailyGroq": daily_provider.get("dailyGroq", 0),
            "dailyOpenRouter": daily_provider.get("dailyOpenRouter", 0),
            "dailyGemini": daily_provider.get("dailyGemini", 0),
            "dailyCloudflare": daily_provider.get("dailyCloudflare", 0),
            "dailyHuggingFace": daily_provider.get("dailyHuggingFace", 0),
            "dailyMistral": daily_provider.get("dailyMistral", 0),
            "dailyGithub": daily_provider.get("dailyGithub", 0),
            "dailyNrouter": daily_provider.get("dailyNrouter", 0),
            "dailyTextCortex": daily_provider.get("dailyTextCortex", 0),
            "groqLimit": int(os.getenv("GROQ_DAILY_LIMIT", 1000000)),
            "openRouterLimit": int(os.getenv("OPENROUTER_DAILY_LIMIT", 1000000)),
            "geminiLimit": int(os.getenv("GEMINI_DAILY_LIMIT", 1500)),
            "cloudflareLimit": int(os.getenv("CLOUDFLARE_DAILY_LIMIT", 1000000)),
            "huggingFaceLimit": int(os.getenv("HUGGINGFACE_DAILY_LIMIT", 1000000)),
            "mistralLimit": int(os.getenv("MISTRAL_DAILY_LIMIT", 1000000)),
            "githubLimit": int(os.getenv("GITHUB_DAILY_LIMIT", 1000000)),
            "nrouterLimit": int(os.getenv("NROUTER_DAILY_LIMIT", 1000000)),
            "textCortexLimit": int(os.getenv("TEXT_CORTEX_DAILY_LIMIT", 100)),
            "activeProvider": active_provider,
        },
        "providerStatus": provider_status,
        "dailyQueries": daily_queries,
        "recentUsers": recent_users,
        "timestamp": datetime.utcnow().isoformat()
    }

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

@app.post("/api/explain-code")
async def explain_code(data: CodeRequest, user: dict = Depends(get_current_user)):
    if not data.code:
        raise HTTPException(status_code=400, detail="No code provided")
    prompt = f"""Explain the following code in clear, simple terms. Focus on what it does, its purpose, and any key logic. Keep it concise (max 200 words).

```html
{data.code}
```"""
    ai_result = await route_ai_request(
        workspace="general",
        task_type="explain",
        prompt=prompt,
        history=[],
        files=[],
        max_tokens=1024,
        temp=0.3,
        tier=user.get("tier", "free"),
        user=user
    )
    if not ai_result.get("success"):
        raise HTTPException(status_code=503, detail="AI service unavailable")
    return {"success": True, "explanation": ai_result["text"]}

@app.post("/api/generate-tests")
async def generate_tests(data: CodeRequest, user: dict = Depends(get_current_user)):
    if not data.code:
        raise HTTPException(status_code=400, detail="No code provided")
    prompt = f"""Generate a set of unit tests for the following code. Assume a testing framework like Jest (JavaScript) or pytest (Python). Provide the complete test code, with comments, that covers main functionality and edge cases.

```html
{data.code}
```"""
    ai_result = await route_ai_request(
        workspace="general",
        task_type="generate_tests",
        prompt=prompt,
        history=[],
        files=[],
        max_tokens=2048,
        temp=0.2,
        tier=user.get("tier", "free"),
        user=user
    )
    if not ai_result.get("success"):
        raise HTTPException(status_code=503, detail="AI service unavailable")
    tests = ai_result["text"]
    code_match = re.search(r"```(?:javascript|python|js)?\s*([\s\S]*?)```", tests, re.DOTALL)
    if code_match:
        tests = code_match.group(1).strip()
    return {"success": True, "tests": tests}

@app.post("/api/summarize")
async def summarize(data: TextRequest, user: dict = Depends(get_current_user)):
    if not data.text:
        raise HTTPException(status_code=400, detail="No text provided")
    prompt = f"Summarize the following text concisely (max 150 words):\n\n{data.text}"
    ai_result = await route_ai_request(
        workspace="general",
        task_type="summarize",
        prompt=prompt,
        history=[],
        files=[],
        max_tokens=512,
        temp=0.3,
        tier=user.get("tier", "free"),
        user=user
    )
    if not ai_result.get("success"):
        raise HTTPException(status_code=503, detail="AI service unavailable")
    return {"success": True, "summary": ai_result["text"]}

@app.post("/api/brainstorm")
async def brainstorm(data: TextRequest, user: dict = Depends(get_current_user)):
    if not data.text:
        raise HTTPException(status_code=400, detail="No topic provided")
    prompt = f"Brainstorm 10 creative, actionable ideas related to: {data.text}. List them with brief explanations."
    ai_result = await route_ai_request(
        workspace="general",
        task_type="brainstorm",
        prompt=prompt,
        history=[],
        files=[],
        max_tokens=1024,
        temp=0.7,
        tier=user.get("tier", "free"),
        user=user
    )
    if not ai_result.get("success"):
        raise HTTPException(status_code=503, detail="AI service unavailable")
    return {"success": True, "ideas": ai_result["text"]}

@app.get("/api/suggestions")
async def get_suggestions(workspace: str = "data", user: dict = Depends(get_current_user)):
    suggestions = {
        "data": [
            "Extract key metrics from this invoice",
            "Analyze sales data and identify trends",
            "Clean and transform this dataset",
            "Generate a summary of this CSV file",
            "Compare these two spreadsheets"
        ],
        "design": [
            "Design a responsive navbar with dropdown",
            "Create a dark mode toggle button",
            "Generate a pricing card component",
            "Build a login form with validation",
            "Make this existing page mobile-friendly"
        ],
        "general": [
            "Summarize this text",
            "Explain this concept in simple terms",
            "Draft a professional email",
            "Provide a step-by-step guide",
            "Brainstorm ideas for a project"
        ]
    }
    return {"suggestions": suggestions.get(workspace, suggestions["general"])}

@app.get("/api/user/preferences")
async def get_preferences(user: dict = Depends(get_current_user)):
    if not db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")
    prefs = user.get("preferences", {})
    return {"defaultWorkspace": prefs.get("defaultWorkspace", "data")}

class PreferencesUpdate(BaseModel):
    defaultWorkspace: str

@app.put("/api/user/preferences")
async def update_preferences(data: PreferencesUpdate, user: dict = Depends(get_current_user)):
    if not db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")
    if data.defaultWorkspace not in ["data", "design", "general"]:
        raise HTTPException(status_code=400, detail="Invalid workspace")
    await users_col.update_one(
        {"_id": user["_id"]},
        {"$set": {"preferences.defaultWorkspace": data.defaultWorkspace}}
    )
    return {"success": True, "defaultWorkspace": data.defaultWorkspace}

class PuterToggle(BaseModel):
    enabled: bool

@app.post("/api/user/puter-toggle")
async def toggle_puter(data: PuterToggle, user: dict = Depends(get_current_user)):
    if not db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")
    await users_col.update_one({"_id": user["_id"]}, {"$set": {"puter_enabled": data.enabled}})
    return {"success": True, "puter_enabled": data.enabled}

@app.post("/api/admin/validate-provider")
async def validate_provider(provider_name: str, user: dict = Depends(get_current_user)):
    if not user.get("isAdmin"):
        raise HTTPException(403, "Admin only")
    provider_func = PROVIDER_FUNC_MAP.get(provider_name)
    if not provider_func:
        raise HTTPException(400, "Unknown provider")
    key_check = PROVIDER_KEY_CHECK.get(provider_name, False)
    if not key_check:
        return {"status": "skipped", "reason": "Provider does not require a key or is not configured"}
    test_prompt = "Say 'OK'"
    model = PROVIDER_MODELS.get(provider_name, [None])[0]
    if not model:
        return {"status": "error", "reason": "No model configured"}
    try:
        start = time.time()
        resp = await asyncio.wait_for(provider_func(test_prompt, 5, 0.0, model), timeout=5.0)
        latency = (time.time() - start) * 1000
        if resp and len(resp.strip()) > 0:
            return {"status": "healthy", "latency_ms": round(latency, 2), "response_preview": resp[:100]}
        else:
            return {"status": "unhealthy", "response": resp[:50] if resp else "empty"}
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}

# ---------- KEEPALIVE ----------
@app.on_event("startup")
async def start_keepalive():
    asyncio.create_task(_keepalive_loop())

async def _keepalive_loop():
    while True:
        try:
            for url in [
                "https://axelr-backend.onrender.com/",
                "https://axelr-backend.onrender.com/api/health",
                "https://axelr-backend.onrender.com/api/v1/diagnose"
            ]:
                try:
                    await HTTP_CLIENT.get(url, timeout=5.0)
                except:
                    pass
            await asyncio.sleep(180)
        except:
            await asyncio.sleep(60)

# ---------- 404 ----------
@app.exception_handler(404)
async def not_found(request, exc):
    return JSONResponse(status_code=404, content={"success": False, "code": "NOT_FOUND", "message": "Endpoint not found."})

# ---------- MAIN ----------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    logger.info(f"=== STARTING AXELR AI v24.0 ON PORT {port} ===")
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")