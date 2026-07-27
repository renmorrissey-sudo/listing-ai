"""Public SEO helpers: sitemap.xml and robots.txt (no auth, no private data)."""

from xml.sax.saxutils import escape as xml_escape

# Canonical production origin — never www.
CANONICAL_ORIGIN = "https://topairealestatetools.com"

# Public, indexable paths that exist and return 200 without login.
# Do not list /tutorial (subscription-gated) or private/CRM routes.
PUBLIC_SITEMAP_ENTRIES = (
    {"path": "/", "changefreq": "weekly", "priority": "1.0"},
    {"path": "/pricing", "changefreq": "monthly", "priority": "0.9"},
    {"path": "/features", "changefreq": "monthly", "priority": "0.8"},
    {"path": "/how-it-works", "changefreq": "monthly", "priority": "0.8"},
    {"path": "/terms", "changefreq": "yearly", "priority": "0.3"},
    {"path": "/privacy", "changefreq": "yearly", "priority": "0.3"},
    {"path": "/refund-policy", "changefreq": "yearly", "priority": "0.3"},
    {"path": "/contact", "changefreq": "yearly", "priority": "0.4"},
)

ROBOTS_DISALLOW = (
    "/dashboard",
    "/crm/",
    "/account/",
    "/billing/",
    "/api/",
    "/webhook/",
    "/recordings/",
    "/transcripts/",
)


def canonical_loc(path: str) -> str:
    path = path if path.startswith("/") else f"/{path}"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return f"{CANONICAL_ORIGIN}{path}"


def build_sitemap_xml() -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for entry in PUBLIC_SITEMAP_ENTRIES:
        loc = xml_escape(canonical_loc(entry["path"]))
        lines.append("  <url>")
        lines.append(f"    <loc>{loc}</loc>")
        if entry.get("changefreq"):
            lines.append(f"    <changefreq>{xml_escape(entry['changefreq'])}</changefreq>")
        if entry.get("priority"):
            lines.append(f"    <priority>{xml_escape(entry['priority'])}</priority>")
        lines.append("  </url>")
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def build_robots_txt() -> str:
    lines = ["User-agent: *", "Allow: /"]
    for path in ROBOTS_DISALLOW:
        lines.append(f"Disallow: {path}")
    lines.append(f"Sitemap: {CANONICAL_ORIGIN}/sitemap.xml")
    return "\n".join(lines) + "\n"
