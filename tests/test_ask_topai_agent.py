"""Ask TopAI Claude agent: tool loop, confirmation, tenant scope, no keyword router."""

import json

import config
import crm_db
import db
from ask_topai import agent, audit, registry, sessions, tools
from ask_topai.schemas import number_grounded
from tests.test_ask_topai import _as_complete, _lead, _login


class FakeResponse:
    def __init__(self, stop_reason, content):
        self.stop_reason = stop_reason
        self.content = content


def _tool(name, arguments, tool_id="t1"):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": arguments}


def _text(message):
    return {"type": "text", "text": message}


def _script(monkeypatch, *responses):
    queue = list(responses)
    calls = []

    def fake_claude(messages, *, system, tools_spec):
        calls.append({"messages": messages, "system": system, "tools": tools_spec})
        names = {item["name"] for item in tools_spec}
        assert "create_lead" in names
        assert "find_lead" in names
        assert "send_email" not in names
        if not queue:
            raise AssertionError("unexpected extra Claude round")
        return queue.pop(0)

    monkeypatch.setattr("ask_topai.agent.call_claude", fake_claude)
    return calls


def test_model_default_is_configurable(monkeypatch):
    monkeypatch.setattr(config, "ASK_TOPAI_MODEL", "")
    assert agent.model_name() == "claude-sonnet-5"
    monkeypatch.setattr(config, "ASK_TOPAI_MODEL", "claude-sonnet-5")
    assert agent.model_name() == "claude-sonnet-5"


def test_registry_exposes_phase1_not_future_tools():
    names = {item["name"] for item in registry.anthropic_tools()}
    assert names == registry.ENABLED_TOOLS
    for future in registry.FUTURE_TOOLS:
        assert future not in names
        assert registry.is_future_tool(future)
        assert not registry.is_enabled(future)


def test_thousand_price_is_grounded_in_transcript():
    assert number_grounded("Sarah can go up to 750 thousand", 750000)
    assert number_grounded("under $900,000", 900000)
    assert not number_grounded("remind me Friday", 750000)


def test_create_john_smith_plan(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    transcript = "Create John Smith at 303-555-1212 as a buyer."

    def fake(_text, _context):
        return {
            "status": "ok",
            "commands": [
                {
                    "action": "create_lead",
                    "arguments": {
                        "name": "John Smith",
                        "phone": "303-555-1212",
                        "lead_type": "buyer",
                    },
                }
            ],
        }

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(fake))
    res = app_client.post("/api/ask-topai/interpret", json={"text": transcript, "source": "text"})
    data = res.get_json()
    assert data["status"] == "executed"
    assert data["confirmation_token"] is None
    assert "John Smith" in (data.get("message") or "")
    leads = [lead for lead in db.list_leads(u1, limit=50) if lead.get("name") == "John Smith"]
    assert len(leads) == 1


def test_add_note_to_sarah_plan(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    sarah_id, _ = _lead(u1, "Sarah Johnson", "3035554101")

    def fake(_text, _context):
        return {
            "status": "ok",
            "commands": [
                {
                    "action": "add_lead_note",
                    "arguments": {"lead_name": "Sarah Johnson", "note": "Wants to tour Saturday."},
                }
            ],
        }

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(fake))
    data = app_client.post(
        "/api/ask-topai/interpret",
        json={"text": "Add a note to Sarah that she wants to tour Saturday."},
    ).get_json()
    assert data["status"] == "executed"
    assert "Saturday" in (db.get_lead(sarah_id, u1).get("notes") or "")


def test_remind_me_tomorrow_task_plan(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    sarah_id, _ = _lead(u1, "Sarah Johnson", "3035554102")

    def fake(_text, _context):
        return {
            "status": "ok",
            "commands": [
                {
                    "action": "create_task",
                    "arguments": {
                        "title": "Call Sarah about the listings",
                        "lead_name": "Sarah Johnson",
                        "due_date": "tomorrow",
                    },
                }
            ],
        }

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(fake))
    data = app_client.post(
        "/api/ask-topai/interpret",
        json={"text": "Remind me tomorrow to call Sarah about the listings."},
    ).get_json()
    assert data["status"] == "executed"
    tasks = [task for task in crm_db.list_tasks(u1) if task.get("lead_id") == sarah_id]
    assert len(tasks) == 1
    assert tasks[0]["due_at"]


def test_sarah_max_price_updates_criteria(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    sarah_id, _ = _lead(u1, "Sarah Johnson", "3035554103")
    db.update_lead_contact_fields(sarah_id, u1, property_interest="Denver condos")

    def fake(_text, _context):
        return {
            "status": "ok",
            "commands": [
                {
                    "action": "update_property_criteria",
                    "arguments": {"lead_name": "Sarah Johnson", "price_max": 900000},
                }
            ],
        }

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(fake))
    data = app_client.post(
        "/api/ask-topai/interpret",
        json={"text": "Sarah can go up to $900,000"},
    ).get_json()
    assert data["status"] == "executed"
    sarah = db.get_lead(sarah_id, u1)
    criteria = json.loads(sarah.get("property_criteria_json") or "{}")
    assert criteria.get("price_max") == 900000
    assert "Denver" in (sarah.get("property_interest") or "")


def test_multi_action_plan_defers_new_lead(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    transcript = (
        "Create Mike Johnson at 720-555-1234. He's a buyer looking for four bedrooms "
        "in Castle Rock under $900,000 and remind me tomorrow afternoon to call him."
    )

    def fake(_text, _context):
        return {
            "status": "ok",
            "commands": [
                {
                    "action": "create_lead",
                    "arguments": {
                        "name": "Mike Johnson",
                        "phone": "720-555-1234",
                        "lead_type": "buyer",
                    },
                },
                {
                    "action": "update_property_criteria",
                    "arguments": {
                        "lead_name": "Mike Johnson",
                        "bedrooms": 4,
                        "city": "Castle Rock",
                        "price_max": 900000,
                    },
                },
                {
                    "action": "create_task",
                    "arguments": {
                        "lead_name": "Mike",
                        "title": "Call Mike Johnson",
                        "due_date": "tomorrow",
                        "due_time": "afternoon",
                    },
                },
            ],
        }

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(fake))
    before_tasks = len(crm_db.list_tasks(u1))
    data = app_client.post("/api/ask-topai/interpret", json={"text": transcript}).get_json()
    assert data["status"] == "executed"
    assert [item["action"] for item in data["results"]] == [
        "create_lead",
        "update_property_criteria",
        "create_task",
    ]
    assert "Mike Johnson" in data["message"]
    leads = [lead for lead in db.list_leads(u1, limit=50) if "Mike" in (lead.get("name") or "")]
    assert len(leads) == 1
    criteria = json.loads(leads[0].get("property_criteria_json") or "{}")
    assert criteria.get("city") == "Castle Rock"
    assert criteria.get("bedrooms") == 4
    assert criteria.get("price_max") == 900000
    assert len(crm_db.list_tasks(u1)) == before_tasks + 1
    task = [item for item in crm_db.list_tasks(u1) if item.get("lead_id") == leads[0]["id"]][0]
    assert "Call" in (task.get("title") or "")


def test_selected_lead_context_resolves_pronoun(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    sarah_id, _ = _lead(u1, "Sarah Johnson", "3035554104")
    _lead(u1, "Ryan Serhant", "3035554105")

    def fake(_text, context):
        assert context.get("lead_id") == sarah_id
        return {
            "status": "ok",
            "commands": [
                {
                    "action": "add_lead_note",
                    "arguments": {"note": "She wants a finished basement."},
                }
            ],
        }

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(fake))
    data = app_client.post(
        "/api/ask-topai/interpret",
        json={
            "text": "Add that she wants a finished basement.",
            "context": {"page": f"/crm/leads/{sarah_id}", "lead_id": sarah_id},
        },
    ).get_json()
    assert data["status"] == "executed"
    assert "finished basement" in (db.get_lead(sarah_id, u1).get("notes") or "")


def test_ambiguous_lead_clarifies_without_mutation(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    a_id, _ = _lead(u1, "John Smith", "3035554106")
    b_id, _ = _lead(u1, "John Williams", "3035554107")

    def fake(_text, _context):
        return {
            "status": "ok",
            "commands": [
                {
                    "action": "add_lead_note",
                    "arguments": {"lead_name": "John", "note": "Tour Saturday."},
                }
            ],
        }

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(fake))
    data = app_client.post(
        "/api/ask-topai/interpret", json={"text": "Add a note to John that he wants to tour Saturday."}
    ).get_json()
    assert data["status"] == "clarification_required"
    assert data["confirmation_token"] is None
    assert "John Smith" in data["message"]
    assert "John Williams" in data["message"]
    assert "Tour Saturday" not in (db.get_lead(a_id, u1).get("notes") or "")
    assert "Tour Saturday" not in (db.get_lead(b_id, u1).get("notes") or "")


def test_clarification_continues_prior_create(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    session = "sess-jennifer"

    def fake(_user, transcript, context, session_id=None, source="text"):
        if "720" in transcript or "555-0194" in transcript:
            return {
                "status": "ok",
                "message": "Confirm creating Jennifer.",
                "commands": [
                    {
                        "action": "create_lead",
                        "arguments": {
                            "name": "Jennifer",
                            "phone": "720-555-0194",
                            "lead_type": "buyer",
                        },
                    }
                ],
                "tools_invoked": ["create_lead"],
                "model": "claude-sonnet-5",
                "session_id": session_id or session,
                "source": source,
                "choices": [],
                "grounding_transcript": "Create a buyer lead named Jennifer.\n720-555-0194.",
            }
        return {
            "status": "needs_clarification",
            "message": "What phone number or email should I use for Jennifer?",
            "commands": [],
            "tools_invoked": ["ask_clarification"],
            "model": "claude-sonnet-5",
            "session_id": session_id or session,
            "source": source,
            "choices": [],
            "grounding_transcript": transcript,
        }

    monkeypatch.setattr("ask_topai.agent.complete", fake)
    first = app_client.post(
        "/api/ask-topai/interpret",
        json={"text": "Create a buyer lead named Jennifer.", "session_id": session},
    ).get_json()
    assert first["status"] == "clarification_required"
    assert "phone" in first["message"].lower()
    assert first["confirmation_token"] is None
    assert db.list_leads(u1, limit=20) == []

    second = app_client.post(
        "/api/ask-topai/interpret",
        json={"text": "720-555-0194.", "session_id": session},
    ).get_json()
    assert second["status"] == "executed"
    assert "Jennifer" in (second.get("message") or "")
    leads = db.list_leads(u1, limit=20)
    assert len(leads) == 1
    assert leads[0]["name"] == "Jennifer"


def test_unsupported_email_is_understood_not_executed(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _lead(u1, "Sarah Johnson", "3035554108")

    def fake(_text, _context):
        return {
            "status": "unsupported",
            "message": (
                "I understand that you want to email Sarah listings, but Ask TopAI "
                "doesn't have email-sending permission yet."
            ),
            "commands": [],
        }

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(fake))
    data = app_client.post(
        "/api/ask-topai/interpret", json={"text": "Email Sarah these listings."}
    ).get_json()
    assert data["status"] == "unsupported_action"
    assert data["confirmation_token"] is None
    assert "email" in data["message"].lower()
    assert "doesn't have" in data["message"].lower() or "does not have" in data["message"].lower()


def test_claude_cannot_invoke_undefined_tools(two_users, monkeypatch):
    u1, _ = two_users
    _script(
        monkeypatch,
        FakeResponse("end_turn", [_tool("send_email", {"to": "sarah@example.com", "body": "hi"})]),
    )
    result = agent.complete(u1, "Email Sarah these listings.", {}, session_id="s-undef")
    assert result["status"] == "unsupported"
    assert result["commands"] == []
    assert "send_email" in result["tools_invoked"]
    assert db.list_leads(u1, limit=10) == []


def test_cross_account_lead_access_is_impossible(app_client, two_users, monkeypatch):
    u1, u2 = two_users
    sarah_id, _ = _lead(u1, "Sarah Johnson", "3035554109")
    found = tools.find_lead(u2, {"name": "Sarah Johnson"}, {})
    assert found["count"] == 0
    ctx = tools.get_lead_context(u2, {"lead_id": sarah_id}, {})
    assert ctx.get("error")

    def fake(_text, _context):
        return {
            "status": "ok",
            "commands": [
                {
                    "action": "add_lead_note",
                    "arguments": {"lead_id": sarah_id, "note": "cross account"},
                }
            ],
        }

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(fake))
    _login(app_client, u2)
    data = app_client.post(
        "/api/ask-topai/interpret",
        json={
            "text": "Add a note that she wants a tour.",
            "context": {"lead_id": sarah_id},
        },
    ).get_json()
    assert data["confirmation_token"] is None
    assert data["status"] in {"clarification_required", "error", "unsupported_action"}
    assert "cross account" not in (db.get_lead(sarah_id, u1).get("notes") or "")


def test_clarification_does_not_mutate(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)

    def fake(_text, _context):
        return {
            "status": "needs_clarification",
            "message": "What phone number should I use?",
            "commands": [],
        }

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(fake))
    app_client.post("/api/ask-topai/interpret", json={"text": "Create Pat Lee"})
    assert db.list_leads(u1, limit=20) == []


def test_repeated_send_does_not_duplicate(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)

    def fake(_text, _context):
        return {
            "status": "ok",
            "commands": [
                {"action": "create_task", "arguments": {"title": "Call the title company"}}
            ],
        }

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(fake))
    payload = {"text": "Give me a task to call the title company", "request_id": "req-title-1"}
    first = app_client.post("/api/ask-topai/interpret", json=payload).get_json()
    second = app_client.post("/api/ask-topai/interpret", json=payload).get_json()
    assert first["status"] == "executed"
    assert second.get("duplicate") is True
    matches = [task for task in crm_db.list_tasks(u1) if "title company" in (task.get("title") or "").lower()]
    assert len(matches) == 1


def test_claude_failure_creates_no_mutation(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)

    def boom(*_args, **_kwargs):
        return {
            "status": "error",
            "message": "Ask TopAI could not reach Claude. Your CRM data was not changed.",
            "commands": [],
            "tools_invoked": [],
            "model": "claude-sonnet-5",
            "session_id": "sess-fail",
            "source": "text",
            "choices": [],
        }

    monkeypatch.setattr("ask_topai.agent.complete", boom)
    data = app_client.post(
        "/api/ask-topai/interpret", json={"text": "Create a lead named Failure Case at 303-555-4111"}
    ).get_json()
    assert data["status"] == "error"
    assert data["confirmation_token"] is None
    assert db.list_leads(u1, limit=20) == []
    rows = audit.list_recent(u1)
    assert rows[0]["status"] == "error"


def test_voice_and_text_share_interpret_path(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    seen = []

    def fake(user_id, transcript, context, session_id=None, source="text"):
        seen.append(source)
        return {
            "status": "ok",
            "message": "plan",
            "commands": [
                {"action": "create_task", "arguments": {"title": "Follow up with the lender"}}
            ],
            "tools_invoked": ["create_task"],
            "model": "claude-sonnet-5",
            "session_id": session_id or "sess-src",
            "source": source,
            "choices": [],
            "grounding_transcript": transcript,
        }

    monkeypatch.setattr("ask_topai.agent.complete", fake)
    text = "Remind me to follow up with the lender"
    a = app_client.post("/api/ask-topai/interpret", json={"text": text, "source": "text"}).get_json()
    b = app_client.post("/api/ask-topai/interpret", json={"text": text, "source": "voice"}).get_json()
    assert a["status"] == b["status"] == "executed"
    assert seen == ["text", "voice"]
    assert [row["action"] for row in a["results"]] == [row["action"] for row in b["results"]]


def test_partial_failure_does_not_claim_full_success(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)

    def fake(_text, _context):
        return {
            "status": "ok",
            "commands": [
                {
                    "action": "create_lead",
                    "arguments": {"name": "Mike Johnson", "phone": "720-555-4112", "lead_type": "buyer"},
                },
                {"action": "create_task", "arguments": {"title": "Call Mike Johnson", "lead_name": "Mike Johnson"}},
            ],
        }

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(fake))
    monkeypatch.setattr(
        "ask_topai.actions.execute_create_task",
        lambda *_args, **_kwargs: (None, "task provider failed", None),
    )
    data = app_client.post(
        "/api/ask-topai/interpret",
        json={"text": "Create Mike Johnson at 720-555-4112 and remind me to call him."},
    ).get_json()
    body = data
    assert body["status"] == "partial"
    assert "Mike Johnson" in body["message"]
    assert "couldn't" in body["message"].lower() or "could not" in body["message"].lower()
    leads = [lead for lead in db.list_leads(u1, limit=20) if lead.get("name") == "Mike Johnson"]
    assert len(leads) == 1
    assert crm_db.list_tasks(u1) == []


def test_agent_loop_uses_read_then_queues_write(two_users, monkeypatch):
    u1, _ = two_users
    sarah_id, _ = _lead(u1, "Sarah Johnson", "3035554113")
    calls = _script(
        monkeypatch,
        FakeResponse("tool_use", [_tool("find_lead", {"name": "Sarah"}, "f1")]),
        FakeResponse(
            "end_turn",
            [
                _tool(
                    "update_property_criteria",
                    {"lead_id": sarah_id, "price_max": 750000},
                    "w1",
                )
            ],
        ),
    )
    result = agent.complete(
        u1,
        "Add that Sarah now wants to stay under 750 thousand",
        {},
        session_id="s-loop",
    )
    assert result["status"] == "ok"
    assert result["commands"][0]["action"] == "update_property_criteria"
    assert result["commands"][0]["arguments"]["price_max"] == 750000
    assert "find_lead" in result["tools_invoked"]
    assert len(calls) == 2
    assert db.get_lead(sarah_id, u1).get("property_criteria_json") in (None, "")


def test_session_state_is_tenant_scoped(two_users):
    u1, u2 = two_users
    sessions.save_session(
        u1,
        "same-key",
        [{"role": "user", "content": "secret for user one"}],
        status="clarifying",
    )
    assert sessions.load_messages(u2, "same-key") == []
    assert "secret" in sessions.conversation_transcript(sessions.load_messages(u1, "same-key"))


def test_widget_does_not_expose_anthropic_key(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    html = app_client.get("/crm/leads").get_data(as_text=True)
    assert "ANTHROPIC_API_KEY" not in html
    assert "sk-ant" not in html
    assert "OPENAI_API_KEY" not in html
    assert "Start Live Conversation" in html
    assert "session_id" in html
