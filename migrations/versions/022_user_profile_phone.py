"""Add subscriber phone number for email signatures."""

VERSION = "022_user_profile_phone"


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
    cols = _sqlite_columns(conn, "users")
    if "phone_number" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN phone_number TEXT")


def upgrade_postgres(conn):
    if not _postgres_table_exists(conn, "users"):
        raise RuntimeError(
            "Additive migration 022 cannot run: table 'users' does not exist. "
            "Baseline migration 001_baseline must create it first."
        )
    if not _postgres_has_column(conn, "users", "phone_number"):
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_number TEXT")
