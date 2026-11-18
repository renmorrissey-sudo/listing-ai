"""Remove the duplicate demo lead 11 (tenant 1) so Ren's number has exactly one lead owner (041).

STAGING ONLY — this file lives on the topai-launch-readiness-release
branch and must NEVER be merged to main.

Why: the ren04 routing re-check (run before the staging-only Telnyx number
was set) created lead 11 under tenant 1 for Ren's number (+13038703107).
Lead 9 already holds that phone under Ren's tenant (repaired by migration
040), so the global phone lookup now returns two owners and the resolver
takes the ambiguous branch, which would pick tenant 1 (lead 11 is more
recent). This migration removes the duplicate so the lookup returns exactly
one owner (Ren's tenant), and bumps lead 9's updated_at so it is the most
recently active lead for the number.
"""

VERSION = "041_remove_duplicate_demo_lead_11"

EMAIL = "ren.morrissey@gmail.com"
PHONE = "+13038703107"
LEAD_ID = 9
STALE_LEAD_ID = 11


def _ren_user_id(conn):
    row = conn.execute(
        "SELECT id FROM users WHERE email = '" + EMAIL + "'"
    ).fetchone()
    if row is None:
        raise RuntimeError("041: user not found: " + EMAIL)
    return row["id"]


def _guard(conn):
    lead = conn.execute(
        "SELECT id FROM leads WHERE id = %s" % LEAD_ID
    ).fetchone()
    if lead is None:
        raise RuntimeError("041: lead %s not found" % LEAD_ID)


def upgrade_postgres(conn):
    ren_id = _ren_user_id(conn)
    _guard(conn)
    # Remove the accidental duplicate lead 11 and its messages.
    conn.execute(
        "DELETE FROM sms_messages WHERE lead_id = %s" % STALE_LEAD_ID
    )
    conn.execute(
        "DELETE FROM leads WHERE id = %s" % STALE_LEAD_ID
    )
    # Make lead 9 the most recently active lead for the number.
    conn.execute(
        "UPDATE leads SET updated_at = NOW() WHERE id = %s" % LEAD_ID
    )
    # Inline checks (the runner does not call verify() for non-baseline
    # migrations, so a failure here rolls the migration back).
    row = conn.execute(
        "SELECT user_id, phone_number FROM leads WHERE id = %s" % LEAD_ID
    ).fetchone()
    if row is None or row["user_id"] != ren_id or row["phone_number"] != PHONE:
        raise RuntimeError("041: lead %s not under Ren's tenant" % LEAD_ID)
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM leads WHERE phone_number = '%s'" % PHONE
    ).fetchone()
    if count is None or count["c"] != 1:
        raise RuntimeError("041: expected exactly 1 lead for the phone, found %s" % (count["c"] if count else None))
    stale = conn.execute(
        "SELECT id FROM leads WHERE id = %s" % STALE_LEAD_ID
    ).fetchone()
    if stale is not None:
        raise RuntimeError("041: stale lead %s still present" % STALE_LEAD_ID)


def upgrade_sqlite(conn):
    # Staging-only data fix; test databases do not need it. No-op so the
    # local test suite gains no real user row.
    return None


def verify(conn, dialect):
    if dialect == "postgresql":
        _verify_postgres(conn)


def _verify_postgres(conn):
    ren_id = _ren_user_id(conn)
    row = conn.execute(
        "SELECT user_id, phone_number FROM leads WHERE id = %s" % LEAD_ID
    ).fetchone()
    if row is None or row["user_id"] != ren_id or row["phone_number"] != PHONE:
        raise RuntimeError("041_remove_duplicate_demo_lead_11 failed: lead not repaired")


def verify_postgres(conn):
    _verify_postgres(conn)
