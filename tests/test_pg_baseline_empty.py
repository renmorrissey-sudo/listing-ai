"""Empty PostgreSQL bootstrap tests.

Uses a FakeRaw Postgres connection that exercises the real upgrade_postgres /
pg_ddl code paths (autocommit DDL, pg_class existence checks, stamping).

When TEST_DATABASE_URL is set, also runs against a genuine empty Postgres DB.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone

import pytest

import config
from importlib import import_module

from migrations.runner import (
    REQUIRED_BASELINE_TABLES,
    apply_pending_migrations,
    missing_baseline_tables,
    verify_baseline_tables,
)

_m001 = import_module("migrations.versions.001_baseline")
POSTGRES_INDEXES = _m001.POSTGRES_INDEXES
POSTGRES_TABLE_ORDER = _m001.POSTGRES_TABLE_ORDER


class _FakeCursor:
    def __init__(self, rows=None):
        self._rows = rows or []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        return None


class FakeRawPostgres:
    """Minimal psycopg-like connection that persists CREATE TABLE/INDEX."""

    def __init__(self):
        self.autocommit = False
        self.tables = set()
        self.indexes = set()
        self.migrations = set()
        self.executed = []
        self.row_factory = None

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        upper = text.upper()
        self.executed.append((text, params))

        if upper.startswith("CREATE TABLE IF NOT EXISTS"):
            match = re.search(r"CREATE TABLE IF NOT EXISTS\s+([a-z0-9_]+)", text, re.I)
            assert match, text
            self.tables.add(match.group(1))
            return _FakeCursor()

        if upper.startswith("CREATE INDEX IF NOT EXISTS"):
            match = re.search(r"CREATE INDEX IF NOT EXISTS\s+([a-z0-9_]+)", text, re.I)
            assert match, text
            self.indexes.add(match.group(1))
            return _FakeCursor()

        if "FROM pg_catalog.pg_class" in text and "relkind = 'r'" in text:
            name = params[0] if params else None
            return _FakeCursor([{"ok": name in self.tables}])

        if "FROM pg_catalog.pg_class" in text and "relkind = 'i'" in text:
            name = params[0] if params else None
            return _FakeCursor([{"ok": name in self.indexes}])

        if "FROM information_schema.tables" in text:
            name = params[0] if params else None
            return _FakeCursor([{"ok": 1}] if name in self.tables else [])

        if "FROM information_schema.columns" in text:
            # Additive migrations: pretend columns already exist (baseline is complete).
            return _FakeCursor([{"ok": 1}])

        if upper.startswith("ALTER TABLE"):
            return _FakeCursor()

        if upper.startswith("INSERT INTO SCHEMA_MIGRATIONS"):
            version = params[0] if params else None
            self.migrations.add(version)
            return _FakeCursor()

        if upper.startswith("DELETE FROM SCHEMA_MIGRATIONS"):
            self.migrations.clear()
            return _FakeCursor()

        if "SELECT VERSION FROM SCHEMA_MIGRATIONS" in upper:
            rows = [{"version": v} for v in sorted(self.migrations)]
            return _FakeCursor(rows)

        if upper.startswith("SELECT PG_ADVISORY_LOCK") or upper.startswith("SELECT PG_ADVISORY_UNLOCK"):
            return _FakeCursor([{"pg_advisory_lock": True}])

        if "COUNT(*)" in upper:
            return _FakeCursor([{"c": 0}])

        return _FakeCursor()

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


class FakeCompatConn:
    def __init__(self, raw):
        self._raw = raw
        self.engine = "postgres"

    def execute(self, sql, params=None):
        # Mirror db_backend placeholder conversion for queries with params.
        if params:
            sql = sql.replace("?", "%s")
            return self._raw.execute(sql, params)
        return self._raw.execute(sql)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()


@pytest.fixture
def fake_postgres(monkeypatch):
    raw = FakeRawPostgres()
    # Shared raw so "fresh" connections see persisted DDL (autocommit semantics).
    monkeypatch.setattr(config, "DB_ENGINE", "postgres")
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://fake:fake@localhost:5432/fake")
    monkeypatch.setattr(config, "APP_ENV", "test")

    def _connect():
        return FakeCompatConn(raw)

    monkeypatch.setattr("db_backend.connect", _connect)
    monkeypatch.setattr("migrations.runner.connect", _connect)
    return raw


def test_empty_postgres_001_creates_every_required_table(fake_postgres):
    apply_pending_migrations()
    for table in REQUIRED_BASELINE_TABLES:
        assert table in fake_postgres.tables, f"missing {table}"
    for table in POSTGRES_TABLE_ORDER:
        assert table in fake_postgres.tables
    assert "users" in fake_postgres.tables
    assert "tasks" in fake_postgres.tables


def test_empty_postgres_indexes_created(fake_postgres):
    apply_pending_migrations()
    for index_name, _ in POSTGRES_INDEXES:
        assert index_name in fake_postgres.indexes, f"missing index {index_name}"


def test_empty_postgres_migration_order_and_ledger(fake_postgres):
    apply_pending_migrations()
    assert fake_postgres.migrations == {
        "001_baseline",
        "002_safe_additive_columns",
        "003_user_business_profile",
    }
    # 001 CREATE TABLE must appear before any additive ALTER semantics / stamps of 002.
    create_users_idx = next(
        i for i, (sql, _) in enumerate(fake_postgres.executed) if "CREATE TABLE IF NOT EXISTS users" in sql
    )
    stamp_001 = next(
        i
        for i, (sql, params) in enumerate(fake_postgres.executed)
        if sql.upper().startswith("INSERT INTO SCHEMA_MIGRATIONS") and params and params[0] == "001_baseline"
    )
    stamp_002 = next(
        i
        for i, (sql, params) in enumerate(fake_postgres.executed)
        if sql.upper().startswith("INSERT INTO SCHEMA_MIGRATIONS") and params and params[0] == "002_safe_additive_columns"
    )
    assert create_users_idx < stamp_001 < stamp_002


def test_empty_postgres_second_startup_idempotent(fake_postgres):
    apply_pending_migrations()
    tables_before = set(fake_postgres.tables)
    migrations_before = set(fake_postgres.migrations)
    create_count_before = sum(
        1 for sql, _ in fake_postgres.executed if sql.upper().startswith("CREATE TABLE")
    )
    apply_pending_migrations()
    assert fake_postgres.tables == tables_before
    assert fake_postgres.migrations == migrations_before
    # Second run may re-check existence but must not create a second ledger set.
    assert fake_postgres.migrations == {
        "001_baseline",
        "002_safe_additive_columns",
        "003_user_business_profile",
    }
    assert create_count_before > 0


def test_empty_postgres_user_lead_task_survive_restart(fake_postgres):
    apply_pending_migrations()
    # Simulate retained application rows; COUNT(*) must stay non-zero so repair
    # does not treat the DB as empty, and migrations must remain idempotent.
    fake_postgres.row_counts = {"users": 1, "leads": 1, "tasks": 1}
    original_execute = fake_postgres.execute

    def count_aware_execute(sql, params=None):
        text = " ".join(str(sql).split())
        upper = text.upper()
        if "COUNT(*)" in upper:
            table = text.split(" FROM ")[1].split()[0].strip().strip(";")
            return _FakeCursor([{"c": fake_postgres.row_counts.get(table, 0)}])
        return original_execute(sql, params)

    fake_postgres.execute = count_aware_execute  # type: ignore[method-assign]
    apply_pending_migrations()
    assert fake_postgres.row_counts["users"] == 1
    assert fake_postgres.row_counts["leads"] == 1
    assert fake_postgres.row_counts["tasks"] == 1
    assert fake_postgres.migrations == {
        "001_baseline",
        "002_safe_additive_columns",
        "003_user_business_profile",
    }


def test_false_stamp_repaired_on_empty_postgres(fake_postgres):
    fake_postgres.migrations.add("001_baseline")
    fake_postgres.tables.add("schema_migrations")
    # users etc. missing → empty DB repair
    apply_pending_migrations()
    assert "users" in fake_postgres.tables
    assert "001_baseline" in fake_postgres.migrations
    assert "002_safe_additive_columns" in fake_postgres.migrations


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="Set TEST_DATABASE_URL for genuine empty PostgreSQL bootstrap",
)
def test_genuine_empty_postgres_database(monkeypatch):
    url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setattr(config, "DATABASE_URL", url)
    monkeypatch.setattr(config, "DB_ENGINE", "postgres")
    monkeypatch.setattr(config, "APP_ENV", "test")

    from db_backend import connect
    import db
    import crm_db

    conn = connect()
    try:
        conn._raw.autocommit = True
        conn._raw.execute("DROP SCHEMA public CASCADE")
        conn._raw.execute("CREATE SCHEMA public")
    finally:
        conn.close()

    apply_pending_migrations()
    with db.get_db() as conn:
        verify_baseline_tables(conn)
        assert missing_baseline_tables(conn) == []

    # Retain rows across restart/migrations.
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO users (email, password_hash, subscription_status, created_at)
            VALUES (%s, %s, %s, %s)
            """,
            ("pg@example.com", "hash", "active", datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        user = conn.execute(
            "SELECT id FROM users WHERE email = %s", ("pg@example.com",)
        ).fetchone()
        uid = user["id"]
        conn.execute(
            """
            INSERT INTO leads (user_id, name, phone_number, status, source, consent_status,
                               opt_out_status, created_at, updated_at)
            VALUES (%s, %s, %s, 'new', 'sms', 'unknown', 'active', %s, %s)
            """,
            (uid, "Lead", "+15550001111", datetime.now(timezone.utc).isoformat(),
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        lead = conn.execute(
            "SELECT id FROM leads WHERE user_id = %s", (uid,)
        ).fetchone()

    task_id, err = crm_db.create_task(
        uid, {"title": "PG survive", "task_type": "call", "lead_id": lead["id"]}
    )
    assert err is None

    apply_pending_migrations()
    apply_pending_migrations()

    with db.get_db() as conn:
        assert conn.execute(
            "SELECT email FROM users WHERE id = %s", (uid,)
        ).fetchone()["email"] == "pg@example.com"
        assert crm_db.get_task(uid, task_id)["title"] == "PG survive"
        versions = {
            r["version"]
            for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        assert versions == {
            "001_baseline",
            "002_safe_additive_columns",
            "003_user_business_profile",
        }
