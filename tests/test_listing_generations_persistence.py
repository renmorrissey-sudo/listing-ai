"""Auto-save behavior for the Listing Generator: /generate persistence,
non-destructive failure handling, save-retry, and tenant scoping."""

from unittest.mock import patch

import db
import listing_generations_db as listing_db


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["session_version"] = 1


def _fake_claude_message(listing="A great home.", social="1. [INSTAGRAM] hi", email="Subject: Hi\n\nBody."):
    raw = f"---LISTING DESCRIPTION---\n{listing}\n---SOCIAL POSTS---\n{social}\n---PROSPECT EMAIL---\n{email}"
    block = type("Block", (), {"text": raw})()
    return type("Message", (), {"content": [block]})()


VALID_PAYLOAD = {
    "address": "123 Main Street, Denver, CO",
    "price": "485,000",
    "beds": "4",
    "baths": "2.5",
    "sqft": "2,200",
    "year_built": "",
    "garage": "None",
    "pool": "No",
    "features": "",
    "neighborhood": "",
}


def test_successful_generation_automatically_persists(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    db.update_user_subscription(u1, "active")
    with patch("app.client.messages.create", return_value=_fake_claude_message()):
        res = app_client.post("/generate", json=VALID_PAYLOAD)
    assert res.status_code == 200
    data = res.get_json()
    assert data.get("generation_id")
    assert "save_warning" not in data

    saved = listing_db.get_by_id(u1, data["generation_id"])
    assert saved is not None
    assert saved["display_address"] == VALID_PAYLOAD["address"]
    assert saved["output_snapshot"]["listing"] == "A great home."
    assert saved["output_snapshot"]["social"] == "1. [INSTAGRAM] hi"
    assert saved["output_snapshot"]["email"] == "Subject: Hi\n\nBody."
    assert saved["social_content"]
    assert data["social"] == "1. [INSTAGRAM] hi"
    assert data["email"] == "Subject: Hi\n\nBody."
    assert data["social_content"] == saved["social_content"]


def test_failed_generation_is_not_falsely_stored_as_successful(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    db.update_user_subscription(u1, "active")
    with patch("app.client.messages.create", side_effect=RuntimeError("boom")):
        res = app_client.post("/generate", json=VALID_PAYLOAD)
    assert res.status_code == 500
    assert listing_db.list_recent(u1) == []


def test_listing_belongs_to_correct_tenant(app_client, two_users):
    u1, u2 = two_users
    _login(app_client, u1)
    db.update_user_subscription(u1, "active")
    with patch("app.client.messages.create", return_value=_fake_claude_message()):
        res = app_client.post("/generate", json=VALID_PAYLOAD)
    gen_id = res.get_json()["generation_id"]

    assert listing_db.get_by_id(u1, gen_id) is not None
    assert listing_db.get_by_id(u2, gen_id) is None
    assert listing_db.list_recent(u2) == []


def test_persistence_failure_returns_content_with_warning_and_retry_payload(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    db.update_user_subscription(u1, "active")
    with patch("app.client.messages.create", return_value=_fake_claude_message()), patch(
        "listing_generations_db.create_generation", side_effect=RuntimeError("db down")
    ):
        res = app_client.post("/generate", json=VALID_PAYLOAD)
    assert res.status_code == 200
    data = res.get_json()
    assert data["listing"] == "A great home."
    assert data["social"] == "1. [INSTAGRAM] hi"
    assert data["email"] == "Subject: Hi\n\nBody."
    assert "generation_id" not in data
    assert data.get("save_warning")
    assert data["save_retry_payload"]["address"] == VALID_PAYLOAD["address"]
    assert listing_db.list_recent(u1) == []


def test_save_retry_persists_without_new_ai_call(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    db.update_user_subscription(u1, "active")
    payload = {
        "address": "999 Retry Lane",
        "input_snapshot": VALID_PAYLOAD,
        "output_snapshot": {"listing": "L", "social": "S", "email": "E"},
        "social_content": {"baseCaption": "S"},
    }
    res = app_client.post("/listings/save-retry", json=payload)
    assert res.status_code == 200
    gen_id = res.get_json()["generation_id"]
    saved = listing_db.get_by_id(u1, gen_id)
    assert saved["display_address"] == "999 Retry Lane"
    assert saved["output_snapshot"]["listing"] == "L"
    assert saved["output_snapshot"]["social"] == "S"
    assert saved["output_snapshot"]["email"] == "E"


def test_save_retry_missing_content_is_rejected(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    db.update_user_subscription(u1, "active")
    res = app_client.post("/listings/save-retry", json={"address": "1 Nowhere"})
    assert res.status_code == 400
