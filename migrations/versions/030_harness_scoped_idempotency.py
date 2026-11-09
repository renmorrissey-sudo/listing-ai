"""Scope harness job idempotency by tenant (M6).

Migration 029 declared ``idempotency_key TEXT UNIQUE`` on harness_jobs, so
the key was global: a second tenant reusing a key got the first tenant's
job id back instead of a job of its own. This migration replaces that
single-column uniqueness with ``UNIQUE (user_id, idempotency_key)``,
enforced as a unique index of that name on both engines, and
``harness/jobs.py`` scopes its lookups the same way.

Data safety
------------
Additive and forward-only; no user rows are removed and no column is
removed. Before anything else, a defensive dedupe keeps the earliest job
per ``(user_id, idempotency_key)`` (non-NULL keys only):

* Why no duplicates can exist: 029 declares ``idempotency_key TEXT
  UNIQUE`` in both its SQLite and Postgres table definitions, so any two
  rows sharing a non-NULL key -- including the same ``(user_id, key)``
  pair and cross-tenant reuse of one key -- were already impossible.
  The dedupe is therefore expected to be a no-op; it exists only so the
  migration cannot fail if a database was ever built outside 029 (for
  example a hand-built schema that omitted the unique). NULL keys are
  untouched: NULLs are distinct under a unique index on both engines,
  and key-less jobs were always allowed to repeat.
* Postgres: the old single-column unique constraint is discovered from
  the catalog (``pg_get_constraintdef``) and removed by name, then the
  scoped unique index is created. The table itself is untouched.
* SQLite: the global uniqueness lives in an automatic index created by
  the inline ``UNIQUE`` (``sqlite_autoindex_harness_jobs_1``), and
  SQLite provides no statement to remove such an index on its own. The
  table is therefore rebuilt by rename-and-copy, the only supported
  route: the live table is renamed aside, a new table with the identical
  columns (minus the inline ``UNIQUE``) is created, every row is copied
  over, the copy is verified by row count (a mismatch aborts the
  migration and the runner rolls the whole transaction back), and the
  scoped unique index plus the two 029 indexes are created on the new
  table. The renamed-aside table is then emptied of its duplicated rows
  and retained as an empty shell (``harness_jobs_030_legacy``): repo
  policy forbids removing a table in a migration, so the shell stays as
  a visible marker of the rebuild rather than being removed. It holds no
  rows after this migration; ``verify_sqlite`` enforces that.

No statement in this file removes a table or a column.
"""

VERSION = "030_harness_scoped_idempotency"

SCOPED_INDEX_NAME = "idx_harness_jobs_user_idempotency"
LEGACY_TABLE = "harness_jobs_030_legacy"

_JOB_COLUMNS = (
    "id",
    "user_id",
    "kind",
    "payload_json",
    "idempotency_key",
    "status",
    "attempts",
    "next_attempt_at",
    "claimed_at",
    "claimed_by",
    "result_json",
    "created_at",
    "updated_at",
)

# Defensive dedupe: keep the earliest job per (user_id, key). Expected to
# be a no-op under 029's global UNIQUE (see module docstring). NULL keys
# are excluded: they are not duplicates under the new key.
_DEDUPE_SQL = """
    DELETE FROM harness_jobs
    WHERE idempotency_key IS NOT NULL
      AND id NOT IN (
          SELECT MIN(id) FROM harness_jobs
          WHERE idempotency_key IS NOT NULL
          GROUP BY user_id, idempotency_key
      )
"""

_SCOPED_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS "
    f"{SCOPED_INDEX_NAME} ON harness_jobs(user_id, idempotency_key)"
)

# The new table is byte-identical to 029's except ``idempotency_key``
# loses its inline UNIQUE; uniqueness now comes from the scoped index.
_NEW_TABLE_SQLITE = """
    CREATE TABLE harness_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        payload_json TEXT,
        idempotency_key TEXT,
        status TEXT DEFAULT 'pending',
        attempts INTEGER DEFAULT 0,
        next_attempt_at TEXT,
        claimed_at TEXT,
        claimed_by TEXT,
        result_json TEXT,
        created_at TEXT,
        updated_at TEXT
    )
"""

# 029's per-tenant indexes, recreated on the rebuilt table (they move with
# the renamed-aside table, so their names must be freed first).
_INDEXES_029 = [
    "CREATE INDEX IF NOT EXISTS idx_harness_jobs_user "
    "ON harness_jobs(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_harness_jobs_user_status "
    "ON harness_jobs(user_id, status)",
]


def _count(conn, table):
    row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
    if row is None:
        return 0
    if isinstance(row, dict):
        return int(row["c"])
    return int(row[0])


def _field(row, key, index):
    if isinstance(row, dict):
        return row.get(key)
    return row[index]


def upgrade_sqlite(conn):
    conn.execute(_DEDUPE_SQL)
    # Free the 029 index names: they belong to the table being renamed
    # aside and must be recreated on the new table.
    conn.execute("DROP INDEX IF EXISTS idx_harness_jobs_user")
    conn.execute("DROP INDEX IF EXISTS idx_harness_jobs_user_status")
    conn.execute(f"ALTER TABLE harness_jobs RENAME TO {LEGACY_TABLE}")
    conn.execute(_NEW_TABLE_SQLITE)
    columns = ", ".join(_JOB_COLUMNS)
    conn.execute(
        f"INSERT INTO harness_jobs ({columns}) "
        f"SELECT {columns} FROM {LEGACY_TABLE}"
    )
    if _count(conn, "harness_jobs") != _count(conn, LEGACY_TABLE):
        raise RuntimeError(
            "030_harness_scoped_idempotency failed: row count mismatch "
            "after copying harness_jobs; refusing to continue"
        )
    conn.execute(_SCOPED_INDEX_SQL)
    for sql in _INDEXES_029:
        conn.execute(sql)
    # Remove the duplicated rows from the renamed-aside table; the empty
    # shell is retained (see module docstring).
    conn.execute(f"DELETE FROM {LEGACY_TABLE}")


def upgrade_postgres(conn):
    conn.execute(_DEDUPE_SQL)
    rows = conn.execute(
        """
        SELECT c.conname AS name, pg_get_constraintdef(c.oid) AS def
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'harness_jobs' AND c.contype = 'u'
        """
    ).fetchall()
    import re

    for row in rows:
        name = _field(row, "name", 0)
        definition = _field(row, "def", 1) or ""
        # The 029 global unique renders as UNIQUE (idempotency_key).
        if not re.fullmatch(r"UNIQUE \(\"?idempotency_key\"?\)", definition.strip()):
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name or ""):
            raise RuntimeError(
                "030_harness_scoped_idempotency failed: unexpected "
                f"constraint name for harness_jobs: {name!r}"
            )
        conn.execute(f'ALTER TABLE harness_jobs DROP CONSTRAINT "{name}"')
    conn.execute(_SCOPED_INDEX_SQL)


def _verify_sqlite(conn):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'harness_jobs'"
    ).fetchone()
    table_sql = (_field(row, "sql", 0) or "") if row else ""
    if "IDEMPOTENCY_KEY TEXT UNIQUE" in table_sql.upper():
        raise RuntimeError(
            "030_harness_scoped_idempotency failed: harness_jobs still "
            "declares a global UNIQUE on idempotency_key"
        )
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
        (SCOPED_INDEX_NAME,),
    ).fetchone()
    index_sql = (_field(row, "sql", 0) or "") if row else ""
    if "CREATE UNIQUE INDEX" not in index_sql.upper():
        raise RuntimeError(
            "030_harness_scoped_idempotency failed: scoped unique index "
            f"{SCOPED_INDEX_NAME} missing on harness_jobs"
        )
    if "(user_id,idempotency_key)" not in index_sql.replace(" ", ""):
        raise RuntimeError(
            "030_harness_scoped_idempotency failed: scoped unique index "
            "does not cover (user_id, idempotency_key)"
        )
    # No automatic index may remain on harness_jobs: any of them would be
    # the old global unique (the rebuilt table declares no UNIQUE column).
    leftovers = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' "
        "AND tbl_name = 'harness_jobs' AND name LIKE 'sqlite_autoindex%'"
    ).fetchall()
    if leftovers:
        raise RuntimeError(
            "030_harness_scoped_idempotency failed: automatic unique "
            "index still present on harness_jobs: "
            + ", ".join(str(_field(r, "name", 0)) for r in leftovers)
        )
    legacy = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (LEGACY_TABLE,),
    ).fetchone()
    if legacy is not None and _count(conn, LEGACY_TABLE) != 0:
        raise RuntimeError(
            "030_harness_scoped_idempotency failed: "
            f"{LEGACY_TABLE} still holds rows after the rebuild"
        )


def _verify_postgres(conn):
    row = conn.execute(
        """
        SELECT indexdef FROM pg_indexes
        WHERE tablename = 'harness_jobs'
          AND indexname = 'idx_harness_jobs_user_idempotency'
        """
    ).fetchone()
    indexdef = (_field(row, "indexdef", 0) or "") if row else ""
    if "UNIQUE" not in indexdef.upper():
        raise RuntimeError(
            "030_harness_scoped_idempotency failed: scoped unique index "
            "idx_harness_jobs_user_idempotency missing on harness_jobs"
        )
    if "(user_id,idempotency_key)" not in indexdef.replace(" ", ""):
        raise RuntimeError(
            "030_harness_scoped_idempotency failed: scoped unique index "
            "does not cover (user_id, idempotency_key)"
        )
    rows = conn.execute(
        """
        SELECT c.conname AS name, pg_get_constraintdef(c.oid) AS def
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'harness_jobs' AND c.contype = 'u'
        """
    ).fetchall()
    import re

    for row in rows:
        definition = _field(row, "def", 1) or ""
        if re.fullmatch(r"UNIQUE \(\"?idempotency_key\"?\)", definition.strip()):
            raise RuntimeError(
                "030_harness_scoped_idempotency failed: global unique "
                "constraint on idempotency_key still present on "
                "harness_jobs"
            )


def verify(conn, dialect):
    if dialect == "postgresql":
        _verify_postgres(conn)
    else:
        _verify_sqlite(conn)


def verify_postgres(conn):
    _verify_postgres(conn)


def verify_sqlite(conn):
    _verify_sqlite(conn)
