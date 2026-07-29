"""Stripe customer / subscription helpers for auth and billing sync."""

from __future__ import annotations

import stripe

import config
import db


def stripe_status_from_subscription(subscription):
    status = subscription.get("status", "none")
    if status in ("active", "trialing"):
        return "active"
    if status in ("canceled", "unpaid", "incomplete_expired"):
        return "canceled"
    return status


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
