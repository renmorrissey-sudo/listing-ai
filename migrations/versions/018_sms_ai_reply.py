"""AI SMS auto-reply correlation columns + provider message lookup index.

Additive only:
- sms_messages.reply_to_message_id: inbound sms_messages.id this outbound row answers.
  A partial UNIQUE index guarantees at most ONE automated reply per inbound message,
  even across concurrent gunicorn workers or Telnyx webhook retries.
- sms_messages.ai_generated: 1/TRUE when the message body was produced by the AI SMS Agent.
- Index on sms_messages.provider_message_id for webhook delivery-status lookups.
"""

VERSION = "018_sms_ai_reply"

MESSAGE_COLS_SQLITE = [
    ("reply_to_message_id", "INTEGER"),
    ("ai_generated", "INTEGER NOT NULL DEFAULT 0"),
]

MESSAGE_COLS_PG = [
    ("reply_to_message_id", "BIGINT"),
    ("ai_generated", "BOOLEAN NOT NULL DEFAULT FALSE"),
]

INDEXES = [
    # Partial unique: only rows that ARE replies participate; NULLs are unconstrained.
    (
        "uq_sms_messages_reply_to",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_sms_messages_reply_to "
        "ON sms_messages(reply_to_message_id) WHERE reply_to_message_id IS NOT NULL",
    ),
    (
        "idx_sms_messages_provider_message_id",
        "CREATE INDEX IF NOT EXISTS idx_sms_messages_provider_message_id "
        "ON sms_messages(provider_message_id)",
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
