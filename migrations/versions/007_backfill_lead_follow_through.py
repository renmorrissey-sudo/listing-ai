"""Backfill empty Next Action / Follow-up on leads after voice outcomes.

Non-destructive: only fills NULL/empty next_action and missing next_follow_up_at
for active leads that already have completed voice-call context or appointment
status. Does not reset, drop, or recreate tables.
"""

VERSION = "007_backfill_lead_follow_through"

from datetime import datetime, timedelta, timezone


def _due_iso(days=1):
    due = datetime.now(timezone.utc) + timedelta(days=days)
    return due.replace(hour=15, minute=0, second=0, microsecond=0).isoformat()


def _row_get(row, key, index):
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except Exception:
        return row[index]


def _backfill(conn):
    now = datetime.now(timezone.utc).isoformat()
    due_appt = _due_iso(1)
    due_default = _due_iso(2)

    conn.execute(
        """
        UPDATE leads
        SET next_action = 'Confirm appointment details and send a calendar invite',
            updated_at = ?
        WHERE (next_action IS NULL OR next_action = '')
          AND status = 'appointment_scheduled'
        """,
        (now,),
    )

    rows = conn.execute(
        """
        SELECT l.id AS lead_id, l.user_id AS user_id, vc.summary AS summary
        FROM leads l
        JOIN voice_calls vc ON vc.lead_id = l.id AND vc.user_id = l.user_id
        WHERE (l.next_action IS NULL OR l.next_action = '')
          AND l.status NOT IN ('closed_won', 'closed_lost', 'do_not_contact')
          AND vc.status = 'completed'
          AND vc.summary IS NOT NULL
          AND vc.summary != ''
        ORDER BY vc.completed_at DESC, vc.id DESC
        """
    ).fetchall()

    seen = set()
    for row in rows:
        lead_id = _row_get(row, "lead_id", 0)
        user_id = _row_get(row, "user_id", 1)
        summary = str(_row_get(row, "summary", 2) or "").strip()
        key = (user_id, lead_id)
        if key in seen or not summary:
            continue
        seen.add(key)
        conn.execute(
            """
            UPDATE leads
            SET next_action = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
              AND (next_action IS NULL OR next_action = '')
            """,
            (f"Follow up on call: {summary[:160]}", now, lead_id, user_id),
        )

    conn.execute(
        """
        UPDATE leads
        SET next_follow_up_at = ?,
            follow_up_reason = COALESCE(NULLIF(follow_up_reason, ''), 'Confirm appointment from AI call'),
            follow_up_priority = COALESCE(follow_up_priority, 'high'),
            updated_at = ?
        WHERE next_follow_up_at IS NULL
          AND (follow_up_at IS NULL OR follow_up_at = '')
          AND status = 'appointment_scheduled'
        """,
        (due_appt, now),
    )

    conn.execute(
        """
        UPDATE leads
        SET next_follow_up_at = ?,
            follow_up_reason = COALESCE(NULLIF(follow_up_reason, ''), 'Follow up after AI call'),
            follow_up_priority = COALESCE(follow_up_priority, 'normal'),
            updated_at = ?
        WHERE next_follow_up_at IS NULL
          AND (follow_up_at IS NULL OR follow_up_at = '')
          AND status IN ('contacted', 'qualified', 'nurture', 'attempting_contact')
          AND EXISTS (
              SELECT 1 FROM voice_calls vc
              WHERE vc.lead_id = leads.id
                AND vc.user_id = leads.user_id
                AND vc.status = 'completed'
          )
        """,
        (due_default, now),
    )


def upgrade_postgres(conn):
    _backfill(conn)


def upgrade_sqlite(conn):
    _backfill(conn)
