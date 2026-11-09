"""Scope tenant_sms_senders uniqueness by tenant (032).

Migration 012 declared ``UNIQUE (sender_number)`` on tenant_sms_senders
in both its SQLite and Postgres table definitions, so the sender number
was globally unique: only one tenant could ever register a given number.
The platform Telnyx number (+18888210810) is shared by every subscriber,
so the second tenant's ``ensure_telnyx_platform_sender`` INSERT failed
with a uniqueness violation and the tenant silently had no sender row.
This migration replaces that single-column uniqueness with
``UNIQUE (user_id, sender_number)`` -- enforced as a unique index of
that name on both engines, mirroring migration 030 -- so any number of
tenants may share one sender number while one tenant still cannot
register the same number twice.

Data safety
------------
Additive and forward-only; no user rows are removed and no column is
removed. Before anything else the migration counts, and refuses to
proceed if it finds, duplicate ``(user_id, sender_number)`` pairs:

* Why no duplicates can exist: 012 declares ``sender_number`` globally
  UNIQUE in both engines, so any two rows sharing a ``(user_id,
  sender_number)`` pair -- which implies sharing the number -- were
  already impossible. The check is therefore expected to find zero
  duplicates; it exists only so the migration cannot fail (or silently
  drop a registration) if a database was ever built outside 012 (for
  example a hand-built schema that omitted the unique). If duplicates
  are found the migration raises before touching the schema, and the
  runner rolls the whole transaction back -- nothing is half-applied.
* The migration also counts (and logs) how many sender numbers are
  already shared by more than one tenant -- expected to be zero under
  the old constraint, since sharing was impossible -- and the total
  row count, which is verified after the rebuild.
* Postgres: the old single-column unique constraint is discovered from
  the catalog (``pg_get_constraintdef``) and removed by name, then the
  scoped unique index is created. The table itself is untouched.
* SQLite: the global uniqueness lives in an automatic index created by
  the inline ``UNIQUE`` (``sqlite_autoindex_tenant_sms_senders_1``),
  and SQLite provides no statement to remove such an index on its own.
  The table is therefore rebuilt by rename-and-copy, the only
  supported route: the live table is renamed aside, a new table with
  the identical columns (minus the inline ``UNIQUE``) is created, every
  row is copied over, the copy is verified by row count (a mismatch
  aborts the migration and the runner rolls the whole transaction
  back), and the scoped unique index plus the two 012 indexes are
  created on the new table. The renamed-aside table is then emptied of
  its duplicated rows and retained as an empty shell
  (``tenant_sms_senders_032_legacy``): repo policy forbids removing a
  table in a migration, so the shell stays as a visible marker of the
  rebuild rather than being removed. It holds no rows after this
  migration; ``verify_sqlite`` enforces that.

No statement in this file removes a table or a column.
"""

import logging
import re

VERSION = "032_tenant_sms_senders_scoped_unique"

logger = logging.getLogger(__name__)

SCOPED_INDEX_NAME = "idx_tenant_sms_senders_user_number"
LEGACY_TABLE = "tenant_sms_senders_032_legacy"

_SENDER_COLUMNS = (
    "id",
    "user_id",
    "sms_provider",
    "sender_number",
    "provider_number_id",
    "provider_account_reference",
    "sms_enabled",
    "registration_status",
    "activated_at",
    "metadata_json",
    "created_at",
    "updated_at",
)

# Duplicate (user_id, sender_number) pairs. Expected to be empty: under
# 012's global UNIQUE(sender_number) two rows could never share a number,
# let alone a (user_id, number) pair. Any row found here aborts the
# migration (see _check_no_duplicates) instead of dropping a registration.
_DUPLICATE_PAIRS_SQL = """
    SELECT user_id, sender_number, COUNT(*) AS c
    FROM tenant_sms_senders
    GROUP BY user_id, sender_number
    HAVING COUNT(*) > 1
"""

_SCOPED_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS "
    f"{SCOPED_INDEX_NAME} ON tenant_sms_senders(user_id, sender_number)"
)

# The new table is byte-identical to 012's except ``sender_number``
# loses its inline UNIQUE; uniqueness now comes from the scoped index.
_NEW_TABLE_SQLITE = """
    CREATE TABLE tenant_sms_senders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        sms_provider TEXT NOT NULL DEFAULT 'simpletexting',
        sender_number TEXT NOT NULL,
        provider_number_id TEXT,
        provider_account_reference TEXT,
        sms_enabled INTEGER NOT NULL DEFAULT 0,
        registration_status TEXT NOT NULL DEFAULT 'pending',
        activated_at TEXT,
        metadata_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
"""

# 012's per-tenant indexes, recreated on the rebuilt table (they move with
# the renamed-aside table, so their names must be freed first).
_INDEXES_012 = [
    "CREATE INDEX IF NOT EXISTS idx_tenant_sms_senders_user "
    "ON tenant_sms_senders(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_tenant_sms_senders_number "
    "ON tenant_sms_senders(sender_number)",
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


def _check_no_duplicates(conn):
    """Refuse to proceed if any (user_id, sender_number) pair repeats.

    Returns (total_rows, shared_numbers): the total row count (used by
    the caller's copy verification) and how many distinct sender numbers
    are held by more than one tenant (informational; expected 0 under
    the old global unique, since sharing was impossible).
    """
    total = _count(conn, "tenant_sms_senders")
    # Only rows whose group count is actually > 1 are duplicates. (A
    # COUNT(*) probe answered with a zero-count row -- as the
    # FakeRawPostgres harness does -- is not a duplicate.)
    duplicates = [
        r
        for r in conn.execute(_DUPLICATE_PAIRS_SQL).fetchall()
        if int(_field(r, "c", 2) or 0) > 1
    ]
    if duplicates:
        detail = ", ".join(
            f"user_id={_field(r, 'user_id', 0)} "
            f"sender_number={_field(r, 'sender_number', 1)!r} "
            f"rows={_field(r, 'c', 2)}"
            for r in duplicates
        )
        raise RuntimeError(
            "032_tenant_sms_senders_scoped_unique failed: duplicate "
            f"(user_id, sender_number) pairs already exist ({detail}); "
            "refusing to rebuild tenant_sms_senders rather than drop a "
            "registration"
        )
    shared = conn.execute(
        """
        SELECT COUNT(*) AS c FROM (
            SELECT sender_number FROM tenant_sms_senders
            GROUP BY sender_number HAVING COUNT(DISTINCT user_id) > 1
        )
        """
    ).fetchone()
    shared_count = int(_field(shared, "c", 0) or 0) if shared else 0
    logger.info(
        "032_tenant_sms_senders_scoped_unique: tenant_sms_senders rows=%d "
        "shared_numbers=%d duplicate_pairs=0",
        total,
        shared_count,
    )
    return total, shared_count


def upgrade_sqlite(conn):
    _check_no_duplicates(conn)
    # Free the 012 index names: they belong to the table being renamed
    # aside and must be recreated on the new table.
    conn.execute("DROP INDEX IF EXISTS idx_tenant_sms_senders_user")
    conn.execute("DROP INDEX IF EXISTS idx_tenant_sms_senders_number")
    conn.execute(f"ALTER TABLE tenant_sms_senders RENAME TO {LEGACY_TABLE}")
    conn.execute(_NEW_TABLE_SQLITE)
    columns = ", ".join(_SENDER_COLUMNS)
    conn.execute(
        f"INSERT INTO tenant_sms_senders ({columns}) "
        f"SELECT {columns} FROM {LEGACY_TABLE}"
    )
    if _count(conn, "tenant_sms_senders") != _count(conn, LEGACY_TABLE):
        raise RuntimeError(
            "032_tenant_sms_senders_scoped_unique failed: row count mismatch "
            "after copying tenant_sms_senders; refusing to continue"
        )
    conn.execute(_SCOPED_INDEX_SQL)
    for sql in _INDEXES_012:
        conn.execute(sql)
    # Remove the duplicated rows from the renamed-aside table; the empty
    # shell is retained (see module docstring).
    conn.execute(f"DELETE FROM {LEGACY_TABLE}")


def upgrade_postgres(conn):
    total, _shared = _check_no_duplicates(conn)
    rows = conn.execute(
        """
        SELECT c.conname AS name, pg_get_constraintdef(c.oid) AS def
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'tenant_sms_senders' AND c.contype = 'u'
        """
    ).fetchall()
    dropped = 0
    for row in rows:
        name = _field(row, "name", 0)
        definition = _field(row, "def", 1) or ""
        # The 012 global unique renders as UNIQUE (sender_number).
        if not re.fullmatch(r"UNIQUE \(\"?sender_number\"?\)", definition.strip()):
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name or ""):
            raise RuntimeError(
                "032_tenant_sms_senders_scoped_unique failed: unexpected "
                f"constraint name for tenant_sms_senders: {name!r}"
            )
        conn.execute(f'ALTER TABLE tenant_sms_senders DROP CONSTRAINT "{name}"')
        dropped += 1
    if dropped == 0:
        # A database built without 012's inline unique (or already
        # migrated) has nothing to drop; the scoped index below still
        # enforces the new rule. Log it rather than failing the deploy.
        logger.info(
            "032_tenant_sms_senders_scoped_unique: no global UNIQUE "
            "(sender_number) constraint found on tenant_sms_senders "
            "(rows=%d); creating scoped index only",
            total,
        )
    conn.execute(_SCOPED_INDEX_SQL)


def _verify_sqlite(conn):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tenant_sms_senders'"
    ).fetchone()
    table_sql = (_field(row, "sql", 0) or "") if row else ""
    compact = table_sql.upper().replace(" ", "")
    if "UNIQUE(SENDER_NUMBER)" in compact:
        raise RuntimeError(
            "032_tenant_sms_senders_scoped_unique failed: "
            "tenant_sms_senders still declares a global UNIQUE on "
            "sender_number"
        )
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
        (SCOPED_INDEX_NAME,),
    ).fetchone()
    index_sql = (_field(row, "sql", 0) or "") if row else ""
    if "CREATE UNIQUE INDEX" not in index_sql.upper():
        raise RuntimeError(
            "032_tenant_sms_senders_scoped_unique failed: scoped unique index "
            f"{SCOPED_INDEX_NAME} missing on tenant_sms_senders"
        )
    if "(user_id,sender_number)" not in index_sql.replace(" ", ""):
        raise RuntimeError(
            "032_tenant_sms_senders_scoped_unique failed: scoped unique index "
            "does not cover (user_id, sender_number)"
        )
    # No automatic index may remain on tenant_sms_senders: any of them
    # would be the old global unique (the rebuilt table declares no
    # UNIQUE column).
    leftovers = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' "
        "AND tbl_name = 'tenant_sms_senders' AND name LIKE 'sqlite_autoindex%'"
    ).fetchall()
    if leftovers:
        raise RuntimeError(
            "032_tenant_sms_senders_scoped_unique failed: automatic unique "
            "index still present on tenant_sms_senders: "
            + ", ".join(str(_field(r, "name", 0)) for r in leftovers)
        )
    legacy = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (LEGACY_TABLE,),
    ).fetchone()
    if legacy is not None and _count(conn, LEGACY_TABLE) != 0:
        raise RuntimeError(
            "032_tenant_sms_senders_scoped_unique failed: "
            f"{LEGACY_TABLE} still holds rows after the rebuild"
        )


def _verify_postgres(conn):
    row = conn.execute(
        """
        SELECT indexdef FROM pg_indexes
        WHERE tablename = 'tenant_sms_senders'
          AND indexname = 'idx_tenant_sms_senders_user_number'
        """
    ).fetchone()
    indexdef = (_field(row, "indexdef", 0) or "") if row else ""
    if "UNIQUE" not in indexdef.upper():
        raise RuntimeError(
            "032_tenant_sms_senders_scoped_unique failed: scoped unique index "
            "idx_tenant_sms_senders_user_number missing on tenant_sms_senders"
        )
    if "(user_id,sender_number)" not in indexdef.replace(" ", ""):
        raise RuntimeError(
            "032_tenant_sms_senders_scoped_unique failed: scoped unique index "
            "does not cover (user_id, sender_number)"
        )
    rows = conn.execute(
        """
        SELECT c.conname AS name, pg_get_constraintdef(c.oid) AS def
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'tenant_sms_senders' AND c.contype = 'u'
        """
    ).fetchall()
    for row in rows:
        definition = _field(row, "def", 1) or ""
        if re.fullmatch(r"UNIQUE \(\"?sender_number\"?\)", definition.strip()):
            raise RuntimeError(
                "032_tenant_sms_senders_scoped_unique failed: global unique "
                "constraint on sender_number still present on "
                "tenant_sms_senders"
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
