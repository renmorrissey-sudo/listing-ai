"""Additive Telnyx webhook events + tenant sender profile fields."""

VERSION = "013_telnyx_messaging"

SENDER_COLS_SQLITE = [
    ("messaging_profile_id", "TEXT"),
    ("trial_mode", "INTEGER NOT NULL DEFAULT 0"),
    ("brand_id", "TEXT"),
    ("campaign_registration_id", "TEXT"),
    ("onboarding_status", "TEXT"),
]

SENDER_COLS_PG = [
    ("messaging_profile_id", "TEXT"),
    ("trial_mode", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("brand_id", "TEXT"),
    ("campaign_registration_id", "TEXT"),
    ("onboarding_status", "TEXT"),
]

MESSAGE_COLS_SQLITE = [
    ("raw_provider_status", "TEXT"),
    ("provider_cost", "TEXT"),
    ("failure_code", "TEXT"),
    ("submitted_at", "TEXT"),
    ("delivered_at", "TEXT"),
    ("failed_at", "TEXT"),
]

MESSAGE_COLS_PG = [
    ("raw_provider_status", "TEXT"),
    ("provider_cost", "TEXT"),
    ("failure_code", "TEXT"),
    ("submitted_at", "TIMESTAMPTZ"),
    ("delivered_at", "TIMESTAMPTZ"),
    ("failed_at", "TIMESTAMPTZ"),
]

SQLITE_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS sms_webhook_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT NOT NULL,
        provider_event_id TEXT,
        event_type TEXT NOT NULL,
        tenant_id INTEGER,
        provider_message_id TEXT,
        processing_status TEXT NOT NULL DEFAULT 'received',
        received_at TEXT NOT NULL,
        processed_at TEXT,
        safe_metadata TEXT,
        UNIQUE (provider, provider_event_id)
    )
    """,
]

PG_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS sms_webhook_events (
        id BIGSERIAL PRIMARY KEY,
        provider TEXT NOT NULL,
        provider_event_id TEXT,
        event_type TEXT NOT NULL,
        tenant_id BIGINT,
        provider_message_id TEXT,
        processing_status TEXT NOT NULL DEFAULT 'received',
        received_at TIMESTAMPTZ NOT NULL,
        processed_at TIMESTAMPTZ,
        safe_metadata TEXT,
        UNIQUE (provider, provider_event_id)
    )
    """,
]


def _sqlite_has_column(conn, table, column):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    names = {dict(r)["name"] if hasattr(r, "keys") else r[1] for r in rows}
    return column in names


def _postgres_has_column(conn, table, column):
    row = conn.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        LIMIT 1
        """,
        (table, column),
    ).fetchone()
    return bool(row)


def _add_columns(conn, table, columns, *, is_postgres):
    for name, typ in columns:
        has = _postgres_has_column(conn, table, name) if is_postgres else _sqlite_has_column(conn, table, name)
        if has:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


def upgrade_sqlite(conn):
    for ddl in SQLITE_TABLES:
        conn.execute(ddl)
    _add_columns(conn, "tenant_sms_senders", SENDER_COLS_SQLITE, is_postgres=False)
    _add_columns(conn, "sms_messages", MESSAGE_COLS_SQLITE, is_postgres=False)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sms_webhook_events_provider "
        "ON sms_webhook_events(provider, provider_event_id)"
    )


def upgrade_postgres(conn):
    for ddl in PG_TABLES:
        conn.execute(ddl)
    _add_columns(conn, "tenant_sms_senders", SENDER_COLS_PG, is_postgres=True)
    _add_columns(conn, "sms_messages", MESSAGE_COLS_PG, is_postgres=True)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sms_webhook_events_provider "
        "ON sms_webhook_events(provider, provider_event_id)"
    )
