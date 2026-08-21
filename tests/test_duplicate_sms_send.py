"""Prevent duplicate Telnyx sends after AI auto-reply; collapse history copies."""

from unittest.mock import patch
import uuid

import config
import db
import sms_ai_agent
import sms_coach
from sms_providers.telnyx import TelnyxSMSProvider

from tests.test_telnyx_inbound_ai_workflow import (
    _analysis,
    _inbound_payload,
    _lead,
    _setup_tenant,
    _unique_e164,
)


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["session_version"] = 1


def _phone():
    return f"+1555{uuid.uuid4().hex[:7]}"


def txwh_inbound(contact, account, text="Tell me more"):
    import telnyx_webhooks as txwh

    result, status = txwh.handle_messaging_webhook(
        _inbound_payload(contact, account, text=text)
    )
    assert status == 200
    return result, status


def test_ai_auto_reply_sends_once_and_consumes_suggestion(two_users, monkeypatch):
    u1, _ = two_users
    account = _setup_tenant(u1, monkeypatch)
    contact = _unique_e164("651")
    lead_id, _ = _lead(u1, contact)
    result, _ = txwh_inbound(contact, account)

    inbound_id = result["message_id"]
    with patch.object(sms_coach, "analyze_inbound_reply", return_value=_analysis()), \
         patch.object(TelnyxSMSProvider, "send_message") as send:
        send.return_value = {
            "provider_message_id": f"tx-out-{uuid.uuid4().hex[:8]}",
            "status": "queued",
        }
        outcome = sms_ai_agent.process_inbound_ai(
            u1, lead_id, inbound_id, "Tell me more", account
        )

    assert outcome["replied"] is True
    assert send.call_count == 1

    stored = db.list_lead_messages(u1, lead_id)
    suggested = [m for m in stored if m["direction"] == "suggested"]
    assert not suggested
    outbound = [
        m for m in stored
        if m["direction"] == "outbound" and m.get("reply_to_message_id") == inbound_id
    ]
    assert len(outbound) == 1

    with db.get_db() as conn:
        insight = conn.execute(
            "SELECT * FROM lead_insights WHERE lead_id = ? AND user_id = ?",
            (lead_id, u1),
        ).fetchone()
    assert insight["status"] == "sent"
    assert all(i["lead_id"] != lead_id for i in db.list_pending_insights(u1))


def test_approve_after_auto_reply_does_not_send_again(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    account = _setup_tenant(u1, monkeypatch)
    contact = _unique_e164("652")
    lead_id, _ = _lead(u1, contact)
    result, _ = txwh_inbound(contact, account)
    inbound_id = result["message_id"]

    with patch.object(sms_coach, "analyze_inbound_reply", return_value=_analysis()), \
         patch.object(TelnyxSMSProvider, "send_message") as send:
        send.return_value = {"provider_message_id": "tx-ai-1", "status": "queued"}
        sms_ai_agent.process_inbound_ai(u1, lead_id, inbound_id, "Tell me more", account)

    with db.get_db() as conn:
        insight = dict(
            conn.execute(
                "SELECT * FROM lead_insights WHERE lead_id = ? AND user_id = ?",
                (lead_id, u1),
            ).fetchone()
        )

    with patch("sms_outbound.send_authorized_sms") as send_auth:
        first = app_client.post(
            f"/sms/suggestions/{insight['id']}/send",
            json={"compliance_confirmed": True},
        )
        second = app_client.post(
            f"/sms/suggestions/{insight['id']}/send",
            json={"compliance_confirmed": True},
        )
    assert first.status_code == 200
    assert first.get_json()["already_sent"] is True
    assert second.status_code == 200
    assert second.get_json()["already_sent"] is True
    assert send_auth.call_count == 0

    outbound = [
        m
        for m in db.list_lead_messages(u1, lead_id)
        if m["direction"] == "outbound"
        and m["status"] not in {"suggested", "dismissed", "draft"}
    ]
    assert len(outbound) == 1


def test_manual_suggestion_approval_still_sends_when_no_ai_outbound(
    app_client, two_users, monkeypatch
):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    phone = _phone()
    lead_id = db.upsert_lead(u1, phone, {"name": "Manual Lead"}, source="sms")
    inbound_id = db.create_sms_message(
        user_id=u1,
        persona_id=None,
        provider="telnyx",
        data={"lead_name": "Manual Lead", "phone_number": phone, "message_body": "Hello?"},
        status="received",
        lead_id=lead_id,
        direction="inbound",
    )
    suggested_id = db.create_sms_message(
        user_id=u1,
        persona_id=None,
        provider="telnyx",
        data={
            "lead_name": "Manual Lead",
            "phone_number": phone,
            "message_body": "Happy to help this weekend.",
        },
        status="suggested",
        lead_id=lead_id,
        direction="suggested",
    )
    insight_id = db.create_lead_insight(
        lead_id,
        u1,
        inbound_id,
        {
            "summary": "Lead asked a question",
            "intent": "question",
            "next_best_step": "Reply",
            "recommended_action": "Reply",
            "suggested_reply": "Happy to help this weekend.",
            "home_value_pitch": None,
            "confidence_score": 0.9,
            "requires_manual_review": False,
            "escalation_topics": [],
            "raw_json": None,
        },
        suggested_message_id=suggested_id,
    )

    with patch("sms_outbound.send_authorized_sms") as send_auth:
        send_auth.return_value = (
            {"id": suggested_id, "status": "queued", "lead_id": lead_id},
            None,
            201,
        )
        res = app_client.post(
            f"/sms/suggestions/{insight_id}/send",
            json={
                "compliance_confirmed": True,
                "message_body": "Happy to help this weekend.",
            },
        )
    assert res.status_code == 201
    assert send_auth.call_count == 1
    assert res.get_json().get("already_sent") is not True


def test_visible_history_collapses_ai_duplicate_but_keeps_rows(two_users):
    u1, _ = two_users
    phone = _phone()
    lead_id = db.upsert_lead(u1, phone, {"name": "Dup Lead"}, source="sms")
    inbound_id = db.create_sms_message(
        user_id=u1,
        persona_id=None,
        provider="telnyx",
        data={"lead_name": "Dup Lead", "phone_number": phone, "message_body": "Inbound hi"},
        status="received",
        lead_id=lead_id,
        direction="inbound",
    )
    ai_id = db.create_ai_reply_message(
        u1,
        lead_id,
        inbound_id,
        phone_number=phone,
        message_body="Happy to help this weekend.",
        provider="telnyx",
        lead_name="Dup Lead",
    )
    db.update_sms_message_send_result(ai_id, provider_message_id="tx-ai-dup", status="delivered")
    copy_id = db.create_sms_message(
        user_id=u1,
        persona_id=None,
        provider="telnyx",
        data={
            "lead_name": "Dup Lead",
            "phone_number": phone,
            "message_body": "Happy to help this weekend.",
        },
        status="delivered",
        lead_id=lead_id,
        direction="outbound",
    )
    db.update_sms_message_send_result(copy_id, provider_message_id="tx-copy-dup", status="delivered")

    stored = db.list_lead_messages(u1, lead_id)
    stored_ids = {m["id"] for m in stored}
    assert ai_id in stored_ids and copy_id in stored_ids

    visible = db.list_lead_messages(u1, lead_id, visible_only=True)
    visible_out = [m for m in visible if m["direction"] == "outbound"]
    assert len(visible_out) == 1
    assert visible_out[0]["id"] == ai_id
    assert [m["message_body"] for m in visible if m["direction"] == "inbound"] == ["Inbound hi"]


def test_legitimate_duplicate_text_not_collapsed(two_users):
    u1, _ = two_users
    phone = _phone()
    lead_id = db.upsert_lead(u1, phone, {"name": "Manual Dup"}, source="sms")
    for _ in range(2):
        mid = db.create_sms_message(
            user_id=u1,
            persona_id=None,
            provider="telnyx",
            data={
                "lead_name": "Manual Dup",
                "phone_number": phone,
                "message_body": "Just circling back on the listing.",
            },
            status="sent",
            lead_id=lead_id,
            direction="outbound",
        )
        db.update_sms_message_send_result(
            mid, provider_message_id=f"tx-man-{mid}", status="sent"
        )

    visible = db.list_lead_messages(u1, lead_id, visible_only=True)
    assert len(visible) == 2
    assert all(m["message_body"] == "Just circling back on the listing." for m in visible)


def test_suggested_filter_still_hides_drafts(two_users):
    u1, _ = two_users
    phone = _phone()
    lead_id = db.upsert_lead(u1, phone, {"name": "Filter Lead"}, source="sms")
    db.create_sms_message(
        user_id=u1,
        persona_id=None,
        provider="telnyx",
        data={"lead_name": "Filter Lead", "phone_number": phone, "message_body": "Lead inbound"},
        status="received",
        lead_id=lead_id,
        direction="inbound",
    )
    db.create_sms_message(
        user_id=u1,
        persona_id=None,
        provider="telnyx",
        data={"lead_name": "Filter Lead", "phone_number": phone, "message_body": "Hidden suggested"},
        status="suggested",
        lead_id=lead_id,
        direction="suggested",
    )
    db.create_sms_message(
        user_id=u1,
        persona_id=None,
        provider="telnyx",
        data={"lead_name": "Filter Lead", "phone_number": phone, "message_body": "Real outbound"},
        status="sent",
        lead_id=lead_id,
        direction="outbound",
    )
    visible = db.list_lead_messages(u1, lead_id, visible_only=True)
    bodies = [m["message_body"] for m in visible]
    assert bodies == ["Lead inbound", "Real outbound"]
    stored_bodies = [m["message_body"] for m in db.list_lead_messages(u1, lead_id)]
    assert "Hidden suggested" in stored_bodies
