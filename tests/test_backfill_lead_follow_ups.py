"""scripts/backfill_lead_follow_ups.py: reconcile structured evidence of a
determined future action (leads.next_follow_up_at drift, or a past Claude SMS
coaching suggestion) into a real lead_follow_ups record.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

import crm_db
import db
from migrations.runner import apply_pending_migrations
from scripts import backfill_lead_follow_ups as backfill


def _lead(user_id, name="Backfill Lead"):
    apply_pending_migrations()
    phone = f"+1555{uuid.uuid4().hex[:7]}"
    return db.upsert_lead(
        user_id, phone, {"name": name, "lead_type": "buyer"}, source="sms"
    )


def _open_follow_ups(user_id, lead_id):
    return [
        f
        for f in crm_db.list_lead_follow_ups(user_id, lead_id, include_completed=True)
        if f["status"] == "pending"
    ]


def _set_next_follow_up_at_directly(user_id, lead_id, due_at, reason):
    """Simulate historical drift: next_follow_up_at set without a matching
    lead_follow_ups row (as could happen before this fix existed)."""
    with db.get_db() as conn:
        conn.execute(
            """
            UPDATE leads
            SET next_follow_up_at = ?, follow_up_reason = ?
            WHERE id = ? AND user_id = ?
            """,
            (due_at, reason, lead_id, user_id),
        )


def _create_insight_with_suggestion(user_id, lead_id, due_at, reason):
    raw_json = json.dumps(
        {
            "recommended_next_action": "Confirm the reschedule with the lead",
            "suggested_follow_up_at": due_at,
            "suggested_follow_up_reason": reason,
        }
    )
    return db.create_lead_insight(
        lead_id,
        user_id,
        None,
        {
            "summary": "Lead asked to reschedule.",
            "intent": "reschedule",
            "next_best_step": "Confirm reschedule",
            "recommended_action": "Confirm reschedule",
            "suggested_reply": "",
            "home_value_pitch": None,
            "confidence_score": 0.9,
            "requires_manual_review": False,
            "escalation_topics": [],
            "raw_json": raw_json,
        },
        model="claude",
    )


def test_backfills_from_next_follow_up_at_drift(two_users):
    u1, _ = two_users
    lead_id = _lead(u1, "Drift Lead")
    due = (datetime.now(timezone.utc) + timedelta(days=-1)).isoformat()
    _set_next_follow_up_at_directly(u1, lead_id, due, "sms follow up")
    assert _open_follow_ups(u1, lead_id) == []

    candidates = backfill.find_candidates()
    assert any(c["user_id"] == u1 and c["lead_id"] == lead_id for c in candidates)

    for c in candidates:
        if c["user_id"] != u1 or c["lead_id"] != lead_id:
            continue
        result, err = crm_db.set_lead_follow_up(
            c["user_id"], c["lead_id"], c["due_at"], c["reason"], priority=c["priority"]
        )
        assert err is None
        assert result["created"] is True

    items = _open_follow_ups(u1, lead_id)
    assert len(items) == 1
    assert items[0]["due_at"] == due

    # Idempotent: re-running finds no more candidates for this lead.
    candidates2 = backfill.find_candidates()
    assert not any(c["user_id"] == u1 and c["lead_id"] == lead_id for c in candidates2)


def test_backfills_from_lead_insight_suggestion(two_users):
    u1, _ = two_users
    lead_id = _lead(u1, "Sarah Johnson")
    due = (datetime.now(timezone.utc) + timedelta(days=-2)).isoformat()
    _create_insight_with_suggestion(
        u1, lead_id, due, "Confirm the reschedule with Sarah"
    )
    assert _open_follow_ups(u1, lead_id) == []

    candidates = backfill.find_candidates()
    match = [c for c in candidates if c["user_id"] == u1 and c["lead_id"] == lead_id]
    assert len(match) == 1
    assert match[0]["source"] == "lead_insights.raw_json"
    assert match[0]["reason"] == "Confirm the reschedule with Sarah"


def test_skips_leads_with_no_structured_evidence(two_users):
    u1, _ = two_users
    lead_id = _lead(u1, "No Evidence Lead")
    db.update_lead_from_analysis(
        lead_id, u1, next_action="Should probably contact again"
    )
    candidates = backfill.find_candidates()
    assert not any(c["user_id"] == u1 and c["lead_id"] == lead_id for c in candidates)


def test_dry_run_does_not_write(two_users, capsys, monkeypatch):
    u1, _ = two_users
    lead_id = _lead(u1, "Dry Run Lead")
    due = (datetime.now(timezone.utc) + timedelta(days=-1)).isoformat()
    _set_next_follow_up_at_directly(u1, lead_id, due, "sms follow up")

    monkeypatch.setattr("sys.argv", ["backfill_lead_follow_ups.py"])
    backfill.main()
    out = capsys.readouterr().out
    assert "Dry run only" in out
    assert _open_follow_ups(u1, lead_id) == []


def test_apply_writes_and_is_idempotent(two_users, monkeypatch):
    u1, _ = two_users
    lead_id = _lead(u1, "Apply Lead")
    due = (datetime.now(timezone.utc) + timedelta(days=-1)).isoformat()
    _set_next_follow_up_at_directly(u1, lead_id, due, "Applied follow up")

    monkeypatch.setattr("sys.argv", ["backfill_lead_follow_ups.py", "--apply"])
    backfill.main()
    items = _open_follow_ups(u1, lead_id)
    assert len(items) == 1

    # Second run is a no-op (already has an open follow-up).
    backfill.main()
    assert len(_open_follow_ups(u1, lead_id)) == 1


def test_tenant_scoped(two_users):
    u1, u2 = two_users
    lead1 = _lead(u1, "Tenant One Lead")
    lead2 = _lead(u2, "Tenant Two Lead")
    due = (datetime.now(timezone.utc) + timedelta(days=-1)).isoformat()
    _set_next_follow_up_at_directly(u1, lead1, due, "Tenant one follow up")
    _set_next_follow_up_at_directly(u2, lead2, due, "Tenant two follow up")

    candidates = backfill.find_candidates()
    u1_candidates = [c for c in candidates if c["lead_id"] == lead1]
    u2_candidates = [c for c in candidates if c["lead_id"] == lead2]
    assert u1_candidates and u1_candidates[0]["user_id"] == u1
    assert u2_candidates and u2_candidates[0]["user_id"] == u2
