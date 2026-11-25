"""ALLOW-path template fix: on ALLOW, the handler's resolved/filled
template draft must be the message sent — not a fresh coach composition.

Background (III-5 follow-up): the handler resolves the coach's template
claim (by id or by name) server-side and fills its slots into a draft.
But on ALLOW the handler called ``process_inbound_ai`` with no draft,
so it re-ran the coach, which re-composed the reply and discarded the
handler's resolved/filled draft — the template_id was lost and the sent
text was the coach's free composition instead of the approved template
verbatim.

These tests prove:
- Template by id: an ALLOW decision on a draft that used a template
  (by id) sends the handler's filled template text verbatim, with
  template_id recorded (audit gate_reason), and the coach is NOT
  re-run (called exactly once).
- Template by name: same, for a template resolved by name.
- Non-template path unchanged: a free-composed draft (no template) on
  ALLOW still flows as before — the coach IS re-run and the fresh
  free composition is what goes out (regression guard).

The coach is mocked with two different return values (first call =
the handler's coach run, second call = the re-run inside
``process_inbound_ai``), so the tests discriminate which draft was
actually sent. Deterministic only: no model, no provider, no network.
"""

import json
import uuid
from unittest.mock import MagicMock, patch

import pytest

import db
from harness.jobs import enqueue_job
from sms_providers.mock import clear_sent_messages, sent_messages
from tests.test_harness_staging_safety import (
    _GateOpen,
    _set_settings,
    _staging_mock_config,
)
from workers.harness_worker import run_once

_TEMPLATE_NAME = "Follow-up nudge"
_TEMPLATE_BODY = "Hi {lead_name}, just checking in!"
_FIRST_FREE_DRAFT = "Free-composed first draft that must not be sent."
_SECOND_FREE_DRAFT = "Free-composed second draft that must not be sent."


@pytest.fixture(autouse=True)
def _isolate_job_queue():
    """The session DB is shared by every test (tests/conftest.py).
    Clear the harness queue and the mock provider's recorded sends so
    each test's worker run sees only its own job/send."""
    db.init_db()
    with db.get_db() as conn:
        conn.execute("DELETE FROM harness_jobs")
    clear_sent_messages()
    yield


def _unique_phone(prefix="303"):
    n = int(uuid.uuid4().hex[:8], 16) % 10_000_000
    return f"+1{prefix}{n:07d}"[:12]


def _make_lead(user_id, phone=None):
    """A consenting lead (same shape as the authorized E2E tests)."""
    from lead_service import upsert_crm_lead

    phone = phone or _unique_phone()
    lead_id, _, _ = upsert_crm_lead(
        user_id, phone, {"name": "Authorized E2E Lead", "phone_number": phone},
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


def _approved_template(user_id, name=_TEMPLATE_NAME, body=_TEMPLATE_BODY):
    from harness import templates_db

    row = templates_db.create_template(
        user_id, name=name, channel="sms",
        purpose="follow_up_no_reply", body=body,
    )
    return templates_db.approve_template(user_id, row["id"], approved_by=user_id)


def _analysis(template_claim, draft_reply):
    return {
        "summary": "Lead asked about availability.",
        "intent": "question",
        "draft_reply": draft_reply,
        "suggested_reply": draft_reply,
        "sensitive_topic": False,
        "requires_manual_review": False,
        "escalation_topics": [],
        "suggested_lead_status": "contacted",
        "appointment_details": None,
        "appointment_requested": False,
        "template_id": template_claim,
    }


def _coach_result(analysis):
    return {"analysis": analysis, "insight_id": None, "error": None}


def _run_job(user_id, lead_id, inbound_id, body):
    job_id = enqueue_job(
        user_id,
        "sms_auto_reply",
        {
            "lead_id": lead_id,
            "inbound_id": inbound_id,
            "body": body,
            "receiving_number": "+18880000000",
        },
        idempotency_key=f"allow-template-{uuid.uuid4().hex[:12]}",
    )
    assert run_once() is True, "worker ran nothing for the enqueued job"
    return job_id


def _job_row(job_id):
    with db.get_db() as conn:
        return dict(
            conn.execute(
                "SELECT * FROM harness_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        )


def _job_result(job_id):
    raw = _job_row(job_id).get("result_json")
    return json.loads(raw) if raw else {}


def _outbound_rows(user_id, lead_id):
    with db.get_db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM sms_messages WHERE user_id = ? AND lead_id = ? "
                "AND direction = 'outbound'",
                (user_id, lead_id),
            ).fetchall()
        ]


def _audit_rows(user_id, action=None):
    with db.get_db() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM harness_audit WHERE user_id = ? ORDER BY id",
                (user_id,),
            ).fetchall()
        ]
    if action:
        rows = [r for r in rows if r["action"] == action]
    return rows


def test_allow_sends_handler_template_by_id_verbatim(
    two_users, monkeypatch
):
    """Template by id: the ALLOW send is the handler's filled template
    text verbatim — not the coach's fresh free composition — the
    template_id is recorded in the audit gate_reason, and the coach
    runs exactly once (no re-run)."""
    # Build the job with the template claim by id on the first call.
    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    template = _approved_template(u1)
    filled = "Hi Authorized E2E Lead, just checking in!"
    first = _analysis(template["id"], _FIRST_FREE_DRAFT)
    second = _analysis(None, _SECOND_FREE_DRAFT)
    lead_id, phone = _make_lead(u1)
    inbound_id = _make_inbound(
        u1, lead_id, phone, "Hi, is the house still available?"
    )
    coach_mock = MagicMock(side_effect=[
        _coach_result(first),
        _coach_result(second),
    ])
    with _GateOpen(), patch(
        "sms_inbound.analyze_inbound_and_coach", coach_mock
    ):
        job_id = _run_job(
            u1, lead_id, inbound_id, "Hi, is the house still available?"
        )

    job = _job_row(job_id)
    assert job["status"] == "succeeded", job
    result = _job_result(job_id)
    assert result.get("decision") == "ALLOW", result
    assert result.get("sent") is True, result

    recorded = sent_messages()
    assert len(recorded) == 1, recorded
    assert recorded[0]["to"] == phone
    # The sent body is the handler's filled template verbatim — not
    # the first free draft and not the re-run's free draft.
    assert recorded[0]["body"] == filled, recorded[0]

    outbound = _outbound_rows(u1, lead_id)
    assert len(outbound) == 1, outbound
    assert outbound[0]["message_body"] == filled

    # The template_id is recorded on the ALLOW audit row.
    sent_audit = _audit_rows(u1, action="sms_auto_reply_sent")
    assert len(sent_audit) == 1
    assert f'template #{template["id"]}' in sent_audit[0]["gate_reason"]
    assert _TEMPLATE_NAME in sent_audit[0]["gate_reason"]

    # The coach ran once (the handler's run) — no re-run inside the
    # ALLOW send path.
    assert coach_mock.call_count == 1, coach_mock.call_count


def test_allow_sends_handler_template_by_name_verbatim(
    two_users, monkeypatch
):
    """Template by name: the coach claims the template by its name
    (not its id); the handler resolves the name server-side and the
    ALLOW send is the filled template verbatim, with the template_id
    recorded and the coach run exactly once."""
    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    template = _approved_template(u1)
    filled = "Hi Authorized E2E Lead, just checking in!"
    first = _analysis(_TEMPLATE_NAME, _FIRST_FREE_DRAFT)
    second = _analysis(None, _SECOND_FREE_DRAFT)
    lead_id, phone = _make_lead(u1)
    inbound_id = _make_inbound(
        u1, lead_id, phone, "Hi, is the house still available?"
    )
    coach_mock = MagicMock(side_effect=[
        _coach_result(first),
        _coach_result(second),
    ])
    with _GateOpen(), patch(
        "sms_inbound.analyze_inbound_and_coach", coach_mock
    ):
        job_id = _run_job(
            u1, lead_id, inbound_id, "Hi, is the house still available?"
        )

    job = _job_row(job_id)
    assert job["status"] == "succeeded", job
    result = _job_result(job_id)
    assert result.get("decision") == "ALLOW", result
    assert result.get("sent") is True, result

    recorded = sent_messages()
    assert len(recorded) == 1, recorded
    assert recorded[0]["body"] == filled, recorded[0]

    outbound = _outbound_rows(u1, lead_id)
    assert len(outbound) == 1, outbound
    assert outbound[0]["message_body"] == filled

    sent_audit = _audit_rows(u1, action="sms_auto_reply_sent")
    assert len(sent_audit) == 1
    assert f'template #{template["id"]}' in sent_audit[0]["gate_reason"]
    assert _TEMPLATE_NAME in sent_audit[0]["gate_reason"]

    assert coach_mock.call_count == 1, coach_mock.call_count


def test_allow_without_template_still_reruns_coach(
    two_users, monkeypatch
):
    """Non-template regression guard: a free-composed draft (no
    template) on ALLOW still flows as before — the coach IS re-run
    inside the send path and the fresh free composition is what goes
    out. Must pass before and after the fix."""
    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    _approved_template(u1)  # a template exists; the coach claims none
    first = _analysis(None, _FIRST_FREE_DRAFT)
    second = _analysis(None, _SECOND_FREE_DRAFT)
    lead_id, phone = _make_lead(u1)
    inbound_id = _make_inbound(
        u1, lead_id, phone, "Hi, is the house still available?"
    )
    coach_mock = MagicMock(side_effect=[
        _coach_result(first),
        _coach_result(second),
    ])
    with _GateOpen(), patch(
        "sms_inbound.analyze_inbound_and_coach", coach_mock
    ):
        job_id = _run_job(
            u1, lead_id, inbound_id, "Hi, is the house still available?"
        )

    job = _job_row(job_id)
    assert job["status"] == "succeeded", job
    result = _job_result(job_id)
    assert result.get("decision") == "ALLOW", result
    assert result.get("sent") is True, result

    recorded = sent_messages()
    assert len(recorded) == 1, recorded
    # The sent body is the RE-RUN's free composition (the second
    # coach call's draft) — the pre-fix behaviour, preserved.
    assert recorded[0]["body"] == _SECOND_FREE_DRAFT, recorded[0]

    outbound = _outbound_rows(u1, lead_id)
    assert len(outbound) == 1, outbound
    assert outbound[0]["message_body"] == _SECOND_FREE_DRAFT

    # The coach ran twice: the handler's run plus the re-run inside
    # the ALLOW send path.
    assert coach_mock.call_count == 2, coach_mock.call_count
