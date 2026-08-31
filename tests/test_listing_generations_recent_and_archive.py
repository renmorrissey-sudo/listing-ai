"""Recent-20 cap/order, Archive search/pagination, 60-day retention window."""

from datetime import datetime, timedelta, timezone

import config
import db
import listing_generations_db as listing_db


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["session_version"] = 1


def _make(user_id, address, **kwargs):
    output_snapshot = kwargs.pop("output_snapshot", {"listing": address})
    return listing_db.create_generation(
        user_id, display_address=address, output_snapshot=output_snapshot, **kwargs
    )


def _age_row(user_id, generation_id, days_old):
    created = datetime.now(timezone.utc) - timedelta(days=days_old)
    expires = created + timedelta(days=config.LISTING_GENERATION_RETENTION_DAYS)
    with db.get_db() as conn:
        conn.execute(
            "UPDATE listing_generations SET created_at = ?, expires_at = ? WHERE id = ? AND user_id = ?",
            (created.isoformat(), expires.isoformat(), generation_id, user_id),
        )


def test_recent_20_query_returns_only_latest_20(two_users):
    u1, _ = two_users
    for i in range(25):
        _make(u1, f"{i} Test St")
    recent = listing_db.list_recent(u1, limit=20)
    assert len(recent) == 20
    # Newest first.
    assert recent[0]["display_address"] == "24 Test St"
    assert recent[-1]["display_address"] == "5 Test St"


def test_archive_returns_retained_listings_within_60_days(two_users):
    u1, _ = two_users
    fresh = _make(u1, "1 Fresh St")
    old_but_retained = _make(u1, "2 Old St")
    _age_row(u1, old_but_retained["id"], days_old=59)

    result = listing_db.search_archive(u1)
    ids = {item["id"] for item in result["items"]}
    assert fresh["id"] in ids
    assert old_but_retained["id"] in ids
    assert result["total"] == 2


def test_61_day_old_listing_is_inaccessible(two_users):
    u1, _ = two_users
    expired = _make(u1, "3 Expired St")
    _age_row(u1, expired["id"], days_old=61)

    assert listing_db.get_by_id(u1, expired["id"]) is None
    assert expired["id"] not in {i["id"] for i in listing_db.list_recent(u1)}
    archive = listing_db.search_archive(u1)
    assert expired["id"] not in {i["id"] for i in archive["items"]}


def test_cleanup_removes_expired_listing_generation_data(two_users):
    u1, _ = two_users
    keep = _make(u1, "4 Keep St")
    expired = _make(u1, "5 Gone St")
    _age_row(u1, expired["id"], days_old=61)

    deleted = listing_db.cleanup_expired()
    assert deleted >= 1

    with db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM listing_generations WHERE id = ?", (expired["id"],)
        ).fetchone()
    assert row is None
    assert listing_db.get_by_id(u1, keep["id"]) is not None


def test_cleanup_cascades_social_publications(two_users, monkeypatch):
    import config
    from cryptography.fernet import Fernet

    import social_connections_db as social_db

    monkeypatch.setattr(config, "SOCIAL_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

    u1, _ = two_users
    expired = _make(u1, "6 Cascade St")
    _age_row(u1, expired["id"], days_old=100)
    conn_row = social_db.upsert_connection(
        u1, "linkedin", external_account_id="urn:li:person:x", access_token="tok"
    )
    social_db.create_publication(u1, expired["id"], "linkedin", conn_row["id"], "op1:linkedin")

    listing_db.cleanup_expired()

    with db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM social_publications WHERE listing_generation_id = ?",
            (expired["id"],),
        ).fetchone()
    assert row is None


def test_address_search_matches_normalized_variants(two_users):
    u1, _ = two_users
    _make(u1, "12015 Wandsworth Dr")
    result = listing_db.search_archive(u1, query="12015 Wandsworth Drive")
    assert result["total"] == 1


def test_archive_pagination(two_users):
    u1, _ = two_users
    for i in range(5):
        _make(u1, f"{i} Page St")
    page1 = listing_db.search_archive(u1, page=1, page_size=2)
    page2 = listing_db.search_archive(u1, page=2, page_size=2)
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 2
    assert page1["items"][0]["id"] != page2["items"][0]["id"]
    assert page1["total"] == 5


def test_tenant_a_cannot_retrieve_tenant_b_listings(two_users):
    u1, u2 = two_users
    gen = _make(u1, "7 Private St")
    assert listing_db.get_by_id(u2, gen["id"]) is None
    assert listing_db.list_recent(u2) == []
    assert listing_db.search_archive(u2)["total"] == 0


def test_archive_route_requires_login(app_client):
    res = app_client.get("/listings/archive/search")
    assert res.status_code == 401


def test_archive_route_returns_json_for_logged_in_user(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    db.update_user_subscription(u1, "active")
    _make(u1, "8 Route St")
    res = app_client.get("/listings/archive/search?q=Route")
    assert res.status_code == 200
    data = res.get_json()
    assert data["total"] == 1


def test_address_archive_page_shows_all_versions_and_delete_buttons(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    db.update_user_subscription(u1, "active")
    first = _make(
        u1,
        "12015 Wandsworth Dr",
        output_snapshot={
            "listing": "First listing version",
            "social": "First social version",
            "email": "First email version",
        },
    )
    second = _make(
        u1,
        "12015 Wandsworth Drive, Tampa FL",
        output_snapshot={
            "listing": "Second listing version",
            "social": "Second social version",
            "email": "Second email version",
        },
    )

    archive = app_client.get("/listings/archive").get_data(as_text=True)
    assert "/listings/archive/address/" in archive

    res = app_client.get(f"/listings/archive/address/{first['normalized_address']}")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "First listing version" in html
    assert "First social version" in html
    assert "First email version" in html
    assert "Second listing version" in html
    assert "Second social version" in html
    assert "Second email version" in html
    assert "Listing" in html
    assert "Social" in html
    assert "Email" in html
    assert f'data-listing-id="{first["id"]}"' in html
    assert f'data-listing-id="{second["id"]}"' in html
    assert "Delete" in html


def test_delete_listing_removes_only_requested_tenant_listing(app_client, two_users):
    u1, u2 = two_users
    _login(app_client, u1)
    db.update_user_subscription(u1, "active")
    owned = _make(u1, "1 Delete St")
    keep = _make(u1, "1 Delete St", output_snapshot={"listing": "Keep me"})
    other = _make(u2, "1 Delete St")

    missing = app_client.delete(f"/listings/{other['id']}")
    assert missing.status_code == 404
    assert listing_db.get_by_id(u2, other["id"]) is not None

    res = app_client.delete(f"/listings/{owned['id']}")
    assert res.status_code == 200
    assert res.get_json()["ok"] is True
    assert listing_db.get_by_id(u1, owned["id"]) is None
    assert listing_db.get_by_id(u1, keep["id"]) is not None
