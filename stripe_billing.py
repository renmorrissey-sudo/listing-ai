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
    """Map Stripe subscription.status to local subscription_status.

    Keep past_due / unpaid / incomplete / paused so billing recovery UI works.
    Store trialing distinctly for the Billing page (access still granted).
    """
    status = (_sub_get(subscription, "status") or "none").lower()
    if status in ("canceled", "incomplete_expired"):
        return "canceled"
    if status in (
        "active",
        "trialing",
        "past_due",
        "unpaid",
        "incomplete",
        "paused",
    ):
        return status
    return status or "none"


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
    if status in ("active", "trialing"):
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
            "show_open_tools": status == "past_due",
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


# Payment failure codes that mean the saved method must be replaced (not a soft retry).
PAYMENT_METHOD_UPDATE_CODES = frozenset(
    {
        "link_connection_closed",
        "payment_method_unavailable",
        "payment_method_provider_decline",
        "expired_card",
        "card_declined",
        "incorrect_cvc",
        "incorrect_number",
        "invalid_card_type",
        "invalid_cvc",
        "invalid_expiry_month",
        "invalid_expiry_year",
        "invalid_number",
        "lost_card",
        "stolen_card",
        "authentication_required",
    }
)

PAYMENT_METHOD_UPDATE_MESSAGE = (
    "Your saved payment method needs to be updated. "
    "Update your payment method to continue your TopAI subscription."
)


def billing_portal_return_url() -> str:
    """Return URL after Stripe Customer Portal. Production uses the public www host."""
    if getattr(config, "IS_PRODUCTION", False):
        return "https://www.topairealestatetools.com/billing"
    return f"{(config.APP_URL or 'http://localhost:8080').rstrip('/')}/billing"


def extract_subscription_fields(subscription) -> dict:
    """Pull id, status, price id, and period end from a Stripe Subscription object/dict."""
    sub_id = _sub_get(subscription, "id")
    status = stripe_status_from_subscription(subscription)
    period_end = _sub_get(subscription, "current_period_end")
    price_id = None
    items = _sub_get(subscription, "items")
    data = None
    if items is not None:
        if isinstance(items, dict):
            data = items.get("data") or []
        else:
            data = getattr(items, "data", None) or []
    if data:
        first = data[0]
        price = _sub_get(first, "price")
        if price is not None:
            price_id = _sub_get(price, "id") if not isinstance(price, str) else price
        if not price_id:
            price_id = _sub_get(first, "price")
            if isinstance(price_id, dict):
                price_id = price_id.get("id")
    return {
        "subscription_id": sub_id,
        "status": status,
        "stripe_price_id": price_id,
        "current_period_end": period_end,
    }


def payment_error_code_from_stripe_object(obj) -> str | None:
    """Best-effort decline/error code from Invoice or PaymentIntent payload."""
    if not obj:
        return None
    if isinstance(obj, dict):
        getter = obj.get
    else:
        getter = lambda k, d=None: getattr(obj, k, d)

    # Invoice: last_payment_error on the PaymentIntent, or charge outcome
    last_err = getter("last_payment_error")
    if last_err:
        code = _sub_get(last_err, "code") or _sub_get(last_err, "decline_code")
        if code:
            return str(code)

    # Nested payment_intent on invoice
    pi = getter("payment_intent")
    if isinstance(pi, dict):
        err = pi.get("last_payment_error") or {}
        code = err.get("code") or err.get("decline_code")
        if code:
            return str(code)

    # invoice.charge → outcome.reason sometimes
    outcome = getter("outcome")
    if outcome:
        reason = _sub_get(outcome, "reason") or _sub_get(outcome, "type")
        if reason:
            return str(reason)
    return None


def payment_failure_user_message(error_code: str | None) -> str:
    code = (error_code or "").strip().lower()
    if code in PAYMENT_METHOD_UPDATE_CODES or not code:
        return PAYMENT_METHOD_UPDATE_MESSAGE
    return PAYMENT_METHOD_UPDATE_MESSAGE


def status_display_label(status: str | None) -> str:
    labels = {
        "active": "Active",
        "trialing": "Trialing",
        "past_due": "Past Due",
        "canceled": "Canceled",
        "unpaid": "Unpaid",
        "incomplete": "Incomplete",
        "paused": "Paused",
        "none": "None",
    }
    key = (status or "none").lower()
    return labels.get(key, (status or "Unknown").replace("_", " ").title())


def build_billing_summary(user) -> dict:
    """Assemble Billing page fields from local DB (optionally enriched by Stripe)."""
    status = (user.get("subscription_status") or "none").lower()
    period_end = user.get("subscription_current_period_end")
    price_id = user.get("stripe_price_id") or config.STRIPE_PRICE_ID
    payment_action = bool(user.get("payment_action_required"))
    last_error = user.get("last_payment_error")
    customer_id = user.get("stripe_customer_id")

    if payment_action or status in ("past_due", "unpaid"):
        payment_status = "Payment action required"
        warning = payment_failure_user_message(last_error)
    elif status in ("active", "trialing"):
        payment_status = "Paid"
        warning = None
    elif status == "canceled":
        payment_status = "Canceled"
        warning = None
    elif status == "incomplete":
        payment_status = "Checkout incomplete"
        warning = None
    else:
        payment_status = "No active subscription"
        warning = None

    plan_name = "TopAI Monthly"
    monthly_price = config.SUBSCRIPTION_PRICE

    return {
        "plan_name": plan_name,
        "status": status,
        "status_label": status_display_label(status),
        "monthly_price": monthly_price,
        "stripe_price_id": price_id,
        "renewal_date": format_period_end(period_end),
        "current_period_end": period_end,
        "customer_email": user.get("email"),
        "stripe_customer_id": customer_id,
        "has_stripe_customer": bool(customer_id),
        "payment_status": payment_status,
        "payment_action_required": payment_action or status in ("past_due", "unpaid"),
        "warning_message": warning,
        "last_payment_error": last_error,
        "subscription_id": user.get("subscription_id"),
    }


def create_billing_portal_session(user, *, return_url: str | None = None):
    """
    Create a Stripe Billing Portal session for the authenticated user.

    Customer ID is ALWAYS taken from the database user row — never from the request.
    Returns (portal_url, error_code, http_status).
    error_code: None on success; 'no_customer' | 'not_configured' | 'stripe_error'
    """
    if not user:
        return None, "unauthorized", 401
    if not config.STRIPE_SECRET_KEY:
        return None, "not_configured", 503
    customer_id = user.get("stripe_customer_id")
    if not customer_id:
        return None, "no_customer", 400
    url = return_url or billing_portal_return_url()
    try:
        portal = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=url,
        )
        return portal.url, None, 200
    except stripe.StripeError:
        logger.exception(
            "Failed creating billing portal session for user_id=%s", user.get("id")
        )
        return None, "stripe_error", 502


def apply_subscription_to_user(user_id, subscription, *, stripe_customer_id=None):
    """Persist subscription fields from a Stripe Subscription object."""
    fields = extract_subscription_fields(subscription)
    status = fields["status"]
    clear_err = status in ("active", "trialing")
    db.update_user_subscription(
        user_id,
        status,
        subscription_id=fields["subscription_id"],
        stripe_customer_id=stripe_customer_id,
        stripe_price_id=fields.get("stripe_price_id"),
        current_period_end=fields.get("current_period_end"),
        clear_payment_error=clear_err,
        payment_action_required=(False if clear_err else None),
    )
    return fields


def handle_invoice_payment_failed(invoice) -> bool:
    """Flag past_due / payment action required. Does not disable the account."""
    customer_id = _sub_get(invoice, "customer")
    if not customer_id:
        return False
    user = db.get_user_by_stripe_customer(customer_id)
    if not user:
        return False
    error_code = payment_error_code_from_stripe_object(invoice)
    db.flag_payment_action_required(user["id"], error_code)
    sub_id = _sub_get(invoice, "subscription")
    if sub_id:
        db.update_user_subscription(
            user["id"],
            "past_due",
            subscription_id=sub_id if isinstance(sub_id, str) else _sub_get(sub_id, "id"),
            payment_action_required=True,
            last_payment_error=error_code,
        )
    return True


def handle_invoice_paid(invoice) -> bool:
    customer_id = _sub_get(invoice, "customer")
    if not customer_id:
        return False
    user = db.get_user_by_stripe_customer(customer_id)
    if not user:
        return False
    sub_id = _sub_get(invoice, "subscription")
    if sub_id and config.STRIPE_SECRET_KEY:
        try:
            sub = stripe.Subscription.retrieve(
                sub_id if isinstance(sub_id, str) else _sub_get(sub_id, "id")
            )
            apply_subscription_to_user(
                user["id"], sub, stripe_customer_id=customer_id
            )
            return True
        except stripe.StripeError:
            logger.exception("Failed retrieving subscription after invoice.paid")
    db.update_user_subscription(
        user["id"],
        "active",
        subscription_id=sub_id if isinstance(sub_id, str) else None,
        clear_payment_error=True,
    )
    return True


def handle_payment_intent_failed(payment_intent) -> bool:
    customer_id = _sub_get(payment_intent, "customer")
    if not customer_id:
        return False
    user = db.get_user_by_stripe_customer(customer_id)
    if not user:
        return False
    error_code = payment_error_code_from_stripe_object(payment_intent)
    db.flag_payment_action_required(user["id"], error_code)
    return True


def sync_user_from_stripe(user, email):
    if not config.STRIPE_SECRET_KEY:
        return
    customer = stripe_customer_for_email(email)
    if not customer:
        return
    db.set_stripe_customer(user["id"], customer.id)
    for status in ("active", "trialing", "past_due"):
        subs = stripe.Subscription.list(customer=customer.id, status=status, limit=1)
        if subs.data:
            apply_subscription_to_user(
                user["id"], subs.data[0], stripe_customer_id=customer.id
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
    """Create a Stripe Checkout Session for subscription. Does not charge until paid.

    Primary payment options are limited to card and Link (no Klarna / Cash App / Amazon Pay).
    """
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
        # Limit wallets / BNPL so Link disconnects and card issues surface clearly.
        payment_method_types=["card", "link"],
    )
    key = idempotency_key or checkout_idempotency_key(user["id"])
    return stripe.checkout.Session.create(**params, idempotency_key=key)
