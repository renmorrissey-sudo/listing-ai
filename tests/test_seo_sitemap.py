"""Public sitemap.xml and robots.txt — no auth, no private data."""

import re
import xml.etree.ElementTree as ET

import seo


SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

PRIVATE_SNIPPETS = (
    "/dashboard",
    "/crm/",
    "/crm/leads",
    "/crm/tasks",
    "/crm/follow-ups",
    "/crm/calendar",
    "/crm/needs-attention",
    "/account",
    "/billing",
    "/login",
    "/logout",
    "/api/",
    "/webhook/",
    "/recordings/",
    "/transcripts/",
)


def test_sitemap_returns_200_xml(app_client):
    res = app_client.get("/sitemap.xml")
    assert res.status_code == 200
    ctype = res.headers.get("Content-Type", "")
    assert "xml" in ctype.lower()
    body = res.get_data(as_text=True)
    assert body.startswith("<?xml")
    root = ET.fromstring(body)
    assert root.tag == "{http://www.sitemaps.org/schemas/sitemap/0.9}urlset"


def test_sitemap_canonical_https_urls(app_client):
    res = app_client.get("/sitemap.xml")
    body = res.get_data(as_text=True)
    root = ET.fromstring(body)
    locs = [
        el.text
        for el in root.findall("sm:url/sm:loc", SITEMAP_NS)
    ]
    assert locs
    for loc in locs:
        assert loc.startswith("https://topairealestatetools.com")
        assert "www." not in loc
        assert "http://" not in loc


def test_sitemap_includes_public_pages_only(app_client):
    res = app_client.get("/sitemap.xml")
    body = res.get_data(as_text=True)
    for path in ("/", "/pricing", "/features", "/how-it-works", "/sms-consent", "/terms", "/privacy", "/refund-policy", "/contact"):
        assert f"https://topairealestatetools.com{path}" in body or (
            path == "/" and "https://topairealestatetools.com/</loc>" in body
        )
    for snippet in PRIVATE_SNIPPETS:
        assert snippet not in body
    assert "/tutorial" not in body
    assert "ai-real-estate-calling-assistant" not in body


def test_sitemap_listed_paths_return_200(app_client):
    for entry in seo.PUBLIC_SITEMAP_ENTRIES:
        res = app_client.get(entry["path"])
        assert res.status_code == 200, entry["path"]


def test_sitemap_no_customer_data(app_client, two_users):
    u1, _ = two_users
    import db

    # Seed a lead so we can prove it never leaks into SEO files.
    db.upsert_lead(u1, "+15551234999", {"name": "Secret Lead XYZ"}, source="sms")
    body = app_client.get("/sitemap.xml").get_data(as_text=True)
    robots = app_client.get("/robots.txt").get_data(as_text=True)
    assert "Secret Lead" not in body
    assert "Secret Lead" not in robots
    assert "+15551234999" not in body
    assert "@example.com" not in body


def test_robots_txt(app_client):
    res = app_client.get("/robots.txt")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    assert "User-agent: *" in body
    assert "Allow: /" in body
    assert "Sitemap: https://topairealestatetools.com/sitemap.xml" in body
    for path in seo.ROBOTS_DISALLOW:
        assert f"Disallow: {path}" in body
    assert "Secret" not in body


def test_seo_routes_do_not_require_login(app_client):
    # No session — must still succeed.
    assert app_client.get("/sitemap.xml").status_code == 200
    assert app_client.get("/robots.txt").status_code == 200


def test_private_pages_have_noindex(app_client, two_users):
    u1, _ = two_users
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    for path in (
        "/dashboard?local_date=2026-07-26&tz_offset_minutes=0",
        "/crm/leads",
        "/tutorial",
        "/app",
    ):
        html = app_client.get(path).get_data(as_text=True)
        assert re.search(r'name=["\']robots["\']\s+content=["\']noindex', html, re.I)


def test_login_register_have_noindex(app_client):
    for path in ("/login", "/register"):
        html = app_client.get(path).get_data(as_text=True)
        assert "noindex" in html.lower()
