"""Move the demo lead to Ren's staging tenant (039).

STAGING ONLY — this file lives on the topai-launch-readiness-release
branch and must NEVER be merged to main.

Why: the Nov 18 staging demo injected an inbound SMS from Ren's own
number (+13038703107). The webhook accepted it (lead 9, message 22)
and the worker drafted a reply with approval 8 — but all of it landed
under tenant 1 (the original staging tenant, staging-admin@), because
the shared platform number +18888210810 routes by lead ownership and
Ren's number was filed as a lead under tenant 1 when the inbound
arrived. Ren's account (ren.morrissey@gmail.com) is a fresh tenant, so
its /harness showed 0 pending approvals and the demo stalled at the
approval step — even though the pipeline itself worked end to end.

This migration reassigns the demo-created lead 9 (and its messages)
to ren.morrissey@gmail.com's user, so a fresh inbound from Ren's
number routes to Ren's tenant by lead ownership and the new draft +
approval appear on Ren's /harness. Approval 8 stays under tenant 1 as
a stale staging artifact; the fresh injection creates a new approval
under Ren's tenant. Idempotent: re-running sets the same values.
Guards: fails loudly if Ren's user, lead 9, or a conflicting lead
(a lead with the same phone already under Ren's tenant) is missing.
"""

VERSION = "039_move_demo_lead_to_ren_tenant"

EMAIL = "ren.morrissey@gmail.com"
LEAD_ID = 9


def _ren_user_id(conn):
    row = conn.execute(
        "SELECT id FROM users WHERE email = '" + EMAIL + "'"
    ).fetchone()
    if row is None:
        raise RuntimeError("039: user not found: " + EMAIL)
    return row[0]


def _guard(conn, ren_id):
    lead = conn.execute(
        "SELECT phone_number FROM leads WHERE id = %s" % LEAD_ID
    ).fetchone()
    if lead is None:
        raise RuntimeError("039: lead %s not found" % LEAD_ID)
    phone = lead[0]
    clash = conn.execute(
        "SELECT id FROM leads WHERE user_id = %s AND phone_number = '%s'"
        % (ren_id, phone)
    ).fetchone()
    if clash is not None:
        raise RuntimeError(
            "039: lead %s already exists under Ren's tenant for %s" % (clash[0], phone)
        )


def upgrade_postgres(conn):
    ren_id = _ren_user_id(conn)
    _guard(conn, ren_id)
    conn.execute(
        "UPDATE leads SET user_id = %s WHERE id = %s" % (ren_id, LEAD_ID)
    )
    conn.execute(
        "UPDATE sms_messages SET user_id = %s WHERE lead_id = %s" % (ren_id, LEAD_ID)
    )


def upgrade_sqlite(conn):
    # Staging-only data fix; test databases do not need it. No-op so the
    # local test suite gains no real user row.
    return None


def _verify_postgres(conn):
    ren_id = _ren_user_id(conn)
    row = conn.execute(
        "SELECT user_id FROM leads WHERE id = %s" % LEAD_ID
    ).fetchone()
    if row is None or row[0] != ren_id:
        raise RuntimeError("039_move_demo_lead_to_ren_tenant failed: lead not moved")


def verify(conn, dialect):
    if dialect == "postgresql":
        _verify_postgres(conn)


def verify_postgres(conn):
    _verify_postgres(conn)
