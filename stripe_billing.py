"""Stripe customer / subscription helpers for auth and billing sync."""

from __future__ import annotations

import logging
import re

import stripe

import config
import db

logger = logging.getLogger(__name__)

_PRICE_ID_RE = re.compile(r"^price_[A-Za-z0-9]+$")
_PLACEHOLDER_PRICE_IDS = {"price_...", "price_xxx", "price_test", "price_live"}


def stripe_status_from_subscription(subscription):
    status = subscription.get("status", "none")
    if status in ("active", "trialing"):
        return "active"
    if status in ("canceled", "unpaid", "incomplete_expired"):
        return "canceled"
    return status


def stripe_key_mode(secret_key: str | None = None) -> str:
    """Return 'test', 'live', or 'unknown' from a Stripe secret key prefix."""
    key = secret_key if secret_key is not None else config.STRIPE_SECRET_KEY
    if not key:
        return "unknown"
    if key.startswith("sk_test"):
        return "test"
    if key.startswith("sk_live"):
        return "live"
    return "unknown"


def publishable_key_mode(publishable_key: str | None = None) -> str:
    key = publishable_key if publishable_key is not None else getattr(
        config, "STRIPE_PUBLISHABLE_KEY", None
    )
    if not key:
        return "unknown"
    if key.startswith("pk_test"):
        return "test"
    if key.startswith("pk_live"):
        return "live"
    return "unknown"


def stripe_mode_mismatch(
    secret_key: str | None = None, publishable_key: str | None = None
) -> bool:
    """True when both keys are set and their test/live modes disagree."""
    secret_mode = stripe_key_mode(secret_key)
    pub_mode = publishable_key_mode(publishable_key)
    if secret_mode == "unknown" or pub_mode == "unknown":
        return False
    return secret_mode != pub_mode


def is_valid_stripe_price_id(price_id: str | None) -> bool:
    if not price_id:
        return False
    cleaned = str(price_id).strip()
    if cleaned in _PLACEHOLDER_PRICE_IDS:
        return False
    if len(cleaned) < 12:
        return False
    return bool(_PRICE_ID_RE.match(cleaned))


def billing_is_configured() -> bool:
    return bool(
        config.STRIPE_SECRET_KEY
        and is_valid_stripe_price_id(config.STRIPE_PRICE_ID)
        and not stripe_mode_mismatch()
    )


def billing_config_error() -> str | None:
    """Human-safe reason billing cannot start checkout (no secrets)."""
    if not config.STRIPE_SECRET_KEY:
        return "missing_secret_key"
    if not is_valid_stripe_price_id(config.STRIPE_PRICE_ID):
        return "invalid_price_id"
    if stripe_mode_mismatch():
        return "mode_mismatch"
    return None


def stripe_customer_for_email(email):
    if not config.STRIPE_SECRET_KEY:
        return None
    customers = stripe.Customer.list(email=email, limit=1)
    return customers.data[0] if customers.data else None


def stripe_has_active_subscription(email):
    customer = stripe_customer_for_email(email)
    if not customer:
        return False
    for status in ("active", "trialing"):
        subs = stripe.Subscription.list(customer=customer.id, status=status, limit=1)
        if subs.data:
            return True
    return False


def sync_user_from_stripe(user, email):
    if not config.STRIPE_SECRET_KEY:
        return
    customer = stripe_customer_for_email(email)
    if not customer:
        return
    db.set_stripe_customer(user["id"], customer.id)
    for status in ("active", "trialing"):
        subs = stripe.Subscription.list(customer=customer.id, status=status, limit=1)
        if subs.data:
            db.update_user_subscription(
                user["id"],
                stripe_status_from_subscription(subs.data[0]),
                subscription_id=subs.data[0].id,
                stripe_customer_id=customer.id,
            )
            return


def ensure_stripe_customer(user):
    """Return Stripe customer id for user, creating or reusing by email."""
    customer_id = user.get("stripe_customer_id")
    if customer_id:
        return customer_id
    existing = stripe_customer_for_email(user["email"])
    if existing:
        db.set_stripe_customer(user["id"], existing.id)
        return existing.id
    customer = stripe.Customer.create(
        email=user["email"],
        metadata={"user_id": str(user["id"])},
    )
    db.set_stripe_customer(user["id"], customer.id)
    return customer.id


def create_subscription_checkout_session(user, *, success_url: str, cancel_url: str):
    """Create a Stripe Checkout Session for subscription. Does not charge until paid."""
    if not billing_is_configured():
        raise RuntimeError(f"Billing not configured: {billing_config_error()}")
    customer_id = ensure_stripe_customer(user)
    return stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": config.STRIPE_PRICE_ID, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=str(user["id"]),
        metadata={"user_id": str(user["id"])},
        allow_promotion_codes=True,
    )
