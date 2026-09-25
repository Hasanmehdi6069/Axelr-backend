# api/routes_billing.py
"""
AXELR — Stripe billing.
=======================
Checkout · Portal · Status · Cancel · Webhook (signature-verified, idempotent).

Every Stripe SDK call is wrapped in ``asyncio.to_thread`` so the 0.1-CPU
Render Free Tier event loop never blocks.

Environment:
  STRIPE_SECRET_KEY      – required for any billing route to work
  STRIPE_WEBHOOK_SECRET  – required to verify webhook signatures
  STRIPE_PRICE_*         – optional; falls back to inline price_data
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .auth import get_current_user
from .config import (
    ADMIN_EMAIL,
    SMTP_HOST,
    SMTP_PASS,
    SMTP_PORT,
    SMTP_USER,
    STRIPE_CANCEL_URL,
    STRIPE_PORTAL_RETURN,
    STRIPE_PRICE_AMOUNTS,
    STRIPE_PRICE_CATALOG,
    STRIPE_SECRET_KEY,
    STRIPE_SUCCESS_URL,
    STRIPE_TRIAL_DAYS,
    STRIPE_WEBHOOK_SECRET,
    TIER_LABELS,
    VALID_PERIODS,
    VALID_SUBTIERS,
    VALID_TIERS,
)
from .state import get_object_id, limiter, state

logger = logging.getLogger("axelr.billing")

router = APIRouter(tags=["billing"])


# ═══════════════════════════════════════════════════════════════════════════
# Stripe init — guarded, never raises
# ═══════════════════════════════════════════════════════════════════════════

try:
    import stripe
    STRIPE_LIB_AVAILABLE = True
except ImportError:
    stripe = None                                    # type: ignore
    STRIPE_LIB_AVAILABLE = False

STRIPE_AVAILABLE = False
if STRIPE_LIB_AVAILABLE and STRIPE_SECRET_KEY:
    try:
        stripe.api_key = STRIPE_SECRET_KEY
        stripe.max_network_retries = 2
        stripe.app_info = {"name": "Axelr AI", "version": "24.4"}
        STRIPE_AVAILABLE = True
        logger.info("stripe_initialized")
    except Exception as e:                            # noqa: BLE001
        logger.warning("stripe_init_failed error=%s", e)
elif not STRIPE_LIB_AVAILABLE:
    logger.warning("stripe_lib_missing")
elif not STRIPE_SECRET_KEY:
    logger.warning("STRIPE_SECRET_KEY_missing")


def _require_stripe() -> None:
    if not STRIPE_AVAILABLE:
        raise HTTPException(status_code=503, detail="Billing temporarily unavailable")


# ═══════════════════════════════════════════════════════════════════════════
# Request models
# ═══════════════════════════════════════════════════════════════════════════

class CheckoutRequest(BaseModel):
    tier: str
    subTier: str = "full"
    period: str = "monthly"


class PortalRequest(BaseModel):
    returnUrl: str | None = None


# ═══════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════

def _resolve_price(tier: str, sub_tier: str, period: str) -> dict[str, Any]:
    """Return the ``line_items[0]`` payload for Stripe."""
    price_id = (STRIPE_PRICE_CATALOG.get(tier, {})
                                    .get(sub_tier, {})
                                    .get(period))
    if price_id:
        return {"price": price_id, "quantity": 1}

    amount = (STRIPE_PRICE_AMOUNTS.get(tier, {})
                                   .get(sub_tier, {})
                                   .get(period))
    if not amount:
        raise HTTPException(status_code=400, detail="Invalid pricing combination")

    interval = "year" if period == "annual" else "month"
    label = TIER_LABELS.get((tier, sub_tier), f"Axelr {tier.title()} ({sub_tier})")

    return {
        "quantity": 1,
        "price_data": {
            "currency": "usd",
            "unit_amount": amount,
            "recurring": {"interval": interval},
            "product_data": {
                "name": label,
                "description": f"Axelr AI · {tier.upper()} tier · {sub_tier} workspace access",
                "metadata": {"tier": tier, "subTier": sub_tier, "period": period},
            },
        },
    }


async def _ensure_stripe_customer(user: dict) -> str:
    """Return a valid Stripe Customer ID, creating one if needed."""
    existing = user.get("stripeCustomerId")
    if existing:
        try:
            cust = await asyncio.to_thread(stripe.Customer.retrieve, existing)
            if not getattr(cust, "deleted", False):
                return existing
        except Exception as e:                        # noqa: BLE001
            logger.warning("stripe_customer_stale cid=%s error=%s", existing, e)

    metadata = {"axelrUserId": str(user["_id"])}
    if user.get("googleId"):
        metadata["googleId"] = user["googleId"]
    if user.get("githubId"):
        metadata["githubId"] = user["githubId"]

    customer = await asyncio.to_thread(
        stripe.Customer.create,
        email=user.get("email"),
        name=user.get("displayName") or user.get("email"),
        metadata=metadata,
    )
    await state.users_col.update_one(
        {"_id": user["_id"]},
        {"$set": {"stripeCustomerId": customer.id}},
    )
    logger.info("stripe_customer_created cid=%s", customer.id)
    return customer.id


async def _apply_subscription_to_user(user_doc: dict, sub: dict) -> None:
    """Idempotently apply a Stripe subscription object to a user document."""
    meta = sub.get("metadata", {}) or {}
    tier = (meta.get("tier") or "pro").lower()
    sub_tier = (meta.get("subTier") or "full").lower()
    period = (meta.get("period") or "monthly").lower()

    has_data = sub_tier in ("full", "data")
    has_design = sub_tier in ("full", "design")
    status = sub.get("status")
    is_active = status in ("active", "trialing")

    update = {
        "tier": tier if is_active else "free",
        "stripeCustomerId": sub.get("customer"),
        "stripeSubscriptionId": sub.get("id"),
        "subscriptionStatus": status,
        "subscriptionCancelAtPeriodEnd": bool(sub.get("cancel_at_period_end", False)),
        "subscriptionCurrentPeriodEnd": (
            datetime.utcfromtimestamp(sub["current_period_end"]).isoformat()
            if sub.get("current_period_end") else None
        ),
        "subTierOptions.hasDataAccess": has_data if is_active else False,
        "subTierOptions.hasDesignAccess": has_design if is_active else False,
        "billingPeriod": period,
    }
    await state.users_col.update_one({"_id": user_doc["_id"]}, {"$set": update})
    logger.info("subscription_applied email=%s status=%s", user_doc.get("email"), status)


async def _find_user_for_subscription(sub: dict) -> dict | None:
    """Resolve a Stripe subscription to an Axelr user: metadata → customer → email."""
    meta = sub.get("metadata", {}) or {}

    axelr_uid = meta.get("axelrUserId")
    if axelr_uid:
        ObjectId = get_object_id()
        if ObjectId and ObjectId.is_valid(axelr_uid):
            doc = await state.users_col.find_one({"_id": ObjectId(axelr_uid)})
            if doc:
                return doc

    customer_id = sub.get("customer")
    if customer_id:
        doc = await state.users_col.find_one({"stripeCustomerId": customer_id})
        if doc:
            return doc
        try:
            cust = await asyncio.to_thread(stripe.Customer.retrieve, customer_id)
            email = getattr(cust, "email", None)
            if email:
                doc = await state.users_col.find_one({"email": email})
                if doc:
                    return doc
        except Exception as e:                        # noqa: BLE001
            logger.warning("stripe_customer_lookup_failed error=%s", e)
    return None


def _get_email_transport():
    """Best-effort SMTP login. Returns an SMTP object or None."""
    if not (SMTP_USER and SMTP_PASS):
        return None
    import smtplib
    try:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        return server
    except Exception as e:                            # noqa: BLE001
        logger.warning("email_transport_failed error=%s", e)
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Checkout
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/api/billing/checkout")
@limiter.limit("5/minute")
async def billing_checkout(request: Request, data: CheckoutRequest,
                            user: dict = Depends(get_current_user)):
    _require_stripe()

    tier = (data.tier or "").lower().strip()
    sub_tier = (data.subTier or "full").lower().strip()
    period = (data.period or "monthly").lower().strip()

    if tier not in VALID_TIERS:
        raise HTTPException(400, "Invalid tier")
    if sub_tier not in VALID_SUBTIERS:
        raise HTTPException(400, "Invalid sub-tier")
    if period not in VALID_PERIODS:
        raise HTTPException(400, "Invalid billing period")

    current_tier = (user.get("tier") or "free").lower()
    if current_tier == tier:
        raise HTTPException(409, "Already on this tier. Use the portal to change plans.")

    customer_id = await _ensure_stripe_customer(user)
    line_item = _resolve_price(tier, sub_tier, period)

    try:
        session = await asyncio.to_thread(
            stripe.checkout.Session.create,
            mode="subscription",
            customer=customer_id,
            line_items=[line_item],
            allow_promotion_codes=True,
            billing_address_collection="auto",
            automatic_tax={"enabled": True},
            success_url=f"{STRIPE_SUCCESS_URL}&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=STRIPE_CANCEL_URL,
            client_reference_id=str(user["_id"]),
            subscription_data={
                "metadata": {
                    "axelrUserId": str(user["_id"]),
                    "tier": tier, "subTier": sub_tier, "period": period,
                },
                **({"trial_period_days": STRIPE_TRIAL_DAYS} if STRIPE_TRIAL_DAYS > 0 else {}),
            },
            metadata={
                "axelrUserId": str(user["_id"]),
                "tier": tier, "subTier": sub_tier, "period": period,
            },
        )
    except stripe.error.StripeError as e:             # type: ignore[union-attr]
        logger.error("stripe_checkout_failed error=%s", e)
        raise HTTPException(502, f"Stripe error: {getattr(e, 'user_message', None) or e!s}")

    return {"success": True, "url": session.url, "sessionId": session.id}


# ═══════════════════════════════════════════════════════════════════════════
# Portal
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/api/billing/portal")
async def billing_portal(data: PortalRequest, user: dict = Depends(get_current_user)):
    _require_stripe()

    customer_id = user.get("stripeCustomerId")
    if not customer_id:
        raise HTTPException(400, "No active subscription found")

    try:
        portal = await asyncio.to_thread(
            stripe.billing_portal.Session.create,
            customer=customer_id,
            return_url=data.returnUrl or STRIPE_PORTAL_RETURN,
        )
    except stripe.error.StripeError as e:             # type: ignore[union-attr]
        logger.error("stripe_portal_failed error=%s", e)
        raise HTTPException(502, f"Stripe error: {getattr(e, 'user_message', None) or e!s}")

    return {"success": True, "url": portal.url}


# ═══════════════════════════════════════════════════════════════════════════
# Status
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/api/billing/status")
async def billing_status(user: dict = Depends(get_current_user)):
    tier = user.get("tier", "free")
    sub_opts = user.get("subTierOptions", {}) or {}
    subscription_id = user.get("stripeSubscriptionId")

    payload = {
        "tier": tier,
        "subTierOptions": sub_opts,
        "isPaid": tier in ("pro", "business"),
        "hasActiveSubscription": bool(subscription_id),
        "stripeCustomerId": user.get("stripeCustomerId"),
        "subscriptionId": subscription_id,
        "cancelAtPeriodEnd": bool(user.get("subscriptionCancelAtPeriodEnd", False)),
        "currentPeriodEnd": user.get("subscriptionCurrentPeriodEnd"),
    }

    if STRIPE_AVAILABLE and subscription_id:
        try:
            sub = await asyncio.to_thread(stripe.Subscription.retrieve, subscription_id)
            payload.update({
                "status": sub.status,
                "cancelAtPeriodEnd": sub.cancel_at_period_end,
                "currentPeriodEnd": (
                    datetime.utcfromtimestamp(sub.current_period_end).isoformat()
                    if sub.current_period_end else None
                ),
            })
        except Exception as e:                        # noqa: BLE001
            logger.warning("subscription_refresh_failed error=%s", e)

    return payload


# ═══════════════════════════════════════════════════════════════════════════
# Cancel
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/api/billing/cancel")
async def billing_cancel(user: dict = Depends(get_current_user)):
    _require_stripe()

    subscription_id = user.get("stripeSubscriptionId")
    if not subscription_id:
        raise HTTPException(400, "No active subscription to cancel")

    try:
        sub = await asyncio.to_thread(
            stripe.Subscription.modify,
            subscription_id,
            cancel_at_period_end=True,
        )
    except stripe.error.StripeError as e:             # type: ignore[union-attr]
        logger.error("stripe_cancel_failed error=%s", e)
        raise HTTPException(502, f"Stripe error: {getattr(e, 'user_message', None) or e!s}")

    await state.users_col.update_one(
        {"_id": user["_id"]},
        {"$set": {
            "subscriptionCancelAtPeriodEnd": True,
            "subscriptionCurrentPeriodEnd": (
                datetime.utcfromtimestamp(sub.current_period_end).isoformat()
                if sub.current_period_end else None
            ),
        }},
    )
    return {"success": True, "cancelAtPeriodEnd": True}


# ═══════════════════════════════════════════════════════════════════════════
# Webhook — signature-verified, idempotent
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request):
    """
    Return codes:
        200 — processed, or already processed (idempotent)
        400 — bad signature / bad payload       (Stripe retries)
        503 — transient (DB down)               (Stripe retries)
    """
    if not STRIPE_AVAILABLE:
        return JSONResponse(status_code=200,
                            content={"received": True, "note": "stripe_disabled"})

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    if not sig_header:
        raise HTTPException(400, "Missing stripe-signature header")
    if not STRIPE_WEBHOOK_SECRET:
        logger.error("stripe_webhook_secret_missing")
        raise HTTPException(503, "Webhook not configured")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError as e:   # type: ignore[union-attr]
        logger.warning("stripe_signature_invalid error=%s", e)
        raise HTTPException(400, "Invalid signature")
    except Exception as e:                                 # noqa: BLE001
        logger.error("stripe_payload_invalid error=%s", e)
        raise HTTPException(400, "Invalid payload")

    event_id = event.get("id")
    event_type = event.get("type")
    data_obj = (event.get("data") or {}).get("object") or {}
    logger.info("stripe_webhook_received type=%s id=%s", event_type, event_id)

    if not state.db_available:
        logger.error("stripe_webhook_db_unavailable")
        raise HTTPException(503, "DB unavailable, retry")

    # Idempotency guard
    events_col = state.db.get_collection("stripe_events")
    try:
        await events_col.insert_one({
            "_id": event_id, "type": event_type,
            "receivedAt": datetime.utcnow(),
        })
    except Exception:
        logger.info("stripe_webhook_duplicate id=%s", event_id)
        return {"received": True, "duplicate": True}

    try:
        # ─── CHECKOUT COMPLETED ───────────────────────────────────────────
        if event_type == "checkout.session.completed":
            session = data_obj
            subscription_id = session.get("subscription")
            customer_id = session.get("customer")
            client_ref = session.get("client_reference_id")

            user_doc = None
            if client_ref:
                ObjectId = get_object_id()
                if ObjectId and ObjectId.is_valid(client_ref):
                    user_doc = await state.users_col.find_one({"_id": ObjectId(client_ref)})
            if not user_doc and customer_id:
                user_doc = await state.users_col.find_one({"stripeCustomerId": customer_id})
            if not user_doc:
                logger.warning("checkout_no_user session=%s", session.get("id"))
                return {"received": True}

            if customer_id and not user_doc.get("stripeCustomerId"):
                await state.users_col.update_one(
                    {"_id": user_doc["_id"]},
                    {"$set": {"stripeCustomerId": customer_id}},
                )

            if subscription_id:
                try:
                    sub = await asyncio.to_thread(stripe.Subscription.retrieve, subscription_id)
                    sub = (sub.to_dict_recursive()
                           if hasattr(sub, "to_dict_recursive") else dict(sub))
                    if not sub.get("metadata"):
                        sub["metadata"] = session.get("metadata", {}) or {}
                    await _apply_subscription_to_user(user_doc, sub)
                except Exception as e:                # noqa: BLE001
                    logger.error("checkout_sub_fetch_failed error=%s", e)

            # welcome email (best-effort)
            if SMTP_USER and SMTP_PASS:
                try:
                    from email.mime.multipart import MIMEMultipart
                    from email.mime.text import MIMEText
                    server = _get_email_transport()
                    if server:
                        tier_label = (session.get("metadata", {}) or {}).get("tier", "pro").upper()
                        msg = MIMEMultipart()
                        msg["From"] = SMTP_USER
                        msg["To"] = user_doc["email"]
                        msg["Subject"] = f"Axelr AI — Welcome to {tier_label}"
                        msg.attach(MIMEText(
                            f"<h2>Welcome to Axelr AI {tier_label}!</h2>"
                            "<p>Your subscription is active.</p>",
                            "html",
                        ))
                        server.sendmail(SMTP_USER, user_doc["email"], msg.as_string())
                        server.quit()
                except Exception as e:                # noqa: BLE001
                    logger.warning("welcome_email_failed error=%s", e)

        # ─── SUBSCRIPTION CREATED / UPDATED ───────────────────────────────
        elif event_type in ("customer.subscription.created",
                            "customer.subscription.updated"):
            sub = data_obj
            user_doc = await _find_user_for_subscription(sub)
            if user_doc:
                await _apply_subscription_to_user(user_doc, sub)
            else:
                logger.warning("no_user_for_subscription id=%s", sub.get("id"))

        # ─── SUBSCRIPTION DELETED ─────────────────────────────────────────
        elif event_type == "customer.subscription.deleted":
            sub = data_obj
            user_doc = await _find_user_for_subscription(sub)
            if user_doc:
                await state.users_col.update_one(
                    {"_id": user_doc["_id"]},
                    {"$set": {
                        "tier": "free",
                        "subTierOptions.hasDataAccess": False,
                        "subTierOptions.hasDesignAccess": False,
                        "stripeSubscriptionId": None,
                        "subscriptionStatus": "canceled",
                        "subscriptionCancelAtPeriodEnd": False,
                        "subscriptionCurrentPeriodEnd": None,
                    }},
                )
                logger.info("subscription_cancelled email=%s", user_doc.get("email"))

        # ─── PAYMENT FAILED / ACTION REQUIRED ─────────────────────────────
        elif event_type in ("invoice.payment_failed", "invoice.payment_action_required"):
            customer_id = data_obj.get("customer")
            if customer_id:
                user_doc = await state.users_col.find_one({"stripeCustomerId": customer_id})
                if user_doc:
                    await state.users_col.update_one(
                        {"_id": user_doc["_id"]},
                        {"$set": {"subscriptionStatus": "past_due"}},
                    )

        # ─── INVOICE PAID ─────────────────────────────────────────────────
        elif event_type == "invoice.paid":
            sub_id = data_obj.get("subscription")
            if sub_id:
                try:
                    sub = await asyncio.to_thread(stripe.Subscription.retrieve, sub_id)
                    sub = (sub.to_dict_recursive()
                           if hasattr(sub, "to_dict_recursive") else dict(sub))
                    user_doc = await _find_user_for_subscription(sub)
                    if user_doc:
                        await _apply_subscription_to_user(user_doc, sub)
                except Exception as e:                # noqa: BLE001
                    logger.warning("invoice_paid_refresh_failed error=%s", e)

    except Exception as e:                            # noqa: BLE001
        logger.exception("stripe_webhook_processing_failed type=%s error=%s", event_type, e)
        # Remove the idempotency marker so Stripe's retry can re-process
        try:
            await events_col.delete_one({"_id": event_id})
        except Exception:
            pass
        raise HTTPException(500, "processing_error")

    return {"received": True}


__all__ = ["router", "STRIPE_AVAILABLE"]