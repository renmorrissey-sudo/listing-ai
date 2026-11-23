"""Ensure the mock calendar connection for Ren's staging tenant (100).

STAGING ONLY — this file lives on the topai-launch-readiness-release
branch and must NEVER be merged to main.

Why: the Phase III-4 live verification (§7) needs the mock calendar
provider active for Ren's staging tenant (user_id 2). The confirm
path resolves the tenant's calendar via
calendar_connections_db.resolve_calendar_provider, which returns
(None, None) unless a ``calendar_connections`` row exists — and the
mock provider has no OAuth flow and is never a user-facing connect
option (registry PROVIDER_ORDER is ["google"] only; the mock's
get_authorize_url raises NotImplementedError), so no UI path can
create the row. ``ensure_mock_connection`` is only called from tests.
Without this row every inbound booking request lands with
no_calendar_connected=1 and every confirm is refused — §7 steps 2-4
and 6 cannot run (step 5, the no-calendar case, is the only one
verifiable without it).

This migration creates the row by calling
calendar_connections_db.ensure_mock_connection(2), the same helper
the tests use. Guards: refused unless the mock transport is on
(config.HARNESS_MOCK_PROVIDERS) — the helper returns None and this
migration fails loudly, so the mock can never be connected in
production; fails loudly if user 2 is not Ren's staging account.
Idempotent: re-running upserts the same values. The row is written
on the helper's own committed connection; the postcondition check
below reads it back on the migration connection.

Numbering: 100, not 049. Main's latest migration is 048 and its
Phase III-5/III-6 work will most likely take 049+ next, so a
staging-only 049 would collide when main's 049 later lands in the
merged staging runner. 100 sits far above main's near-term numbering;
the runner applies by set membership, not version order, so the gap
is harmless. The staging runner lists this module immediately before
099 (after 048, which creates the calendar_connections table), so
099 stays last.
"""

VERSION = "100_ensure_mock_calendar_connection_for_ren_tenant"

USER_ID = 2
EMAIL = "ren.morrissey@gmail.com"


def upgrade_postgres(conn):
    row = conn.execute(
        "SELECT email FROM users WHERE id = %s" % USER_ID
    ).fetchone()
    if row is None:
        raise RuntimeError("100: user %s not found" % USER_ID)
    if row["email"] != EMAIL:
        raise RuntimeError(
            "100: user %s is not %s (found %s); refusing"
            % (USER_ID, EMAIL, row["email"])
        )
    from calendar_connections_db import ensure_mock_connection

    connection = ensure_mock_connection(USER_ID)
    if connection is None:
        raise RuntimeError(
            "100: ensure_mock_connection returned None "
            "(mock transport off — refusing to connect a mock calendar)"
        )
    # Inline postcondition check (the runner does not call verify()):
    # the helper commits on its own connection, so the row must be
    # visible on this connection.
    check = conn.execute(
        "SELECT id, provider, status FROM calendar_connections "
        "WHERE user_id = %s AND provider = 'mock'"
        % USER_ID
    ).fetchone()
    if check is None:
        raise RuntimeError(
            "100: no mock calendar_connections row for user %s after ensure"
            % USER_ID
        )
    if check["status"] != "connected":
        raise RuntimeError(
            "100: mock calendar row for user %s is not connected: status=%r"
            % (USER_ID, check["status"])
        )


def upgrade_sqlite(conn):
    # Staging-only data fix; test databases do not need it. No-op so the
    # local test suite gains no mock connection row.
    return None


def _verify_postgres(conn):
    row = conn.execute(
        "SELECT id, provider, status FROM calendar_connections "
        "WHERE user_id = %s AND provider = 'mock'"
        % USER_ID
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "100_ensure_mock_calendar_connection_for_ren_tenant failed: "
            "no mock calendar row for user %s" % USER_ID
        )
    if row["status"] != "connected":
        raise RuntimeError(
            "100_ensure_mock_calendar_connection_for_ren_tenant failed: "
            "mock calendar row for user %s is not connected: status=%r"
            % (USER_ID, row["status"])
        )


def verify(conn, dialect):
    if dialect == "postgresql":
        _verify_postgres(conn)


def verify_postgres(conn):
    _verify_postgres(conn)
