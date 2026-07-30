"""Phase 2 CRM persistence helpers. All reads/writes are user-scoped."""

from datetime import datetime, timedelta, timezone
import json
import logging
import re

from crm_constants import (
    APPOINTMENT_OUTCOMES,
    APPOINTMENT_STATUSES,
    APPOINTMENT_TYPES,
    CALENDAR_EVENT_TYPE_SET,
    CONFIDENCE_THRESHOLD,
    FIRST_RESPONSE_HOURS,
    FOLLOW_UP_CANCEL_REASON_SET,
    NEEDS_ATTENTION_REASONS,
    PIPELINE_STAGES,
    PRIORITIES,
    PROTECTED_RESOLVE_REASONS,
    TASK_COMPLETION_RANGES,
    TASK_OPEN_STATUSES,
    TASK_STATUSES,
    TASK_TYPES,
    build_appointment_outcome_suggestion,
    cancel_reason_label,
    normalize_lead_status,
    outcome_label,
    status_label,
)
from db import get_db, merge_lead_call_outcome_notes

logger = logging.getLogger(__name__)


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


def _consolidate_follow_up_activities(activities):
    """Keep one visible follow_up_scheduled row per follow_up_id (newest wins)."""
    seen = set()
    out = []
    for activity in activities:
        if activity.get("event_type") != "follow_up_scheduled":
            out.append(activity)
            continue
        payload = parse_activity_payload(activity)
        key = payload.get("follow_up_id")
        if key is None:
            # Legacy rows without follow_up_id: collapse identical summaries.
            key = ("summary", activity.get("summary") or "")
        else:
            key = ("id", int(key))
        if key in seen:
            continue
        seen.add(key)
        out.append(activity)
    return out


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
        items = _consolidate_follow_up_activities(items)
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


FOLLOW_UP_OPEN_STATUSES = ("pending",)
FOLLOW_UP_DONE_STATUSES = ("done", "cancelled")


def normalize_follow_up_reason(reason):
    text = re.sub(r"\s+", " ", str(reason or "").strip().lower())
    return text[:500] or "follow up"


def _normalize_due_at_key(due_at):
    """Collapse due timestamps to the minute for idempotent duplicate detection."""
    if not due_at:
        return ""
    text = str(due_at).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
        return dt.isoformat()
    except ValueError:
        return str(due_at)[:16]


def _parse_iso_dt(value):
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _local_date_for_due(due_at, tz_offset_minutes=None, local_date=None):
    """Return YYYY-MM-DD in the agent's local timezone when offset is provided."""
    dt = _parse_iso_dt(due_at)
    if dt is None:
        return str(due_at or "")[:10]
    if tz_offset_minutes is None:
        # Fall back to UTC calendar day (or caller-provided local_date context).
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    try:
        offset = int(tz_offset_minutes)
    except (TypeError, ValueError):
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    local_dt = dt.astimezone(timezone.utc) - timedelta(minutes=offset)
    return local_dt.strftime("%Y-%m-%d")


def _follow_up_select_sql():
    return """
        SELECT f.*,
               l.name AS lead_name,
               l.phone_number,
               l.status AS lead_status,
               l.source AS lead_source,
               u.email AS created_by_email,
               cu.email AS cancelled_by_email
        FROM lead_follow_ups f
        JOIN leads l ON l.id = f.lead_id
        LEFT JOIN users u ON u.id = f.created_by
        LEFT JOIN users cu ON cu.id = f.cancelled_by_user_id
    """


def _row_to_follow_up(row):
    if not row:
        return None
    item = dict(row)
    item["reason_normalized"] = normalize_follow_up_reason(item.get("reason"))
    item["is_open"] = item.get("status") in FOLLOW_UP_OPEN_STATUSES
    return item


def _sync_lead_next_follow_up(conn, user_id, lead_id):
    """Keep denormalized lead next-follow-up fields aligned with open rows."""
    now = _now()
    nxt = conn.execute(
        """
        SELECT due_at, reason, priority, created_by
        FROM lead_follow_ups
        WHERE user_id = ? AND lead_id = ? AND status = 'pending'
        ORDER BY due_at ASC, id ASC
        LIMIT 1
        """,
        (user_id, lead_id),
    ).fetchone()
    if nxt:
        conn.execute(
            """
            UPDATE leads
            SET next_follow_up_at = ?, follow_up_reason = ?, follow_up_priority = ?,
                follow_up_completed_at = NULL, follow_up_created_by = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                nxt["due_at"],
                nxt["reason"],
                nxt["priority"] or "normal",
                nxt["created_by"] or user_id,
                now,
                lead_id,
                user_id,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE leads
            SET next_follow_up_at = NULL, follow_up_reason = NULL, follow_up_priority = NULL,
                updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (now, lead_id, user_id),
        )


def _activity_summary_for_schedule(due_at, reason, local_due_label=None):
    reason_text = str(reason or "Follow up").strip() or "Follow up"
    label = str(local_due_label or "").strip()
    if not label:
        dt = _parse_iso_dt(due_at)
        if dt:
            label = dt.astimezone(timezone.utc).strftime("%B %d, %Y at %H:%M UTC")
        else:
            label = str(due_at or "")[:16]
    return f"Follow-up scheduled for {label} — {reason_text}."


def set_lead_follow_up(
    user_id,
    lead_id,
    due_at,
    reason,
    priority="normal",
    created_by=None,
    *,
    replace_existing=True,
    force_create=False,
    local_due_label=None,
):
    """Create or update a follow-up. Source of truth: lead_follow_ups.

    Dedupes open follow-ups for the same tenant/lead/normalized reason/due minute.
    Quick actions default to replace_existing=True so repeated clicks reschedule
    instead of inserting duplicates.
    """
    priority = priority if priority in PRIORITIES else "normal"
    reason = str(reason or "Follow up").strip()[:500] or "Follow up"
    reason_key = normalize_follow_up_reason(reason)
    due_key = _normalize_due_at_key(due_at)
    actor = created_by or user_id
    now = _now()

    if not due_at:
        return None, "due_at is required."

    with get_db() as conn:
        lead = conn.execute(
            "SELECT id FROM leads WHERE id = ? AND user_id = ?",
            (lead_id, user_id),
        ).fetchone()
        if not lead:
            return None, "Lead not found."

        open_rows = conn.execute(
            """
            SELECT * FROM lead_follow_ups
            WHERE user_id = ? AND lead_id = ? AND status = 'pending'
            ORDER BY due_at ASC, id ASC
            """,
            (user_id, lead_id),
        ).fetchall()
        open_rows = [dict(r) for r in open_rows]

        exact = next(
            (
                r
                for r in open_rows
                if normalize_follow_up_reason(r.get("reason")) == reason_key
                and _normalize_due_at_key(r.get("due_at")) == due_key
            ),
            None,
        )
        if exact and not force_create:
            _sync_lead_next_follow_up(conn, user_id, lead_id)
            return {
                "follow_up_id": exact["id"],
                "due_at": exact["due_at"],
                "reason": exact.get("reason") or reason,
                "priority": exact.get("priority") or priority,
                "created": False,
                "updated": False,
                "duplicate": True,
                "confirmation": _activity_summary_for_schedule(
                    exact["due_at"], exact.get("reason") or reason, local_due_label
                ).rstrip(".")
                + " (already scheduled).",
            }, None

        same_reason = next(
            (
                r
                for r in open_rows
                if normalize_follow_up_reason(r.get("reason")) == reason_key
            ),
            None,
        )
        if same_reason and not force_create:
            if not replace_existing:
                return {
                    "conflict": True,
                    "existing_follow_up_id": same_reason["id"],
                    "existing_due_at": same_reason["due_at"],
                    "existing_reason": same_reason.get("reason") or reason,
                    "message": (
                        "An open follow-up with this reason already exists. "
                        "Replace it or create another?"
                    ),
                }, "conflict"

            conn.execute(
                """
                UPDATE lead_follow_ups
                SET due_at = ?, reason = ?, priority = ?
                WHERE id = ? AND user_id = ?
                """,
                (due_at, reason, priority, same_reason["id"], user_id),
            )
            follow_up_id = same_reason["id"]
            _sync_lead_next_follow_up(conn, user_id, lead_id)
            summary = _activity_summary_for_schedule(due_at, reason, local_due_label)
            # One timeline entry for reschedule — avoid spam on repeated clicks.
            existing_activity = conn.execute(
                """
                SELECT id, payload_json FROM lead_activities
                WHERE user_id = ? AND lead_id = ? AND event_type = 'follow_up_scheduled'
                ORDER BY id DESC LIMIT 20
                """,
                (user_id, lead_id),
            ).fetchall()
            updated_activity = False
            for act in existing_activity:
                try:
                    payload = json.loads(act["payload_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    payload = {}
                if int(payload.get("follow_up_id") or 0) == int(follow_up_id):
                    conn.execute(
                        """
                        UPDATE lead_activities
                        SET summary = ?, payload_json = ?, created_at = ?
                        WHERE id = ? AND user_id = ?
                        """,
                        (
                            summary,
                            json.dumps(
                                {
                                    "due_at": due_at,
                                    "priority": priority,
                                    "follow_up_id": follow_up_id,
                                    "reason": reason,
                                    "rescheduled": True,
                                }
                            )[:4000],
                            now,
                            act["id"],
                            user_id,
                        ),
                    )
                    updated_activity = True
                    break
            if not updated_activity:
                _insert_activity(
                    conn,
                    lead_id,
                    user_id,
                    "follow_up_scheduled",
                    summary,
                    {
                        "due_at": due_at,
                        "priority": priority,
                        "follow_up_id": follow_up_id,
                        "reason": reason,
                        "rescheduled": True,
                    },
                    actor_user_id=actor,
                )
            return {
                "follow_up_id": follow_up_id,
                "due_at": due_at,
                "reason": reason,
                "priority": priority,
                "created": False,
                "updated": True,
                "duplicate": False,
                "confirmation": summary.rstrip("."),
            }, None

        cur = conn.execute(
            """
            INSERT INTO lead_follow_ups
                (lead_id, user_id, due_at, reason, status, created_at, priority, created_by)
            VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (lead_id, user_id, due_at, reason, now, priority, actor),
        )
        follow_up_id = cur.lastrowid
        _sync_lead_next_follow_up(conn, user_id, lead_id)
        summary = _activity_summary_for_schedule(due_at, reason, local_due_label)
        _insert_activity(
            conn,
            lead_id,
            user_id,
            "follow_up_scheduled",
            summary,
            {
                "due_at": due_at,
                "priority": priority,
                "follow_up_id": follow_up_id,
                "reason": reason,
            },
            actor_user_id=actor,
        )
        return {
            "follow_up_id": follow_up_id,
            "due_at": due_at,
            "reason": reason,
            "priority": priority,
            "created": True,
            "updated": False,
            "duplicate": False,
            "confirmation": summary.rstrip("."),
        }, None


def get_follow_up(user_id, follow_up_id):
    with get_db() as conn:
        row = conn.execute(
            _follow_up_select_sql() + " WHERE f.id = ? AND f.user_id = ?",
            (follow_up_id, user_id),
        ).fetchone()
        return _row_to_follow_up(row)


def list_lead_follow_ups(user_id, lead_id, include_completed=True, limit=100):
    with get_db() as conn:
        lead = conn.execute(
            "SELECT id FROM leads WHERE id = ? AND user_id = ?",
            (lead_id, user_id),
        ).fetchone()
        if not lead:
            return []
        if include_completed:
            rows = conn.execute(
                _follow_up_select_sql()
                + """
                WHERE f.user_id = ? AND f.lead_id = ?
                ORDER BY
                  CASE WHEN f.status = 'pending' THEN 0 ELSE 1 END,
                  f.due_at ASC, f.id ASC
                LIMIT ?
                """,
                (user_id, lead_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                _follow_up_select_sql()
                + """
                WHERE f.user_id = ? AND f.lead_id = ? AND f.status = 'pending'
                ORDER BY f.due_at ASC, f.id ASC
                LIMIT ?
                """,
                (user_id, lead_id, limit),
            ).fetchall()
        return [_row_to_follow_up(r) for r in rows]


def group_follow_ups_for_lead(follow_ups, local_date=None, tz_offset_minutes=None):
    day = _calendar_day(local_date)
    groups = {
        "overdue": [],
        "today": [],
        "upcoming": [],
        "completed": [],
        "cancelled": [],
    }
    for item in follow_ups:
        status = item.get("status")
        if status == "cancelled":
            groups["cancelled"].append(item)
            continue
        if status != "pending":
            groups["completed"].append(item)
            continue
        due_day = _local_date_for_due(
            item.get("due_at"), tz_offset_minutes=tz_offset_minutes, local_date=day
        )
        if due_day < day:
            groups["overdue"].append(item)
        elif due_day == day:
            groups["today"].append(item)
        else:
            groups["upcoming"].append(item)
    return groups


def list_follow_ups(
    user_id,
    bucket="all",
    limit=200,
    local_date=None,
    tz_offset_minutes=None,
    start_at=None,
    end_at=None,
):
    """List follow-ups for calendar / agenda views. Always scoped to user_id."""
    day = _calendar_day(local_date)
    with get_db() as conn:
        rows = conn.execute(
            _follow_up_select_sql()
            + """
            WHERE f.user_id = ?
            ORDER BY f.due_at ASC, f.id ASC
            LIMIT ?
            """,
            (user_id, max(int(limit), 1)),
        ).fetchall()
    items = [_row_to_follow_up(r) for r in rows]

    if start_at or end_at:
        filtered = []
        for item in items:
            due = str(item.get("due_at") or "")
            if start_at and due < str(start_at):
                continue
            if end_at and due > str(end_at):
                continue
            filtered.append(item)
        items = filtered

    if bucket in (None, "", "all", "agenda"):
        return items
    if bucket == "completed":
        return [i for i in items if i.get("status") != "pending"]
    if bucket == "overdue":
        return [
            i
            for i in items
            if i.get("status") == "pending"
            and _local_date_for_due(i.get("due_at"), tz_offset_minutes, day) < day
        ]
    if bucket == "today":
        return [
            i
            for i in items
            if i.get("status") == "pending"
            and _local_date_for_due(i.get("due_at"), tz_offset_minutes, day) == day
        ]
    if bucket == "upcoming":
        return [
            i
            for i in items
            if i.get("status") == "pending"
            and _local_date_for_due(i.get("due_at"), tz_offset_minutes, day) > day
        ]
    if bucket == "week":
        start = datetime.strptime(day, "%Y-%m-%d").date()
        end = start + timedelta(days=7)
        out = []
        for item in items:
            if item.get("status") != "pending":
                continue
            due_day = _local_date_for_due(item.get("due_at"), tz_offset_minutes, day)
            try:
                d = datetime.strptime(due_day, "%Y-%m-%d").date()
            except ValueError:
                continue
            if start <= d < end:
                out.append(item)
        return out
    return items


def follow_up_dashboard_counts(user_id, local_date=None, tz_offset_minutes=None):
    """Counts for dashboard cards — must match list_follow_ups_for_dashboard_range."""
    return {
        "follow_ups_due_today": len(
            list_follow_ups_for_dashboard_range(
                user_id,
                "today",
                local_date=local_date,
                tz_offset_minutes=tz_offset_minutes,
            )
        ),
        "follow_ups_overdue": len(
            list_follow_ups_for_dashboard_range(
                user_id,
                "overdue",
                local_date=local_date,
                tz_offset_minutes=tz_offset_minutes,
            )
        ),
        "follow_ups_due_this_week": len(
            list_follow_ups_for_dashboard_range(
                user_id,
                "this_week",
                local_date=local_date,
                tz_offset_minutes=tz_offset_minutes,
            )
        ),
    }


def update_follow_up(user_id, follow_up_id, *, due_at=None, reason=None, priority=None, local_due_label=None):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM lead_follow_ups WHERE id = ? AND user_id = ?",
            (follow_up_id, user_id),
        ).fetchone()
        if not row:
            return None, "Follow-up not found."
        item = dict(row)
        new_due = due_at if due_at is not None else item["due_at"]
        new_reason = (
            str(reason).strip()[:500]
            if reason is not None
            else (item.get("reason") or "Follow up")
        ) or "Follow up"
        new_priority = (
            priority
            if priority in PRIORITIES
            else (item.get("priority") if item.get("priority") in PRIORITIES else "normal")
        )
        conn.execute(
            """
            UPDATE lead_follow_ups
            SET due_at = ?, reason = ?, priority = ?
            WHERE id = ? AND user_id = ?
            """,
            (new_due, new_reason, new_priority, follow_up_id, user_id),
        )
        _sync_lead_next_follow_up(conn, user_id, item["lead_id"])
        if item.get("status") == "pending":
            summary = _activity_summary_for_schedule(new_due, new_reason, local_due_label)
            # Update newest matching activity instead of appending duplicates.
            acts = conn.execute(
                """
                SELECT id, payload_json FROM lead_activities
                WHERE user_id = ? AND lead_id = ? AND event_type = 'follow_up_scheduled'
                ORDER BY id DESC LIMIT 30
                """,
                (user_id, item["lead_id"]),
            ).fetchall()
            touched = False
            for act in acts:
                try:
                    payload = json.loads(act["payload_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    payload = {}
                if int(payload.get("follow_up_id") or 0) == int(follow_up_id):
                    conn.execute(
                        """
                        UPDATE lead_activities
                        SET summary = ?, payload_json = ?
                        WHERE id = ? AND user_id = ?
                        """,
                        (
                            summary,
                            json.dumps(
                                {
                                    "due_at": new_due,
                                    "priority": new_priority,
                                    "follow_up_id": follow_up_id,
                                    "reason": new_reason,
                                    "rescheduled": True,
                                }
                            )[:4000],
                            act["id"],
                            user_id,
                        ),
                    )
                    touched = True
                    break
            if not touched:
                _insert_activity(
                    conn,
                    item["lead_id"],
                    user_id,
                    "follow_up_scheduled",
                    summary,
                    {
                        "due_at": new_due,
                        "priority": new_priority,
                        "follow_up_id": follow_up_id,
                        "reason": new_reason,
                        "rescheduled": True,
                    },
                    actor_user_id=user_id,
                )
    return get_follow_up(user_id, follow_up_id), None


def complete_follow_up(user_id, follow_up_id):
    now = _now()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM lead_follow_ups WHERE id = ? AND user_id = ?",
            (follow_up_id, user_id),
        ).fetchone()
        if not row:
            return False, "Follow-up not found."
        item = dict(row)
        if item.get("status") != "pending":
            return True, None
        conn.execute(
            """
            UPDATE lead_follow_ups
            SET status = 'done', completed_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (now, follow_up_id, user_id),
        )
        remaining = conn.execute(
            """
            SELECT COUNT(*) AS count FROM lead_follow_ups
            WHERE user_id = ? AND lead_id = ? AND status = 'pending'
            """,
            (user_id, item["lead_id"]),
        ).fetchone()["count"]
        if remaining == 0:
            conn.execute(
                """
                UPDATE leads
                SET follow_up_completed_at = ?, next_follow_up_at = NULL, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (now, now, item["lead_id"], user_id),
            )
        else:
            _sync_lead_next_follow_up(conn, user_id, item["lead_id"])
        _insert_activity(
            conn,
            item["lead_id"],
            user_id,
            "follow_up_completed",
            f"Follow-up completed — {item.get('reason') or 'Follow up'}",
            {
                "follow_up_id": follow_up_id,
                "original_due_at": item.get("due_at"),
                "reason": item.get("reason"),
                "priority": item.get("priority"),
            },
            actor_user_id=user_id,
        )
    return True, None


def cancel_follow_up(
    user_id,
    follow_up_id,
    *,
    cancel_reason_code,
    cancel_reason_notes="",
    cancelled_by_user_id=None,
):
    """Cancel a follow-up with a required reason. Never deletes the row."""
    code = str(cancel_reason_code or "").strip()
    notes = str(cancel_reason_notes or "").strip()[:1000]
    if code not in FOLLOW_UP_CANCEL_REASON_SET:
        return None, "A valid cancellation reason is required."
    if code == "other" and not notes:
        return None, "Please explain why this follow-up is being cancelled."

    actor = cancelled_by_user_id or user_id
    now = _now()
    label = cancel_reason_label(code)

    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM lead_follow_ups WHERE id = ? AND user_id = ?",
            (follow_up_id, user_id),
        ).fetchone()
        if not row:
            return None, "Follow-up not found."
        item = dict(row)

        # Idempotent: already cancelled with same code → success, no new activity.
        if item.get("status") == "cancelled":
            if (item.get("cancel_reason_code") or "") == code:
                return {
                    "ok": True,
                    "duplicate": True,
                    "follow_up_id": follow_up_id,
                    "status": "cancelled",
                    "cancel_reason_code": code,
                    "offer_dnc": code == "lead_requested_no_further_contact",
                }, None
            return None, "Follow-up is already cancelled."

        if item.get("status") != "pending":
            return None, "Only open follow-ups can be cancelled."

        conn.execute(
            """
            UPDATE lead_follow_ups
            SET status = 'cancelled',
                completed_at = ?,
                cancelled_at = ?,
                cancelled_by_user_id = ?,
                cancel_reason_code = ?,
                cancel_reason_notes = ?
            WHERE id = ? AND user_id = ? AND status = 'pending'
            """,
            (now, now, actor, code, notes or None, follow_up_id, user_id),
        )
        _sync_lead_next_follow_up(conn, user_id, item["lead_id"])

        summary = f"Follow-up cancelled: {label}"
        payload = {
            "follow_up_id": follow_up_id,
            "cancel_reason_code": code,
            "cancel_reason_notes": notes,
            "original_due_at": item.get("due_at"),
            "follow_up_reason": item.get("reason"),
        }
        # Avoid duplicate cancel activities on retry races.
        existing = conn.execute(
            """
            SELECT id, payload_json FROM lead_activities
            WHERE user_id = ? AND lead_id = ? AND event_type = 'follow_up_cancelled'
            ORDER BY id DESC LIMIT 20
            """,
            (user_id, item["lead_id"]),
        ).fetchall()
        already = False
        for act in existing:
            try:
                p = json.loads(act["payload_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                p = {}
            if int(p.get("follow_up_id") or 0) == int(follow_up_id):
                already = True
                break
        if not already:
            note_bit = f" — {notes}" if notes else ""
            due_bit = ""
            if item.get("due_at"):
                due_bit = f" (was due {str(item['due_at'])[:16]} UTC)"
            _insert_activity(
                conn,
                item["lead_id"],
                user_id,
                "follow_up_cancelled",
                f"{summary}{due_bit}{note_bit}",
                payload,
                actor_user_id=actor,
            )

    return {
        "ok": True,
        "duplicate": False,
        "follow_up_id": follow_up_id,
        "status": "cancelled",
        "cancel_reason_code": code,
        "cancel_reason_label": label,
        "offer_dnc": code == "lead_requested_no_further_contact",
    }, None


def dismiss_follow_up(user_id, follow_up_id, reason="Dismissed"):
    """Backward-compatible wrapper — maps free-text dismiss to cancel other/notes."""
    notes = str(reason or "").strip()
    code = "other"
    if normalize_follow_up_reason(notes) == "duplicate follow-up":
        code = "duplicate_follow_up"
        notes = notes or "Duplicate follow-up"
    result, error = cancel_follow_up(
        user_id,
        follow_up_id,
        cancel_reason_code=code,
        cancel_reason_notes=notes or "Dismissed",
        cancelled_by_user_id=user_id,
    )
    if error:
        return False, error
    return True, None


def complete_lead_follow_up(user_id, lead_id, follow_up_id=None):
    """Complete one follow-up (by id) or the next open follow-up for the lead."""
    if follow_up_id:
        return complete_follow_up(user_id, follow_up_id)
    open_items = list_lead_follow_ups(user_id, lead_id, include_completed=False, limit=1)
    if not open_items:
        lead = None
        with get_db() as conn:
            lead = conn.execute(
                "SELECT id FROM leads WHERE id = ? AND user_id = ?",
                (lead_id, user_id),
            ).fetchone()
        if not lead:
            return False, "Lead not found."
        return True, None
    return complete_follow_up(user_id, open_items[0]["id"])


def dismiss_lead_follow_up(user_id, lead_id, reason="Dismissed", follow_up_id=None):
    if follow_up_id:
        return dismiss_follow_up(user_id, follow_up_id, reason=reason)
    open_items = list_lead_follow_ups(user_id, lead_id, include_completed=False, limit=50)
    if not open_items:
        return True
    for item in open_items:
        dismiss_follow_up(user_id, item["id"], reason=reason)
    return True


def find_duplicate_open_follow_ups(user_id, *, dry_run=True):
    """Find pending follow-ups that share tenant/lead/reason/due-minute.

    Keeps the oldest row in each group; cancels the rest when dry_run=False.
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM lead_follow_ups
            WHERE user_id = ? AND status = 'pending'
            ORDER BY lead_id ASC, id ASC
            """,
            (user_id,),
        ).fetchall()
    groups = {}
    for row in rows:
        item = dict(row)
        key = (
            int(item["lead_id"]),
            normalize_follow_up_reason(item.get("reason")),
            _normalize_due_at_key(item.get("due_at")),
        )
        groups.setdefault(key, []).append(item)

    report = []
    cancel_ids = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        keep = members[0]
        dupes = members[1:]
        report.append(
            {
                "lead_id": key[0],
                "reason": keep.get("reason"),
                "due_at": keep.get("due_at"),
                "keep_id": keep["id"],
                "duplicate_ids": [d["id"] for d in dupes],
                "duplicate_count": len(dupes),
            }
        )
        cancel_ids.extend(d["id"] for d in dupes)

    cancelled = []
    if not dry_run and cancel_ids:
        for fid in cancel_ids:
            result, error = cancel_follow_up(
                user_id,
                fid,
                cancel_reason_code="duplicate_follow_up",
                cancel_reason_notes=(
                    "System cleanup: duplicate open follow-up with the same "
                    "lead, reason, and due time."
                ),
                cancelled_by_user_id=user_id,
            )
            if not error:
                cancelled.append(result["follow_up_id"])

    return {
        "dry_run": dry_run,
        "groups": report,
        "duplicate_count": sum(g["duplicate_count"] for g in report),
        "cancelled_ids": cancelled,
    }


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
            SELECT t.*, l.name AS lead_name, l.phone_number,
                   cu.email AS completed_by_email
            FROM tasks t
            LEFT JOIN leads l ON l.id = t.lead_id
            LEFT JOIN users cu ON cu.id = t.completed_by
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


def complete_task(user_id, task_id, actor_user_id=None):
    now = _now()
    actor = actor_user_id or user_id
    with get_db() as conn:
        task = conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        ).fetchone()
        if not task:
            return None, "Task not found."
        # Idempotent: repeated clicks must not overwrite the original completion
        # metadata or emit duplicate activity entries.
        if task["status"] == "completed":
            return get_task(user_id, task_id), None
        conn.execute(
            """
            UPDATE tasks
            SET status = 'completed', completed_at = ?, completed_by = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (now, actor, now, task_id, user_id),
        )
    if task["lead_id"]:
        add_lead_activity(
            task["lead_id"],
            user_id,
            "task_completed",
            f"Task completed: {task['title']}",
            {"task_id": task_id, "completed_at": now, "completed_by": actor},
            actor_user_id=actor,
        )
    resolve_needs_attention_for_source(user_id, "task", task_id, "Task completed")
    return get_task(user_id, task_id), None


def reopen_task(user_id, task_id, actor_user_id=None):
    """Return a completed task to the open queue while preserving audit history.

    Clears completed_at/completed_by (so the task is genuinely active again) but
    records a task_reopened activity capturing the prior completion for an
    auditable history. Idempotent: reopening a task that is not completed is a
    no-op and emits no duplicate activity.
    """
    now = _now()
    actor = actor_user_id or user_id
    with get_db() as conn:
        task = conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        ).fetchone()
        if not task:
            return None, "Task not found."
        if task["status"] != "completed":
            return get_task(user_id, task_id), None
        prev_completed_at = task["completed_at"]
        prev_completed_by = task["completed_by"] if "completed_by" in task.keys() else None
        conn.execute(
            """
            UPDATE tasks
            SET status = 'open', completed_at = NULL, completed_by = NULL, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (now, task_id, user_id),
        )
    if task["lead_id"]:
        add_lead_activity(
            task["lead_id"],
            user_id,
            "task_reopened",
            f"Task reopened: {task['title']}",
            {
                "task_id": task_id,
                "reopened_at": now,
                "reopened_by": actor,
                "previous_completed_at": prev_completed_at,
                "previous_completed_by": prev_completed_by,
            },
            actor_user_id=actor,
        )
    return get_task(user_id, task_id), None


def _completion_date_bounds(range_key=None, local_date=None, start_date=None, end_date=None):
    """Return (start_day, end_day) inclusive YYYY-MM-DD bounds for a completion range.

    Any missing/invalid bound is returned as None (no constraint on that side).
    All values are validated to the YYYY-MM-DD shape so they are never used to
    build raw/unsafe SQL — they are always passed as bound parameters.
    """
    key = (range_key or "all").strip().lower()
    if key not in TASK_COMPLETION_RANGES:
        key = "all"
    day = _calendar_day(local_date)
    today = datetime.strptime(day, "%Y-%m-%d").date()

    def _valid(value):
        value = str(value or "").strip()[:10]
        return value if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else None

    if key == "today":
        return day, day
    if key == "last_7_days":
        return (today - timedelta(days=6)).strftime("%Y-%m-%d"), day
    if key == "last_30_days":
        return (today - timedelta(days=29)).strftime("%Y-%m-%d"), day
    if key == "custom":
        return _valid(start_date), _valid(end_date)
    return None, None


def list_completed_tasks(
    user_id,
    completion_range="all",
    local_date=None,
    start_date=None,
    end_date=None,
    lead_id=None,
    owner_id=None,
    priority=None,
    task_type=None,
    limit=500,
):
    """List completed tasks for a user, newest completion first, with safe filters.

    All filters are applied via bound parameters and validated against known
    allow-lists; URL values are never interpolated into SQL.
    """
    start_day, end_day = _completion_date_bounds(
        completion_range, local_date=local_date, start_date=start_date, end_date=end_date
    )
    clauses = ["t.user_id = ?", "t.status = 'completed'"]
    params = [user_id]
    if start_day:
        clauses.append("substr(t.completed_at, 1, 10) >= ?")
        params.append(start_day)
    if end_day:
        clauses.append("substr(t.completed_at, 1, 10) <= ?")
        params.append(end_day)
    if lead_id not in (None, "", 0, "0"):
        try:
            params.append(int(lead_id))
            clauses.append("t.lead_id = ?")
        except (TypeError, ValueError):
            pass
    if owner_id not in (None, "", 0, "0"):
        try:
            params.append(int(owner_id))
            clauses.append("t.assigned_user_id = ?")
        except (TypeError, ValueError):
            pass
    if priority in PRIORITIES:
        clauses.append("t.priority = ?")
        params.append(priority)
    if task_type in TASK_TYPES:
        clauses.append("t.task_type = ?")
        params.append(task_type)
    try:
        limit = max(1, min(int(limit), 2000))
    except (TypeError, ValueError):
        limit = 500
    params.append(limit)
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT t.*, l.name AS lead_name, l.phone_number,
                   cu.email AS completed_by_email
            FROM tasks t
            LEFT JOIN leads l ON l.id = t.lead_id
            LEFT JOIN users cu ON cu.id = t.completed_by
            WHERE {' AND '.join(clauses)}
            ORDER BY COALESCE(t.completed_at, t.updated_at) DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]


def count_tasks_completed(user_id, range_key="today", local_date=None):
    """Count completed tasks whose completion date falls in the given range."""
    start_day, end_day = _completion_date_bounds(range_key, local_date=local_date)
    clauses = ["user_id = ?", "status = 'completed'"]
    params = [user_id]
    if start_day:
        clauses.append("substr(completed_at, 1, 10) >= ?")
        params.append(start_day)
    if end_day:
        clauses.append("substr(completed_at, 1, 10) <= ?")
        params.append(end_day)
    with get_db() as conn:
        return conn.execute(
            f"SELECT COUNT(*) AS count FROM tasks WHERE {' AND '.join(clauses)}",
            tuple(params),
        ).fetchone()["count"]


def list_completed_task_lead_options(user_id, limit=200):
    """Distinct leads that have completed tasks, for the completion filter dropdown."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT t.lead_id, l.name AS lead_name
            FROM tasks t
            JOIN leads l ON l.id = t.lead_id
            WHERE t.user_id = ? AND t.status = 'completed' AND t.lead_id IS NOT NULL
            ORDER BY l.name ASC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def list_completed_task_owner_options(user_id, limit=50):
    """Distinct owners (assigned users) that have completed tasks, for filtering."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT t.assigned_user_id AS owner_id, u.email AS owner_email
            FROM tasks t
            LEFT JOIN users u ON u.id = t.assigned_user_id
            WHERE t.user_id = ? AND t.status = 'completed'
              AND t.assigned_user_id IS NOT NULL
            ORDER BY u.email ASC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


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


def _insert_activity(conn, lead_id, user_id, event_type, summary, payload, actor_user_id=None):
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
    """Save appointment outcome and approved side-effects in one DB transaction."""
    logger.info(
        "appointment_outcome_start appointment_id=%s outcome_present=%s "
        "apply_status=%s apply_follow_up=%s apply_task=%s apply_needs_attention=%s",
        appointment_id,
        bool(outcome),
        bool(apply_lead_status),
        bool(apply_follow_up),
        bool(apply_task),
        bool(apply_needs_attention),
    )
    if outcome not in APPOINTMENT_OUTCOMES:
        logger.info("appointment_outcome_invalid appointment_id=%s", appointment_id)
        return None, "Invalid appointment outcome."

    now = _now()
    outcome_notes = str(outcome_notes or "")[:2000]
    next_action = str(next_action or "")[:500]
    apply_lead_status = bool(apply_lead_status)
    apply_follow_up = bool(apply_follow_up)
    apply_task = bool(apply_task)
    apply_needs_attention = bool(apply_needs_attention)

    try:
        with get_db() as conn:
            appt = conn.execute(
                "SELECT * FROM appointments WHERE id = ? AND user_id = ?",
                (appointment_id, user_id),
            ).fetchone()
            if not appt:
                logger.info(
                    "appointment_outcome_not_found appointment_id=%s", appointment_id
                )
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
            any_apply = (
                apply_lead_status or apply_follow_up or apply_task or apply_needs_attention
            )
            if same_outcome and not any_apply:
                logger.info(
                    "appointment_outcome_duplicate appointment_id=%s", appointment_id
                )
                return {
                    "ok": True,
                    "duplicate": True,
                    "confirmation": "Outcome already saved. No changes.",
                    "appointment_id": appointment_id,
                    "lead_id": appt["lead_id"],
                    "applied": {},
                }, None

            # a/b) Save outcome + mark appointment status.
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
                _insert_activity(
                    conn,
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

            previous_status = normalize_lead_status(lead["status"])
            if apply_lead_status and suggestion.get("suggested_lead_status"):
                new_status = normalize_lead_status(suggestion["suggested_lead_status"])
                if previous_status != new_status:
                    if previous_status == "do_not_contact" and new_status != "do_not_contact":
                        # Still allow agent-approved manual change.
                        pass
                    conn.execute(
                        "UPDATE leads SET status = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                        (new_status, now, appt["lead_id"], user_id),
                    )
                    _insert_activity(
                        conn,
                        appt["lead_id"],
                        user_id,
                        "status_change",
                        (
                            f"Lead status changed from {status_label(previous_status)} "
                            f"to {status_label(new_status)}"
                        ),
                        {
                            "previous_status": previous_status,
                            "new_status": new_status,
                            "source": "appointment_outcome",
                            "appointment_id": appointment_id,
                        },
                        actor_user_id=user_id,
                    )
                    confirmation_bits.append(
                        f"Lead status changed to {status_label(new_status)}"
                    )
                else:
                    confirmation_bits.append(
                        f"Lead status already {status_label(new_status)}"
                    )
                applied["lead_status"] = suggestion["suggested_lead_status"]

            if next_action:
                conn.execute(
                    """
                    UPDATE leads
                    SET next_action = COALESCE(?, next_action), updated_at = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (next_action, now, appt["lead_id"], user_id),
                )

            if apply_follow_up and suggestion.get("suggested_follow_up_at"):
                due_at = suggestion["suggested_follow_up_at"]
                reason = (
                    f"Follow-up after appointment outcome: {outcome_label(outcome)} "
                    f"[appointment:{appointment_id}]"
                )
                existing_fu = conn.execute(
                    """
                    SELECT id FROM lead_follow_ups
                    WHERE user_id = ? AND lead_id = ? AND status = 'pending'
                      AND reason LIKE ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (user_id, appt["lead_id"], f"%[appointment:{appointment_id}]%"),
                ).fetchone()
                priority = "high" if suggestion.get("needs_attention") else "normal"
                if existing_fu:
                    conn.execute(
                        """
                        UPDATE lead_follow_ups
                        SET due_at = ?, reason = ?, priority = ?
                        WHERE id = ? AND user_id = ?
                        """,
                        (due_at, reason, priority, existing_fu["id"], user_id),
                    )
                    follow_id = existing_fu["id"]
                else:
                    cur = conn.execute(
                        """
                        INSERT INTO lead_follow_ups
                            (lead_id, user_id, due_at, reason, status, created_at, priority, created_by)
                        VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
                        """,
                        (
                            appt["lead_id"],
                            user_id,
                            due_at,
                            reason,
                            now,
                            priority,
                            user_id,
                        ),
                    )
                    follow_id = cur.lastrowid
                    _insert_activity(
                        conn,
                        appt["lead_id"],
                        user_id,
                        "follow_up_scheduled",
                        f"Follow-up scheduled: {reason}",
                        {
                            "due_at": due_at,
                            "priority": priority,
                            "follow_up_id": follow_id,
                            "appointment_id": appointment_id,
                        },
                        actor_user_id=user_id,
                    )
                conn.execute(
                    """
                    UPDATE leads
                    SET next_follow_up_at = ?, follow_up_reason = ?, follow_up_priority = ?,
                        follow_up_completed_at = NULL, follow_up_created_by = ?, updated_at = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        due_at,
                        reason,
                        priority,
                        user_id,
                        now,
                        appt["lead_id"],
                        user_id,
                    ),
                )
                applied["follow_up_at"] = due_at
                applied["follow_up_id"] = follow_id
                confirmation_bits.append(f"Follow-up scheduled for {due_at[:10]}")

            if apply_task and suggestion.get("suggested_task_title"):
                title = suggestion["suggested_task_title"]
                task_type = suggestion.get("suggested_task_type") or "general_follow_up"
                marker = f"appointment_id:{appointment_id}"
                existing_task = conn.execute(
                    """
                    SELECT id FROM tasks
                    WHERE user_id = ? AND lead_id = ? AND title = ?
                      AND status IN ('open', 'in_progress')
                      AND description LIKE ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (user_id, appt["lead_id"], title, f"%{marker}%"),
                ).fetchone()
                if existing_task:
                    task_id = existing_task["id"]
                else:
                    cur = conn.execute(
                        """
                        INSERT INTO tasks
                            (user_id, lead_id, assigned_user_id, title, description, due_at,
                             priority, status, task_type, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
                        """,
                        (
                            user_id,
                            appt["lead_id"],
                            user_id,
                            title,
                            f"Created from appointment outcome ({marker})",
                            suggestion.get("suggested_follow_up_at"),
                            "high" if suggestion.get("needs_attention") else "normal",
                            task_type,
                            now,
                            now,
                        ),
                    )
                    task_id = cur.lastrowid
                    _insert_activity(
                        conn,
                        appt["lead_id"],
                        user_id,
                        "task_created",
                        f"Task created: {title}",
                        {
                            "task_id": task_id,
                            "task_type": task_type,
                            "appointment_id": appointment_id,
                        },
                        actor_user_id=user_id,
                    )
                    confirmation_bits.append(f"Task created: {title}")
                applied["task_id"] = task_id
                if existing_task:
                    confirmation_bits.append(f"Task already exists: {title}")

            if apply_needs_attention and suggestion.get("needs_attention"):
                reason_code = (
                    "appointment_no_show" if outcome == "no_show" else "review_call_outcome"
                )
                existing_na = conn.execute(
                    """
                    SELECT id FROM needs_attention
                    WHERE user_id = ? AND lead_id = ? AND reason_code = ?
                      AND status = 'open'
                      AND source_ref_type = 'appointment' AND source_ref_id = ?
                    LIMIT 1
                    """,
                    (user_id, appt["lead_id"], reason_code, appointment_id),
                ).fetchone()
                if not existing_na:
                    conn.execute(
                        """
                        INSERT INTO needs_attention
                            (user_id, lead_id, reason_code, reason_text, priority,
                             source_ref_type, source_ref_id, status, created_at)
                        VALUES (?, ?, ?, ?, 'high', 'appointment', ?, 'open', ?)
                        """,
                        (
                            user_id,
                            appt["lead_id"],
                            reason_code,
                            (
                                "Lead no-showed appointment — reschedule or confirm interest."
                                if outcome == "no_show"
                                else (
                                    f"Appointment outcome {outcome_label(outcome)} "
                                    "needs agent follow-through."
                                )
                            ),
                            appointment_id,
                            now,
                        ),
                    )
                applied["needs_attention"] = True
                confirmation_bits.append("Needs Attention item opened")

            # Resolve missing-outcome NA items for this lead.
            conn.execute(
                """
                UPDATE needs_attention
                SET status = 'resolved', resolved_at = ?, resolution_reason = ?
                WHERE user_id = ? AND lead_id = ? AND reason_code = 'appointment_outcome_missing'
                  AND status = 'open'
                """,
                (now, "Outcome recorded", user_id, appt["lead_id"]),
            )

            confirmation = ". ".join(confirmation_bits) + "."
            logger.info(
                "appointment_outcome_success appointment_id=%s lead_id=%s "
                "status_applied=%s follow_up_applied=%s task_applied=%s",
                appointment_id,
                appt["lead_id"],
                bool(applied.get("lead_status")),
                bool(applied.get("follow_up_at")),
                bool(applied.get("task_id")),
            )
            return {
                "ok": True,
                "duplicate": False,
                "confirmation": confirmation,
                "appointment_id": appointment_id,
                "lead_id": appt["lead_id"],
                "outcome": outcome,
                "appointment_status": appointment_status,
                "next_action": next_action,
                "applied": applied,
                "suggestion": suggestion,
            }, None
    except Exception:
        # Log and re-raise so the route can return 500 and surface a visible error.
        # get_db() already rolled back the transaction.
        logger.exception(
            "appointment_outcome_failed appointment_id=%s outcome=%s",
            appointment_id,
            outcome,
        )
        raise


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


def get_pipeline_metrics(user_id, since_iso=None, local_date=None, tz_offset_minutes=None):
    """Pipeline card counts. Uses the same helpers as filtered destination lists."""
    refresh_needs_attention(user_id, local_date=local_date)
    now = _now()
    week_end = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    month_start = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    day = _calendar_day(local_date)

    with get_db() as conn:
        def count(sql, params):
            return conn.execute(sql, params).fetchone()["count"]

        new_leads = count(
            "SELECT COUNT(*) AS count FROM leads WHERE user_id = ? AND status = 'new'",
            (user_id,),
        )
        if since_iso:
            new_leads = count(
                "SELECT COUNT(*) AS count FROM leads WHERE user_id = ? AND created_at >= ?",
                (user_id, since_iso),
            )
        unreviewed = count(
            "SELECT COUNT(*) AS count FROM needs_attention WHERE user_id = ? AND status='open' AND reason_code='unreviewed_inbound'",
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
        sent = count(
            "SELECT COUNT(*) AS count FROM sms_messages WHERE user_id = ? AND status IN ('sent','delivered','queued')",
            (user_id,),
        )
        failed = count(
            "SELECT COUNT(*) AS count FROM sms_messages WHERE user_id = ? AND status='failed'",
            (user_id,),
        )
        status_rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM leads WHERE user_id = ? GROUP BY status",
            (user_id,),
        ).fetchall()
        try:
            unverified_consent = count(
                """
                SELECT COUNT(*) AS count FROM leads
                WHERE user_id = ? AND sms_consent_status = 'unverified'
                """,
                (user_id,),
            )
            sms_blocked = count(
                """
                SELECT COUNT(*) AS count FROM leads
                WHERE user_id = ? AND COALESCE(sms_sending_blocked, 1) = 1
                """,
                (user_id,),
            )
            external_leads = count(
                """
                SELECT COUNT(*) AS count FROM leads
                WHERE user_id = ?
                  AND (external_source_id IS NOT NULL OR source LIKE 'external:%')
                """,
                (user_id,),
            )
            consent_review = count(
                """
                SELECT COUNT(*) AS count FROM needs_attention
                WHERE user_id = ? AND status='open'
                  AND reason_code='consent_review_required'
                """,
                (user_id,),
            )
            verified_consent = count(
                """
                SELECT COUNT(*) AS count FROM leads
                WHERE user_id = ? AND sms_consent_status = 'verified'
                  AND COALESCE(sms_sending_blocked, 1) = 0
                """,
                (user_id,),
            )
            opted_out_consent = count(
                """
                SELECT COUNT(*) AS count FROM leads
                WHERE user_id = ?
                  AND (sms_consent_status = 'opted_out' OR opt_out_status = 'opted_out')
                """,
                (user_id,),
            )
        except Exception:
            unverified_consent = sms_blocked = external_leads = consent_review = 0
            verified_consent = opted_out_consent = 0

    by_status = {normalize_lead_status(r["status"]): r["count"] for r in status_rows}
    stages = []
    for stage_id, label, members in PIPELINE_STAGES:
        stages.append(
            {
                "id": stage_id,
                "label": label,
                # Same helper as /crm/leads?stage=… destination list.
                "count": count_filtered_leads(user_id, stage=stage_id),
                "href": f"/crm/leads?stage={stage_id}",
            }
        )
    delivery_total = sent + failed
    fu_counts = follow_up_dashboard_counts(
        user_id, local_date=day, tz_offset_minutes=tz_offset_minutes
    )
    return {
        "active_leads": count_filtered_leads(user_id, scope="active"),
        "new_leads": new_leads,
        "needs_attention": count_open_needs_attention(user_id, local_date=day),
        "overdue_follow_ups": fu_counts["follow_ups_overdue"],
        "follow_ups_due_today": fu_counts["follow_ups_due_today"],
        "follow_ups_due_this_week": fu_counts["follow_ups_due_this_week"],
        "tasks_due_today": count_tasks_due_today(user_id, local_date=day),
        "tasks_completed_today": count_tasks_completed(user_id, "today", local_date=day),
        "tasks_completed_this_week": count_tasks_completed(
            user_id, "last_7_days", local_date=day
        ),
        "appointments_today": count_appointments_today(user_id, local_date=day),
        "unreviewed_inbound": unreviewed,
        "drafts_awaiting_approval": count_pending_draft_insights(user_id),
        "appointments_this_week": appts_week,
        "outcomes_this_month": outcomes_month,
        "sms_delivery_success_rate": round((sent / delivery_total) * 100, 1)
        if delivery_total
        else 100.0,
        "leads_by_status": by_status,
        "pipeline_stages": stages,
        "average_first_response_hours": None,
        "unverified_consent": unverified_consent,
        "sms_blocked": sms_blocked,
        "external_leads": external_leads,
        "consent_review_required": consent_review,
        "verified_consent": verified_consent,
        "opted_out_consent": opted_out_consent,
    }


def _map_appointment_event_type(appt):
    appt_type = str(appt.get("appointment_type") or "")
    status = str(appt.get("status") or "")
    outcome = appt.get("outcome")
    start = str(appt.get("start_at") or "")
    now = _now()
    if (
        not outcome
        and status in {"completed", "no_show", "scheduled", "confirmed"}
        and start
        and start < now
        and status != "cancelled"
    ):
        # Past appointments still missing an outcome surface as reminders.
        if status in {"completed", "no_show"} or (
            status in {"scheduled", "confirmed"} and start < now
        ):
            if status in {"completed", "no_show"}:
                return "outcome_required"
    mapping = {
        "property_showing": "showing",
        "buyer_consultation": "buyer_consultation",
        "listing_consultation": "listing_consultation",
        "phone_call": "call",
        "video_meeting": "call",
        "open_house_follow_up": "appointment",
    }
    return mapping.get(appt_type, "appointment")


def _map_task_event_type(task):
    task_type = str(task.get("task_type") or "")
    mapping = {
        "call": "call",
        "send_sms": "sms_follow_up",
        "schedule_showing": "showing",
        "buyer_consultation": "buyer_consultation",
        "listing_consultation": "listing_consultation",
    }
    return mapping.get(task_type, "task")


def list_calendar_events(
    user_id,
    *,
    start_at=None,
    end_at=None,
    event_types=None,
    statuses=None,
    priorities=None,
    lead_status=None,
    lead_source=None,
    assigned_user_id=None,
    include_cancelled=False,
    include_completed=False,
    limit=1000,
):
    """Unified calendar events from follow-ups, tasks, and appointments.

    Stable ids: followup:<id>, task:<id>, appointment:<id>.
    Always scoped to user_id (tenant ownership).
    """
    wanted_types = None
    if event_types:
        wanted_types = {t for t in event_types if t in CALENDAR_EVENT_TYPE_SET}
    status_filter = {s for s in (statuses or []) if s}
    priority_filter = {p for p in (priorities or []) if p in PRIORITIES}
    assignee = assigned_user_id

    events = []
    with get_db() as conn:
        fu_rows = conn.execute(
            _follow_up_select_sql()
            + " WHERE f.user_id = ? ORDER BY f.due_at ASC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        for row in fu_rows:
            item = _row_to_follow_up(row)
            status = item.get("status") or "pending"
            if status == "cancelled" and not include_cancelled:
                continue
            if status == "done" and not include_completed:
                continue
            if status_filter and status not in status_filter:
                continue
            if priority_filter and (item.get("priority") or "normal") not in priority_filter:
                continue
            if lead_status and normalize_lead_status(item.get("lead_status")) != normalize_lead_status(lead_status):
                continue
            if lead_source and str(item.get("lead_source") or "") != str(lead_source):
                continue
            if assignee and int(item.get("created_by") or item.get("user_id") or 0) != int(assignee):
                continue
            due = item.get("due_at")
            if start_at and due and due < str(start_at):
                continue
            if end_at and due and due > str(end_at):
                continue
            events.append(
                {
                    "id": f"followup:{item['id']}",
                    "source_type": "follow_up",
                    "source_id": item["id"],
                    "event_type": "follow_up",
                    "lead_id": item.get("lead_id"),
                    "lead_name": item.get("lead_name"),
                    "lead_status": item.get("lead_status"),
                    "lead_source": item.get("lead_source"),
                    "phone_number": item.get("phone_number"),
                    "title": item.get("reason") or "Follow up",
                    "start_at": due,
                    "end_at": due,
                    "status": status,
                    "priority": item.get("priority") or "normal",
                    "assigned_agent": item.get("created_by_email"),
                    "assigned_user_id": item.get("created_by") or item.get("user_id"),
                }
            )

        task_rows = conn.execute(
            """
            SELECT t.*, l.name AS lead_name, l.phone_number, l.status AS lead_status,
                   l.source AS lead_source, u.email AS assigned_email
            FROM tasks t
            LEFT JOIN leads l ON l.id = t.lead_id
            LEFT JOIN users u ON u.id = t.assigned_user_id
            WHERE t.user_id = ?
            ORDER BY COALESCE(t.due_at, t.created_at) ASC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        for row in task_rows:
            item = dict(row)
            status = item.get("status") or "open"
            if status == "cancelled" and not include_cancelled:
                continue
            if status == "completed" and not include_completed:
                continue
            if status_filter and status not in status_filter:
                continue
            if priority_filter and (item.get("priority") or "normal") not in priority_filter:
                continue
            if lead_status and item.get("lead_id") and normalize_lead_status(item.get("lead_status")) != normalize_lead_status(lead_status):
                continue
            if lead_source and str(item.get("lead_source") or "") != str(lead_source):
                continue
            if assignee and int(item.get("assigned_user_id") or item.get("user_id") or 0) != int(assignee):
                continue
            due = item.get("due_at")
            if start_at and due and due < str(start_at):
                continue
            if end_at and due and due > str(end_at):
                continue
            etype = _map_task_event_type(item)
            events.append(
                {
                    "id": f"task:{item['id']}",
                    "source_type": "task",
                    "source_id": item["id"],
                    "event_type": etype,
                    "lead_id": item.get("lead_id"),
                    "lead_name": item.get("lead_name"),
                    "lead_status": item.get("lead_status"),
                    "lead_source": item.get("lead_source"),
                    "phone_number": item.get("phone_number"),
                    "title": item.get("title") or "Task",
                    "start_at": due,
                    "end_at": due,
                    "status": status,
                    "priority": item.get("priority") or "normal",
                    "assigned_agent": item.get("assigned_email"),
                    "assigned_user_id": item.get("assigned_user_id") or item.get("user_id"),
                    "task_type": item.get("task_type"),
                }
            )

        appt_rows = conn.execute(
            """
            SELECT a.*, l.name AS lead_name, l.phone_number, l.status AS lead_status,
                   l.source AS lead_source, u.email AS owner_email
            FROM appointments a
            JOIN leads l ON l.id = a.lead_id
            LEFT JOIN users u ON u.id = a.user_id
            WHERE a.user_id = ?
            ORDER BY a.start_at ASC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        for row in appt_rows:
            item = dict(row)
            status = item.get("status") or "scheduled"
            if status == "cancelled" and not include_cancelled:
                continue
            if status == "completed" and not include_completed and item.get("outcome"):
                continue
            if status_filter and status not in status_filter:
                continue
            if lead_status and normalize_lead_status(item.get("lead_status")) != normalize_lead_status(lead_status):
                continue
            if lead_source and str(item.get("lead_source") or "") != str(lead_source):
                continue
            if assignee and int(item.get("user_id") or 0) != int(assignee):
                continue
            start = item.get("start_at")
            if start_at and start and start < str(start_at):
                continue
            if end_at and start and start > str(end_at):
                continue
            etype = _map_appointment_event_type(item)
            events.append(
                {
                    "id": f"appointment:{item['id']}",
                    "source_type": "appointment",
                    "source_id": item["id"],
                    "event_type": etype,
                    "lead_id": item.get("lead_id"),
                    "lead_name": item.get("lead_name"),
                    "lead_status": item.get("lead_status"),
                    "lead_source": item.get("lead_source"),
                    "phone_number": item.get("phone_number"),
                    "title": (item.get("appointment_type") or "appointment").replace("_", " ").title(),
                    "start_at": start,
                    "end_at": item.get("end_at") or start,
                    "status": status,
                    "priority": "normal",
                    "assigned_agent": item.get("owner_email"),
                    "assigned_user_id": item.get("user_id"),
                    "appointment_type": item.get("appointment_type"),
                    "outcome": item.get("outcome"),
                }
            )

    if wanted_types is not None:
        events = [e for e in events if e.get("event_type") in wanted_types]

    # Deduplicate by stable event id (retries / overlapping queries never double-render).
    seen = set()
    unique = []
    for event in sorted(events, key=lambda e: (e.get("start_at") or "", e["id"])):
        if event["id"] in seen:
            continue
        seen.add(event["id"])
        unique.append(event)
    return unique[: max(int(limit), 1)]


def calendar_summary(user_id, local_date=None, tz_offset_minutes=None):
    day = _calendar_day(local_date)
    counts = follow_up_dashboard_counts(
        user_id, local_date=day, tz_offset_minutes=tz_offset_minutes
    )
    tasks_today = list_tasks(user_id, bucket="today", local_date=day, limit=50)
    appts = list_appointments(user_id, limit=200)
    appts_today = [
        a
        for a in appts
        if a.get("status") not in {"cancelled"}
        and _local_date_for_due(a.get("start_at"), tz_offset_minutes, day) == day
    ]
    week_events = list_calendar_events(
        user_id,
        start_at=f"{day}T00:00:00",
        end_at=(datetime.strptime(day, "%Y-%m-%d") + timedelta(days=7)).strftime(
            "%Y-%m-%dT23:59:59"
        ),
        include_cancelled=False,
        include_completed=False,
        limit=300,
    )
    return {
        **counts,
        "tasks_due_today": len(tasks_today),
        "appointments_today": len(appts_today),
        "upcoming_events_this_week": len(week_events),
    }


ACTIVE_LEAD_EXCLUDED_STATUSES = ("closed_won", "closed_lost", "do_not_contact")


def pipeline_stage_statuses(stage):
    """Return lead status members for a pipeline stage id, or empty set."""
    stage = str(stage or "").strip().lower()
    for stage_id, _label, members in PIPELINE_STAGES:
        if stage_id == stage:
            return set(members)
    return set()


def filter_leads(
    user_id,
    status=None,
    source=None,
    scope=None,
    stage=None,
    limit=200,
    *,
    sms_consent_status=None,
    sms_sending_blocked=None,
    pond_status=None,
    external_only=None,
    import_batch_id=None,
    consent_review_required=None,
):
    """List leads for this tenant. scope=active / stage=* match dashboard Pipeline cards."""
    from crm_constants import normalize_lead_status as norm

    with get_db() as conn:
        sql = """
            SELECT l.*,
                   (SELECT COUNT(*) FROM sms_messages sm WHERE sm.lead_id = l.id) AS message_count
            FROM leads l
            WHERE l.user_id = ?
        """
        params = [user_id]
        if scope == "active":
            sql += (
                " AND l.status NOT IN ('closed_won', 'closed_lost', 'do_not_contact')"
            )
        stage_members = pipeline_stage_statuses(stage) if stage else set()
        if stage_members:
            placeholders = ", ".join("?" for _ in stage_members)
            sql += f" AND l.status IN ({placeholders})"
            params.extend(sorted(stage_members))
        elif status:
            sql += " AND l.status = ?"
            params.append(norm(status))
        if source:
            sql += " AND l.source = ?"
            params.append(source)
        if sms_consent_status:
            sql += " AND l.sms_consent_status = ?"
            params.append(sms_consent_status)
        if sms_sending_blocked is not None:
            blocked = 1 if sms_sending_blocked in (True, 1, "1", "true") else 0
            sql += " AND COALESCE(l.sms_sending_blocked, 1) = ?"
            params.append(blocked)
        if pond_status:
            sql += " AND l.pond_status = ?"
            params.append(pond_status)
        if external_only in (True, 1, "1", "true", "yes"):
            sql += " AND (l.external_source_id IS NOT NULL OR l.source LIKE 'external:%')"
        if import_batch_id:
            sql += " AND l.import_batch_id = ?"
            params.append(import_batch_id)
        if consent_review_required in (True, 1, "1", "true", "yes"):
            sql += """
                AND EXISTS (
                    SELECT 1 FROM needs_attention na
                    WHERE na.lead_id = l.id AND na.user_id = l.user_id
                      AND na.reason_code = 'consent_review_required'
                      AND na.status = 'open'
                )
            """
        sql += " ORDER BY l.updated_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def count_filtered_leads(
    user_id,
    status=None,
    source=None,
    scope=None,
    stage=None,
    **kwargs,
):
    return len(
        filter_leads(
            user_id,
            status=status,
            source=source,
            scope=scope,
            stage=stage,
            limit=100000,
            **kwargs,
        )
    )


def list_follow_ups_for_dashboard_range(
    user_id, range_key="today", local_date=None, tz_offset_minutes=None, limit=500
):
    """Follow-ups for dashboard cards. Same bucketing as follow_up_dashboard_counts."""
    range_key = str(range_key or "today").strip().lower()
    day = _calendar_day(local_date)
    items = list_follow_ups(
        user_id,
        bucket="all",
        limit=limit,
        local_date=day,
        tz_offset_minutes=tz_offset_minutes,
    )
    start = datetime.strptime(day, "%Y-%m-%d").date()
    end = start + timedelta(days=7)
    out = []
    for item in items:
        if item.get("status") != "pending":
            continue
        due_day = _local_date_for_due(item.get("due_at"), tz_offset_minutes, day)
        try:
            d = datetime.strptime(due_day, "%Y-%m-%d").date()
        except ValueError:
            continue
        if range_key in {"today", "due_today"} and d == start:
            out.append(item)
        elif range_key == "overdue" and d < start:
            out.append(item)
        elif range_key in {"this_week", "week"} and start <= d < end:
            out.append(item)
        elif range_key in {"upcoming"} and d > start:
            out.append(item)
        elif range_key in {"all", "open"}:
            out.append(item)
    return out


def list_pending_draft_insights(user_id, limit=100):
    """Pending Claude draft suggestions — same source as drafts_awaiting_approval."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT i.*, l.name AS lead_name, l.phone_number, l.status AS lead_status
            FROM lead_insights i
            LEFT JOIN leads l ON l.id = i.lead_id
            WHERE i.user_id = ? AND i.status = 'pending'
            ORDER BY i.created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def count_pending_draft_insights(user_id):
    with get_db() as conn:
        return conn.execute(
            """
            SELECT COUNT(*) AS count FROM lead_insights
            WHERE user_id = ? AND status = 'pending'
            """,
            (user_id,),
        ).fetchone()["count"]


def count_open_needs_attention(user_id, local_date=None):
    refresh_needs_attention(user_id, local_date=local_date)
    with get_db() as conn:
        return conn.execute(
            """
            SELECT COUNT(*) AS count FROM needs_attention
            WHERE user_id = ? AND status = 'open'
            """,
            (user_id,),
        ).fetchone()["count"]


def count_tasks_due_today(user_id, local_date=None):
    day = _calendar_day(local_date)
    return len(list_tasks(user_id, bucket="today", local_date=day, limit=10000))


def count_appointments_today(user_id, local_date=None):
    day = _calendar_day(local_date)
    with get_db() as conn:
        return conn.execute(
            """
            SELECT COUNT(*) AS count FROM appointments
            WHERE user_id = ? AND substr(start_at, 1, 10) = ?
              AND status NOT IN ('cancelled')
            """,
            (user_id, day),
        ).fetchone()["count"]


def list_appointments_for_range(user_id, range_key="today", local_date=None, limit=200):
    day = _calendar_day(local_date)
    start = datetime.strptime(day, "%Y-%m-%d").date()
    items = list_appointments(user_id, limit=limit)
    out = []
    for item in items:
        if item.get("status") == "cancelled":
            continue
        due_day = str(item.get("start_at") or "")[:10]
        try:
            d = datetime.strptime(due_day, "%Y-%m-%d").date()
        except ValueError:
            continue
        if range_key == "today" and d == start:
            out.append(item)
        elif range_key == "overdue" and d < start:
            out.append(item)
        elif range_key in {"this_week", "week"} and start <= d < start + timedelta(days=7):
            out.append(item)
    return out
