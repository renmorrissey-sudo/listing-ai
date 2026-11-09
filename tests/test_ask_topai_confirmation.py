"""Ask TopAI confirmation-token removal pins + future-tool classification.

Removed in Phase I hardening (2026-11-08) — see
``ask_topai/parser.py`` module docstring for the full reasoning:

* ``service.interpret()`` executes routine Phase 1 commands
  immediately and returns NO ``confirmation_token`` field at all.
* The confirmation-token flow could never succeed and was removed:
  no path ever issued or stored a token (no ``pending_confirmation``
  row was ever created), so ``service.confirm()`` could only ever
  return 400; nothing outside tests called ``issue_confirmation_token``,
  ``/api/ask-topai/confirm``, or ``/api/ask-topai/cancel`` (the
  cancel route returned success unconditionally while cancelling
  nothing); the widget never read the field. Removed:
  ``service.confirm()``, both routes, ``audit.issue_confirmation_token``,
  ``audit.get_by_token``, ``audit.get_pending_by_token``, and the
  ``confirmation_token`` / ``expires_at`` parameters of
  ``audit.record_command()``.
* The harness human-approval flow (``harness.approvals``) is the
  approval flow that actually executes, and is unaffected by this
  removal. Any future Ask TopAI confirmation step must be built on
  it, not by resurrecting this token flow.

Also pins the future-tool classification in ask_topai/registry.py:
``create_cma`` and ``generate_listing_content`` CREATE content, so
they are future WRITES — never routine reads — even though
``harness/tools.py`` still classifies them read/routine today (live
fix belongs there; see the registry.py note).
"""

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
    # The field is gone entirely — not None, not present.
    assert "confirmation_token" not in data
    # The write really happened immediately — no confirmation step.
    import db

    leads = [
        lead for lead in db.list_leads(u1, limit=50) if lead.get("name") == "Confirm Pin"
    ]
    assert len(leads) == 1


def test_confirmation_token_flow_is_removed(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    # No issuer, no token lookup, no confirm entry point.
    assert not hasattr(audit, "issue_confirmation_token")
    assert not hasattr(audit, "get_by_token")
    assert not hasattr(audit, "get_pending_by_token")
    assert not hasattr(service, "confirm")
    # The routes are gone (404), not stubs returning 400/200.
    assert (
        app_client.post(
            "/api/ask-topai/confirm", json={"confirmation_token": "anything"}
        ).status_code
        == 404
    )
    assert (
        app_client.post(
            "/api/ask-topai/cancel", json={"confirmation_token": "anything"}
        ).status_code
        == 404
    )


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
