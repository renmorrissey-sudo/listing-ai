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


def test_get_subscribe_returns_200_html(app_client):
    with patch.object(config, "STRIPE_SECRET_KEY", "sk_test_123"), patch.object(
        config, "STRIPE_PRICE_ID", "price_1TfRM1BKSi4KGHsxagBmTsgG"
    ), patch.object(config, "STRIPE_PUBLISHABLE_KEY", None):
        res = app_client.get("/subscribe")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "text/html" in res.content_type
    assert "TopAI" in html
    assert "Start your subscription" in html
    assert config.SUBSCRIPTION_PRICE.split("/")[0] in html or "$49" in html
    assert "Billed monthly" in html
    assert "TRIAL50" in html or "50% off" in html
    assert 'name="email"' in html
    assert 'name="password"' in html
    assert 'name="confirm_password"' in html
    assert 'href="/terms"' in html
    assert 'href="/privacy"' in html
    assert 'href="/refund-policy"' in html
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
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_123")
    monkeypatch.setattr(config, "STRIPE_PRICE_ID", "price_1TfRM1BKSi4KGHsxagBmTsgG")
    monkeypatch.setattr(config, "STRIPE_PUBLISHABLE_KEY", "pk_test_abc")
    monkeypatch.setattr(config, "SUBSCRIPTION_REQUIRED", True)

    fake_session = MagicMock()
    fake_session.url = "https://checkout.stripe.test/session/cs_test_123"

    with patch(
        "app._create_subscription_checkout_session", return_value=fake_session
    ) as create_checkout:
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

    user = db.get_user_by_email("new.subscriber@example.com")
    assert user is not None
    assert user["password_hash"] != "SecurePass99!"
    assert auth.verify_password(user["password_hash"], "SecurePass99!")
    assert user.get("subscription_status") != "active"


def test_duplicate_email_blocks_signup(app_client, two_users, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_123")
    monkeypatch.setattr(config, "STRIPE_PRICE_ID", "price_1TfRM1BKSi4KGHsxagBmTsgG")
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
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    res = app_client.get("/subscribe", follow_redirects=False)
    assert res.status_code in (301, 302)
    assert "/app" in res.headers["Location"]


def test_start_trial_homepage_points_to_subscribe(app_client):
    html = app_client.get("/").get_data(as_text=True)
    assert 'href="/subscribe"' in html
    assert "Start trial" in html
    assert "View pricing" in html
    assert 'href="/login' in html
