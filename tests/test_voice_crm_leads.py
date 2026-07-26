"""AI Calling Assistant ↔ CRM Leads integration."""

from datetime import datetime, timezone

import crm_db
import db
import lead_service
from lead_service import VOICE_SOURCE, SMS_SOURCE, upsert_crm_lead
from migrations.runner import apply_pending_migrations


def _profile(user_id):
    db.update_business_profile(
        user_id,
        agent_name="Ada Agent",
        brokerage_name="Ada Realty",
        company_name="Ada Homes",
    )


def _persona(user_id):
    return db.create_voice_persona(
        user_id,
        {
            "name": "ISA",
            "persona_type": "buyer",
            "prompt": "Qualify the lead.",
            "tone": "professional",
            "goal": "Book a consult",
            "objection_handling_notes": "",
        },
    )


def test_first_outbound_call_creates_one_lead(app_client, two_users, monkeypatch):
    u1, _ = two_users
    apply_pending_migrations()
    _profile(u1)
    persona_id = _persona(u1)

    monkeypatch.setattr(
        "app.get_voice_provider",
        lambda: type(
            "P",
            (),
            {
                "start_outbound_call": staticmethod(
                    lambda *a, **k: {"provider_call_id": "vapi_call_1"}
                )
            },
        )(),
    )

    with app_client.session_transaction() as sess:
        sess["user_id"] = u1

    res = app_client.post(
        "/voice/calls",
        json={
            "persona_id": persona_id,
            "lead_name": "Ben Miller",
            "phone_number": "3038703107",
            "call_purpose": "Open house follow-up",
            "lead_context": "Attended Meadow Ranch",
            "property_interest": "Townhome",
            "desired_outcome": "Book consultation",
            "compliance_confirmed": True,
        },
    )
    assert res.status_code == 201, res.get_json()
    body = res.get_json()
    assert body["lead_id"]
    leads = db.list_leads(u1)
    assert len(leads) == 1
    assert leads[0]["phone_number"] == "+13038703107"
    assert leads[0]["source"] == VOICE_SOURCE
    assert leads[0]["status"] == "attempting_contact"
    call = db.get_voice_call(body["id"], u1)
    assert call["lead_id"] == body["lead_id"]


def test_second_call_same_number_reuses_lead(app_client, two_users, monkeypatch):
    u1, _ = two_users
    apply_pending_migrations()
    _profile(u1)
    persona_id = _persona(u1)
    calls = {"n": 0}

    def start(*a, **k):
        calls["n"] += 1
        return {"provider_call_id": f"vapi_call_{calls['n']}"}

    monkeypatch.setattr(
        "app.get_voice_provider",
        lambda: type("P", (), {"start_outbound_call": staticmethod(start)})(),
    )
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1

    payload = {
        "persona_id": persona_id,
        "lead_name": "Ben Miller",
        "phone_number": "+1 (303) 870-3107",
        "compliance_confirmed": True,
        "desired_outcome": "Book consult",
    }
    r1 = app_client.post("/voice/calls", json=payload)
    r2 = app_client.post("/voice/calls", json={**payload, "lead_name": "Benjamin Miller"})
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.get_json()["lead_id"] == r2.get_json()["lead_id"]
    assert len(db.list_leads(u1)) == 1


def test_sms_and_voice_share_one_lead(two_users):
    u1, _ = two_users
    apply_pending_migrations()
    sms_id, _, _ = upsert_crm_lead(
        u1,
        "3038703107",
        {"lead_name": "Ben", "notes": "From SMS"},
        source=SMS_SOURCE,
        touch_sms=True,
    )
    voice_id, created, lead = upsert_crm_lead(
        u1,
        "+13038703107",
        {"lead_name": "Ben Miller", "call_purpose": "Follow up"},
        source=VOICE_SOURCE,
        initial_status="attempting_contact",
        touch_call=True,
    )
    assert created is False
    assert sms_id == voice_id
    assert lead["source"] == SMS_SOURCE  # preserved
    assert len(db.list_leads(u1)) == 1


def test_tenants_isolated_same_phone(two_users):
    u1, u2 = two_users
    apply_pending_migrations()
    id1, _, _ = upsert_crm_lead(u1, "3038703107", {"lead_name": "A"}, source=VOICE_SOURCE)
    id2, _, _ = upsert_crm_lead(u2, "3038703107", {"lead_name": "B"}, source=VOICE_SOURCE)
    assert id1 != id2
    assert db.get_lead(id1, u2) is None
    assert db.get_lead(id2, u1) is None


def test_call_appears_in_lead_timeline(app_client, two_users, monkeypatch):
    u1, _ = two_users
    apply_pending_migrations()
    _profile(u1)
    persona_id = _persona(u1)
    monkeypatch.setattr(
        "app.get_voice_provider",
        lambda: type(
            "P",
            (),
            {"start_outbound_call": staticmethod(lambda *a, **k: {"provider_call_id": "vapi_tl"})},
        )(),
    )
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    res = app_client.post(
        "/voice/calls",
        json={
            "persona_id": persona_id,
            "lead_name": "Timeline Lead",
            "phone_number": "3035551212",
            "compliance_confirmed": True,
        },
    )
    lead_id = res.get_json()["lead_id"]
    activities = crm_db.list_lead_activities(u1, lead_id)
    types = {a["event_type"] for a in activities}
    assert "voice_call_started" in types
    assert "lead_created" in types or "status_change" in types


def test_vapi_webhook_updates_linked_lead(app_client, two_users, monkeypatch):
    u1, _ = two_users
    apply_pending_migrations()
    _profile(u1)
    persona_id = _persona(u1)
    monkeypatch.setattr(
        "app.get_voice_provider",
        lambda: type(
            "P",
            (),
            {
                "start_outbound_call": staticmethod(
                    lambda *a, **k: {"provider_call_id": "vapi_end_1"}
                )
            },
        )(),
    )
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    started = app_client.post(
        "/voice/calls",
        json={
            "persona_id": persona_id,
            "lead_name": "Webhook Lead",
            "phone_number": "3035559999",
            "compliance_confirmed": True,
        },
    )
    lead_id = started.get_json()["lead_id"]

    res = app_client.post(
        "/webhook/voice",
        json={
            "message": {
                "type": "end-of-call-report",
                "call": {"id": "vapi_end_1", "durationSeconds": 95},
                "summary": "Lead wants a buyer consultation next week",
                "endedReason": "customer-ended-call",
                "analysis": {"summary": "Lead wants a buyer consultation next week"},
            }
        },
    )
    assert res.status_code == 200
    lead = db.get_lead(lead_id, u1)
    assert lead["status"] == "contacted"
    assert lead.get("latest_call_at")
    activities = crm_db.list_lead_activities(u1, lead_id)
    assert any(a["event_type"] == "voice_call_completed" for a in activities)


def test_old_calls_without_lead_id_do_not_break_lists(two_users):
    u1, _ = two_users
    apply_pending_migrations()
    now = datetime.now(timezone.utc).isoformat()
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO voice_calls
                (user_id, persona_id, lead_id, provider, direction, lead_name, phone_number,
                 status, created_at)
            VALUES (?, NULL, NULL, 'vapi', 'outbound', 'Legacy', '+15550001111', 'completed', ?)
            """,
            (u1, now),
        )
    calls = db.list_voice_calls(u1)
    assert calls
    assert calls[0].get("lead_id") in (None, "")
    # Leads page query still works with zero voice-linked leads.
    assert db.list_leads(u1) == []
