"""Stripe customer / subscription helpers for auth and billing sync."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timezone

import stripe

import config
import db

logger = logging.getLogger(__name__)

_PRICE_ID_RE = re.compile(r"^price_[A-Za-z0-9]+$")
_PLACEHOLDER_PRICE_IDS = {"price_...", "price_xxx", "price_test", "price_live"}

# Stripe statuses that must not get a second Checkout Session.
_BLOCKING_STATUSES = (
    "active",
    "trialing",
    "past_due",
    "incomplete",
    "unpaid",
    "paused",
)


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def stripe_status_from_subscription(subscription):
    status = _sub_get(subscription, "status") or "none"
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


def _sub_get(subscription, key, default=None):
    if subscription is None:
        return default
    if isinstance(subscription, dict):
        return subscription.get(key, default)
    return getattr(subscription, key, default)


def format_period_end(unix_ts) -> str | None:
    if not unix_ts:
        return None
    try:
        dt = datetime.fromtimestamp(int(unix_ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None
    return dt.strftime("%B %d, %Y")


def stripe_customer_for_email(email):
    if not config.STRIPE_SECRET_KEY:
        return None
    email = normalize_email(email)
    if not email:
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


def list_blocking_subscriptions(customer_id):
    """Subscriptions that must block creating another Checkout Session."""
    if not customer_id or not config.STRIPE_SECRET_KEY:
        return []
    found = []
    seen = set()
    for status in _BLOCKING_STATUSES:
        try:
            subs = stripe.Subscription.list(customer=customer_id, status=status, limit=10)
        except stripe.StripeError:
            logger.exception("Failed listing Stripe subscriptions status=%s", status)
            continue
        for sub in subs.data:
            sub_id = _sub_get(sub, "id")
            if sub_id and sub_id in seen:
                continue
            if sub_id:
                seen.add(sub_id)
            found.append(sub)
    return found


def classify_subscription(subscription) -> dict:
    """Classify a Stripe subscription into a subscribe-gate state."""
    status = (_sub_get(subscription, "status") or "none").lower()
    cancel_at_period_end = bool(_sub_get(subscription, "cancel_at_period_end"))
    period_end = _sub_get(subscription, "current_period_end")
    now = int(time.time())

    if status in ("active", "trialing"):
        if cancel_at_period_end and period_end and int(period_end) > now:
            state = "canceling"
        else:
            state = "active"
    elif status in ("past_due", "incomplete", "unpaid", "paused"):
        state = status
    else:
        state = status or "none"

    return {
        "state": state,
        "status": status,
        "subscription_id": _sub_get(subscription, "id"),
        "cancel_at_period_end": cancel_at_period_end,
        "current_period_end": period_end,
        "access_ends_on": format_period_end(period_end),
    }


def _gate_from_local_status(local_status: str | None) -> dict | None:
    status = (local_status or "none").lower()
    if status == "active":
        return {
            "can_checkout": False,
            "state": "active",
            "message": "Your subscription is already active.",
            "access_ends_on": None,
            "show_manage_billing": True,
            "show_open_tools": True,
            "redirect": "subscriber_app",
        }
    if status in ("past_due", "incomplete", "unpaid", "paused"):
        labels = {
            "past_due": "Your payment is past due. Update billing to restore full access — do not start a new subscription.",
            "incomplete": "Your checkout was not completed. Resume billing below instead of creating a new subscription.",
            "unpaid": "Your subscription has an unpaid invoice. Update billing to continue — a new subscription is blocked.",
            "paused": "Your subscription is paused. Manage billing to resume — a new subscription is blocked.",
        }
        return {
            "can_checkout": False,
            "state": status,
            "message": labels[status],
            "access_ends_on": None,
            "show_manage_billing": True,
            "show_open_tools": False,
            "redirect": None,
        }
    return None


def resolve_subscribe_gate(user, *, check_stripe: bool = True) -> dict:
    """
    Decide whether /subscribe may create Checkout for this user.

    Prefer live Stripe when configured; fall back to local subscription_status.
    """
    allow = {
        "can_checkout": True,
        "state": "none",
        "message": None,
        "access_ends_on": None,
        "show_manage_billing": False,
        "show_open_tools": False,
        "redirect": None,
        "subscription_id": None,
    }
    if not user:
        return allow

    if auth_free_access(user):
        return {
            **allow,
            "can_checkout": False,
            "state": "active",
            "message": "Your subscription is already active.",
            "show_open_tools": True,
            "redirect": "subscriber_app",
        }

    stripe_gate = None
    if check_stripe and config.STRIPE_SECRET_KEY:
        try:
            customer_id = user.get("stripe_customer_id")
            if not customer_id:
                existing = stripe_customer_for_email(user.get("email"))
                if existing:
                    customer_id = existing.id
                    db.set_stripe_customer(user["id"], customer_id)
            if customer_id:
                blocking = list_blocking_subscriptions(customer_id)
                if blocking:
                    # Prefer the most actionable subscription (recovery > canceling > active).
                    classified = [classify_subscription(s) for s in blocking]
                    priority = {
                        "past_due": 0,
                        "unpaid": 1,
                        "incomplete": 2,
                        "paused": 3,
                        "canceling": 4,
                        "active": 5,
                        "trialing": 5,
                    }
                    classified.sort(key=lambda c: priority.get(c["state"], 99))
                    top = classified[0]
                    state = top["state"]
                    if state == "active":
                        stripe_gate = {
                            "can_checkout": False,
                            "state": "active",
                            "message": "Your subscription is already active.",
                            "access_ends_on": top.get("access_ends_on"),
                            "show_manage_billing": True,
                            "show_open_tools": True,
                            "redirect": "subscriber_app",
                            "subscription_id": top.get("subscription_id"),
                        }
                    elif state == "canceling":
                        end = top.get("access_ends_on") or "the end of your billing period"
                        stripe_gate = {
                            "can_checkout": False,
                            "state": "canceling",
                            "message": (
                                f"Your subscription is set to cancel. "
                                f"Access continues until {end}. "
                                "Manage billing to reactivate — a new subscription is not needed."
                            ),
                            "access_ends_on": top.get("access_ends_on"),
                            "show_manage_billing": True,
                            "show_open_tools": True,
                            "redirect": None,
                            "subscription_id": top.get("subscription_id"),
                        }
                    else:
                        localish = _gate_from_local_status(state)
                        stripe_gate = {
                            **(localish or allow),
                            "can_checkout": False,
                            "state": state,
                            "access_ends_on": top.get("access_ends_on"),
                            "subscription_id": top.get("subscription_id"),
                            "show_manage_billing": True,
                        }
        except stripe.StripeError:
            logger.exception(
                "Stripe subscribe-gate check failed for user_id=%s", user.get("id")
            )

    if stripe_gate:
        # Keep local DB roughly aligned when Stripe is authoritative.
        mapped = stripe_status_from_subscription(
            {"status": "active" if stripe_gate["state"] in ("active", "canceling") else stripe_gate["state"]}
        )
        if stripe_gate["state"] == "canceling":
            mapped = "active"
        try:
            if user.get("subscription_status") != mapped or (
                stripe_gate.get("subscription_id")
                and user.get("subscription_id") != stripe_gate.get("subscription_id")
            ):
                db.update_user_subscription(
                    user["id"],
                    mapped,
                    subscription_id=stripe_gate.get("subscription_id"),
                )
        except Exception:
            logger.exception("Failed syncing local status from Stripe gate")
        return stripe_gate

    local = _gate_from_local_status(user.get("subscription_status"))
    if local:
        return local
    return allow


def auth_free_access(user) -> bool:
    email = normalize_email((user or {}).get("email"))
    return bool(email and email in getattr(config, "FREE_ACCESS_EMAILS", set()))


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
    import registration_gate

    customer_id = user.get("stripe_customer_id")
    if customer_id:
        return customer_id
    existing = stripe_customer_for_email(user["email"])
    if existing:
        db.set_stripe_customer(user["id"], existing.id)
        return existing.id
    # Creating a new Stripe Customer is part of signup/checkout — gate first.
    registration_gate.assert_registration_allowed(user.get("email"))
    customer = stripe.Customer.create(
        email=normalize_email(user["email"]),
        metadata={"user_id": str(user["id"])},
    )
    db.set_stripe_customer(user["id"], customer.id)
    return customer.id


def checkout_idempotency_key(user_id, *, bucket_seconds: int = 120) -> str:
    """Stable key across double-clicks / refresh within a short window."""
    bucket = int(time.time()) // max(int(bucket_seconds), 1)
    raw = f"sub_checkout:{user_id}:{bucket}:{config.STRIPE_PRICE_ID}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"subchk_{user_id}_{digest}"


def create_subscription_checkout_session(
    user,
    *,
    success_url: str,
    cancel_url: str,
    idempotency_key: str | None = None,
):
    """Create a Stripe Checkout Session for subscription. Does not charge until paid."""
    import registration_gate

    # Enforce before any Stripe Customer or Checkout Session mutation.
    registration_gate.assert_registration_allowed((user or {}).get("email"))
    if not billing_is_configured():
        raise RuntimeError(f"Billing not configured: {billing_config_error()}")
    customer_id = ensure_stripe_customer(user)
    params = dict(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": config.STRIPE_PRICE_ID, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=str(user["id"]),
        metadata={"user_id": str(user["id"])},
        allow_promotion_codes=True,
    )
    key = idempotency_key or checkout_idempotency_key(user["id"])
    return stripe.checkout.Session.create(**params, idempotency_key=key)
