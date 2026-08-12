#!/usr/bin/env python3
"""Read-only Stripe subscription diagnostic for a TopAI customer email.

Usage:
  python scripts/diagnose_stripe_subscription.py --email sbh.spacecoast@gmail.com

Prints safe metadata only — never secrets, client_secrets, or full card numbers.
Does not modify Stripe or local DB.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Allow running from repo root without installing as a package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _pm_summary(pm):
    if not pm:
        return {"id": None, "type": None, "is_link": False, "last4": None, "brand": None}
    if isinstance(pm, str):
        return {"id": pm, "type": None, "is_link": None, "last4": None, "brand": None}
    pm_type = (_get(pm, "type") or "").lower()
    card = _get(pm, "card") or {}
    return {
        "id": _get(pm, "id"),
        "type": pm_type,
        "is_link": pm_type == "link",
        "last4": _get(card, "last4"),
        "brand": _get(card, "brand"),
    }


def main():
    parser = argparse.ArgumentParser(description="Diagnose TopAI Stripe subscription billing.")
    parser.add_argument("--email", required=True, help="Customer email to inspect")
    args = parser.parse_args()

    import stripe

    import config
    import db
    from stripe_billing import normalize_email, stripe_customer_for_email

    email = normalize_email(args.email)
    if not config.STRIPE_SECRET_KEY:
        print("ERROR: STRIPE_SECRET_KEY is not set.", file=sys.stderr)
        sys.exit(2)

    stripe.api_key = config.STRIPE_SECRET_KEY

    local_user = db.get_user_by_email(email)
    customer = stripe_customer_for_email(email)
    report = {
        "email": email,
        "local_user_id": (local_user or {}).get("id"),
        "local_subscription_status": (local_user or {}).get("subscription_status"),
        "local_subscription_id": (local_user or {}).get("subscription_id"),
        "local_stripe_customer_id": (local_user or {}).get("stripe_customer_id"),
        "stripe_customer_id": _get(customer, "id") if customer else None,
        "subscription": None,
        "subscription_default_payment_method": None,
        "customer_invoice_default_payment_method": None,
        "latest_open_invoice": None,
        "default_is_link": None,
        "notes": [],
    }

    if not customer:
        report["notes"].append("No Stripe customer found for this email.")
        print(json.dumps(report, indent=2))
        return

    customer = stripe.Customer.retrieve(
        customer.id, expand=["invoice_settings.default_payment_method"]
    )
    inv_pm = _get(_get(customer, "invoice_settings") or {}, "default_payment_method")
    report["customer_invoice_default_payment_method"] = _pm_summary(inv_pm)

    # Prefer local subscription_id, else list recoverable statuses.
    sub = None
    sub_id = (local_user or {}).get("subscription_id")
    if sub_id:
        try:
            sub = stripe.Subscription.retrieve(
                sub_id, expand=["default_payment_method", "latest_invoice.payment_intent"]
            )
        except stripe.StripeError as exc:
            report["notes"].append(f"Could not retrieve local subscription_id: {exc}")

    if not sub:
        for status in ("past_due", "unpaid", "incomplete", "active", "trialing", "paused"):
            listed = stripe.Subscription.list(customer=customer.id, status=status, limit=3)
            if listed.data:
                sub = stripe.Subscription.retrieve(
                    listed.data[0].id,
                    expand=["default_payment_method", "latest_invoice.payment_intent"],
                )
                break

    if not sub:
        report["notes"].append("No subscription found on this customer.")
        print(json.dumps(report, indent=2))
        return

    sub_pm = _get(sub, "default_payment_method")
    report["subscription"] = {
        "id": _get(sub, "id"),
        "status": _get(sub, "status"),
        "payment_settings": _get(sub, "payment_settings"),
    }
    report["subscription_default_payment_method"] = _pm_summary(sub_pm)
    report["default_is_link"] = bool((_pm_summary(sub_pm) or {}).get("is_link"))

    open_invoices = stripe.Invoice.list(
        customer=customer.id, subscription=_get(sub, "id"), status="open", limit=3
    )
    if open_invoices.data:
        inv = open_invoices.data[0]
        pi = _get(inv, "payment_intent")
        last_err = None
        if pi and not isinstance(pi, str):
            err = _get(pi, "last_payment_error")
            if err:
                last_err = {
                    "code": _get(err, "code"),
                    "decline_code": _get(err, "decline_code"),
                    "message": _get(err, "message"),
                }
        report["latest_open_invoice"] = {
            "id": _get(inv, "id"),
            "status": _get(inv, "status"),
            "amount_due": _get(inv, "amount_due"),
            "currency": _get(inv, "currency"),
            "attempt_count": _get(inv, "attempt_count"),
            "next_payment_attempt": _get(inv, "next_payment_attempt"),
            "last_payment_error": last_err,
        }
        if last_err and last_err.get("code") == "link_connection_closed":
            report["notes"].append(
                "FAILURE IS link_connection_closed — replace payment method; do not wait for Smart Retries."
            )
    else:
        report["notes"].append("No open invoice on this subscription.")

    if report["default_is_link"]:
        report["notes"].append(
            "subscription.default_payment_method is Link — update via TopAI Billing → Update payment method."
        )

    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
