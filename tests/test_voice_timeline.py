"""Activity Timeline should show meaningful voice events only (no queued spam)."""

import json

import crm_db
import db
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


def _start_call(app_client, user_id, persona_id, phone, provider_call_id, monkeypatch):
    monkeypatch.setattr(
        "app.get_voice_provider",
        lambda: type(
            "P",
            (),
            {
                "start_outbound_call": staticmethod(
                    lambda *a, **k: {"provider_call_id": provider_call_id}
                )
            },
        )(),
    )
    with app_client.session_transaction() as sess:
        sess["user_id"] = user_id
    res = app_client.post(
        "/voice/calls",
        json={
            "persona_id": persona_id,
            "lead_name": "Timeline Lead",
            "phone_number": phone,
            "compliance_confirmed": True,
        },
    )
    assert res.status_code == 201, res.get_json()
    return res.get_json()


def _status_update(provider_call_id, status):
    return {
        "message": {
            "type": "status-update",
            "call": {"id": provider_call_id, "status": status},
            "status": status,
        }
    }


def test_repeated_queued_webhooks_create_no_visible_activity(app_client, two_users, monkeypatch):
    u1, _ = two_users
    apply_pending_migrations()
    _profile(u1)
    persona_id = _persona(u1)
    started = _start_call(app_client, u1, persona_id, "3035550201", "vapi_q1", monkeypatch)
    lead_id = started["lead_id"]

    for _ in range(5):
        res = app_client.post("/webhook/voice", json=_status_update("vapi_q1", "queued"))
        assert res.status_code == 200
    app_client.post("/webhook/voice", json=_status_update("vapi_q1", "ringing"))
    app_client.post("/webhook/voice", json=_status_update("vapi_q1", "initiated"))

    raw = crm_db.list_lead_activities(u1, lead_id, for_timeline=False)
    voice_raw = [a for a in raw if a["event_type"].startswith("voice_call_")]
    assert voice_raw == []

    timeline = crm_db.list_lead_activities(u1, lead_id, for_timeline=True)
    assert not any("queued" in (a.get("summary") or "").lower() for a in timeline)
    assert not any(a["event_type"] == "voice_call_started" for a in timeline)

    page = app_client.get(f"/crm/leads/{lead_id}")
    html = page.get_data(as_text=True)
    assert "AI call started: queued" not in html
    assert "AI call started" not in html


def test_completed_failed_unanswered_appear_and_retries_idempotent(app_client, two_users, monkeypatch):
    u1, u2 = two_users
    apply_pending_migrations()
    _profile(u1)
    persona_id = _persona(u1)

    completed = _start_call(app_client, u1, persona_id, "3035550202", "vapi_ok", monkeypatch)
    payload = {
        "message": {
            "type": "end-of-call-report",
            "endedReason": "customer-ended-call",
            "durationSeconds": 95,
            "call": {"id": "vapi_ok", "durationSeconds": 95},
            "artifact": {
                "transcript": "AI: Hello\nUser: Interested",
                "recording": {"mono": "https://storage.example/mono.wav"},
                "recordingUrl": "https://storage.example/mono.wav",
            },
            "analysis": {"summary": "Lead wants a consult"},
            "summary": "Lead wants a consult",
        }
    }
    assert app_client.post("/webhook/voice", json=payload).status_code == 200
    assert app_client.post("/webhook/voice", json=payload).status_code == 200

    lead_id = completed["lead_id"]
    timeline = crm_db.list_lead_activities(u1, lead_id, for_timeline=True)
    completed_rows = [a for a in timeline if a["event_type"] == "voice_call_completed"]
    assert len(completed_rows) == 1
    assert "Voice call completed" in (completed_rows[0]["summary"] or "")

    page = app_client.get(f"/crm/leads/{lead_id}")
    html = page.get_data(as_text=True)
    assert "Voice call completed" in html
    assert f"/api/voice-calls/{completed['id']}/recording" in html
    assert "View transcript" in html
    assert "AI call started: queued" not in html

    failed = _start_call(app_client, u1, persona_id, "3035550203", "vapi_fail", monkeypatch)
    fail_payload = {
        "message": {
            "type": "status-update",
            "endedReason": "customer-did-not-answer",
            "call": {"id": "vapi_fail", "status": "ended"},
        }
    }
    assert app_client.post("/webhook/voice", json=fail_payload).status_code == 200
    assert app_client.post("/webhook/voice", json=fail_payload).status_code == 200
    fail_timeline = crm_db.list_lead_activities(u1, failed["lead_id"], for_timeline=True)
    unanswered = [a for a in fail_timeline if a["event_type"] == "voice_call_unanswered"]
    assert len(unanswered) == 1
    assert unanswered[0]["summary"] == "Call unanswered"

    # Tenant isolation: u2 cannot see u1 lead timeline/recording.
    with app_client.session_transaction() as sess:
        sess["user_id"] = u2
    denied = app_client.get(f"/crm/leads/{lead_id}", follow_redirects=False)
    assert denied.status_code in (302, 404)
    denied_rec = app_client.get(f"/api/voice-calls/{completed['id']}/recording")
    assert denied_rec.status_code == 404


def test_appointment_call_populates_follow_up_and_next_action(app_client, two_users, monkeypatch):
    u1, _ = two_users
    apply_pending_migrations()
    _profile(u1)
    persona_id = _persona(u1)
    started = _start_call(app_client, u1, persona_id, "3035550208", "vapi_appt", monkeypatch)
    lead_id = started["lead_id"]
    res = app_client.post(
        "/webhook/voice",
        json={
            "message": {
                "type": "end-of-call-report",
                "endedReason": "customer-ended-call",
                "durationSeconds": 120,
                "call": {"id": "vapi_appt"},
                "summary": "Lead asked to book an appointment for a showing next week",
                "analysis": {
                    "summary": "Lead asked to book an appointment for a showing next week",
                    "nextAction": "Text two showing times",
                },
                "artifact": {"transcript": "User: Can we set an appointment?"},
            }
        },
    )
    assert res.status_code == 200
    lead = db.get_lead(lead_id, u1)
    assert lead["status"] == "appointment_scheduled"
    assert lead.get("next_action")
    assert "appointment" in lead["next_action"].lower() or "showing" in lead["next_action"].lower()
    assert lead.get("next_follow_up_at")
    # Retry must not clear fields or create a second competing follow-up date wipe.
    app_client.post(
        "/webhook/voice",
        json={
            "message": {
                "type": "end-of-call-report",
                "endedReason": "customer-ended-call",
                "call": {"id": "vapi_appt"},
                "summary": "Lead asked to book an appointment for a showing next week",
                "artifact": {"transcript": "User: Can we set an appointment?"},
            }
        },
    )
    lead2 = db.get_lead(lead_id, u1)
    assert lead2["next_action"]
    assert lead2["next_follow_up_at"] == lead["next_follow_up_at"]

    page = app_client.get("/crm/leads")
    html = page.get_data(as_text=True)
    assert lead2["next_action"][:40] in html
    assert (lead2["next_follow_up_at"] or "")[:10] in html


def test_timeline_hides_legacy_queued_rows_without_deleting_completed(two_users):
    from datetime import datetime, timezone

    u1, _ = two_users
    apply_pending_migrations()
    now = datetime.now(timezone.utc).isoformat()
    with db.get_db() as conn:
        lead_cur = conn.execute(
            """
            INSERT INTO leads (user_id, name, phone_number, status, source, created_at, updated_at)
            VALUES (?, 'Legacy', '+13035550204', 'contacted', 'voice', ?, ?)
            """,
            (u1, now, now),
        )
        lead_id = lead_cur.lastrowid
        call_cur = conn.execute(
            """
            INSERT INTO voice_calls
                (user_id, persona_id, lead_id, provider, direction, lead_name, phone_number,
                 status, summary, recording_url, transcript, created_at, completed_at)
            VALUES (?, NULL, ?, 'vapi', 'outbound', 'Legacy', '+13035550204',
                    'completed', 'Keep me', 'https://example/rec.wav', 'Hello', ?, ?)
            """,
            (u1, lead_id, now, now),
        )
        call_id = call_cur.lastrowid
        conn.execute(
            """
            INSERT INTO lead_activities
                (lead_id, user_id, actor_user_id, event_type, summary, payload_json, created_at)
            VALUES (?, ?, ?, 'voice_call_started', 'AI call started', ?, ?)
            """,
            (
                lead_id,
                u1,
                u1,
                json.dumps({"voice_call_id": call_id, "status": "started"}),
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO lead_activities
                (lead_id, user_id, actor_user_id, event_type, summary, payload_json, created_at)
            VALUES (?, ?, ?, 'voice_call_updated', 'AI call started: queued', ?, ?)
            """,
            (
                lead_id,
                u1,
                u1,
                json.dumps({"voice_call_id": call_id, "status": "started", "outcome": "queued"}),
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO lead_activities
                (lead_id, user_id, actor_user_id, event_type, summary, payload_json, created_at)
            VALUES (?, ?, ?, 'voice_call_completed', 'Voice call completed', ?, ?)
            """,
            (
                lead_id,
                u1,
                u1,
                json.dumps(
                    {
                        "voice_call_id": call_id,
                        "status": "completed",
                        "has_recording": True,
                        "has_transcript": True,
                        "summary": "Keep me",
                    }
                ),
                now,
            ),
        )

    timeline = crm_db.list_lead_activities(u1, lead_id, for_timeline=True)
    types = [a["event_type"] for a in timeline]
    assert types.count("voice_call_completed") == 1
    assert "voice_call_started" not in types
    assert "voice_call_updated" not in types

    # Cleanup migration removes only transient rows (invoke directly; 006 may
    # already be stamped from earlier apply_pending_migrations in this DB).
    from importlib import import_module

    cleanup = import_module("migrations.versions.006_cleanup_transient_voice_activities")
    with db.get_db() as conn:
        cleanup.upgrade_sqlite(conn)

    remaining = crm_db.list_lead_activities(u1, lead_id, for_timeline=False)
    remaining_types = {a["event_type"] for a in remaining}
    assert "voice_call_completed" in remaining_types
    assert "voice_call_started" not in remaining_types
    assert not any(
        a["event_type"] == "voice_call_updated" and "queued" in (a.get("summary") or "").lower()
        for a in remaining
    )
    call = db.get_voice_call(call_id, u1)
    assert call["summary"] == "Keep me"
    assert call["recording_url"]
    assert db.get_lead(lead_id, u1)["name"] == "Legacy"
