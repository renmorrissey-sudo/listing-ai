"""Lead qualification fields + stored score (046).

Phase III-2 lead qualification state machine + scoring: four
structured qualification fields and a stored score cache on
``leads``. The state machine itself lives in
``harness/qualification.py`` and reads these columns; the score
cache is recomputed on every field write and on signal events, and
the lead-detail page always renders the on-read computation, so the
stored value can never mislead the agent.

Data safety
------------
Additive and forward-only. Six columns added to ``leads`` with
``IF NOT EXISTS`` / PRAGMA-guarded DDL (Postgres / SQLite twins).
No existing data is modified (all existing leads default to
``unknown`` / score 0), no seed rows. Postcondition checks run
INLINE in the upgrade functions (the runner does not call a
migration's own ``verify()``) so a failure rolls the migration
back. The Postgres checks only test ``row is not None`` — they
never index into a row, so they are safe against the
empty-Postgres test fake, which returns stub rows.

Rollback
--------
Additive. Rollback is a code-only revert — the columns stay,
unused, and no data is lost. No down-migration is run (the release
has no down-migrations by design).
"""

VERSION = "046_lead_qualification"

TABLE = "leads"

COLUMNS = (
    "qualification_budget",
    "qualification_timeline",
    "qualification_preapproval",
    "qualification_motivation",
    "qualification_score",
    "qualification_score_json",
)

# column -> (SQLite DDL fragment, Postgres DDL fragment)
COLUMN_DEFS = {
    "qualification_budget": (
        "TEXT NOT NULL DEFAULT 'unknown'",
        "TEXT NOT NULL DEFAULT 'unknown'",
    ),
    "qualification_timeline": (
        "TEXT NOT NULL DEFAULT 'unknown'",
        "TEXT NOT NULL DEFAULT 'unknown'",
    ),
    "qualification_preapproval": (
        "TEXT NOT NULL DEFAULT 'unknown'",
        "TEXT NOT NULL DEFAULT 'unknown'",
    ),
    "qualification_motivation": (
        "TEXT NOT NULL DEFAULT 'unknown'",
        "TEXT NOT NULL DEFAULT 'unknown'",
    ),
    "qualification_score": (
        "INTEGER NOT NULL DEFAULT 0",
        "INTEGER NOT NULL DEFAULT 0",
    ),
    "qualification_score_json": (
        "TEXT",
        "TEXT",
    ),
}


def _sqlite_has_column(conn, table, column):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    names = set()
    for row in rows:
        try:
            names.add(row["name"])
        except (KeyError, TypeError, IndexError):
            names.add(row[1])
    return column in names


def _postgres_has_column(conn, table, column):
    row = conn.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        """,
        (table, column),
    ).fetchone()
    return row is not None


def _check_columns(conn, dialect):
    """Inline postcondition: all six columns exist on ``leads``."""
    if dialect == "postgresql":
        for column in COLUMNS:
            if not _postgres_has_column(conn, TABLE, column):
                raise RuntimeError(
                    "046_lead_qualification failed: "
                    f"{TABLE}.{column} missing"
                )
        return
    present = set()
    rows = conn.execute(f"PRAGMA table_info({TABLE})").fetchall()
    for row in rows:
        try:
            present.add(row["name"])
        except (KeyError, TypeError, IndexError):
            present.add(row[1])
    missing = [c for c in COLUMNS if c not in present]
    if missing:
        raise RuntimeError(
            "046_lead_qualification failed: "
            f"{TABLE} missing columns: {', '.join(missing)}"
        )


def upgrade_sqlite(conn):
    for column in COLUMNS:
        if not _sqlite_has_column(conn, TABLE, column):
            conn.execute(
                f"ALTER TABLE {TABLE} ADD COLUMN {column} "
                f"{COLUMN_DEFS[column][0]}"
            )
    _check_columns(conn, "sqlite")


def upgrade_postgres(conn):
    for column in COLUMNS:
        if not _postgres_has_column(conn, TABLE, column):
            conn.execute(
                f"ALTER TABLE {TABLE} ADD COLUMN {column} "
                f"{COLUMN_DEFS[column][1]}"
            )
    _check_columns(conn, "postgresql")


def verify(conn, dialect):
    _check_columns(conn, dialect)


def verify_postgres(conn):
    _check_columns(conn, "postgresql")


def verify_sqlite(conn):
    _check_columns(conn, "sqlite")
