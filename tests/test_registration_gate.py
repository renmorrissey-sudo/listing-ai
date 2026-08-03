"""Private-beta registration gate (REGISTRATION_ENABLED / ALLOWLIST)."""

from unittest.mock import MagicMock, patch

import auth
import config
import db
import registration_gate
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


def _close_registration(monkeypatch, allowlist=None):
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", False)
    monkeypatch.setattr(config, "REGISTRATION_ALLOWLIST", set(allowlist or []))


def _open_registration(monkeypatch):
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(config, "REGISTRATION_ALLOWLIST", set())


def test_missing_registration_enabled_defaults_closed(monkeypatch):
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", False)
    assert registration_gate.registration_is_open() is False
    assert registration_gate.registration_allowed_for_email("anyone@example.com") is False


def test_registration_enabled_true_restores_flow(app_client, monkeypatch):
    _billing_ok(monkeypatch)
    _open_registration(monkeypatch)
    res = app_client.get("/subscribe")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Continue to checkout" in html
    assert "Private beta" not in html or "Create account" in html


def test_closed_blocks_public_account_creation(app_client, monkeypatch):
    _billing_ok(monkeypatch)
    _close_registration(monkeypatch)
    before = db.get_user_by_email("blocked-new@example.com")
    assert before is None
    with patch("app._create_subscription_checkout_session") as create_checkout, patch(
        "stripe.Customer.create"
    ) as customer_create, patch(
        "stripe.checkout.Session.create"
    ) as session_create:
        res = app_client.post(
            "/subscribe",
            data={
                "email": "blocked-new@example.com",
                "password": "password123",
                "confirm_password": "password123",
            },
            headers={"Accept": "application/json"},
        )
    assert res.status_code == 403
    data = res.get_json()
    assert data["error"] == "registration_closed"
    assert "unavailable" in data["message"].lower()
    assert db.get_user_by_email("blocked-new@example.com") is None
    create_checkout.assert_not_called()
    customer_create.assert_not_called()
    session_create.assert_not_called()


def test_closed_blocks_direct_checkout_session_creation(monkeypatch):
    _billing_ok(monkeypatch)
    _close_registration(monkeypatch)
    user = {"id": 1, "email": "nope@example.com", "stripe_customer_id": None}
    with patch("stripe.Customer.create") as customer_create, patch(
        "stripe.checkout.Session.create"
    ) as session_create, patch(
        "stripe_billing.stripe_customer_for_email", return_value=None
    ):
        try:
            stripe_billing.create_subscription_checkout_session(
                user,
                success_url="https://example.com/ok",
                cancel_url="https://example.com/cancel",
            )
            raised = False
        except registration_gate.RegistrationClosedError:
            raised = True
    assert raised
    customer_create.assert_not_called()
    session_create.assert_not_called()


def test_public_subscribe_get_shows_private_beta(app_client, monkeypatch):
    _billing_ok(monkeypatch)
    _close_registration(monkeypatch)
    res = app_client.get("/subscribe", follow_redirects=False)
    assert res.status_code in (301, 302)
    assert "/private-beta" in res.headers["Location"]
    page = app_client.get("/private-beta")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "TopAI is currently in private beta" in html
    assert "Sign in" in html
    assert "support@topairealestatetools.com" in html
    assert "Traceback" not in html
    assert "Something went wrong" not in html


def test_existing_users_can_still_sign_in(app_client, two_users, monkeypatch):
    _close_registration(monkeypatch)
    u1, _ = two_users
    email = db.get_user_by_id(u1)["email"]
    res = app_client.post(
        "/login",
        data={"email": email, "password": "password123"},
        follow_redirects=False,
    )
    assert res.status_code in (301, 302)
    assert "/app" in res.headers["Location"]


def test_existing_subscribers_retain_tool_access(app_client, two_users, monkeypatch):
    _close_registration(monkeypatch)
    monkeypatch.setattr(config, "SUBSCRIPTION_REQUIRED", True)
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    res = app_client.get("/app")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Listing Generator" in html or "tool-" in html or "Subscriber" in html


def test_manage_billing_remains_available(app_client, two_users, monkeypatch):
    _close_registration(monkeypatch)
    _billing_ok(monkeypatch)
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    db.set_stripe_customer(u1, "cus_existing_portal")
    _login(app_client, u1)
    with patch(
        "stripe.billing_portal.Session.create",
        return_value=MagicMock(url="https://billing.stripe.test/session"),
    ) as portal:
        res = app_client.get("/billing/portal", follow_redirects=False)
    assert res.status_code in (301, 302, 303)
    assert "billing.stripe.test" in res.headers["Location"]
    portal.assert_called_once()


def test_password_reset_remains_available(app_client, monkeypatch):
    _close_registration(monkeypatch)
    res = app_client.get("/forgot-password")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "email" in html.lower()
    assert "private beta" not in html.lower() or "forgot" in html.lower()


def test_allowlisted_email_can_register_and_subscribe(app_client, monkeypatch):
    _billing_ok(monkeypatch)
    _close_registration(monkeypatch, allowlist={"tester@example.com"})
    fake_session = MagicMock()
    fake_session.url = "https://checkout.stripe.test/session/cs_allow"
    with patch(
        "app._create_subscription_checkout_session", return_value=fake_session
    ) as create_checkout, patch(
        "app._stripe_customer_for_email", return_value=None
    ):
        res = app_client.post(
            "/subscribe",
            data={
                "email": "tester@example.com",
                "password": "password123",
                "confirm_password": "password123",
            },
            follow_redirects=False,
        )
    assert res.status_code == 303
    assert "checkout.stripe.test" in res.headers["Location"]
    create_checkout.assert_called_once()
    assert db.get_user_by_email("tester@example.com") is not None


def test_allowlist_matching_is_case_insensitive(app_client, monkeypatch):
    _billing_ok(monkeypatch)
    _close_registration(monkeypatch, allowlist={"ren.morrissey@gmail.com"})
    fake_session = MagicMock()
    fake_session.url = "https://checkout.stripe.test/session/cs_case"
    with patch(
        "app._create_subscription_checkout_session", return_value=fake_session
    ) as create_checkout, patch(
        "app._stripe_customer_for_email", return_value=None
    ):
        res = app_client.post(
            "/subscribe",
            data={
                "email": "Ren.Morrissey@Gmail.com",
                "password": "password123",
                "confirm_password": "password123",
            },
            follow_redirects=False,
        )
    assert res.status_code == 303
    create_checkout.assert_called_once()
    assert db.get_user_by_email("ren.morrissey@gmail.com") is not None


def test_non_allowlisted_authenticated_cannot_create_subscription(
    app_client, two_users, monkeypatch
):
    _billing_ok(monkeypatch)
    _close_registration(monkeypatch)
    u1, _ = two_users
    _login(app_client, u1)
    with patch("app._create_subscription_checkout_session") as create_checkout, patch(
        "stripe.checkout.Session.create"
    ) as session_create:
        res = app_client.post(
            "/subscribe",
            data={},
            headers={"Accept": "application/json"},
        )
    assert res.status_code == 403
    assert res.get_json()["error"] == "registration_closed"
    create_checkout.assert_not_called()
    session_create.assert_not_called()


def test_api_routes_return_structured_403(app_client, monkeypatch):
    _billing_ok(monkeypatch)
    _close_registration(monkeypatch)
    res = app_client.post(
        "/subscribe",
        data={
            "email": "api-block@example.com",
            "password": "password123",
            "confirm_password": "password123",
        },
        headers={"Accept": "application/json"},
    )
    assert res.status_code == 403
    assert res.is_json
    body = res.get_json()
    assert body == {
        "error": "registration_closed",
        "message": "New registrations are temporarily unavailable.",
    }


def test_mobile_nav_no_active_signup_path(app_client, monkeypatch):
    _close_registration(monkeypatch)
    for path in ("/", "/features", "/how-it-works", "/pricing"):
        html = app_client.get(path).get_data(as_text=True)
        assert 'id="mkt-nav-toggle"' in html, path
        assert "Private beta" in html, path
        assert 'href="/private-beta"' in html, path
        # No active public signup CTA in the marketing nav.
        nav = html.split('id="mkt-nav"', 1)[1].split("</nav>", 1)[0]
        assert 'href="/subscribe"' not in nav, path
        assert "Continue to checkout" not in html
        assert "Create account" not in nav


def test_direct_register_endpoint_cannot_bypass(app_client, monkeypatch):
    _close_registration(monkeypatch)
    res = app_client.get("/register", follow_redirects=False)
    assert res.status_code in (301, 302)
    assert "/private-beta" in res.headers["Location"]
    post = app_client.post(
        "/register",
        data={
            "email": "bypass@example.com",
            "password": "password123",
            "confirm_password": "password123",
        },
        headers={"Accept": "application/json"},
    )
    assert post.status_code == 403
    assert post.get_json()["error"] == "registration_closed"
    assert db.get_user_by_email("bypass@example.com") is None


def test_existing_subscriptions_unchanged(app_client, two_users, monkeypatch):
    _close_registration(monkeypatch)
    u1, _ = two_users
    db.update_user_subscription(u1, "active", subscription_id="sub_keep_me")
    db.set_stripe_customer(u1, "cus_keep_me")
    before = db.get_user_by_id(u1)
    _login(app_client, u1)
    app_client.get("/subscribe")
    app_client.get("/private-beta")
    after = db.get_user_by_id(u1)
    assert after["subscription_status"] == before["subscription_status"] == "active"
    assert after["subscription_id"] == before["subscription_id"] == "sub_keep_me"
    assert after["stripe_customer_id"] == before["stripe_customer_id"] == "cus_keep_me"


def test_homepage_shows_private_beta_cta(app_client, monkeypatch):
    _close_registration(monkeypatch)
    html = app_client.get("/").get_data(as_text=True)
    assert "Private beta" in html
    assert 'id="hero-start-trial"' in html
    assert 'href="/private-beta"' in html
    assert registration_gate.PRIVATE_BETA_SUPPORTING in html


def test_allowlisted_authenticated_user_gets_subscribe_flow(
    app_client, monkeypatch
):
    _billing_ok(monkeypatch)
    _close_registration(monkeypatch, allowlist={"allow@example.com"})
    uid = db.create_user("allow@example.com", auth.hash_password("password123"))
    _login(app_client, uid)
    res = app_client.get("/subscribe")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Continue to checkout" in html
    assert 'action="/subscribe"' in html


def test_request_param_cannot_force_registration_open(app_client, monkeypatch):
    _billing_ok(monkeypatch)
    _close_registration(monkeypatch)
    res = app_client.get(
        "/subscribe?REGISTRATION_ENABLED=true&registration_enabled=1",
        follow_redirects=False,
    )
    assert res.status_code in (301, 302)
    assert "/private-beta" in res.headers["Location"]
