"""Ask TopAI Phase 1: interpret/confirm, tenant scope, no silent overwrite."""

import json

import crm_db
import db
from ask_topai import audit, service
from ask_topai.parser import validate_model_payload
from lead_service import upsert_crm_lead


def _as_complete(fake_llm):
    """Adapt old (transcript, context) stubs to agent.complete()."""

    def complete(user_id, transcript, context, session_id=None, source="text"):
        payload = fake_llm(transcript, context)
        payload.setdefault("tools_invoked", [c.get("action") for c in payload.get("commands") or []])
        payload.setdefault("model", "claude-sonnet-5")
        payload.setdefault("session_id", session_id or "sess-test")
        payload.setdefault("source", source)
        payload.setdefault("choices", [])
        payload.setdefault("grounding_transcript", transcript)
        return payload

    return complete


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["session_version"] = 1


def _lead(user_id, name, phone):
    lead_id, _, lead = upsert_crm_lead(user_id, phone, {"lead_name": name})
    return lead_id, lead


AUTHENTICATED_PAGES = [
    "/dashboard?local_date=2026-08-17&tz_offset_minutes=0",
    "/crm/leads",
    "/crm/tasks",
    "/crm/follow-ups",
    "/app",
    "/billing",
    "/tutorial",
    "/social/connections",
    "/integrations/email-marketing",
    "/listings/archive",
]


def _assert_ask_topai_widget(html, path):
    assert html.count('id="ask-topai-fab"') == 1, path
    assert html.count('id="ask-topai-root"') == 1, path
    assert "Ask TopAI" in html, path
    assert "ask-topai-fab-label" in html, path
    assert "Start Live Conversation" in html, path
    assert "End Conversation" in html, path
    assert 'id="ask-topai-text"' in html, path
    assert "/api/ask-topai/interpret" in html, path
    assert "/api/ask-topai/live/session" in html, path
    assert "/api/ask-topai/live/webrtc" in html, path
    assert "RTCPeerConnection" in html, path
    assert ">Send<" in html, path
    assert "Ask TopAI is working..." in html, path
    assert "session_id" in html, path
    assert "clarification_required" in html, path
    assert "z-index: 10050" in html, path
    assert "document.body.appendChild" in html, path
    assert "OPENAI_API_KEY" not in html, path
    assert "SpeechRecognition" not in html, path


def test_ask_topai_is_globally_mounted_on_authenticated_pages(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    for path in AUTHENTICATED_PAGES:
        res = app_client.get(path)
        assert res.status_code == 200, path
        _assert_ask_topai_widget(res.get_data(as_text=True), path)


def test_ask_topai_not_on_public_pages(app_client):
    for path in ("/", "/login", "/features", "/pricing"):
        html = app_client.get(path).get_data(as_text=True)
        assert 'id="ask-topai-fab"' not in html, path


def test_ask_topai_floats_from_shared_header_not_hidden_app_footer(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    html = app_client.get("/app").get_data(as_text=True)
    fab_idx = html.find('id="ask-topai-fab"')
    footer_idx = html.find('id="subscriber-footer"')
    assert fab_idx != -1
    assert footer_idx != -1
    assert fab_idx < footer_idx


def test_text_fallback_reaches_interpret_endpoint(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    html = app_client.get("/crm/leads").get_data(as_text=True)
    assert "fetch('/api/ask-topai/interpret'" in html

    def fake_llm(_text, _context):
        return {
            "status": "ok",
            "commands": [
                {
                    "action": "create_lead",
                    "arguments": {"name": "Ada Lopez", "phone": "303-555-0100"},
                }
            ],
        }

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(fake_llm))
    res = app_client.post(
        "/api/ask-topai/interpret",
        json={"text": "Create a lead named Ada Lopez at 303-555-0100"},
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] == "executed"
    assert data["confirmation_token"] is None
    assert "Ada Lopez" in (data.get("message") or "")
    leads = [lead for lead in db.list_leads(u1, limit=100) if lead.get("name") == "Ada Lopez"]
    assert len(leads) == 1


def test_unauthenticated_interpret_is_rejected(app_client):
    res = app_client.post("/api/ask-topai/interpret", json={"text": "Create a lead"})
    assert res.status_code == 401


def test_create_lead_executes_on_send(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    transcript = (
        "Create a new buyer lead named John Smith. His number is 303-555-1212. "
        "He wants a four-bedroom home in Highlands Ranch under $900,000."
    )

    def fake_llm(_text, _context):
        return {
            "status": "ok",
            "commands": [
                {
                    "action": "create_lead",
                    "arguments": {
                        "name": "John Smith",
                        "phone": "303-555-1212",
                        "lead_type": "buyer",
                        "bedrooms": 4,
                        "city": "Highlands Ranch",
                        "price_max": 900000,
                    },
                }
            ],
        }

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(fake_llm))
    interpreted = app_client.post("/api/ask-topai/interpret", json={"text": transcript})
    assert interpreted.status_code == 200
    data = interpreted.get_json()
    assert data["status"] == "executed"
    assert data["confirmation_token"] is None
    assert "Lead created: John Smith" in data["message"]
    leads = [lead for lead in db.list_leads(u1, limit=100) if lead["phone_number"] == "+13035551212"]
    assert len(leads) == 1
    assert leads[0]["name"] == "John Smith"


def test_duplicate_phone_does_not_overwrite(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    sarah_id, _ = _lead(u1, "Sarah Johnson", "3035551212")
    db.update_lead_contact_fields(sarah_id, u1, notes="Open house visitor")

    def fake_llm(_text, _context):
        return {
            "status": "ok",
            "commands": [
                {
                    "action": "create_lead",
                    "arguments": {"name": "Ryan Serhant", "phone": "+13035551212"},
                }
            ],
        }

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(fake_llm))
    res = app_client.post(
        "/api/ask-topai/interpret",
        json={"text": "Create Ryan Serhant at +13035551212"},
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] == "clarification_required"
    assert "Sarah Johnson" in data["message"]
    assert data["confirmation_token"] is None
    sarah = db.get_lead(sarah_id, u1)
    assert sarah["name"] == "Sarah Johnson"
    assert "Open house visitor" in (sarah.get("notes") or "")
    assert not any(lead["name"] == "Ryan Serhant" for lead in db.list_leads(u1, limit=100))


def test_add_note_uses_current_lead_context(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    sarah_id, _ = _lead(u1, "Sarah Johnson", "3038703107")
    ryan_id, _ = _lead(u1, "Ryan Serhant", "3038703106")

    def fake_llm(_text, _context):
        return {
            "status": "ok",
            "commands": [{"action": "add_lead_note", "arguments": {"note": "She wants to see the property Saturday."}}],
        }

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(fake_llm))
    res = app_client.post(
        "/api/ask-topai/interpret",
        json={
            "text": "Add a note that she wants to see the property Saturday.",
            "context": {"page": f"/crm/leads/{sarah_id}", "lead_id": sarah_id},
        },
    )
    data = res.get_json()
    assert data["status"] == "executed"
    assert "Note added to Sarah Johnson" in data["message"]
    sarah = db.get_lead(sarah_id, u1)
    ryan = db.get_lead(ryan_id, u1)
    assert "Saturday" in (sarah.get("notes") or "")
    assert "Saturday" not in (ryan.get("notes") or "")


def test_ambiguous_lead_name_does_not_guess(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    john1, _ = _lead(u1, "John Smith", "3035551001")
    john2, _ = _lead(u1, "John Adams", "3035551002")

    def fake_llm(_text, _context):
        return {
            "status": "ok",
            "commands": [{"action": "add_lead_note", "arguments": {"lead_name": "John", "note": "Call back"}}],
        }

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(fake_llm))
    res = app_client.post("/api/ask-topai/interpret", json={"text": "Add a note to John: Call back"})
    data = res.get_json()
    assert data["status"] == "clarification_required"
    assert "multiple leads named John" in data["message"]
    assert data["confirmation_token"] is None
    assert "Call back" not in (db.get_lead(john1, u1).get("notes") or "")
    assert "Call back" not in (db.get_lead(john2, u1).get("notes") or "")


def test_create_task_creates_exactly_one(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    sarah_id, _ = _lead(u1, "Sarah Johnson", "3035552001")

    def fake_llm(_text, _context):
        return {
            "status": "ok",
            "commands": [
                {
                    "action": "create_task",
                    "arguments": {
                        "title": "Prepare CMA for Sarah Johnson",
                        "lead_name": "Sarah Johnson",
                        "due_date": "tomorrow",
                    },
                }
            ],
        }

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(fake_llm))
    before = len(crm_db.list_tasks(u1))
    interpreted = app_client.post(
        "/api/ask-topai/interpret",
        json={"text": "Create a task to prepare a CMA for Sarah Johnson tomorrow."},
    )
    assert interpreted.get_json()["status"] == "executed"
    tasks = crm_db.list_tasks(u1)
    assert len(tasks) == before + 1
    created = [task for task in tasks if "CMA" in (task.get("title") or "")]
    assert len(created) == 1
    assert created[0]["lead_id"] == sarah_id
    assert created[0]["due_at"]


def test_property_criteria_updates_intended_lead_only(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    sarah_id, _ = _lead(u1, "Sarah Johnson", "3035553001")
    ryan_id, _ = _lead(u1, "Ryan Serhant", "3035553002")
    db.update_lead_contact_fields(ryan_id, u1, property_interest="Manhattan penthouse")

    def fake_llm(_text, _context):
        return {
            "status": "ok",
            "commands": [
                {
                    "action": "update_property_criteria",
                    "arguments": {
                        "lead_name": "Sarah Johnson",
                        "bedrooms": 3,
                        "property_type": "townhomes",
                        "city": "Littleton",
                        "price_max": 650000,
                    },
                }
            ],
        }

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(fake_llm))
    interpreted = app_client.post(
        "/api/ask-topai/interpret",
        json={"text": "Sarah wants 3-bedroom townhomes in Littleton under $650,000."},
    )
    assert interpreted.get_json()["status"] == "executed"
    sarah = db.get_lead(sarah_id, u1)
    ryan = db.get_lead(ryan_id, u1)
    assert "Littleton" in (sarah.get("property_interest") or "")
    assert "650" in (sarah.get("property_interest") or "")
    assert ryan.get("property_interest") == "Manhattan penthouse"


def test_missing_phone_asks_instead_of_inventing(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)

    def fake_llm(_text, _context):
        return {
            "status": "ok",
            "commands": [
                {
                    "action": "create_lead",
                    "arguments": {"name": "Michael", "phone": "9995550000"},
                }
            ],
        }

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(fake_llm))
    res = app_client.post("/api/ask-topai/interpret", json={"text": "Create a lead named Michael."})
    data = res.get_json()
    assert data["status"] == "clarification_required"
    assert "phone" in data["message"].lower()
    assert data["confirmation_token"] is None
    assert db.list_leads(u1, limit=20) == []


def test_unsupported_and_arbitrary_actions_rejected(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)

    def send_sms(_text, _context):
        return {"status": "ok", "commands": [{"action": "send_sms", "arguments": {"body": "hi"}}]}

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(send_sms))
    unsupported = app_client.post("/api/ask-topai/interpret", json={"text": "Text Sarah that I am running late"})
    assert unsupported.get_json()["status"] == "unsupported_action"
    assert unsupported.get_json()["confirmation_token"] is None

    def drop_table(_text, _context):
        return {"status": "ok", "commands": [{"action": "drop_table", "arguments": {"name": "leads"}}]}

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(drop_table))
    arbitrary = app_client.post("/api/ask-topai/interpret", json={"text": "Drop the leads table"})
    assert arbitrary.get_json()["status"] == "unsupported_action"

    payload = validate_model_payload(
        {"action": "execute_sql", "arguments": {"sql": "DELETE FROM leads"}},
        "execute sql",
    )
    assert payload["status"] == "unsupported"


def test_executed_command_is_audited(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)

    def fake_llm(_text, _context):
        return {
            "status": "ok",
            "commands": [{"action": "create_task", "arguments": {"title": "Call Ryan"}}],
        }

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(fake_llm))
    interpreted = app_client.post(
        "/api/ask-topai/interpret",
        json={"text": "Remind me to call Ryan", "source": "voice"},
    )
    assert interpreted.get_json()["status"] == "executed"
    rows = audit.list_recent(u1)
    assert rows
    assert rows[0]["source"] == "ask_topai"
    assert rows[0]["status"] == "executed"
    assert "call Ryan" in (rows[0].get("transcript") or "")
    interpreted_json = json.loads(rows[0]["interpreted_json"])
    assert interpreted_json["commands"][0]["action"] == "create_task"
    assert rows[0].get("model") == "claude-sonnet-5"
    assert rows[0].get("input_source") == "voice"


def test_client_cannot_inject_model_payload(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)

    def fake_llm(text, _context):
        return {
            "status": "needs_clarification",
            "message": "I need a bit more information.",
            "commands": [],
        }

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(fake_llm))
    res = app_client.post(
        "/api/ask-topai/interpret",
        json={
            "text": "hello",
            "model_payload": {
                "status": "ok",
                "commands": [{"action": "create_lead", "arguments": {"name": "Hack", "phone": "3035551212"}}],
            },
        },
    )
    assert res.get_json()["status"] == "clarification_required"
    assert db.list_leads(u1, limit=20) == []
