"""Add a correlation id column to harness_jobs (L4).

Every harness job gets a ``correlation_id`` (uuid hex) at enqueue time so
one identifier can be followed through the whole chain: inbound webhook
-> job -> policy decision -> audit row -> provider execution log. Before
this migration the audit rows carried only a job id (or approval id) as
the correlation value, so a single inbound event could not be traced
across surfaces.

Data safety
------------
Additive and forward-only. ``ALTER TABLE ... ADD COLUMN`` on both
engines; no rows are modified, no column or table is removed, no index
is dropped. The column is nullable: jobs created before this migration
simply have ``correlation_id IS NULL`` (new jobs always carry one, since
``enqueue_job`` generates it when the caller does not supply it).
Idempotent: both upgrade functions check for the column first and
no-op when it already exists.
"""

VERSION = "031_harness_job_correlation"


def _has_column_sqlite(conn):
    rows = conn.execute("PRAGMA table_info(harness_jobs)").fetchall()
    names = set()
    for row in rows:
        try:
            names.add(row["name"])
        except (KeyError, TypeError, IndexError):
            names.add(row[1])
    return "correlation_id" in names


def _has_column_postgres(conn):
    row = conn.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'harness_jobs' AND column_name = 'correlation_id'
        """
    ).fetchone()
    return row is not None


def upgrade_sqlite(conn):
    if not _has_column_sqlite(conn):
        conn.execute("ALTER TABLE harness_jobs ADD COLUMN correlation_id TEXT")


def upgrade_postgres(conn):
    conn.execute(
        "ALTER TABLE harness_jobs ADD COLUMN IF NOT EXISTS correlation_id TEXT"
    )


def _verify_sqlite(conn):
    if not _has_column_sqlite(conn):
        raise RuntimeError(
            "031_harness_job_correlation failed: harness_jobs.correlation_id missing"
        )


def _verify_postgres(conn):
    if not _has_column_postgres(conn):
        raise RuntimeError(
            "031_harness_job_correlation failed: harness_jobs.correlation_id missing"
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
