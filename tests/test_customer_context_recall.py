"""Phase III-5 customer context / memory recall — tests.

Red-before-green limbs (fail on un-fixed code, pass on the gate):
(c) recall context reaches the draft prompt — the recall block
    contains the lead's open-task title and an activity summary
    (fails on un-fixed code: no recall block exists in the prompt
    today);
(c) recall alone writes nothing — the lead row is byte-identical
    before/after a recall call, no new activity row, no qualification
    change (guards the wiring);
(d) the recall block never contains a raw message body (summaries
    only);
(e) validate_coach_response keeps an int template_id and drops a
    non-int one (schema-only; resolution is server-side).

Every test asserts on durable ROWS / returned values — never on
dashboard counters.
"""

import json

import pytest

import crm_db
import db
from tests.test_follow_up_sms_sending import _make_lead


@pytest.fixture(autouse=True)
def _isolate():
    db.init_db()
    yield


def _lead_with_context(user_id):
    """A lead with an open task, an activity summary, and a planted
    raw SMS body that must never appear in recall."""
    lead_id, phone = _make_lead(user_id)
    task_id, error = crm_db.create_task(
        user_id,
        {
            "title": "Prepare for Friday 2pm meeting",
            "lead_id": lead_id,
            "task_type": "call",
        },
    )
    assert error is None, error
    crm_db.add_lead_activity(
        lead_id,
        user_id,
        "note_added",
        "Agent noted the lead prefers evening calls",
        {"note": "prefers evening calls"},
    )
    # A raw SMS body (distinctive marker) that recall must never leak.
    db.create_sms_message(
        user_id,
        None,
        "telnyx",
        {
            "lead_name": "Follow-up Lead",
            "phone_number": phone,
            "message_body": "SECRET-RAW-BODY-7f3a9 do not repeat this",
        },
        status="received",
        lead_id=lead_id,
        direction="inbound",
        consent_status="verified",
        opt_out_status="active",
    )
    return lead_id, task_id


def _lead_row(user_id, lead_id):
    return dict(db.get_lead(lead_id, user_id))


def _activity_count(user_id, lead_id):
    return len(crm_db.list_lead_activities(user_id, lead_id, limit=100) or [])


# --- Criterion 3: memory recall ----------------------------------------------


def test_build_recall_context_contains_task_and_activity(two_users):
    """(c) Recall reaches the draft prompt: the block contains the
    lead's open-task title and an activity summary."""
    from harness.memory import build_recall_context

    u1, _ = two_users
    lead_id, _ = _lead_with_context(u1)
    block = build_recall_context(u1, lead_id)
    assert block
    assert "Prepare for Friday 2pm meeting" in block
    assert "prefers evening calls" in block
    assert "Follow-up Lead" in block


def test_recall_alone_writes_nothing(two_users):
    """(c) Recall alone writes nothing: the lead row is byte-identical
    before/after, no new activity row, no qualification change."""
    from harness.memory import build_recall_context

    u1, _ = two_users
    lead_id, _ = _lead_with_context(u1)
    before = _lead_row(u1, lead_id)
    activities_before = _activity_count(u1, lead_id)
    score_before = before.get("qualification_score")
    block = build_recall_context(u1, lead_id)
    assert block
    after = _lead_row(u1, lead_id)
    assert after == before
    assert _activity_count(u1, lead_id) == activities_before
    assert after.get("qualification_score") == score_before


def test_recall_block_never_contains_raw_message_body(two_users):
    """(d) The recall block never contains a raw message body —
    summaries only."""
    from harness.memory import build_recall_context

    u1, _ = two_users
    lead_id, _ = _lead_with_context(u1)
    block = build_recall_context(u1, lead_id)
    assert "SECRET-RAW-BODY-7f3a9" not in block
    assert "do not repeat this" not in block


def test_prompt_includes_recall_block(two_users):
    """(c) The inbound coach prompt carries the recall block as a
    CUSTOMER CONTEXT section."""
    from harness.memory import build_recall_context
    from sms_prompts import build_inbound_reply_analysis_prompt

    u1, _ = two_users
    lead_id, _ = _lead_with_context(u1)
    lead = db.get_lead(lead_id, u1)
    recall = build_recall_context(u1, lead_id)
    prompt = build_inbound_reply_analysis_prompt(
        lead, [], "Can we meet Friday?", recall_context=recall,
    )
    assert "CUSTOMER CONTEXT" in prompt
    assert "Prepare for Friday 2pm meeting" in prompt
    assert "SECRET-RAW-BODY-7f3a9" not in prompt.split("CUSTOMER CONTEXT", 1)[1]


def test_prompt_includes_approved_templates(two_users):
    """The inbound coach prompt lists the tenant's approved SMS
    templates (name + purpose + body) and the template_id key."""
    from harness import templates_db
    from sms_prompts import build_inbound_reply_analysis_prompt

    u1, _ = two_users
    lead_id, _ = _make_lead(u1)
    tpl = templates_db.create_template(
        u1, name="Follow-up nudge", channel="sms",
        purpose="follow_up_no_reply", body="Hi {lead_name}, checking in!",
    )
    templates_db.approve_template(u1, tpl["id"], approved_by=u1)
    # A draft template is never listed.
    templates_db.create_template(
        u1, name="Draft only", channel="sms",
        purpose="greeting", body="Hi {lead_name}",
    )
    lead = db.get_lead(lead_id, u1)
    approved = templates_db.list_approved_templates(u1, channel="sms")
    prompt = build_inbound_reply_analysis_prompt(
        lead, [], "Hi there", approved_templates=approved,
    )
    assert "APPROVED TEMPLATES" in prompt
    assert "Follow-up nudge" in prompt
    assert "follow_up_no_reply" in prompt
    assert "Hi {lead_name}, checking in!" in prompt
    assert "Draft only" not in prompt
    assert "template_id" in prompt


def test_validate_coach_response_template_id():
    """validate_coach_response keeps an int template_id and drops a
    non-int one (schema-only; server-side resolution decides use)."""
    from sms_coach import validate_coach_response

    ok = validate_coach_response(json.dumps({
        "summary": "s", "draft_reply": "Hi", "template_id": 7,
    }))
    assert ok["template_id"] == 7
    bad = validate_coach_response(json.dumps({
        "summary": "s", "draft_reply": "Hi", "template_id": "seven",
    }))
    assert bad["template_id"] is None
    missing = validate_coach_response(json.dumps({
        "summary": "s", "draft_reply": "Hi",
    }))
    assert missing["template_id"] is None
