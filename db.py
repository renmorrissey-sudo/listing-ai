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


def update_voice_call_from_webhook(provider_call_id, status=None, outcome=None, transcript=None, summary=None, recording_url=None, appointment_requested=False):
    completed_at = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE voice_calls
            SET status = COALESCE(?, status),
                outcome = COALESCE(?, outcome),
                transcript = COALESCE(?, transcript),
                summary = COALESCE(?, summary),
                recording_url = COALESCE(?, recording_url),
                appointment_requested = ?,
                completed_at = ?
            WHERE provider_call_id = ?
            """,
            (
                status,
                outcome,
                transcript,
                summary,
                recording_url,
                1 if appointment_requested else 0,
                completed_at,
                provider_call_id,
            ),
        )


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
