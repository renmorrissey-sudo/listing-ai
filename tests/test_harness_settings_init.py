"""L8: safe harness-settings initialization for newly created tenants.

A tenant created after migration 029 has no ``harness_settings`` row —
the migration only seeded users that existed when it ran, and user
creation does not insert one. ``harness.policy.get_settings`` must
initialize the safe defaults (drafts_only / unpaused / AI enabled /
$50 spend-cap default) idempotently, without relying on the migration
seed, so that direct SQL readers see a real row too. Synthetic tenants
only; no real provider calls.
"""

import uuid

import auth
import db
from harness import costs, policy
from harness.policy import decide, get_settings


def _new_synthetic_tenant():
    suffix = uuid.uuid4().hex[:10]
    return db.create_user(
        f"l8-tenant-{suffix}@example.com", auth.hash_password("password123")
    )


def _settings_row(user_id):
    with db.get_db() as conn:
        return conn.execute(
            "SELECT mode, paused, ai_enabled FROM harness_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()


def _count_settings_rows(user_id):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM harness_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return int(row["n"])


def test_brand_new_tenant_gets_safe_defaults(two_users):
    db.init_db()
    user_id = _new_synthetic_tenant()
    assert _settings_row(user_id) is None  # no migration seed for this tenant
    settings = get_settings(user_id)
    assert settings["mode"] == "drafts_only"
    assert settings["paused"] is False
    assert settings["ai_enabled"] is True
    assert settings["monthly_spend_cap_cents"] == costs.get_monthly_spend_cap_cents()
    assert settings["monthly_spend_cap_cents"] == 5000


def test_brand_new_tenant_row_is_persisted_for_direct_readers(two_users):
    db.init_db()
    user_id = _new_synthetic_tenant()
    get_settings(user_id)
    row = _settings_row(user_id)
    assert row is not None
    assert row["mode"] == "drafts_only"
    assert int(row["paused"]) == 0
    assert int(row["ai_enabled"]) == 1


def test_initialization_is_idempotent_and_never_overwrites(two_users):
    db.init_db()
    user_id = _new_synthetic_tenant()
    first = get_settings(user_id)
    second = get_settings(user_id)
    assert first == second
    assert _count_settings_rows(user_id) == 1

    # A tenant that later opts into a stored mode keeps it: init must
    # not clobber an existing row.
    with db.get_db() as conn:
        conn.execute(
            "UPDATE harness_settings SET mode = 'authorized' WHERE user_id = ?",
            (user_id,),
        )
    assert get_settings(user_id)["mode"] == "authorized"
    assert _count_settings_rows(user_id) == 1


def test_brand_new_tenant_gate_is_drafts_only(two_users):
    db.init_db()
    user_id = _new_synthetic_tenant()
    decision, _ = decide(user_id, "send_sms", "send_sms")
    assert decision == "DRAFT"
    decision, _ = decide(user_id, "find_lead", "find_lead")
    assert decision == "ALLOW"


def test_get_settings_with_no_user_still_returns_safe_defaults():
    settings = get_settings(None)
    assert settings["mode"] == "drafts_only"
    assert settings["paused"] is False
    assert settings["ai_enabled"] is True
    assert settings["monthly_spend_cap_cents"] == 5000
