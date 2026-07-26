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
                user_id INTEGER,
                persona_id INTEGER,
                lead_id INTEGER,
                provider TEXT NOT NULL DEFAULT 'twilio',
                provider_message_id TEXT,
                direction TEXT NOT NULL DEFAULT 'outbound',
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
            """
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT,
                phone_number TEXT NOT NULL,
                lead_type TEXT,
                property_interest TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                source TEXT NOT NULL DEFAULT 'sms',
                notes TEXT,
                next_action TEXT,
                follow_up_at TEXT,
                last_inbound_at TEXT,
                last_outbound_at TEXT,
                consent_status TEXT NOT NULL DEFAULT 'unknown',
                opt_out_status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, phone_number)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lead_follow_ups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                due_at TEXT NOT NULL,
                reason TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lead_insights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                inbound_message_id INTEGER,
                suggested_message_id INTEGER,
                summary TEXT,
                intent TEXT,
                next_best_step TEXT,
                recommended_action TEXT,
                suggested_reply TEXT,
                home_value_pitch TEXT,
                confidence_score REAL,
                requires_manual_review INTEGER NOT NULL DEFAULT 0,
                escalation_topics TEXT,
                model TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                raw_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        _ensure_column(conn, "sms_messages", "lead_id", "INTEGER")
        _ensure_column(conn, "sms_messages", "direction", "TEXT DEFAULT 'outbound'")
        _ensure_column(conn, "sms_messages", "consent_status", "TEXT DEFAULT 'unknown'")
        _ensure_column(conn, "sms_messages", "opt_out_status", "TEXT DEFAULT 'active'")
        _ensure_column(conn, "leads", "consent_status", "TEXT DEFAULT 'unknown'")
        _ensure_column(conn, "leads", "opt_out_status", "TEXT DEFAULT 'active'")
        _ensure_column(conn, "lead_insights", "intent", "TEXT")
        _ensure_column(conn, "lead_insights", "confidence_score", "REAL")
        _ensure_column(conn, "lead_insights", "requires_manual_review", "INTEGER DEFAULT 0")
        _ensure_column(conn, "lead_insights", "escalation_topics", "TEXT")
        _ensure_column(conn, "users", "role", "TEXT DEFAULT 'agent'")
        _ensure_column(conn, "leads", "priority", "TEXT DEFAULT 'normal'")
        _ensure_column(conn, "leads", "assigned_user_id", "INTEGER")
        _ensure_column(conn, "leads", "next_follow_up_at", "TEXT")
        _ensure_column(conn, "leads", "follow_up_reason", "TEXT")
        _ensure_column(conn, "leads", "follow_up_priority", "TEXT DEFAULT 'normal'")
        _ensure_column(conn, "leads", "follow_up_completed_at", "TEXT")
        _ensure_column(conn, "leads", "follow_up_created_by", "INTEGER")
        _ensure_column(conn, "lead_follow_ups", "priority", "TEXT DEFAULT 'normal'")
        _ensure_column(conn, "lead_follow_ups", "created_by", "INTEGER")
        _ensure_column(conn, "lead_follow_ups", "completed_at", "TEXT")
        _ensure_column(conn, "sms_messages", "approved_by_user_id", "INTEGER")
        _ensure_column(conn, "sms_messages", "consent_source", "TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lead_activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                actor_user_id INTEGER,
                event_type TEXT NOT NULL,
                summary TEXT,
                payload_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                lead_id INTEGER,
                assigned_user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                due_at TEXT,
                priority TEXT NOT NULL DEFAULT 'normal',
                status TEXT NOT NULL DEFAULT 'open',
                task_type TEXT NOT NULL DEFAULT 'general_follow_up',
                completed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                lead_id INTEGER NOT NULL,
                appointment_type TEXT NOT NULL DEFAULT 'phone_call',
                start_at TEXT NOT NULL,
                end_at TEXT,
                location TEXT,
                notes TEXT,
                status TEXT NOT NULL DEFAULT 'scheduled',
                outcome TEXT,
                outcome_notes TEXT,
                next_action TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS needs_attention (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                lead_id INTEGER,
                reason_code TEXT NOT NULL,
                reason_text TEXT,
                priority TEXT NOT NULL DEFAULT 'normal',
                source_ref_type TEXT,
                source_ref_id INTEGER,
                status TEXT NOT NULL DEFAULT 'open',
                resolution_reason TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                resolved_by INTEGER
            )
            """
        )
        _ensure_needs_attention_lead_nullable(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT,
                link TEXT,
                lead_id INTEGER,
                read_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sms_messages_user_created ON sms_messages(user_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sms_messages_provider_id ON sms_messages(provider_message_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sms_messages_lead_created ON sms_messages(lead_id, created_at ASC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_leads_user_updated ON leads(user_id, updated_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_lead_follow_ups_user_due ON lead_follow_ups(user_id, due_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_lead_insights_user_status ON lead_insights(user_id, status, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tool_usage_user_created ON tool_usage(user_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tool_usage_user_tool ON tool_usage(user_id, tool_key)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_lead_activities_lead ON lead_activities(lead_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_user_due ON tasks(user_id, due_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_appointments_user_start ON appointments(user_id, start_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_needs_attention_user_status ON needs_attention(user_id, status, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, created_at DESC)"
        )
        _ensure_default_voice_personas(conn)


def _ensure_column(conn, table, column, definition):
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _ensure_needs_attention_lead_nullable(conn):
    """Allow Needs Attention items for overdue tasks with no linked lead."""
    info = conn.execute("PRAGMA table_info(needs_attention)").fetchall()
    if not info:
        return
    lead_col = next((row for row in info if row[1] == "lead_id"), None)
    if not lead_col or lead_col[3] == 0:
        return
    conn.execute(
        """
        CREATE TABLE needs_attention_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            lead_id INTEGER,
            reason_code TEXT NOT NULL,
            reason_text TEXT,
            priority TEXT NOT NULL DEFAULT 'normal',
            source_ref_type TEXT,
            source_ref_id INTEGER,
            status TEXT NOT NULL DEFAULT 'open',
            resolution_reason TEXT,
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            resolved_by INTEGER
        )
        """
    )
    conn.execute(
        """
        INSERT INTO needs_attention_new
            (id, user_id, lead_id, reason_code, reason_text, priority, source_ref_type,
             source_ref_id, status, resolution_reason, created_at, resolved_at, resolved_by)
        SELECT id, user_id, lead_id, reason_code, reason_text, priority, source_ref_type,
               source_ref_id, status, resolution_reason, created_at, resolved_at, resolved_by
        FROM needs_attention
        """
    )
    conn.execute("DROP TABLE needs_attention")
    conn.execute("ALTER TABLE needs_attention_new RENAME TO needs_attention")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_needs_attention_user_status ON needs_attention(user_id, status, created_at DESC)"
    )


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


def upsert_lead(user_id, phone_number, data=None, source="sms"):
    data = data or {}
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM leads WHERE user_id = ? AND phone_number = ?",
            (user_id, phone_number),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE leads
                SET name = COALESCE(?, name),
                    lead_type = COALESCE(?, lead_type),
                    property_interest = COALESCE(?, property_interest),
                    notes = COALESCE(?, notes),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    data.get("lead_name") or data.get("name"),
                    data.get("lead_type"),
                    data.get("property_interest"),
                    data.get("notes"),
                    now,
                    existing["id"],
                ),
            )
            return existing["id"]
        cur = conn.execute(
            """
            INSERT INTO leads
                (user_id, name, phone_number, lead_type, property_interest, status, source,
                 notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'new', ?, ?, ?, ?)
            """,
            (
                user_id,
                data.get("lead_name") or data.get("name") or "Lead",
                phone_number,
                data.get("lead_type"),
                data.get("property_interest"),
                source,
                data.get("notes"),
                now,
                now,
            ),
        )
        return cur.lastrowid


def get_lead(lead_id, user_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM leads WHERE id = ? AND user_id = ?",
            (lead_id, user_id),
        ).fetchone()
        return dict(row) if row else None


def get_lead_by_phone(user_id, phone_number):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM leads WHERE user_id = ? AND phone_number = ?",
            (user_id, phone_number),
        ).fetchone()
        return dict(row) if row else None


def list_leads(user_id, limit=50):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT l.*,
                   (SELECT COUNT(*) FROM sms_messages sm WHERE sm.lead_id = l.id) AS message_count
            FROM leads l
            WHERE l.user_id = ?
            ORDER BY l.updated_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def update_lead_from_analysis(lead_id, user_id, *, status=None, notes=None, next_action=None, follow_up_at=None, last_inbound_at=None):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE leads
            SET status = COALESCE(?, status),
                notes = COALESCE(?, notes),
                next_action = COALESCE(?, next_action),
                follow_up_at = COALESCE(?, follow_up_at),
                last_inbound_at = COALESCE(?, last_inbound_at),
                updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (status, notes, next_action, follow_up_at, last_inbound_at, now, lead_id, user_id),
        )


def touch_lead_outbound(lead_id, user_id):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE leads
            SET last_outbound_at = ?,
                status = CASE WHEN status = 'new' THEN 'contacted' ELSE status END,
                updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (now, now, lead_id, user_id),
        )


def create_follow_up(lead_id, user_id, due_at, reason):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO lead_follow_ups (lead_id, user_id, due_at, reason, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (lead_id, user_id, due_at, reason, now),
        )
        return cur.lastrowid


def list_due_follow_ups(user_id, limit=20):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT f.*, l.name AS lead_name, l.phone_number, l.status AS lead_status
            FROM lead_follow_ups f
            JOIN leads l ON l.id = f.lead_id
            WHERE f.user_id = ? AND f.status = 'pending' AND f.due_at <= ?
            ORDER BY f.due_at ASC
            LIMIT ?
            """,
            (user_id, now, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def create_lead_insight(lead_id, user_id, inbound_message_id, analysis, suggested_message_id=None, model="claude"):
    now = datetime.now(timezone.utc).isoformat()
    topics = analysis.get("escalation_topics") or []
    if isinstance(topics, list):
        topics_value = ",".join(topics)
    else:
        topics_value = str(topics or "")
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO lead_insights
                (lead_id, user_id, inbound_message_id, suggested_message_id, summary, intent, next_best_step,
                 recommended_action, suggested_reply, home_value_pitch, confidence_score,
                 requires_manual_review, escalation_topics, model, status, raw_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                lead_id,
                user_id,
                inbound_message_id,
                suggested_message_id,
                analysis.get("summary"),
                analysis.get("intent"),
                analysis.get("next_best_step"),
                analysis.get("recommended_action"),
                analysis.get("suggested_reply"),
                analysis.get("home_value_pitch"),
                analysis.get("confidence_score"),
                1 if analysis.get("requires_manual_review") else 0,
                topics_value,
                model,
                analysis.get("raw_json"),
                now,
            ),
        )
        return cur.lastrowid


def list_pending_insights(user_id, limit=20):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT i.*, l.name AS lead_name, l.phone_number, l.status AS lead_status,
                   sm.message_body AS inbound_body
            FROM lead_insights i
            JOIN leads l ON l.id = i.lead_id
            LEFT JOIN sms_messages sm ON sm.id = i.inbound_message_id
            WHERE i.user_id = ? AND i.status = 'pending'
            ORDER BY i.created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def get_insight(insight_id, user_id):
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT i.*, l.name AS lead_name, l.phone_number, l.lead_type, l.property_interest
            FROM lead_insights i
            JOIN leads l ON l.id = i.lead_id
            WHERE i.id = ? AND i.user_id = ?
            """,
            (insight_id, user_id),
        ).fetchone()
        return dict(row) if row else None


def update_insight_status(insight_id, user_id, status):
    with get_db() as conn:
        conn.execute(
            "UPDATE lead_insights SET status = ? WHERE id = ? AND user_id = ?",
            (status, insight_id, user_id),
        )


def create_sms_message(
    user_id,
    persona_id,
    provider,
    data,
    status="draft",
    lead_id=None,
    direction="outbound",
    consent_status="unknown",
    opt_out_status="active",
):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO sms_messages
                (user_id, persona_id, lead_id, provider, direction, lead_name, phone_number, lead_type,
                 property_interest, desired_outcome, notes, message_body, status, consent_status,
                 opt_out_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                persona_id,
                lead_id,
                provider,
                direction,
                data.get("lead_name"),
                data.get("phone_number"),
                data.get("lead_type"),
                data.get("property_interest"),
                data.get("desired_outcome"),
                data.get("notes"),
                data.get("message_body"),
                status,
                consent_status,
                opt_out_status,
                now,
            ),
        )
        return cur.lastrowid


def mark_lead_opt_out(lead_id, user_id):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE leads
            SET opt_out_status = 'opted_out',
                status = 'do_not_contact',
                next_action = 'Do not contact — lead opted out',
                next_follow_up_at = NULL,
                follow_up_reason = NULL,
                updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (now, lead_id, user_id),
        )
        conn.execute(
            """
            UPDATE sms_messages
            SET status = 'cancelled',
                error_message = 'Cancelled — lead opted out',
                direction = 'suggested'
            WHERE lead_id = ? AND user_id = ? AND status = 'suggested'
            """,
            (lead_id, user_id),
        )
        conn.execute(
            """
            UPDATE lead_insights
            SET status = 'dismissed'
            WHERE lead_id = ? AND user_id = ? AND status = 'pending'
            """,
            (lead_id, user_id),
        )


def cancel_suggested_sms_for_lead(lead_id, user_id, reason="Cancelled"):
    with get_db() as conn:
        conn.execute(
            """
            UPDATE sms_messages
            SET status = 'cancelled', error_message = ?
            WHERE lead_id = ? AND user_id = ? AND status = 'suggested'
            """,
            (reason[:500], lead_id, user_id),
        )


def get_sms_message_by_provider_id(provider_message_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM sms_messages WHERE provider_message_id = ? LIMIT 1",
            (provider_message_id,),
        ).fetchone()
        return dict(row) if row else None


def set_lead_consent(lead_id, user_id, consent_status="confirmed"):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE leads
            SET consent_status = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (consent_status, now, lead_id, user_id),
        )


def update_sms_message_body(message_id, user_id, message_body, direction="outbound"):
    with get_db() as conn:
        conn.execute(
            """
            UPDATE sms_messages
            SET message_body = ?, direction = ?
            WHERE id = ? AND user_id = ?
            """,
            (message_body, direction, message_id, user_id),
        )


def update_sms_compliance(message_id, user_id, consent_status=None, opt_out_status=None):
    with get_db() as conn:
        conn.execute(
            """
            UPDATE sms_messages
            SET consent_status = COALESCE(?, consent_status),
                opt_out_status = COALESCE(?, opt_out_status)
            WHERE id = ? AND user_id = ?
            """,
            (consent_status, opt_out_status, message_id, user_id),
        )


def update_sms_message_send_result(message_id, provider_message_id=None, status="sent", error_message=None):
    sent_at = datetime.now(timezone.utc).isoformat() if status in ("sent", "delivered", "queued") else None
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


def update_sms_message_by_provider_id(provider_message_id, status=None, error_message=None):
    if not provider_message_id:
        return 0
    sent_at = datetime.now(timezone.utc).isoformat() if status in ("sent", "delivered") else None
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE sms_messages
            SET status = COALESCE(?, status),
                error_message = COALESCE(?, error_message),
                sent_at = COALESCE(?, sent_at)
            WHERE provider_message_id = ?
            """,
            (status, error_message, sent_at, provider_message_id),
        )
        return cur.rowcount


def find_sms_user_by_phone(phone_number):
    if not phone_number:
        return None
    with get_db() as conn:
        lead = conn.execute(
            """
            SELECT user_id FROM leads
            WHERE phone_number = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (phone_number,),
        ).fetchone()
        if lead:
            return lead["user_id"]
        row = conn.execute(
            """
            SELECT user_id
            FROM sms_messages
            WHERE phone_number = ? AND status != 'received' AND user_id IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (phone_number,),
        ).fetchone()
        return row["user_id"] if row else None


def create_inbound_sms_message(
    user_id,
    phone_number,
    message_body,
    provider_message_id=None,
    lead_id=None,
    lead_name=None,
    opt_out_status="active",
):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO sms_messages
                (user_id, persona_id, lead_id, provider, provider_message_id, direction, lead_name,
                 phone_number, message_body, status, consent_status, opt_out_status, created_at)
            VALUES (?, NULL, ?, 'twilio', ?, 'inbound', ?, ?, ?, 'received', 'unknown', ?, ?)
            """,
            (user_id, lead_id, provider_message_id, lead_name, phone_number, message_body, opt_out_status, now),
        )
        return cur.lastrowid


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


def list_lead_messages(user_id, lead_id, limit=100):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT sm.*, vp.name AS persona_name
            FROM sms_messages sm
            LEFT JOIN voice_personas vp ON vp.id = sm.persona_id
            WHERE sm.user_id = ? AND sm.lead_id = ?
            ORDER BY sm.created_at ASC
            LIMIT ?
            """,
            (user_id, lead_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def get_sms_message(message_id, user_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM sms_messages WHERE id = ? AND user_id = ?",
            (message_id, user_id),
        ).fetchone()
        return dict(row) if row else None


def last_outbound_seed_for_phone(user_id, phone_number):
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT lead_name, lead_type, property_interest, notes, desired_outcome
            FROM sms_messages
            WHERE user_id = ? AND phone_number = ? AND status != 'received'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id, phone_number),
        ).fetchone()
        return dict(row) if row else {}


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

        leads_total = conn.execute(
            "SELECT COUNT(*) AS count FROM leads WHERE user_id = ?",
            (user_id,),
        ).fetchone()["count"]
        leads_replied = conn.execute(
            "SELECT COUNT(*) AS count FROM leads WHERE user_id = ? AND status = 'replied'",
            (user_id,),
        ).fetchone()["count"]
        leads_nurture = conn.execute(
            "SELECT COUNT(*) AS count FROM leads WHERE user_id = ? AND status = 'nurture'",
            (user_id,),
        ).fetchone()["count"]
        leads_hot = conn.execute(
            "SELECT COUNT(*) AS count FROM leads WHERE user_id = ? AND status = 'hot'",
            (user_id,),
        ).fetchone()["count"]
        pending_suggestions = conn.execute(
            "SELECT COUNT(*) AS count FROM lead_insights WHERE user_id = ? AND status = 'pending'",
            (user_id,),
        ).fetchone()["count"]
        follow_ups_due = conn.execute(
            """
            SELECT COUNT(*) AS count FROM lead_follow_ups
            WHERE user_id = ? AND status = 'pending' AND due_at <= ?
            """,
            (user_id, now.isoformat()),
        ).fetchone()["count"]
        recent_leads = conn.execute(
            """
            SELECT id, name, phone_number, status, next_action, follow_up_at, updated_at
            FROM leads
            WHERE user_id = ?
            ORDER BY updated_at DESC
            LIMIT 8
            """,
            (user_id,),
        ).fetchall()
        due_follow_ups = conn.execute(
            """
            SELECT f.id, f.due_at, f.reason, l.name AS lead_name, l.phone_number, l.id AS lead_id
            FROM lead_follow_ups f
            JOIN leads l ON l.id = f.lead_id
            WHERE f.user_id = ? AND f.status = 'pending' AND f.due_at <= ?
            ORDER BY f.due_at ASC
            LIMIT 8
            """,
            (user_id, now.isoformat()),
        ).fetchall()

    content_pieces = (listing_total * 3) + (scripts_total * 3)
    return {
        "overview": {
            "listing_generations": listing_total,
            "script_generations": scripts_total,
            "ai_calls": calls_total,
            "sms_messages": sms_total,
            "leads_total": leads_total,
            "appointments_requested": calls_appointments,
            "content_pieces": content_pieces,
            "activity_7d": listing_7d + scripts_7d + calls_7d + sms_7d,
            "activity_30d": listing_30d + scripts_30d + calls_30d + sms_30d,
        },
        "crm": {
            "leads_total": leads_total,
            "leads_replied": leads_replied,
            "leads_nurture": leads_nurture,
            "leads_hot": leads_hot,
            "pending_suggestions": pending_suggestions,
            "follow_ups_due": follow_ups_due,
            "recent_leads": [dict(row) for row in recent_leads],
            "due_follow_ups": [dict(row) for row in due_follow_ups],
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
