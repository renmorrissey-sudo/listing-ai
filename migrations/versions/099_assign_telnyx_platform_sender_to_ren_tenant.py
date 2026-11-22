"""Assign the Telnyx platform sender to Ren's staging tenant (099).

STAGING ONLY — this file lives on the topai-launch-readiness-release
branch and must NEVER be merged to main.

Why: the Phase III-1 follow_up_sms approval-execute path calls
send_authorized_sms, which calls require_tenant_sender(user_id)
(sms_outbound.py:349). Ren's staging tenant (user_id 2) has no
tenant_sms_senders row — only tenant 1 holds a sender row for the
platform number — so every follow_up_sms execute fails with "No SMS
sender is assigned to this account." The runtime auto-registration
branch in require_tenant_sender never runs on staging because
telnyx_configuration_complete() requires TELNYX_API_KEY, which staging
has none of by design (and must not gain one: PM ruling, Option A —
no Telnyx key on staging, no sender-check waiver in production code).

This migration pre-registers the platform sender for user 2 by calling
tdb.ensure_telnyx_platform_sender(2), which upserts a verified/enabled
tenant_sms_senders row for the staging platform number
(config.TELNYX_PHONE_NUMBER, +15550123456). Idempotent: re-running
upserts the same values. Guards: fails loudly if the platform number
is unset, if user 2 is not Ren's staging account, or if the row is not
verified/enabled after the upsert. The row is written on the helper's
own committed connection; the postcondition check below reads it back
on the migration connection.

Numbering: 099, not 046. Main's latest migration is 045 and its Phase
III-2 work will most likely take 046 next (then 047+ for III-3..III-6),
so a staging-only 046 would collide when main's 046 later lands in the
merged staging runner. 099 sits far above main's near-term numbering;
the runner applies by set membership, not version order, so the gap is
harmless. The staging runner appends this module after 045.
"""

VERSION = "099_assign_telnyx_platform_sender_to_ren_tenant"

USER_ID = 2
EMAIL = "ren.morrissey@gmail.com"


def upgrade_postgres(conn):
    import config

    phone = (config.TELNYX_PHONE_NUMBER or "").strip()
    if not phone:
        raise RuntimeError(
            "099: TELNYX_PHONE_NUMBER is not set; refusing to register a sender"
        )
    row = conn.execute(
        "SELECT email FROM users WHERE id = %s" % USER_ID
    ).fetchone()
    if row is None:
        raise RuntimeError("099: user %s not found" % USER_ID)
    if row["email"] != EMAIL:
        raise RuntimeError(
            "099: user %s is not %s (found %s); refusing"
            % (USER_ID, EMAIL, row["email"])
        )
    from tenant_sms_db import ensure_telnyx_platform_sender

    sender_id = ensure_telnyx_platform_sender(USER_ID)
    if sender_id is None:
        raise RuntimeError(
            "099: ensure_telnyx_platform_sender returned None (no platform number)"
        )
    # Inline postcondition check (the runner does not call verify()):
    # the helper commits on its own connection, so the row must be
    # visible on this connection.
    check = conn.execute(
        "SELECT sender_number, sms_enabled, registration_status "
        "FROM tenant_sms_senders WHERE user_id = %s" % USER_ID
    ).fetchone()
    if check is None:
        raise RuntimeError(
            "099: no tenant_sms_senders row for user %s after ensure" % USER_ID
        )
    if check["sender_number"] != phone:
        raise RuntimeError(
            "099: sender_number mismatch for user %s: %s != %s"
            % (USER_ID, check["sender_number"], phone)
        )
    if not check["sms_enabled"] or check["registration_status"] != "verified":
        raise RuntimeError(
            "099: sender row for user %s is not verified/enabled: enabled=%r status=%r"
            % (USER_ID, check["sms_enabled"], check["registration_status"])
        )


def upgrade_sqlite(conn):
    # Staging-only data fix; test databases do not need it. No-op so the
    # local test suite gains no sender row.
    return None


def _verify_postgres(conn):
    row = conn.execute(
        "SELECT sender_number, sms_enabled, registration_status "
        "FROM tenant_sms_senders WHERE user_id = %s" % USER_ID
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "099_assign_telnyx_platform_sender_to_ren_tenant failed: "
            "no sender row for user %s" % USER_ID
        )
    if not row["sms_enabled"] or row["registration_status"] != "verified":
        raise RuntimeError(
            "099_assign_telnyx_platform_sender_to_ren_tenant failed: "
            "sender row for user %s is not verified/enabled" % USER_ID
        )


def verify(conn, dialect):
    if dialect == "postgresql":
        _verify_postgres(conn)


def verify_postgres(conn):
    _verify_postgres(conn)
