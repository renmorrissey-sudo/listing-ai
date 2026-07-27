"""Secure voice-call recording storage, playback auth, and timeline controls."""

from datetime import datetime, timezone

import crm_db
import db
from migrations.runner import apply_pending_migrations
from voice_provider import VoiceProviderError, normalize_voice_webhook


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
            "lead_name": "Recording Lead",
            "phone_number": phone,
            "compliance_confirmed": True,
        },
    )
    assert res.status_code == 201, res.get_json()
    return res.get_json()


def _end_of_call_payload(provider_call_id, *, with_recording=True, duration=222):
    artifact = {
        "transcript": "AI: Hello?\nUser: Yes, still interested.",
    }
    if with_recording:
        artifact["recording"] = {
            "mono": "https://storage.example/vapi/mono-expired.wav",
            "stereo": "https://storage.example/vapi/stereo-expired.wav",
        }
        artifact["recordingUrl"] = "https://storage.example/vapi/mono-expired.wav"
        artifact["stereoRecordingUrl"] = "https://storage.example/vapi/stereo-expired.wav"
    else:
        artifact["recording"] = {}
    return {
        "message": {
            "type": "end-of-call-report",
            "endedReason": "customer-ended-call",
            "durationSeconds": duration,
            "call": {"id": provider_call_id, "durationSeconds": duration},
            "artifact": artifact,
            "analysis": {"summary": "Lead confirmed interest in a consultation."},
            "summary": "Lead confirmed interest in a consultation.",
        }
    }


def test_normalize_extracts_recording_fields():
    normalized = normalize_voice_webhook(
        _end_of_call_payload("vapi_rec_norm", duration=222)
    )
    assert normalized["provider_call_id"] == "vapi_rec_norm"
    assert normalized["recording_url"].endswith("mono-expired.wav")
    assert normalized["stereo_recording_url"].endswith("stereo-expired.wav")
    assert normalized["recording_duration_seconds"] == 222
    assert normalized["recording_status"] == "available"
    assert "still interested" in (normalized["transcript"] or "")


def test_completed_webhook_stores_recording_and_is_idempotent(app_client, two_users, monkeypatch):
    u1, _ = two_users
    apply_pending_migrations()
    _profile(u1)
    persona_id = _persona(u1)
    started = _start_call(app_client, u1, persona_id, "3035550101", "vapi_rec_1", monkeypatch)
    lead_id = started["lead_id"]
    call_id = started["id"]

    payload = _end_of_call_payload("vapi_rec_1")
    first = app_client.post("/webhook/voice", json=payload)
    second = app_client.post("/webhook/voice", json=payload)
    assert first.status_code == 200
    assert second.status_code == 200

    call = db.get_voice_call(call_id, u1)
    assert call["recording_url"].endswith("mono-expired.wav")
    assert call["stereo_recording_url"].endswith("stereo-expired.wav")
    assert call["recording_duration_seconds"] == 222
    assert call["recording_status"] == "available"
    assert call["transcript"]
    assert call["status"] == "completed"

    activities = [
        a for a in crm_db.list_lead_activities(u1, lead_id)
        if a["event_type"] == "voice_call_completed"
    ]
    assert len(activities) == 1

    calls = db.list_voice_calls(u1)
    assert len([c for c in calls if c["provider_call_id"] == "vapi_rec_1"]) == 1


def test_lead_detail_shows_recording_control(app_client, two_users, monkeypatch):
    u1, _ = two_users
    apply_pending_migrations()
    _profile(u1)
    persona_id = _persona(u1)
    started = _start_call(app_client, u1, persona_id, "3035550102", "vapi_rec_2", monkeypatch)
    lead_id = started["lead_id"]
    call_id = started["id"]
    app_client.post("/webhook/voice", json=_end_of_call_payload("vapi_rec_2"))

    page = app_client.get(f"/crm/leads/{lead_id}")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Voice call completed" in html
    assert f"/api/voice-calls/{call_id}/recording" in html
    assert "preload=\"none\"" in html
    assert "Open recording" in html
    assert "View transcript" in html
    assert "https://storage.example/vapi/mono-expired.wav" not in html
    assert "3 min 42 sec" in html


def test_other_tenant_cannot_access_recording(app_client, two_users, monkeypatch):
    u1, u2 = two_users
    apply_pending_migrations()
    _profile(u1)
    persona_id = _persona(u1)
    started = _start_call(app_client, u1, persona_id, "3035550103", "vapi_rec_3", monkeypatch)
    call_id = started["id"]
    app_client.post("/webhook/voice", json=_end_of_call_payload("vapi_rec_3"))

    with app_client.session_transaction() as sess:
        sess["user_id"] = u2
    denied = app_client.get(f"/api/voice-calls/{call_id}/recording")
    assert denied.status_code == 404
    denied_t = app_client.get(f"/api/voice-calls/{call_id}/transcript")
    assert denied_t.status_code == 404


def test_calls_without_recording_show_unavailable_states(app_client, two_users, monkeypatch):
    u1, _ = two_users
    apply_pending_migrations()
    _profile(u1)
    persona_id = _persona(u1)

    # not_enabled / empty artifact.recording
    started = _start_call(app_client, u1, persona_id, "3035550104", "vapi_rec_4", monkeypatch)
    lead_id = started["lead_id"]
    app_client.post(
        "/webhook/voice",
        json=_end_of_call_payload("vapi_rec_4", with_recording=False),
    )
    call = db.get_voice_call(started["id"], u1)
    assert call["recording_status"] == "not_enabled"
    assert not call.get("recording_url")

    page = app_client.get(f"/crm/leads/{lead_id}")
    assert "Recording was not enabled for this call" in page.get_data(as_text=True)

    # processing: completed with no recording keys at all
    started2 = _start_call(app_client, u1, persona_id, "3035550105", "vapi_rec_5", monkeypatch)
    app_client.post(
        "/webhook/voice",
        json={
            "message": {
                "type": "end-of-call-report",
                "call": {"id": "vapi_rec_5", "durationSeconds": 30},
                "endedReason": "customer-ended-call",
                "summary": "Short call",
                "artifact": {"transcript": "AI: Hi"},
            }
        },
    )
    call2 = db.get_voice_call(started2["id"], u1)
    assert call2["recording_status"] == "processing"
    page2 = app_client.get(f"/crm/leads/{started2['lead_id']}")
    assert "Recording processing" in page2.get_data(as_text=True)


def test_expired_recording_refresh_and_failure(app_client, two_users, monkeypatch):
    u1, _ = two_users
    apply_pending_migrations()
    _profile(u1)
    persona_id = _persona(u1)
    started = _start_call(app_client, u1, persona_id, "3035550106", "vapi_rec_6", monkeypatch)
    call_id = started["id"]
    app_client.post("/webhook/voice", json=_end_of_call_payload("vapi_rec_6"))

    monkeypatch.setattr(
        "app.get_voice_provider",
        lambda: type(
            "P",
            (),
            {
                "get_recording_download_url": staticmethod(
                    lambda *a, **k: "https://cdn.example/fresh-presigned.wav"
                )
            },
        )(),
    )
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    ok = app_client.get(f"/api/voice-calls/{call_id}/recording")
    assert ok.status_code == 302
    assert ok.headers["Location"] == "https://cdn.example/fresh-presigned.wav"

    monkeypatch.setattr(
        "app.get_voice_provider",
        lambda: type(
            "P",
            (),
            {
                "get_recording_download_url": staticmethod(
                    lambda *a, **k: (_ for _ in ()).throw(
                        VoiceProviderError("presign expired")
                    )
                )
            },
        )(),
    )
    failed = app_client.get(f"/api/voice-calls/{call_id}/recording")
    assert failed.status_code == 503
    body = failed.get_json()
    assert body["recording_status"] == "unavailable"
    assert "Recording unavailable" in body["error"]


def test_voice_calls_list_never_exposes_raw_vapi_url(app_client, two_users, monkeypatch):
    u1, _ = two_users
    apply_pending_migrations()
    _profile(u1)
    persona_id = _persona(u1)
    started = _start_call(app_client, u1, persona_id, "3035550107", "vapi_rec_7", monkeypatch)
    call_id = started["id"]
    app_client.post("/webhook/voice", json=_end_of_call_payload("vapi_rec_7"))

    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    res = app_client.get("/voice/calls")
    assert res.status_code == 200
    payload = res.get_json()
    match = next(c for c in payload["calls"] if c["id"] == call_id)
    assert match["recording_url"] == f"/api/voice-calls/{call_id}/recording"
    assert match["transcript_url"] == f"/api/voice-calls/{call_id}/transcript"
    assert "storage.example" not in str(payload)


def test_migration_preserves_existing_calls_and_leads(two_users):
    u1, _ = two_users
    apply_pending_migrations()
    now = datetime.now(timezone.utc).isoformat()
    with db.get_db() as conn:
        lead_cur = conn.execute(
            """
            INSERT INTO leads (user_id, name, phone_number, status, source, created_at, updated_at)
            VALUES (?, 'Keep Me', '+13035550199', 'contacted', 'voice', ?, ?)
            """,
            (u1, now, now),
        )
        lead_id = lead_cur.lastrowid
        call_cur = conn.execute(
            """
            INSERT INTO voice_calls
                (user_id, persona_id, lead_id, provider, direction, lead_name, phone_number,
                 status, summary, created_at, completed_at)
            VALUES (?, NULL, ?, 'vapi', 'outbound', 'Keep Me', '+13035550199',
                    'completed', 'Pre-migration call', ?, ?)
            """,
            (u1, lead_id, now, now),
        )
        call_id = call_cur.lastrowid

    # Re-running additive migration must be a no-op and keep rows.
    apply_pending_migrations()
    lead = db.get_lead(lead_id, u1)
    call = db.get_voice_call(call_id, u1)
    assert lead["name"] == "Keep Me"
    assert call["summary"] == "Pre-migration call"
    assert "recording_status" in call  # column exists (may be null)
    assert call.get("recording_url") in (None, "")
