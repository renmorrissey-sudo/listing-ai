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
