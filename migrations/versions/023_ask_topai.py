"""Ask TopAI command audit log + optional structured property criteria on leads.

Additive only. ask_topai_commands stores interpreted/confirmed voice and text
commands. leads.property_criteria_json holds mergeable buyer criteria without
replacing the existing property_interest display field.
"""

VERSION = "023_ask_topai"

SQLITE_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS ask_topai_commands (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        source TEXT NOT NULL DEFAULT 'ask_topai',
        transcript TEXT NOT NULL,
        interpreted_json TEXT,
        confirmation_token_hash TEXT,
        status TEXT NOT NULL,
        lead_id INTEGER,
        result_json TEXT,
        created_at TEXT NOT NULL,
        executed_at TEXT,
        expires_at TEXT
    )
    """,
]

PG_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS ask_topai_commands (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        source TEXT NOT NULL DEFAULT 'ask_topai',
        transcript TEXT NOT NULL,
        interpreted_json TEXT,
        confirmation_token_hash TEXT,
        status TEXT NOT NULL,
        lead_id BIGINT,
        result_json TEXT,
        created_at TEXT NOT NULL,
        executed_at TEXT,
        expires_at TEXT
    )
    """,
]

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_ask_topai_commands_user ON ask_topai_commands(user_id, created_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ask_topai_commands_token ON ask_topai_commands(confirmation_token_hash)",
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


def upgrade_sqlite(conn):
    for ddl in SQLITE_TABLES:
        conn.execute(ddl)
    for sql in INDEXES:
        conn.execute(sql)
    if not _sqlite_has_column(conn, "leads", "property_criteria_json"):
        conn.execute("ALTER TABLE leads ADD COLUMN property_criteria_json TEXT")


def upgrade_postgres(conn):
    for ddl in PG_TABLES:
        conn.execute(ddl)
    for sql in INDEXES:
        conn.execute(sql)
    if not _postgres_has_column(conn, "leads", "property_criteria_json"):
        conn.execute("ALTER TABLE leads ADD COLUMN property_criteria_json TEXT")
