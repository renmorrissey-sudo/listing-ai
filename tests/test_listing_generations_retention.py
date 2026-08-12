"""Retention cleanup: hard-delete is idempotent, tenant-isolated, and does
not touch content still inside the 60-day window."""

from datetime import datetime, timedelta, timezone

import config
import db
import listing_generations_db as listing_db


def _make(user_id, address):
    return listing_db.create_generation(
        user_id, display_address=address, output_snapshot={"listing": address}
    )


def _age_row(user_id, generation_id, days_old):
    created = datetime.now(timezone.utc) - timedelta(days=days_old)
    expires = created + timedelta(days=config.LISTING_GENERATION_RETENTION_DAYS)
    with db.get_db() as conn:
        conn.execute(
            "UPDATE listing_generations SET created_at = ?, expires_at = ? WHERE id = ? AND user_id = ?",
            (created.isoformat(), expires.isoformat(), generation_id, user_id),
        )


def test_cleanup_is_idempotent(two_users):
    u1, _ = two_users
    expired = _make(u1, "1 Idempotent St")
    _age_row(u1, expired["id"], days_old=90)

    first_pass = listing_db.cleanup_expired()
    second_pass = listing_db.cleanup_expired()
    assert first_pass >= 1
    assert second_pass == 0


def test_cleanup_never_deletes_active_rows_within_retention_window(two_users):
    u1, u2 = two_users
    active_u1 = _make(u1, "2 Active St")
    active_u2 = _make(u2, "3 Active St")
    expired_u1 = _make(u1, "4 Expired St")
    _age_row(u1, expired_u1["id"], days_old=61)

    listing_db.cleanup_expired()

    assert listing_db.get_by_id(u1, active_u1["id"]) is not None
    assert listing_db.get_by_id(u2, active_u2["id"]) is not None
    assert listing_db.get_by_id(u1, expired_u1["id"]) is None


def test_row_exactly_at_59_days_is_still_retained(two_users):
    u1, _ = two_users
    row = _make(u1, "5 Still Good St")
    _age_row(u1, row["id"], days_old=59)

    listing_db.cleanup_expired()

    assert listing_db.get_by_id(u1, row["id"]) is not None


def test_cleanup_sweeps_across_all_tenants_in_one_pass(two_users):
    u1, u2 = two_users
    expired_u1 = _make(u1, "6 Cross Tenant St")
    expired_u2 = _make(u2, "7 Cross Tenant St")
    _age_row(u1, expired_u1["id"], days_old=70)
    _age_row(u2, expired_u2["id"], days_old=70)

    deleted = listing_db.cleanup_expired()

    assert deleted >= 2
    assert listing_db.get_by_id(u1, expired_u1["id"]) is None
    assert listing_db.get_by_id(u2, expired_u2["id"]) is None
