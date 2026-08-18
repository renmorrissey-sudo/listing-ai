"""Add scheduled_send_at so quiet-hours SMS can be deferred without sending now.

Additive only:
- sms_messages.scheduled_send_at: UTC ISO timestamp when a status='scheduled'
  outbound row becomes eligible for the campaign worker to send.
"""

VERSION = "022_sms_scheduled_send_at"

MESSAGE_COLS_SQLITE = [
    ("scheduled_send_at", "TEXT"),
]

MESSAGE_COLS_PG = [
    ("scheduled_send_at", "TEXT"),
]

INDEXES = [
    (
        "idx_sms_messages_scheduled_due",
        "CREATE INDEX IF NOT EXISTS idx_sms_messages_scheduled_due "
        "ON sms_messages(scheduled_send_at) WHERE status = 'scheduled'",
    ),
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
        has = (
            _postgres_has_column(conn, table, name)
            if is_postgres
            else _sqlite_has_column(conn, table, name)
        )
        if has:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


def upgrade_sqlite(conn):
    _add_columns(conn, "sms_messages", MESSAGE_COLS_SQLITE, is_postgres=False)
    for _name, ddl in INDEXES:
        conn.execute(ddl)


def upgrade_postgres(conn):
    _add_columns(conn, "sms_messages", MESSAGE_COLS_PG, is_postgres=True)
    for _name, ddl in INDEXES:
        conn.execute(ddl)
