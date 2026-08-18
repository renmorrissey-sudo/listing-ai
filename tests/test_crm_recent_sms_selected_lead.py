"""Recent SMS on the SMS Assistant must follow the selected CRM lead."""

import uuid

import db


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["session_version"] = 1


def _lead(user_id, phone, name):
    return db.upsert_lead(user_id, phone, {"name": name, "lead_type": "buyer"}, source="sms")


def _msg(user_id, lead_id, *, body, phone, name, status="sent", direction="outbound"):
    return db.create_sms_message(
        user_id=user_id,
        persona_id=None,
        provider="telnyx",
        data={
            "lead_name": name,
            "phone_number": phone,
            "message_body": body,
        },
        status=status,
        lead_id=lead_id,
        direction=direction,
    )


def _bodies(res):
    return [m["message_body"] for m in res.get_json()["messages"]]


def test_recent_sms_filters_by_selected_lead_id(app_client, two_users):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    ryan_phone = "+13038703106"
    sarah_phone = "+13038703107"
    ryan_id = _lead(u1, ryan_phone, "Ryan Serhant")
    sarah_id = _lead(u1, sarah_phone, "Sarah Johnson")
    _msg(u1, ryan_id, body="Ryan outbound", phone=ryan_phone, name="Ryan Serhant")
    _msg(
        u1,
        sarah_id,
        body="Sarah inbound",
        phone=sarah_phone,
        name="Sarah Johnson",
        status="received",
        direction="inbound",
    )
    _msg(u1, sarah_id, body="Sarah outbound", phone=sarah_phone, name="Sarah Johnson")

    ryan = app_client.get(f"/sms/messages?lead_id={ryan_id}")
    assert ryan.status_code == 200
    assert ryan.get_json()["filtered_lead_id"] == ryan_id
    assert _bodies(ryan) == ["Ryan outbound"]
    assert all(m["lead_id"] == ryan_id for m in ryan.get_json()["messages"])
    assert all("Sarah" not in (m["lead_name"] or "") for m in ryan.get_json()["messages"])

    sarah = app_client.get(f"/sms/messages?lead_id={sarah_id}")
    assert sarah.status_code == 200
    assert sarah.get_json()["filtered_lead_id"] == sarah_id
    assert set(_bodies(sarah)) == {"Sarah inbound", "Sarah outbound"}
    assert all(m["lead_id"] == sarah_id for m in sarah.get_json()["messages"])
    assert "Ryan outbound" not in _bodies(sarah)

    global_list = app_client.get("/sms/messages")
    assert global_list.status_code == 200
    assert global_list.get_json()["filtered_lead_id"] is None
    assert "Ryan outbound" in _bodies(global_list)
    assert "Sarah outbound" in _bodies(global_list)


def test_recent_sms_empty_state_when_lead_has_no_history(app_client, two_users):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    ryan_id = _lead(u1, "+13038703106", "Ryan Serhant")
    sarah_id = _lead(u1, "+13038703107", "Sarah Johnson")
    _msg(
        u1,
        sarah_id,
        body="Sarah only",
        phone="+13038703107",
        name="Sarah Johnson",
        status="received",
        direction="inbound",
    )

    ryan = app_client.get(f"/sms/messages?lead_id={ryan_id}")
    assert ryan.status_code == 200
    assert ryan.get_json()["messages"] == []
    assert ryan.get_json()["filtered_lead_id"] == ryan_id

    html = app_client.get("/app").get_data(as_text=True)
    assert "No SMS history for this lead yet." in html


def test_switching_leads_does_not_leave_stale_recent_sms(app_client, two_users):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    ryan_id = _lead(u1, "+13038703106", "Ryan Serhant")
    sarah_id = _lead(u1, "+13038703107", "Sarah Johnson")
    _msg(u1, ryan_id, body="Ryan ping", phone="+13038703106", name="Ryan Serhant")
    _msg(u1, sarah_id, body="Sarah ping", phone="+13038703107", name="Sarah Johnson")

    first = app_client.get(f"/sms/messages?lead_id={ryan_id}")
    second = app_client.get(f"/sms/messages?lead_id={sarah_id}")
    third = app_client.get(f"/sms/messages?lead_id={ryan_id}")
    assert _bodies(first) == ["Ryan ping"]
    assert _bodies(second) == ["Sarah ping"]
    assert _bodies(third) == ["Ryan ping"]
    assert "Sarah ping" not in _bodies(third)

    html = app_client.get("/app").get_data(as_text=True)
    assert "/sms/messages?lead_id=" in html
    assert "selectedSmsLeadId" in html
    assert "openSmsLead" in html
    assert "loadSmsMessages()" in html


def test_two_leads_never_share_recent_sms_by_phone_or_name(app_client, two_users):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    a_id = _lead(u1, "+15551110001", "Alex")
    b_id = _lead(u1, "+15551110002", "Blair")
    _msg(u1, a_id, body="Alex secret", phone="+15551110001", name="Alex")
    _msg(u1, b_id, body="Blair secret", phone="+15551110002", name="Blair")

    a_res = app_client.get(f"/sms/messages?lead_id={a_id}")
    b_res = app_client.get(f"/sms/messages?lead_id={b_id}")
    assert _bodies(a_res) == ["Alex secret"]
    assert _bodies(b_res) == ["Blair secret"]
    assert all(m["lead_id"] == a_id for m in a_res.get_json()["messages"])
    assert all(m["lead_id"] == b_id for m in b_res.get_json()["messages"])


def test_recent_sms_lead_filter_rejects_other_tenant_and_invalid_id(app_client, two_users):
    u1, u2 = two_users
    db.update_user_subscription(u1, "active")
    db.update_user_subscription(u2, "active")
    owner_lead = _lead(u1, f"+1555{uuid.uuid4().hex[:7]}", "Owner")
    _login(app_client, u2)
    missing = app_client.get(f"/sms/messages?lead_id={owner_lead}")
    assert missing.status_code == 404
    bad = app_client.get("/sms/messages?lead_id=not-an-id")
    assert bad.status_code == 400


def test_filtered_recent_sms_still_omits_suggested(app_client, two_users):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    phone = f"+1555{uuid.uuid4().hex[:7]}"
    lead_id = _lead(u1, phone, "Filter Lead")
    _msg(u1, lead_id, body="Visible sent", phone=phone, name="Filter Lead")
    _msg(
        u1,
        lead_id,
        body="Hidden suggested",
        phone=phone,
        name="Filter Lead",
        status="suggested",
        direction="suggested",
    )
    res = app_client.get(f"/sms/messages?lead_id={lead_id}")
    bodies = _bodies(res)
    assert bodies == ["Visible sent"]
    assert all(m["direction"] in ("inbound", "outbound") for m in res.get_json()["messages"])
    thread = app_client.get(f"/sms/leads/{lead_id}/messages")
    assert [m["message_body"] for m in thread.get_json()["messages"]] == ["Visible sent"]
