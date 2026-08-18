"""Creating a lead with an already-used phone must not overwrite CRM identity."""

import crm_db
import db
from lead_service import normalize_phone_e164, upsert_crm_lead


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["session_version"] = 1


def _sms(user_id, lead_id, *, body, status, direction, phone, lead_name):
    return db.create_sms_message(
        user_id=user_id,
        persona_id=None,
        provider="telnyx",
        data={
            "lead_name": lead_name,
            "phone_number": phone,
            "message_body": body,
        },
        status=status,
        lead_id=lead_id,
        direction=direction,
    )


def test_phone_formats_normalize_to_same_identity():
    assert normalize_phone_e164("3038703107") == "+13038703107"
    assert normalize_phone_e164("(303) 870-3107") == "+13038703107"
    assert normalize_phone_e164("+1 303-870-3107") == "+13038703107"
    assert normalize_phone_e164("+13038703107") == "+13038703107"


def test_create_ryan_does_not_overwrite_sarah(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    created = app_client.post(
        "/api/crm/leads",
        json={
            "first_name": "Sarah",
            "last_name": "Johnson",
            "phone": "3038703107",
            "notes": "Open house visitor",
            "lead_type": "buyer",
        },
    )
    assert created.status_code == 201
    sarah_id = created.get_json()["lead_id"]
    db.update_lead_contact_fields(
        sarah_id, u1, property_interest="3-bed downtown", notes="Open house visitor"
    )
    inbound_id = _sms(
        u1,
        sarah_id,
        body="Hi, is the listing still available?",
        status="received",
        direction="inbound",
        phone="+13038703107",
        lead_name="Sarah Johnson",
    )
    outbound_id = _sms(
        u1,
        sarah_id,
        body="Hi Sarah, yes it is — when works for a tour?",
        status="sent",
        direction="outbound",
        phone="+13038703107",
        lead_name="Sarah Johnson",
    )
    suggested_id = _sms(
        u1,
        sarah_id,
        body="Suggested draft only",
        status="suggested",
        direction="suggested",
        phone="+13038703107",
        lead_name="Sarah Johnson",
    )

    duplicate = app_client.post(
        "/api/crm/leads",
        json={"first_name": "Ryan", "last_name": "Serhant", "phone": "+13038703107"},
    )
    assert duplicate.status_code == 409
    data = duplicate.get_json()
    assert data["duplicate"] is True
    assert data["lead_id"] == sarah_id
    assert "Sarah Johnson" in data["error"]
    assert "+1 303-870-3107" in data["error"]
    assert data["url"] == f"/crm/leads/{sarah_id}"

    sarah = db.get_lead(sarah_id, u1)
    assert sarah["name"] == "Sarah Johnson"
    assert sarah["phone_number"] == "+13038703107"
    assert sarah["property_interest"] == "3-bed downtown"
    assert "Open house visitor" in (sarah.get("notes") or "")

    listed = crm_db.filter_leads(u1, limit=1000)
    names = [lead["name"] for lead in listed if lead["phone_number"] == "+13038703107"]
    assert names == ["Sarah Johnson"]
    assert not any(lead["name"] == "Ryan Serhant" for lead in listed)

    visible = db.list_lead_messages(u1, sarah_id, visible_only=True)
    visible_ids = {row["id"] for row in visible}
    assert inbound_id in visible_ids
    assert outbound_id in visible_ids
    assert suggested_id not in visible_ids
    assert all(row["lead_id"] == sarah_id for row in visible)

    all_rows = db.list_lead_messages(u1, sarah_id, visible_only=False)
    assert {row["id"] for row in all_rows} >= {inbound_id, outbound_id, suggested_id}


def test_sms_upsert_matching_phone_does_not_rename_lead(two_users):
    u1, _ = two_users
    sarah_id, created, sarah = upsert_crm_lead(
        u1, "3038703107", {"lead_name": "Sarah Johnson", "lead_type": "buyer"}
    )
    assert created is True
    _sms(
        u1,
        sarah_id,
        body="Thanks",
        status="received",
        direction="inbound",
        phone="+13038703107",
        lead_name="Sarah Johnson",
    )
    same_id, created2, after = upsert_crm_lead(
        u1, "+13038703107", {"lead_name": "Ryan Serhant", "lead_type": "seller"}
    )
    assert created2 is False
    assert same_id == sarah_id
    assert after["name"] == "Sarah Johnson"
    assert after["lead_type"] == "buyer"
    messages = db.list_lead_messages(u1, sarah_id)
    assert messages[0]["lead_id"] == sarah_id
    assert messages[0]["lead_name"] == "Sarah Johnson"


def test_explicit_edit_still_renames_lead(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    created = app_client.post(
        "/api/crm/leads",
        json={"first_name": "Sarah", "last_name": "Johnson", "phone": "3038703108"},
    ).get_json()
    lead_id = created["lead_id"]
    res = app_client.patch(
        f"/api/crm/leads/{lead_id}",
        json={"name": "Sarah J.", "lead_type": "buyer"},
    )
    assert res.status_code == 200
    lead = db.get_lead(lead_id, u1)
    assert lead["name"] == "Sarah J."
    assert lead["lead_type"] == "buyer"


def test_different_phone_creates_new_lead(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    sarah = app_client.post(
        "/api/crm/leads",
        json={"first_name": "Sarah", "last_name": "Johnson", "phone": "3038703107"},
    ).get_json()
    other = app_client.post(
        "/api/crm/leads",
        json={"first_name": "Alex", "last_name": "River", "phone": "7205550199"},
    )
    assert other.status_code == 201
    other_json = other.get_json()
    assert other_json["lead_id"] != sarah["lead_id"]
    assert db.get_lead(other_json["lead_id"], u1)["name"] == "Alex River"


def test_inbound_sms_matches_existing_lead_without_renaming(two_users, monkeypatch):
    from sms_inbound import process_inbound_sms

    u1, _ = two_users
    lead_id, _, lead = upsert_crm_lead(
        u1, "(303) 870-3107", {"lead_name": "Sarah Johnson"}
    )
    found = db.find_lead_by_phone_normalized(u1, "3038703107")
    assert found["id"] == lead_id
    assert found["name"] == "Sarah Johnson"
    later_id, created, later = upsert_crm_lead(
        u1, "+1 303-870-3107", {"lead_name": "Someone Else"}
    )
    assert created is False
    assert later_id == lead_id
    assert later["name"] == "Sarah Johnson"

    monkeypatch.setattr("sms_inbound.destination_allowed", lambda *a, **k: True)
    inbound = process_inbound_sms(
        {
            "from_number": "+13038703107",
            "to_number": "+18885551212",
            "body": "Is the listing still available?",
            "message_sid": "SM-sarah-inbound-keep-identity",
        },
        defer_coach=True,
    )
    assert inbound["ok"] is True
    assert inbound["lead_id"] == lead_id
    assert db.get_lead(lead_id, u1)["name"] == "Sarah Johnson"
    stored = db.get_sms_message(inbound["inbound_id"], u1)
    assert stored["lead_id"] == lead_id
    assert stored["lead_name"] == "Sarah Johnson"


def test_restore_name_from_sms_history_after_overwrite(two_users):
    u1, _ = two_users
    from lead_service import restore_lead_name_from_sms_history

    lead_id, _, _ = upsert_crm_lead(u1, "+13038703107", {"lead_name": "Sarah Johnson"})
    _sms(
        u1,
        lead_id,
        body="Hello",
        status="sent",
        direction="outbound",
        phone="+13038703107",
        lead_name="Sarah Johnson",
    )
    # Simulate the production overwrite on the same row.
    db.update_lead_contact_fields(lead_id, u1, name="Ryan Serhant")
    assert db.get_lead(lead_id, u1)["name"] == "Ryan Serhant"
    restored, err = restore_lead_name_from_sms_history(u1, lead_id)
    assert err is None
    assert restored["name"] == "Sarah Johnson"
    messages = db.list_lead_messages(u1, lead_id)
    assert len(messages) == 1
    assert messages[0]["lead_id"] == lead_id
    assert messages[0]["message_body"] == "Hello"


def test_restore_name_api_and_suggested_filter_intact(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    lead_id, _, _ = upsert_crm_lead(u1, "+13038703109", {"lead_name": "Sarah Johnson"})
    _sms(
        u1,
        lead_id,
        body="Visible outbound",
        status="sent",
        direction="outbound",
        phone="+13038703109",
        lead_name="Sarah Johnson",
    )
    _sms(
        u1,
        lead_id,
        body="Hidden suggestion",
        status="suggested",
        direction="suggested",
        phone="+13038703109",
        lead_name="Sarah Johnson",
    )
    db.update_lead_contact_fields(lead_id, u1, name="Ryan Serhant")
    res = app_client.post(f"/api/crm/leads/{lead_id}/restore-name-from-history")
    assert res.status_code == 200
    assert res.get_json()["lead"]["name"] == "Sarah Johnson"
    visible = db.list_lead_messages(u1, lead_id, visible_only=True)
    assert [m["message_body"] for m in visible] == ["Visible outbound"]


def test_new_lead_drawer_html_has_no_create_anyway(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    html = app_client.get("/crm/leads").get_data(as_text=True)
    assert "Create anyway" not in html
    assert "Open Existing Lead" in html
    assert "newLeadIsDuplicate" in html
