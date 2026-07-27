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
    ("Cold Call Scripts", "/app#coldcall"),
    ("AI Calling Assistant", "/app#voice"),
    ("AI SMS Assistant", "/app#sms"),
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
    assert "Access Tools" in html
    assert 'id="gate"' not in html


def test_app_tools_page_has_gate_for_visitors(app_client):
    html = app_client.get("/app").get_data(as_text=True)
    assert 'id="public-nav"' in html
    assert 'id="gate"' in html
    assert "Subscriber Access" in html


def test_all_application_links_in_tool_nav(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    html = app_client.get("/crm/leads").get_data(as_text=True)
    for label, href in APP_NAV_LINKS:
        assert label in html
        assert href in html or href.replace("&", "&amp;") in html


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
    assert 'id="public-nav"' in html
    # Subscriber footer provides legal links when JS hides public-nav.
    assert "/terms" in html
    assert "/privacy" in html
