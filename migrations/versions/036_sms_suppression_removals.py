"""Governed suppression-removal requests (036).

Phase II Batch 1, item 5: the shared ``sms_suppression_list`` (migration
012) has no removal path at all — a number that lands on it (STOP
keyword, opt-out) can never come off by any supported means. This
migration adds a request table so removal is human-reviewed and
approval-gated instead of a self-service delete:

* A tenant requests removal of one of their own suppressed numbers
  with a written reason (``tenant_sms_db.request_suppression_removal``).
* A harness admin (``HARNESS_ADMIN_EMAILS`` allowlist) approves or
  rejects it (``tenant_sms_db.review_suppression_removal``). The
  reviewer must differ from the requester (two-person rule); a
  request can only be reviewed while pending.
* On approval the suppression row is deleted by
  ``tenant_sms_db.remove_suppression`` and every step is written to
  the SMS audit trail (``suppression_removal_requested`` /
  ``suppression_removal_approved`` / ``suppression_removed`` /
  ``suppression_removal_rejected``). START never clears suppression —
  that rule is unchanged.

Data safety
------------
Additive and forward-only. One new table, created ``IF NOT EXISTS``,
plus one index. No existing rows are modified, no column or table is
removed. Idempotent: the upgrade functions check first and no-op when
the table/index already exists.

Rollback
--------
Additive. Rollback is a code-only revert — the table stays, unused,
and no data is lost. No down-migration is run (the release has no
down-migrations by design).
"""

VERSION = "036_sms_suppression_removals"

TABLE = "sms_suppression_removals"
INDEX = "idx_sms_suppression_removals_user_status"


def _table_exists_sqlite(conn, table):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _table_exists_postgres(conn, table):
    row = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        (table,),
    ).fetchone()
    return row is not None


def upgrade_sqlite(conn):
    if not _table_exists_sqlite(conn, TABLE):
        conn.execute(
            f"""
            CREATE TABLE {TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                phone_number TEXT NOT NULL,
                reason TEXT NOT NULL,
                requested_by INTEGER NOT NULL,
                requested_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                reviewed_by INTEGER,
                reviewed_at TEXT,
                review_note TEXT
            )
            """
        )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {INDEX}
        ON {TABLE}(user_id, status)
        """
    )


def upgrade_postgres(conn):
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            phone_number TEXT NOT NULL,
            reason TEXT NOT NULL,
            requested_by BIGINT NOT NULL,
            requested_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            reviewed_by BIGINT,
            reviewed_at TEXT,
            review_note TEXT
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {INDEX}
        ON {TABLE}(user_id, status)
        """
    )


def _verify_sqlite(conn):
    if not _table_exists_sqlite(conn, TABLE):
        raise RuntimeError(
            "036_sms_suppression_removals failed: "
            f"{TABLE} missing"
        )


def _verify_postgres(conn):
    if not _table_exists_postgres(conn, TABLE):
        raise RuntimeError(
            "036_sms_suppression_removals failed: "
            f"{TABLE} missing"
        )


def verify(conn, dialect):
    if dialect == "postgresql":
        _verify_postgres(conn)
    else:
        _verify_sqlite(conn)


def verify_postgres(conn):
    _verify_postgres(conn)
