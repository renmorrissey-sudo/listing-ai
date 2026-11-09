"""Multi-tenant shared sender number: migration 032, fail-closed
registration, and inbound routing isolation.

The platform Telnyx number (+18888210810) is shared by every subscriber.
Before migration 032 its global UNIQUE(sender_number) let only the first
tenant register it; before the fail-closed fix, a failed registration
was swallowed in require_tenant_sender and outbound sending proceeded
on a fabricated in-memory "verified" sender; and before the routing
fix, an inbound message with no lead owner on the shared number was
filed under the first-registered tenant (candidates[0] / any-Telnyx-
sender fallback). All three are covered here with synthetic tenants
and mocked providers only -- no real SMS, calls, or payments.
"""

from __future__ import annotations

import importlib
import sqlite3
import uuid

import pytest

import config
import db
import tenant_sms_db as tdb
import telnyx_webhooks as txwh
from sms_authorization import NO_SENDER_MSG, require_tenant_sender

PLATFORM_NUMBER = "+18888210810"


def _unique_e164(prefix="303"):
    n = int(uuid.uuid4().hex[:8], 16) % 10_000_000
    return f"+1{prefix}{n:07d}"[:12]


def _lead(user_id, phone, name="Test Lead"):
    from lead_service import upsert_crm_lead

    lead_id, _, lead = upsert_crm_lead(
        user_id,
        phone,
        {"name": name, "phone_number": phone},
        source="test",
        touch_sms=True,
        assigned_user_id=user_id,
    )
    return lead_id, lead


def _inbound_payload(contact, account, text="Hi, is the house still available?"):
    return {
        "data": {
            "event_type": "message.received",
            "id": f"evt-{uuid.uuid4().hex[:12]}",
            "occurred_at": "2026-08-10T18:00:00.000Z",
            "payload": {
                "id": f"tx-{uuid.uuid4().hex[:12]}",
                "direction": "inbound",
                "from": {"phone_number": contact},
                "to": [{"phone_number": account}],
                "text": text,
            },
        }
    }


def _register_shared(user_id, shared):
    return tdb.upsert_tenant_sender(
        user_id,
        sender_number=shared,
        sms_provider="telnyx",
        sms_enabled=True,
        registration_status="verified",
    )


def _audit_rows(user_id, action):
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM sms_audit_events WHERE user_id = ? AND action = ?",
            (user_id, action),
        ).fetchall()
        return [dict(r) for r in rows]


# --- 1. Shared-number registration -----------------------------------------


def test_two_tenants_register_platform_number(two_users, monkeypatch):
    """Both tenants can hold the platform number; neither registration
    fails or overwrites the other's row."""
    u1, u2 = two_users
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", PLATFORM_NUMBER)
    monkeypatch.setattr(config, "TELNYX_MESSAGING_PROFILE_ID", "profile-1")

    tdb.ensure_telnyx_platform_sender(u1)
    tdb.ensure_telnyx_platform_sender(u2)

    s1 = tdb.get_active_sender(u1)
    s2 = tdb.get_active_sender(u2)
    assert s1 is not None and s2 is not None
    assert s1["user_id"] == u1 and s2["user_id"] == u2
    assert s1["sender_number"] == PLATFORM_NUMBER
    assert s2["sender_number"] == PLATFORM_NUMBER
    assert s1["id"] != s2["id"]
    assert s1["registration_status"] == "verified"
    assert s2["registration_status"] == "verified"
    assert len(tdb.list_senders_by_number(PLATFORM_NUMBER)) >= 2


def test_scoped_uniqueness_enforced(two_users):
    """Same tenant + same number is rejected at the DB level; a different
    tenant may hold the same number."""
    u1, u2 = two_users
    shared = _unique_e164("883")
    _register_shared(u1, shared)
    _register_shared(u2, shared)

    with db.get_db() as conn:
        with pytest.raises(Exception) as excinfo:
            conn.execute(
                "INSERT INTO tenant_sms_senders "
                "(user_id, sms_provider, sender_number, sms_enabled, "
                " registration_status, created_at, updated_at) "
                "VALUES (?, 'telnyx', ?, 1, 'verified', '2026-11-08T00:00:00+00:00', "
                " '2026-11-08T00:00:00+00:00')",
                (u1, shared),
            )
        assert "UNIQUE" in str(excinfo.value).upper()
        cur = conn.execute(
            "INSERT INTO tenant_sms_senders "
            "(user_id, sms_provider, sender_number, sms_enabled, "
            " registration_status, created_at, updated_at) "
            "VALUES (?, 'telnyx', ?, 1, 'verified', '2026-11-08T00:00:00+00:00', "
            " '2026-11-08T00:00:00+00:00')",
            (u2, _unique_e164("884")),
        )
        assert cur.lastrowid is not None


def test_migration_032_scoped_index_present_after_runner(two_users):
    """The shared test DB was built by db.init_db() -> the real runner
    (001-032): the scoped unique index must exist and 032's verify must
    pass on it."""
    mod = importlib.import_module(
        "migrations.versions.032_tenant_sms_senders_scoped_unique"
    )
    with db.get_db() as conn:
        mod.verify_sqlite(conn)
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_tenant_sms_senders_user_number'"
        ).fetchall()
    assert len(rows) == 1
    assert "CREATE UNIQUE INDEX" in dict(rows[0])["sql"].upper()
    # The old global unique (an automatic index) must be gone.
    with db.get_db() as conn:
        leftovers = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'tenant_sms_senders' AND name LIKE 'sqlite_autoindex%'"
        ).fetchall()
    assert leftovers == []


# --- Migration 032 through the real runner ----------------------------------


def test_migration_032_applies_forward_via_runner(tmp_path, monkeypatch):
    """Stage a scratch DB at 031, seed rows under the old global unique,
    then apply 032 via the real runner: rows survive intact, sharing the
    number afterwards succeeds, and a same-tenant duplicate is rejected.

    Under 012's global UNIQUE the seed cannot contain cross-tenant
    number reuse or same-pair duplicates (that is the evidence no
    dedupe/drop is needed in practice); the test therefore seeds two
    tenants with distinct numbers and verifies both survive the
    rebuild intact.
    """
    import migrations.runner as migration_runner

    scratch = str(tmp_path / "mig032.db")
    monkeypatch.setattr(config, "DATABASE_PATH", scratch)
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "DB_ENGINE", "sqlite")

    full_modules = list(migration_runner.MIGRATION_MODULES)
    idx_031 = next(
        i for i, m in enumerate(full_modules) if m.endswith("031_harness_job_correlation")
    )
    modules_031 = full_modules[: idx_031 + 1]

    # Phase 1: apply 001-031 only.
    monkeypatch.setattr(migration_runner, "MIGRATION_MODULES", modules_031)
    migration_runner.apply_pending_migrations()

    raw = sqlite3.connect(scratch)
    raw.execute(
        "INSERT INTO tenant_sms_senders (user_id, sms_provider, sender_number, "
        "sms_enabled, registration_status, created_at, updated_at) "
        "VALUES (101, 'telnyx', '+18888210810', 1, 'verified', "
        "'2026-11-08T00:00:00+00:00', '2026-11-08T00:00:00+00:00')"
    )
    raw.execute(
        "INSERT INTO tenant_sms_senders (user_id, sms_provider, sender_number, "
        "sms_enabled, registration_status, created_at, updated_at) "
        "VALUES (202, 'telnyx', '+15550001111', 1, 'verified', "
        "'2026-11-08T00:00:00+00:00', '2026-11-08T00:00:00+00:00')"
    )
    raw.commit()
    before = raw.execute(
        "SELECT id, user_id, sms_provider, sender_number, sms_enabled, "
        "registration_status FROM tenant_sms_senders ORDER BY id"
    ).fetchall()
    assert len(before) == 2
    # Under the old global unique, the second tenant cannot take the
    # platform number yet -- this is the bug 032 fixes.
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute(
            "INSERT INTO tenant_sms_senders (user_id, sms_provider, sender_number, "
            "sms_enabled, registration_status, created_at, updated_at) "
            "VALUES (202, 'telnyx', '+18888210810', 1, 'verified', "
            "'2026-11-08T00:00:00+00:00', '2026-11-08T00:00:00+00:00')"
        )
    raw.close()

    # Phase 2: apply 032 via the real runner.
    monkeypatch.setattr(migration_runner, "MIGRATION_MODULES", full_modules)
    migration_runner.apply_pending_migrations()

    with db.get_db() as conn:
        stamped = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = ?",
            ("032_tenant_sms_senders_scoped_unique",),
        ).fetchone()
        assert stamped is not None
        after = conn.execute(
            "SELECT id, user_id, sms_provider, sender_number, sms_enabled, "
            "registration_status FROM tenant_sms_senders ORDER BY id"
        ).fetchall()
        assert [tuple(r) if not isinstance(r, dict) else tuple(r.values())
                for r in after] == [tuple(r) for r in before]
        # The renamed-aside table remains as an empty shell (repo policy
        # forbids removing a table in a migration); it must hold no rows.
        legacy = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'tenant_sms_senders_032_legacy'"
        ).fetchone()
        assert legacy is not None
        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM tenant_sms_senders_032_legacy"
            ).fetchone()["c"]
            == 0
        )
        autoindexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'tenant_sms_senders' AND name LIKE 'sqlite_autoindex%'"
        ).fetchall()
        assert autoindexes == []

    # 032's own verify passes on the migrated database.
    mod = importlib.import_module(
        "migrations.versions.032_tenant_sms_senders_scoped_unique"
    )
    with db.get_db() as conn:
        mod.verify_sqlite(conn)

    # Scoped uniqueness now holds at the database level: the second
    # tenant can share the platform number, but the same tenant cannot
    # register it twice.
    raw = sqlite3.connect(scratch)
    raw.execute(
        "INSERT INTO tenant_sms_senders (user_id, sms_provider, sender_number, "
        "sms_enabled, registration_status, created_at, updated_at) "
        "VALUES (202, 'telnyx', '+18888210810', 1, 'verified', "
        "'2026-11-08T00:00:00+00:00', '2026-11-08T00:00:00+00:00')"
    )
    raw.commit()
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute(
            "INSERT INTO tenant_sms_senders (user_id, sms_provider, sender_number, "
            "sms_enabled, registration_status, created_at, updated_at) "
            "VALUES (101, 'telnyx', '+18888210810', 1, 'verified', "
            "'2026-11-08T00:00:00+00:00', '2026-11-08T00:00:00+00:00')"
        )
    raw.close()


# --- 2. Fail-closed registration --------------------------------------------


def test_ensure_failure_raises_and_is_logged(two_users, monkeypatch, caplog):
    """A database failure in ensure_telnyx_platform_sender is logged and
    raised -- never silently treated as a successful registration."""
    u1, _ = two_users
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", PLATFORM_NUMBER)

    def _boom(*args, **kwargs):
        raise RuntimeError("synthetic DB failure")

    monkeypatch.setattr(tdb, "upsert_tenant_sender", _boom)
    with caplog.at_level("ERROR", logger="tenant_sms_db"):
        with pytest.raises(RuntimeError, match="synthetic DB failure"):
            tdb.ensure_telnyx_platform_sender(u1)
    assert "ensure_telnyx_platform_sender failed" in caplog.text
    assert tdb.get_active_sender(u1) is None


def test_require_tenant_sender_fails_closed_when_registration_fails(
    two_users, monkeypatch, caplog
):
    """When registration fails, require_tenant_sender returns (None, err)
    and records an audit row -- it must NOT return a fabricated
    in-memory 'verified' sender dict."""
    u1, _ = two_users
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", PLATFORM_NUMBER)
    monkeypatch.setattr(config, "TELNYX_MESSAGING_PROFILE_ID", "profile-1")

    def _boom(user_id):
        raise RuntimeError("synthetic DB failure")

    monkeypatch.setattr(tdb, "ensure_telnyx_platform_sender", _boom)
    with caplog.at_level("ERROR", logger="sms_authorization"):
        sender, err = require_tenant_sender(u1)
    assert sender is None
    assert err
    assert "registration failed" in err.lower()
    assert "refusing to send" in caplog.text
    assert tdb.get_active_sender(u1) is None
    assert _audit_rows(u1, "sender_registration_failed")


def test_require_tenant_sender_fails_closed_when_no_row_after_registration(
    two_users, monkeypatch
):
    """Registration reporting success but leaving no active sender row
    must also block sending instead of inventing a sender."""
    u1, _ = two_users
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", PLATFORM_NUMBER)
    monkeypatch.setattr(config, "TELNYX_MESSAGING_PROFILE_ID", "profile-1")
    monkeypatch.setattr(tdb, "ensure_telnyx_platform_sender", lambda user_id: 123)
    monkeypatch.setattr(tdb, "get_active_sender", lambda user_id: None)

    sender, err = require_tenant_sender(u1)
    assert sender is None
    assert err == NO_SENDER_MSG


# --- 3. Inbound routing isolation on the shared number ----------------------


def test_shared_number_routes_to_each_tenants_own_lead(two_users, monkeypatch):
    """Both tenants share the platform number. A reply from tenant A's
    lead lands in A's inbox only; a reply from tenant B's lead lands in
    B's inbox only."""
    u1, u2 = two_users
    shared = _unique_e164("883")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", shared)
    _register_shared(u1, shared)
    _register_shared(u2, shared)

    contact_a = _unique_e164("701")
    contact_b = _unique_e164("702")
    _lead(u1, contact_a)
    _lead(u2, contact_b)

    res_a, status_a = txwh.handle_messaging_webhook(_inbound_payload(contact_a, shared))
    assert status_a == 200
    assert db.get_sms_message(res_a["message_id"], u1) is not None
    assert db.get_sms_message(res_a["message_id"], u2) is None

    res_b, status_b = txwh.handle_messaging_webhook(_inbound_payload(contact_b, shared))
    assert status_b == 200
    assert db.get_sms_message(res_b["message_id"], u2) is not None
    assert db.get_sms_message(res_b["message_id"], u1) is None


def test_shared_number_unknown_contact_is_not_attributed(two_users, monkeypatch):
    """A stranger texting the shared platform number -- no lead under
    either tenant -- is acked as unknown_destination and stored nowhere,
    not filed under the first-registered tenant."""
    u1, u2 = two_users
    shared = _unique_e164("884")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", shared)
    _register_shared(u1, shared)
    _register_shared(u2, shared)

    contact = _unique_e164("703")
    payload = _inbound_payload(contact, shared)
    result, status = txwh.handle_messaging_webhook(payload)
    assert status == 200
    assert result.get("ignored") == "unknown_destination"
    assert db.get_sms_message_by_provider_id(payload["data"]["payload"]["id"]) is None
    assert db.find_lead_by_phone_normalized(u1, contact) is None
    assert db.find_lead_by_phone_normalized(u2, contact) is None


def test_shared_number_both_owners_prefers_most_recent_outbound(
    two_users, monkeypatch
):
    """The contact is a lead for BOTH tenants. Tenant A's lead was
    touched most recently, but tenant B sent the most recent outbound
    message to the contact: the reply belongs to B's conversation, and
    the ambiguity is audited."""
    u1, u2 = two_users
    shared = _unique_e164("885")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", shared)
    _register_shared(u1, shared)
    _register_shared(u2, shared)

    contact = _unique_e164("704")
    stale_lead_id, _ = _lead(u1, contact, name="Old Import")
    active_lead_id, _ = _lead(u2, contact, name="Sarah Johnson")
    db.touch_lead_outbound(stale_lead_id, u1)  # A's lead is most recently active
    db.create_sms_message(
        user_id=u2,
        persona_id=None,
        provider="telnyx",
        data={
            "lead_name": "Sarah Johnson",
            "phone_number": contact,
            "message_body": "Hi -- still interested in the showing?",
        },
        status="sent",
        lead_id=active_lead_id,
        direction="outbound",
        consent_status="confirmed",
        opt_out_status="active",
    )

    result, status = txwh.handle_messaging_webhook(_inbound_payload(contact, shared))
    assert status == 200
    assert result["lead_id"] == active_lead_id
    assert db.get_sms_message(result["message_id"], u2) is not None
    assert db.get_sms_message(result["message_id"], u1) is None
    audit = _audit_rows(u2, "inbound_routing_ambiguous")
    assert len(audit) == 1
    assert "most_recent_outbound" in (audit[0].get("metadata_json") or "")


def test_shared_number_both_owners_no_outbound_prefers_most_recent_lead(
    two_users, monkeypatch
):
    """The contact is a lead for BOTH tenants and neither ever messaged
    them: the most recently active lead owner wins deterministically,
    and the ambiguity is audited."""
    u1, u2 = two_users
    shared = _unique_e164("886")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", shared)
    _register_shared(u1, shared)
    _register_shared(u2, shared)

    contact = _unique_e164("705")
    stale_lead_id, _ = _lead(u1, contact, name="Old Import")
    active_lead_id, _ = _lead(u2, contact, name="Sarah Johnson")
    db.touch_lead_outbound(active_lead_id, u2)  # B's lead is most recently active

    result, status = txwh.handle_messaging_webhook(_inbound_payload(contact, shared))
    assert status == 200
    assert result["lead_id"] == active_lead_id
    assert db.get_sms_message(result["message_id"], u2) is not None
    assert db.get_sms_message(result["message_id"], u1) is None
    audit = _audit_rows(u2, "inbound_routing_ambiguous")
    assert len(audit) == 1
    assert "most_recent_lead" in (audit[0].get("metadata_json") or "")
