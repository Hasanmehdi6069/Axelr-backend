# api/config.py
"""
AXELR API configuration.
========================
Single source of truth for every env var, static model list, Stripe catalog,
and the shared HTTP client. Imported by every other `api/*` module and by
the `app.py` warhead.

Zero logic beyond reading env and building the shared clients.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import certifi
import httpx
from dotenv import load_dotenv
# ── Hardening: refuse to boot in prod with a weak/missing SECRET_KEY ──────
_ENV = (os.getenv("ENV") or "dev").lower()
_SECRET = (os.getenv("JWT_SECRET") or "").strip()
if _ENV not in ("dev", "local", "test", "development"):
    if len(_SECRET) < 32:
        raise RuntimeError(
            "FATAL: JWT_SECRET must be set (>=32 chars) in production. "
            "Refusing to boot."
        )
else:
    if not _SECRET:
        os.environ["JWT_SECRET"] = "dev-insecure-secret-do-not-use-in-prod"
# Load .env once, before any os.getenv() runs.
load_dotenv(override=True)

logger = logging.getLogger("axelr")


# ── Core config ──────────────────────────────────────────────────────────────

GOOGLE_CLIENT_ID = (
    os.getenv("GOOGLE_CLIENT_ID")
    or "474929925590-kfpurq4aou35pkscf6gbr963vf4hfa7g.apps.googleusercontent.com"
).strip()

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "shanh1346@gmail.com")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
NETLIFY_ACCESS_TOKEN = os.getenv("NETLIFY_ACCESS_TOKEN")

MONGO_URI = (os.getenv("MONGO_URI") or "").strip()
DATA_WORKER_URL = (os.getenv("DATA_WORKER_URL") or "").strip()

# Redis: accept either a canonical URL or an Upstash REST-embedded URL.
REDIS_URL = os.getenv("REDIS_URL") or ""
_upstash_raw = os.getenv("UPSTASH_REDIS_REST_URL", "")
if "redis://" in _upstash_raw:
    import re as _re
    _m = _re.search(r"redis://[^\s]+", _upstash_raw)
    if _m:
        REDIS_URL = REDIS_URL or _m.group(0)


# ── AI provider API keys ─────────────────────────────────────────────────────

GROQ_API_KEY          = (os.getenv("GROQ_API_KEY") or "").strip()
CLOUDFLARE_API_KEY    = (os.getenv("CLOUDFLARE_API_KEY") or "").strip()
CLOUDFLARE_ACCOUNT_ID = (os.getenv("CLOUDFLARE_ACCOUNT_ID") or "").strip()
OPENROUTER_API_KEY    = (os.getenv("OPENROUTER_API_KEY") or "").strip()
HF_API_KEY            = (os.getenv("HUGGINGFACE_API_KEY") or "").strip()
GEMINI_API_KEY        = (os.getenv("GEMINI_API_KEY") or "").strip()
MISTRAL_API_KEY       = (os.getenv("MISTRAL_API_KEY") or "").strip()
GITHUB_MODELS_TOKEN   = (os.getenv("GITHUB_MODELS_TOKEN") or "").strip()
NROUTER_API_KEY       = (os.getenv("NROUTER_API_KEY") or "").strip()
TEXT_CORTEX_API_KEY   = (os.getenv("TEXT_CORTEX_API_KEY") or "").strip()
NARAROUTER_API_KEY    = (os.getenv("NARAROUTER_API_KEY") or "").strip()
BAZAARLINK_API_KEY    = (os.getenv("BAZAARLINK_API_KEY") or "").strip()
SILICONFLOW_API_KEY   = (os.getenv("SILICONFLOW_API_KEY") or "").strip()
AGNES_API_KEY         = (os.getenv("AGNES_API_KEY") or "").strip()
OLLAMA_API_KEY        = (os.getenv("OLLAMA_API_KEY") or "").strip()
ANYAPI_API_KEY        = (os.getenv("ANYAPI_API_KEY") or "").strip()
MODELSCOPE_API_KEY    = (os.getenv("MODELSCOPE_API_KEY") or "").strip()
OVHCLOUD_API_KEY      = (os.getenv("OVHCLOUD_API_KEY") or "").strip()
REQUESTY_API_KEY      = (os.getenv("REQUESTY_API_KEY") or "").strip()
MANIFEST_API_KEY      = (os.getenv("MANIFEST_API_KEY") or "").strip()
GLAMA_API_KEY         = (os.getenv("GLAMA_API_KEY") or "").strip()
ZAI_API_KEY           = (os.getenv("ZAI_API_KEY") or "").strip()
TEAMOROUTER_API_KEY   = (os.getenv("TEAMOROUTER_API_KEY") or "").strip()


# ── OAuth / WebAuthn ─────────────────────────────────────────────────────────

GITHUB_CLIENT_ID     = os.getenv("GITHUB_CLIENT_ID", "").strip()
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "").strip()
GITHUB_REDIRECT_URI  = os.getenv(
    "GITHUB_REDIRECT_URI",
    "https://axelr-backend.onrender.com/api/auth/github/callback",
)
RP_ID   = os.getenv("RP_ID", "axelr.in")
RP_NAME = os.getenv("RP_NAME", "AXELR AI")
ORIGIN  = os.getenv("ORIGIN", "https://axelr.in").rstrip("/")


# ── JWT ──────────────────────────────────────────────────────────────────────

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7

# SECRET_KEY is resolved in app.py with production hardening.
# Read here so other modules can import it as a stable symbol.
SECRET_KEY = (os.getenv("JWT_SECRET") or "").strip()


# ── Stripe ───────────────────────────────────────────────────────────────────

STRIPE_SECRET_KEY     = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_SUCCESS_URL    = os.getenv("STRIPE_SUCCESS_URL", "https://axelr.in/?billing=success")
STRIPE_CANCEL_URL     = os.getenv("STRIPE_CANCEL_URL", "https://axelr.in/?billing=cancelled")
STRIPE_PORTAL_RETURN  = os.getenv("STRIPE_PORTAL_RETURN_URL", "https://axelr.in/?billing=portal_return")
STRIPE_TRIAL_DAYS     = max(0, int(os.getenv("STRIPE_TRIAL_DAYS", "0")))

STRIPE_PRICE_CATALOG = {
    "pro": {
        "full":   {"monthly": os.getenv("STRIPE_PRICE_PRO_FULL_MONTHLY"),
                   "annual":  os.getenv("STRIPE_PRICE_PRO_FULL_ANNUAL")},
        "data":   {"monthly": os.getenv("STRIPE_PRICE_PRO_DATA_MONTHLY"),
                   "annual":  os.getenv("STRIPE_PRICE_PRO_DATA_ANNUAL")},
        "design": {"monthly": os.getenv("STRIPE_PRICE_PRO_DESIGN_MONTHLY"),
                   "annual":  os.getenv("STRIPE_PRICE_PRO_DESIGN_ANNUAL")},
    },
    "business": {
        "full":   {"monthly": os.getenv("STRIPE_PRICE_BIZ_FULL_MONTHLY"),
                   "annual":  os.getenv("STRIPE_PRICE_BIZ_FULL_ANNUAL")},
        "data":   {"monthly": os.getenv("STRIPE_PRICE_BIZ_DATA_MONTHLY"),
                   "annual":  os.getenv("STRIPE_PRICE_BIZ_DATA_ANNUAL")},
        "design": {"monthly": os.getenv("STRIPE_PRICE_BIZ_DESIGN_MONTHLY"),
                   "annual":  os.getenv("STRIPE_PRICE_BIZ_DESIGN_ANNUAL")},
    },
}

STRIPE_PRICE_AMOUNTS = {
    "pro": {
        "full":   {"monthly": 1500, "annual": 14400},
        "data":   {"monthly": 900,  "annual": 8400},
        "design": {"monthly": 1000, "annual": 9600},
    },
    "business": {
        "full":   {"monthly": 3500, "annual": 33600},
        "data":   {"monthly": 2200, "annual": 21600},
        "design": {"monthly": 2400, "annual": 22800},
    },
}

TIER_LABELS = {
    ("pro", "full"):    "Axelr Pro Architect (Full)",
    ("pro", "data"):    "Axelr Pro Architect (Data)",
    ("pro", "design"):  "Axelr Pro Architect (Design)",
    ("business", "full"):   "Axelr Business Collective (Full)",
    ("business", "data"):   "Axelr Business Collective (Data)",
    ("business", "design"): "Axelr Business Collective (Design)",
}

VALID_TIERS    = {"pro", "business"}
VALID_SUBTIERS = {"full", "data", "design"}
VALID_PERIODS  = {"monthly", "annual"}


# ── Feature flags ────────────────────────────────────────────────────────────

ENABLE_INTENT_CLASSIFIER = os.getenv("ENABLE_INTENT_CLASSIFIER", "true").lower() == "true"
ENABLE_CONTEXT_REGISTRY  = os.getenv("ENABLE_CONTEXT_REGISTRY", "true").lower() == "true"
ENABLE_CRITIC            = os.getenv("ENABLE_CRITIC", "true").lower() == "true"
ENABLE_SELF_HEAL         = os.getenv("ENABLE_SELF_HEAL", "true").lower() == "true"
ENABLE_BLAST_RADIUS      = os.getenv("ENABLE_BLAST_RADIUS", "false").lower() == "true"
ENABLE_PR_DEFENSE        = os.getenv("ENABLE_PR_DEFENSE", "true").lower() == "true"
WORKSPACE_ROOT           = os.getenv("WORKSPACE_ROOT", "")


# ── Static model catalogs ────────────────────────────────────────────────────

def _csv_env(key: str, default: str) -> list[str]:
    return [m.strip() for m in os.getenv(key, default).split(",") if m.strip()]


GEMINI_MODELS      = _csv_env("GEMINI_MODEL", "gemini-1.5-flash")
GEMINI_MODEL       = GEMINI_MODELS[0] if GEMINI_MODELS else "gemini-1.5-flash"
GROQ_MODELS        = _csv_env("GROQ_MODELS", "llama3-70b-8192,mixtral-8x7b-32768,gemma2-9b-it")
OPENROUTER_MODELS  = _csv_env("OPENROUTER_MODELS", "openrouter/auto,mistralai/mistral-7b-instruct:free,deepseek/deepseek-chat:free")
CLOUDFLARE_MODEL   = os.getenv("CLOUDFLARE_MODEL", "@cf/meta/llama-3.1-8b-instruct")
MODELSCOPE_MODELS  = _csv_env("MODELSCOPE_MODELS", "qwen-max,deepseek-v3")
OLLAMA_MODELS      = _csv_env("OLLAMA_MODELS", "mistral-large-3:675b-cloud,kimi-k2.6,glm-5.3")
NARA_MODELS        = _csv_env("NARA_MODELS", "minimax-m3,deepseek-v3")
MISTRAL_MODELS     = _csv_env("MISTRAL_MODELS", "open-mistral-7b,mistral-small-latest")
HF_MODELS          = _csv_env("HUGGINGFACE_MODELS", "meta-llama/Llama-3.2-3B-Instruct,mistralai/Mistral-7B-Instruct-v0.3")
GITHUB_MODEL       = os.getenv("GITHUB_MODEL", "gpt-4o-mini")
OVHCLOUD_MODELS    = _csv_env("OVHCLOUD_MODELS", "llama-3.3-70b-instruct,mistral-7b-instruct")
SILICONFLOW_MODELS = _csv_env("SILICONFLOW_MODELS", "deepseek-ai/DeepSeek-V3,Qwen/Qwen2.5-7B-Instruct")
AGNES_MODEL        = os.getenv("AGNES_MODEL", "agnes-2.0-flash")
ZHIPU_MODEL        = os.getenv("ZHIPU_MODEL", "glm-4.5-flash")
TEAMOROUTER_MODEL  = os.getenv("TEAMOROUTER_MODEL", "teamorouter-free")
BAZAARLINK_MODEL   = os.getenv("BAZAARLINK_MODEL", "auto:free")
REQUESTY_MODEL     = os.getenv("REQUESTY_MODEL", "auto:free")
NROUTER_MODEL      = os.getenv("NROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct")
GLAMA_MODEL        = os.getenv("GLAMA_MODEL", "gpt-3.5-turbo")

# Static lists for the "free" providers
BIFROST_MODELS      = ["llama3.1:70b", "mistral:7b"]
FREEGPT4_MODELS     = ["gpt-4"]
PUTER_MODEL         = "gpt-3.5-turbo"
FREETHEAI_MODEL     = "gpt-3.5-turbo"
OMNIGPT_MODELS      = ["gpt-3.5-turbo"]
OPENDODE_MODELS     = ["qwen3-coder"]
FREEFLOW_MODEL      = "gpt-3.5-turbo"
QODER_MODEL         = "qwen3-coder"
MANIFEST_MODEL      = "auto:free"
KEYLESS_MODEL       = "gpt-3.5-turbo"
CHUBVENUS_MODEL     = "gpt-3.5-turbo"
BLOCKRUN_MODELS     = ["deepseek-v4-flash"]
ANYAPI_MODEL        = "poolside/laguna-xs.2:free"
AYMO_MODELS         = ["gemini-flash", "deepseek-v3.2", "qwen3"]
ZEROTWO_MODELS      = ["gpt-5-mini", "gemini-flash-lite"]
AIHUBMIX_MODELS     = ["gpt-5.5", "gemini-3", "glm-5.1", "kimi", "minimax"]
AISURE_MODEL        = "gpt-4o"

FREE_TIER_TOKEN_LIMIT = int(os.getenv("FREE_TIER_TOKEN_LIMIT", "1000000"))

PROVIDER_COOLDOWN = 60  # seconds


# ── Workspace LLM config ─────────────────────────────────────────────────────

WORKSPACE_LLM_CONFIG = {
    "data":   {"temperature": 0.1, "max_tokens": 4096, "rpm_limit": 15, "tpm_limit": 50000},
    "design": {"temperature": 0.6, "max_tokens": 8192, "rpm_limit": 10, "tpm_limit": 80000},
    "core":   {"temperature": 0.5, "max_tokens": 8192, "rpm_limit": 15, "tpm_limit": 50000},
}


# ── Rate limits (tier → RPM) ─────────────────────────────────────────────────

RATE_LIMITS: dict[str, int] = {"free": 2, "pro": 5, "business": 8}


# ── Shared HTTP client ───────────────────────────────────────────────────────

HTTP_CLIENT = httpx.AsyncClient(
    timeout=httpx.Timeout(12.0, connect=8.0, read=12.0, write=8.0),
    verify=certifi.where(),
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
)


# ── Supabase ─────────────────────────────────────────────────────────────────

SUPABASE_URL    = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY    = (os.getenv("SUPABASE_SERVICE_KEY") or "").strip()
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "axelr-uploads")


# ── Public surface ───────────────────────────────────────────────────────────
__all__ = [
    # core config
    "GOOGLE_CLIENT_ID", "ADMIN_EMAIL", "SMTP_HOST", "SMTP_PORT",
    "SMTP_USER", "SMTP_PASS", "NETLIFY_ACCESS_TOKEN",
    "MONGO_URI", "DATA_WORKER_URL", "REDIS_URL",
    # provider keys
    "GROQ_API_KEY", "CLOUDFLARE_API_KEY", "CLOUDFLARE_ACCOUNT_ID",
    "OPENROUTER_API_KEY", "HF_API_KEY", "GEMINI_API_KEY",
    "MISTRAL_API_KEY", "GITHUB_MODELS_TOKEN", "NROUTER_API_KEY",
    "TEXT_CORTEX_API_KEY", "NARAROUTER_API_KEY", "BAZAARLINK_API_KEY",
    "SILICONFLOW_API_KEY", "AGNES_API_KEY", "OLLAMA_API_KEY",
    "ANYAPI_API_KEY", "MODELSCOPE_API_KEY", "OVHCLOUD_API_KEY",
    "REQUESTY_API_KEY", "MANIFEST_API_KEY", "GLAMA_API_KEY",
    "ZAI_API_KEY", "TEAMOROUTER_API_KEY",
    # oauth / webauthn
    "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "GITHUB_REDIRECT_URI",
    "RP_ID", "RP_NAME", "ORIGIN",
    # jwt
    "ALGORITHM", "ACCESS_TOKEN_EXPIRE_MINUTES", "SECRET_KEY",
    # stripe
    "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
    "STRIPE_SUCCESS_URL", "STRIPE_CANCEL_URL", "STRIPE_PORTAL_RETURN",
    "STRIPE_TRIAL_DAYS", "STRIPE_PRICE_CATALOG", "STRIPE_PRICE_AMOUNTS",
    "TIER_LABELS", "VALID_TIERS", "VALID_SUBTIERS", "VALID_PERIODS",
    # flags
    "ENABLE_INTENT_CLASSIFIER", "ENABLE_CONTEXT_REGISTRY", "ENABLE_CRITIC",
    "ENABLE_SELF_HEAL", "ENABLE_BLAST_RADIUS", "ENABLE_PR_DEFENSE",
    "WORKSPACE_ROOT",
    # model catalogs
    "GEMINI_MODELS", "GEMINI_MODEL", "GROQ_MODELS", "OPENROUTER_MODELS",
    "CLOUDFLARE_MODEL", "MODELSCOPE_MODELS", "OLLAMA_MODELS",
    "NARA_MODELS", "MISTRAL_MODELS", "HF_MODELS", "GITHUB_MODEL",
    "OVHCLOUD_MODELS", "SILICONFLOW_MODELS", "AGNES_MODEL",
    "ZHIPU_MODEL", "TEAMOROUTER_MODEL", "BAZAARLINK_MODEL",
    "REQUESTY_MODEL", "NROUTER_MODEL", "GLAMA_MODEL",
    "BIFROST_MODELS", "FREEGPT4_MODELS", "PUTER_MODEL", "FREETHEAI_MODEL",
    "OMNIGPT_MODELS", "OPENDODE_MODELS", "FREEFLOW_MODEL",
    "QODER_MODEL", "MANIFEST_MODEL", "KEYLESS_MODEL", "CHUBVENUS_MODEL",
    "BLOCKRUN_MODELS", "ANYAPI_MODEL", "AYMO_MODELS", "ZEROTWO_MODELS",
    "AIHUBMIX_MODELS", "AISURE_MODEL", "FREE_TIER_TOKEN_LIMIT",
    "PROVIDER_COOLDOWN",
    # workspace config
    "WORKSPACE_LLM_CONFIG", "RATE_LIMITS",
    # shared clients
    "HTTP_CLIENT",
    # supabase
    "SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_BUCKET",
]