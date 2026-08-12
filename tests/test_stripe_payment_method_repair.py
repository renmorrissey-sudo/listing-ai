"""Stripe payment-method repair: no Link, update defaults, retry invoice, webhooks."""

import uuid
from unittest.mock import MagicMock, patch

import auth
import config
import db
import stripe_billing


def _cid(prefix="cus"):
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        user = db.get_user_by_id(user_id)
        sess["session_version"] = int((user or {}).get("session_version") or 1)


def _billing_ok(monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_123")
    monkeypatch.setattr(config, "STRIPE_PRICE_ID", "price_1TfRM1BKSi4KGHsxagBmTsgG")
    monkeypatch.setattr(config, "STRIPE_PUBLISHABLE_KEY", "pk_test_abc")
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test_secret")
    monkeypatch.setattr(config, "SUBSCRIPTION_REQUIRED", True)
    monkeypatch.setattr(config, "STRIPE_SUBSCRIPTION_PAYMENT_METHOD_CONFIGURATION", "")


def test_new_subscription_checkout_uses_card_not_link(monkeypatch):
    _billing_ok(monkeypatch)
    user = {"id": 1, "email": "a@example.com", "stripe_customer_id": "cus_1"}
    with patch("stripe.checkout.Session.create", return_value=MagicMock()) as create:
        stripe_billing.create_subscription_checkout_session(
            user, success_url="https://x/ok", cancel_url="https://x/cancel"
        )
    kwargs = create.call_args.kwargs
    assert kwargs["payment_method_types"] == ["card"]
    assert "link" not in kwargs["payment_method_types"]
    assert kwargs["subscription_data"]["payment_settings"]["payment_method_types"] == ["card"]
    assert "automatic_payment_methods" not in kwargs


def test_setup_checkout_also_excludes_link(monkeypatch):
    _billing_ok(monkeypatch)
    user = {"id": 1, "email": "a@example.com", "stripe_customer_id": "cus_1"}
    with patch("stripe.checkout.Session.create", return_value=MagicMock()) as create:
        stripe_billing.create_payment_method_update_session(
            user, success_url="https://x/ok", cancel_url="https://x/cancel"
        )
    assert create.call_args.kwargs["mode"] == "setup"
    assert create.call_args.kwargs["payment_method_types"] == ["card"]


def test_invoice_payment_failed_persists_past_due(app_client, two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    customer_id = _cid("cus_fail")
    db.update_user_subscription(
        u1, "active", subscription_id="sub_fail_1", stripe_customer_id=customer_id
    )
    event = {
        "id": f"evt_invoice_failed_{uuid.uuid4().hex[:8]}",
        "type": "invoice.payment_failed",
        "data": {
            "object": {
                "customer": customer_id,
                "subscription": "sub_fail_1",
                "id": "in_fail_1",
            }
        },
    }
    with patch("stripe.Webhook.construct_event", return_value=event):
        res = app_client.post(
            "/webhook/stripe", data=b"{}", headers={"Stripe-Signature": "t=1,v1=fake"}
        )
    assert res.status_code == 200
    user = db.get_user_by_id(u1)
    assert user["subscription_status"] == "past_due"
    # past_due keeps tool access so payment recovery UI remains reachable.
    assert auth.user_has_active_subscription(user) is True


def test_link_connection_closed_surfaces_update_payment_method_ux(app_client, two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    db.update_user_subscription(
        u1, "past_due", subscription_id="sub_1", stripe_customer_id=_cid("cus_link")
    )
    _login(app_client, u1)
    summary = {
        "plan_name": "TopAI Pro",
        "price_label": "$49/month",
        "status_label": "Payment method needs attention",
        "payment_method": {"label": "Unavailable", "is_link": True},
        "attention_message": stripe_billing.PAYMENT_METHOD_REPLACEMENT_MSG,
        "needs_payment_method_update": True,
        "show_update_payment_method": True,
        "show_manage_subscription": True,
        "has_stripe_customer": True,
        "local_status": "past_due",
        "failure_code": "link_connection_closed",
    }
    with patch("app._billing_summary", return_value=summary):
        res = app_client.get("/billing")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "no longer available" in html.lower()
    assert "Update payment method" in html
    assert "link_connection_closed" not in html


def test_apply_default_payment_method_updates_subscription_and_customer(two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    user = {
        "id": u1,
        "email": "a@example.com",
        "stripe_customer_id": "cus_1",
        "subscription_id": "sub_1",
    }
    pm = {"id": "pm_card_1", "type": "card", "customer": "cus_1", "card": {"brand": "visa", "last4": "4242"}}
    sub = {"id": "sub_1", "customer": "cus_1", "status": "past_due", "default_payment_method": None}

    with patch("stripe.PaymentMethod.retrieve", return_value=pm), patch(
        "stripe.Subscription.retrieve", return_value=sub
    ), patch("stripe.Subscription.modify") as sub_mod, patch(
        "stripe.Customer.modify"
    ) as cust_mod, patch(
        "stripe_billing.list_blocking_subscriptions", return_value=[sub]
    ):
        result = stripe_billing.apply_default_payment_method(user, "pm_card_1")

    assert result["payment_method_id"] == "pm_card_1"
    sub_mod.assert_called_once()
    assert sub_mod.call_args.args[0] == "sub_1"
    assert sub_mod.call_args.kwargs["default_payment_method"] == "pm_card_1"
    assert sub_mod.call_args.kwargs["payment_settings"]["payment_method_types"] == ["card"]
    cust_mod.assert_called_once()
    assert cust_mod.call_args.kwargs["invoice_settings"]["default_payment_method"] == "pm_card_1"


def test_apply_default_rejects_link_payment_method(two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    user = {"id": u1, "stripe_customer_id": "cus_1", "subscription_id": "sub_1"}
    pm = {"id": "pm_link_1", "type": "link", "customer": "cus_1"}
    with patch("stripe.PaymentMethod.retrieve", return_value=pm):
        try:
            stripe_billing.apply_default_payment_method(user, "pm_link_1")
            assert False, "expected ValueError"
        except ValueError as exc:
            assert "Link" in str(exc)


def test_apply_default_rejects_cross_tenant_payment_method(two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    user = {"id": u1, "stripe_customer_id": "cus_1", "subscription_id": "sub_1"}
    pm = {"id": "pm_other", "type": "card", "customer": "cus_OTHER"}
    with patch("stripe.PaymentMethod.retrieve", return_value=pm):
        try:
            stripe_billing.apply_default_payment_method(user, "pm_other")
            assert False, "expected PermissionError"
        except PermissionError:
            pass


def test_existing_subscription_reused_not_duplicated(app_client, two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    db.update_user_subscription(
        u1, "past_due", subscription_id="sub_existing", stripe_customer_id=_cid("cus_reuse")
    )
    _login(app_client, u1)
    with patch(
        "app._resolve_subscribe_gate",
        return_value={
            "can_checkout": False,
            "state": "past_due",
            "message": "past due",
            "show_manage_billing": True,
            "show_update_payment_method": True,
            "show_open_tools": False,
            "redirect": None,
        },
    ), patch("app._create_subscription_checkout_session") as create_sub:
        res = app_client.post("/subscribe", data={"email": "x@y.com"})
        create_sub.assert_not_called()
    assert res.status_code == 409


def test_retry_open_invoice_uses_new_payment_method(two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    user = {"id": u1, "stripe_customer_id": "cus_1", "subscription_id": "sub_1"}
    sub = {"id": "sub_1", "customer": "cus_1", "status": "past_due"}
    invoice = {"id": "in_open_1", "status": "open"}
    paid = {"id": "in_open_1", "status": "paid"}
    with patch("stripe_billing.primary_subscription_for_user", return_value=sub), patch(
        "stripe.Invoice.list", return_value=MagicMock(data=[invoice])
    ), patch("stripe.Invoice.pay", return_value=paid) as pay:
        result = stripe_billing.retry_open_subscription_invoice(user, payment_method_id="pm_new")
    pay.assert_called_once_with("in_open_1", payment_method="pm_new")
    assert result["paid"] is True
    assert result["status"] == "paid"


def test_invoice_paid_restores_active(app_client, two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    customer_id = _cid("cus_paid")
    sub_id = f"sub_{uuid.uuid4().hex[:8]}"
    db.update_user_subscription(
        u1, "past_due", subscription_id=sub_id, stripe_customer_id=customer_id
    )
    event = {
        "id": f"evt_invoice_paid_{uuid.uuid4().hex[:8]}",
        "type": "invoice.paid",
        "data": {"object": {"customer": customer_id, "subscription": sub_id}},
    }
    active_sub = {"id": sub_id, "status": "active", "customer": customer_id}
    with patch("stripe.Webhook.construct_event", return_value=event), patch(
        "stripe.Subscription.retrieve", return_value=active_sub
    ):
        res = app_client.post(
            "/webhook/stripe", data=b"{}", headers={"Stripe-Signature": "t=1,v1=fake"}
        )
    assert res.status_code == 200
    user = db.get_user_by_id(u1)
    assert user["subscription_status"] == "active"


def test_duplicate_webhook_does_not_reprocess(app_client, two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    customer_id = _cid("cus_dup")
    db.set_stripe_customer(u1, customer_id)
    event = {
        "id": f"evt_dup_{uuid.uuid4().hex[:8]}",
        "type": "invoice.payment_failed",
        "data": {"object": {"customer": customer_id, "subscription": "sub_dup"}},
    }
    with patch("stripe.Webhook.construct_event", return_value=event):
        first = app_client.post(
            "/webhook/stripe", data=b"{}", headers={"Stripe-Signature": "t=1,v1=fake"}
        )
        second = app_client.post(
            "/webhook/stripe", data=b"{}", headers={"Stripe-Signature": "t=1,v1=fake"}
        )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.get_json().get("duplicate") is True


def test_cross_tenant_payment_method_success_rejected(app_client, two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, u2 = two_users
    cus1, cus2 = _cid("cus_a"), _cid("cus_b")
    db.set_stripe_customer(u1, cus1)
    db.set_stripe_customer(u2, cus2)
    _login(app_client, u1)
    fake_session = {
        "id": "cs_setup",
        "mode": "setup",
        "customer": cus2,
        "client_reference_id": str(u2),
        "metadata": {"user_id": str(u2)},
        "setup_intent": "seti_1",
    }
    with patch("stripe.checkout.Session.retrieve", return_value=fake_session), patch(
        "app._complete_payment_method_update"
    ) as complete:
        res = app_client.get("/billing/payment-method/success?session_id=cs_setup", follow_redirects=False)
    complete.assert_not_called()
    assert res.status_code in (302, 303)
    assert "error=session_mismatch" in res.headers["Location"]


def test_bad_stripe_signature_rejected(app_client, monkeypatch):
    _billing_ok(monkeypatch)
    import stripe as stripe_mod

    with patch(
        "stripe.Webhook.construct_event",
        side_effect=stripe_mod.SignatureVerificationError("bad", "sig"),
    ):
        res = app_client.post(
            "/webhook/stripe", data=b"{}", headers={"Stripe-Signature": "bad"}
        )
    assert res.status_code == 400


def test_payment_action_required_does_not_mark_paid(two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    user = {"id": u1, "stripe_customer_id": "cus_1", "subscription_id": "sub_1"}
    sub = {"id": "sub_1", "customer": "cus_1", "status": "past_due"}
    invoice = {"id": "in_3ds", "status": "open"}
    unpaid = {
        "id": "in_3ds",
        "status": "open",
        "payment_intent": {"status": "requires_action"},
        "hosted_invoice_url": "https://invoice.stripe.test/pay",
    }
    with patch("stripe_billing.primary_subscription_for_user", return_value=sub), patch(
        "stripe.Invoice.list", return_value=MagicMock(data=[invoice])
    ), patch("stripe.Invoice.pay", return_value=unpaid):
        result = stripe_billing.retry_open_subscription_invoice(user, payment_method_id="pm_new")
    assert result["paid"] is False
    assert result["status"] == "payment_action_required"


def test_browser_return_alone_does_not_mark_invoice_paid(app_client, two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    customer_id = _cid("cus_browser")
    db.update_user_subscription(
        u1, "past_due", subscription_id="sub_1", stripe_customer_id=customer_id
    )
    _login(app_client, u1)
    # Incomplete setup — SetupIntent not succeeded.
    fake_session = {
        "id": "cs_setup",
        "mode": "setup",
        "customer": customer_id,
        "client_reference_id": str(u1),
        "metadata": {"user_id": str(u1), "purpose": "payment_method_update"},
        "setup_intent": {"id": "seti_1", "status": "requires_payment_method", "payment_method": None},
    }
    with patch("stripe.checkout.Session.retrieve", return_value=fake_session), patch(
        "stripe.Invoice.pay"
    ) as pay:
        res = app_client.get("/billing/payment-method/success?session_id=cs_setup", follow_redirects=False)
    pay.assert_not_called()
    assert res.status_code in (302, 303)
    user = db.get_user_by_id(u1)
    assert user["subscription_status"] == "past_due"


def test_complete_payment_method_update_happy_path(two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    customer_id = _cid("cus_complete")
    db.update_user_subscription(
        u1, "past_due", subscription_id="sub_1", stripe_customer_id=customer_id
    )
    user = db.get_user_by_id(u1)
    session = {
        "id": "cs_ok",
        "mode": "setup",
        "customer": customer_id,
        "setup_intent": {
            "id": "seti_ok",
            "status": "succeeded",
            "payment_method": "pm_card_new",
        },
    }
    with patch(
        "stripe_billing.apply_default_payment_method",
        return_value={
            "payment_method_id": "pm_card_new",
            "subscription_id": "sub_1",
            "customer_id": customer_id,
            "payment_method_type": "card",
        },
    ) as apply, patch(
        "stripe_billing.retry_open_subscription_invoice",
        return_value={"status": "paid", "invoice_id": "in_1", "paid": True},
    ) as retry, patch(
        "stripe_billing.primary_subscription_for_user",
        return_value={"id": "sub_1", "status": "active", "customer": customer_id},
    ):
        result = stripe_billing.complete_payment_method_update(user, checkout_session=session)
    apply.assert_called_once()
    retry.assert_called_once()
    assert result["applied"] is True
    assert result["paid"] is True
    assert db.get_user_by_id(u1)["subscription_status"] == "active"


def test_billing_page_requires_login(app_client):
    res = app_client.get("/billing", follow_redirects=False)
    assert res.status_code in (301, 302, 303)
    assert "/login" in res.headers.get("Location", "")


def test_update_payment_method_starts_setup_checkout(app_client, two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    db.set_stripe_customer(u1, _cid("cus_upd"))
    _login(app_client, u1)
    fake = MagicMock()
    fake.url = "https://checkout.stripe.test/setup"
    with patch("app._create_payment_method_update_session", return_value=fake) as create:
        res = app_client.post("/billing/update-payment-method", follow_redirects=False)
    create.assert_called_once()
    assert res.status_code == 303
    assert res.headers["Location"] == "https://checkout.stripe.test/setup"
