"""Phase III-5 approved templates store — tests.

Red-before-green limbs (fail on un-fixed code, pass on the gate):
(a) the coach claims a draft / archived / other-tenant template id ->
    the draft records template_id null and template_approved False
    (the handler has no resolution step today, so a handler trusting
    the claim would record the draft id);
(b) an approved template selected -> the draft is the slot-filled
    text, template_id + template_name recorded, template_approved True
    (fails on un-fixed code: no resolution, no filling, no recording);
(c) a template with a missing slot value is never used (fail closed);
(d) follow-up scheduling with a draft template id is refused;
(e) a template archived after scheduling -> supervised DRAFTs at fire,
    Authorized still ALLOWs (fire-time re-check asymmetry).

Every test asserts on durable ROWS (harness_message_templates,
harness_approvals, harness_audit, harness_follow_up_sends, mock
provider sends) — never on dashboard counters.
"""

import json
import uuid
from unittest.mock import patch

import pytest

import db
from harness.followup_sends import (
    FollowUpScheduleError,
    schedule_follow_up_sms,
)
from sms_providers.mock import clear_sent_messages, sent_messages
from tests.test_follow_up_sms_sending import (
    _approvals,
    _iso_in,
    _enable_sending,
    _gate_open,
    _job_result,
    _make_follow_up,
    _make_lead,
    _mock_env,
    _run_due,
    _schedule,
    _send_row,
)
from tests.test_harness_staging_safety import (
    _set_settings,
    _staging_mock_config,
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


def _template(user_id, name="Follow-up nudge", body="Hi {lead_name}, just checking in!",
              channel="sms", purpose="follow_up_no_reply"):
    from harness import templates_db

    return templates_db.create_template(
        user_id, name=name, channel=channel, purpose=purpose, body=body,
    )


def _approved_template(user_id, **kwargs):
    from harness import templates_db

    row = _template(user_id, **kwargs)
    return templates_db.approve_template(user_id, row["id"], approved_by=user_id)


def _approval_draft(approval_id):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT draft_json FROM harness_approvals WHERE id = ?", (approval_id,)
        ).fetchone()
    return json.loads(row["draft_json"]) if row else {}


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


def _analysis(template_id=None, draft_reply="Hi! Yes, it is still available."):
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
        "template_id": template_id,
    }


def _run_auto_reply_job(user_id, lead_id, analysis):
    """Run one sms_auto_reply job with the coach stubbed.

    A fresh inbound id per call: the AI-reply slot has a UNIQUE index
    on reply_to_message_id, so reusing the default 424242 across tests
    that actually send trips "already_replied" (see
    test_harness_approval_escalation.py).
    """
    from harness.jobs import enqueue_job
    from harness.sms_handler import process_sms_auto_reply_job

    inbound_id = uuid.uuid4().int % 900_000_000 + 100_000_000
    job_id = enqueue_job(
        user_id,
        "sms_auto_reply",
        {
            "lead_id": lead_id,
            "inbound_id": inbound_id,
            "body": "Hi, is the house still available?",
            "receiving_number": "+18880000000",
        },
        idempotency_key=f"sms-auto-reply-tpl-{uuid.uuid4().hex[:8]}",
    )
    with db.get_db() as conn:
        job = dict(
            conn.execute(
                "SELECT * FROM harness_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        )
    with patch(
        "sms_inbound.analyze_inbound_and_coach",
        return_value={"analysis": analysis, "insight_id": None, "error": None},
    ):
        return process_sms_auto_reply_job(job)


# --- Criterion 1: templates store -------------------------------------------


def test_migration_049_creates_templates_table(two_users):
    """Migration 049: the templates table, its columns, and indexes."""
    with db.get_db() as conn:
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(harness_message_templates)").fetchall()}
        indexes = {r["name"] for r in conn.execute(
            "PRAGMA index_list(harness_message_templates)").fetchall()}
        stamped = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            ("049_message_templates",),
        ).fetchone()
    assert {
        "id", "user_id", "name", "channel", "purpose", "body", "status",
        "created_by", "approved_by", "approved_at", "created_at", "updated_at",
    } <= cols
    assert {
        "idx_msg_tpl_user_status_channel",
        "idx_msg_tpl_user_name",
    } <= indexes
    assert stamped is not None


def test_create_template_starts_draft(two_users):
    """Create -> draft, fields stored, audit row written."""
    from harness import templates_db

    u1, _ = two_users
    row = _template(u1, name="Greeting", purpose="greeting",
                    body="Hi {lead_name}, welcome!")
    assert row["status"] == "draft"
    assert row["name"] == "Greeting"
    assert row["channel"] == "sms"
    assert row["purpose"] == "greeting"
    assert row["body"] == "Hi {lead_name}, welcome!"
    assert row["approved_by"] is None
    assert row["approved_at"] is None
    audit = _audit_rows(u1, action="message_template_created")
    assert len(audit) == 1
    assert str(row["id"]) in (audit[0]["gate_reason"] or "")
    assert "Greeting" in (audit[0]["gate_reason"] or "")


def test_approve_template_records_approver_and_audit(two_users):
    """Approve -> approved with approved_by/at + audit row."""
    from harness import templates_db

    u1, _ = two_users
    row = _template(u1)
    approved = templates_db.approve_template(u1, row["id"], approved_by=u1)
    assert approved["status"] == "approved"
    assert approved["approved_by"] == u1
    assert approved["approved_at"]
    audit = _audit_rows(u1, action="message_template_approved")
    assert len(audit) == 1
    assert str(row["id"]) in (audit[0]["gate_reason"] or "")


def test_archive_template_never_listed_approved(two_users):
    """Archive -> archived; the approved-list query never returns it."""
    from harness import templates_db

    u1, _ = two_users
    row = _approved_template(u1)
    assert templates_db.list_approved_templates(u1) != []
    archived = templates_db.archive_template(u1, row["id"])
    assert archived["status"] == "archived"
    assert templates_db.list_approved_templates(u1) == []
    assert _audit_rows(u1, action="message_template_archived")


def test_edit_approved_template_resets_to_draft(two_users):
    """Any edit to an approved template resets it to draft — approval
    attaches to the exact text."""
    from harness import templates_db

    u1, _ = two_users
    row = _approved_template(u1, body="Hi {lead_name}, checking in!")
    edited = templates_db.edit_template(
        u1, row["id"], body="Hi {lead_name}, changed text!"
    )
    assert edited["status"] == "draft"
    assert edited["body"] == "Hi {lead_name}, changed text!"
    assert edited["approved_by"] is None
    assert edited["approved_at"] is None
    assert edited["id"] == row["id"]
    # The old approved text is no longer usable.
    assert templates_db.list_approved_templates(u1) == []


def test_archived_template_cannot_approve_directly(two_users):
    """archived -> approved is disallowed; re-approval requires draft."""
    from harness import templates_db

    u1, _ = two_users
    row = _template(u1)
    templates_db.archive_template(u1, row["id"])
    with pytest.raises(templates_db.TemplateError):
        templates_db.approve_template(u1, row["id"], approved_by=u1)


def test_duplicate_name_per_tenant_rejected(two_users):
    """Same name twice for one tenant -> error; same name under a
    second tenant is independent."""
    from harness import templates_db

    u1, u2 = two_users
    _template(u1, name="Same name")
    with pytest.raises(templates_db.TemplateError):
        _template(u1, name="Same name")
    other = _template(u2, name="Same name")
    assert other["user_id"] == u2


def test_unknown_slot_at_creation_rejected(two_users):
    """A body slot outside the allowlist is a 400 naming the slot."""
    from harness import templates_db

    u1, _ = two_users
    with pytest.raises(templates_db.TemplateError) as excinfo:
        _template(u1, body="Hi {lead_nickname}, welcome!")
    assert "lead_nickname" in str(excinfo.value)


def test_body_over_480_rejected(two_users):
    """Body over 480 chars is rejected, not truncated silently."""
    from harness import templates_db

    u1, _ = two_users
    with pytest.raises(templates_db.TemplateError):
        _template(u1, body="x" * 481)


def test_cross_tenant_template_invisible(two_users):
    """A tenant touching another tenant's template sees 404 (never
    revealed): get/list/approve all tenant-scoped."""
    from harness import templates_db

    u1, u2 = two_users
    row = _approved_template(u2, name="Other tenant template")
    assert templates_db.get_template(u1, row["id"]) is None
    assert all(
        t["id"] != row["id"] for t in templates_db.list_templates(u1)
    )
    assert all(
        t["id"] != row["id"]
        for t in templates_db.list_approved_templates(u1)
    )
    with pytest.raises(templates_db.TemplateError):
        templates_db.approve_template(u1, row["id"], approved_by=u1)


def test_no_like_in_templates_module():
    """III-2 lesson: the new SQL uses equality only — no LIKE in the
    templates module or the migration, outside their docstrings."""
    import inspect

    import harness.templates_db as mod

    source = inspect.getsource(mod)
    body = source.split('"""', 2)[2] if source.count('"""') >= 2 else source
    assert "like" not in body.lower()
    with open("migrations/versions/049_message_templates.py", encoding="utf-8") as fh:
        migration = fh.read()
    migration_body = (
        migration.split('"""', 2)[2]
        if migration.count('"""') >= 2
        else migration
    )
    assert "like" not in migration_body.lower()


def test_no_new_dependency():
    """No new dependency was added (criterion 4)."""
    import os

    root = os.path.join(os.path.dirname(__file__), "..")
    with open(os.path.join(root, "requirements.txt"), encoding="utf-8") as fh:
        content = fh.read()
    # The templates store is the app's own database — no new package.
    assert "templates" not in content.lower().replace("jinja2", "")


# --- Criterion 2: template use in composing (RED-BEFORE-GREEN) --------------


def test_draft_template_claim_is_not_used(two_users, monkeypatch):
    """(a) The coach claims a DRAFT template id -> the draft records
    template_id null (key present) and the free text is used, never
    the draft template's body."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="drafts_only")
    lead_id, _ = _make_lead(u1)
    draft_tpl = _template(u1, name="Draft only", body="DRAFT TEXT {lead_name}")
    result = _run_auto_reply_job(u1, lead_id, _analysis(template_id=draft_tpl["id"]))
    assert result["decision"] == "DRAFT"
    assert result["approval_id"] is not None
    draft = _approval_draft(result["approval_id"])
    assert "template_id" in draft
    assert draft["template_id"] is None
    assert draft["draft_reply"] == "Hi! Yes, it is still available."
    assert "DRAFT TEXT" not in draft["draft_reply"]
    assert sent_messages() == []


def test_archived_template_claim_is_not_used(two_users, monkeypatch):
    """(a) The coach claims an ARCHIVED template id -> not used."""
    from harness import templates_db

    _mock_env(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="drafts_only")
    lead_id, _ = _make_lead(u1)
    row = _approved_template(u1, name="Was approved", body="OLD TEXT {lead_name}")
    templates_db.archive_template(u1, row["id"])
    result = _run_auto_reply_job(u1, lead_id, _analysis(template_id=row["id"]))
    assert result["decision"] == "DRAFT"
    draft = _approval_draft(result["approval_id"])
    assert draft["template_id"] is None
    assert "OLD TEXT" not in draft["draft_reply"]
    assert sent_messages() == []


def test_other_tenant_template_claim_is_not_used(two_users, monkeypatch):
    """(a) The coach claims another tenant's approved template id ->
    not used (cross-tenant claim)."""
    _mock_env(monkeypatch)
    u1, u2 = two_users
    _set_settings(u1, mode="drafts_only")
    lead_id, _ = _make_lead(u1)
    other = _approved_template(u2, name="Other tenant", body="OTHER TEXT {lead_name}")
    result = _run_auto_reply_job(u1, lead_id, _analysis(template_id=other["id"]))
    assert result["decision"] == "DRAFT"
    draft = _approval_draft(result["approval_id"])
    assert draft["template_id"] is None
    assert "OTHER TEXT" not in draft["draft_reply"]
    assert sent_messages() == []


def test_approved_template_selected_fills_and_records(two_users, monkeypatch):
    """(b) An approved template selected -> the draft is the slot-filled
    text, template_id + template_name recorded, gate_reason names it."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="drafts_only")
    lead_id, _ = _make_lead(u1)
    tpl = _approved_template(
        u1, name="Follow-up nudge",
        body="Hi {lead_name}, just checking in!",
    )
    result = _run_auto_reply_job(u1, lead_id, _analysis(template_id=tpl["id"]))
    assert result["decision"] == "DRAFT"
    draft = _approval_draft(result["approval_id"])
    assert draft["template_id"] == tpl["id"]
    assert draft["template_name"] == "Follow-up nudge"
    assert draft["draft_reply"] == "Hi Follow-up Lead, just checking in!"
    assert f'template #{tpl["id"]} "Follow-up nudge"' in (draft.get("gate_reason") or "")
    assert sent_messages() == []


def test_template_with_missing_slot_value_not_used(two_users, monkeypatch):
    """(c) A slot with no value for this lead/context -> the template
    is not used (fail closed), free compose, template_id null."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="drafts_only")
    lead_id, _ = _make_lead(u1)
    tpl = _approved_template(
        u1, name="Needs appointment time",
        body="See you {appointment_time}!",
    )
    result = _run_auto_reply_job(u1, lead_id, _analysis(template_id=tpl["id"]))
    assert result["decision"] == "DRAFT"
    draft = _approval_draft(result["approval_id"])
    assert draft["template_id"] is None
    assert draft["draft_reply"] == "Hi! Yes, it is still available."
    assert sent_messages() == []


def test_supervised_template_approved_send_allowed(two_users, monkeypatch):
    """Supervised mode + approved template -> ALLOW (the flag is real,
    not hardcoded False). No approval is created; the mock records
    exactly one send."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="supervised", paused=False)
    lead_id, _ = _make_lead(u1)
    tpl = _approved_template(
        u1, name="Follow-up nudge",
        body="Hi {lead_name}, just checking in!",
    )
    with _gate_open():
        result = _run_auto_reply_job(u1, lead_id, _analysis(template_id=tpl["id"]))
    assert result["decision"] == "ALLOW", result
    assert result["sent"] is True
    assert len(sent_messages()) == 1
    assert _approvals(u1) == []


def test_supervised_free_compose_still_drafts(two_users, monkeypatch):
    """Supervised mode + free compose (no template) -> DRAFT, no send.
    The flag is False exactly when no approved template was used."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="supervised", paused=False)
    lead_id, _ = _make_lead(u1)
    result = _run_auto_reply_job(u1, lead_id, _analysis(template_id=None))
    assert result["decision"] == "DRAFT"
    assert result["approval_id"] is not None
    draft = _approval_draft(result["approval_id"])
    assert draft["template_id"] is None
    assert sent_messages() == []


# --- Follow-up templates ------------------------------------------------------


def test_follow_up_schedule_with_template_fills_and_records(two_users, monkeypatch):
    """Schedule with an approved template_id -> the stored body is the
    filled template text (verbatim rule preserved), the job payload
    carries template_id, and the drafts_only approval records it."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="drafts_only")
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    tpl = _approved_template(
        u1, name="Check-in",
        body="Hi {lead_name}, checking in: {follow_up_reason}",
    )
    scheduled = schedule_follow_up_sms(
        u1, lead_id, follow_up_id, "caller body ignored",
        _iso_in(), "3-day check-in after showing",
        template_id=tpl["id"],
    )
    assert scheduled["duplicate"] is False
    from tests.test_follow_up_sms_sending import _sms_row

    sms = _sms_row(scheduled["message_id"], u1)
    assert sms["message_body"] == (
        "Hi Follow-up Lead, checking in: 3-day check-in after showing"
    )
    job_payload = json.loads(_job_payload(scheduled["job_id"]))
    assert job_payload.get("template_id") == tpl["id"]
    # Fire in drafts_only -> approval records the template.
    with _gate_open():
        assert run_once() is True
    assert sent_messages() == []
    approvals = _approvals(u1)
    assert len(approvals) == 1
    draft = json.loads(approvals[0]["draft_json"])
    assert draft["template_id"] == tpl["id"]
    assert draft["template_name"] == "Check-in"
    assert draft["draft_reply"] == (
        "Hi Follow-up Lead, checking in: 3-day check-in after showing"
    )


def _job_payload(job_id):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT payload_json FROM harness_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    return row["payload_json"] if row else "{}"


def test_follow_up_schedule_with_draft_template_refused(two_users, monkeypatch):
    """(d) Scheduling with a DRAFT template id is refused — nothing
    enqueued, no send row, no sms row."""
    _mock_env(monkeypatch)
    u1, _ = two_users
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    draft_tpl = _template(u1, name="Not approved", body="Hi {lead_name}")
    with pytest.raises(FollowUpScheduleError) as excinfo:
        schedule_follow_up_sms(
            u1, lead_id, follow_up_id, "caller body",
            _iso_in(), "3-day check-in after showing",
            template_id=draft_tpl["id"],
        )
    assert excinfo.value.code == "invalid_template_id"
    assert _send_row_for_lead(u1, lead_id) == []


def _send_row_for_lead(user_id, lead_id):
    with db.get_db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM harness_follow_up_sends WHERE user_id = ? AND lead_id = ?",
                (user_id, lead_id),
            ).fetchall()
        ]


def test_follow_up_fire_time_archive_drafts_in_supervised(two_users, monkeypatch):
    """(e) A template archived after scheduling -> supervised DRAFTs at
    fire (template_approved False at fire), approval created, no send."""
    from harness import templates_db

    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="supervised", paused=False)
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    tpl = _approved_template(u1, name="Check-in", body="Hi {lead_name}, checking in!")
    scheduled = schedule_follow_up_sms(
        u1, lead_id, follow_up_id, "caller body",
        _iso_in(), "3-day check-in after showing",
        template_id=tpl["id"],
    )
    templates_db.archive_template(u1, tpl["id"])
    with _gate_open():
        assert run_once() is True
    assert sent_messages() == []
    result = _job_result(scheduled["job_id"])
    assert result["decision"] == "DRAFT", result
    assert len(_approvals(u1)) == 1


def test_follow_up_fire_time_archive_still_allows_in_authorized(two_users, monkeypatch):
    """(e) Same archival, Authorized mode -> still ALLOWs (the
    schedule-time approval governs the send; the flag governs only
    the supervised auto-send)."""
    from harness import templates_db

    _mock_env(monkeypatch)
    u1, _ = two_users
    _enable_sending(u1)
    _set_settings(u1, mode="authorized", paused=False)
    lead_id, _ = _make_lead(u1)
    follow_up_id = _make_follow_up(u1, lead_id)
    tpl = _approved_template(u1, name="Check-in", body="Hi {lead_name}, checking in!")
    scheduled = schedule_follow_up_sms(
        u1, lead_id, follow_up_id, "caller body",
        _iso_in(), "3-day check-in after showing",
        template_id=tpl["id"],
    )
    templates_db.archive_template(u1, tpl["id"])
    with _gate_open():
        assert run_once() is True
    result = _job_result(scheduled["job_id"])
    assert result["decision"] == "ALLOW", result
    assert result["sent"] is True
    assert len(sent_messages()) == 1


# --- Validation (defense in depth) -------------------------------------------


# --- Agent surface (routes) --------------------------------------------------


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["session_version"] = 1


def test_templates_page_renders(app_client, two_users):
    """The agent page lists the tenant's templates."""
    u1, _ = two_users
    _template(u1, name="Page template", body="Hi {lead_name}")
    _login(app_client, u1)
    res = app_client.get("/harness/templates")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Message Templates" in html
    assert "Page template" in html


def test_templates_page_requires_login(app_client):
    """Logged out -> redirect to login, never the list."""
    res = app_client.get("/harness/templates")
    assert res.status_code in (302, 303)
    assert "/login" in res.headers.get("Location", "")


def test_templates_create_approve_edit_flow(app_client, two_users):
    """Create -> draft, approve -> approved, edit -> draft again,
    via the POST actions (the agent surface)."""
    from harness import templates_db

    u1, _ = two_users
    _login(app_client, u1)
    res = app_client.post(
        "/harness/templates",
        data={
            "name": "Route template",
            "channel": "sms",
            "purpose": "greeting",
            "body": "Hi {lead_name}, welcome!",
        },
        follow_redirects=False,
    )
    assert res.status_code in (302, 303)
    row = templates_db.get_template_by_name(u1, "Route template")
    assert row is not None
    assert row["status"] == "draft"

    res = app_client.post(
        f"/harness/templates/{row['id']}/approve", follow_redirects=False
    )
    assert res.status_code in (302, 303)
    assert templates_db.get_template(u1, row["id"])["status"] == "approved"

    res = app_client.post(
        f"/harness/templates/{row['id']}/edit",
        data={
            "name": "Route template",
            "channel": "sms",
            "purpose": "greeting",
            "body": "Hi {lead_name}, changed!",
        },
        follow_redirects=False,
    )
    assert res.status_code in (302, 303)
    edited = templates_db.get_template(u1, row["id"])
    assert edited["status"] == "draft"
    assert edited["body"] == "Hi {lead_name}, changed!"


def test_templates_api_lists_tenant_templates(app_client, two_users):
    """GET /api/harness/templates returns the tenant's templates as JSON."""
    u1, u2 = two_users
    _template(u1, name="Mine", body="Hi {lead_name}")
    _template(u2, name="Not mine", body="Hi {lead_name}")
    _login(app_client, u1)
    res = app_client.get("/api/harness/templates")
    assert res.status_code == 200
    names = [t["name"] for t in res.get_json()["templates"]]
    assert "Mine" in names
    assert "Not mine" not in names


def test_templates_cross_tenant_action_is_404(app_client, two_users):
    """Approving another tenant's template -> 404 (never revealed)."""
    u1, u2 = two_users
    other = _template(u2, name="Other tenant", body="Hi {lead_name}")
    _login(app_client, u1)
    res = app_client.post(
        f"/harness/templates/{other['id']}/approve", follow_redirects=False
    )
    assert res.status_code == 404


def test_validation_strips_unresolvable_template_id(two_users):
    """An sms_reply draft carrying a template id that does not resolve
    to an approved template for this tenant has the claim stripped —
    the exact approved text still executes (a human approved it)."""
    from harness.validation import validate_sms_reply_draft

    u1, _ = two_users
    lead_id, _ = _make_lead(u1)
    cleaned, error = validate_sms_reply_draft(
        u1,
        {
            "lead_id": lead_id,
            "inbound_id": 424242,
            "draft_reply": "Hi there.",
            "template_id": 999999,
            "template_name": "Ghost",
        },
    )
    assert error is None
    assert cleaned["template_id"] is None
    assert cleaned["draft_reply"] == "Hi there."


def test_validation_keeps_resolvable_template_id(two_users):
    """A draft carrying a live approved template id keeps it."""
    from harness.validation import validate_sms_reply_draft

    u1, _ = two_users
    lead_id, _ = _make_lead(u1)
    tpl = _approved_template(u1, name="Kept", body="Hi {lead_name}")
    cleaned, error = validate_sms_reply_draft(
        u1,
        {
            "lead_id": lead_id,
            "inbound_id": 424242,
            "draft_reply": "Hi Follow-up Lead",
            "template_id": tpl["id"],
            "template_name": "Kept",
        },
    )
    assert error is None
    assert cleaned["template_id"] == tpl["id"]
    assert cleaned["template_name"] == "Kept"
