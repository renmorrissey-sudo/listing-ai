"""Ask TopAI confirmation-token design + future-tool classification pins.

Documented (not removed) — see ask_topai/parser.py module docstring:

* ``service.interpret()`` executes routine Phase 1 commands
  immediately and always returns ``confirmation_token: None``. The
  ``confirmation_token`` field stays in the API response shape for
  forward compatibility; it is not a bug that it is None.
* ``audit.issue_confirmation_token()`` mints a token for a FUTURE
  confirmation flow. It has zero callers in non-test code today, by
  design: no path creates a ``pending_confirmation`` row. It is kept
  because ``service.confirm()`` + ``/api/ask-topai/confirm`` are
  already wired to token lookup. Removing it (or the None field)
  would break that future flow and the API shape.
* The harness human-approval flow (``harness.approvals``) is separate
  and unaffected by this token flow.

Also pins the future-tool classification in ask_topai/registry.py:
``create_cma`` and ``generate_listing_content`` CREATE content, so
they are future WRITES — never routine reads — even though
``harness/tools.py`` still classifies them read/routine today (live
fix belongs there; see the registry.py note).
"""

from datetime import datetime, timedelta, timezone

from ask_topai import audit, registry, service
from tests.test_ask_topai import _as_complete, _login


def test_interpret_executes_immediately_with_no_confirmation_token(
    app_client, two_users, monkeypatch
):
    u1, _ = two_users
    _login(app_client, u1)

    def fake(_text, _context):
        return {
            "status": "ok",
            "commands": [
                {
                    "action": "create_lead",
                    "arguments": {
                        "name": "Confirm Pin",
                        "phone": "303-555-0199",
                        "lead_type": "buyer",
                    },
                }
            ],
        }

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(fake))
    data = app_client.post(
        "/api/ask-topai/interpret",
        json={"text": "Create Confirm Pin at 303-555-0199 as a buyer."},
    ).get_json()
    assert data["status"] == "executed"
    assert data["confirmation_token"] is None
    # The write really happened immediately — no confirmation step.
    import db

    leads = [
        lead for lead in db.list_leads(u1, limit=50) if lead.get("name") == "Confirm Pin"
    ]
    assert len(leads) == 1


def test_issue_confirmation_token_is_never_called_by_interpret(
    app_client, two_users, monkeypatch
):
    u1, _ = two_users
    _login(app_client, u1)
    calls = []

    def spy_issue():
        calls.append(True)
        raise AssertionError(
            "issue_confirmation_token must not be called by interpret()"
        )

    # Spy on the issuer through the audit module attribute interpret
    # would have to call; the real issuer is exercised separately below.
    monkeypatch.setattr(audit, "issue_confirmation_token", spy_issue)

    def fake(_text, _context):
        return {
            "status": "ok",
            "commands": [
                {
                    "action": "create_lead",
                    "arguments": {
                        "name": "Spy Pin",
                        "phone": "303-555-0188",
                        "lead_type": "buyer",
                    },
                }
            ],
        }

    monkeypatch.setattr("ask_topai.agent.complete", _as_complete(fake))
    data = app_client.post(
        "/api/ask-topai/interpret",
        json={"text": "Create Spy Pin at 303-555-0188 as a buyer."},
    ).get_json()
    assert data["status"] == "executed"
    assert calls == []


def test_issue_confirmation_token_still_mints_a_usable_token():
    token, expires = audit.issue_confirmation_token()
    assert isinstance(token, str) and len(token) >= 32
    assert token != audit.issue_confirmation_token()[0]
    expiry = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    delta = expiry - datetime.now(timezone.utc)
    assert timedelta(minutes=14) < delta <= timedelta(minutes=15, seconds=30)


def test_confirm_with_an_unissued_token_is_rejected(two_users):
    u1, _ = two_users
    token, _expires = audit.issue_confirmation_token()
    # Issued but never stored (no pending_confirmation row exists):
    # confirm() must reject it — the token alone grants nothing.
    assert audit.get_by_token(u1, token) is None
    body, status = service.confirm(u1, token)
    assert status == 400
    assert body["ok"] is False


def test_create_cma_and_generate_listing_content_are_future_writes():
    assert "create_cma" in registry.FUTURE_TOOLS
    assert "generate_listing_content" in registry.FUTURE_TOOLS
    assert "create_cma" in registry.FUTURE_WRITE_TOOLS
    assert "generate_listing_content" in registry.FUTURE_WRITE_TOOLS
    assert "create_cma" not in registry.READ_TOOLS
    assert "generate_listing_content" not in registry.READ_TOOLS
    assert "create_cma" not in registry.WRITE_TOOLS  # not enabled yet
    assert registry.future_tool_kind("create_cma") == "write"
    assert registry.future_tool_kind("generate_listing_content") == "write"
    assert registry.future_tool_kind("find_matching_listings") == "read"
    assert registry.future_tool_kind("send_email") == "write"
    assert registry.future_tool_kind("create_lead") is None  # already enabled


def test_future_tools_are_not_enabled_tools():
    for name in registry.FUTURE_WRITE_TOOLS | registry.FUTURE_READ_TOOLS:
        assert registry.is_future_tool(name)
        assert not registry.is_enabled(name)
        assert not registry.is_write_tool(name)
        assert not registry.is_read_tool(name)
