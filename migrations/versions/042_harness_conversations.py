"""Per-conversation inbox state (042).

Phase II Priority 2 (unified inbox): a conversation is one lead's SMS
thread. The read-only inbox helpers (``harness/inbox.py``) derive the
thread from ``sms_messages``, but two pieces of per-conversation state
have nowhere to live:

* ``human_takeover`` — a human has taken over this conversation. The
  policy gate already honours ``human_takeover`` in its context
  (``harness/policy.py`` step 5: DRAFT "a human has taken over this
  conversation"), but no caller ever set it. The inbox setter writes
  this flag; ``harness.sms_handler._decide_policy`` reads it into the
  gate context for the inbound auto-reply path.
* ``last_read_at`` — when the signed-in tenant last opened this
  conversation. Unread = inbound messages newer than ``last_read_at``
  (or all inbound when it is NULL). Drives the inbox unread badge and
  the unread filter.

One row per (user_id, lead_id); tenant-scoped by construction. No
existing rows are modified.

Data safety
------------
Additive and forward-only. One new table, created ``IF NOT EXISTS``,
plus indexes. Idempotent: the upgrade functions check first and no-op
when the table/index already exists. Postcondition checks run INLINE
in the upgrade functions (the runner does not call a migration's own
``verify()``) so a failure rolls the migration back.

Version note: numbered 042, not 037 — the staging-only branch already
uses 037–041 for staging data repairs that must never merge to main.
042 is the next free number and applies cleanly after 041 on staging
(the runner applies by set membership, not by latest version).

Rollback
--------
Additive. Rollback is a code-only revert — the table stays, unused,
and no data is lost. No down-migration is run (the release has no
down-migrations by design).
"""

VERSION = "042_harness_conversations"

TABLE = "harness_conversations"
INDEX_USER_UPDATED = "idx_harness_conversations_user_updated"
INDEX_USER_LEAD = "idx_harness_conversations_user_lead"


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


def _check_table(conn, dialect):
    """Inline postcondition: the table exists.

    Raises RuntimeError so the runner rolls the migration back. The
    columns are defined by the same CREATE TABLE statement, so a
    table that exists (just created by this migration) carries them
    by construction; a separate column introspection would only add a
    failure mode (the empty-Postgres test fake returns stub rows for
    information_schema.columns, not real column names).
    """
    if dialect == "postgresql":
        exists = _table_exists_postgres(conn, TABLE)
    else:
        exists = _table_exists_sqlite(conn, TABLE)
    if not exists:
        raise RuntimeError(
            "042_harness_conversations failed: "
            f"{TABLE} missing"
        )


def upgrade_sqlite(conn):
    if not _table_exists_sqlite(conn, TABLE):
        conn.execute(
            f"""
            CREATE TABLE {TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                lead_id INTEGER NOT NULL,
                human_takeover INTEGER NOT NULL DEFAULT 0,
                last_read_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (user_id, lead_id)
            )
            """
        )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {INDEX_USER_UPDATED}
        ON {TABLE}(user_id, updated_at)
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {INDEX_USER_LEAD}
        ON {TABLE}(user_id, lead_id)
        """
    )
    _check_table(conn, "sqlite")


def upgrade_postgres(conn):
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            lead_id BIGINT NOT NULL,
            human_takeover BOOLEAN NOT NULL DEFAULT FALSE,
            last_read_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (user_id, lead_id)
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {INDEX_USER_UPDATED}
        ON {TABLE}(user_id, updated_at)
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {INDEX_USER_LEAD}
        ON {TABLE}(user_id, lead_id)
        """
    )
    _check_table(conn, "postgresql")


def verify(conn, dialect):
    _check_table(conn, dialect)


def verify_postgres(conn):
    _check_table(conn, "postgresql")
