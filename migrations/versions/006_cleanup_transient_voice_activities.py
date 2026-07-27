"""Non-destructive cleanup of duplicate transient voice timeline activities.

Deletes only low-value voice_call_started / voice_call_updated rows whose
payload status/outcome is a transient dialer state (queued, initiated, pending,
started, ringing, etc.) and that do not carry recording, transcript, summary,
or appointment signals.

Never deletes voice_call_completed, failures with meaningful status, opt-outs,
appointments, or non-voice activities. Does not touch voice_calls or leads.
"""

VERSION = "006_cleanup_transient_voice_activities"

TRANSIENT = (
    "queued",
    "ringing",
    "initiated",
    "pending",
    "started",
    "starting",
    "scheduled",
    "connecting",
    "loading",
)


def _payload_text(row):
    if isinstance(row, dict):
        return str(row.get("payload_json") or "")
    try:
        return str(row["payload_json"] or "")
    except Exception:
        return str(row[5] if len(row) > 5 else "")


def _summary_text(row):
    if isinstance(row, dict):
        return str(row.get("summary") or "")
    try:
        return str(row["summary"] or "")
    except Exception:
        return ""


def _event_type(row):
    if isinstance(row, dict):
        return str(row.get("event_type") or "")
    try:
        return str(row["event_type"] or "")
    except Exception:
        return ""


def _is_safe_transient_delete(row):
    event_type = _event_type(row)
    if event_type not in {"voice_call_started", "voice_call_updated"}:
        return False
    payload = _payload_text(row).lower()
    summary = _summary_text(row).lower()

    # Keep anything with agent-useful content attached.
    if "has_recording\": true" in payload or "has_transcript\": true" in payload:
        return False
    if "appointment_requested\": true" in payload:
        return False

    if event_type == "voice_call_started":
        return True

    transient_hit = any(
        f'"status": "{token}"' in payload
        or f'"status":"{token}"' in payload
        or f'"outcome": "{token}"' in payload
        or f'"outcome":"{token}"' in payload
        or f'"lifecycle_status": "{token}"' in payload
        or f'"meaningful_status": "{token}"' in payload
        or token in summary
        for token in TRANSIENT
    )
    if not transient_hit:
        # Also catch legacy "AI call started" / "AI call updated" with no useful body.
        if summary.strip() in {"ai call started", "ai call updated"}:
            return True
        if summary.startswith("ai call started:") or summary.startswith("ai call updated:"):
            return any(token in summary for token in TRANSIENT)
        return False
    return True


def upgrade_postgres(conn):
    from migrations.pg_ddl import pg_execute

    raw = conn._raw
    cur = raw.execute(
        """
        SELECT id, lead_id, user_id, event_type, summary, payload_json
        FROM lead_activities
        WHERE event_type IN ('voice_call_started', 'voice_call_updated')
        """
    )
    rows = cur.fetchall()
    try:
        cur.close()
    except Exception:
        pass

    # psycopg rows may be tuples; normalize via column indexes from query order.
    normalized = []
    for row in rows:
        if isinstance(row, dict):
            normalized.append(row)
        else:
            normalized.append(
                {
                    "id": row[0],
                    "lead_id": row[1],
                    "user_id": row[2],
                    "event_type": row[3],
                    "summary": row[4],
                    "payload_json": row[5],
                }
            )

    for row in normalized:
        if not _is_safe_transient_delete(row):
            continue
        pg_execute(
            conn,
            "DELETE FROM lead_activities WHERE id = %s AND user_id = %s",
            (row["id"], row["user_id"]),
        )


def upgrade_sqlite(conn):
    rows = conn.execute(
        """
        SELECT id, lead_id, user_id, event_type, summary, payload_json
        FROM lead_activities
        WHERE event_type IN ('voice_call_started', 'voice_call_updated')
        """
    ).fetchall()
    for row in rows:
        item = dict(row)
        if not _is_safe_transient_delete(item):
            continue
        conn.execute(
            "DELETE FROM lead_activities WHERE id = ? AND user_id = ?",
            (item["id"], item["user_id"]),
        )
