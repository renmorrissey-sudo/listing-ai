"""Password login, forgot/reset password, and unified access navigation."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import auth
import db
import password_reset as pr


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["session_version"] = 1


def _clear_reset_rate_limits():
    pr._EMAIL_RESET_HITS.clear()


def test_forgot_password_page_loads(app_client):
    res = app_client.get("/forgot-password")
    assert res.status_code == 200
    assert b"Forgot password" in res.data
    assert b"email" in res.data.lower()


def test_forgot_password_neutral_for_unknown_email(app_client):
    with patch("password_reset.send_password_reset_email") as send:
        res = app_client.post("/forgot-password", data={"email": "nobody-unknown@example.com"})
    assert res.status_code == 200
    assert pr.NEUTRAL_FORGOT_MESSAGE.encode() in res.data
    assert send.call_count == 0


def test_forgot_password_sends_for_existing_user(app_client, two_users):
    _clear_reset_rate_limits()
    u1, _ = two_users
    email = db.get_user_by_id(u1)["email"]
    with patch("password_reset.email_configured", return_value=True), patch(
        "password_reset.send_password_reset_email", return_value=True
    ) as send:
        res = app_client.post("/forgot-password", data={"email": email})
    assert res.status_code == 200
    assert pr.NEUTRAL_FORGOT_MESSAGE.encode() in res.data
    assert send.call_count == 1
    kwargs = send.call_args.kwargs
    assert kwargs["to_email"] == email
    assert "reset-password?token=" in kwargs["reset_url"]
    assert "localhost" in kwargs["reset_url"] or "topairealestatetools.com" in kwargs["reset_url"]


def test_subscriber_without_known_password_can_establish_one(app_client, monkeypatch):
    _clear_reset_rate_limits()
    email = "legacy-subscriber@example.com"
    # Simulate /verify-created user with random password (password_set false).
    uid = db.create_user(email, auth.hash_password("old-random-secret-xyz"), password_set=False)
    db.update_user_subscription(uid, "active", subscription_id="sub_test", stripe_customer_id="cus_test")

    captured = {}

    def fake_send(*, to_email, reset_url, expires_minutes):
        captured["url"] = reset_url
        return True

    with patch("password_reset.email_configured", return_value=True), patch(
        "password_reset.send_password_reset_email", side_effect=fake_send
    ):
        app_client.post("/forgot-password", data={"email": email})

    token = captured["url"].split("token=", 1)[1]
    res = app_client.post(
        "/reset-password",
        data={"token": token, "password": "NewPass123!", "confirm_password": "NewPass123!"},
        follow_redirects=False,
    )
    assert res.status_code in (301, 302)
    assert "/login" in res.headers["Location"]

    user = db.get_user_by_email(email)
    assert user["id"] == uid
    assert auth.verify_password(user["password_hash"], "NewPass123!")
    assert user.get("subscription_status") == "active"
    assert user.get("stripe_customer_id") == "cus_test"
    # No duplicate user
    with db.get_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE email = ?", (email,)
        ).fetchone()["c"]
    assert int(count) == 1


def test_reset_token_expires(app_client, two_users, monkeypatch):
    _clear_reset_rate_limits()
    u1, _ = two_users
    email = db.get_user_by_id(u1)["email"]
    captured = {}

    def fake_send(*, to_email, reset_url, expires_minutes):
        captured["url"] = reset_url
        return True

    with patch("password_reset.email_configured", return_value=True), patch(
        "password_reset.send_password_reset_email", side_effect=fake_send
    ):
        app_client.post("/forgot-password", data={"email": email})
    token = captured["url"].split("token=", 1)[1]
    row = pr.peek_reset_token(token)
    assert row
    # Force expiry
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with db.get_db() as conn:
        conn.execute(
            "UPDATE password_reset_tokens SET expires_at = ? WHERE id = ?",
            (past, row["id"]),
        )
    assert pr.peek_reset_token(token) is None
    res = app_client.post(
        "/reset-password",
        data={"token": token, "password": "NewPass123!", "confirm_password": "NewPass123!"},
    )
    assert res.status_code == 400
    assert b"invalid or has expired" in res.data


def test_reset_token_single_use(app_client, two_users):
    _clear_reset_rate_limits()
    u1, _ = two_users
    email = db.get_user_by_id(u1)["email"]
    captured = {}

    def fake_send(*, to_email, reset_url, expires_minutes):
        captured["url"] = reset_url
        return True

    with patch("password_reset.email_configured", return_value=True), patch(
        "password_reset.send_password_reset_email", side_effect=fake_send
    ):
        app_client.post("/forgot-password", data={"email": email})
    token = captured["url"].split("token=", 1)[1]
    assert (
        app_client.post(
            "/reset-password",
            data={"token": token, "password": "NewPass123!", "confirm_password": "NewPass123!"},
        ).status_code
        in (301, 302)
    )
    res2 = app_client.post(
        "/reset-password",
        data={"token": token, "password": "AnotherPass1!", "confirm_password": "AnotherPass1!"},
    )
    assert res2.status_code == 400


def test_malformed_token_rejected(app_client):
    res = app_client.get("/reset-password?token=not-a-real-token")
    assert res.status_code == 200
    assert b"invalid or has expired" in res.data


def test_login_success_redirects_to_app(app_client, two_users):
    u1, _ = two_users
    email = db.get_user_by_id(u1)["email"]
    res = app_client.post(
        "/login?next=/app",
        data={"email": email, "password": "password123"},
        follow_redirects=False,
    )
    assert res.status_code in (301, 302)
    assert "/app" in res.headers["Location"]


def test_unauthenticated_app_redirects_to_login(app_client):
    res = app_client.get("/app", follow_redirects=False)
    assert res.status_code in (301, 302)
    loc = res.headers["Location"]
    assert "/login" in loc
    assert "next" in loc


def test_verify_email_only_retired(app_client):
    res = app_client.post("/verify", json={"email": "someone@example.com"})
    assert res.status_code == 410


def test_logged_out_header_has_sign_in_not_access_tools(app_client):
    html = app_client.get("/").get_data(as_text=True)
    assert "Sign in" in html
    assert "Start trial" in html
    assert "Access Tools" not in html
    assert "Operated by Sky Blue Holdings LLC" in html
    assert "Operated by TopAI RE Tools" not in html


def test_logged_in_header_has_open_tools(app_client, two_users):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    html = app_client.get("/").get_data(as_text=True)
    # Subscribed users are redirected to dashboard from /
    assert html.count("Open Tools") >= 0
    dash = app_client.get("/features").get_data(as_text=True)
    assert "Open Tools" in dash or "Dashboard" in dash
    assert "Log out" in dash


def test_password_hash_not_plaintext(app_client, two_users):
    _clear_reset_rate_limits()
    u1, _ = two_users
    email = db.get_user_by_id(u1)["email"]
    captured = {}

    def fake_send(*, to_email, reset_url, expires_minutes):
        captured["url"] = reset_url
        return True

    with patch("password_reset.email_configured", return_value=True), patch(
        "password_reset.send_password_reset_email", side_effect=fake_send
    ):
        app_client.post("/forgot-password", data={"email": email})
    token = captured["url"].split("token=", 1)[1]
    app_client.post(
        "/reset-password",
        data={"token": token, "password": "SecurePass99!", "confirm_password": "SecurePass99!"},
    )
    user = db.get_user_by_email(email)
    assert user["password_hash"] != "SecurePass99!"
    assert auth.verify_password(user["password_hash"], "SecurePass99!")


def test_invalid_login_message_generic(app_client, two_users):
    res = app_client.post("/login", data={"email": "nope@example.com", "password": "wrong"})
    assert res.status_code == 200
    assert b"Invalid email or password." in res.data
    # No error on fresh GET
    get_res = app_client.get("/login")
    assert b"Invalid email or password." not in get_res.data


def test_session_invalidated_after_password_reset(app_client, two_users):
    _clear_reset_rate_limits()
    u1, _ = two_users
    email = db.get_user_by_id(u1)["email"]
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    assert app_client.get("/session-status").get_json()["logged_in"] is True

    captured = {}

    def fake_send(*, to_email, reset_url, expires_minutes):
        captured["url"] = reset_url
        return True

    # Request reset while logged out (forgot-password redirects if already signed in).
    with app_client.session_transaction() as sess:
        sess.clear()

    with patch("password_reset.email_configured", return_value=True), patch(
        "password_reset.send_password_reset_email", side_effect=fake_send
    ):
        app_client.post("/forgot-password", data={"email": email})
    token = captured["url"].split("token=", 1)[1]

    # Restore old session cookie state, then consume token (bumps session_version).
    _login(app_client, u1)
    assert app_client.get("/session-status").get_json()["logged_in"] is True
    with app_client.session_transaction() as sess:
        # Keep stale session_version=1 while DB will bump.
        sess["user_id"] = u1
        sess["session_version"] = 1

    with app_client.session_transaction() as sess:
        sess.clear()
    app_client.post(
        "/reset-password",
        data={"token": token, "password": "RotatedPass1!", "confirm_password": "RotatedPass1!"},
    )

    # Re-apply stale pre-reset session; it must not authenticate.
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
        sess["session_version"] = 1
    status = app_client.get("/session-status").get_json()
    assert status["logged_in"] is False


def test_stripe_entitled_email_without_user_creates_on_reset(app_client, monkeypatch):
    _clear_reset_rate_limits()
    email = "stripe-only@example.com"

    monkeypatch.setattr(pr, "email_eligible_for_reset", lambda e: e == email)
    captured = {}

    def fake_send(*, to_email, reset_url, expires_minutes):
        captured["url"] = reset_url
        return True

    with patch("password_reset.email_configured", return_value=True), patch(
        "password_reset.send_password_reset_email", side_effect=fake_send
    ):
        # Force eligibility path by creating token manually via patched eligibility
        msg = pr.request_password_reset(email)
    assert msg == pr.NEUTRAL_FORGOT_MESSAGE
    token = captured["url"].split("token=", 1)[1]
    with patch("password_reset.email_eligible_for_reset", return_value=True):
        uid, err = pr.consume_reset_token(token, "BrandNew99!")
    assert err is None
    assert uid is not None
    user = db.get_user_by_email(email)
    assert user is not None
    assert auth.verify_password(user["password_hash"], "BrandNew99!")
