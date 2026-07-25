import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import config


def _connect():
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                stripe_customer_id TEXT,
                subscription_status TEXT NOT NULL DEFAULT 'none',
                subscription_id TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS voice_personas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                name TEXT NOT NULL,
                persona_type TEXT NOT NULL,
                prompt TEXT NOT NULL,
                tone TEXT NOT NULL DEFAULT 'professional',
                goal TEXT NOT NULL,
                objection_handling_notes TEXT,
                is_default INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS voice_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                persona_id INTEGER,
                provider TEXT NOT NULL,
                provider_call_id TEXT,
                direction TEXT NOT NULL,
                lead_name TEXT,
                phone_number TEXT NOT NULL,
                lead_type TEXT,
                property_interest TEXT,
                desired_outcome TEXT,
                notes TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                outcome TEXT,
                appointment_requested INTEGER NOT NULL DEFAULT 0,
                transcript TEXT,
                summary TEXT,
                recording_url TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tool_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                tool_key TEXT NOT NULL,
                event_type TEXT NOT NULL DEFAULT 'generated',
                metadata TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sms_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                persona_id INTEGER,
                provider TEXT NOT NULL DEFAULT 'twilio',
                provider_message_id TEXT,
                lead_name TEXT,
                phone_number TEXT NOT NULL,
                lead_type TEXT,
                property_interest TEXT,
                desired_outcome TEXT,
                notes TEXT,
                message_body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                error_message TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sms_messages_user_created ON sms_messages(user_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tool_usage_user_created ON tool_usage(user_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tool_usage_user_tool ON tool_usage(user_id, tool_key)"
        )
        _ensure_default_voice_personas(conn)


def _ensure_default_voice_personas(conn):
    existing = conn.execute("SELECT COUNT(*) AS count FROM voice_personas WHERE is_default = 1").fetchone()
    if existing and existing["count"]:
        return
    now = datetime.now(timezone.utc).isoformat()
    personas = [
        (
            "ISA / New Lead Follow-up",
            "isa",
            "You are an AI calling assistant for a real estate agent. Your job is to quickly respond to a new lead, qualify their needs, answer basic objections, and ask for a short appointment with the agent.",
            "friendly, confident, and concise",
            "Qualify the lead and request an appointment with the agent.",
            "If the lead is hesitant, acknowledge the concern, ask one helpful question, and offer a low-pressure next step.",
        ),
        (
            "Open House Follow-up",
            "open_house",
            "You are an AI calling assistant following up after an open house. Your job is to thank the visitor, learn what they thought of the property, ask where they are in their home search, and offer to schedule a showing or buyer consultation.",
            "warm, helpful, and conversational",
            "Understand buyer interest and ask for a showing or consultation.",
            "If the lead is just browsing, offer to send similar listings and ask what criteria matter most.",
        ),
    ]
    for name, persona_type, prompt, tone, goal, objection_notes in personas:
        conn.execute(
            """
            INSERT INTO voice_personas
                (user_id, name, persona_type, prompt, tone, goal, objection_handling_notes, is_default, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?)
            """,
            (None, name, persona_type, prompt, tone, goal, objection_notes, now),
        )


def create_user(email, password_hash):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
            (email.lower().strip(), password_hash, now),
        )
        return cur.lastrowid


def get_user_by_email(email):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
        ).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_user_by_stripe_customer(customer_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE stripe_customer_id = ?", (customer_id,)
        ).fetchone()
        return dict(row) if row else None


def update_user_subscription(user_id, status, subscription_id=None, stripe_customer_id=None):
    with get_db() as conn:
        conn.execute(
            """
            UPDATE users
            SET subscription_status = ?,
                subscription_id = COALESCE(?, subscription_id),
                stripe_customer_id = COALESCE(?, stripe_customer_id)
            WHERE id = ?
            """,
            (status, subscription_id, stripe_customer_id, user_id),
        )


def set_stripe_customer(user_id, stripe_customer_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET stripe_customer_id = ? WHERE id = ?",
            (stripe_customer_id, user_id),
        )


def list_voice_personas(user_id=None):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM voice_personas
            WHERE active = 1 AND (is_default = 1 OR user_id = ?)
            ORDER BY is_default DESC, name ASC
            """,
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_voice_persona(persona_id, user_id=None):
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM voice_personas
            WHERE id = ? AND active = 1 AND (is_default = 1 OR user_id = ?)
            """,
            (persona_id, user_id),
        ).fetchone()
        return dict(row) if row else None


def create_voice_persona(user_id, data):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO voice_personas
                (user_id, name, persona_type, prompt, tone, goal, objection_handling_notes, is_default, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1, ?)
            """,
            (
                user_id,
                data.get("name"),
                data.get("persona_type"),
                data.get("prompt"),
                data.get("tone"),
                data.get("goal"),
                data.get("objection_handling_notes"),
                now,
            ),
        )
        return cur.lastrowid


def create_voice_call(user_id, persona_id, provider, direction, data):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO voice_calls
                (user_id, persona_id, provider, direction, lead_name, phone_number, lead_type,
                 property_interest, desired_outcome, notes, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)
            """,
            (
                user_id,
                persona_id,
                provider,
                direction,
                data.get("lead_name"),
                data.get("phone_number"),
                data.get("lead_type"),
                data.get("property_interest"),
                data.get("desired_outcome"),
                data.get("notes"),
                now,
            ),
        )
        return cur.lastrowid


def update_voice_call_provider(call_id, provider_call_id, status):
    with get_db() as conn:
        conn.execute(
            "UPDATE voice_calls SET provider_call_id = ?, status = ? WHERE id = ?",
            (provider_call_id, status, call_id),
        )


def update_voice_call_from_webhook(call_id=None, provider_call_id=None, status=None, outcome=None, transcript=None, summary=None, recording_url=None, appointment_requested=False):
    completed_at = datetime.now(timezone.utc).isoformat()
    appointment_flag = 1 if appointment_requested else 0
    update_sql = """
        UPDATE voice_calls
        SET provider_call_id = COALESCE(?, provider_call_id),
            status = COALESCE(?, status),
            outcome = COALESCE(?, outcome),
            transcript = COALESCE(?, transcript),
            summary = COALESCE(?, summary),
            recording_url = COALESCE(?, recording_url),
            appointment_requested = CASE WHEN ? = 1 THEN 1 ELSE appointment_requested END,
            completed_at = CASE WHEN ? = 'completed' THEN COALESCE(completed_at, ?) ELSE completed_at END
        WHERE {where_clause}
        """

    params = (
        provider_call_id,
        status,
        outcome,
        transcript,
        summary,
        recording_url,
        appointment_flag,
        status,
        completed_at,
    )

    with get_db() as conn:
        if provider_call_id:
            cur = conn.execute(
                update_sql.format(where_clause="provider_call_id = ?"),
                params + (provider_call_id,),
            )
            if cur.rowcount:
                return cur.rowcount

        if call_id:
            cur = conn.execute(
                update_sql.format(where_clause="id = ?"),
                params + (call_id,),
            )
            return cur.rowcount

        return 0


def get_voice_call(call_id, user_id):
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT vc.*, vp.name AS persona_name
            FROM voice_calls vc
            LEFT JOIN voice_personas vp ON vp.id = vc.persona_id
            WHERE vc.id = ? AND vc.user_id = ?
            """,
            (call_id, user_id),
        ).fetchone()
        return dict(row) if row else None


def list_voice_calls(user_id, limit=20):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT vc.*, vp.name AS persona_name
            FROM voice_calls vc
            LEFT JOIN voice_personas vp ON vp.id = vc.persona_id
            WHERE vc.user_id = ?
            ORDER BY vc.created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def create_sms_message(user_id, persona_id, provider, data, status="draft"):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO sms_messages
                (user_id, persona_id, provider, lead_name, phone_number, lead_type,
                 property_interest, desired_outcome, notes, message_body, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                persona_id,
                provider,
                data.get("lead_name"),
                data.get("phone_number"),
                data.get("lead_type"),
                data.get("property_interest"),
                data.get("desired_outcome"),
                data.get("notes"),
                data.get("message_body"),
                status,
                now,
            ),
        )
        return cur.lastrowid


def update_sms_message_send_result(message_id, provider_message_id=None, status="sent", error_message=None):
    sent_at = datetime.now(timezone.utc).isoformat() if status == "sent" else None
    with get_db() as conn:
        conn.execute(
            """
            UPDATE sms_messages
            SET provider_message_id = COALESCE(?, provider_message_id),
                status = ?,
                error_message = ?,
                sent_at = COALESCE(?, sent_at)
            WHERE id = ?
            """,
            (provider_message_id, status, error_message, sent_at, message_id),
        )


def list_sms_messages(user_id, limit=20):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT sm.*, vp.name AS persona_name
            FROM sms_messages sm
            LEFT JOIN voice_personas vp ON vp.id = sm.persona_id
            WHERE sm.user_id = ?
            ORDER BY sm.created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def record_tool_usage(user_id, tool_key, event_type="generated", metadata=None):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO tool_usage (user_id, tool_key, event_type, metadata, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, tool_key, event_type, metadata, now),
        )
        return cur.lastrowid


def _count_usage_since(conn, user_id, tool_key, since_iso=None):
    if since_iso:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM tool_usage
            WHERE user_id = ? AND tool_key = ? AND created_at >= ?
            """,
            (user_id, tool_key, since_iso),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM tool_usage
            WHERE user_id = ? AND tool_key = ?
            """,
            (user_id, tool_key),
        ).fetchone()
    return int(row["count"] if row else 0)


def _last_usage_at(conn, user_id, tool_key):
    row = conn.execute(
        """
        SELECT created_at
        FROM tool_usage
        WHERE user_id = ? AND tool_key = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (user_id, tool_key),
    ).fetchone()
    return row["created_at"] if row else None


def get_dashboard_metrics(user_id):
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    since_7d = (now - timedelta(days=7)).isoformat()
    since_30d = (now - timedelta(days=30)).isoformat()

    with get_db() as conn:
        listing_total = _count_usage_since(conn, user_id, "listing_generator")
        listing_7d = _count_usage_since(conn, user_id, "listing_generator", since_7d)
        listing_30d = _count_usage_since(conn, user_id, "listing_generator", since_30d)
        listing_last = _last_usage_at(conn, user_id, "listing_generator")

        scripts_total = _count_usage_since(conn, user_id, "cold_call_scripts")
        scripts_7d = _count_usage_since(conn, user_id, "cold_call_scripts", since_7d)
        scripts_30d = _count_usage_since(conn, user_id, "cold_call_scripts", since_30d)
        scripts_last = _last_usage_at(conn, user_id, "cold_call_scripts")

        calls_total = conn.execute(
            "SELECT COUNT(*) AS count FROM voice_calls WHERE user_id = ?",
            (user_id,),
        ).fetchone()["count"]
        calls_7d = conn.execute(
            "SELECT COUNT(*) AS count FROM voice_calls WHERE user_id = ? AND created_at >= ?",
            (user_id, since_7d),
        ).fetchone()["count"]
        calls_30d = conn.execute(
            "SELECT COUNT(*) AS count FROM voice_calls WHERE user_id = ? AND created_at >= ?",
            (user_id, since_30d),
        ).fetchone()["count"]
        calls_completed = conn.execute(
            "SELECT COUNT(*) AS count FROM voice_calls WHERE user_id = ? AND status = 'completed'",
            (user_id,),
        ).fetchone()["count"]
        calls_appointments = conn.execute(
            "SELECT COUNT(*) AS count FROM voice_calls WHERE user_id = ? AND appointment_requested = 1",
            (user_id,),
        ).fetchone()["count"]
        calls_with_recording = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM voice_calls
            WHERE user_id = ? AND recording_url IS NOT NULL AND recording_url != ''
            """,
            (user_id,),
        ).fetchone()["count"]
        calls_last = conn.execute(
            """
            SELECT created_at FROM voice_calls
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        calls_last = calls_last["created_at"] if calls_last else None

        recent_usage = conn.execute(
            """
            SELECT tool_key, event_type, created_at
            FROM tool_usage
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 8
            """,
            (user_id,),
        ).fetchall()
        recent_calls = conn.execute(
            """
            SELECT lead_name, status, appointment_requested, created_at, summary
            FROM voice_calls
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 5
            """,
            (user_id,),
        ).fetchall()

        sms_total = conn.execute(
            "SELECT COUNT(*) AS count FROM sms_messages WHERE user_id = ?",
            (user_id,),
        ).fetchone()["count"]
        sms_7d = conn.execute(
            "SELECT COUNT(*) AS count FROM sms_messages WHERE user_id = ? AND created_at >= ?",
            (user_id, since_7d),
        ).fetchone()["count"]
        sms_30d = conn.execute(
            "SELECT COUNT(*) AS count FROM sms_messages WHERE user_id = ? AND created_at >= ?",
            (user_id, since_30d),
        ).fetchone()["count"]
        sms_sent = conn.execute(
            "SELECT COUNT(*) AS count FROM sms_messages WHERE user_id = ? AND status = 'sent'",
            (user_id,),
        ).fetchone()["count"]
        sms_draft = conn.execute(
            "SELECT COUNT(*) AS count FROM sms_messages WHERE user_id = ? AND status = 'draft'",
            (user_id,),
        ).fetchone()["count"]
        sms_last = conn.execute(
            """
            SELECT created_at FROM sms_messages
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        sms_last = sms_last["created_at"] if sms_last else None
        recent_sms = conn.execute(
            """
            SELECT lead_name, phone_number, status, message_body, created_at
            FROM sms_messages
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 5
            """,
            (user_id,),
        ).fetchall()

    content_pieces = (listing_total * 3) + (scripts_total * 3)
    return {
        "overview": {
            "listing_generations": listing_total,
            "script_generations": scripts_total,
            "ai_calls": calls_total,
            "sms_messages": sms_total,
            "appointments_requested": calls_appointments,
            "content_pieces": content_pieces,
            "activity_7d": listing_7d + scripts_7d + calls_7d + sms_7d,
            "activity_30d": listing_30d + scripts_30d + calls_30d + sms_30d,
        },
        "tools": {
            "listing_generator": {
                "label": "Listing Generator",
                "total": listing_total,
                "last_7_days": listing_7d,
                "last_30_days": listing_30d,
                "last_used_at": listing_last,
                "outputs_per_run": 3,
                "outputs_label": "listing + social + email drafts",
            },
            "cold_call_scripts": {
                "label": "Cold Call Scripts",
                "total": scripts_total,
                "last_7_days": scripts_7d,
                "last_30_days": scripts_30d,
                "last_used_at": scripts_last,
                "outputs_per_run": 3,
                "outputs_label": "opening + objections + voicemail",
            },
            "ai_calling": {
                "label": "AI Calling Assistant",
                "total": calls_total,
                "last_7_days": calls_7d,
                "last_30_days": calls_30d,
                "last_used_at": calls_last,
                "completed": calls_completed,
                "appointments_requested": calls_appointments,
                "recordings_available": calls_with_recording,
            },
            "ai_sms": {
                "label": "AI SMS Assistant",
                "total": sms_total,
                "last_7_days": sms_7d,
                "last_30_days": sms_30d,
                "last_used_at": sms_last,
                "sent": sms_sent,
                "drafts": sms_draft,
            },
        },
        "recent_usage": [dict(row) for row in recent_usage],
        "recent_calls": [dict(row) for row in recent_calls],
        "recent_sms": [dict(row) for row in recent_sms],
    }
