"""Add paid user phone number to the business profile."""

VERSION = "028_user_profile_phone"


def _has_column(conn, table, column):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any((row[1] if not hasattr(row, "keys") else row["name"]) == column for row in rows)


def upgrade_sqlite(conn):
    if not _has_column(conn, "users", "phone_number"):
        conn.execute("ALTER TABLE users ADD COLUMN phone_number TEXT")


def upgrade_postgres(conn):
    conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_number TEXT")


def verify(conn, dialect):
    if dialect == "postgresql":
        row = conn.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = 'users' AND column_name = 'phone_number'
            """
        ).fetchone()
        if not row:
            raise RuntimeError("028_user_profile_phone failed: users.phone_number missing")
        return

    if not _has_column(conn, "users", "phone_number"):
        raise RuntimeError("028_user_profile_phone failed: users.phone_number missing")


def verify_postgres(conn):
    row = conn.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'phone_number'
        """
    ).fetchone()
    if not row:
        raise RuntimeError("028_user_profile_phone failed: users.phone_number missing")


def verify_sqlite(conn):
    if not _has_column(conn, "users", "phone_number"):
        raise RuntimeError("028_user_profile_phone failed: users.phone_number missing")
