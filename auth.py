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

import logging
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
import jwt
from jwt import InvalidTokenError as JWTError
from pydantic import BaseModel

from .config import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ADMIN_EMAIL,
    ALGORITHM,
    GITHUB_CLIENT_ID,
    GITHUB_CLIENT_SECRET,
    GITHUB_REDIRECT_URI,
    GOOGLE_CLIENT_ID,
    HTTP_CLIENT,
    ORIGIN,
    RP_ID,
    RP_NAME,
    SECRET_KEY,
)
from .state import (
    delete_redis_cache,
    get_object_id,
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

async def _create_user_from_google(idinfo: dict) -> dict:
    is_admin = idinfo["email"] == ADMIN_EMAIL
    new_user = {
        "googleId": idinfo["sub"],
        "email": idinfo["email"],
        "displayName": idinfo.get("name", idinfo["email"]),
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
            "lastQuotaReset": datetime.utcnow(),
        },
        "tokenUsage": {
            "totalPromptTokens": 0,
            "totalCompletionTokens": 0,
            "dailyPromptTokens": 0,
            "dailyCompletionTokens": 0,
            "lastTokenReset": datetime.now(timezone.utc),
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
        "preferences": {"defaultWorkspace": "data"},
    }
    result = await state.users_col.insert_one(new_user)
    user_doc = await state.users_col.find_one({"_id": result.inserted_id})
    logger.info("user_created_google email=%s", idinfo["email"])
    return user_doc


async def _reset_quotas_if_needed(user_doc: dict) -> dict:
    now = datetime.utcnow()
    today = datetime(now.year, now.month, now.day)
    last_reset = user_doc.get("quotas", {}).get("lastQuotaReset")
    if last_reset:
        last_reset_day = datetime(last_reset.year, last_reset.month, last_reset.day)
        if today > last_reset_day:
            await state.users_col.update_one(
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
                    "lastAiQuotaReset": datetime.utcnow(),
                }},
            )
            user_doc = await state.users_col.find_one({"_id": user_doc["_id"]})
    return user_doc


# ── The auth dependency ──────────────────────────────────────────────────────

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """
    Single source of truth for auth.

    Order:
      1. Google ID token (verified against Google JWKS)
      2. Internal JWT (issued by GitHub OAuth / email login)
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
    return await _reset_quotas_if_needed(user_doc)


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
        resp = await HTTP_CLIENT.post(
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
        user_resp = await HTTP_CLIENT.get(
            "https://api.github.com/user", headers=user_headers, timeout=10.0,
        )
        user_resp.raise_for_status()
        user_data = user_resp.json()
    except Exception as e:
        logger.error("github_user_fetch_failed error=%s", e)
        raise HTTPException(status_code=503, detail="Failed to fetch GitHub profile")

    try:
        email_resp = await HTTP_CLIENT.get(
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
                "lastQuotaReset": datetime.utcnow(),
            },
            "tokenUsage": {
                "totalPromptTokens": 0,
                "totalCompletionTokens": 0,
                "dailyPromptTokens": 0,
                "dailyCompletionTokens": 0,
                "lastTokenReset": datetime.utcnow(),
            },
            "isAdmin": primary_email == ADMIN_EMAIL,
            "puter_enabled": False,
            "preferences": {"defaultWorkspace": "data"},
            "createdAt": datetime.utcnow(),
        }
        result = await state.users_col.insert_one(new_user)
        user_doc = await state.users_col.find_one({"_id": result.inserted_id})
        logger.info("user_created_github email=%s", primary_email)
    else:
        await state.users_col.update_one(
            {"_id": user_doc["_id"]},
            {"$set": {"lastUsageDate": datetime.utcnow()}},
        )
        user_doc = await state.users_col.find_one({"_id": user_doc["_id"]})
        user_doc = await _reset_quotas_if_needed(user_doc)

    token = create_access_token({"sub": user_doc["email"]})
    return RedirectResponse(url=f"{ORIGIN}/?auth=github&token={token}")


# ── WebAuthn (guarded — optional) ────────────────────────────────────────────

WEBAUTHN_AVAILABLE = False
try:
    from webauthn import (
        generate_authentication_options,
        generate_registration_options,
        verify_authentication_response,
        verify_registration_response,
    )
    from webauthn.helpers.structs import (
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
        email: str

    class WebAuthnRegistrationFinishRequest(BaseModel):
        email: str
        credential: dict

    class WebAuthnLoginBeginRequest(BaseModel):
        email: str

    class WebAuthnLoginFinishRequest(BaseModel):
        email: str
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

    async def _get_webauthn_credential(user_id, credential_id: bytes):
        user = await state.users_col.find_one({"_id": user_id})
        if not user:
            return None
        for cred in user.get("webauthnCredentials", []):
            if cred["credentialId"] == credential_id.hex():
                return cred
        return None

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
        challenge_data = await get_redis_cache(
            f"webauthn_challenge:{data.credential.get('challenge')}"
        )
        if not challenge_data or challenge_data.get("email") != data.email:
            raise HTTPException(status_code=400, detail="Invalid or expired challenge")
        await delete_redis_cache(f"webauthn_challenge:{data.credential.get('challenge')}")
        try:
            credential = RegistrationCredential(**data.credential)
            verification = verify_registration_response(
                credential=credential,
                expected_challenge=data.credential.get("challenge"),
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
        try:
            credential = AuthenticationCredential(**data.credential)
            stored_cred = await _get_webauthn_credential(
                user["_id"], bytes.fromhex(credential.id)
            )
            if not stored_cred:
                raise HTTPException(status_code=400, detail="Credential not found")
            verification = verify_authentication_response(
                credential=credential,
                expected_challenge=challenge_token,
                expected_rp_id=RP_ID,
                expected_origin=ORIGIN,
                credential_public_key=bytes.fromhex(stored_cred["publicKey"]),
                credential_current_sign_count=stored_cred["signCount"],
            )
            await state.users_col.update_one(
                {"_id": user["_id"], "webauthnCredentials.credentialId": credential.id},
                {"$set": {"webauthnCredentials.$.signCount": verification.new_sign_count}},
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("webauthn_login_verify_failed error=%s", e)
            raise HTTPException(status_code=400, detail="Authentication failed")
        token = create_access_token({"sub": user["email"]})
        return {"success": True, "token": token}


# ── Email / password ─────────────────────────────────────────────────────────

class EmailLoginRequest(BaseModel):
    email: str
    password: str


@router.post("/api/auth/email")
async def email_login(data: EmailLoginRequest):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    user = await state.users_col.find_one({"email": data.email})
    if not user:
        raise HTTPException(401, "User not found")
    if not verify_password(data.password, user.get("password_hash", "")):
        raise HTTPException(401, "Invalid password")
    token = create_access_token({"sub": user["email"]})
    return {"success": True, "token": token}


@router.post("/api/auth/email/register")
async def email_register(data: EmailLoginRequest):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    existing = await state.users_col.find_one({"email": data.email})
    if existing:
        raise HTTPException(400, "Email already registered")
    new_user = {
        "email": data.email,
        "displayName": data.email.split("@")[0],
        "password_hash": hash_password(data.password),
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
            "lastQuotaReset": datetime.utcnow(),
        },
        "tokenUsage": {
            "totalPromptTokens": 0,
            "totalCompletionTokens": 0,
            "dailyPromptTokens": 0,
            "dailyCompletionTokens": 0,
            "lastTokenReset": datetime.utcnow(),
        },
        "isAdmin": data.email == ADMIN_EMAIL,
        "puter_enabled": False,
        "preferences": {"defaultWorkspace": "data"},
        "createdAt": datetime.utcnow(),
    }
    result = await state.users_col.insert_one(new_user)
    user_doc = await state.users_col.find_one({"_id": result.inserted_id})
    token = create_access_token({"sub": user_doc["email"]})
    return {"success": True, "token": token}


# ── Public surface ───────────────────────────────────────────────────────────
__all__ = [
    "router",
    "get_current_user",
    "security",
    "hash_password",
    "verify_password",
    "create_access_token",
    "decode_token",
    "WEBAUTHN_AVAILABLE",
    "_create_user_from_google",
    "_reset_quotas_if_needed",
]