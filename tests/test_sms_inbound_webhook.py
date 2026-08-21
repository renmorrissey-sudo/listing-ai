"""Inbound Twilio SMS webhook: signature, matching, compliance, idempotency, coach draft."""

from unittest.mock import patch

import config
import crm_db
import db
from twilio.request_validator import RequestValidator

TWILIO_TOKEN = "test_twilio_auth_token_inbound"
TWILIO_TO = "+18888210810"
SENDER = "+15557654321"
MSID = "MGaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _sign(url, params, token=TWILIO_TOKEN):
    return RequestValidator(token).compute_signature(url, params)


def _seed_lead(user_id, phone=SENDER, name="Inbound Lead"):
    lead_id = db.upsert_lead(
        user_id,
        phone,
        {"name": name, "lead_type": "buyer"},
        source="sms",
    )
    # Prior outbound so find_sms_user_by_phone / seed path is realistic.
    db.create_sms_message(
        user_id=user_id,
        persona_id=None,
        provider="twilio",
        data={
            "lead_name": name,
            "phone_number": phone,
            "message_body": "Hi — interested in a showing?",
            "lead_type": "buyer",
        },
        status="sent",
        lead_id=lead_id,
        direction="outbound",
        consent_status="confirmed",
        opt_out_status="active",
    )
    return lead_id


def _post_inbound(
    client,
    *,
    body="Interested this weekend",
    message_sid="SMinbound0001",
    from_number=SENDER,
    to_number=TWILIO_TO,
    path="/webhooks/twilio/sms",
    extra=None,
    token=TWILIO_TOKEN,
    app_url="https://topairealestatetools.com",
    twilio_phone=TWILIO_TO,
    bad_signature=False,
):
    params = {
        "From": from_number,
        "To": to_number,
        "Body": body,
        "MessageSid": message_sid,
        "SmsStatus": "received",
        "MessagingServiceSid": MSID,
    }
    if extra:
        params.update(extra)

    url = f"{app_url.rstrip('/')}{path}"
    signature = _sign(url, params, token=token)
    if bad_signature:
        signature = "invalid" + signature[7:]

    with patch.object(config, "TWILIO_AUTH_TOKEN", token), \
         patch.object(config, "TWILIO_PHONE_NUMBER", twilio_phone), \
         patch.object(config, "TWILIO_MESSAGING_SERVICE_SID", MSID), \
         patch.object(config, "APP_URL", app_url), \
         patch("sms_inbound._schedule_coach"):
        return client.post(
            path,
            data=params,
            headers={"X-Twilio-Signature": signature},
            content_type="application/x-www-form-urlencoded",
        )


def test_valid_inbound_sms(app_client, two_users):
    u1, _ = two_users
    lead_id = _seed_lead(u1)

    res = _post_inbound(app_client, body="Yes, Saturday works", message_sid="SMvalid001")
    assert res.status_code == 200
    assert b"<Response>" in res.data

    msgs = db.list_lead_messages(u1, lead_id)
    inbound = [m for m in msgs if m["direction"] == "inbound"]
    assert len(inbound) == 1
    assert inbound[0]["message_body"] == "Yes, Saturday works"
    assert inbound[0]["provider_message_id"] == "SMvalid001"
    assert inbound[0]["phone_number"] == SENDER
    assert inbound[0].get("to_number") in (None, TWILIO_TO)  # column may exist post-migration
    assert inbound[0]["status"] == "received"

    activities = crm_db.list_lead_activities(u1, lead_id)
    assert any(a["event_type"] == "sms_inbound" for a in activities)
    needs = crm_db.list_needs_attention(u1)
    assert not any(
        n["lead_id"] == lead_id and n.get("reason_code") == "unreviewed_inbound"
        for n in needs
    )


def test_invalid_twilio_signature(app_client, two_users):
    u1, _ = two_users
    lead_id = _seed_lead(u1)

    res = _post_inbound(
        app_client,
        body="Should not store",
        message_sid="SMbadsig001",
        bad_signature=True,
    )
    assert res.status_code == 403
    msgs = [m for m in db.list_lead_messages(u1, lead_id) if m["direction"] == "inbound"]
    assert msgs == []


def test_unknown_sender(app_client, two_users):
    _u1, _ = two_users
    res = _post_inbound(
        app_client,
        from_number="+15559990000",
        body="Hello?",
        message_sid="SMunknownsender",
    )
    assert res.status_code == 200
    assert db.get_sms_message_by_provider_id("SMunknownsender") is None


def test_unknown_destination_number(app_client, two_users):
    u1, _ = two_users
    lead_id = _seed_lead(u1)
    res = _post_inbound(
        app_client,
        to_number="+15550001111",
        body="Wrong To",
        message_sid="SMwrongto001",
    )
    assert res.status_code == 200
    assert db.get_sms_message_by_provider_id("SMwrongto001") is None
    msgs = [m for m in db.list_lead_messages(u1, lead_id) if m["direction"] == "inbound"]
    assert msgs == []


def test_duplicate_webhook_retry(app_client, two_users):
    u1, _ = two_users
    lead_id = _seed_lead(u1)
    sid = "SMdupretry001"

    r1 = _post_inbound(app_client, body="First delivery", message_sid=sid)
    r2 = _post_inbound(app_client, body="First delivery", message_sid=sid)
    assert r1.status_code == 200
    assert r2.status_code == 200

    inbound = [
        m
        for m in db.list_lead_messages(u1, lead_id)
        if m["direction"] == "inbound" and m["provider_message_id"] == sid
    ]
    assert len(inbound) == 1


def test_stop_keyword_opts_out(app_client, two_users):
    u1, _ = two_users
    lead_id = _seed_lead(u1)
    # Seed a suggested draft that should be cancelled on STOP.
    db.create_sms_message(
        user_id=u1,
        persona_id=None,
        provider="claude",
        data={"lead_name": "Inbound Lead", "phone_number": SENDER, "message_body": "Draft"},
        status="suggested",
        lead_id=lead_id,
        direction="suggested",
    )

    res = _post_inbound(app_client, body="STOP", message_sid="SMstop001")
    assert res.status_code == 200

    lead = db.get_lead(lead_id, u1)
    assert lead["opt_out_status"] == "opted_out"
    assert lead["status"] == "do_not_contact"

    activities = crm_db.list_lead_activities(u1, lead_id)
    assert any(a["event_type"] == "opt_out" for a in activities)

    drafts = [
        m
        for m in db.list_lead_messages(u1, lead_id)
        if m["status"] == "suggested"
    ]
    assert drafts == [] or all(m["status"] != "suggested" for m in db.list_lead_messages(u1, lead_id))
    remaining = [m for m in db.list_lead_messages(u1, lead_id) if m.get("message_body") == "Draft"]
    assert all(m["status"] == "cancelled" for m in remaining)


def test_help_keyword_no_app_reply(app_client, two_users):
    u1, _ = two_users
    lead_id = _seed_lead(u1)

    with patch("sms_provider.get_sms_provider") as get_provider:
        provider = get_provider.return_value
        provider.send_sms.side_effect = AssertionError("must not auto-send on HELP")
        res = _post_inbound(app_client, body="HELP", message_sid="SMhelp001")

    assert res.status_code == 200
    assert b"<Response></Response>" in res.data.replace(b" ", b"") or b"<Response>" in res.data

    activities = crm_db.list_lead_activities(u1, lead_id)
    assert any(a["event_type"] == "sms_help" for a in activities)
    # No outbound messages created by HELP handling.
    outbound = [
        m
        for m in db.list_lead_messages(u1, lead_id)
        if m["direction"] == "outbound" and m["status"] in ("queued", "sent", "delivered")
    ]
    # Only the seed outbound should exist.
    assert len(outbound) == 1


def test_media_message(app_client, two_users):
    u1, _ = two_users
    lead_id = _seed_lead(u1)
    res = _post_inbound(
        app_client,
        body="",
        message_sid="SMmedia001",
        extra={
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/media/example.jpg",
        },
    )
    assert res.status_code == 200
    inbound = [m for m in db.list_lead_messages(u1, lead_id) if m["provider_message_id"] == "SMmedia001"]
    assert len(inbound) == 1
    assert inbound[0]["direction"] == "inbound"
    assert "media" in (inbound[0]["message_body"] or "").lower()


def test_tenant_isolation(app_client, two_users):
    u1, u2 = two_users
    lead1 = _seed_lead(u1, phone="+15551110001", name="Tenant One Lead")
    lead2 = _seed_lead(u2, phone="+15551110002", name="Tenant Two Lead")

    res = _post_inbound(
        app_client,
        from_number="+15551110001",
        body="Only for tenant one",
        message_sid="SMtenant001",
    )
    assert res.status_code == 200

    msgs1 = db.list_lead_messages(u1, lead1)
    msgs2 = db.list_lead_messages(u2, lead2)
    assert any(m["provider_message_id"] == "SMtenant001" for m in msgs1)
    assert not any(m["provider_message_id"] == "SMtenant001" for m in msgs2)
    assert db.get_lead(lead1, u2) is None


def test_inbound_ai_auto_sends_reply(app_client, two_users):
    u1, _ = two_users
    lead_id = _seed_lead(u1)

    fake_analysis = {
        "summary": "Lead wants weekend showing",
        "intent": "schedule",
        "recommended_next_action": "Offer two times",
        "draft_reply": "Great — Sat 11am or Sun 2pm?",
        "suggested_reply": "Great — Sat 11am or Sun 2pm?",
        "confidence": 0.9,
        "confidence_score": 0.9,
        "sensitive_topic": False,
        "requires_manual_review": False,
        "escalation_topics": [],
        "home_value_pitch": None,
        "suggested_lead_status": "contacted",
        "suggested_follow_up_at": None,
        "suggested_tasks": [],
        "appointment_requested": False,
        "appointment_details": None,
        "needs_attention_reasons": [],
    }

    from sms_inbound import parse_inbound_form, process_inbound_sms

    params = {
        "From": SENDER,
        "To": TWILIO_TO,
        "Body": "Can we tour Saturday?",
        "MessageSid": "SMcoach001",
        "SmsStatus": "received",
        "MessagingServiceSid": MSID,
    }

    class _Prov:
        def send_sms(self, *args, **kwargs):
            return {"provider_message_id": "SM-auto-1", "status": "queued"}

    with patch.object(config, "TWILIO_PHONE_NUMBER", TWILIO_TO), \
         patch.object(config, "TWILIO_MESSAGING_SERVICE_SID", MSID), \
         patch.object(config, "SMS_AI_AUTO_REPLY_ENABLED", True), \
         patch("sms_coach.is_configured", return_value=True), \
         patch("sms_coach.analyze_inbound_reply", return_value=fake_analysis) as analyze, \
         patch("sms_ai_agent._auto_reply_allowed", return_value=(True, None)), \
         patch("sms_quiet_hours.in_quiet_hours", return_value=False), \
         patch("sms_providers.get_sms_provider", return_value=_Prov()):
        result = process_inbound_sms(
            parse_inbound_form(params),
            defer_coach=False,
            app=None,
        )

    assert result["ok"] is True
    analyze.assert_called()
    msgs = db.list_lead_messages(u1, lead_id)
    suggested = [m for m in msgs if m["status"] == "suggested" or m["direction"] == "suggested"]
    assert suggested == []
    outbound = [m for m in msgs if m["direction"] == "outbound" and m.get("reply_to_message_id")]
    assert outbound
    assert db.list_pending_insights(u1) == [] or all(
        i["lead_id"] != lead_id for i in db.list_pending_insights(u1)
    )

    with patch("sms_inbound._schedule_coach"):
        res = _post_inbound(
            app_client,
            body="Another reply",
            message_sid="SMcoach002",
        )
    assert res.status_code == 200


def test_legacy_inbound_path_still_works(app_client, two_users):
    u1, _ = two_users
    lead_id = _seed_lead(u1)
    res = _post_inbound(
        app_client,
        path="/webhook/sms/inbound",
        body="Legacy path reply",
        message_sid="SMlegacy001",
    )
    assert res.status_code == 200
    assert any(
        m["provider_message_id"] == "SMlegacy001"
        for m in db.list_lead_messages(u1, lead_id)
    )


def test_start_restores_opt_out_without_sending(app_client, two_users):
    u1, _ = two_users
    lead_id = _seed_lead(u1)
    db.mark_lead_opt_out(lead_id, u1)

    with patch("sms_provider.TwilioSmsProvider.send_sms") as send_sms:
        res = _post_inbound(app_client, body="START", message_sid="SMstart001")
    assert res.status_code == 200
    send_sms.assert_not_called()

    lead = db.get_lead(lead_id, u1)
    assert lead["opt_out_status"] == "active"
    activities = crm_db.list_lead_activities(u1, lead_id)
    assert any(a["event_type"] == "sms_opt_in" for a in activities)
