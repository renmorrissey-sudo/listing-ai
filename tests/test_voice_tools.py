from datetime import datetime, timezone

import crm_db
import db
from migrations.runner import apply_pending_migrations


def _lead(user_id, name, status="new", phone="+13035550101", **extra):
    lead_id = db.create_lead_record(
        user_id,
        phone,
        name=name,
        status=status,
        source="voice",
        property_interest=extra.get("property_interest"),
    )
    if extra:
        fields = []
        params = []
        for key in ("email", "sms_consent_status", "sms_sending_blocked", "next_action"):
            if key in extra:
                fields.append(f"{key} = ?")
                params.append(extra[key])
        if fields:
            with db.get_db() as conn:
                conn.execute(
                    f"UPDATE leads SET {', '.join(fields)} WHERE id = ? AND user_id = ?",
                    tuple(params + [lead_id, user_id]),
                )
    return lead_id


def _voice_call(user_id, provider_call_id="vapi_tools"):
    now = datetime.now(timezone.utc).isoformat()
    with db.get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO voice_calls
                (user_id, persona_id, lead_id, provider, direction, lead_name,
                 phone_number, status, provider_call_id, created_at)
            VALUES (?, NULL, NULL, 'vapi', 'outbound', 'Voice Tools',
                    '+13035550199', 'started', ?, ?)
            """,
            (user_id, provider_call_id, now),
        )
        return cur.lastrowid


def _tool_payload(call_id, provider_call_id, tool_name, arguments=None, tool_id="tool_1"):
    return {
        "message": {
            "type": "tool-calls",
            "call": {
                "id": provider_call_id,
                "metadata": {"topai_call_id": str(call_id)},
            },
            "toolCallList": [
                {
                    "id": tool_id,
                    "name": tool_name,
                    "arguments": arguments or {},
                }
            ],
        }
    }


def _tool_result(response):
    body = response.get_json()
    assert "results" in body
    assert body["results"][0]["toolCallId"] == "tool_1"
    return body["results"][0]["result"]


def test_voice_tool_lists_all_open_leads_by_name(app_client, two_users):
    u1, u2 = two_users
    apply_pending_migrations()
    call_id = _voice_call(u1)
    _lead(u1, "Ada Buyer", status="new", phone="+13035550101")
    _lead(u1, "Ben Seller", status="qualified", phone="+13035550102")
    _lead(u1, "Closed Client", status="closed_won", phone="+13035550103")
    _lead(u2, "Other Tenant", status="new", phone="+13035550104")

    res = app_client.post(
        "/webhook/voice",
        json=_tool_payload(call_id, "vapi_tools", "list_open_leads"),
    )

    assert res.status_code == 200
    result = _tool_result(res)
    assert result["count"] == 2
    names = {lead["name"] for lead in result["leads"]}
    assert names == {"Ada Buyer", "Ben Seller"}
    assert "Closed Client" not in result["summary"]
    assert "Other Tenant" not in result["summary"]


def test_voice_tool_can_update_every_pipeline_status(app_client, two_users):
    u1, _ = two_users
    apply_pending_migrations()
    call_id = _voice_call(u1)
    lead_id = _lead(u1, "Status Lead")

    for status in [
        "attempting_contact",
        "contacted",
        "qualified",
        "appointment_scheduled",
        "appointment_completed",
        "nurture",
        "under_contract",
        "closed_won",
        "closed_lost",
        "do_not_contact",
        "new",
    ]:
        res = app_client.post(
            "/webhook/voice",
            json=_tool_payload(
                call_id,
                "vapi_tools",
                "update_lead_status",
                {"lead_id": lead_id, "status": status},
            ),
        )
        assert res.status_code == 200
        assert _tool_result(res)["ok"] is True
        assert db.get_lead(lead_id, u1)["status"] == status


def test_voice_tool_can_mark_lead_sms_verified(app_client, two_users):
    u1, _ = two_users
    apply_pending_migrations()
    call_id = _voice_call(u1)
    lead_id = _lead(
        u1,
        "Consent Lead",
        sms_consent_status="unverified",
        sms_sending_blocked=1,
    )

    res = app_client.post(
        "/webhook/voice",
        json=_tool_payload(
            call_id,
            "vapi_tools",
            "update_lead_sms_consent_status",
            {"lead_id": lead_id, "sms_consent_status": "SMS Verified"},
        ),
    )

    assert res.status_code == 200
    assert _tool_result(res)["ok"] is True
    lead = db.get_lead(lead_id, u1)
    assert lead["sms_consent_status"] == "verified"
    assert not bool(lead["sms_sending_blocked"])


def test_voice_tool_updates_lead_contact_info(app_client, two_users):
    u1, _ = two_users
    apply_pending_migrations()
    call_id = _voice_call(u1)
    lead_id = _lead(u1, "Contact Lead", phone="+13035550105", email="old@example.com")

    res = app_client.post(
        "/webhook/voice",
        json=_tool_payload(
            call_id,
            "vapi_tools",
            "update_lead_contact_info",
            {
                "lead_id": lead_id,
                "name": "Contact Lead Updated",
                "phone_number": "(303) 555-0106",
                "email": "new@example.com",
                "lead_type": "seller",
                "property_interest": "Listing consultation",
                "notes": "Asked for a CMA",
                "next_action": "Schedule listing appointment",
            },
        ),
    )

    assert res.status_code == 200
    result = _tool_result(res)
    assert result["ok"] is True
    lead = db.get_lead(lead_id, u1)
    assert lead["name"] == "Contact Lead Updated"
    assert lead["phone_number"] == "+13035550106"
    assert lead["email"] == "new@example.com"
    assert lead["lead_type"] == "seller"
    assert lead["property_interest"] == "Listing consultation"
    assert lead["notes"] == "Asked for a CMA"
    assert lead["next_action"] == "Schedule listing appointment"
    activities = crm_db.list_lead_activities(u1, lead_id)
    assert activities[0]["event_type"] == "contact_updated"


def test_voice_tool_saves_email_draft_to_lead_timeline(app_client, two_users):
    u1, _ = two_users
    apply_pending_migrations()
    call_id = _voice_call(u1)
    lead_id = _lead(u1, "Email Lead", email="lead@example.com")

    res = app_client.post(
        "/webhook/voice",
        json=_tool_payload(
            call_id,
            "vapi_tools",
            "draft_lead_email",
            {
                "lead_id": lead_id,
                "subject": "Checking in",
                "body": "Hi, are you still interested in touring homes this week?",
            },
        ),
    )

    assert res.status_code == 200
    result = _tool_result(res)
    assert result["ok"] is True
    activities = crm_db.list_lead_activities(u1, lead_id)
    assert activities[0]["event_type"] == "email_draft_created"
    assert "Checking in" in activities[0]["summary"]


def test_voice_tool_sends_email_to_lead(app_client, two_users, monkeypatch):
    u1, _ = two_users
    apply_pending_migrations()
    call_id = _voice_call(u1)
    lead_id = _lead(u1, "Email Lead", email="lead@example.com")
    sent = {}

    def fake_send(user_id, selected_lead_id, **kwargs):
        sent.update(
            {
                "user_id": user_id,
                "lead_id": selected_lead_id,
                "subject": kwargs["subject"],
                "body": kwargs["body"],
            }
        )
        return {
            "ok": True,
            "to_email": "lead@example.com",
            "provider_status": "sent",
        }, None

    monkeypatch.setattr("voice_tools.send_lead_email", fake_send)

    res = app_client.post(
        "/webhook/voice",
        json=_tool_payload(
            call_id,
            "vapi_tools",
            "send_lead_email",
            {
                "lead_id": lead_id,
                "subject": "Showing follow-up",
                "body": "Would you like to see the home this week?",
            },
        ),
    )

    assert res.status_code == 200
    result = _tool_result(res)
    assert result["ok"] is True
    assert result["email"]["provider_status"] == "sent"
    assert sent == {
        "user_id": u1,
        "lead_id": lead_id,
        "subject": "Showing follow-up",
        "body": "Would you like to see the home this week?",
    }
