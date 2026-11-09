"""Adversarial cross-tenant tests for harness job scoping (M5/M6).

M5: complete_job / fail_job must enforce tenant ownership when a
``user_id`` is given — a cross-tenant call fails and leaves the job
untouched. The unscoped call (no ``user_id``) is the documented
authenticated-worker path and keeps working for the harness worker.

M6: idempotency is scoped by ``(user_id, idempotency_key)`` — a tenant
reusing another tenant's key gets its own new job, while the same tenant
reusing its own key still gets the original job id back.

The last test drives migration 030 forward through the real runner on a
scratch database (001-029, seed rows, then 030) and checks the schema,
row preservation and the scoped uniqueness.

All provider calls are mocked / unused here; nothing sends a real SMS.
"""

import importlib
import json
import sqlite3

import pytest

import config
import db
from harness.jobs import (
    claim_jobs,
    complete_job,
    enqueue_job,
    fail_job,
    get_job,
    list_jobs,
)
from migrations import runner as migration_runner


@pytest.fixture(autouse=True)
def _isolate_job_queue():
    """The session DB is shared by every test (tests/conftest.py), so
    earlier tests can leave harness jobs behind. Tests in this file
    assert on the queue as a whole (claim order), so clear the queue
    first: the assertions below are then about this test's jobs only.
    The migration test below stages its own scratch database and is
    unaffected (this fixture only touches the session DB)."""
    db.init_db()
    with db.get_db() as conn:
        conn.execute("DELETE FROM harness_jobs")
    yield


def _row(job_id):
    with db.get_db() as conn:
        return dict(
            conn.execute(
                "SELECT * FROM harness_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        )


# --- M5: unscoped job mutations ---------------------------------------------


def test_complete_job_wrong_tenant_raises_and_leaves_unchanged(two_users):
    u1, u2 = two_users
    job_id = enqueue_job(u1, "sms_auto_reply", {"owner": "a"})
    with pytest.raises(PermissionError):
        complete_job(job_id, result={"ok": True}, user_id=u2)
    row = _row(job_id)
    assert row["status"] == "pending"
    assert row["result_json"] is None
    assert row["user_id"] == u1
    # The rightful owner can still complete it.
    complete_job(job_id, result={"ok": True}, user_id=u1)
    assert _row(job_id)["status"] == "succeeded"
    assert json.loads(_row(job_id)["result_json"]) == {"ok": True}


def test_fail_job_wrong_tenant_raises_and_leaves_unchanged(two_users):
    u1, u2 = two_users
    job_id = enqueue_job(u1, "sms_auto_reply", {"owner": "a"})
    claimed = claim_jobs(limit=1, claimed_by="test-worker")
    assert [c["id"] for c in claimed] == [job_id]
    with pytest.raises(PermissionError):
        fail_job(job_id, "tenant B trying to fail A's job", user_id=u2)
    row = _row(job_id)
    assert row["status"] == "claimed"
    assert row["attempts"] == 1
    assert row["result_json"] is None
    assert row["next_attempt_at"] is None
    # The rightful owner can still fail it (retryable -> pending + backoff).
    status = fail_job(job_id, "transient boom", retryable=True, user_id=u1)
    assert status == "pending"
    row = _row(job_id)
    assert row["status"] == "pending"
    assert row["next_attempt_at"] is not None
    assert "transient boom" in (row["result_json"] or "")


def test_complete_and_fail_unknown_job_still_raise_value_error(two_users):
    u1, _ = two_users
    with pytest.raises(ValueError):
        complete_job(999999999, user_id=u1)
    with pytest.raises(ValueError):
        fail_job(999999999, "boom", user_id=u1)
    # Unknown ids raise ValueError, never PermissionError: there is no
    # owner to be wrong about.
    with pytest.raises(ValueError):
        complete_job(999999999)


def test_worker_authority_call_without_user_id_still_works(two_users):
    """The unscoped call is the authenticated-worker path (see
    harness/jobs.py module docstring): only the harness worker, which
    mutates jobs it has just claimed, may call without a tenant. It keeps
    working so the worker and existing call sites are not broken; any
    tenant-facing caller must pass ``user_id`` instead."""
    u1, _ = two_users
    job_id = enqueue_job(u1, "sms_auto_reply", {})
    claim_jobs(limit=1)
    status = fail_job(job_id, "transient boom", retryable=True)
    assert status == "pending"
    claim_jobs(limit=1)
    complete_job(job_id, result={"ok": True})
    assert _row(job_id)["status"] == "succeeded"


# --- M6: tenant-scoped idempotency ------------------------------------------


def test_enqueue_same_key_different_tenant_gets_own_job(two_users):
    u1, u2 = two_users
    first = enqueue_job(
        u1, "sms_auto_reply", {"owner": "a"}, idempotency_key="key-shared-m6"
    )
    second = enqueue_job(
        u2, "sms_auto_reply", {"owner": "b"}, idempotency_key="key-shared-m6"
    )
    assert second != first
    assert get_job(second, u2) is not None
    assert get_job(second, u1) is None
    assert get_job(first, u1) is not None
    assert get_job(first, u2) is None
    assert json.loads(_row(second)["payload_json"]) == {"owner": "b"}
    assert json.loads(_row(first)["payload_json"]) == {"owner": "a"}
    assert [j["id"] for j in list_jobs(u1)] == [first]
    assert [j["id"] for j in list_jobs(u2)] == [second]
    complete_job(first, user_id=u1)
    complete_job(second, user_id=u2)


def test_enqueue_same_key_same_tenant_still_idempotent(two_users):
    u1, _ = two_users
    first = enqueue_job(
        u1, "sms_auto_reply", {"a": 1}, idempotency_key="key-dup-m6"
    )
    second = enqueue_job(
        u1, "sms_auto_reply", {"a": 2}, idempotency_key="key-dup-m6"
    )
    assert first == second
    assert len(list_jobs(u1)) == 1
    assert json.loads(_row(first)["payload_json"]) == {"a": 1}
    complete_job(first, user_id=u1)


def test_enqueue_same_key_after_first_job_completed(two_users):
    u1, u2 = two_users
    first = enqueue_job(
        u1, "sms_auto_reply", {"a": 1}, idempotency_key="key-done-m6"
    )
    complete_job(first, user_id=u1)
    # Same tenant, same key: still the original (completed) job.
    assert (
        enqueue_job(u1, "sms_auto_reply", {"a": 2}, idempotency_key="key-done-m6")
        == first
    )
    # Other tenant, same key: a new job of its own, pending.
    other = enqueue_job(
        u2, "sms_auto_reply", {"a": 3}, idempotency_key="key-done-m6"
    )
    assert other != first
    assert _row(other)["status"] == "pending"
    complete_job(other, user_id=u2)


def test_scoped_unique_index_present_after_runner(two_users):
    """The shared test DB was built by db.init_db() -> the real runner
    (001-030): the scoped unique index must exist and 030's verify must
    pass on it."""
    mod = importlib.import_module(
        "migrations.versions.030_harness_scoped_idempotency"
    )
    with db.get_db() as conn:
        mod.verify_sqlite(conn)
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_harness_jobs_user_idempotency'"
        ).fetchall()
    assert len(rows) == 1
    assert "CREATE UNIQUE INDEX" in dict(rows[0])["sql"].upper()


# --- Migration 030 through the real runner ----------------------------------


def test_migration_030_applies_forward_via_runner(tmp_path, monkeypatch):
    """Stage a scratch DB at 029, seed rows, then apply 030 via the real
    runner and check the schema, row preservation and scoped uniqueness.

    Under 029's global UNIQUE the seed cannot contain cross-tenant key
    reuse or same-pair duplicates (that is the evidence no dedupe is
    needed in practice); the test therefore seeds distinct keys plus
    NULL-key rows and verifies all of them survive the rebuild intact.
    """
    scratch = str(tmp_path / "mig030.db")
    monkeypatch.setattr(config, "DATABASE_PATH", scratch)
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "DB_ENGINE", "sqlite")

    full_modules = list(migration_runner.MIGRATION_MODULES)
    # 030 is the migration under test; later migrations (031+) must not
    # change what this test stages (001-029) or applies (030).
    idx_030 = next(
        i
        for i, m in enumerate(full_modules)
        if m.endswith("030_harness_scoped_idempotency")
    )
    modules_030 = full_modules[: idx_030 + 1]

    # Phase 1: apply 001-029 only.
    monkeypatch.setattr(
        migration_runner, "MIGRATION_MODULES", modules_030[:-1]
    )
    migration_runner.apply_pending_migrations()

    raw = sqlite3.connect(scratch)
    raw.execute(
        "INSERT INTO harness_jobs (user_id, kind, payload_json, "
        "idempotency_key, status, attempts, created_at, updated_at) "
        "VALUES (101, 'sms_auto_reply', '{\"owner\": \"a\"}', 'key-a', "
        "'pending', 0, '2026-11-08T00:00:00+00:00', '2026-11-08T00:00:00+00:00')"
    )
    raw.execute(
        "INSERT INTO harness_jobs (user_id, kind, payload_json, "
        "idempotency_key, status, attempts, created_at, updated_at) "
        "VALUES (202, 'sms_auto_reply', '{\"owner\": \"b\"}', 'key-b', "
        "'pending', 0, '2026-11-08T00:00:00+00:00', '2026-11-08T00:00:00+00:00')"
    )
    raw.execute(
        "INSERT INTO harness_jobs (user_id, kind, payload_json, "
        "idempotency_key, status, attempts, created_at, updated_at) "
        "VALUES (101, 'sms_auto_reply', '{}', NULL, 'succeeded', 1, "
        "'2026-11-08T00:00:00+00:00', '2026-11-08T00:00:00+00:00')"
    )
    raw.execute(
        "INSERT INTO harness_jobs (user_id, kind, payload_json, "
        "idempotency_key, status, attempts, created_at, updated_at) "
        "VALUES (202, 'sms_auto_reply', '{}', NULL, 'pending', 0, "
        "'2026-11-08T00:00:00+00:00', '2026-11-08T00:00:00+00:00')"
    )
    raw.commit()
    before = raw.execute(
        "SELECT id, user_id, kind, payload_json, idempotency_key, status, "
        "attempts FROM harness_jobs ORDER BY id"
    ).fetchall()
    assert len(before) == 4
    max_id_before = max(r[0] for r in before)
    raw.close()

    # Phase 2: apply 030 via the real runner. 031 is applied too: it
    # only adds the correlation_id column that enqueue_job now writes,
    # and the runner always migrates to latest in real deployments, so
    # a DB staged at exactly 030 is not a state the app ever runs on.
    idx_031 = next(
        i
        for i, m in enumerate(full_modules)
        if m.endswith("031_harness_job_correlation")
    )
    monkeypatch.setattr(
        migration_runner, "MIGRATION_MODULES", full_modules[: idx_031 + 1]
    )
    migration_runner.apply_pending_migrations()

    with db.get_db() as conn:
        stamped = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = ?",
            ("030_harness_scoped_idempotency",),
        ).fetchone()
        assert stamped is not None
        after = conn.execute(
            "SELECT id, user_id, kind, payload_json, idempotency_key, "
            "status, attempts FROM harness_jobs ORDER BY id"
        ).fetchall()
        assert [tuple(r) if not isinstance(r, dict) else tuple(r.values())
                for r in after] == [tuple(r) for r in before]
        # The renamed-aside table remains as an empty shell (repo policy
        # forbids removing a table in a migration); it must hold no rows.
        legacy = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'harness_jobs_030_legacy'"
        ).fetchone()
        assert legacy is not None
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM harness_jobs_030_legacy"
        ).fetchone()["c"] == 0
        # No automatic index (the old global unique) may remain.
        autoindexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'harness_jobs' AND name LIKE 'sqlite_autoindex%'"
        ).fetchall()
        assert autoindexes == []

    # 030's own verify passes on the migrated database.
    mod = importlib.import_module(
        "migrations.versions.030_harness_scoped_idempotency"
    )
    with db.get_db() as conn:
        mod.verify_sqlite(conn)

    # Scoped uniqueness now holds at the database level: same tenant +
    # same key is rejected, cross-tenant reuse is allowed.
    raw = sqlite3.connect(scratch)
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute(
            "INSERT INTO harness_jobs (user_id, kind, idempotency_key) "
            "VALUES (101, 'sms_auto_reply', 'key-a')"
        )
    raw.execute(
        "INSERT INTO harness_jobs (user_id, kind, idempotency_key) "
        "VALUES (202, 'sms_auto_reply', 'key-a')"
    )
    raw.commit()
    raw.close()

    # And through the application code: tenant 202 reusing tenant 101's
    # key gets its own job (the row just inserted above proves the DB
    # allows it; enqueue must return that job, not 101's).
    other = enqueue_job(
        202, "sms_auto_reply", {"owner": "b2"}, idempotency_key="key-a"
    )
    assert other != before[0][0]
    assert get_job(other, 202) is not None
    # ID sequence continues past the pre-migration max (AUTOINCREMENT
    # survived the rebuild): a fresh key-less job gets a new, higher id.
    fresh = enqueue_job(101, "sms_auto_reply", {"n": 1})
    assert fresh > max_id_before
