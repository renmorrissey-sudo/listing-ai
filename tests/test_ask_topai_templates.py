"""Phase III-5 fix round — Ask TopAI schedules by template reference.

Red-before-green limbs (fail on un-fixed code, pass on the fix):

(a) the ``schedule_follow_up_sms`` tool spec offers ``template_id`` /
    ``template_name`` and no longer requires a body (the model could
    not pass a template_id at all — the spec had no such parameter and
    its description said the body "must be supplied");
(b) the Ask TopAI context carries the tenant's approved SMS templates
    (id, name, purpose, body) so the model can reference one by id or
    name instead of asking the user to paste the body;
(c) scheduling by template NAME resolves server-side to the template
    id and the draft records template_id + template_name;
(d) scheduling by an unapproved template NAME (draft / archived /
    other-tenant) is refused fail-closed — nothing enqueued;
(e) the schedule-time and fire-time log lines carry template_id, so a
    live verification can confirm it from the logs alone.

Every test asserts on the tool spec, the prompt text, the sanitize
output, or durable ROWS (harness_follow_up_sends, harness_approvals,
harness_jobs) — never on dashboard counters.
"""

import json
import logging

import pytest

import db
from harness.followup_sends import schedule_follow_up_sms
from sms_providers.mock import clear_sent_messages, sent_messages
from tests.test_follow_up_sms_sending import (
    _approvals,
    _gate_open,
    _iso_in,
    _make_follow_up,
    _make_lead,
    _mock_env,
)
from tests.test_harness_staging_safety import _set_settings
from tests.test_message_templates import (
    _approved_template,
    _send_row_for_lead,
    _template,
)
from workers.harness_worker import run_once


@pytest.fixture(autouse=True)
def _isolate():
    db.init_db()
    with db.get_db() as conn:
        conn.execute("DELETE FROM harness_jobs")
        conn.execute("DELETE FROM harness_approvals")
        conn.execute("DELETE FROM harness_follow_up_sends")
        try:
            conn.execute("DELETE FROM harness_message_templates")
        except Exception:
            pass
    clear_sent_messages()
    yield
    clear_sent_messages()


def _tool_spec(name):
    from ask_topai import registry

    for tool in registry.anthropic_tools():
        if tool.get("name") == name:
            return tool
    return None


def _schedule_via_ask_topai(user_id, arguments):
    """The live Ask TopAI path: sanitize_command -> execute_command."""
    from ask_topai.actions import execute_command
    from ask_topai.schemas import sanitize_command

    cleaned, err = sanitize_command(
        {"action": "schedule_follow_up_sms", "arguments": arguments},
        "Schedule a follow-up SMS",
    )
    assert err is None, err
    action, payload, error, _choices = execute_command(user_id, cleaned, None)
    return action, payload, error


def _job_payload(job_id):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT payload_json FROM harness_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    return json.loads(row["payload_json"]) if row else {}


# --- (a) the tool spec offers a template reference --------------------------


def test_schedule_tool_spec_offers_template_reference():
    """The schedule_follow_up_sms tool the model sees must offer
    template_id / template_name and must not require a body — the body
    is only required when no template is given."""
    spec = _tool_spec("schedule_follow_up_sms")
    assert spec is not None
    properties = spec["input_schema"]["properties"]
    assert "template_id" in properties
    assert properties["template_id"]["type"] == "integer"
    assert "template_name" in properties
    assert properties["template_name"]["type"] == "string"
    assert "body" not in spec["input_schema"].get("required", [])
    assert "template" in spec["description"].lower()


# --- (b) the Ask TopAI context carries the approved templates ---------------


def test_ask_topai_context_carries_approved_templates(two_users):
    """The prompt-carried list includes the approved template's id,
    name, purpose and body, and excludes a draft template."""
    from ask_topai.agent import _approved_templates_text

    u1, _ = two_users
    approved = _approved_template(
        u1, name="Follow-up nudge", body="Hi {lead_name}, just checking in!",
        purpose="follow_up_no_reply",
    )
    _template(u1, name="Unapproved draft", body="This should never be used.")
    text = _approved_templates_text(u1)
    assert str(approved["id"]) in text
    assert "Follow-up nudge" in text
    assert "follow_up_no_reply" in text
    assert "Hi {lead_name}, just checking in!" in text
    assert "Unapproved draft" not in text
    assert "This should never be used." not in text


def test_system_prompt_tells_model_to_pass_template_id():
    """The system prompt must tell the model to pass template_id (or
    template_name) when the agent references an approved template, and
    not to ask for the body — the live failure was the model saying
    'I don't have access to template content' and asking the user to
    paste the body."""
    from ask_topai.agent import SYSTEM_PROMPT

    lowered = SYSTEM_PROMPT.lower()
    assert "template_id" in lowered
    assert "template" in lowered
    assert "do not ask" in lowered or "without asking" in lowered


# --- (c) schedule by template NAME records template_id ----------------------


def test_sanitize_keeps_template_name():
    """sanitize_command keeps template_name (and template_id) and does
    not demand a body when a template reference is given."""
    from ask_topai.schemas import sanitize_command

    scheduled_at = _iso_in(minutes=15)
    cleaned, err = sanitize_command(
        {
            "action": "schedule_follow_up_sms",
            "arguments": {
                "lead_name": "Follow-up Lead",
                "template_name": "Follow-up nudge",
                "scheduled_at": scheduled_at,
                "reason": "3-day check-in after showing",
            },
        },
        "Schedule a follow-up SMS using the Follow-up nudge template",
    )
    assert err is None, err
    assert cleaned["arguments"]["template_name"] == "Follow-up nudge"
    assert not cleaned["arguments"]["body"]

    cleaned, err = sanitize_command(
        {
            "action": "schedule_follow_up_sms",
            "arguments": {
                "lead_name": "Follow-up Lead",
                "template_id": 1,
                "scheduled_at": scheduled_at,
            },
        },
        "Schedule a follow-up SMS using template 1",
    )
    assert err is None, err
    assert cleaned["arguments"]["template_id"] == 1


def test_schedule_by_template_name_records_template_id(two_users, monkeypatch):
    """Schedule by NAME through the Ask TopAI path -> the job payload
    and the fired approval draft record template_id=1 (+ name), and
    the draft text is the filled template."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="drafts_only")
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    tpl = _approved_template(
        u1, name="Follow-up nudge", body="Hi {lead_name}, just checking in!",
    )
    action, payload, error = _schedule_via_ask_topai(
        u1,
        {
            "lead_id": lead_id,
            "follow_up_id": follow_up_id,
            "template_name": "Follow-up nudge",
            "scheduled_at": _iso_in(minutes=0),
            "reason": "3-day check-in after showing",
        },
    )
    assert action == "schedule_follow_up_sms"
    assert error is None, error
    assert payload["template_id"] == tpl["id"]
    assert payload["template_name"] == "Follow-up nudge"
    assert payload["duplicate"] is False
    job_payload = _job_payload(payload["job_id"])
    assert job_payload.get("template_id") == tpl["id"]
    # Fire in drafts_only -> the approval draft records the template.
    with _gate_open():
        assert run_once() is True
    assert sent_messages() == []
    approvals = _approvals(u1)
    assert len(approvals) == 1
    draft = json.loads(approvals[0]["draft_json"])
    assert draft["template_id"] == tpl["id"]
    assert draft["template_name"] == "Follow-up nudge"
    assert draft["draft_reply"] == "Hi Follow-up Lead, just checking in!"


def test_schedule_by_template_id_via_ask_topai_records_template_id(
    two_users, monkeypatch
):
    """Schedule by ID through the Ask TopAI path (the live failing
    case: the model could not pass template_id at all) -> the draft
    records template_id=1 (+ name)."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="drafts_only")
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    tpl = _approved_template(
        u1, name="Follow-up nudge", body="Hi {lead_name}, just checking in!",
    )
    action, payload, error = _schedule_via_ask_topai(
        u1,
        {
            "lead_id": lead_id,
            "follow_up_id": follow_up_id,
            "template_id": tpl["id"],
            "scheduled_at": _iso_in(minutes=0),
            "reason": "3-day check-in after showing",
        },
    )
    assert action == "schedule_follow_up_sms"
    assert error is None, error
    assert payload["template_id"] == tpl["id"]
    with _gate_open():
        assert run_once() is True
    assert sent_messages() == []
    approvals = _approvals(u1)
    assert len(approvals) == 1
    draft = json.loads(approvals[0]["draft_json"])
    assert draft["template_id"] == tpl["id"]
    assert draft["template_name"] == "Follow-up nudge"
    assert draft["draft_reply"] == "Hi Follow-up Lead, just checking in!"


# --- (d) unapproved template NAME refused fail-closed -----------------------


def test_schedule_by_unapproved_template_name_refused(two_users, monkeypatch):
    """A draft, archived, or other-tenant template NAME is refused
    with a template error — nothing enqueued, no send row, no job."""
    from harness import templates_db

    _mock_env(monkeypatch)
    u1, u2 = two_users
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    _template(u1, name="Not approved", body="Hi {lead_name}")
    archived = _approved_template(u1, name="Old nudge", body="Hi {lead_name}")
    templates_db.archive_template(u1, archived["id"])
    _approved_template(u2, name="Their nudge", body="Hi {lead_name}")
    for name in ("Not approved", "Old nudge", "Their nudge"):
        action, payload, error = _schedule_via_ask_topai(
            u1,
            {
                "lead_id": lead_id,
                "follow_up_id": follow_up_id,
                "template_name": name,
                "scheduled_at": _iso_in(minutes=15),
                "reason": "3-day check-in after showing",
            },
        )
        assert action == "schedule_follow_up_sms"
        assert error is not None
        assert "template" in error.lower(), error
        assert payload is None
    assert _send_row_for_lead(u1, lead_id) == []
    with db.get_db() as conn:
        jobs = conn.execute(
            "SELECT COUNT(*) AS n FROM harness_jobs WHERE user_id = ? AND kind = ?",
            (u1, "follow_up_sms_send"),
        ).fetchone()["n"]
    assert jobs == 0


# --- (e) schedule/fire log lines carry template_id --------------------------


def test_schedule_and_fire_logs_carry_template_id(two_users, monkeypatch, caplog):
    """The schedule-time log line and the fire-time drafted line both
    carry template_id (and template_name), so a live verification can
    confirm the template from the logs alone."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="drafts_only")
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    tpl = _approved_template(
        u1, name="Follow-up nudge", body="Hi {lead_name}, just checking in!",
    )
    with caplog.at_level(logging.INFO, logger="harness.followup_sends"):
        scheduled = schedule_follow_up_sms(
            u1, lead_id, follow_up_id, "caller body ignored",
            _iso_in(minutes=0), "3-day check-in after showing",
            template_id=tpl["id"],
        )
        assert scheduled["duplicate"] is False
        schedule_lines = [
            r for r in caplog.records
            if r.name == "harness.followup_sends" and "scheduled" in r.message
        ]
        assert schedule_lines, "no schedule-time log line"
        assert any(
            f"template_id={tpl['id']}" in r.message for r in schedule_lines
        ), [r.message for r in schedule_lines]
        caplog.clear()
        with _gate_open():
            assert run_once() is True
        drafted_lines = [
            r for r in caplog.records
            if r.name == "harness.followup_sends"
            and "follow_up_sms drafted" in r.message
        ]
        assert drafted_lines, "no fire-time drafted log line"
        assert any(
            f"template_id={tpl['id']}" in r.message for r in drafted_lines
        ), [r.message for r in drafted_lines]
