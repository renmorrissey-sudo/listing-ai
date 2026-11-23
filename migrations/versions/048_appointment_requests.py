"""Appointment requests through the gate + calendar connections (048).

Phase III-4: inbound booking requests no longer auto-execute. A request
from a lead creates a pending ``harness_appointment_requests`` row; the
calendar event is created and the lead is told it is booked ONLY after
the agent confirms (one tap in the inbox / approval flow). Also adds
``calendar_connections`` (per-tenant OAuth, Fernet-encrypted tokens)
and ``calendar_oauth_states`` (short-lived CSRF state, mirroring the
social pattern).

Data safety
------------
Additive and forward-only. Three new tables + indexes, created
``IF NOT EXISTS`` (Postgres) or after PRAGMA checks (SQLite). No
existing table is altered; no existing data is modified; no seed rows.
Postcondition checks run INLINE in the upgrade functions (the runner
does not call a migration's own ``verify()``) so a failure rolls the
migration back. The Postgres checks only test ``row is not None`` —
they never index into a row, so they are safe against the
empty-Postgres test fake, which returns stub rows.

Rollback
--------
Additive. Rollback is a code-only revert — the tables stay, unused,
and no data is lost. No down-migration is run (the release has no
down-migrations by design).
"""

import logging

VERSION = "048_appointment_requests"

REQUESTS_TABLE = "harness_appointment_requests"
CONNECTIONS_TABLE = "calendar_connections"
STATES_TABLE = "calendar_oauth_states"

REQUESTS_COLUMNS = (
    "id",
    "user_id",
    "lead_id",
    "channel",
    "source_ref_type",
    "source_ref_id",
    "requested_start_at",
    "requested_end_at",
    "requested_window_json",
    "appointment_type",
    "raw_request_text",
    "status",
    "confirmed_appointment_id",
    "external_event_id",
    "calendar_provider",
    "confirm_text_message_id",
    "no_calendar_connected",
    "reschedules_appointment_id",
    "approval_id",
    "sync_status",
    "sync_error",
    "created_at",
    "updated_at",
)

CONNECTIONS_COLUMNS = (
    "id",
    "user_id",
    "provider",
    "external_account_id",
    "display_name",
    "access_token_encrypted",
    "refresh_token_encrypted",
    "token_expires_at",
    "scopes",
    "calendar_id",
    "status",
    "created_at",
    "updated_at",
)

STATES_COLUMNS = (
    "id",
    "state",
    "user_id",
    "provider",
    "code_verifier",
    "redirect_uri",
    "created_at",
    "expires_at",
    "consumed_at",
)

REQUESTS_INDEXES = (
    "idx_appt_req_user_lead_status",
    "idx_appt_req_user_status_created",
    "idx_appt_req_user_slot",
    "idx_appt_req_source",
)

CONNECTIONS_INDEXES = (
    "idx_cal_conn_user_provider",
)

STATES_INDEXES = (
    "idx_cal_oauth_states_expires",
)

SQLITE_REQUESTS_TABLE = """
CREATE TABLE IF NOT EXISTS harness_appointment_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    lead_id INTEGER NOT NULL,
    channel TEXT NOT NULL DEFAULT 'sms',
    source_ref_type TEXT,
    source_ref_id INTEGER,
    requested_start_at TEXT,
    requested_end_at TEXT,
    requested_window_json TEXT,
    appointment_type TEXT NOT NULL DEFAULT 'phone_call',
    raw_request_text TEXT,
    status TEXT NOT NULL DEFAULT 'requested',
    confirmed_appointment_id INTEGER,
    external_event_id TEXT,
    calendar_provider TEXT,
    confirm_text_message_id INTEGER,
    no_calendar_connected INTEGER NOT NULL DEFAULT 0,
    reschedules_appointment_id INTEGER,
    approval_id INTEGER,
    sync_status TEXT NOT NULL DEFAULT 'pending',
    sync_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

PG_REQUESTS_TABLE = """
CREATE TABLE IF NOT EXISTS harness_appointment_requests (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    lead_id BIGINT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'sms',
    source_ref_type TEXT,
    source_ref_id BIGINT,
    requested_start_at TEXT,
    requested_end_at TEXT,
    requested_window_json TEXT,
    appointment_type TEXT NOT NULL DEFAULT 'phone_call',
    raw_request_text TEXT,
    status TEXT NOT NULL DEFAULT 'requested',
    confirmed_appointment_id BIGINT,
    external_event_id TEXT,
    calendar_provider TEXT,
    confirm_text_message_id BIGINT,
    no_calendar_connected INTEGER NOT NULL DEFAULT 0,
    reschedules_appointment_id BIGINT,
    approval_id BIGINT,
    sync_status TEXT NOT NULL DEFAULT 'pending',
    sync_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

SQLITE_CONNECTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS calendar_connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    external_account_id TEXT NOT NULL,
    display_name TEXT,
    access_token_encrypted TEXT,
    refresh_token_encrypted TEXT,
    token_expires_at TEXT,
    scopes TEXT,
    calendar_id TEXT,
    status TEXT NOT NULL DEFAULT 'connected',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, provider, external_account_id)
)
"""

PG_CONNECTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS calendar_connections (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    provider TEXT NOT NULL,
    external_account_id TEXT NOT NULL,
    display_name TEXT,
    access_token_encrypted TEXT,
    refresh_token_encrypted TEXT,
    token_expires_at TEXT,
    scopes TEXT,
    calendar_id TEXT,
    status TEXT NOT NULL DEFAULT 'connected',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, provider, external_account_id)
)
"""

SQLITE_STATES_TABLE = """
CREATE TABLE IF NOT EXISTS calendar_oauth_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    state TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    code_verifier TEXT,
    redirect_uri TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
)
"""

PG_STATES_TABLE = """
CREATE TABLE IF NOT EXISTS calendar_oauth_states (
    id BIGSERIAL PRIMARY KEY,
    state TEXT NOT NULL UNIQUE,
    user_id BIGINT NOT NULL,
    provider TEXT NOT NULL,
    code_verifier TEXT,
    redirect_uri TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
)
"""

SQLITE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_appt_req_user_lead_status "
    "ON harness_appointment_requests(user_id, lead_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_appt_req_user_status_created "
    "ON harness_appointment_requests(user_id, status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_appt_req_user_slot "
    "ON harness_appointment_requests(user_id, requested_start_at, status)",
    "CREATE INDEX IF NOT EXISTS idx_appt_req_source "
    "ON harness_appointment_requests(user_id, source_ref_type, source_ref_id)",
    "CREATE INDEX IF NOT EXISTS idx_cal_conn_user_provider "
    "ON calendar_connections(user_id, provider, status)",
    "CREATE INDEX IF NOT EXISTS idx_cal_oauth_states_expires "
    "ON calendar_oauth_states(expires_at)",
)

PG_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_appt_req_user_lead_status "
    "ON harness_appointment_requests(user_id, lead_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_appt_req_user_status_created "
    "ON harness_appointment_requests(user_id, status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_appt_req_user_slot "
    "ON harness_appointment_requests(user_id, requested_start_at, status)",
    "CREATE INDEX IF NOT EXISTS idx_appt_req_source "
    "ON harness_appointment_requests(user_id, source_ref_type, source_ref_id)",
    "CREATE INDEX IF NOT EXISTS idx_cal_conn_user_provider "
    "ON calendar_connections(user_id, provider, status)",
    "CREATE INDEX IF NOT EXISTS idx_cal_oauth_states_expires "
    "ON calendar_oauth_states(expires_at)",
)

logger = logging.getLogger(__name__)


def _columns_sqlite(conn, table):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    names = set()
    for row in rows:
        try:
            names.add(row["name"])
        except (KeyError, TypeError, IndexError):
            names.add(row[1])
    return names


def _indexes_sqlite(conn, table):
    rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
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


def _has_index_postgres(conn, table, index):
    row = conn.execute(
        """
        SELECT 1 FROM pg_indexes
        WHERE tablename = %s AND indexname = %s
        """,
        (table, index),
    ).fetchone()
    return row is not None


def _check_table(conn, dialect):
    """Inline postcondition: tables + columns exist (and indexes on
    SQLite). Raises so the runner rolls the migration back.

    The Postgres index check is intentionally NOT inline: the
    empty-Postgres test fake stubs ``information_schema.columns`` but
    not ``pg_indexes``, and the runner never calls a migration's own
    ``verify()`` — so, following the 030/045/047 precedent, Postgres
    index verification lives in :func:`verify_postgres`, not in the
    upgrade path. The Postgres checks only test row existence, never
    index into a row.
    """
    tables = (
        (REQUESTS_TABLE, REQUESTS_COLUMNS),
        (CONNECTIONS_TABLE, CONNECTIONS_COLUMNS),
        (STATES_TABLE, STATES_COLUMNS),
    )
    if dialect == "postgresql":
        for table, columns in tables:
            for column in columns:
                if not _has_column_postgres(conn, table, column):
                    raise RuntimeError(
                        "048_appointment_requests failed: "
                        f"{table}.{column} missing"
                    )
        return
    index_map = {
        REQUESTS_TABLE: REQUESTS_INDEXES,
        CONNECTIONS_TABLE: CONNECTIONS_INDEXES,
        STATES_TABLE: STATES_INDEXES,
    }
    for table, columns in tables:
        present = _columns_sqlite(conn, table)
        missing = [c for c in columns if c not in present]
        if missing:
            raise RuntimeError(
                "048_appointment_requests failed: "
                f"{table} missing columns: {', '.join(missing)}"
            )
        indexes = _indexes_sqlite(conn, table)
        missing_indexes = [i for i in index_map[table] if i not in indexes]
        if missing_indexes:
            raise RuntimeError(
                "048_appointment_requests failed: "
                f"{table} missing indexes: {', '.join(missing_indexes)}"
            )


def _check_indexes_postgres(conn):
    """Postgres index postcondition (verify-time only, see above)."""
    index_map = {
        REQUESTS_TABLE: REQUESTS_INDEXES,
        CONNECTIONS_TABLE: CONNECTIONS_INDEXES,
        STATES_TABLE: STATES_INDEXES,
    }
    for table, indexes in index_map.items():
        for index in indexes:
            if not _has_index_postgres(conn, table, index):
                raise RuntimeError(
                    "048_appointment_requests failed: "
                    f"index {index} missing"
                )


def upgrade_sqlite(conn):
    conn.execute(SQLITE_REQUESTS_TABLE)
    conn.execute(SQLITE_CONNECTIONS_TABLE)
    conn.execute(SQLITE_STATES_TABLE)
    for statement in SQLITE_INDEXES:
        conn.execute(statement)
    logger.info(
        "048_appointment_requests: tables created "
        "(harness_appointment_requests, calendar_connections, calendar_oauth_states)"
    )
    _check_table(conn, "sqlite")


def upgrade_postgres(conn):
    conn.execute(PG_REQUESTS_TABLE)
    conn.execute(PG_CONNECTIONS_TABLE)
    conn.execute(PG_STATES_TABLE)
    for statement in PG_INDEXES:
        conn.execute(statement)
    logger.info(
        "048_appointment_requests: tables created "
        "(harness_appointment_requests, calendar_connections, calendar_oauth_states)"
    )
    _check_table(conn, "postgresql")


def verify(conn, dialect):
    _check_table(conn, dialect)
    if dialect == "postgresql":
        _check_indexes_postgres(conn)


def verify_postgres(conn):
    _check_table(conn, "postgresql")
    _check_indexes_postgres(conn)


def verify_sqlite(conn):
    _check_table(conn, "sqlite")
