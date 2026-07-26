"""Ensure CRM columns exist without dropping tables or deleting rows."""

VERSION = "002_safe_additive_columns"

SQLITE_COLUMNS = [
    ("users", "role", "TEXT DEFAULT 'agent'"),
    ("sms_messages", "lead_id", "INTEGER"),
    ("sms_messages", "direction", "TEXT DEFAULT 'outbound'"),
    ("sms_messages", "consent_status", "TEXT DEFAULT 'unknown'"),
    ("sms_messages", "opt_out_status", "TEXT DEFAULT 'active'"),
    ("sms_messages", "approved_by_user_id", "INTEGER"),
    ("sms_messages", "consent_source", "TEXT"),
    ("leads", "consent_status", "TEXT DEFAULT 'unknown'"),
    ("leads", "opt_out_status", "TEXT DEFAULT 'active'"),
    ("leads", "priority", "TEXT DEFAULT 'normal'"),
    ("leads", "assigned_user_id", "INTEGER"),
    ("leads", "next_follow_up_at", "TEXT"),
    ("leads", "follow_up_reason", "TEXT"),
    ("leads", "follow_up_priority", "TEXT DEFAULT 'normal'"),
    ("leads", "follow_up_completed_at", "TEXT"),
    ("leads", "follow_up_created_by", "INTEGER"),
    ("lead_follow_ups", "priority", "TEXT DEFAULT 'normal'"),
    ("lead_follow_ups", "created_by", "INTEGER"),
    ("lead_follow_ups", "completed_at", "TEXT"),
    ("lead_insights", "intent", "TEXT"),
    ("lead_insights", "confidence_score", "REAL"),
    ("lead_insights", "requires_manual_review", "INTEGER DEFAULT 0"),
    ("lead_insights", "escalation_topics", "TEXT"),
]

POSTGRES_COLUMNS = [
    ("users", "role", "TEXT DEFAULT 'agent'"),
    ("sms_messages", "lead_id", "BIGINT"),
    ("sms_messages", "direction", "TEXT DEFAULT 'outbound'"),
    ("sms_messages", "consent_status", "TEXT DEFAULT 'unknown'"),
    ("sms_messages", "opt_out_status", "TEXT DEFAULT 'active'"),
    ("sms_messages", "approved_by_user_id", "BIGINT"),
    ("sms_messages", "consent_source", "TEXT"),
    ("leads", "consent_status", "TEXT DEFAULT 'unknown'"),
    ("leads", "opt_out_status", "TEXT DEFAULT 'active'"),
    ("leads", "priority", "TEXT DEFAULT 'normal'"),
    ("leads", "assigned_user_id", "BIGINT"),
    ("leads", "next_follow_up_at", "TEXT"),
    ("leads", "follow_up_reason", "TEXT"),
    ("leads", "follow_up_priority", "TEXT DEFAULT 'normal'"),
    ("leads", "follow_up_completed_at", "TEXT"),
    ("leads", "follow_up_created_by", "BIGINT"),
    ("lead_follow_ups", "priority", "TEXT DEFAULT 'normal'"),
    ("lead_follow_ups", "created_by", "BIGINT"),
    ("lead_follow_ups", "completed_at", "TEXT"),
    ("lead_insights", "intent", "TEXT"),
    ("lead_insights", "confidence_score", "DOUBLE PRECISION"),
    ("lead_insights", "requires_manual_review", "INTEGER DEFAULT 0"),
    ("lead_insights", "escalation_topics", "TEXT"),
]


def _sqlite_columns(conn, table):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] if isinstance(r, dict) else r[1] for r in rows}


def _postgres_has_column(conn, table, column):
    row = conn.execute(
        """
        SELECT 1 AS ok
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = ? AND column_name = ?
        LIMIT 1
        """,
        (table, column),
    ).fetchone()
    return bool(row)


def _postgres_table_exists(conn, table):
    row = conn.execute(
        """
        SELECT 1 AS ok
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = ?
        LIMIT 1
        """,
        (table,),
    ).fetchone()
    return bool(row)


def upgrade_sqlite(conn):
    for table, column, definition in SQLITE_COLUMNS:
        cols = _sqlite_columns(conn, table)
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def upgrade_postgres(conn):
    for table, column, definition in POSTGRES_COLUMNS:
        if not _postgres_table_exists(conn, table):
            raise RuntimeError(
                f"Additive migration 002 cannot run: table {table!r} does not exist. "
                "Baseline migration 001_baseline must create it first."
            )
        # Idempotent on PostgreSQL 9.1+.
        if not _postgres_has_column(conn, table, column):
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}"
            )
