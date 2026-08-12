"""Subscriber navigation: single primary nav bar on authenticated pages."""

import re

import db


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id


APP_NAV_LINKS = [
    ("Dashboard", "/dashboard"),
    ("Leads", "/crm/leads"),
    ("Leads Calendar", "/crm/calendar"),
    ("Follow-ups", "/crm/follow-ups"),
    ("Tasks", "/crm/tasks"),
    ("Needs Attention", "/crm/needs-attention"),
    ("Listing Generator", "/app"),
    ("Listing Archive", "/listings/archive"),
    ("Cold Call Scripts", "/app#coldcall"),
    ("AI Calling Assistant", "/app#voice"),
    ("AI SMS Assistant", "/app#sms"),
    ("Bulk SMS", "/crm/sms-campaigns"),
]

ACCOUNT_LINKS = [
    ("Billing", "/billing"),
    ("Social Media", "/social/connections"),
    ("Email Marketing", "/integrations/email-marketing"),
    ("SMS Diagnostics", "/crm/sms-diagnostics"),
    ("Tutorial", "/tutorial"),
]

LEGAL_LINKS = [
    ("/terms", "Terms"),
    ("/privacy", "Privacy"),
    ("/refund-policy", "Refund Policy"),
    ("/contact", "Contact"),
]


def test_authenticated_pages_have_one_primary_nav(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    for path in [
        "/dashboard?local_date=2026-07-26&tz_offset_minutes=0",
        "/crm/leads",
        "/crm/tasks",
        "/tutorial",
        "/app",
        "/billing",
    ]:
        html = app_client.get(path).get_data(as_text=True)
        assert html.count('class="public-nav"') == 0
        assert html.count('aria-label="Main application navigation"') == 1
        assert html.count('id="tool-nav-bar"') == 1


def test_public_pages_keep_marketing_navigation(app_client):
    html = app_client.get("/features").get_data(as_text=True)
    assert "Features" in html
    assert "Pricing" in html or "How It Works" in html
    assert 'aria-label="Main application navigation"' not in html


def test_index_public_nav_for_visitors(app_client):
    html = app_client.get("/").get_data(as_text=True)
    assert "Features" in html
    assert "Pricing" in html
    assert "How It Works" in html
    assert "Sign in" in html
    assert "Access Tools" not in html
    assert 'id="gate"' not in html


def test_app_tools_page_requires_login_for_visitors(app_client):
    res = app_client.get("/app", follow_redirects=False)
    assert res.status_code in (301, 302)
    assert "/login" in res.headers.get("Location", "")


def test_all_application_links_in_tool_nav(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    html = app_client.get("/crm/leads").get_data(as_text=True)
    nav_section = html.split('aria-label="Main application navigation"')[1].split("</nav>")[0]
    for label, href in APP_NAV_LINKS:
        assert label in nav_section
        assert href in nav_section or href.replace("&", "&amp;") in nav_section
    for label, href in ACCOUNT_LINKS:
        assert label not in nav_section
        assert href not in nav_section


def test_account_menu_contains_billing_and_configuration_links(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    html = app_client.get("/dashboard?local_date=2026-07-26").get_data(as_text=True)
    account = html.split('id="account-menu"', 1)[1].split("</details>", 1)[0]
    assert "Account" in account
    for label, href in ACCOUNT_LINKS:
        assert label in account
        assert f'href="{href}"' in account
    assert 'action="/logout"' in account
    assert "Sign out" in account


def test_legal_links_in_footer_not_tool_nav(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    html = app_client.get("/dashboard?local_date=2026-07-26&tz_offset_minutes=0").get_data(as_text=True)
    nav_section = html.split('aria-label="Main application navigation"')[1].split("</nav>")[0]
    for href, label in LEGAL_LINKS:
        assert label in html
        assert href in html
        assert href not in nav_section


def test_active_nav_state(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    tasks_html = app_client.get("/crm/tasks").get_data(as_text=True)
    assert re.search(
        r'href="/crm/tasks"\s+class="active"',
        tasks_html,
    )
    assert 'href="/crm/tasks" class="active"' in tasks_html or (
        "/crm/tasks" in tasks_html and "active" in tasks_html
    )

    dash_html = app_client.get("/dashboard?local_date=2026-07-26&tz_offset_minutes=0").get_data(as_text=True)
    assert re.search(r'href="/dashboard"\s+class="active"', dash_html)


def test_logo_links_to_dashboard(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    html = app_client.get("/crm/leads").get_data(as_text=True)
    assert 'href="/dashboard"' in html
    assert 'aria-label="TopAI Real Estate Tools — Dashboard"' in html
    assert re.search(
        r'<a class="logo" href="/dashboard"[^>]*>TopAI',
        html,
    )


def test_mobile_nav_toggle_present(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    html = app_client.get("/crm/leads").get_data(as_text=True)
    assert 'id="tool-nav-toggle"' in html
    assert 'aria-controls="tool-nav"' in html
    assert 'id="account-menu"' in html
    assert 'aria-label="Open account menu"' in html
    assert 'name="viewport"' in html and "width=device-width" in html


def test_index_hides_public_nav_when_logged_in(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    # Logged-in subscribers hitting / are redirected to the dashboard.
    home = app_client.get("/", follow_redirects=False)
    assert home.status_code in (301, 302)
    assert "/dashboard" in home.headers.get("Location", "")

    html = app_client.get("/app").get_data(as_text=True)
    assert 'id="subscriber-footer"' in html
    assert 'id="public-nav"' not in html
    assert html.count('id="tool-nav-bar"') == 1
    assert html.count('id="account-menu"') == 1
    assert "/terms" in html
    assert "/privacy" in html


def test_billing_uses_authenticated_application_shell(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    html = app_client.get("/billing").get_data(as_text=True)
    assert "<h1>Billing</h1>" in html
    assert 'id="tool-nav-bar"' in html
    assert 'id="account-menu"' in html
    assert 'href="/billing"' in html
    assert 'aria-current="page"' in html
    assert 'action="/billing/update-payment-method"' in html


def test_account_menu_billing_visible_without_stripe_customer(app_client, two_users):
    u1, _ = two_users
    assert not db.get_user_by_id(u1).get("stripe_customer_id")
    _login(app_client, u1)
    html = app_client.get("/crm/leads").get_data(as_text=True)
    account = html.split('id="account-menu"', 1)[1].split("</details>", 1)[0]
    assert 'href="/billing"' in account


def test_account_menu_sign_out_clears_session(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    response = app_client.post("/logout", follow_redirects=False)
    assert response.status_code in (200, 301, 302, 303)
    billing = app_client.get("/billing", follow_redirects=False)
    assert billing.status_code in (301, 302, 303)
    assert "/login" in billing.headers.get("Location", "")
