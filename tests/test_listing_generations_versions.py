"""Same-address version history: multiple generations never overwrite each
other, and opening a prior version restores its exact frozen snapshot."""

import time

import db
import listing_generations_db as listing_db


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["session_version"] = 1


def _make(user_id, address, listing_text):
    return listing_db.create_generation(
        user_id, display_address=address, output_snapshot={"listing": listing_text}
    )


def test_same_address_can_have_multiple_versions(two_users):
    u1, _ = two_users
    v1 = _make(u1, "123 Main Street", "First draft")
    time.sleep(0.01)
    v2 = _make(u1, "123 Main Street", "Second draft")
    time.sleep(0.01)
    v3 = _make(u1, "123 Main Street, Denver CO", "Third draft")

    versions = listing_db.list_versions_for_address(u1, v1["normalized_address"])
    assert len(versions) == 3
    ids = {v["id"] for v in versions}
    assert ids == {v1["id"], v2["id"], v3["id"]}
    # Newest first.
    assert versions[0]["id"] == v3["id"]


def test_opening_prior_version_returns_exact_historical_output(two_users):
    u1, _ = two_users
    v1 = _make(u1, "456 Oak Avenue", "Original content, not to be changed")
    _make(u1, "456 Oak Avenue", "Newer content")

    reopened = listing_db.get_by_id(u1, v1["id"])
    assert reopened["output_snapshot"]["listing"] == "Original content, not to be changed"


def test_regenerating_same_address_does_not_destroy_prior_version(two_users):
    u1, _ = two_users
    v1 = _make(u1, "789 Elm St", "Version one")
    v2 = _make(u1, "789 Elm St", "Version two")

    assert listing_db.get_by_id(u1, v1["id"]) is not None
    assert listing_db.get_by_id(u1, v2["id"]) is not None
    assert listing_db.get_by_id(u1, v1["id"])["output_snapshot"]["listing"] == "Version one"


def test_listings_get_one_route_includes_version_list(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    db.update_user_subscription(u1, "active")
    v1 = _make(u1, "1 Version Rd", "Old")
    v2 = _make(u1, "1 Version Rd", "New")

    res = app_client.get(f"/listings/{v2['id']}")
    assert res.status_code == 200
    data = res.get_json()
    assert data["generation"]["output_snapshot"]["listing"] == "New"
    version_ids = {v["id"] for v in data["versions"]}
    assert version_ids == {v1["id"], v2["id"]}


def test_listings_get_one_route_is_tenant_scoped(app_client, two_users):
    u1, u2 = two_users
    _login(app_client, u2)
    db.update_user_subscription(u2, "active")
    v1 = _make(u1, "2 Scoped Rd", "Tenant 1 only")

    res = app_client.get(f"/listings/{v1['id']}")
    assert res.status_code == 404
