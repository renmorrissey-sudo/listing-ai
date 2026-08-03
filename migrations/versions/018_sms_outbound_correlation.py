"""Add correlation_id on sms_messages for safe outbound send diagnostics."""

VERSION = "018_sms_outbound_correlation"

MESSAGE_COLS_SQLITE = [
    ("correlation_id", "TEXT"),
]

MESSAGE_COLS_PG = [
    ("correlation_id", "TEXT"),
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sms_messages_correlation "
        "ON sms_messages(correlation_id)"
    )


def upgrade_postgres(conn):
    _add_columns(conn, "sms_messages", MESSAGE_COLS_PG, is_postgres=True)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sms_messages_correlation "
        "ON sms_messages(correlation_id)"
    )
