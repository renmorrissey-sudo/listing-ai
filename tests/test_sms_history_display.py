"""Visible SMS conversation history excludes Suggested AI drafts.

list_lead_messages() still returns suggested rows by default so coaching,
webhooks, and approval flows are unchanged. Display/history retrieval uses
visible_only=True (or the helper) so existing stored drafts disappear from
the thread, Recent SMS, CRM lead page, and dashboard.
"""

import uuid

import db


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["session_version"] = 1


def _phone():
    return f"+1555{uuid.uuid4().hex[:7]}"


def _lead(user_id, phone=None, name="History Lead"):
    return db.upsert_lead(
        user_id,
        phone or _phone(),
        {"name": name, "lead_type": "buyer"},
        source="sms",
    )


def _msg(user_id, lead_id, *, body, status, direction, phone):
    return db.create_sms_message(
        user_id=user_id,
        persona_id=None,
        provider="telnyx",
        data={
            "lead_name": "History Lead",
            "phone_number": phone,
            "message_body": body,
        },
        status=status,
        lead_id=lead_id,
        direction=direction,
    )


def test_helper_keeps_real_inbound_and_sent_outbound_only():
    assert db.is_visible_conversation_sms(
        {"direction": "inbound", "status": "received"}
    )
    for status in ("sent", "delivered", "queued", "sending", "failed"):
        assert db.is_visible_conversation_sms(
            {"direction": "outbound", "status": status}
        ), status
    for status in ("suggested", "dismissed", "cancelled", "draft", "scheduled"):
        assert not db.is_visible_conversation_sms(
            {"direction": "outbound", "status": status}
        ), status
    assert not db.is_visible_conversation_sms(
        {"direction": "suggested", "status": "suggested"}
    )
    assert not db.is_visible_conversation_sms(
        {"direction": "suggested", "status": "cancelled"}
    )


def test_default_list_lead_messages_still_includes_suggested(two_users):
    u1, _ = two_users
    phone = _phone()
    lead_id = _lead(u1, phone)
    _msg(u1, lead_id, body="Inbound hello", status="received", direction="inbound", phone=phone)
    _msg(
        u1,
        lead_id,
        body="AI draft pending approval",
        status="suggested",
        direction="suggested",
        phone=phone,
    )
    _msg(u1, lead_id, body="Actually sent", status="sent", direction="outbound", phone=phone)

    stored = db.list_lead_messages(u1, lead_id)
    bodies = [m["message_body"] for m in stored]
    assert "Inbound hello" in bodies
    assert "AI draft pending approval" in bodies
    assert "Actually sent" in bodies


def test_visible_history_hides_suggested_keeps_real_messages(two_users):
    u1, _ = two_users
    phone = _phone()
    lead_id = _lead(u1, phone)
    _msg(u1, lead_id, body="Lead inbound", status="received", direction="inbound", phone=phone)
    _msg(
        u1,
        lead_id,
        body="Suggested draft",
        status="suggested",
        direction="suggested",
        phone=phone,
    )
    _msg(
        u1,
        lead_id,
        body="Dismissed draft",
        status="dismissed",
        direction="suggested",
        phone=phone,
    )
    _msg(
        u1,
        lead_id,
        body="Cancelled draft",
        status="cancelled",
        direction="suggested",
        phone=phone,
    )
    _msg(u1, lead_id, body="Unsent compose draft", status="draft", direction="outbound", phone=phone)
    _msg(u1, lead_id, body="Sent outbound", status="sent", direction="outbound", phone=phone)
    _msg(u1, lead_id, body="Delivered outbound", status="delivered", direction="outbound", phone=phone)
    _msg(u1, lead_id, body="Failed outbound", status="failed", direction="outbound", phone=phone)

    visible = db.list_lead_messages(u1, lead_id, visible_only=True)
    bodies = [m["message_body"] for m in visible]
    assert bodies == [
        "Lead inbound",
        "Sent outbound",
        "Delivered outbound",
        "Failed outbound",
    ]
    assert all(m["direction"] in ("inbound", "outbound") for m in visible)
    stored = db.list_lead_messages(u1, lead_id)
    assert "Suggested draft" in [m["message_body"] for m in stored]


def test_sms_thread_api_omits_suggested_on_reload(app_client, two_users):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    phone = _phone()
    lead_id = _lead(u1, phone)
    _msg(u1, lead_id, body="Historical inbound", status="received", direction="inbound", phone=phone)
    _msg(
        u1,
        lead_id,
        body="Historical suggested reply",
        status="suggested",
        direction="suggested",
        phone=phone,
    )
    _msg(u1, lead_id, body="Historical outbound", status="sent", direction="outbound", phone=phone)

    first = app_client.get(f"/sms/leads/{lead_id}/messages")
    assert first.status_code == 200
    bodies = [m["message_body"] for m in first.get_json()["messages"]]
    assert bodies == ["Historical inbound", "Historical outbound"]
    assert "Historical suggested reply" not in bodies

    reload = app_client.get(f"/sms/leads/{lead_id}/messages")
    assert [m["message_body"] for m in reload.get_json()["messages"]] == bodies


def test_recent_sms_api_omits_suggested(app_client, two_users):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    phone = _phone()
    lead_id = _lead(u1, phone)
    _msg(u1, lead_id, body="Visible sent", status="sent", direction="outbound", phone=phone)
    _msg(
        u1,
        lead_id,
        body="Hidden suggested",
        status="suggested",
        direction="suggested",
        phone=phone,
    )

    res = app_client.get("/sms/messages")
    assert res.status_code == 200
    bodies = [m["message_body"] for m in res.get_json()["messages"]]
    assert "Visible sent" in bodies
    assert "Hidden suggested" not in bodies
    assert all(m["direction"] in ("inbound", "outbound") for m in res.get_json()["messages"])


def test_crm_lead_page_reload_omits_suggested_body(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    phone = _phone()
    lead_id = _lead(u1, phone)
    _msg(u1, lead_id, body="CRM inbound body", status="received", direction="inbound", phone=phone)
    _msg(
        u1,
        lead_id,
        body="CRM suggested body UNIQUE_SUGGESTED_TOKEN",
        status="suggested",
        direction="suggested",
        phone=phone,
    )
    _msg(u1, lead_id, body="CRM outbound body", status="sent", direction="outbound", phone=phone)

    html = app_client.get(f"/crm/leads/{lead_id}").get_data(as_text=True)
    assert "CRM inbound body" in html
    assert "CRM outbound body" in html
    assert "UNIQUE_SUGGESTED_TOKEN" not in html

    html2 = app_client.get(f"/crm/leads/{lead_id}").get_data(as_text=True)
    assert "CRM inbound body" in html2
    assert "CRM outbound body" in html2
    assert "UNIQUE_SUGGESTED_TOKEN" not in html2


def test_dashboard_recent_sms_omits_suggested(two_users):
    u1, _ = two_users
    phone = _phone()
    lead_id = _lead(u1, phone)
    _msg(u1, lead_id, body="Dash sent", status="sent", direction="outbound", phone=phone)
    _msg(
        u1,
        lead_id,
        body="Dash suggested UNIQUE_DASH_SUGGESTED",
        status="suggested",
        direction="suggested",
        phone=phone,
    )
    metrics = db.get_dashboard_metrics(u1)
    bodies = [m["message_body"] for m in metrics["recent_sms"]]
    assert "Dash sent" in bodies
    assert "UNIQUE_DASH_SUGGESTED" not in bodies
