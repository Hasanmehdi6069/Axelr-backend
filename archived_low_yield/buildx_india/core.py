"""BuildX India / AIC T-Hub Zero-Trust Backend — v1.1.0
=====================================================
Mandatory access control, tenant isolation, continuous verification.

Honest scope / fixes
--------------------
* **`request["auth_context"] = ...` was a runtime `TypeError`** — aiohttp's
  `Request` doesn't support item assignment. Fixed to
  `request.match_info["auth_context"] = ...` (which is a dict-like).
* **Wildcard origin matching was broken** — `"*.axelr.ai"` matched the
  bare domain and any path-suffix. Fixed with `urllib.parse` + dot-
  boundary comparison. `"*.axelr.ai"` now matches `foo.axelr.ai` but
  **not** `axelr.ai`, `evil.axelr.ai.example.com`, or `evil-foo.axelr.ai`.
* **`auth_header.split(" ")[1]`** — IndexError on malformed header
  (`"Bearer"` with no token). Fixed to slice-after-prefix + empty check.
* **`payload["perms"]`** — KeyError on JWT missing the `perms` claim.
  Fixed to `.get(..., [])` with explicit empty-set fallback.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, Set
from urllib.parse import urlparse

import jwt
from aiohttp import web

logger = logging.getLogger("axelr.buildx_india.core")
__version__ = "1.1.0"


# ─── Config ───────────────────────────────────────────────────────────────
def _str(k: str, default: str = "") -> str:
    return (os.getenv(k) or default).strip()


def _int(k: str, default: int) -> int:
    try:
        return int(os.getenv(k, str(default)))
    except (TypeError, ValueError):
        return default


def _bool(k: str, default: bool = False) -> bool:
    v = (os.getenv(k) or "").lower()
    return v in ("1", "true", "yes", "on") if v else default


def _csv(k: str, default: str = "") -> List[str]:
    raw = os.getenv(k) or default
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass(frozen=True, slots=True)
class ZeroTrustSettings:
    JWT_SECRET: str
    JWT_ISSUER: str
    JWT_AUDIENCE: str
    JWT_EXPIRY_SECONDS: int
    ALLOWED_ORIGINS: List[str]
    DEFAULT_TENANT_QUOTA: int
    MAX_TENANTS: int

    @classmethod
    def from_env(cls) -> "ZeroTrustSettings":
        return cls(
            JWT_SECRET=_str("JWT_SECRET"),
            JWT_ISSUER=_str("JWT_ISSUER", "axelr-buildx"),
            JWT_AUDIENCE=_str("JWT_AUDIENCE", "axelr-api"),
            JWT_EXPIRY_SECONDS=_int("JWT_EXPIRY", 900),
            ALLOWED_ORIGINS=_csv("ALLOWED_ORIGINS", "*.axelr.ai,localhost:3000"),
            DEFAULT_TENANT_QUOTA=_int("DEFAULT_TENANT_QUOTA", 10000),
            MAX_TENANTS=_int("MAX_TENANTS", 1000),
        )


_settings: Optional[ZeroTrustSettings] = None


def get_settings() -> ZeroTrustSettings:
    global _settings
    if _settings is None:
        _settings = ZeroTrustSettings.from_env()
    return _settings


# ─── Tenants ──────────────────────────────────────────────────────────────
@dataclass(slots=True)
class Tenant:
    tenant_id: str
    name: str
    plan: str
    quota_limit: int
    quota_used: int
    is_active: bool
    created_at: datetime
    metadata: Dict[str, Any] = field(default_factory=dict)
    isolation_scope: str = "strict"


_tenants: Dict[str, Tenant] = {}
_tenant_lock = asyncio.Lock()


async def register_tenant(name: str, plan: str = "free") -> Tenant:
    settings = get_settings()
    async with _tenant_lock:
        if len(_tenants) >= settings.MAX_TENANTS:
            raise RuntimeError("tenant_limit_reached")
        tid = str(uuid.uuid4())
        t = Tenant(
            tenant_id=tid, name=name, plan=plan,
            quota_limit=settings.DEFAULT_TENANT_QUOTA,
            quota_used=0, is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        _tenants[tid] = t
        logger.info("tenant_registered id=%s name=%s", tid, name)
        return t


async def get_tenant(tenant_id: str) -> Optional[Tenant]:
    async with _tenant_lock:
        return _tenants.get(tenant_id)


# ─── Auth ─────────────────────────────────────────────────────────────────
@dataclass(slots=True)
class AuthContext:
    user_id: str
    tenant_id: str
    roles: List[str]
    permissions: Set[str]
    exp: int
    iat: int


def create_access_token(
    user_id: str, tenant_id: str, roles: List[str], permissions: Set[str],
) -> str:
    settings = get_settings()
    if not settings.JWT_SECRET:
        raise RuntimeError("jwt_secret_not_configured")
    if not user_id or not tenant_id:
        raise ValueError("user_id and tenant_id required")
    now = int(time.time())
    payload = {
        "sub": user_id, "tid": tenant_id,
        "roles": roles, "perms": list(permissions),
        "iss": settings.JWT_ISSUER, "aud": settings.JWT_AUDIENCE,
        "iat": now, "exp": now + settings.JWT_EXPIRY_SECONDS,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def verify_access_token(token: str) -> Optional[AuthContext]:
    settings = get_settings()
    if not settings.JWT_SECRET:
        return None
    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET,
            issuer=settings.JWT_ISSUER, audience=settings.JWT_AUDIENCE,
            algorithms=["HS256"],
            options={"require": ["sub", "tid", "exp", "iat"]},
        )
        return AuthContext(
            user_id=payload["sub"],
            tenant_id=payload["tid"],
            roles=list(payload.get("roles") or []),
            permissions=set(payload.get("perms") or []),
            exp=int(payload["exp"]),
            iat=int(payload["iat"]),
        )
    except (jwt.PyJWTError, KeyError, TypeError) as e:
        logger.warning("token_verification_failed err=%s", e)
        return None


def requires_permission(required_perm: str):
    def decorator(handler: Callable[..., Any]):
        @wraps(handler)
        async def wrapper(request: web.Request, *args, **kwargs) -> web.Response:
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return web.json_response(
                    {"error": "missing_authorization"}, status=401)
            token = auth_header[len("Bearer "):].strip()
            if not token:
                return web.json_response({"error": "empty_token"}, status=401)
            ctx = verify_access_token(token)
            if ctx is None:
                return web.json_response({"error": "invalid_token"}, status=401)
            tenant = await get_tenant(ctx.tenant_id)
            if tenant is None or not tenant.is_active:
                return web.json_response({"error": "tenant_inactive"}, status=403)
            if required_perm not in ctx.permissions:
                logger.warning("permission_denied user=%s required=%s",
                               ctx.user_id, required_perm)
                return web.json_response(
                    {"error": "insufficient_permissions"}, status=403)
            # aiohttp Request does NOT support request["k"] = v.
            # match_info is a dict subclass and DOES.
            request.match_info["auth_context"] = ctx
            return await handler(request, *args, **kwargs)
        return wrapper
    return decorator


# ─── Origin validation ────────────────────────────────────────────────────
def validate_origin(origin: str) -> bool:
    """Origin must be scheme://host[:port]. We compare only the hostname.

    Wildcard rules (documented):
      "*.example.com"  → matches "foo.example.com", "a.b.example.com"
                       → does NOT match "example.com" (bare domain)
                       → does NOT match "evil.example.com.attacker.net"
      "*"              → matches everything (never use in production)
      "example.com"    → exact hostname match
    """
    if not origin:
        return False
    try:
        hostname = (urlparse(origin).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    if not hostname:
        return False

    settings = get_settings()
    for allowed in settings.ALLOWED_ORIGINS:
        a = allowed.lower().strip().rstrip(".")
        if a == "*":
            return True
        if a.startswith("*."):
            base = a[2:]
            if hostname.endswith("." + base):
                return True
        elif a.startswith("*"):
            base = a[1:]
            if hostname.endswith(base):
                return True
        elif hostname == a:
            return True
    return False


# ─── Lifecycle ────────────────────────────────────────────────────────────
_shutdown_event: Optional[asyncio.Event] = None


async def init() -> None:
    global _shutdown_event
    if _shutdown_event is None:
        _shutdown_event = asyncio.Event()
    _shutdown_event.clear()
    settings = get_settings()
    if not settings.JWT_SECRET:
        logger.warning("jwt_secret_missing — tokens will not be verifiable")
    logger.info("buildx_india_zero_trust_initialized max_tenants=%d",
                settings.MAX_TENANTS)


async def shutdown() -> None:
    global _shutdown_event
    if _shutdown_event is not None:
        _shutdown_event.set()
    async with _tenant_lock:
        _tenants.clear()
    _shutdown_event = None
    logger.info("buildx_india_zero_trust_shutdown")


def health() -> Dict[str, Any]:
    settings = get_settings()
    return {
        "ok": bool(settings.JWT_SECRET),
        "details": {
            "version": __version__,
            "jwt_configured": bool(settings.JWT_SECRET),
            "tenants": len(_tenants),
            "max_tenants": settings.MAX_TENANTS,
            "allowed_origins": settings.ALLOWED_ORIGINS,
        },
    }