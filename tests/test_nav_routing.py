"""Customer-facing Sign in / Start trial / View pricing routing."""

import re
from html.parser import HTMLParser

import auth
import db


class _AnchorNestChecker(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.nested = False

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            if self.depth > 0:
                self.nested = True
            self.depth += 1

    def handle_endtag(self, tag):
        if tag == "a" and self.depth:
            self.depth -= 1


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        user = db.get_user_by_id(user_id)
        sess["session_version"] = int((user or {}).get("session_version") or 1)


def _href_for_label(html, label):
    pattern = rf'<a\b[^>]*href="([^"]*)"[^>]*>\s*{re.escape(label)}\s*</a>'
    match = re.search(pattern, html, re.I)
    if match:
        return match.group(1)
    # href after text (rare)
    pattern2 = rf'<a\b[^>]*>\s*{re.escape(label)}\s*</a>'
    for m in re.finditer(rf'<a\b([^>]*)>\s*{re.escape(label)}\s*</a>', html, re.I):
        hm = re.search(r'href="([^"]*)"', m.group(1))
        if hm:
            return hm.group(1)
    return None


def test_homepage_header_and_hero_routing(app_client):
    html = app_client.get("/").get_data(as_text=True)
    assert 'href="/login" id="mkt-sign-in"' in html or re.search(
        r'id="mkt-sign-in"[^>]*href="/login"|href="/login"[^>]*id="mkt-sign-in"', html
    )
    assert 'id="mkt-start-trial"' in html and 'href="/subscribe"' in html
    assert 'id="hero-start-trial"' in html
    assert 'href="/subscribe" id="hero-start-trial"' in html or re.search(
        r'id="hero-start-trial"[^>]*href="/subscribe"|href="/subscribe"[^>]*id="hero-start-trial"',
        html,
    )
    assert re.search(
        r'id="hero-view-pricing"[^>]*href="/pricing"|href="/pricing"[^>]*id="hero-view-pricing"',
        html,
    )
    assert re.search(
        r'id="access-tools-cta"[^>]*href="/login"|href="/login"[^>]*id="access-tools-cta"',
        html,
    )
    assert 'href="/login?next=/app" id="access-tools-cta"' not in html
    assert "buy.stripe.com" not in html
    assert _href_for_label(html, "Sign in") == "/login"
    assert _href_for_label(html, "Start trial") == "/subscribe"
    assert _href_for_label(html, "View pricing") == "/pricing"


def test_subscribe_sign_in_goes_to_login(app_client, monkeypatch):
    monkeypatch.setattr("config.STRIPE_SECRET_KEY", "sk_test_123")
    monkeypatch.setattr("config.STRIPE_PRICE_ID", "price_1TfRM1BKSi4KGHsxagBmTsgG")
    html = app_client.get("/subscribe").get_data(as_text=True)
    assert "Already have an account?" in html
    assert re.search(
        r'Already have an account\?\s*<a href="/login">Sign in</a>',
        html,
    )
    assert "checkout.stripe.com" not in html
    assert "buy.stripe.com" not in html


def test_mobile_marketing_nav_present(app_client):
    for path in ("/", "/features", "/how-it-works", "/pricing"):
        html = app_client.get(path).get_data(as_text=True)
        assert 'id="mkt-nav-toggle"' in html, path
        assert 'id="mkt-nav"' in html, path
        assert 'aria-controls="mkt-nav"' in html, path
        assert 'href="/login"' in html, path
        assert 'href="/subscribe"' in html, path


def test_unauth_header_sign_in_and_start_trial(app_client):
    html = app_client.get("/features").get_data(as_text=True)
    assert "Sign in" in html
    assert "Start trial" in html
    assert "Open Tools" not in html
    assert _href_for_label(html, "Sign in") == "/login"
    assert _href_for_label(html, "Start trial") == "/subscribe"


def test_auth_subscribed_header_open_tools_hides_trial(app_client, two_users, monkeypatch):
    monkeypatch.setattr("config.SUBSCRIPTION_REQUIRED", True)
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    html = app_client.get("/features").get_data(as_text=True)
    nav = html.split('id="mkt-nav"', 1)[1].split("</nav>", 1)[0]
    assert "Open Tools" in nav
    assert "Dashboard" in nav
    assert "Log out" in nav
    assert "Sign in" not in nav
    assert "Start trial" not in nav
    assert "Sign in" not in html
    assert "Start trial" not in html
    assert 'href="/app"' in html


def test_auth_unsubscribed_header_open_tools_keeps_trial(app_client, two_users, monkeypatch):
    monkeypatch.setattr("config.SUBSCRIPTION_REQUIRED", True)
    u1, _ = two_users
    _login(app_client, u1)
    html = app_client.get("/features").get_data(as_text=True)
    nav = html.split('id="mkt-nav"', 1)[1].split("</nav>", 1)[0]
    assert "Open Tools" in nav
    assert "Start trial" in nav
    assert "Sign in" not in nav
    assert "Sign in" not in html
    assert _href_for_label(html, "Start trial") == "/subscribe"
    assert 'href="/app"' in html


def test_unauthenticated_app_redirects_with_next(app_client):
    res = app_client.get("/app", follow_redirects=False)
    assert res.status_code in (301, 302)
    loc = res.headers["Location"]
    assert "/login" in loc
    assert "next=/app" in loc or "next=%2Fapp" in loc


def test_external_next_rejected_on_login(app_client, two_users):
    u1, _ = two_users
    email = db.get_user_by_id(u1)["email"]
    for bad in (
        "https://evil.example/phish",
        "//evil.example",
        "/\\evil.example",
        "\\\\evil.example",
    ):
        res = app_client.post(
            f"/login?next={bad}",
            data={"email": email, "password": "password123", "next": bad},
            follow_redirects=False,
        )
        assert res.status_code in (301, 302), bad
        loc = res.headers["Location"]
        assert "evil.example" not in loc, loc
        assert loc.endswith("/app") or loc.rstrip("/").endswith("/app")


def test_safe_next_url_helper():
    assert auth.safe_next_url("/dashboard") == "/dashboard"
    assert auth.safe_next_url("/app?x=1") == "/app?x=1"
    assert auth.safe_next_url("https://evil.com") == "/app"
    assert auth.safe_next_url("//evil.com") == "/app"
    assert auth.safe_next_url("/\\evil.com") == "/app"
    assert auth.safe_next_url(None) == "/app"
    assert auth.safe_next_url("") == "/app"


def test_no_nested_anchors_on_marketing_pages(app_client):
    for path in ("/", "/features", "/how-it-works", "/pricing", "/subscribe", "/login"):
        html = app_client.get(path).get_data(as_text=True)
        checker = _AnchorNestChecker()
        checker.feed(html)
        assert not checker.nested, path


def test_marketing_sign_in_not_stripe_checkout(app_client):
    for path in ("/", "/features", "/how-it-works", "/pricing"):
        html = app_client.get(path).get_data(as_text=True)
        for m in re.finditer(r'<a\b[^>]*>\s*Sign in\s*</a>', html, re.I):
            tag = m.group(0)
            assert "stripe" not in tag.lower()
            assert "checkout" not in tag.lower()
            assert 'href="/login"' in tag or "href='/login'" in tag


def test_login_success_uses_validated_next(app_client, two_users):
    u1, _ = two_users
    email = db.get_user_by_id(u1)["email"]
    res = app_client.post(
        "/login?next=/dashboard",
        data={"email": email, "password": "password123"},
        follow_redirects=False,
    )
    assert res.status_code in (301, 302)
    assert "/dashboard" in res.headers["Location"]
