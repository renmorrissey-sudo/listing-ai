"""Subscribe / account-creation / Stripe checkout flow."""

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


def test_get_subscribe_returns_200_html(app_client):
    with patch.object(config, "STRIPE_SECRET_KEY", "sk_test_123"), patch.object(
        config, "STRIPE_PRICE_ID", "price_1TfRM1BKSi4KGHsxagBmTsgG"
    ), patch.object(config, "STRIPE_PUBLISHABLE_KEY", None):
        res = app_client.get("/subscribe")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "text/html" in res.content_type
    assert "TopAI" in html
    assert "Subscribe" in html
    assert config.SUBSCRIPTION_PRICE.split("/")[0] in html or "$49" in html
    assert "Billed monthly" in html
    assert "TRIAL50" in html or "50% off" in html
    assert "not a free trial" in html.lower() or "discount" in html.lower()
    assert 'name="email"' in html
    assert 'name="password"' in html
    assert 'name="confirm_password"' in html
    assert 'href="/terms"' in html
    assert 'href="/privacy"' in html
    assert 'href="/refund-policy"' in html
    assert "Start trial" not in html
    assert "checkout.Session.create" not in html
    assert "sk_test" not in html
    assert "sk_live" not in html


def test_login_create_one_links_to_subscribe(app_client):
    html = app_client.get("/login").get_data(as_text=True)
    assert 'href="/subscribe"' in html
    assert "Create one" in html


def test_register_redirects_to_subscribe(app_client):
    res = app_client.get("/register", follow_redirects=False)
    assert res.status_code in (301, 302)
    assert "/subscribe" in res.headers["Location"]


def test_missing_stripe_config_safe_page(app_client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", None)
    monkeypatch.setattr(config, "STRIPE_PRICE_ID", None)
    res = app_client.get("/subscribe")
    assert res.status_code == 503
    html = res.get_data(as_text=True)
    assert "Billing is temporarily unavailable" in html
    assert "sk_" not in html
    assert "Traceback" not in html


def test_invalid_price_id_detected():
    assert stripe_billing.is_valid_stripe_price_id("price_...") is False
    assert stripe_billing.is_valid_stripe_price_id("price_xxx") is False
    assert stripe_billing.is_valid_stripe_price_id("price_1TfRM1BKSi4KGHsxagBmTsgG") is True
    assert stripe_billing.is_valid_stripe_price_id("") is False
    assert stripe_billing.is_valid_stripe_price_id(None) is False


def test_mode_mismatch_detection():
    assert stripe_billing.stripe_mode_mismatch("sk_test_x", "pk_live_y") is True
    assert stripe_billing.stripe_mode_mismatch("sk_live_x", "pk_test_y") is True
    assert stripe_billing.stripe_mode_mismatch("sk_live_x", "pk_live_y") is False
    assert stripe_billing.stripe_mode_mismatch("sk_test_x", "pk_test_y") is False
    assert stripe_billing.stripe_mode_mismatch("sk_live_x", None) is False


def test_checkout_session_only_on_valid_signup(app_client, monkeypatch):
    _billing_ok(monkeypatch)

    fake_session = MagicMock()
    fake_session.url = "https://checkout.stripe.test/session/cs_test_123"

    with patch(
        "app._create_subscription_checkout_session", return_value=fake_session
    ) as create_checkout, patch(
        "app._stripe_customer_for_email", return_value=None
    ):
        # GET must not create checkout
        get_res = app_client.get("/subscribe")
        assert get_res.status_code == 200
        create_checkout.assert_not_called()

        post = app_client.post(
            "/subscribe",
            data={
                "email": "new.subscriber@example.com",
                "password": "SecurePass99!",
                "confirm_password": "SecurePass99!",
            },
            follow_redirects=False,
        )
        assert post.status_code == 303
        assert post.headers["Location"] == fake_session.url
        create_checkout.assert_called_once()
        kwargs = create_checkout.call_args.kwargs
        assert kwargs.get("idempotency_key")

    user = db.get_user_by_email("new.subscriber@example.com")
    assert user is not None
    assert user["password_hash"] != "SecurePass99!"
    assert auth.verify_password(user["password_hash"], "SecurePass99!")
    assert user.get("subscription_status") != "active"


def test_duplicate_email_blocks_signup(app_client, two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    email = db.get_user_by_id(u1)["email"]
    before = db.get_user_by_email(email)

    with patch("app._create_subscription_checkout_session") as create_checkout:
        res = app_client.post(
            "/subscribe",
            data={
                "email": email.upper(),  # case-insensitive
                "password": "SecurePass99!",
                "confirm_password": "SecurePass99!",
            },
        )
        create_checkout.assert_not_called()

    assert res.status_code == 400
    assert b"already exists" in res.data
    assert b"sign in" in res.data.lower()
    after = db.get_user_by_email(email)
    assert after["id"] == before["id"]


def test_webhook_gates_access_and_is_idempotent(app_client, two_users, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test_secret")
    monkeypatch.setattr(config, "SUBSCRIPTION_REQUIRED", True)
    u1, _ = two_users
    assert auth.user_has_active_subscription(db.get_user_by_id(u1)) is False

    event = {
        "id": "evt_test_checkout_1",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "client_reference_id": str(u1),
                "subscription": "sub_test_1",
                "customer": "cus_test_1",
            }
        },
    }
    fake_sub = {"id": "sub_test_1", "status": "active"}

    with patch("stripe.Webhook.construct_event", return_value=event), patch(
        "stripe.Subscription.retrieve", return_value=fake_sub
    ):
        first = app_client.post(
            "/webhook/stripe",
            data=b"{}",
            headers={"Stripe-Signature": "t=1,v1=fake"},
        )
        second = app_client.post(
            "/webhook/stripe",
            data=b"{}",
            headers={"Stripe-Signature": "t=1,v1=fake"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.get_json().get("duplicate") is True
    user = db.get_user_by_id(u1)
    assert user["subscription_status"] == "active"
    assert auth.user_has_active_subscription(user) is True


def test_billing_success_does_not_activate(app_client, two_users, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_123")
    monkeypatch.setattr(config, "SUBSCRIPTION_REQUIRED", True)
    u1, _ = two_users
    _login(app_client, u1)
    fake_checkout = MagicMock()
    fake_checkout.customer = "cus_test_success"
    fake_checkout.subscription = "sub_should_not_activate"

    with patch("stripe.checkout.Session.retrieve", return_value=fake_checkout):
        res = app_client.get("/billing/success?session_id=cs_test")
    assert res.status_code == 200
    assert b"confirming your subscription" in res.data.lower()
    user = db.get_user_by_id(u1)
    assert user.get("subscription_status") != "active"
    assert user.get("stripe_customer_id") == "cus_test_success"


def test_cancelled_checkout_returns_safely(app_client, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_123")
    monkeypatch.setattr(config, "STRIPE_PRICE_ID", "price_1TfRM1BKSi4KGHsxagBmTsgG")
    res = app_client.get("/subscribe?cancelled=1")
    assert res.status_code == 200
    assert b"cancelled" in res.data.lower()
    assert b"Traceback" not in res.data
    assert b"sk_test" not in res.data


def test_authenticated_subscriber_redirected_from_subscribe(app_client, two_users, monkeypatch):
    monkeypatch.setattr(config, "SUBSCRIPTION_REQUIRED", True)
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", None)
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    res = app_client.get("/subscribe", follow_redirects=False)
    assert res.status_code in (301, 302)
    loc = res.headers["Location"]
    assert "/app" in loc
    assert "already_subscribed=1" in loc


def test_subscribe_cta_homepage_points_to_subscribe(app_client):
    html = app_client.get("/").get_data(as_text=True)
    assert 'href="/subscribe"' in html
    assert "Subscribe" in html
    assert "Start trial" not in html
    assert "View pricing" in html
    assert 'href="/login' in html


def test_logged_out_subscribe_allows_checkout_form(app_client, monkeypatch):
    _billing_ok(monkeypatch)
    res = app_client.get("/subscribe")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert 'method="post"' in html
    assert 'action="/subscribe"' in html
    assert "Continue to checkout" in html
    assert "readonly" not in html.split("<form", 1)[-1]


def test_logged_in_unsubscribed_locks_email_and_allows_checkout(app_client, two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    email = db.get_user_by_id(u1)["email"]
    _login(app_client, u1)

    with patch("app._resolve_subscribe_gate", return_value={
        "can_checkout": True,
        "state": "none",
        "message": None,
        "access_ends_on": None,
        "show_manage_billing": False,
        "show_open_tools": False,
        "redirect": None,
    }):
        res = app_client.get("/subscribe")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert email in html
    assert "readonly" in html
    assert "locked to your signed-in account" in html.lower()
    assert 'name="password"' not in html
    assert "Continue to checkout" in html


def test_logged_in_unsubscribed_post_ignores_form_email(app_client, two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    email = db.get_user_by_id(u1)["email"]
    _login(app_client, u1)
    fake_session = MagicMock()
    fake_session.url = "https://checkout.stripe.test/session/cs_locked"

    with patch(
        "app._resolve_subscribe_gate",
        return_value={
            "can_checkout": True,
            "state": "none",
            "message": None,
            "access_ends_on": None,
            "show_manage_billing": False,
            "show_open_tools": False,
            "redirect": None,
        },
    ), patch(
        "app._create_subscription_checkout_session", return_value=fake_session
    ) as create_checkout:
        res = app_client.post(
            "/subscribe",
            data={"email": "attacker@evil.example"},
            follow_redirects=False,
        )
    assert res.status_code == 303
    create_checkout.assert_called_once()
    user_arg = create_checkout.call_args.args[0]
    assert user_arg["email"] == email


def test_active_subscriber_post_blocked(app_client, two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)

    with patch("app._create_subscription_checkout_session") as create_checkout, patch(
        "app._resolve_subscribe_gate",
        return_value={
            "can_checkout": False,
            "state": "active",
            "message": "Your subscription is already active.",
            "access_ends_on": None,
            "show_manage_billing": True,
            "show_open_tools": True,
            "redirect": "subscriber_app",
        },
    ):
        res = app_client.post("/subscribe", data={"email": "x@y.com"}, follow_redirects=False)
        create_checkout.assert_not_called()
    assert res.status_code in (301, 302)
    assert "/app" in res.headers["Location"]


def test_canceling_subscriber_sees_end_date_no_checkout(app_client, two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    gate = {
        "can_checkout": False,
        "state": "canceling",
        "message": "Your subscription is set to cancel. Access continues until July 31, 2026.",
        "access_ends_on": "July 31, 2026",
        "show_manage_billing": True,
        "show_open_tools": True,
        "redirect": None,
    }
    with patch("app._resolve_subscribe_gate", return_value=gate), patch(
        "app._create_subscription_checkout_session"
    ) as create_checkout:
        res = app_client.get("/subscribe")
        create_checkout.assert_not_called()
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "July 31, 2026" in html
    assert "Manage Billing" in html
    assert "Open Tools" in html
    assert "Continue to checkout" not in html
    assert 'method="post"' not in html


def test_past_due_blocks_new_subscription(app_client, two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    db.update_user_subscription(u1, "past_due")
    _login(app_client, u1)
    gate = {
        "can_checkout": False,
        "state": "past_due",
        "message": "Your payment is past due. Update billing to restore full access — do not start a new subscription.",
        "access_ends_on": None,
        "show_manage_billing": True,
        "show_open_tools": False,
        "redirect": None,
    }
    with patch("app._resolve_subscribe_gate", return_value=gate), patch(
        "app._create_subscription_checkout_session"
    ) as create_checkout:
        get_res = app_client.get("/subscribe")
        post_res = app_client.post("/subscribe", data={"email": "x@y.com"})
        create_checkout.assert_not_called()
    assert get_res.status_code == 200
    assert b"past due" in get_res.data.lower()
    assert b"Manage Billing" in get_res.data
    assert b"Continue to checkout" not in get_res.data
    assert post_res.status_code == 409


def test_incomplete_and_unpaid_recovery_states(app_client, two_users, monkeypatch):
    _billing_ok(monkeypatch)
    u1, _ = two_users
    _login(app_client, u1)
    for state, needle in (
        ("incomplete", "not completed"),
        ("unpaid", "unpaid"),
        ("paused", "paused"),
    ):
        gate = {
            "can_checkout": False,
            "state": state,
            "message": f"recovery for {needle}",
            "access_ends_on": None,
            "show_manage_billing": True,
            "show_open_tools": False,
            "redirect": None,
        }
        with patch("app._resolve_subscribe_gate", return_value=gate):
            res = app_client.get("/subscribe")
        assert res.status_code == 200, state
        html = res.get_data(as_text=True)
        assert "Manage Billing" in html, state
        assert "Continue to checkout" not in html, state


def test_resolve_subscribe_gate_classifies_canceling():
    sub = {
        "id": "sub_1",
        "status": "active",
        "cancel_at_period_end": True,
        "current_period_end": 4102444800,  # 2099-12-31
    }
    classified = stripe_billing.classify_subscription(sub)
    assert classified["state"] == "canceling"
    assert classified["access_ends_on"]


def test_resolve_subscribe_gate_local_active(two_users, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", None)
    monkeypatch.setattr(config, "SUBSCRIPTION_REQUIRED", True)
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    user = db.get_user_by_id(u1)
    gate = stripe_billing.resolve_subscribe_gate(user, check_stripe=False)
    assert gate["can_checkout"] is False
    assert gate["state"] == "active"
    assert gate["redirect"] == "subscriber_app"


def test_resolve_subscribe_gate_local_past_due(two_users, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", None)
    u1, _ = two_users
    db.update_user_subscription(u1, "past_due")
    user = db.get_user_by_id(u1)
    gate = stripe_billing.resolve_subscribe_gate(user, check_stripe=False)
    assert gate["can_checkout"] is False
    assert gate["state"] == "past_due"
    assert gate["show_manage_billing"] is True


def test_checkout_idempotency_key_stable_within_bucket(monkeypatch):
    monkeypatch.setattr(config, "STRIPE_PRICE_ID", "price_1TfRM1BKSi4KGHsxagBmTsgG")
    with patch("stripe_billing.time.time", return_value=1_700_000_000):
        a = stripe_billing.checkout_idempotency_key(42)
        b = stripe_billing.checkout_idempotency_key(42)
    assert a == b
    assert a.startswith("subchk_42_")


def test_create_checkout_passes_idempotency_key(monkeypatch):
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
    assert create.call_args.kwargs["idempotency_key"] == "subchk_test_key"
    assert create.call_args.kwargs["customer"] == "cus_abc"


def test_app_already_subscribed_notice(app_client, two_users, monkeypatch):
    monkeypatch.setattr(config, "SUBSCRIPTION_REQUIRED", True)
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    res = app_client.get("/app?already_subscribed=1")
    assert res.status_code == 200
    assert b"already active" in res.data.lower()
