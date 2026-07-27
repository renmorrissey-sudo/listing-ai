"""Public marketing pages must not auto-open the Subscriber Access modal."""

import re

import seo


PUBLIC_PATHS = list(seo.PUBLIC_MARKETING_PATHS)


def test_how_it_works_public_200_without_auth(app_client):
    res = app_client.get("/how-it-works")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert 'id="gate"' not in html
    assert "gate-overlay" not in html
    assert "<h2>Subscriber Access</h2>" not in html
    assert "How" in html and "works" in html.lower()
    assert "<video" in html
    assert "topai-how-it-works.mp4" in html
    assert 'id="access-tools-cta"' in html
    assert 'href="/app"' in html
    assert not re.search(r'name=["\']robots["\']\s+content=["\'][^"\']*noindex', html, re.I)
    assert 'rel="canonical"' in html
    assert "https://topairealestatetools.com/how-it-works" in html


def test_public_marketing_pages_no_gate_overlay(app_client):
    for path in PUBLIC_PATHS:
        res = app_client.get(path)
        assert res.status_code == 200, path
        html = res.get_data(as_text=True)
        assert 'id="gate"' not in html, path
        assert "gate-overlay" not in html, path
        assert "<h2>Subscriber Access</h2>" not in html, path
        assert not re.search(
            r'name=["\']robots["\']\s+content=["\'][^"\']*noindex', html, re.I
        ), path
        assert 'rel="canonical"' in html, path
        assert seo.canonical_loc(path) in html, path


def test_home_landing_is_public_and_indexable(app_client):
    res = app_client.get("/")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "<h2>Subscriber Access</h2>" not in html
    assert 'id="gate"' not in html
    assert "<video" in html or "landing-video" in html
    assert "Start trial" in html
    assert "View pricing" in html
    assert "Access Tools" in html
    assert "Sign in" in html
    assert 'href="/features"' in html
    assert 'href="/how-it-works"' in html
    assert 'href="/pricing"' in html
    assert 'href="/terms"' in html
    assert 'href="/privacy"' in html
    assert 'href="/refund-policy"' in html
    assert 'href="/contact"' in html


def test_access_tools_opens_subscriber_gate(app_client):
    res = app_client.get("/app")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert 'id="gate"' in html
    assert "Subscriber Access" in html
    assert "gate-overlay" in html
    # Modal is present for intentional tool access (not hidden by default).
    assert 'class="gate-overlay hidden"' not in html
    assert re.search(r'name=["\']robots["\']\s+content=["\'][^"\']*noindex', html, re.I)


def test_private_tools_redirect_to_app_gate(app_client):
    for path in ("/dashboard", "/tutorial", "/crm/leads", "/crm/tasks"):
        res = app_client.get(path, follow_redirects=False)
        assert res.status_code in (301, 302), path
        loc = res.headers.get("Location", "")
        assert "/app" in loc, f"{path} -> {loc}"


def test_generate_api_still_protected(app_client):
    res = app_client.post("/generate", json={})
    assert res.status_code in (401, 402)


def test_public_nav_preserved_on_marketing_pages(app_client):
    for path in ("/features", "/how-it-works", "/pricing"):
        html = app_client.get(path).get_data(as_text=True)
        for label in ("Features", "How It Works", "Pricing"):
            assert label in html, path
        assert "Access Tools" in html, path
