"""Billing page, portal session API, and Stripe payment-failure webhooks."""

from unittest.mock import MagicMock, patch

import auth
import config
import db
import stripe_billing


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        user = db.get_user_by_id(user_id)
        sess["session_version"] = int((user or {}).get("session_version") or 1)


def _billing_ok(monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_123")
    monkeypatch.setattr(config, "STRIPE_PRICE_ID", "price_1TfRM1BKSi4KGHsxagBmTsgG")
    monkeypatch.setattr(config, "STRIPE_PUBLISHABLE_KEY", "pk_test_abc")
    monkeypatch.setattr(config, "SUBSCRIPTION_REQUIRED", True)
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test_secret")


def _post_webhook(client, event):
    with patch("stripe.Webhook.construct_event", return_value=event):
        return client.post(
            "/webhook/stripe",
            data=b"{}",
            headers={"Stripe-Signature": "t=1,v1=fake"},
        )


def test_billing_menu_item_for_authenticated_subscriber(app_client, two_users, monkeypatch):
    monkeypatch.setattr(config, "SUBSCRIPTION_REQUIRED", True)
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    html = app_client.get("/crm/leads").get_data(as_text=True)
    nav = html.split('aria-label="Main application navigation"')[1].split("</nav>")[0]
    assert 'href="/billing"' in nav
    assert ">Billing<" in nav.replace(" ", "") or "Billing</a>" in nav
    # Billing must appear immediately after Bulk SMS.
    bulk_pos = nav.find('href="/crm/sms-campaigns"')
    billing_pos = nav.find('href="/billing"')
    assert bulk_pos != -1 and billing_pos != -1
    assert bulk_pos < billing_pos


def test_billing_page_loads_for_subscriber(app_client, two_users, monkeypatch):
    monkeypatch.setattr(config, "SUBSCRIPTION_REQUIRED", True)
    u1, _ = two_users
    db.update_user_subscription(
        u1,
        "active",
        subscription_id="sub_live_1",
        stripe_customer_id="cus_live_1",
        stripe_price_id="price_1TfRM1BKSi4KGHsxagBmTsgG",
        current_period_end=4102444800,
    )
    _login(app_client, u1)
    res = app_client.get("/billing")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Billing" in html
    assert "TopAI Monthly" in html
    assert "Active" in html
    assert config.SUBSCRIPTION_PRICE.split("/")[0] in html or "$49" in html
    assert "Manage Billing" in html
    assert 'id="manage-billing-btn"' in html
    assert "/api/billing/create-portal-session" in html
    assert "sk_test" not in html
    assert "sk_live" not in html
    assert "whsec_" not in html


def test_billing_page_no_customer_shows_subscribe_cta(app_client, two_users, monkeypatch):
    monkeypatch.setattr(config, "SUBSCRIPTION_REQUIRED", False)
    u1, _ = two_users
    _login(app_client, u1)
    res = app_client.get("/billing")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "No Stripe billing account" in html
    assert 'href="/subscribe"' in html
    assert 'id="manage-billing-btn"' not in html


def test_unauthenticated_cannot_create_portal_session(app_client, monkeypatch):
    _billing_ok(monkeypatch)
    res = app_client.post(
        "/api/billing/create-portal-session",
        json={},
        headers={"Accept": "application/json"},
    )
    assert res.status_code == 401
    assert res.get_json()["error"]


def test_portal_session_uses_only_authenticated_user_customer(
    app_client, two_users, monkeypatch
):
    _billing_ok(monkeypatch)
    u1, u2 = two_users
    db.set_stripe_customer(u1, "cus_owner_1")
    db.set_stripe_customer(u2, "cus_other_2")
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)

    with patch(
        "stripe.billing_portal.Session.create",
        return_value=MagicMock(url="https://billing.stripe.test/session/bps_1"),
    ) as portal:
        # Attacker tries to pass another customer's ID — body must be ignored.
        res = app_client.post(
            "/api/billing/create-portal-session",
            json={"customer": "cus_other_2", "stripe_customer_id": "cus_other_2"},
            headers={"Accept": "application/json"},
        )

    assert res.status_code == 200
    data = res.get_json()
    assert data["url"] == "https://billing.stripe.test/session/bps_1"
    portal.assert_called_once()
    assert portal.call_args.kwargs["customer"] == "cus_owner_1"
    assert portal.call_args.kwargs["customer"] != "cus_other_2"
    assert "/billing" in portal.call_args.kwargs["return_url"]


def test_manage_billing_returns_portal_url(app_client, two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    db.set_stripe_customer(u1, "cus_portal")
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    with patch(
        "stripe.billing_portal.Session.create",
        return_value=MagicMock(url="https://billing.stripe.test/portal"),
    ):
        res = app_client.post("/api/billing/create-portal-session", json={})
    assert res.status_code == 200
    assert res.get_json()["url"].startswith("https://billing.stripe.test/")


def test_portal_no_customer_returns_clear_message(app_client, two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    _login(app_client, u1)
    with patch("stripe.billing_portal.Session.create") as portal:
        res = app_client.post("/api/billing/create-portal-session", json={})
    portal.assert_not_called()
    assert res.status_code == 400
    data = res.get_json()
    assert data["error"] == "no_customer"
    assert "Subscribe" in data["message"] or "subscribe" in data["message"].lower()
    assert data.get("subscribe_url") == "/subscribe"


def test_checkout_limits_payment_methods_to_card_and_link(monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_123")
    monkeypatch.setattr(config, "STRIPE_PRICE_ID", "price_1TfRM1BKSi4KGHsxagBmTsgG")
    monkeypatch.setattr(config, "STRIPE_PUBLISHABLE_KEY", "pk_test_abc")
    user = {"id": 9, "email": "a@example.com", "stripe_customer_id": "cus_abc"}
    with patch("stripe.checkout.Session.create", return_value=MagicMock()) as create:
        stripe_billing.create_subscription_checkout_session(
            user,
            success_url="https://example.com/ok",
            cancel_url="https://example.com/cancel",
            idempotency_key="subchk_test_key",
        )
    assert create.call_args.kwargs["payment_method_types"] == ["card"]
    assert "link" not in create.call_args.kwargs["payment_method_types"]
    assert "klarna" not in create.call_args.kwargs["payment_method_types"]


def test_successful_subscription_webhook(app_client, two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    event = {
        "id": "evt_checkout_ok",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "client_reference_id": str(u1),
                "subscription": "sub_ok_1",
                "customer": "cus_ok_1",
            }
        },
    }
    fake_sub = {
        "id": "sub_ok_1",
        "status": "active",
        "current_period_end": 4102444800,
        "items": {
            "data": [{"price": {"id": "price_1TfRM1BKSi4KGHsxagBmTsgG"}}]
        },
    }
    with patch("stripe.Subscription.retrieve", return_value=fake_sub):
        res = _post_webhook(app_client, event)
    assert res.status_code == 200
    user = db.get_user_by_id(u1)
    assert user["subscription_status"] == "active"
    assert user["subscription_id"] == "sub_ok_1"
    assert user["stripe_customer_id"] == "cus_ok_1"
    assert user["stripe_price_id"] == "price_1TfRM1BKSi4KGHsxagBmTsgG"
    assert int(user["subscription_current_period_end"]) == 4102444800
    assert not user.get("payment_action_required")
    assert auth.user_has_active_subscription(user) is True


def test_invoice_payment_failed_flags_past_due_without_disabling_access(
    app_client, two_users, monkeypatch
):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    db.update_user_subscription(
        u1, "active", subscription_id="sub_pd", stripe_customer_id="cus_pd"
    )
    event = {
        "id": "evt_invoice_fail",
        "type": "invoice.payment_failed",
        "data": {
            "object": {
                "customer": "cus_pd",
                "subscription": "sub_pd",
                "last_payment_error": {"code": "link_connection_closed"},
            }
        },
    }
    res = _post_webhook(app_client, event)
    assert res.status_code == 200
    user = db.get_user_by_id(u1)
    assert user["subscription_status"] == "past_due"
    assert user.get("payment_action_required") in (1, True)
    assert user.get("last_payment_error") == "link_connection_closed"
    # Account is flagged, not fully disabled — tools remain reachable.
    assert auth.user_has_active_subscription(user) is True
    assert auth.user_needs_billing_attention(user) is True

    _login(app_client, u1)
    html = app_client.get("/billing").get_data(as_text=True)
    assert "payment method needs to be updated" in html.lower()
    assert "Past Due" in html
    dash = app_client.get(
        "/dashboard?local_date=2026-07-26&tz_offset_minutes=0"
    ).get_data(as_text=True)
    assert "payment method needs to be updated" in dash.lower()


def test_past_due_subscription_webhook(app_client, two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    db.set_stripe_customer(u1, "cus_upd")
    event = {
        "id": "evt_sub_past_due",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_upd",
                "customer": "cus_upd",
                "status": "past_due",
                "current_period_end": 4102444800,
                "items": {"data": [{"price": {"id": "price_1TfRM1BKSi4KGHsxagBmTsgG"}}]},
            }
        },
    }
    res = _post_webhook(app_client, event)
    assert res.status_code == 200
    user = db.get_user_by_id(u1)
    assert user["subscription_status"] == "past_due"
    assert user["subscription_id"] == "sub_upd"


def test_invoice_paid_clears_payment_action(app_client, two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    db.update_user_subscription(
        u1,
        "past_due",
        subscription_id="sub_rec",
        stripe_customer_id="cus_rec",
        payment_action_required=True,
        last_payment_error="link_connection_closed",
    )
    event = {
        "id": "evt_invoice_paid",
        "type": "invoice.paid",
        "data": {
            "object": {
                "customer": "cus_rec",
                "subscription": "sub_rec",
            }
        },
    }
    fake_sub = {
        "id": "sub_rec",
        "status": "active",
        "current_period_end": 4102444800,
        "items": {"data": [{"price": {"id": "price_1TfRM1BKSi4KGHsxagBmTsgG"}}]},
    }
    with patch("stripe.Subscription.retrieve", return_value=fake_sub):
        res = _post_webhook(app_client, event)
    assert res.status_code == 200
    user = db.get_user_by_id(u1)
    assert user["subscription_status"] == "active"
    assert not user.get("payment_action_required")
    assert not user.get("last_payment_error")


def test_canceled_subscription_webhook(app_client, two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    db.update_user_subscription(
        u1, "active", subscription_id="sub_cxl", stripe_customer_id="cus_cxl"
    )
    event = {
        "id": "evt_sub_deleted",
        "type": "customer.subscription.deleted",
        "data": {
            "object": {
                "id": "sub_cxl",
                "customer": "cus_cxl",
                "status": "canceled",
                "current_period_end": 1700000000,
            }
        },
    }
    res = _post_webhook(app_client, event)
    assert res.status_code == 200
    user = db.get_user_by_id(u1)
    assert user["subscription_status"] == "canceled"
    assert auth.user_has_active_subscription(user) is False


def test_payment_intent_failed_link_connection(app_client, two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    db.set_stripe_customer(u1, "cus_pi")
    db.update_user_subscription(u1, "active", stripe_customer_id="cus_pi")
    event = {
        "id": "evt_pi_fail",
        "type": "payment_intent.payment_failed",
        "data": {
            "object": {
                "customer": "cus_pi",
                "last_payment_error": {"code": "link_connection_closed"},
            }
        },
    }
    res = _post_webhook(app_client, event)
    assert res.status_code == 200
    user = db.get_user_by_id(u1)
    assert user.get("payment_action_required") in (1, True)
    assert user.get("last_payment_error") == "link_connection_closed"
    msg = stripe_billing.payment_failure_user_message(user["last_payment_error"])
    assert "payment method needs to be updated" in msg.lower()
    assert msg.lower() != "payment failed."


def test_webhook_signature_validation(app_client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test_secret")
    with patch(
        "stripe.Webhook.construct_event",
        side_effect=__import__("stripe").SignatureVerificationError(
            "bad sig", "sig_header"
        ),
    ):
        res = app_client.post(
            "/webhook/stripe",
            data=b"{}",
            headers={"Stripe-Signature": "t=1,v1=bad"},
        )
    assert res.status_code == 400
    assert res.get_json()["error"] == "Invalid signature."


def test_webhook_rejects_invalid_payload(app_client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test_secret")
    with patch("stripe.Webhook.construct_event", side_effect=ValueError("bad")):
        res = app_client.post(
            "/webhook/stripe",
            data=b"not-json",
            headers={"Stripe-Signature": "t=1,v1=x"},
        )
    assert res.status_code == 400
    assert res.get_json()["error"] == "Invalid payload."


def test_subscriber_cannot_open_other_customer_via_legacy_portal(
    app_client, two_users, monkeypatch
):
    _billing_ok(monkeypatch)
    u1, u2 = two_users
    db.set_stripe_customer(u1, "cus_a")
    db.set_stripe_customer(u2, "cus_b")
    _login(app_client, u1)
    with patch(
        "stripe.billing_portal.Session.create",
        return_value=MagicMock(url="https://billing.stripe.test/a"),
    ) as portal:
        res = app_client.get("/billing/portal?customer=cus_b", follow_redirects=False)
    assert res.status_code in (301, 302, 303)
    portal.assert_called_once()
    assert portal.call_args.kwargs["customer"] == "cus_a"


def test_payment_method_replacement_message_codes():
    for code in (
        "link_connection_closed",
        "payment_method_unavailable",
        "payment_method_provider_decline",
        "expired_card",
    ):
        msg = stripe_billing.payment_failure_user_message(code)
        assert "payment method needs to be updated" in msg.lower()
