"""Custom SMS reply drafts (Claude) — AI SMS Assistant drafting only.

Ren's spec (14 criteria, PM-assigned Nov 25, 2026; design sketch approved,
build authorized). A "Draft reply with Claude" button drafts a reply to a
lead's incoming SMS; the draft lands in the editable composer labelled
"AI draft" and is sent only when the agent clicks Send, through the
existing send path. Generating never sends, never saves a template, never
creates an approval. Automatic sending of custom replies is OUT of scope.

Red-before-green note (honest counts, see the build report): tests 1-7,
15-18 exercise the NEW endpoint / prompt variant / page copy, so they fail
before the build and pass after. Tests 8-11 pin the EXISTING send path the
feature reuses unchanged (it already refuses opted-out sends and already
sends agent-reviewed text verbatim without a template), so they pass
before the build too — they are regression guards, not new-behaviour
proofs. Tests 12-14 likewise pin existing behaviour and pass pre-build.

One correction to the sketch's test 9: the route-level 403 it describes
cannot occur for a merely-uncertified lead, because
``send_authorized_sms`` creates the agent's one-to-one attestation inline
(the agent's compliance certification IS the consent evidence for a
manual one-to-one send). The refusal is therefore proven at the gate
level (``can_send_sms`` with no attestation record -> ``NO_CERT_MSG``)
plus the route-level refusal when the agent does NOT certify compliance.
See the build report.
"""

import json
import uuid
from unittest.mock import patch

import pytest

import config
import db
from sms_authorization import NO_CERT_MSG

_DRAFT_TEXT = "Hi Sarah, thanks for asking — I'll check on the Maple Street house and get back to you shortly."
_EDITED_TEXT = "Hi Sarah, thanks for asking — I checked, and the Maple Street house is still available. Would Friday at 3pm work for a tour?"
_FLAGGED_DRAFT = "Happy to discuss pricing — I'll have the agent follow up on the numbers with you."
_INBOUND_BODY = "Hi, is the house on Maple Street still available, and could I tour it Friday afternoon?"

_OLD_TEMPLATES_DESCRIPTION = (
    "Agent-approved outbound messages. The AI may use an approved template verbatim"
)
_NEW_TEMPLATES_DESCRIPTION = (
    "Approved templates are used for automated outreach. In the AI SMS Assistant, "
    "Claude can draft custom replies to incoming messages for an agent to review, "
    "edit, and send. Editing an approved template returns it to draft until it is "
    "approved again."
)


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["session_version"] = 1


def _unique_phone(prefix="303"):
    n = int(uuid.uuid4().hex[:8], 16) % 10_000_000
    return f"+1{prefix}{n:07d}"[:12]


def _make_lead(user_id, phone=None, name="Draft Test Lead"):
    """A consenting lead (same shape as the authorized E2E tests)."""
    from lead_service import upsert_crm_lead

    phone = phone or _unique_phone()
    lead_id, _, _ = upsert_crm_lead(
        user_id, phone, {"name": name, "phone_number": phone},
        source="test", touch_sms=True, assigned_user_id=user_id,
    )
    with db.get_db() as conn:
        conn.execute(
            "UPDATE leads SET sms_consent_status = ?, opt_out_status = ? "
            "WHERE id = ? AND user_id = ?",
            ("verified", "active", lead_id, user_id),
        )
    return lead_id, phone


def _make_inbound(user_id, lead_id, phone, body):
    return db.create_sms_message(
        user_id, None, "telnyx",
        {"phone_number": phone, "message_body": body},
        status="received", lead_id=lead_id, direction="inbound",
    )


def _persona(user_id):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM voice_personas WHERE user_id IS NULL OR user_id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
    return row["id"]


def _telnyx_ready(monkeypatch, *, verification="verified", trial=False):
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY")
    monkeypatch.setattr(config, "TELNYX_MESSAGING_PROFILE_ID", "profile-1")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "+18888210810")
    monkeypatch.setattr(config, "TELNYX_PUBLIC_KEY", "pk-test")
    monkeypatch.setattr(config, "TELNYX_TRIAL_MODE", trial)
    monkeypatch.setattr(config, "TELNYX_TOLL_FREE_VERIFICATION_STATUS", verification)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 0)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 0)
    monkeypatch.setattr(config, "APP_URL", "https://topairealestatetools.com")


def _coach_dict(draft, topics=None, template_id=None):
    return {
        "summary": "Lead asked a question.",
        "intent": "question",
        "draft_reply": draft,
        "suggested_reply": draft,
        "sensitive_topic": bool(topics),
        "requires_manual_review": bool(topics),
        "escalation_topics": list(topics or []),
        "suggested_lead_status": "contacted",
        "appointment_details": None,
        "appointment_requested": False,
        "template_id": template_id,
        "confidence": 0.9,
    }


def _mock_coach(monkeypatch, response):
    monkeypatch.setattr(
        "sms_coach.analyze_inbound_reply", lambda *a, **k: dict(response)
    )


def _row_counts(user_id):
    with db.get_db() as conn:
        out = {}
        for table in (
            "sms_messages",
            "harness_approvals",
            "harness_message_templates",
            "harness_outreach_ledger",
        ):
            out[table] = conn.execute(
                f"SELECT COUNT(*) AS c FROM {table} WHERE user_id = ?", (user_id,)
            ).fetchone()["c"]
        return out


def _outbound_rows(user_id, lead_id):
    with db.get_db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM sms_messages WHERE user_id = ? AND lead_id = ? "
                "AND direction = 'outbound' ORDER BY id",
                (user_id, lead_id),
            ).fetchall()
        ]


def _draft_call(client, lead_id):
    return client.post("/sms/draft-reply", json={"lead_id": lead_id})


# --- Criterion 4 / 11(b): generating creates nothing --------------------------


def test_draft_endpoint_creates_no_rows(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    lead_id, phone = _make_lead(u1)
    _make_inbound(u1, lead_id, phone, _INBOUND_BODY)
    _mock_coach(monkeypatch, _coach_dict(_DRAFT_TEXT))
    before = _row_counts(u1)
    with patch(
        "sms_providers.telnyx.TelnyxSMSProvider.send_message"
    ) as send_mock:
        res = _draft_call(app_client, lead_id)
    assert res.status_code == 200, res.get_json()
    data = res.get_json()
    assert data["draft_reply"] == _DRAFT_TEXT
    assert data["lead_id"] == lead_id
    assert _row_counts(u1) == before
    assert not send_mock.called
    assert _outbound_rows(u1, lead_id) == []


def test_draft_regenerate_creates_no_rows(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    lead_id, phone = _make_lead(u1)
    _make_inbound(u1, lead_id, phone, _INBOUND_BODY)
    _mock_coach(monkeypatch, _coach_dict(_DRAFT_TEXT))
    assert _draft_call(app_client, lead_id).status_code == 200
    before = _row_counts(u1)
    res = _draft_call(app_client, lead_id)
    assert res.status_code == 200
    assert res.get_json()["draft_reply"] == _DRAFT_TEXT
    assert _row_counts(u1) == before


def test_draft_response_is_not_an_approval(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    lead_id, phone = _make_lead(u1)
    _make_inbound(u1, lead_id, phone, _INBOUND_BODY)
    _mock_coach(monkeypatch, _coach_dict(_DRAFT_TEXT))
    res = _draft_call(app_client, lead_id)
    assert res.status_code == 200
    data = res.get_json()
    assert "approval_id" not in data
    assert "approval" not in data
    # template_id is informational only — no template was used here.
    assert data["template_id"] is None
    assert data["template_name"] is None
    assert _row_counts(u1)["harness_approvals"] == 0


# --- Criterion 7: flags -------------------------------------------------------


def test_draft_returns_flag_negotiation(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    lead_id, phone = _make_lead(u1)
    _make_inbound(u1, lead_id, phone, "Would you take $480k for the house?")
    _mock_coach(monkeypatch, _coach_dict(_FLAGGED_DRAFT, topics=["negotiation"]))
    res = _draft_call(app_client, lead_id)
    assert res.status_code == 200
    data = res.get_json()
    assert data["escalation_topics"] == ["negotiation"]
    assert data["sensitive_topic"] is True
    assert data["requires_manual_review"] is True
    assert "negotiation" not in data["draft_reply"].lower()
    assert "flag" not in data["draft_reply"].lower()


def test_draft_returns_flag_financing(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    lead_id, phone = _make_lead(u1)
    _make_inbound(u1, lead_id, phone, "What mortgage rate can I get?")
    _mock_coach(monkeypatch, _coach_dict(_FLAGGED_DRAFT, topics=["financing"]))
    data = _draft_call(app_client, lead_id).get_json()
    assert data["escalation_topics"] == ["financing"]
    assert data["sensitive_topic"] is True
    assert data["requires_manual_review"] is True
    assert "financing" not in data["draft_reply"].lower()


def test_draft_returns_flag_fair_housing_demographics(
    app_client, two_users, monkeypatch
):
    u1, _ = two_users
    _login(app_client, u1)
    lead_id, phone = _make_lead(u1)
    _make_inbound(u1, lead_id, phone, "What kind of people live in that neighborhood?")
    _mock_coach(monkeypatch, _coach_dict(_FLAGGED_DRAFT, topics=["fair_housing"]))
    data = _draft_call(app_client, lead_id).get_json()
    assert data["escalation_topics"] == ["fair_housing"]
    assert data["sensitive_topic"] is True
    assert data["requires_manual_review"] is True
    assert "fair_housing" not in data["draft_reply"]
    assert "fair housing" not in data["draft_reply"].lower()


def test_draft_no_flag_when_clean(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    lead_id, phone = _make_lead(u1)
    _make_inbound(u1, lead_id, phone, _INBOUND_BODY)
    _mock_coach(monkeypatch, _coach_dict(_DRAFT_TEXT))
    data = _draft_call(app_client, lead_id).get_json()
    assert data["escalation_topics"] == []
    assert data["sensitive_topic"] is False
    assert data["requires_manual_review"] is False


# --- Criterion 9 / 11(c): opted-out and unconsented sends refused -------------


def test_send_custom_reply_opted_out_refused(app_client, two_users, monkeypatch):
    import tenant_sms_db as tdb
    from lead_service import upsert_crm_lead
    from sms_authorization import record_one_to_one_attestation

    u1, _ = two_users
    _login(app_client, u1)
    _telnyx_ready(monkeypatch, verification="verified")
    tdb.accept_sms_terms(u1, u1)
    lead_id, _, _ = upsert_crm_lead(
        u1,
        "+17202891700",
        {"lead_name": "Opt Out", "phone_number": "+17202891700"},
        source="sms",
        touch_sms=True,
        assigned_user_id=u1,
    )
    record_one_to_one_attestation(
        u1, lead_id, message_body="Hello from TopAI", source_page="test"
    )
    db.mark_lead_opt_out(lead_id, u1)
    inbound_id = _make_inbound(u1, lead_id, "+17202891700", _INBOUND_BODY)
    # Ruling 1: drafting is ALLOWED for an opted-out lead, with a warning.
    _mock_coach(monkeypatch, _coach_dict(_DRAFT_TEXT))
    draft = _draft_call(app_client, lead_id)
    assert draft.status_code == 200, draft.get_json()
    assert draft.get_json()["opted_out"] is True
    assert draft.get_json()["draft_reply"] == _DRAFT_TEXT
    # The send is refused server-side.
    with patch(
        "sms_providers.telnyx.TelnyxSMSProvider.send_message"
    ) as send_mock:
        send_mock.return_value = {"provider_message_id": "msg-x", "status": "queued"}
        res = app_client.post(
            "/sms/messages",
            json={
                "persona_id": _persona(u1),
                "lead_name": "Opt Out",
                "phone_number": "+17202891700",
                "message_body": "Hello from TopAI",
                "compliance_confirmed": True,
                "send_now": True,
            },
        )
    assert res.status_code == 403
    assert "opt" in res.get_json()["error"].lower()
    assert not send_mock.called
    assert _outbound_rows(u1, lead_id) == []


def test_send_custom_reply_without_consent_evidence_refused(
    app_client, two_users, monkeypatch
):
    """The consent gate refuses a send with no attestation record.

    Proven at the gate level: ``send_authorized_sms`` creates the
    agent's one-to-one attestation inline (the agent's compliance
    certification IS the consent evidence for a manual one-to-one
    send), so a route-level 403 for a merely-uncertified lead does not
    exist by design. What the gate refuses — no attestation record for
    the exact body and no certified status — is asserted here, plus the
    route-level refusal when the agent does NOT certify compliance.
    """
    from sms_authorization import can_send_sms

    u1, _ = two_users
    _login(app_client, u1)
    _telnyx_ready(monkeypatch, verification="verified")
    lead_id, phone = _make_lead(u1)
    with db.get_db() as conn:
        conn.execute(
            "UPDATE leads SET sms_consent_status = ? WHERE id = ? AND user_id = ?",
            ("not_certified", lead_id, u1),
        )
    # No attestation row exists for this lead/body.
    allowed, message = can_send_sms(
        u1, lead_id, message_body="Hello from TopAI",
        message_purpose="real_estate_follow_up",
    )
    assert allowed is False
    assert message == NO_CERT_MSG
    # Route level: no compliance certification -> consent refusal, nothing sent.
    with patch(
        "sms_providers.telnyx.TelnyxSMSProvider.send_message"
    ) as send_mock:
        res = app_client.post(
            "/sms/messages",
            json={
                "persona_id": _persona(u1),
                "lead_name": "Draft Test Lead",
                "phone_number": phone,
                "message_body": "Hello from TopAI",
                "send_now": True,
            },
        )
    assert res.status_code == 400
    assert "consent" in res.get_json()["error"].lower()
    assert not send_mock.called
    assert _outbound_rows(u1, lead_id) == []


# --- Criterion 13 / 14: agent-reviewed text needs no template -----------------


def test_send_custom_reply_needs_no_template(app_client, two_users, monkeypatch):
    import tenant_sms_db as tdb
    from sms_authorization import record_one_to_one_attestation

    u1, _ = two_users
    _login(app_client, u1)
    _telnyx_ready(monkeypatch, verification="verified")
    tdb.accept_sms_terms(u1, u1)
    # The tenant has zero templates — there is nothing to resolve.
    with db.get_db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM harness_message_templates WHERE user_id = ?",
            (u1,),
        ).fetchone()["c"] == 0
    lead_id, phone = _make_lead(u1)
    record_one_to_one_attestation(
        u1, lead_id, message_body=_DRAFT_TEXT, source_page="test"
    )
    with patch(
        "sms_providers.telnyx.TelnyxSMSProvider.send_message"
    ) as send_mock:
        send_mock.return_value = {"provider_message_id": "msg-tx-1", "status": "queued"}
        res = app_client.post(
            "/sms/messages",
            json={
                "persona_id": _persona(u1),
                "lead_name": "Draft Test Lead",
                "phone_number": phone,
                "message_body": _DRAFT_TEXT,
                "compliance_confirmed": True,
                "send_now": True,
            },
        )
    assert res.status_code == 201, res.get_json()
    assert res.get_json()["message_body"] == _DRAFT_TEXT
    assert send_mock.called
    sent_body = send_mock.call_args.kwargs.get("body") or send_mock.call_args.args[1]
    assert sent_body == _DRAFT_TEXT


def test_send_uses_edited_text_verbatim(app_client, two_users, monkeypatch):
    import tenant_sms_db as tdb
    from sms_authorization import record_one_to_one_attestation

    u1, _ = two_users
    _login(app_client, u1)
    _telnyx_ready(monkeypatch, verification="verified")
    tdb.accept_sms_terms(u1, u1)
    lead_id, phone = _make_lead(u1)
    _make_inbound(u1, lead_id, phone, _INBOUND_BODY)
    _mock_coach(monkeypatch, _coach_dict(_DRAFT_TEXT))
    draft = _draft_call(app_client, lead_id)
    assert draft.status_code == 200
    assert draft.get_json()["draft_reply"] == _DRAFT_TEXT
    # The agent edits the draft, then clicks Send.
    record_one_to_one_attestation(
        u1, lead_id, message_body=_EDITED_TEXT, source_page="test"
    )
    with patch(
        "sms_providers.telnyx.TelnyxSMSProvider.send_message"
    ) as send_mock:
        send_mock.return_value = {"provider_message_id": "msg-tx-2", "status": "queued"}
        res = app_client.post(
            "/sms/messages",
            json={
                "persona_id": _persona(u1),
                "lead_name": "Draft Test Lead",
                "phone_number": phone,
                "message_body": _EDITED_TEXT,
                "compliance_confirmed": True,
                "send_now": True,
            },
        )
    assert res.status_code == 201, res.get_json()
    assert send_mock.called
    sent_body = send_mock.call_args.kwargs.get("body") or send_mock.call_args.args[1]
    assert sent_body == _EDITED_TEXT
    assert sent_body != _DRAFT_TEXT
    rows = _outbound_rows(u1, lead_id)
    assert len(rows) == 1
    assert rows[0]["message_body"] == _EDITED_TEXT
    assert _DRAFT_TEXT not in json.dumps([dict(r) for r in rows])


# --- Criterion 8 / 11(d): automation still requires an approved template ------


def test_scheduled_followup_unapproved_template_refused(two_users):
    from harness.followup_sends import FollowUpScheduleError, schedule_follow_up_sms
    from harness import templates_db
    from tests.test_follow_up_sms_sending import _make_follow_up

    u1, _ = two_users
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    draft_tpl = templates_db.create_template(
        u1, name="Draft Only", channel="sms",
        purpose="follow_up_no_reply", body="Hi {lead_name}, draft only.",
    )
    with pytest.raises(FollowUpScheduleError) as excinfo:
        schedule_follow_up_sms(
            u1, lead_id, follow_up_id, "caller body",
            "2030-01-01T00:00:00Z", "3-day check-in",
            template_id=draft_tpl["id"],
        )
    assert excinfo.value.code == "invalid_template_id"


def test_scheduled_followup_unknown_template_refused(two_users):
    from harness.followup_sends import FollowUpScheduleError, schedule_follow_up_sms
    from tests.test_follow_up_sms_sending import _make_follow_up

    u1, _ = two_users
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    with pytest.raises(FollowUpScheduleError) as excinfo:
        schedule_follow_up_sms(
            u1, lead_id, follow_up_id, "caller body",
            "2030-01-01T00:00:00Z", "3-day check-in",
            template_id=999999,
        )
    assert excinfo.value.code == "invalid_template_id"


def test_supervised_non_template_auto_send_drafts(two_users):
    from harness.policy import decide

    u1, _ = two_users
    decision, reason = decide(
        u1, "send_sms", "send_sms",
        {"mode": "supervised", "template_approved": False},
    )
    assert decision == "DRAFT"
    assert "only approved-template sends auto-execute" in reason
    decision, _ = decide(
        u1, "send_sms", "send_sms",
        {"mode": "supervised", "template_approved": True},
    )
    assert decision == "ALLOW"


# --- Criterion 6: verified facts in the prompt --------------------------------


def _lead_row(**overrides):
    row = {
        "name": "Sarah Johnson",
        "phone_number": "+15551230001",
        "lead_type": "buyer",
        "property_interest": "3-bed near Maple Street",
        "status": "contacted",
        "opt_out_status": "active",
        "notes": "",
    }
    row.update(overrides)
    return row


def test_prompt_marks_availability_unknown():
    from sms_prompts import (
        build_inbound_reply_analysis_prompt,
        build_verified_facts_block,
    )

    block = build_verified_facts_block([], [], "3-bed near Maple Street")
    assert "NOT in the app's data" in block
    prompt = build_inbound_reply_analysis_prompt(
        _lead_row(), [], _INBOUND_BODY,
        approved_templates=[], recall_context="",
        draft_only=True, verified_facts=block,
    )
    assert "VERIFIED FACTS" in prompt
    assert "NOT in the app's data" in prompt
    assert "agent will check" in prompt
    assert "TopAI will NOT send this draft" in prompt
    assert "TopAI will send draft_reply automatically" not in prompt


def test_prompt_includes_confirmed_booking():
    from sms_prompts import (
        build_inbound_reply_analysis_prompt,
        build_verified_facts_block,
    )

    confirmed = [{
        "status": "confirmed",
        "confirmed_appointment_id": 5,
        "requested_start_at": "2026-11-27T21:00:00Z",
        "appointment_type": "in_person",
    }]
    block = build_verified_facts_block(
        [], confirmed, "3-bed near Maple Street", timezone_name="America/Denver"
    )
    assert "Confirmed appointment:" in block
    assert "Nov 27" in block
    assert "2:00 PM" in block
    prompt = build_inbound_reply_analysis_prompt(
        _lead_row(), [], "Great, see you Friday!",
        approved_templates=[], recall_context="",
        draft_only=True, verified_facts=block,
    )
    assert "Confirmed appointment:" in prompt
    assert "Nov 27" in prompt


def test_prompt_includes_open_request_without_confirming():
    from sms_prompts import (
        build_inbound_reply_analysis_prompt,
        build_verified_facts_block,
    )

    open_requests = [{
        "status": "requested",
        "requested_start_at": "2026-11-27T21:00:00Z",
        "appointment_type": "in_person",
    }]
    block = build_verified_facts_block(
        open_requests, [], "3-bed near Maple Street",
        timezone_name="America/Denver",
    )
    assert "Open appointment request:" in block
    assert "Nov 27" in block
    prompt = build_inbound_reply_analysis_prompt(
        _lead_row(), [], "Can we meet Friday?",
        approved_templates=[], recall_context="",
        draft_only=True, verified_facts=block,
    )
    assert "Open appointment request:" in prompt
    assert "never claim" in prompt.lower() or "do not" in prompt.lower()
    assert "Confirmed appointment: none" in prompt


# --- Criterion 10: templates page copy ----------------------------------------


def test_templates_page_description_exact(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    res = app_client.get("/harness/templates")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert _NEW_TEMPLATES_DESCRIPTION in html
    assert _OLD_TEMPLATES_DESCRIPTION not in html
