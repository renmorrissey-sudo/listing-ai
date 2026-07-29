"""Password reset tokens and password_set / session_version on users."""

VERSION = "015_password_reset_tokens"

SQLITE_USERS = [
    ("password_set", "INTEGER NOT NULL DEFAULT 1"),
    ("session_version", "INTEGER NOT NULL DEFAULT 1"),
]

PG_USERS = [
    ("password_set", "BOOLEAN NOT NULL DEFAULT TRUE"),
    ("session_version", "INTEGER NOT NULL DEFAULT 1"),
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
    for name, typ in SQLITE_USERS:
        if not _sqlite_has_column(conn, "users", name):
            conn.execute(f"ALTER TABLE users ADD COLUMN {name} {typ}")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            user_id INTEGER,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_password_reset_email ON password_reset_tokens(email)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_password_reset_expires ON password_reset_tokens(expires_at)"
    )


def upgrade_postgres(conn):
    for name, typ in PG_USERS:
        if not _postgres_has_column(conn, "users", name):
            conn.execute(f"ALTER TABLE users ADD COLUMN {name} {typ}")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id BIGSERIAL PRIMARY KEY,
            email TEXT NOT NULL,
            user_id BIGINT REFERENCES users(id),
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_password_reset_email ON password_reset_tokens(email)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_password_reset_expires ON password_reset_tokens(expires_at)"
    )
