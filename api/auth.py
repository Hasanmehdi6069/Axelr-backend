# api/auth.py
"""
AXELR Auth Layer
================
Single source of truth for identity:
  * ``get_current_user``  — FastAPI dependency used by ~40 routes
  * JWT issue / verify    — internal tokens (GitHub, email)
  * Google ID-token verify
  * GitHub OAuth flow
  * Email + password
  * WebAuthn (optional, guarded)
  * ``_create_user_from_google`` / ``_reset_quotas_if_needed``

No route here mutates billing; that's in ``routes_billing``.
"""
from __future__ import annotations

import contextlib
import logging
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from jwt import InvalidTokenError as JWTError
from pydantic import BaseModel, EmailStr

from .config import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ADMIN_EMAIL,
    ALGORITHM,
    GITHUB_CLIENT_ID,
    GITHUB_CLIENT_SECRET,
    GITHUB_REDIRECT_URI,
    GOOGLE_CLIENT_ID,
    ORIGIN,
    RP_ID,
    RP_NAME,
    SECRET_KEY,
    get_http_client,
)
from .state import (
    delete_redis_cache,
    get_redis_cache,
    set_redis_cache,
    state,
)

logger = logging.getLogger("axelr.auth")
security = HTTPBearer()
router = APIRouter(tags=["auth"])


# ── Password helpers ─────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False


# ── JWT helpers ──────────────────────────────────────────────────────────────

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    to_encode["exp"] = datetime.now(timezone.utc) + timedelta(
        minutes=ACCESS_TOKEN_EXPIRE_MINUTES
    )
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


# ── User provisioning ────────────────────────────────────────────────────────

def _empty_ai_quotas() -> dict[str, int]:
    """Daily per-provider counters, zero-initialised."""
    keys = (
        "dailyCloudflareQuota", "dailyGeminiQuota", "dailyOpenRouterQuota",
        "dailyGroqQuota", "dailyHuggingFaceQuota", "dailyMistralQuota",
        "dailyGithubQuota", "dailyNrouterQuota", "dailyTextCortexQuota",
        "dailyModelscopeQuota", "dailyOllamaQuota", "dailyNaraQuota",
        "dailyOvhcloudQuota", "dailySiliconflowQuota", "dailyAgnesQuota",
        "dailyBazaarlinkQuota", "dailyRequestyQuota", "dailyManifestQuota",
        "dailyZhipuQuota", "dailyTeamorouterQuota",
    )
    return {k: 0 for k in keys}


def _new_user_doc(*, is_admin: bool, **identity: Any) -> dict:
    """Canonical user document. Used by every provisioning path."""
    now = datetime.now(timezone.utc)
    doc: dict[str, Any] = {
        **identity,
        "tier": "free",
        "dailyUsage": 0,
        "dailyUiUxUsage": 0,
        "storageBytesUsed": 0,
        "lastUsageDate": now,
        "customInstructions": "",
        "subTierOptions": {"hasDataAccess": False, "hasDesignAccess": False},
        "quotas": {
            "dailyExtractionsUsed": 0,
            "dailyGenerationsUsed": 0,
            "dailyEnhancementsUsed": 0,
            "monthlyEnhancementsLimit": 3,
            "lastQuotaReset": now,
        },
        "tokenUsage": {
            "totalPromptTokens": 0,
            "totalCompletionTokens": 0,
            "dailyPromptTokens": 0,
            "dailyCompletionTokens": 0,
            "lastTokenReset": now,
        },
        "isAdmin": is_admin,
        **_empty_ai_quotas(),
        "lastAiQuotaReset": now,
        "puter_enabled": False,
        "preferences": {"defaultWorkspace": "data"},
    }
    return doc


async def _create_user_from_google(idinfo: dict) -> dict:
    new_user = _new_user_doc(
        is_admin=idinfo["email"] == ADMIN_EMAIL,
        googleId=idinfo["sub"],
        email=idinfo["email"],
        displayName=idinfo.get("name", idinfo["email"]),
    )
    result = await state.users_col.insert_one(new_user)
    user_doc = await state.users_col.find_one({"_id": result.inserted_id})
    logger.info("user_created_google email=%s", idinfo["email"])
    return user_doc


async def _reset_quotas_if_needed(user_doc: dict) -> dict:
    """
    Auth-path quota refresh. Redis-only, never writes Mongo.

    Invariant (CTO directive #3): zero DB writes on the auth path.
    The background sync worker in api/quota.py is the ONLY writer
    of quota state to Mongo.
    """
    if state.redis_client is None:
        return user_doc

    now = datetime.now(timezone.utc)
    today = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    last_reset = user_doc.get("quotas", {}).get("lastQuotaReset")
    if not (last_reset and isinstance(last_reset, datetime)):
        return user_doc

    if last_reset.tzinfo is None:
        last_reset = last_reset.replace(tzinfo=timezone.utc)
    last_reset_day = datetime(
        last_reset.year, last_reset.month, last_reset.day,
        tzinfo=timezone.utc,
    )
    if today <= last_reset_day:
        return user_doc

    uid = str(user_doc["_id"])
    fields = (
        "dailyUsage", "dailyUiUxUsage",
        "quotas.dailyExtractionsUsed", "quotas.dailyGenerationsUsed",
        "quotas.dailyEnhancementsUsed",
        "tokenUsage.dailyPromptTokens", "tokenUsage.dailyCompletionTokens",
        "dailyCloudflareQuota", "dailyGeminiQuota", "dailyOpenRouterQuota",
        "dailyGroqQuota", "dailyHuggingFaceQuota", "dailyMistralQuota",
        "dailyGithubQuota", "dailyNrouterQuota", "dailyTextCortexQuota",
        "dailyModelscopeQuota", "dailyOllamaQuota", "dailyNaraQuota",
        "dailyOvhcloudQuota", "dailySiliconflowQuota", "dailyAgnesQuota",
        "dailyBazaarlinkQuota", "dailyRequestyQuota", "dailyManifestQuota",
        "dailyZhipuQuota", "dailyTeamorouterQuota",
    )
    try:
        async with state.redis_client.pipeline(transaction=False) as pipe:
            for f in fields:
                pipe.set(f"quota:{uid}:{f}", 0, ex=172800)
            pipe.set(f"quota:{uid}:last_reset", today.isoformat(), ex=172800)
            await pipe.execute()
        logger.info("quota_reset_redis_only user_id=%s day=%s",
                    uid, today.date())
    except Exception as e:
        logger.warning("quota_reset_redis_failed user_id=%s error=%s",
                       uid, e)
    return user_doc

# ── The auth dependency ──────────────────────────────────────────────────────
async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """
    Single source of truth for auth.

    Side effect: populates ``request.state.user`` so the rate-limit
    middleware (api.rate_limiter.rate_limit_middleware) can see the
    authenticated tier without re-decoding the token. Failure to set
    this attribute silently disables per-tier RPM enforcement.
    """
    if not state.db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")

    token = credentials.credentials

    # -- Path 1: Google ID token --
    try:
        idinfo = id_token.verify_oauth2_token(
            token, google_requests.Request(), GOOGLE_CLIENT_ID,
        )
        if idinfo.get("iss") not in ("accounts.google.com",
                                     "https://accounts.google.com"):
            raise HTTPException(status_code=401, detail="Invalid issuer")
        user_doc = await state.users_col.find_one({"googleId": idinfo["sub"]})
        if not user_doc:
            user_doc = await _create_user_from_google(idinfo)
        else:
            user_doc = await _reset_quotas_if_needed(user_doc)
        with contextlib.suppress(Exception):
            request.state.user = user_doc
        return user_doc
    except ValueError as e:
        logger.debug("google_token_verify_failed error=%s", e)
    except HTTPException:
        raise

    # -- Path 2: internal JWT --
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user_doc = await state.users_col.find_one({"email": payload.get("sub")})
    if not user_doc:
        raise HTTPException(status_code=401, detail="User not found")
    user_doc = await _reset_quotas_if_needed(user_doc)
    with contextlib.suppress(Exception):
        request.state.user = user_doc
    return user_doc


# ── Email login (dev/testing) ────────────────────────────────────────────────

class EmailLoginRequest(BaseModel):
    email: EmailStr


@router.post("/api/auth/email")
async def email_login(data: EmailLoginRequest):
    """
    Simplified email login for dev/testing. In a real app, this would
    involve a magic link or password verification.
    """
    user_email = data.email.lower()
    if not state.db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")

    user = await state.users_col.find_one({"email": user_email})
    if not user:
        # For simplicity, auto-create user on first login
        new_user = _new_user_doc(
            is_admin=user_email == ADMIN_EMAIL,
            email=user_email,
            createdAt=datetime.now(timezone.utc),
        )
        result = await state.users_col.insert_one(new_user)
        user = await state.users_col.find_one({"_id": result.inserted_id})

    token = create_access_token({"sub": user["email"]})
    return {"token": token}


# ── GitHub OAuth ─────────────────────────────────────────────────────────────

@router.get("/api/auth/github")
async def github_login():
    if not GITHUB_CLIENT_ID:
        raise HTTPException(status_code=503, detail="GitHub OAuth not configured")
    params = {
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": GITHUB_REDIRECT_URI,
        "scope": "user:email",
        "response_type": "code",
        "state": secrets.token_urlsafe(16),
    }
    return RedirectResponse(
        url=f"https://github.com/login/oauth/authorize?{urllib.parse.urlencode(params)}"
    )


@router.get("/api/auth/github/callback")
async def github_callback(
    request: Request,
    code: str,
    oauth_state: str | None = Query(default=None, alias="state"),
):
    """
    GitHub OAuth callback. The query param ``state`` is bound to ``oauth_state``
    (via Query alias) so the module-level ``state`` container is not shadowed.
    """
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        raise HTTPException(status_code=503, detail="GitHub OAuth not configured")

    try:
        client = get_http_client()
        resp = await client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            json={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
                "redirect_uri": GITHUB_REDIRECT_URI,
                "state": oauth_state,
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        access_token = resp.json().get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="Failed to obtain access token")
    except Exception as e:
        logger.error("github_token_exchange_failed error=%s", e)
        raise HTTPException(status_code=503, detail="GitHub authentication service unavailable")

    user_headers = {"Authorization": f"Bearer {access_token}"}
    try:
        user_resp = await client.get(
            "https://api.github.com/user", headers=user_headers, timeout=10.0,
        )
        user_resp.raise_for_status()
        user_data = user_resp.json()
    except Exception as e:
        logger.error("github_user_fetch_failed error=%s", e)
        raise HTTPException(status_code=503, detail="Failed to fetch GitHub profile")

    try:
        email_resp = await client.get(
            "https://api.github.com/user/emails", headers=user_headers, timeout=10.0,
        )
        email_resp.raise_for_status()
        emails = email_resp.json()
        primary_email = next(
            (e["email"] for e in emails if e.get("primary")),
            user_data.get("email"),
        )
    except Exception:
        primary_email = user_data.get("email") or f"{user_data['id']}@github.user"

    if not state.db_available:
        raise HTTPException(status_code=503, detail="Database unavailable")
    github_id = str(user_data["id"])
    user_doc = await state.users_col.find_one({"githubId": github_id})
    if not user_doc:
        new_user = _new_user_doc(
            is_admin=primary_email == ADMIN_EMAIL,
            githubId=github_id,
            email=primary_email,
            displayName=(user_data.get("name")
                         or user_data.get("login")
                         or primary_email),
            createdAt=datetime.now(timezone.utc),
        )
        result = await state.users_col.insert_one(new_user)
        user_doc = await state.users_col.find_one({"_id": result.inserted_id})
        logger.info("user_created_github email=%s", primary_email)
    else:
        await state.users_col.update_one(
            {"_id": user_doc["_id"]},
            {"$set": {"lastUsageDate": datetime.now(timezone.utc)}},
        )
        user_doc = await state.users_col.find_one({"_id": user_doc["_id"]})
        user_doc = await _reset_quotas_if_needed(user_doc)
    token = create_access_token({"sub": user_doc["email"]})
    return RedirectResponse(url=f"{ORIGIN}/?auth=github&token={token}")


# ── WebAuthn (guarded — optional) ────────────────────────────────────────────

WEBAUTHN_AVAILABLE = False
try:
    from webauthn import (  # type: ignore
        generate_authentication_options,
        generate_registration_options,
        verify_authentication_response,
        verify_registration_response,
    )
    from webauthn.helpers.structs import (  # type: ignore
        AuthenticationCredential,
        AuthenticatorSelectionCriteria,
        PublicKeyCredentialDescriptor,
        RegistrationCredential,
        UserVerificationRequirement,
    )
    WEBAUTHN_AVAILABLE = True
    logger.info("webauthn_enabled")
except ImportError as e:
    logger.warning("webauthn_module_unavailable error=%s", e)


if WEBAUTHN_AVAILABLE:
    class WebAuthnRegistrationBeginRequest(BaseModel):
        email: EmailStr

    class WebAuthnRegistrationFinishRequest(BaseModel):
        email: EmailStr
        credential: dict

    class WebAuthnLoginBeginRequest(BaseModel):
        email: EmailStr

    class WebAuthnLoginFinishRequest(BaseModel):
        email: EmailStr
        credential: dict

    async def _get_user_for_webauthn(email: str) -> dict:
        if not state.db_available:
            raise HTTPException(status_code=503, detail="Database unavailable")
        user = await state.users_col.find_one({"email": email})
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return user

    async def _store_webauthn_credential(user_id, credential_id, public_key, sign_count):
        await state.users_col.update_one(
            {"_id": user_id},
            {"$push": {"webauthnCredentials": {
                "credentialId": credential_id.hex(),
                "publicKey": public_key.hex(),
                "signCount": sign_count,
                "transports": [],
            }}},
        )

    @router.post("/api/auth/webauthn/register/begin")
    async def webauthn_register_begin(data: WebAuthnRegistrationBeginRequest):
        user = await _get_user_for_webauthn(data.email)
        options = generate_registration_options(
            rp_id=RP_ID,
            rp_name=RP_NAME,
            user_id=user["_id"].binary,
            user_name=user.get("displayName", data.email),
            user_display_name=user.get("displayName", data.email),
            authenticator_selection=AuthenticatorSelectionCriteria(
                user_verification=UserVerificationRequirement.PREFERRED,
                resident_key="preferred",
            ),
        )
        await set_redis_cache(
            f"webauthn_challenge:{options.challenge}",
            {"email": data.email}, ttl=300,
        )
        return options.model_dump()

    @router.post("/api/auth/webauthn/register/finish")
    async def webauthn_register_finish(data: WebAuthnRegistrationFinishRequest):
        user = await _get_user_for_webauthn(data.email)
        challenge_token = data.credential.get("challenge")
        challenge_data = await get_redis_cache(f"webauthn_challenge:{challenge_token}")
        if not challenge_data or challenge_data.get("email") != data.email:
            raise HTTPException(status_code=400, detail="Invalid or expired challenge")
        await delete_redis_cache(f"webauthn_challenge:{challenge_token}")
        try:
            credential = RegistrationCredential(**data.credential)
            verification = verify_registration_response(
                credential=credential,
                expected_challenge=challenge_token,
                expected_rp_id=RP_ID,
                expected_origin=ORIGIN,
            )
        except Exception as e:
            logger.error("webauthn_registration_verify_failed error=%s", e)
            raise HTTPException(status_code=400, detail="Registration verification failed")
        await _store_webauthn_credential(
            user["_id"], verification.credential_id,
            verification.credential_public_key, verification.sign_count,
        )
        return {"success": True, "message": "Passkey registered successfully"}

    @router.post("/api/auth/webauthn/login/begin")
    async def webauthn_login_begin(data: WebAuthnLoginBeginRequest):
        user = await _get_user_for_webauthn(data.email)
        credentials = user.get("webauthnCredentials", [])
        if not credentials:
            raise HTTPException(status_code=400, detail="No passkeys registered for this user")
        allowed = [
            PublicKeyCredentialDescriptor(id=bytes.fromhex(c["credentialId"]))
            for c in credentials
        ]
        options = generate_authentication_options(
            rp_id=RP_ID,
            challenge=secrets.token_urlsafe(32),
            allow_credentials=allowed,
            user_verification=UserVerificationRequirement.PREFERRED,
        )
        await set_redis_cache(
            f"webauthn_challenge:{options.challenge}",
            {"email": data.email}, ttl=300,
        )
        return options.model_dump()

    @router.post("/api/auth/webauthn/login/finish")
    async def webauthn_login_finish(data: WebAuthnLoginFinishRequest):
        user = await _get_user_for_webauthn(data.email)
        challenge_token = data.credential.get("challenge")

        challenge_data = await get_redis_cache(f"webauthn_challenge:{challenge_token}")
        if not challenge_data or challenge_data.get("email") != data.email:
            raise HTTPException(status_code=400, detail="Invalid or expired challenge")
        await delete_redis_cache(f"webauthn_challenge:{challenge_token}")

        stored = next(
            (c for c in user.get("webauthnCredentials", [])
             if c["credentialId"] == data.credential.get("id", "")),
            None,
        )
        if stored is None:
            raise HTTPException(status_code=400, detail="Credential not registered")
        try:
            credential = AuthenticationCredential(**data.credential)
            verification = verify_authentication_response(
                credential=credential,
                expected_challenge=challenge_token,
                expected_rp_id=RP_ID,
                expected_origin=ORIGIN,
                credential_public_key=bytes.fromhex(stored["publicKey"]),
                credential_current_sign_count=stored.get("signCount", 0),
            )
        except Exception as e:
            logger.error("webauthn_login_verify_failed error=%s", e)
            raise HTTPException(status_code=400, detail="Authentication verification failed")

        await state.users_col.update_one(
            {"_id": user["_id"], "webauthnCredentials.credentialId": stored["credentialId"]},
            {"$set": {"webauthnCredentials.$.signCount": verification.new_sign_count}},
        )
        token = create_access_token({"sub": user["email"]})
        return {"success": True, "token": token}


__all__ = [
    "WEBAUTHN_AVAILABLE",
    "create_access_token",
    "decode_token",
    "get_current_user",
    "hash_password",
    "router",
    "security",
    "verify_password",
]