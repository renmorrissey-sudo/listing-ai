"""Repair the demo lead's routing so inbound SMS from Ren's number reaches Ren's staging tenant (040).

STAGING ONLY — this file lives on the topai-launch-readiness-release
branch and must NEVER be merged to main.

Why: migration 039 moved lead 9 to ren.morrissey@gmail.com and the worker
applied it, but inbound SMS from Ren's number (+13038703107) still routes to
tenant 1. The third diagnostic injection (ren03) matched lead 10 under tenant 1
(created=0) with no SMS_ROUTING_AMBIGUOUS log line, which proves the global
phone lookup (list_lead_owners_by_phone) does not find lead 9 — lead 9's
phone_number is not the E.164 value the lookup matches on (or is NULL), while
lead 10 (created by the second injection under tenant 1) is findable. So the
resolver falls back to the single sender row (tenant 1) and every inbound
creates/matches a lead there instead of under Ren's tenant.

This migration repairs the demo data so exactly one lead exists for Ren's
number, under Ren's tenant, with the correct phone:
  1. Set lead 9's user_id to Ren's user and its phone_number to +13038703107
     (the E.164 value the lookup matches), and bump updated_at so it is the
     most recently active lead for the number.
  2. Delete the accidental duplicate lead 10 (created under tenant 1 by the
     routing bug) and its sms_messages, so the global lookup returns exactly
     one owner (Ren's tenant) and the ambiguous-owner branch cannot pick
     tenant 1. Lead 10's drafts/approvals under tenant 1 stay as stale
     staging artifacts (like approval 8); they are not deleted.
Guards: fails loudly if Ren's user or lead 9 is missing. The phone repair is
idempotent; the delete is a no-op if lead 10 is already gone. Inline checks
after the writes verify lead 9 is under Ren's tenant with the right phone,
matches the lookup prefilter, and lead 10 is gone — a failure rolls the
migration back.
"""

VERSION = "040_repair_demo_lead_routing"

EMAIL = "ren.morrissey@gmail.com"
PHONE = "+13038703107"
LEAD_ID = 9
STALE_LEAD_ID = 10


def _ren_user_id(conn):
    row = conn.execute(
        "SELECT id FROM users WHERE email = '" + EMAIL + "'"
    ).fetchone()
    if row is None:
        raise RuntimeError("040: user not found: " + EMAIL)
    return row["id"]


def _guard(conn):
    lead = conn.execute(
        "SELECT id FROM leads WHERE id = %s" % LEAD_ID
    ).fetchone()
    if lead is None:
        raise RuntimeError("040: lead %s not found" % LEAD_ID)


def upgrade_postgres(conn):
    ren_id = _ren_user_id(conn)
    _guard(conn)
    # Repair lead 9: correct tenant, correct E.164 phone, most recent.
    conn.execute(
        "UPDATE leads SET user_id = %s, phone_number = '%s', "
        "updated_at = NOW() WHERE id = %s" % (ren_id, PHONE, LEAD_ID)
    )
    conn.execute(
        "UPDATE sms_messages SET user_id = %s WHERE lead_id = %s" % (ren_id, LEAD_ID)
    )
    # Remove the accidental duplicate lead 10 and its messages.
    conn.execute(
        "DELETE FROM sms_messages WHERE lead_id = %s" % STALE_LEAD_ID
    )
    conn.execute(
        "DELETE FROM leads WHERE id = %s" % STALE_LEAD_ID
    )
    # Inline checks (the runner does not call verify() for non-baseline
    # migrations, so a failure here rolls the migration back).
    row = conn.execute(
        "SELECT user_id, phone_number FROM leads WHERE id = %s" % LEAD_ID
    ).fetchone()
    if row is None or row["user_id"] != ren_id or row["phone_number"] != PHONE:
        raise RuntimeError("040: lead %s not repaired to Ren's tenant" % LEAD_ID)
    prefilter = conn.execute(
        "SELECT id FROM leads WHERE phone_number LIKE '%3107' AND id = %s" % LEAD_ID
    ).fetchone()
    if prefilter is None:
        raise RuntimeError("040: lead %s does not match the lookup prefilter" % LEAD_ID)
    stale = conn.execute(
        "SELECT id FROM leads WHERE id = %s" % STALE_LEAD_ID
    ).fetchone()
    if stale is not None:
        raise RuntimeError("040: stale lead %s still present" % STALE_LEAD_ID)


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
        raise RuntimeError("040_repair_demo_lead_routing failed: lead not repaired")


def verify_postgres(conn):
    _verify_postgres(conn)
