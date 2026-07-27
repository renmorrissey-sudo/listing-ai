"""Appointment outcome suggestions require agent approval before applying."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import crm_db
import db
from crm_constants import (
    APPOINTMENT_OUTCOME_SUGGESTIONS,
    APPOINTMENT_OUTCOMES,
    build_appointment_outcome_suggestion,
    outcome_label,
)
from migrations.runner import apply_pending_migrations


def _lead(user_id, status="attempting_contact"):
    apply_pending_migrations()
    phone = f"+1555{uuid.uuid4().hex[:7]}"
    lead_id = db.upsert_lead(
        user_id, phone, {"name": "Appt Lead", "lead_type": "buyer"}, source="sms"
    )
    crm_db.set_lead_status(user_id, lead_id, status)
    return lead_id


def _appt(user_id, lead_id):
    start = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    appt_id, err = crm_db.create_appointment(
        user_id,
        {
            "lead_id": lead_id,
            "appointment_type": "buyer_consultation",
            "start_at": start,
            "end_at": start,
        },
    )
    assert err is None
    return appt_id


def _tasks_for_lead(user_id, lead_id):
    return [t for t in crm_db.list_tasks(user_id, bucket="all") if t.get("lead_id") == lead_id]


def _follow_ups_for_lead(user_id, lead_id):
    with db.get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM lead_follow_ups
            WHERE user_id = ? AND lead_id = ? AND status = 'pending'
            """,
            (user_id, lead_id),
        ).fetchall()
        return [dict(r) for r in rows]


def test_each_outcome_maps_to_expected_lead_status():
    expected = {
        "qualified_opportunity": "qualified",
        "follow_up_required": "contacted",
        "showing_scheduled": "appointment_scheduled",
        "listing_appointment_scheduled": "appointment_scheduled",
        "buyer_agreement_signed": "under_contract",
        "listing_agreement_signed": "under_contract",
        "not_ready": "nurture",
        "not_qualified": "closed_lost",
        "lost_to_another_agent": "closed_lost",
        "no_response": "attempting_contact",
        "no_show": "attempting_contact",
        "other": "appointment_completed",
    }
    assert set(expected) == set(APPOINTMENT_OUTCOMES)
    assert set(expected) == set(APPOINTMENT_OUTCOME_SUGGESTIONS)
    for outcome, status in expected.items():
        suggestion = build_appointment_outcome_suggestion(
            outcome, current_lead_status="new"
        )
        assert suggestion is not None
        assert suggestion["suggested_lead_status"] == status
        assert suggestion["outcome_label"] == outcome_label(outcome)
        assert suggestion["lead_status_would_change"] is True
        if outcome == "qualified_opportunity":
            assert status == "qualified"
            assert status != "appointment_scheduled"


def test_outcome_alone_saves_without_side_effects(two_users):
    u1, _ = two_users
    lead_id = _lead(u1, "appointment_scheduled")
    appt_id = _appt(u1, lead_id)

    result, err = crm_db.record_appointment_outcome(
        u1,
        appt_id,
        "qualified_opportunity",
        outcome_notes="Solid buyer fit",
        apply_lead_status=False,
        apply_follow_up=False,
        apply_task=False,
    )
    assert err is None
    assert result["ok"] is True
    lead = db.get_lead(lead_id, u1)
    assert lead["status"] == "appointment_scheduled"
    assert not _follow_ups_for_lead(u1, lead_id)
    assert not [
        t for t in _tasks_for_lead(u1, lead_id) if t["title"] == "Schedule buyer consultation"
    ]
    with db.get_db() as conn:
        appt = dict(
            conn.execute(
                "SELECT * FROM appointments WHERE id = ? AND user_id = ?",
                (appt_id, u1),
            ).fetchone()
        )
    assert appt["outcome"] == "qualified_opportunity"
    assert appt["outcome_notes"] == "Solid buyer fit"
    assert appt["status"] == "completed"
    assert "Outcome saved" in result["confirmation"]


def test_approved_status_follow_up_and_task_apply(two_users):
    u1, _ = two_users
    lead_id = _lead(u1, "appointment_scheduled")
    appt_id = _appt(u1, lead_id)

    result, err = crm_db.record_appointment_outcome(
        u1,
        appt_id,
        "qualified_opportunity",
        outcome_notes="Ready to buy",
        apply_lead_status=True,
        apply_follow_up=True,
        apply_task=True,
    )
    assert err is None
    lead = db.get_lead(lead_id, u1)
    assert lead["status"] == "qualified"
    assert lead.get("next_action") == "Schedule buyer consultation"
    assert lead.get("next_follow_up_at")
    follow_ups = _follow_ups_for_lead(u1, lead_id)
    assert len(follow_ups) == 1
    assert f"[appointment:{appt_id}]" in follow_ups[0]["reason"]
    tasks = [
        t for t in _tasks_for_lead(u1, lead_id) if t["title"] == "Schedule buyer consultation"
    ]
    assert len(tasks) == 1
    assert "Qualified" in result["confirmation"]
    assert "Follow-up scheduled" in result["confirmation"]
    assert "Task created" in result["confirmation"]


def test_unchecked_actions_are_not_applied(two_users):
    u1, _ = two_users
    lead_id = _lead(u1, "appointment_scheduled")
    appt_id = _appt(u1, lead_id)
    result, err = crm_db.record_appointment_outcome(
        u1, appt_id, "qualified_opportunity", apply_lead_status=False
    )
    assert err is None
    assert db.get_lead(lead_id, u1)["status"] == "appointment_scheduled"
    assert result["applied"]["lead_status"] is None


def test_repeated_submit_does_not_duplicate_records(two_users):
    u1, _ = two_users
    lead_id = _lead(u1, "appointment_scheduled")
    appt_id = _appt(u1, lead_id)
    kwargs = dict(
        outcome_notes="Great conversation",
        apply_lead_status=True,
        apply_follow_up=True,
        apply_task=True,
    )
    first, err = crm_db.record_appointment_outcome(
        u1, appt_id, "qualified_opportunity", **kwargs
    )
    assert err is None and first["duplicate"] is False
    second, err = crm_db.record_appointment_outcome(
        u1, appt_id, "qualified_opportunity", **kwargs
    )
    assert err is None
    assert len(_follow_ups_for_lead(u1, lead_id)) == 1
    assert (
        len(
            [
                t
                for t in _tasks_for_lead(u1, lead_id)
                if t["title"] == "Schedule buyer consultation"
            ]
        )
        == 1
    )
    activities = [
        a
        for a in crm_db.list_lead_activities(u1, lead_id, for_timeline=False)
        if a["event_type"] == "appointment_outcome"
    ]
    assert len(activities) == 1
    status_events = [
        a
        for a in crm_db.list_lead_activities(u1, lead_id, for_timeline=False)
        if a["event_type"] == "status_change" and "Qualified" in (a.get("summary") or "")
    ]
    assert len(status_events) == 1


def test_outcome_preview_and_idempotent_save(two_users):
    u1, _ = two_users
    lead_id = _lead(u1, "attempting_contact")
    appt_id = _appt(u1, lead_id)
    preview, err = crm_db.preview_appointment_outcome(u1, appt_id, "qualified_opportunity")
    assert err is None
    assert preview["suggested_lead_status"] == "qualified"
    assert preview["suggested_next_action"]

    first, err = crm_db.record_appointment_outcome(
        u1, appt_id, "qualified_opportunity", outcome_notes="Great conversation"
    )
    assert err is None and first["duplicate"] is False
    second, err = crm_db.record_appointment_outcome(
        u1, appt_id, "qualified_opportunity", outcome_notes="Great conversation"
    )
    assert err is None and second["duplicate"] is True
    activities = [
        a
        for a in crm_db.list_lead_activities(u1, lead_id, for_timeline=False)
        if a["event_type"] == "appointment_outcome"
    ]
    assert len(activities) == 1


def test_approved_status_change_writes_descriptive_timeline(two_users):
    u1, _ = two_users
    lead_id = _lead(u1, "attempting_contact")
    appt_id = _appt(u1, lead_id)
    result, err = crm_db.record_appointment_outcome(
        u1, appt_id, "not_qualified", apply_lead_status=True
    )
    assert err is None
    assert db.get_lead(lead_id, u1)["status"] == "closed_lost"
    activities = crm_db.list_lead_activities(u1, lead_id, for_timeline=False)
    summaries = [a["summary"] for a in activities]
    assert any(s.startswith("Appointment outcome saved: Not Qualified") for s in summaries)
    assert any(
        "Lead status changed from Attempting Contact to Closed Lost" in s for s in summaries
    )
    assert "Closed Lost" in result["confirmation"]


def test_no_show_can_open_needs_attention_when_approved(two_users):
    u1, _ = two_users
    lead_id = _lead(u1, "appointment_scheduled")
    appt_id = _appt(u1, lead_id)
    result, err = crm_db.record_appointment_outcome(
        u1,
        appt_id,
        "no_show",
        apply_lead_status=True,
        apply_needs_attention=True,
    )
    assert err is None
    lead = db.get_lead(lead_id, u1)
    assert lead["status"] == "attempting_contact"
    items = crm_db.list_needs_attention(u1)
    assert any(i["reason_code"] == "appointment_no_show" for i in items)
    assert "Needs Attention" in result["confirmation"]


def test_invalid_appointment_returns_error(two_users):
    u1, _ = two_users
    result, err = crm_db.record_appointment_outcome(u1, 999999, "qualified_opportunity")
    assert result is None
    assert err == "Appointment not found."


def test_cross_tenant_access_blocked(two_users):
    u1, u2 = two_users
    lead_id = _lead(u1, "appointment_scheduled")
    appt_id = _appt(u1, lead_id)
    result, err = crm_db.record_appointment_outcome(
        u2, appt_id, "qualified_opportunity", apply_lead_status=True
    )
    assert result is None
    assert err == "Appointment not found."
    assert db.get_lead(lead_id, u1)["status"] == "appointment_scheduled"


def test_database_failure_rolls_back_all_changes(two_users):
    u1, _ = two_users
    lead_id = _lead(u1, "appointment_scheduled")
    appt_id = _appt(u1, lead_id)
    real_insert = crm_db._insert_activity

    def flaky(conn, lead_id_arg, user_id, event_type, summary, payload, actor_user_id=None):
        if event_type == "task_created":
            raise RuntimeError("forced task failure")
        return real_insert(
            conn, lead_id_arg, user_id, event_type, summary, payload, actor_user_id
        )

    with patch.object(crm_db, "_insert_activity", side_effect=flaky):
        try:
            crm_db.record_appointment_outcome(
                u1,
                appt_id,
                "qualified_opportunity",
                outcome_notes="Should roll back",
                apply_lead_status=True,
                apply_follow_up=True,
                apply_task=True,
            )
            raised = False
        except RuntimeError:
            raised = True
    assert raised is True
    lead = db.get_lead(lead_id, u1)
    assert lead["status"] == "appointment_scheduled"
    assert not _follow_ups_for_lead(u1, lead_id)
    assert not [
        t for t in _tasks_for_lead(u1, lead_id) if t["title"] == "Schedule buyer consultation"
    ]
    with db.get_db() as conn:
        appt = dict(
            conn.execute(
                "SELECT * FROM appointments WHERE id = ? AND user_id = ?",
                (appt_id, u1),
            ).fetchone()
        )
    assert not appt.get("outcome")


def test_api_preview_and_save_require_auth_and_ownership(app_client, two_users):
    u1, u2 = two_users
    lead_id = _lead(u1, "attempting_contact")
    appt_id = _appt(u1, lead_id)
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    preview = app_client.post(
        f"/api/crm/appointments/{appt_id}/outcome-preview",
        json={"outcome": "showing_scheduled"},
    )
    assert preview.status_code == 200
    body = preview.get_json()["preview"]
    assert body["suggested_lead_status"] == "appointment_scheduled"

    save = app_client.post(
        f"/api/crm/appointments/{appt_id}/outcome",
        json={
            "outcome": "qualified_opportunity",
            "apply_lead_status": True,
            "apply_follow_up": True,
            "apply_task": True,
        },
    )
    assert save.status_code == 200
    body = save.get_json()
    assert "Qualified" in body["confirmation"]
    assert body["applied"]["task_id"]

    with app_client.session_transaction() as sess:
        sess["user_id"] = u2
    denied = app_client.post(
        f"/api/crm/appointments/{appt_id}/outcome",
        json={"outcome": "not_ready", "apply_lead_status": True},
    )
    assert denied.status_code == 404
    assert db.get_lead(lead_id, u1)["status"] == "qualified"


def test_form_post_redirects_with_success_message(app_client, two_users):
    u1, _ = two_users
    lead_id = _lead(u1, "appointment_scheduled")
    appt_id = _appt(u1, lead_id)
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1

    res = app_client.post(
        f"/crm/leads/{lead_id}/appointments/{appt_id}/outcome",
        data={
            "outcome": "qualified_opportunity",
            "outcome_notes": "Ready to buy",
            "apply_lead_status": "1",
            "apply_follow_up": "1",
            "apply_task": "1",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["Location"].endswith(f"/crm/leads/{lead_id}")

    follow = app_client.get(f"/crm/leads/{lead_id}")
    assert follow.status_code == 200
    html = follow.get_data(as_text=True)
    assert "Outcome saved" in html
    assert "Qualified" in html
    assert "Schedule buyer consultation" in html
    lead = db.get_lead(lead_id, u1)
    assert lead["status"] == "qualified"


def test_form_post_invalid_appointment_shows_error(app_client, two_users):
    u1, _ = two_users
    lead_id = _lead(u1, "appointment_scheduled")
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    res = app_client.post(
        f"/crm/leads/{lead_id}/appointments/999999/outcome",
        data={"outcome": "qualified_opportunity", "apply_lead_status": "1"},
    )
    assert res.status_code == 404
    assert "Appointment not found" in res.get_data(as_text=True)


def test_form_keeps_values_on_validation_error(app_client, two_users):
    u1, _ = two_users
    lead_id = _lead(u1, "appointment_scheduled")
    appt_id = _appt(u1, lead_id)
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    res = app_client.post(
        f"/crm/leads/{lead_id}/appointments/{appt_id}/outcome",
        data={
            "outcome": "not_a_real_outcome",
            "outcome_notes": "Keep these notes",
            "apply_lead_status": "1",
        },
    )
    assert res.status_code == 400
    html = res.get_data(as_text=True)
    assert "Invalid appointment outcome" in html
    assert "Keep these notes" in html


def test_api_returns_500_when_save_raises(app_client, two_users):
    u1, _ = two_users
    lead_id = _lead(u1, "appointment_scheduled")
    appt_id = _appt(u1, lead_id)
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    with patch.object(
        crm_db, "record_appointment_outcome", side_effect=RuntimeError("boom")
    ):
        res = app_client.post(
            f"/api/crm/appointments/{appt_id}/outcome",
            json={"outcome": "qualified_opportunity"},
        )
    assert res.status_code == 500
    assert "Could not save" in res.get_json()["error"]
