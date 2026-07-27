"""Additive columns for inbound SMS metadata (to_number, media). Never drops data."""

VERSION = "009_inbound_sms_metadata"

POSTGRES_COLUMNS = [
    ("sms_messages", "to_number", "TEXT"),
    ("sms_messages", "media_urls", "TEXT"),
    ("sms_messages", "num_media", "INTEGER DEFAULT 0"),
]

SQLITE_COLUMNS = [
    ("sms_messages", "to_number", "TEXT"),
    ("sms_messages", "media_urls", "TEXT"),
    ("sms_messages", "num_media", "INTEGER DEFAULT 0"),
]


def _sqlite_columns(conn, table):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] if isinstance(r, dict) else r[1] for r in rows}


def _postgres_has_column(conn, table, column):
    raw = conn._raw
    cur = raw.execute(
        """
        SELECT 1 AS ok
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s AND column_name = %s
        LIMIT 1
        """,
        (table, column),
    )
    row = cur.fetchone()
    try:
        cur.close()
    except Exception:
        pass
    return bool(row)


def upgrade_postgres(conn):
    from migrations.pg_ddl import pg_execute

    for table, column, definition in POSTGRES_COLUMNS:
        if not _postgres_has_column(conn, table, column):
            pg_execute(
                conn,
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}",
            )


def upgrade_sqlite(conn):
    for table, column, definition in SQLITE_COLUMNS:
        cols = _sqlite_columns(conn, table)
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
