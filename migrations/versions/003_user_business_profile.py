"""Add per-user business profile fields for Vapi variableValues."""

VERSION = "003_user_business_profile"

COLUMNS = [
    ("agent_name", "TEXT"),
    ("brokerage_name", "TEXT"),
    ("company_name", "TEXT"),
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


def upgrade_sqlite(conn):
    cols = _sqlite_columns(conn, "users")
    for column, definition in COLUMNS:
        if column not in cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")


def upgrade_postgres(conn):
    for column, definition in COLUMNS:
        if not _postgres_has_column(conn, "users", column):
            conn.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
