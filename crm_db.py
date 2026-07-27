"""Phase 2 CRM persistence helpers. All reads/writes are user-scoped."""

from datetime import datetime, timedelta, timezone
import json
import re

from crm_constants import (
    APPOINTMENT_OUTCOMES,
    APPOINTMENT_STATUSES,
    APPOINTMENT_TYPES,
    CONFIDENCE_THRESHOLD,
    FIRST_RESPONSE_HOURS,
    NEEDS_ATTENTION_REASONS,
    PIPELINE_STAGES,
    PRIORITIES,
    PROTECTED_RESOLVE_REASONS,
    TASK_STATUSES,
    TASK_TYPES,
    build_appointment_outcome_suggestion,
    normalize_lead_status,
    outcome_label,
    status_label,
)
from db import get_db, merge_lead_call_outcome_notes


def _now():
    return datetime.now(timezone.utc).isoformat()


# Transient Vapi / dialer states — never show these on the agent timeline.
VOICE_TRANSIENT_STATUSES = frozenset(
    {
        "queued",
        "ringing",
        "initiated",
        "pending",
        "started",
        "starting",
        "scheduled",
        "connecting",
        "loading",
        "queued-for-retry",
    }
)

VOICE_ACTIVITY_EVENT_TYPES = frozenset(
    {
        "voice_call_started",
        "voice_call_updated",
        "voice_call_completed",
        "voice_call_failed",
        "voice_call_connected",
        "voice_call_unanswered",
        "voice_call_cancelled",
    }
)


def parse_activity_payload(activity):
    try:
        payload = json.loads((activity or {}).get("payload_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _voice_activity_status_tokens(activity):
    payload = parse_activity_payload(activity)
    tokens = {
        str(payload.get("status") or "").lower().strip(),
        str(payload.get("lifecycle_status") or "").lower().strip(),
        str(payload.get("outcome") or "").lower().strip(),
        str(payload.get("ended_reason") or "").lower().strip(),
        str(payload.get("meaningful_status") or "").lower().strip(),
    }
    summary = str((activity or {}).get("summary") or "").lower()
    for marker in VOICE_TRANSIENT_STATUSES:
        if marker in summary:
            tokens.add(marker)
    return {t for t in tokens if t}


def is_transient_voice_timeline_activity(activity):
    """True for low-value dialer noise that should stay out of the agent timeline."""
    event_type = (activity or {}).get("event_type") or ""
    if event_type not in VOICE_ACTIVITY_EVENT_TYPES:
        return False
    if event_type == "voice_call_started":
        return True
    if event_type == "voice_call_completed":
        return False
    if event_type == "voice_call_failed":
        return False

    payload = parse_activity_payload(activity)
    # Keep rows that already carry agent-useful artifacts.
    if payload.get("has_recording") or payload.get("has_transcript") or payload.get("summary"):
        if event_type == "voice_call_completed":
            return False
    tokens = _voice_activity_status_tokens(activity)
    if tokens & VOICE_TRANSIENT_STATUSES and not (
        payload.get("has_recording")
        or payload.get("has_transcript")
        or (payload.get("summary") and event_type == "voice_call_completed")
    ):
        return True
    # Legacy noisy rows: "AI call started: queued", "AI call updated: ringing", etc.
    summary = str((activity or {}).get("summary") or "").lower().strip()
    if summary.startswith("ai call started") or summary.startswith("ai call updated"):
        if any(marker in summary for marker in VOICE_TRANSIENT_STATUSES):
            return True
        if summary in {"ai call started", "ai call updated", "ai call started:", "ai call updated:"}:
            return True
    return False


def filter_voice_activities_for_timeline(activities):
    """Drop transient voice noise and superseded start/update rows for completed calls."""
    completed_call_ids = set()
    for activity in activities:
        if activity.get("event_type") != "voice_call_completed":
            continue
        payload = parse_activity_payload(activity)
        try:
            voice_call_id = int(payload.get("voice_call_id") or 0)
        except (TypeError, ValueError):
            voice_call_id = 0
        if voice_call_id:
            completed_call_ids.add(voice_call_id)

    filtered = []
    for activity in activities:
        if is_transient_voice_timeline_activity(activity):
            continue
        event_type = activity.get("event_type") or ""
        if event_type in {"voice_call_started", "voice_call_updated", "voice_call_connected"}:
            payload = parse_activity_payload(activity)
            try:
                voice_call_id = int(payload.get("voice_call_id") or 0)
            except (TypeError, ValueError):
                voice_call_id = 0
            if voice_call_id and voice_call_id in completed_call_ids:
                continue
        filtered.append(activity)
    return filtered


def add_lead_activity(lead_id, user_id, event_type, summary, payload=None, actor_user_id=None):
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO lead_activities
                (lead_id, user_id, actor_user_id, event_type, summary, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lead_id,
                user_id,
                actor_user_id or user_id,
                event_type,
                summary,
                json.dumps(payload or {})[:4000],
                _now(),
            ),
        )
        return cur.lastrowid


def list_lead_activities(user_id, lead_id, limit=100, *, for_timeline=False):
    fetch_limit = max(int(limit or 100), 1)
    if for_timeline:
        # Over-fetch so filtering transient voice noise still fills the page.
        fetch_limit = min(max(fetch_limit * 4, 50), 400)
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM lead_activities
            WHERE user_id = ? AND lead_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, lead_id, fetch_limit),
        ).fetchall()
        items = [dict(r) for r in rows]
    if for_timeline:
        items = filter_voice_activities_for_timeline(items)
    return items[: max(int(limit or 100), 1)]


def find_lead_activity_for_voice_call(user_id, lead_id, voice_call_id, event_type=None):
    """Return the newest matching activity for a voice call, if any.

    When event_type is None, match any voice-call activity for that call so
    retries can update one consolidated timeline row.
    """
    if not voice_call_id:
        return None
    voice_call_id = int(voice_call_id)
    allowed = {event_type} if event_type else VOICE_ACTIVITY_EVENT_TYPES
    for activity in list_lead_activities(user_id, lead_id, limit=200, for_timeline=False):
        if activity.get("event_type") not in allowed:
            continue
        payload = parse_activity_payload(activity)
        try:
            if int(payload.get("voice_call_id") or 0) == voice_call_id:
                return activity
        except (TypeError, ValueError):
            continue
    return None


def update_lead_activity(user_id, activity_id, summary=None, payload=None, event_type=None):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM lead_activities WHERE id = ? AND user_id = ?",
            (activity_id, user_id),
        ).fetchone()
        if not row:
            return None
        new_summary = summary if summary is not None else row["summary"]
        new_event_type = event_type if event_type is not None else row["event_type"]
        if payload is not None:
            new_payload = json.dumps(payload)[:4000]
        else:
            new_payload = row["payload_json"]
        conn.execute(
            """
            UPDATE lead_activities
            SET summary = ?, payload_json = ?, event_type = ?
            WHERE id = ? AND user_id = ?
            """,
            (new_summary, new_payload, new_event_type, activity_id, user_id),
        )
        updated = conn.execute(
            "SELECT * FROM lead_activities WHERE id = ? AND user_id = ?",
            (activity_id, user_id),
        ).fetchone()
        return dict(updated) if updated else None


def set_lead_status(user_id, lead_id, new_status, actor_user_id=None, from_automation=False):
    new_status = normalize_lead_status(new_status)
    with get_db() as conn:
        lead = conn.execute(
            "SELECT * FROM leads WHERE id = ? AND user_id = ?",
            (lead_id, user_id),
        ).fetchone()
        if not lead:
            return None, "Lead not found."
        previous = normalize_lead_status(lead["status"])
        if from_automation and previous == "do_not_contact" and new_status != "do_not_contact":
            return None, "Do Not Contact cannot be removed by automation."
        if from_automation and (lead["opt_out_status"] or "") == "opted_out" and new_status != "do_not_contact":
            return None, "Opted-out leads cannot leave Do Not Contact via automation."
        if previous == new_status:
            return dict(lead), None
        now = _now()
        conn.execute(
            "UPDATE leads SET status = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (new_status, now, lead_id, user_id),
        )
    add_lead_activity(
        lead_id,
        user_id,
        "status_change",
        f"Lead status changed from {status_label(previous)} to {status_label(new_status)}",
        {"previous_status": previous, "new_status": new_status},
        actor_user_id=actor_user_id or user_id,
    )
    with get_db() as conn:
        lead = conn.execute(
            "SELECT * FROM leads WHERE id = ? AND user_id = ?",
            (lead_id, user_id),
        ).fetchone()
        return dict(lead) if lead else None, None


def set_lead_follow_up(user_id, lead_id, due_at, reason, priority="normal", created_by=None):
    priority = priority if priority in PRIORITIES else "normal"
    now = _now()
    with get_db() as conn:
        lead = conn.execute(
            "SELECT id FROM leads WHERE id = ? AND user_id = ?",
            (lead_id, user_id),
        ).fetchone()
        if not lead:
            return None, "Lead not found."
        conn.execute(
            """
            UPDATE leads
            SET next_follow_up_at = ?, follow_up_reason = ?, follow_up_priority = ?,
                follow_up_completed_at = NULL, follow_up_created_by = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (due_at, reason, priority, created_by or user_id, now, lead_id, user_id),
        )
        cur = conn.execute(
            """
            INSERT INTO lead_follow_ups
                (lead_id, user_id, due_at, reason, status, created_at, priority, created_by)
            VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (lead_id, user_id, due_at, reason, now, priority, created_by or user_id),
        )
        follow_up_id = cur.lastrowid
    add_lead_activity(
        lead_id,
        user_id,
        "follow_up_scheduled",
        f"Follow-up scheduled: {reason or 'Follow up'}",
        {"due_at": due_at, "priority": priority, "follow_up_id": follow_up_id},
        actor_user_id=created_by or user_id,
    )
    return follow_up_id, None


def complete_lead_follow_up(user_id, lead_id):
    now = _now()
    with get_db() as conn:
        lead = conn.execute(
            "SELECT next_follow_up_at, follow_up_reason, follow_up_priority FROM leads WHERE id = ? AND user_id = ?",
            (lead_id, user_id),
        ).fetchone()
        if not lead:
            return False, "Lead not found."
        conn.execute(
            """
            UPDATE leads
            SET follow_up_completed_at = ?, next_follow_up_at = NULL, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (now, now, lead_id, user_id),
        )
        conn.execute(
            """
            UPDATE lead_follow_ups
            SET status = 'done', completed_at = ?
            WHERE lead_id = ? AND user_id = ? AND status = 'pending'
            """,
            (now, lead_id, user_id),
        )
    add_lead_activity(
        lead_id,
        user_id,
        "follow_up_completed",
        "Follow-up marked complete",
        {
            "original_due_at": lead["next_follow_up_at"],
            "reason": lead["follow_up_reason"],
            "priority": lead["follow_up_priority"],
        },
    )
    return True, None


def dismiss_lead_follow_up(user_id, lead_id, reason="Dismissed"):
    now = _now()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE leads
            SET next_follow_up_at = NULL, follow_up_reason = NULL, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (now, lead_id, user_id),
        )
        conn.execute(
            """
            UPDATE lead_follow_ups
            SET status = 'cancelled', completed_at = ?
            WHERE lead_id = ? AND user_id = ? AND status = 'pending'
            """,
            (now, lead_id, user_id),
        )
    add_lead_activity(lead_id, user_id, "follow_up_dismissed", reason, {})
    return True


def create_task(user_id, data):
    now = _now()
    title = str(data.get("title") or "").strip()[:200]
    if not title:
        return None, "Task title is required."
    task_type = data.get("task_type") if data.get("task_type") in TASK_TYPES else "general_follow_up"
    priority = data.get("priority") if data.get("priority") in PRIORITIES else "normal"
    status = data.get("status") if data.get("status") in TASK_STATUSES else "open"
    lead_id = data.get("lead_id")
    if lead_id:
        with get_db() as conn:
            lead = conn.execute(
                "SELECT id FROM leads WHERE id = ? AND user_id = ?",
                (lead_id, user_id),
            ).fetchone()
            if not lead:
                return None, "Lead not found."
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO tasks
                (user_id, lead_id, assigned_user_id, title, description, due_at, priority,
                 status, task_type, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                lead_id,
                data.get("assigned_user_id") or user_id,
                title,
                str(data.get("description") or "")[:2000],
                data.get("due_at"),
                priority,
                status,
                task_type,
                now,
                now,
            ),
        )
        task_id = cur.lastrowid
    if lead_id:
        add_lead_activity(
            lead_id,
            user_id,
            "task_created",
            f"Task created: {title}",
            {"task_id": task_id, "task_type": task_type, "due_at": data.get("due_at")},
        )
    create_notification(
        user_id,
        "task_assigned",
        "Task assigned",
        title,
        link="/crm/tasks",
        lead_id=lead_id,
    )
    return task_id, None


def _calendar_day(local_date=None):
    """YYYY-MM-DD for task bucketing. Prefer the agent's browser local date when provided."""
    value = str(local_date or "").strip()[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def list_tasks(user_id, bucket="all", limit=100, local_date=None):
    """Bucket tasks by calendar due date (YYYY-MM-DD prefix of due_at).

    Pass local_date from the browser so "Due today" matches the agent's local day,
    not only the server UTC day.
    """
    day = _calendar_day(local_date)
    with get_db() as conn:
        if bucket == "overdue":
            # Overdue only when the due calendar day is at least 1 day before today.
            # Same-day tasks stay in "Due today" even if the clock time has passed.
            rows = conn.execute(
                """
                SELECT t.*, l.name AS lead_name, l.phone_number
                FROM tasks t
                LEFT JOIN leads l ON l.id = t.lead_id
                WHERE t.user_id = ? AND t.status IN ('open', 'in_progress')
                  AND t.due_at IS NOT NULL
                  AND substr(t.due_at, 1, 10) < ?
                ORDER BY t.due_at ASC
                LIMIT ?
                """,
                (user_id, day, limit),
            ).fetchall()
        elif bucket == "today":
            # Match the calendar date shown/stored on due_at (first 10 chars).
            rows = conn.execute(
                """
                SELECT t.*, l.name AS lead_name, l.phone_number
                FROM tasks t
                LEFT JOIN leads l ON l.id = t.lead_id
                WHERE t.user_id = ? AND t.status IN ('open', 'in_progress')
                  AND t.due_at IS NOT NULL
                  AND substr(t.due_at, 1, 10) = ?
                ORDER BY t.due_at ASC
                LIMIT ?
                """,
                (user_id, day, limit),
            ).fetchall()
        elif bucket == "upcoming":
            rows = conn.execute(
                """
                SELECT t.*, l.name AS lead_name, l.phone_number
                FROM tasks t
                LEFT JOIN leads l ON l.id = t.lead_id
                WHERE t.user_id = ? AND t.status IN ('open', 'in_progress')
                  AND (
                    t.due_at IS NULL
                    OR substr(t.due_at, 1, 10) > ?
                  )
                ORDER BY t.due_at ASC
                LIMIT ?
                """,
                (user_id, day, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT t.*, l.name AS lead_name, l.phone_number
                FROM tasks t
                LEFT JOIN leads l ON l.id = t.lead_id
                WHERE t.user_id = ?
                ORDER BY COALESCE(t.due_at, t.created_at) ASC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def get_task(user_id, task_id):
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT t.*, l.name AS lead_name, l.phone_number
            FROM tasks t
            LEFT JOIN leads l ON l.id = t.lead_id
            WHERE t.id = ? AND t.user_id = ?
            """,
            (task_id, user_id),
        ).fetchone()
        return dict(row) if row else None


def update_task(user_id, task_id, data):
    title = str(data.get("title") or "").strip()[:200]
    if not title:
        return None, "Task title is required."
    task_type = data.get("task_type") if data.get("task_type") in TASK_TYPES else "general_follow_up"
    priority = data.get("priority") if data.get("priority") in PRIORITIES else "normal"
    due_at = data.get("due_at")
    if due_at == "":
        due_at = None
    description = str(data.get("description") or "")[:2000]
    lead_id = data.get("lead_id")
    if lead_id in ("", None):
        lead_id = None
    elif lead_id is not None:
        try:
            lead_id = int(lead_id)
        except (TypeError, ValueError):
            return None, "Invalid lead."
        with get_db() as conn:
            lead = conn.execute(
                "SELECT id FROM leads WHERE id = ? AND user_id = ?",
                (lead_id, user_id),
            ).fetchone()
            if not lead:
                return None, "Lead not found."

    now = _now()
    with get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        ).fetchone()
        if not existing:
            return None, "Task not found."
        # Keep existing lead when not provided in payload
        if "lead_id" not in data:
            lead_id = existing["lead_id"]
        conn.execute(
            """
            UPDATE tasks
            SET title = ?, description = ?, due_at = ?, priority = ?, task_type = ?,
                lead_id = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (title, description, due_at, priority, task_type, lead_id, now, task_id, user_id),
        )
    activity_lead = lead_id or existing["lead_id"]
    if activity_lead:
        add_lead_activity(
            activity_lead,
            user_id,
            "task_updated",
            f"Task updated: {title}",
            {
                "task_id": task_id,
                "task_type": task_type,
                "due_at": due_at,
                "priority": priority,
            },
        )
    return get_task(user_id, task_id), None


def complete_task(user_id, task_id):
    now = _now()
    with get_db() as conn:
        task = conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        ).fetchone()
        if not task:
            return None, "Task not found."
        conn.execute(
            """
            UPDATE tasks
            SET status = 'completed', completed_at = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (now, now, task_id, user_id),
        )
    if task["lead_id"]:
        add_lead_activity(
            task["lead_id"],
            user_id,
            "task_completed",
            f"Task completed: {task['title']}",
            {"task_id": task_id},
        )
    resolve_needs_attention_for_source(user_id, "task", task_id, "Task completed")
    return dict(task), None


def cancel_task(user_id, task_id):
    now = _now()
    with get_db() as conn:
        task = conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        ).fetchone()
        if not task:
            return None, "Task not found."
        conn.execute(
            """
            UPDATE tasks
            SET status = 'cancelled', updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (now, task_id, user_id),
        )
    if task["lead_id"]:
        add_lead_activity(
            task["lead_id"],
            user_id,
            "task_cancelled",
            f"Task cancelled: {task['title']}",
            {"task_id": task_id},
        )
    resolve_needs_attention_for_source(user_id, "task", task_id, "Task cancelled")
    return True, None


def create_appointment(user_id, data):
    now = _now()
    lead_id = data.get("lead_id")
    start_at = data.get("start_at")
    if not lead_id or not start_at:
        return None, "Lead and start time are required."
    with get_db() as conn:
        lead = conn.execute(
            "SELECT id FROM leads WHERE id = ? AND user_id = ?",
            (lead_id, user_id),
        ).fetchone()
        if not lead:
            return None, "Lead not found."
        appt_type = data.get("appointment_type") if data.get("appointment_type") in APPOINTMENT_TYPES else "phone_call"
        status = data.get("status") if data.get("status") in APPOINTMENT_STATUSES else "scheduled"
        cur = conn.execute(
            """
            INSERT INTO appointments
                (user_id, lead_id, appointment_type, start_at, end_at, location, notes,
                 status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                lead_id,
                appt_type,
                start_at,
                data.get("end_at"),
                str(data.get("location") or "")[:500],
                str(data.get("notes") or "")[:2000],
                status,
                now,
                now,
            ),
        )
        appt_id = cur.lastrowid
    add_lead_activity(
        lead_id,
        user_id,
        "appointment_scheduled",
        f"Appointment scheduled ({appt_type.replace('_', ' ')})",
        {"appointment_id": appt_id, "start_at": start_at},
    )
    create_notification(
        user_id,
        "appointment_upcoming",
        "Appointment scheduled",
        f"Starts {start_at[:16].replace('T', ' ')} UTC",
        link=f"/crm/leads/{lead_id}" if lead_id else "/crm/leads",
        lead_id=lead_id,
    )
    return appt_id, None


def preview_appointment_outcome(user_id, appointment_id, outcome, next_action=""):
    """Return suggestion preview for an appointment outcome (no writes)."""
    if outcome not in APPOINTMENT_OUTCOMES:
        return None, "Invalid appointment outcome."
    with get_db() as conn:
        appt = conn.execute(
            "SELECT * FROM appointments WHERE id = ? AND user_id = ?",
            (appointment_id, user_id),
        ).fetchone()
        if not appt:
            return None, "Appointment not found."
        lead = conn.execute(
            "SELECT * FROM leads WHERE id = ? AND user_id = ?",
            (appt["lead_id"], user_id),
        ).fetchone()
    suggestion = build_appointment_outcome_suggestion(
        outcome,
        current_lead_status=(lead["status"] if lead else None),
        next_action_override=next_action,
    )
    if not suggestion:
        return None, "No suggestion available for this outcome."
    suggestion.update(
        {
            "appointment_id": appointment_id,
            "lead_id": appt["lead_id"],
            "current_appointment_status": appt.get("status"),
            "saved_outcome": appt.get("outcome"),
            "saved_outcome_notes": appt.get("outcome_notes"),
            "saved_next_action": appt.get("next_action"),
        }
    )
    return suggestion, None


def record_appointment_outcome(
    user_id,
    appointment_id,
    outcome,
    outcome_notes="",
    next_action="",
    *,
    apply_lead_status=False,
    apply_follow_up=False,
    apply_task=False,
    apply_needs_attention=False,
):
    """Save appointment outcome. Lead/status/task/follow-up changes require approval flags."""
    if outcome not in APPOINTMENT_OUTCOMES:
        return None, "Invalid appointment outcome."
    now = _now()
    outcome_notes = str(outcome_notes or "")[:2000]
    next_action = str(next_action or "")[:500]
    apply_lead_status = bool(apply_lead_status)
    apply_follow_up = bool(apply_follow_up)
    apply_task = bool(apply_task)
    apply_needs_attention = bool(apply_needs_attention)

    with get_db() as conn:
        appt = conn.execute(
            "SELECT * FROM appointments WHERE id = ? AND user_id = ?",
            (appointment_id, user_id),
        ).fetchone()
        if not appt:
            return None, "Appointment not found."
        lead = conn.execute(
            "SELECT * FROM leads WHERE id = ? AND user_id = ?",
            (appt["lead_id"], user_id),
        ).fetchone()
    if not lead:
        return None, "Lead not found."

    suggestion = build_appointment_outcome_suggestion(
        outcome,
        current_lead_status=lead["status"],
        next_action_override=next_action,
    )
    if not suggestion:
        return None, "Invalid appointment outcome."

    appointment_status = suggestion["appointment_status"]
    if next_action == "" and suggestion.get("suggested_next_action"):
        next_action = suggestion["suggested_next_action"]

    same_outcome = (
        (appt.get("outcome") or "") == outcome
        and (appt.get("outcome_notes") or "") == outcome_notes
        and (appt.get("next_action") or "") == next_action
        and (appt.get("status") or "") == appointment_status
    )
    any_apply = apply_lead_status or apply_follow_up or apply_task or apply_needs_attention
    if same_outcome and not any_apply:
        return {
            "ok": True,
            "duplicate": True,
            "confirmation": "Outcome already saved. No changes.",
            "appointment_id": appointment_id,
            "applied": {},
        }, None

    with get_db() as conn:
        conn.execute(
            """
            UPDATE appointments
            SET outcome = ?, outcome_notes = ?, next_action = ?, status = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                outcome,
                outcome_notes,
                next_action,
                appointment_status,
                now,
                appointment_id,
                user_id,
            ),
        )

    applied = {
        "appointment_status": appointment_status,
        "lead_status": None,
        "follow_up_at": None,
        "task_id": None,
        "needs_attention": False,
        "next_action": next_action or None,
    }
    confirmation_bits = ["Outcome saved"]

    if not same_outcome:
        add_lead_activity(
            appt["lead_id"],
            user_id,
            "appointment_outcome",
            f"Appointment outcome saved: {outcome_label(outcome)}",
            {
                "appointment_id": appointment_id,
                "outcome": outcome,
                "appointment_status": appointment_status,
                "next_action": next_action,
                "outcome_notes": outcome_notes,
                "suggested_lead_status": suggestion.get("suggested_lead_status"),
            },
            actor_user_id=user_id,
        )

    if apply_lead_status and suggestion.get("suggested_lead_status"):
        previous = normalize_lead_status(lead["status"])
        updated_lead, err = set_lead_status(
            user_id,
            appt["lead_id"],
            suggestion["suggested_lead_status"],
            actor_user_id=user_id,
            from_automation=False,
        )
        if err:
            return None, err
        applied["lead_status"] = suggestion["suggested_lead_status"]
        if updated_lead and normalize_lead_status(updated_lead.get("status")) != previous:
            confirmation_bits.append(
                f"Lead status changed to {status_label(suggestion['suggested_lead_status'])}"
            )
        elif previous == suggestion["suggested_lead_status"]:
            confirmation_bits.append(
                f"Lead status already {status_label(suggestion['suggested_lead_status'])}"
            )

    if next_action:
        merge_lead_call_outcome_notes(
            appt["lead_id"],
            user_id,
            next_action=next_action,
        )

    if apply_follow_up and suggestion.get("suggested_follow_up_at"):
        follow_id, err = set_lead_follow_up(
            user_id,
            appt["lead_id"],
            suggestion["suggested_follow_up_at"],
            f"Follow-up after appointment outcome: {outcome_label(outcome)}",
            priority="high" if suggestion.get("needs_attention") else "normal",
            created_by=user_id,
        )
        if err:
            return None, err
        applied["follow_up_at"] = suggestion["suggested_follow_up_at"]
        applied["follow_up_id"] = follow_id
        due_day = (suggestion["suggested_follow_up_at"] or "")[:10]
        confirmation_bits.append(
            f"Follow-up scheduled for {due_day or suggestion.get('suggested_follow_up_label') or 'soon'}"
        )

    if apply_task and suggestion.get("suggested_task_title"):
        task_id, err = create_task(
            user_id,
            {
                "lead_id": appt["lead_id"],
                "title": suggestion["suggested_task_title"],
                "task_type": suggestion.get("suggested_task_type") or "general_follow_up",
                "priority": "high" if suggestion.get("needs_attention") else "normal",
                "due_at": suggestion.get("suggested_follow_up_at"),
            },
        )
        if err:
            return None, err
        applied["task_id"] = task_id
        confirmation_bits.append(f"Task created: {suggestion['suggested_task_title']}")

    if apply_needs_attention and suggestion.get("needs_attention"):
        reason_code = "appointment_no_show" if outcome == "no_show" else "review_call_outcome"
        upsert_needs_attention(
            user_id,
            appt["lead_id"],
            reason_code,
            priority="high",
            source_ref_type="appointment",
            source_ref_id=appointment_id,
            reason_text=(
                "Lead no-showed appointment — reschedule or confirm interest."
                if outcome == "no_show"
                else f"Appointment outcome {outcome_label(outcome)} needs agent follow-through."
            ),
        )
        applied["needs_attention"] = True
        confirmation_bits.append("Needs Attention item opened")

    resolve_needs_attention_by_reason(
        user_id, appt["lead_id"], "appointment_outcome_missing", "Outcome recorded"
    )

    confirmation = ". ".join(confirmation_bits) + "."
    return {
        "ok": True,
        "duplicate": False,
        "confirmation": confirmation,
        "appointment_id": appointment_id,
        "outcome": outcome,
        "appointment_status": appointment_status,
        "next_action": next_action,
        "applied": applied,
        "suggestion": suggestion,
    }, None


def list_appointments(user_id, lead_id=None, limit=50):
    with get_db() as conn:
        if lead_id:
            rows = conn.execute(
                """
                SELECT a.*, l.name AS lead_name
                FROM appointments a
                JOIN leads l ON l.id = a.lead_id
                WHERE a.user_id = ? AND a.lead_id = ?
                ORDER BY a.start_at DESC
                LIMIT ?
                """,
                (user_id, lead_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT a.*, l.name AS lead_name
                FROM appointments a
                JOIN leads l ON l.id = a.lead_id
                WHERE a.user_id = ?
                ORDER BY a.start_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def upsert_needs_attention(
    user_id,
    lead_id,
    reason_code,
    priority="normal",
    source_ref_type=None,
    source_ref_id=None,
    reason_text=None,
):
    reason_text = (reason_text or NEEDS_ATTENTION_REASONS.get(reason_code, reason_code) or reason_code)[:500]
    priority = priority if priority in PRIORITIES else "normal"
    now = _now()
    with get_db() as conn:
        existing = None
        if source_ref_type and source_ref_id is not None:
            existing = conn.execute(
                """
                SELECT id FROM needs_attention
                WHERE user_id = ? AND reason_code = ? AND status = 'open'
                  AND source_ref_type = ? AND source_ref_id = ?
                LIMIT 1
                """,
                (user_id, reason_code, source_ref_type, source_ref_id),
            ).fetchone()
        elif lead_id is not None:
            existing = conn.execute(
                """
                SELECT id FROM needs_attention
                WHERE user_id = ? AND lead_id = ? AND reason_code = ? AND status = 'open'
                LIMIT 1
                """,
                (user_id, lead_id, reason_code),
            ).fetchone()
        if existing:
            return existing["id"]
        cur = conn.execute(
            """
            INSERT INTO needs_attention
                (user_id, lead_id, reason_code, reason_text, priority, source_ref_type,
                 source_ref_id, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            (user_id, lead_id, reason_code, reason_text, priority, source_ref_type, source_ref_id, now),
        )
        item_id = cur.lastrowid
    create_notification(
        user_id,
        "needs_attention",
        "Needs attention",
        reason_text,
        link="/crm/needs-attention",
        lead_id=lead_id,
    )
    return item_id


def resolve_needs_attention(user_id, item_id, resolution_reason=""):
    with get_db() as conn:
        item = conn.execute(
            "SELECT * FROM needs_attention WHERE id = ? AND user_id = ?",
            (item_id, user_id),
        ).fetchone()
        if not item:
            return None, "Item not found."
        if item["reason_code"] in PROTECTED_RESOLVE_REASONS and not str(resolution_reason or "").strip():
            return None, "A resolution reason is required for this alert."
        now = _now()
        conn.execute(
            """
            UPDATE needs_attention
            SET status = 'resolved', resolution_reason = ?, resolved_at = ?, resolved_by = ?
            WHERE id = ? AND user_id = ?
            """,
            (str(resolution_reason or "")[:1000], now, user_id, item_id, user_id),
        )
    if item["lead_id"]:
        add_lead_activity(
            item["lead_id"],
            user_id,
            "needs_attention_resolved",
            f"Resolved: {item['reason_text']}",
            {"item_id": item_id, "reason_code": item["reason_code"], "resolution_reason": resolution_reason},
        )
    return True, None


def resolve_needs_attention_by_reason(user_id, lead_id, reason_code, resolution_reason="Resolved"):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id FROM needs_attention
            WHERE user_id = ? AND lead_id = ? AND reason_code = ? AND status = 'open'
            """,
            (user_id, lead_id, reason_code),
        ).fetchall()
    for row in rows:
        resolve_needs_attention(user_id, row["id"], resolution_reason)


def resolve_needs_attention_for_source(user_id, source_ref_type, source_ref_id, resolution_reason="Resolved"):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id FROM needs_attention
            WHERE user_id = ? AND source_ref_type = ? AND source_ref_id = ? AND status = 'open'
            """,
            (user_id, source_ref_type, source_ref_id),
        ).fetchall()
    for row in rows:
        resolve_needs_attention(user_id, row["id"], resolution_reason)


def list_needs_attention(user_id, limit=100, local_date=None):
    refresh_needs_attention(user_id, local_date=local_date)
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT n.*,
                   l.name AS lead_name,
                   l.phone_number,
                   l.status AS lead_status,
                   l.assigned_user_id,
                   l.next_action,
                   t.title AS task_title,
                   t.due_at AS task_due_at
            FROM needs_attention n
            LEFT JOIN leads l ON l.id = n.lead_id
            LEFT JOIN tasks t ON n.source_ref_type = 'task' AND t.id = n.source_ref_id
            WHERE n.user_id = ? AND n.status = 'open'
            ORDER BY
              CASE n.priority
                WHEN 'urgent' THEN 1 WHEN 'high' THEN 2 WHEN 'normal' THEN 3 ELSE 4 END,
              n.created_at ASC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def refresh_needs_attention(user_id, local_date=None):
    """Compute overdue/system queue items for this user."""
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    cutoff = (now_dt - timedelta(hours=FIRST_RESPONSE_HOURS)).isoformat()
    day = _calendar_day(local_date)
    with get_db() as conn:
        overdue_followups = [dict(r) for r in conn.execute(
            """
            SELECT id FROM leads
            WHERE user_id = ? AND next_follow_up_at IS NOT NULL AND next_follow_up_at < ?
              AND status != 'do_not_contact'
            """,
            (user_id, now),
        ).fetchall()]
        overdue_tasks = [dict(r) for r in conn.execute(
            """
            SELECT id, lead_id, title, due_at FROM tasks
            WHERE user_id = ? AND status IN ('open', 'in_progress')
              AND due_at IS NOT NULL
              AND substr(due_at, 1, 10) < ?
            """,
            (user_id, day),
        ).fetchall()]
        missing_outcomes = [dict(r) for r in conn.execute(
            """
            SELECT id, lead_id FROM appointments
            WHERE user_id = ? AND status IN ('scheduled', 'confirmed')
              AND outcome IS NULL AND end_at IS NOT NULL AND end_at < ?
            """,
            (user_id, now),
        ).fetchall()]
        stale = [dict(r) for r in conn.execute(
            """
            SELECT id FROM leads
            WHERE user_id = ? AND status = 'new'
              AND last_outbound_at IS NULL AND created_at < ?
            """,
            (user_id, cutoff),
        ).fetchall()]

    for lead in overdue_followups:
        upsert_needs_attention(user_id, lead["id"], "follow_up_overdue", priority="high")
    for task in overdue_tasks:
        due_label = (task.get("due_at") or "")[:10]
        title = (task.get("title") or "Task").strip()
        upsert_needs_attention(
            user_id,
            task.get("lead_id"),
            "task_overdue",
            priority="high",
            source_ref_type="task",
            source_ref_id=task["id"],
            reason_text=f"Task overdue: {title}" + (f" (due {due_label})" if due_label else ""),
        )
    for appt in missing_outcomes:
        upsert_needs_attention(
            user_id, appt["lead_id"], "appointment_outcome_missing", priority="high",
            source_ref_type="appointment", source_ref_id=appt["id"],
        )
    for lead in stale:
        upsert_needs_attention(user_id, lead["id"], "no_first_contact", priority="normal")


def create_notification(user_id, ntype, title, body="", link=None, lead_id=None):
    # Guard against f"/crm/leads/{None}" → "/crm/leads/None".
    if isinstance(link, str) and link.rstrip("/").endswith("/None"):
        link = "/crm/leads" if link.startswith("/crm/leads") else None
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO notifications (user_id, type, title, body, link, lead_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, ntype, title[:200], (body or "")[:1000], link, lead_id, _now()),
        )
        return cur.lastrowid


def list_notifications(user_id, unread_only=False, limit=50):
    with get_db() as conn:
        if unread_only:
            rows = conn.execute(
                """
                SELECT * FROM notifications
                WHERE user_id = ? AND read_at IS NULL
                ORDER BY created_at DESC LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM notifications
                WHERE user_id = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def mark_notification_read(user_id, notification_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE notifications SET read_at = ? WHERE id = ? AND user_id = ?",
            (_now(), notification_id, user_id),
        )


def apply_coach_queue_flags(user_id, lead_id, analysis, insight_id=None):
    """Create Needs Attention items from Claude analysis without applying changes."""
    if analysis.get("requires_manual_review") or analysis.get("sensitive_topic"):
        upsert_needs_attention(user_id, lead_id, "sensitive_topic", priority="urgent", source_ref_type="insight", source_ref_id=insight_id)
    confidence = float(analysis.get("confidence_score") or analysis.get("confidence") or 1)
    if confidence < CONFIDENCE_THRESHOLD:
        upsert_needs_attention(user_id, lead_id, "low_confidence", priority="normal", source_ref_type="insight", source_ref_id=insight_id)
    if analysis.get("draft_reply") or analysis.get("suggested_reply"):
        upsert_needs_attention(user_id, lead_id, "draft_awaiting_approval", priority="high", source_ref_type="insight", source_ref_id=insight_id)
    upsert_needs_attention(user_id, lead_id, "unreviewed_inbound", priority="high", source_ref_type="insight", source_ref_id=insight_id)
    intent = str(analysis.get("intent") or "").lower()
    if any(k in intent for k in ("buy", "sell", "ready", "list", "offer")):
        upsert_needs_attention(user_id, lead_id, "high_intent", priority="high", source_ref_type="insight", source_ref_id=insight_id)
    if analysis.get("appointment_requested"):
        upsert_needs_attention(user_id, lead_id, "appointment_requested", priority="high", source_ref_type="insight", source_ref_id=insight_id)
    for reason in analysis.get("needs_attention_reasons") or []:
        code = str(reason).strip().lower().replace(" ", "_")
        if code in NEEDS_ATTENTION_REASONS:
            upsert_needs_attention(user_id, lead_id, code, priority="high", source_ref_type="insight", source_ref_id=insight_id)


def get_pipeline_metrics(user_id, since_iso=None):
    refresh_needs_attention(user_id)
    now = _now()
    today = now[:10]
    week_end = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    with get_db() as conn:
        def count(sql, params):
            return conn.execute(sql, params).fetchone()["count"]

        active = count(
            "SELECT COUNT(*) AS count FROM leads WHERE user_id = ? AND status NOT IN ('closed_won','closed_lost','do_not_contact')",
            (user_id,),
        )
        new_leads = count("SELECT COUNT(*) AS count FROM leads WHERE user_id = ? AND status = 'new'", (user_id,))
        if since_iso:
            new_leads = count(
                "SELECT COUNT(*) AS count FROM leads WHERE user_id = ? AND created_at >= ?",
                (user_id, since_iso),
            )
        needs = count("SELECT COUNT(*) AS count FROM needs_attention WHERE user_id = ? AND status = 'open'", (user_id,))
        overdue_fu = count(
            "SELECT COUNT(*) AS count FROM leads WHERE user_id = ? AND next_follow_up_at IS NOT NULL AND next_follow_up_at < ?",
            (user_id, now),
        )
        tasks_today = count(
            """
            SELECT COUNT(*) AS count FROM tasks
            WHERE user_id = ? AND status IN ('open','in_progress') AND due_at IS NOT NULL
              AND substr(due_at,1,10) = ?
            """,
            (user_id, today),
        )
        appts_today = count(
            "SELECT COUNT(*) AS count FROM appointments WHERE user_id = ? AND substr(start_at,1,10) = ?",
            (user_id, today),
        )
        unreviewed = count(
            "SELECT COUNT(*) AS count FROM needs_attention WHERE user_id = ? AND status='open' AND reason_code='unreviewed_inbound'",
            (user_id,),
        )
        drafts = count(
            "SELECT COUNT(*) AS count FROM lead_insights WHERE user_id = ? AND status='pending'",
            (user_id,),
        )
        appts_week = count(
            "SELECT COUNT(*) AS count FROM appointments WHERE user_id = ? AND start_at >= ? AND start_at <= ?",
            (user_id, now, week_end),
        )
        outcomes_month = count(
            "SELECT COUNT(*) AS count FROM appointments WHERE user_id = ? AND outcome IS NOT NULL AND updated_at >= ?",
            (user_id, month_start),
        )
        sent = count("SELECT COUNT(*) AS count FROM sms_messages WHERE user_id = ? AND status IN ('sent','delivered','queued')", (user_id,))
        failed = count("SELECT COUNT(*) AS count FROM sms_messages WHERE user_id = ? AND status='failed'", (user_id,))
        status_rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM leads WHERE user_id = ? GROUP BY status",
            (user_id,),
        ).fetchall()

    by_status = {normalize_lead_status(r["status"]): r["count"] for r in status_rows}
    stages = []
    for stage_id, label, members in PIPELINE_STAGES:
        stages.append({
            "id": stage_id,
            "label": label,
            "count": sum(by_status.get(s, 0) for s in members),
        })
    delivery_total = sent + failed
    return {
        "active_leads": active,
        "new_leads": new_leads,
        "needs_attention": needs,
        "overdue_follow_ups": overdue_fu,
        "tasks_due_today": tasks_today,
        "appointments_today": appts_today,
        "unreviewed_inbound": unreviewed,
        "drafts_awaiting_approval": drafts,
        "appointments_this_week": appts_week,
        "outcomes_this_month": outcomes_month,
        "sms_delivery_success_rate": round((sent / delivery_total) * 100, 1) if delivery_total else 100.0,
        "leads_by_status": by_status,
        "pipeline_stages": stages,
        "average_first_response_hours": None,
    }


def filter_leads(user_id, status=None, source=None, limit=100):
    from crm_constants import normalize_lead_status as norm
    with get_db() as conn:
        sql = """
            SELECT l.*,
                   (SELECT COUNT(*) FROM sms_messages sm WHERE sm.lead_id = l.id) AS message_count
            FROM leads l
            WHERE l.user_id = ?
        """
        params = [user_id]
        if status:
            sql += " AND l.status = ?"
            params.append(norm(status))
        if source:
            sql += " AND l.source = ?"
            params.append(source)
        sql += " ORDER BY l.updated_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
