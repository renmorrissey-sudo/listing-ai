"""Ask TopAI live conversation: OpenAI Realtime session, tools, idempotency."""

import crm_db
import db
from ask_topai import policy, registry, sessions
from ask_topai.realtime import runtime, settings
from ask_topai.realtime.openai_client import extract_ephemeral_secret
from tests.test_ask_topai import _lead, _login


def _mint(monkeypatch, value="ek_test_ephemeral_not_a_real_key"):
    monkeypatch.setattr(
        "ask_topai.realtime.openai_client.mint_ephemeral_secret",
        lambda session_obj, user_id=None: {"value": value, "expires_at": 1_700_000_000},
    )
    monkeypatch.setattr("ask_topai.realtime.settings.is_configured", lambda: True)
    monkeypatch.setattr("ask_topai.realtime.settings.key_present", lambda: True)


def test_realtime_model_is_configurable(monkeypatch):
    monkeypatch.setattr("config.ASK_TOPAI_REALTIME_MODEL", "")
    assert settings.realtime_model() == "gpt-realtime-2.1"
    monkeypatch.setattr("config.ASK_TOPAI_REALTIME_MODEL", "gpt-live-future")
    assert settings.realtime_model() == "gpt-live-future"


def test_openai_tools_are_model_independent():
    names = {item["name"] for item in registry.openai_tools()}
    assert names == registry.WRITE_TOOLS | registry.READ_TOOLS
    assert "ask_clarification" not in names
    assert "send_email" not in names
    for item in registry.openai_tools():
        assert item["type"] == "function"
        assert "parameters" in item


def test_future_tools_require_spoken_confirmation():
    assert policy.confirmation_mode("create_lead") == policy.MODE_AUTO
    assert policy.confirmation_mode("send_email") == policy.MODE_SPOKEN_CONFIRMATION
    assert policy.confirmation_mode("initiate_ai_call") == policy.MODE_SPOKEN_CONFIRMATION
    assert policy.confirmation_mode("drop_table") == policy.MODE_FORBIDDEN


def test_extract_ephemeral_secret_shapes():
    assert extract_ephemeral_secret({"value": "ek_abc"})[0] == "ek_abc"
    assert extract_ephemeral_secret({"client_secret": {"value": "ek_def", "expires_at": 9}})[0] == "ek_def"


def test_live_session_mints_ephemeral_key_not_openai_api_key(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _mint(monkeypatch)
    res = app_client.post(
        "/api/ask-topai/live/session",
        json={"context": {"page": "/crm/leads"}},
    )
    assert res.status_code == 200
    data = res.get_json()
    body = res.get_data(as_text=True)
    assert data["ok"] is True
    assert data["client_secret"]["value"].startswith("ek_")
    assert data["model"] == "gpt-realtime-2.1"
    assert data["calls_url"] == "https://api.openai.com/v1/realtime/calls"
    assert "OPENAI_API_KEY" not in body
    assert "sk-" not in body
    assert data["session_id"]


def test_live_session_without_openai_key(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    monkeypatch.setattr("ask_topai.realtime.settings.is_configured", lambda: False)
    monkeypatch.setattr("ask_topai.realtime.settings.key_present", lambda: False)
    res = app_client.post("/api/ask-topai/live/session", json={})
    assert res.status_code == 503
    data = res.get_json()
    assert data["ok"] is False
    assert "OPENAI_API_KEY" not in (data.get("error") or "")


def test_live_health_reports_presence_not_secret(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    monkeypatch.setattr("ask_topai.realtime.settings.is_configured", lambda: False)
    monkeypatch.setattr("ask_topai.realtime.settings.key_present", lambda: False)
    res = app_client.get("/api/ask-topai/live/health")
    assert res.status_code == 503
    data = res.get_json()
    assert data["openai_api_key_present"] is False
    assert data["realtime_model"] == "gpt-realtime-2.1"
    assert "OPENAI_API_KEY" not in res.get_data(as_text=True) or data.get("openai_api_key_present") is False
    assert not any(str(v).startswith("sk-") for v in data.values() if isinstance(v, str))


def test_live_create_lead_exactly_once(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    session_id = "live-create-1"
    sessions.save_session(u1, session_id, [], pending={"mode": "live"}, status="live")
    payload = {
        "session_id": session_id,
        "calls": [
            {
                "call_id": "call_lead_1",
                "name": "create_lead",
                "arguments": {
                    "name": "Mike Johnson",
                    "phone": "720-555-1212",
                    "lead_type": "buyer",
                },
            }
        ],
        "transcript": "Create a new buyer lead for Mike Johnson. 720-555-1212.",
    }
    first = app_client.post("/api/ask-topai/live/tools", json=payload).get_json()
    second = app_client.post("/api/ask-topai/live/tools", json=payload).get_json()
    leads = [lead for lead in db.list_leads(u1, limit=50) if lead.get("name") == "Mike Johnson"]
    assert len(leads) == 1
    assert first["results"][0]["output"]["ok"] is True
    assert second["results"][0]["output"].get("duplicate") is True


def test_live_follow_up_criteria_and_task_on_same_lead(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    session_id = "live-multi-1"
    sessions.save_session(u1, session_id, [], pending={"mode": "live"}, status="live")
    created = app_client.post(
        "/api/ask-topai/live/tools",
        json={
            "session_id": session_id,
            "calls": [
                {
                    "call_id": "call_j1",
                    "name": "create_lead",
                    "arguments": {
                        "name": "Jennifer Miller",
                        "phone": "720-555-0194",
                        "lead_type": "buyer",
                    },
                }
            ],
            "transcript": "Create Jennifer Miller as a buyer. 720-555-0194.",
        },
    ).get_json()
    lead_id = created["results"][0]["output"]["lead_id"]
    follow = app_client.post(
        "/api/ask-topai/live/tools",
        json={
            "session_id": session_id,
            "calls": [
                {
                    "call_id": "call_j2",
                    "name": "update_property_criteria",
                    "arguments": {
                        "lead_id": lead_id,
                        "bedrooms": 3,
                        "property_type": "townhomes",
                        "city": "Littleton",
                        "price_max": 650000,
                    },
                },
                {
                    "call_id": "call_j3",
                    "name": "create_task",
                    "arguments": {
                        "lead_id": lead_id,
                        "title": "Call Jennifer Miller",
                        "due_at": "2026-08-20T15:00:00",
                    },
                },
            ],
            "transcript": "She wants three-bedroom townhomes in Littleton below $650,000, and remind me Thursday to call her.",
        },
    ).get_json()
    assert follow["results"][0]["output"]["ok"] is True
    assert follow["results"][1]["output"]["ok"] is True
    lead = db.get_lead(lead_id, u1)
    assert "Littleton" in (lead.get("property_interest") or "")
    tasks = [task for task in crm_db.list_tasks(u1) if task.get("lead_id") == lead_id]
    assert len(tasks) == 1
    retry = app_client.post(
        "/api/ask-topai/live/tools",
        json={
            "session_id": session_id,
            "calls": [
                {
                    "call_id": "call_j3_retry",
                    "name": "create_task",
                    "arguments": {
                        "lead_id": lead_id,
                        "title": "Call Jennifer Miller",
                        "due_at": "2026-08-20T15:00:00",
                    },
                }
            ],
            "transcript": "remind me Thursday to call her",
        },
    ).get_json()
    assert retry["results"][0]["output"].get("duplicate") is True
    assert len([task for task in crm_db.list_tasks(u1) if task.get("lead_id") == lead_id]) == 1


def test_live_selected_lead_pronouns(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    sarah_id, _ = _lead(u1, "Sarah Johnson", "3035557001")
    _lead(u1, "Ryan Serhant", "3035557002")
    session_id = "live-pronoun"
    sessions.save_session(u1, session_id, [], pending={"mode": "live"}, status="live")
    res = app_client.post(
        "/api/ask-topai/live/tools",
        json={
            "session_id": session_id,
            "context": {"page": f"/crm/leads/{sarah_id}", "lead_id": sarah_id},
            "calls": [
                {
                    "call_id": "call_note_she",
                    "name": "add_lead_note",
                    "arguments": {"note": "She wants a finished basement."},
                }
            ],
            "transcript": "Add that she wants a finished basement.",
        },
    ).get_json()
    assert res["results"][0]["output"]["ok"] is True
    assert "finished basement" in (db.get_lead(sarah_id, u1).get("notes") or "")


def test_live_ambiguous_lead_does_not_write(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    john1, _ = _lead(u1, "John Smith", "3035558001")
    john2, _ = _lead(u1, "John Williams", "3035558002")
    session_id = "live-ambig"
    sessions.save_session(u1, session_id, [], pending={"mode": "live"}, status="live")
    res = app_client.post(
        "/api/ask-topai/live/tools",
        json={
            "session_id": session_id,
            "calls": [
                {
                    "call_id": "call_note_john",
                    "name": "add_lead_note",
                    "arguments": {"lead_name": "John", "note": "Wants to see the house Saturday."},
                }
            ],
            "transcript": "Add a note to John that he wants to see the house Saturday.",
        },
    ).get_json()
    output = res["results"][0]["output"]
    assert output["ok"] is False
    assert output.get("choices")
    assert "Saturday" not in (db.get_lead(john1, u1).get("notes") or "")
    assert "Saturday" not in (db.get_lead(john2, u1).get("notes") or "")


def test_live_tool_failure_is_not_success(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    session_id = "live-fail"
    sessions.save_session(u1, session_id, [], pending={"mode": "live"}, status="live")
    monkeypatch.setattr(
        "ask_topai.actions.execute_create_lead",
        lambda *_a, **_k: (None, "CRM write failed", None),
    )
    res = app_client.post(
        "/api/ask-topai/live/tools",
        json={
            "session_id": session_id,
            "calls": [
                {
                    "call_id": "call_fail",
                    "name": "create_lead",
                    "arguments": {"name": "Pat Lee", "phone": "720-555-3333"},
                }
            ],
            "transcript": "Create Pat Lee at 720-555-3333",
        },
    ).get_json()
    output = res["results"][0]["output"]
    assert output["ok"] is False
    assert "failed" in (output.get("message") or "").lower()
    assert not any(lead.get("name") == "Pat Lee" for lead in db.list_leads(u1, limit=20))


def test_live_send_email_is_not_executed(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    session_id = "live-email"
    sessions.save_session(u1, session_id, [], pending={"mode": "live"}, status="live")
    res = app_client.post(
        "/api/ask-topai/live/tools",
        json={
            "session_id": session_id,
            "calls": [
                {
                    "call_id": "call_email",
                    "name": "send_email",
                    "arguments": {"lead_name": "Sarah", "body": "here are listings"},
                }
            ],
            "transcript": "Email Sarah those four listings.",
        },
    ).get_json()
    output = res["results"][0]["output"]
    assert output["ok"] is False
    assert output["status"] == "confirmation_required"
    assert output.get("executed") is False


def test_live_end_releases_session(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    session_id = "live-end-1"
    sessions.save_session(u1, session_id, [], pending={"mode": "live"}, status="live")
    res = app_client.post(
        "/api/ask-topai/live/end",
        json={
            "session_id": session_id,
            "transcript": [{"role": "user", "text": "hello"}, {"role": "assistant", "text": "hi"}],
        },
    )
    assert res.status_code == 200
    assert sessions.get_session(u1, session_id) is None


def test_live_tools_are_tenant_scoped(app_client, two_users):
    u1, u2 = two_users
    sarah_id, _ = _lead(u1, "Sarah Johnson", "3035559001")
    _login(app_client, u2)
    session_id = "live-tenant"
    sessions.save_session(u2, session_id, [], pending={"mode": "live"}, status="live")
    res = app_client.post(
        "/api/ask-topai/live/tools",
        json={
            "session_id": session_id,
            "calls": [
                {
                    "call_id": "call_other",
                    "name": "add_lead_note",
                    "arguments": {"lead_id": sarah_id, "note": "should not write"},
                }
            ],
            "transcript": "Add a note to Sarah",
        },
    ).get_json()
    assert res["results"][0]["output"]["ok"] is False
    assert "should not write" not in (db.get_lead(sarah_id, u1).get("notes") or "")


def test_widget_live_controls_and_no_secrets(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    html = app_client.get("/crm/leads").get_data(as_text=True)
    assert "Start Live Conversation" in html
    assert "End Conversation" in html
    assert "Ask TopAI — Live" in html
    assert "Connecting..." in html
    assert "Listening" in html
    assert "TopAI is speaking" in html
    assert "Working..." in html
    assert "Reconnecting..." in html
    assert "RTCPeerConnection" in html
    assert "response.cancel" in html
    assert "input_audio_buffer.speech_started" in html
    assert "OPENAI_API_KEY" not in html
    assert "ANTHROPIC_API_KEY" not in html
    assert ">Send<" in html


def test_fingerprint_reconnect_does_not_duplicate_create(two_users):
    u1, _ = two_users
    session_id = "live-fp"
    sessions.save_session(u1, session_id, [], pending={"mode": "live"}, status="live")
    call = {
        "call_id": "call_a",
        "name": "create_lead",
        "arguments": {"name": "Ada Lopez", "phone": "303-555-0100", "lead_type": "buyer"},
    }
    first = runtime.execute_calls(u1, session_id, [call], {}, transcript="Create Ada Lopez at 303-555-0100")
    retry = runtime.execute_calls(
        u1,
        session_id,
        [
            {
                "call_id": "call_b",
                "name": "create_lead",
                "arguments": {"name": "Ada Lopez", "phone": "303-555-0100", "lead_type": "buyer"},
            }
        ],
        {},
        transcript="Create Ada Lopez at 303-555-0100",
    )
    leads = [lead for lead in db.list_leads(u1, limit=20) if lead.get("name") == "Ada Lopez"]
    assert len(leads) == 1
    assert first["results"][0]["output"]["ok"] is True
    assert retry["results"][0]["output"].get("duplicate") is True
