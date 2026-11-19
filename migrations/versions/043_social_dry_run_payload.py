"""Social dry-run payload column (043).

Phase A social testing: the staging-only dry-run layer
(``social_dry_run.py``) records the exact payload a provider WOULD
receive — URL, method, headers (secrets redacted), body — on the
``social_publications`` row for that attempt, with status ``'dry_run'``,
without calling any provider. The row doubles as the audit record
(user_id, generation, provider, connection, requested_at,
idempotency_key are already on the table), so no new table is needed.

Data safety
------------
Additive and forward-only. One nullable TEXT column,
``dry_run_payload_json``, added ``IF NOT EXISTS`` (Postgres) or after a
PRAGMA check (SQLite). Existing rows keep NULL in the column; no
existing data is modified. Postcondition checks run INLINE in the
upgrade functions (the runner does not call a migration's own
``verify()``) so a failure rolls the migration back. The Postgres
column check only tests ``row is not None`` — it never indexes into
the row, so it is safe against the empty-Postgres test fake, which
returns stub rows for ``information_schema.columns``.

Rollback
--------
Additive. Rollback is a code-only revert — the column stays, unused,
and no data is lost. No down-migration is run (the release has no
down-migrations by design).
"""

VERSION = "043_social_dry_run_payload"

TABLE = "social_publications"
COLUMN = "dry_run_payload_json"


def _columns_sqlite(conn, table):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    names = set()
    for row in rows:
        try:
            names.add(row["name"])
        except (KeyError, TypeError, IndexError):
            names.add(row[1])
    return names


def _has_column_postgres(conn, table, column):
    row = conn.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        """,
        (table, column),
    ).fetchone()
    return row is not None


def _check_column(conn, dialect):
    """Inline postcondition: the column exists. Raises so the runner
    rolls the migration back. The check only tests row existence, never
    indexes into the row."""
    if dialect == "postgresql":
        exists = _has_column_postgres(conn, TABLE, COLUMN)
    else:
        exists = COLUMN in _columns_sqlite(conn, TABLE)
    if not exists:
        raise RuntimeError(
            "043_social_dry_run_payload failed: "
            f"{TABLE}.{COLUMN} missing"
        )


def upgrade_sqlite(conn):
    if COLUMN not in _columns_sqlite(conn, TABLE):
        conn.execute(
            f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} TEXT"
        )
    _check_column(conn, "sqlite")


def upgrade_postgres(conn):
    conn.execute(
        f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {COLUMN} TEXT"
    )
    _check_column(conn, "postgresql")


def verify(conn, dialect):
    _check_column(conn, dialect)


def verify_postgres(conn):
    _check_column(conn, "postgresql")
