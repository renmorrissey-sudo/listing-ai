"""Harness policy gate for Vapi voice tools (voice_tools.py, C1 fix).

The acceptance test proved Vapi bypassed the harness policy entirely: in
Drafts Only mode a real Vapi payload changed a lead's sms_consent_status
unverified -> verified and send_lead_email reached the provider. These
tests drive the same payload shape through handle_vapi_tool_calls and
assert the gate blocks / drafts / allows exactly as harness.policy.decide
says, with a harness_audit row for every gated decision.
"""

from datetime import datetime, timezone

import db
import voice_tools
from harness.audit import list_audit


def _set_settings(user_id, mode="drafts_only", paused=False, ai_enabled=True):
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO harness_settings
                (user_id, mode, paused, ai_enabled)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, mode, 1 if paused else 0, 1 if ai_enabled else 0),
        )


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
        for key in ("email", "sms_consent_status", "sms_sending_blocked", "next_action", "notes"):
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


def _voice_call(user_id, provider_call_id="vapi_gate"):
    now = datetime.now(timezone.utc).isoformat()
    with db.get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO voice_calls
                (user_id, persona_id, lead_id, provider, direction, lead_name,
                 phone_number, status, provider_call_id, created_at)
            VALUES (?, NULL, NULL, 'vapi', 'outbound', 'Voice Gate',
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
    assert "results" in response
    assert response["results"][0]["toolCallId"] == "tool_1"
    return response["results"][0]["result"]


def _audit_rows(user_id, tool, gate_decision=None):
    rows = [
        row for row in list_audit(user_id, limit=100)
        if row.get("tool") == tool
        and row.get("action") == "vapi_tool_call"
        and row.get("actor") == "ai"
    ]
    if gate_decision is not None:
        rows = [row for row in rows if row.get("gate_decision") == gate_decision]
    return rows


def test_consent_change_blocked_in_drafts_only(two_users):
    u1, _ = two_users
    call_id = _voice_call(u1)
    lead_id = _lead(
        u1,
        "Consent Lead",
        sms_consent_status="unverified",
        sms_sending_blocked=1,
    )

    result = _tool_result(
        voice_tools.handle_vapi_tool_calls(
            _tool_payload(
                call_id,
                "vapi_gate",
                "update_lead_sms_consent_status",
                {"lead_id": lead_id, "sms_consent_status": "SMS Verified"},
            )
        )
    )

    assert isinstance(result, str)
    assert "human approval" in result
    lead = db.get_lead(lead_id, u1)
    assert lead["sms_consent_status"] == "unverified"
    assert bool(lead["sms_sending_blocked"]) is True
    rows = _audit_rows(u1, "update_lead_sms_consent_status", "BLOCK")
    assert rows, "expected a harness_audit BLOCK row for the consent tool"
    assert rows[0]["target_id"] == lead_id
    assert rows[0]["mode"] == "drafts_only"


def test_email_send_blocked_in_drafts_only(two_users, monkeypatch):
    u1, _ = two_users
    call_id = _voice_call(u1)
    lead_id = _lead(u1, "Email Lead", email="lead@example.com")
    sent = []

    def fake_send(user_id, selected_lead_id, **kwargs):
        sent.append((user_id, selected_lead_id, kwargs))
        return {"ok": True, "to_email": "lead@example.com", "provider_status": "sent"}, None

    monkeypatch.setattr("voice_tools.send_lead_email", fake_send)

    result = _tool_result(
        voice_tools.handle_vapi_tool_calls(
            _tool_payload(
                call_id,
                "vapi_gate",
                "send_lead_email",
                {
                    "lead_id": lead_id,
                    "subject": "Showing follow-up",
                    "body": "Would you like to see the home this week?",
                },
            )
        )
    )

    assert isinstance(result, str)
    assert "human approval" in result
    assert sent == [], "send_lead_email must not reach the provider in drafts_only"
    rows = _audit_rows(u1, "send_lead_email", "BLOCK")
    assert rows, "expected a harness_audit BLOCK row for send_lead_email"


def test_list_open_leads_still_works_through_gate(two_users):
    u1, _ = two_users
    call_id = _voice_call(u1)
    _lead(u1, "Ada Buyer", status="new", phone="+13035550101")
    _lead(u1, "Ben Seller", status="qualified", phone="+13035550102")
    _lead(u1, "Closed Client", status="closed_won", phone="+13035550103")

    result = _tool_result(
        voice_tools.handle_vapi_tool_calls(
            _tool_payload(call_id, "vapi_gate", "list_open_leads")
        )
    )

    assert result["count"] == 2
    names = {lead["name"] for lead in result["leads"]}
    assert names == {"Ada Buyer", "Ben Seller"}
    rows = _audit_rows(u1, "list_open_leads", "ALLOW")
    assert rows, "expected a harness_audit ALLOW row for the read tool"


def test_routine_write_blocked_in_drafts_only_allowed_in_supervised(two_users):
    u1, _ = two_users
    call_id = _voice_call(u1)
    lead_id = _lead(u1, "Contact Lead", phone="+13035550105")
    args = {"lead_id": lead_id, "notes": "Asked for a CMA"}

    # drafts_only (default): no DB change, refusal, DRAFT audit row.
    result = _tool_result(
        voice_tools.handle_vapi_tool_calls(
            _tool_payload(call_id, "vapi_gate", "update_lead_contact_info", args)
        )
    )
    assert isinstance(result, str)
    assert "human approval" in result
    assert (db.get_lead(lead_id, u1).get("notes") or "") != "Asked for a CMA"
    assert _audit_rows(u1, "update_lead_contact_info", "DRAFT")

    # supervised: the same call applies the change.
    _set_settings(u1, mode="supervised")
    result = _tool_result(
        voice_tools.handle_vapi_tool_calls(
            _tool_payload(call_id, "vapi_gate", "update_lead_contact_info", args)
        )
    )
    assert isinstance(result, dict) and result.get("ok") is True
    assert db.get_lead(lead_id, u1)["notes"] == "Asked for a CMA"
    assert _audit_rows(u1, "update_lead_contact_info", "ALLOW")


def test_closed_won_blocked_in_authorized_mode(two_users):
    u1, _ = two_users
    _set_settings(u1, mode="authorized")
    call_id = _voice_call(u1)
    lead_id = _lead(u1, "Status Lead")

    result = _tool_result(
        voice_tools.handle_vapi_tool_calls(
            _tool_payload(
                call_id,
                "vapi_gate",
                "update_lead_status",
                {"lead_id": lead_id, "status": "closed_won"},
            )
        )
    )

    assert result == "That status cannot be applied automatically."
    assert db.get_lead(lead_id, u1)["status"] == "new"
    rows = _audit_rows(u1, "update_lead_status", "BLOCK")
    assert rows, "expected a harness_audit BLOCK row for closed_won"

    # A routine status is still allowed in authorized mode.
    result = _tool_result(
        voice_tools.handle_vapi_tool_calls(
            _tool_payload(
                call_id,
                "vapi_gate",
                "update_lead_status",
                {"lead_id": lead_id, "status": "qualified"},
            )
        )
    )
    assert isinstance(result, dict) and result.get("ok") is True
    assert db.get_lead(lead_id, u1)["status"] == "qualified"


def test_draft_lead_email_still_creates_draft_in_drafts_only(two_users):
    u1, _ = two_users
    call_id = _voice_call(u1)
    lead_id = _lead(u1, "Email Lead", email="lead@example.com")

    result = _tool_result(
        voice_tools.handle_vapi_tool_calls(
            _tool_payload(
                call_id,
                "vapi_gate",
                "draft_lead_email",
                {
                    "lead_id": lead_id,
                    "subject": "Checking in",
                    "body": "Hi, are you still interested in touring homes this week?",
                },
            )
        )
    )

    assert isinstance(result, dict) and result.get("ok") is True
    import crm_db

    activities = crm_db.list_lead_activities(u1, lead_id)
    assert activities[0]["event_type"] == "email_draft_created"
    assert "Checking in" in activities[0]["summary"]
    rows = _audit_rows(u1, "draft_lead_email", "DRAFT")
    assert rows, "expected a harness_audit DRAFT row for the draft tool"


def test_unknown_tool_keeps_existing_behaviour(two_users):
    u1, _ = two_users
    call_id = _voice_call(u1)
    result = _tool_result(
        voice_tools.handle_vapi_tool_calls(
            _tool_payload(call_id, "vapi_gate", "not_a_real_tool", {})
        )
    )
    assert result == "Unknown TopAI voice tool: not_a_real_tool."
    assert _audit_rows(u1, "not_a_real_tool") == []
