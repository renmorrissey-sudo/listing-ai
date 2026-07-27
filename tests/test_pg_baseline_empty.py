"""Empty PostgreSQL bootstrap + transactional migration architecture tests.

Uses a FakeRaw Postgres connection that exercises the real upgrade_postgres /
pg_ddl / runner paths without toggling autocommit.

When TEST_DATABASE_URL is set, also runs against a genuine empty Postgres DB.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from importlib import import_module

import pytest

import config
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
    """Minimal psycopg-like connection with transactional CREATE TABLE rollback."""

    def __init__(self):
        self.autocommit = False
        self._tables = set()
        self._indexes = set()
        self._migrations = set()
        self._txn_tables = None
        self._txn_indexes = None
        self._txn_migrations = None
        self.in_transaction = False
        self.executed = []
        self.autocommit_set_attempts = []

    @property
    def tables(self):
        if self._txn_tables is not None:
            return self._txn_tables
        return self._tables

    @property
    def indexes(self):
        if self._txn_indexes is not None:
            return self._txn_indexes
        return self._indexes

    @property
    def migrations(self):
        if self._txn_migrations is not None:
            return self._txn_migrations
        return self._migrations

    def _ensure_txn(self):
        if self.autocommit:
            return
        if not self.in_transaction:
            self.in_transaction = True
            self._txn_tables = set(self._tables)
            self._txn_indexes = set(self._indexes)
            self._txn_migrations = set(self._migrations)

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        upper = text.upper()
        self.executed.append((text, params))
        self._ensure_txn()

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
            return _FakeCursor([{"ok": 1}])

        if upper.startswith("ALTER TABLE"):
            # Track additive columns from migration 004.
            if "ADD COLUMN" in upper and "LEAD_ID" in upper and "VOICE_CALLS" in upper:
                self.tables.add("voice_calls")
            return _FakeCursor()

        if "FROM information_schema.table_constraints" in text:
            return _FakeCursor([])

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

        if upper.startswith("SELECT PG_ADVISORY_LOCK") or upper.startswith(
            "SELECT PG_ADVISORY_UNLOCK"
        ):
            return _FakeCursor([{"pg_advisory_lock": True}])

        if "COUNT(*)" in upper:
            return _FakeCursor([{"c": getattr(self, "row_counts", {}).get(
                text.split(" FROM ")[1].split()[0].strip().strip(";"), 0
            )}])

        return _FakeCursor()

    def commit(self):
        if self._txn_tables is not None:
            self._tables = set(self._txn_tables)
            self._indexes = set(self._txn_indexes)
            self._migrations = set(self._txn_migrations)
        self._txn_tables = None
        self._txn_indexes = None
        self._txn_migrations = None
        self.in_transaction = False

    def rollback(self):
        self._txn_tables = None
        self._txn_indexes = None
        self._txn_migrations = None
        self.in_transaction = False

    def close(self):
        return None


class _AutocommitGuard:
    """Raise if production-style INTRANS autocommit toggle is attempted."""

    def __init__(self, raw):
        self._raw = raw
        object.__setattr__(self, "_setting", False)

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def __setattr__(self, name, value):
        if name in {"_raw", "_setting"}:
            object.__setattr__(self, name, value)
            return
        if name == "autocommit":
            self._raw.autocommit_set_attempts.append(value)
            if self._raw.in_transaction and value is True:
                raise Exception(
                    "can't change 'autocommit' now: connection in transaction status INTRANS"
                )
            # Allow runner to force False when currently False/True at connect time.
            self._raw.autocommit = value
            return
        setattr(self._raw, name, value)


class FakeCompatConn:
    def __init__(self, raw):
        self._raw = _AutocommitGuard(raw) if not isinstance(raw, _AutocommitGuard) else raw
        self._inner = raw if not isinstance(raw, _AutocommitGuard) else raw._raw
        self.engine = "postgres"

    def execute(self, sql, params=None):
        if params:
            sql = sql.replace("?", "%s")
            return self._inner.execute(sql, params)
        return self._inner.execute(sql)

    def commit(self):
        self._inner.commit()

    def rollback(self):
        self._inner.rollback()

    def close(self):
        self._inner.close()


@pytest.fixture
def fake_postgres(monkeypatch):
    raw = FakeRawPostgres()
    monkeypatch.setattr(config, "DB_ENGINE", "postgres")
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://fake:fake@localhost:5432/fake")
    monkeypatch.setattr(config, "APP_ENV", "test")

    def _connect():
        return FakeCompatConn(raw)

    monkeypatch.setattr("db_backend.connect", _connect)
    monkeypatch.setattr("migrations.runner.connect", _connect)
    return raw


def test_migration_001_does_not_toggle_autocommit(fake_postgres):
    apply_pending_migrations()
    # Runner may set autocommit=False at connect; never True.
    assert True not in fake_postgres.autocommit_set_attempts
    # Source-level regression: only runner may assign autocommit (False only).
    root = os.path.join(os.path.dirname(__file__), "..", "migrations")
    for dirpath, _, files in os.walk(root):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            text = open(path, encoding="utf-8").read()
            assert "autocommit = True" not in text, path
            assert "autocommit=True" not in text, path
            if name != "runner.py":
                assert re.search(r"\.autocommit\s*=", text) is None, path


def test_regression_no_intrans_autocommit_error(fake_postgres):
    """Baseline must not raise the production INTRANS autocommit ProgrammingError."""
    try:
        apply_pending_migrations()
    except Exception as exc:
        assert "can't change 'autocommit'" not in str(exc)
        assert "INTRANS" not in str(exc)
        raise
    assert "users" in fake_postgres.tables


def test_empty_postgres_001_creates_every_required_table(fake_postgres):
    apply_pending_migrations()
    for table in REQUIRED_BASELINE_TABLES:
        assert table in fake_postgres.tables, f"missing {table}"
    for table in POSTGRES_TABLE_ORDER:
        assert table in fake_postgres.tables


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
        "004_voice_call_lead_link",
        "005_voice_call_recording_fields",
        "006_cleanup_transient_voice_activities",
        "007_backfill_lead_follow_through",
    }
    create_users_idx = next(
        i for i, (sql, _) in enumerate(fake_postgres.executed)
        if "CREATE TABLE IF NOT EXISTS users" in sql
    )
    stamp_001 = next(
        i
        for i, (sql, params) in enumerate(fake_postgres.executed)
        if sql.upper().startswith("INSERT INTO SCHEMA_MIGRATIONS")
        and params
        and params[0] == "001_baseline"
    )
    stamp_002 = next(
        i
        for i, (sql, params) in enumerate(fake_postgres.executed)
        if sql.upper().startswith("INSERT INTO SCHEMA_MIGRATIONS")
        and params
        and params[0] == "002_safe_additive_columns"
    )
    assert create_users_idx < stamp_001 < stamp_002


def test_failed_baseline_rolls_back_and_is_not_recorded(fake_postgres, monkeypatch):
    m1 = import_module("migrations.versions.001_baseline")
    real = m1.upgrade_postgres

    def boom(conn):
        real(conn)
        raise RuntimeError("simulated failure after DDL")

    monkeypatch.setattr(m1, "upgrade_postgres", boom)
    with pytest.raises(RuntimeError, match="simulated failure after DDL"):
        apply_pending_migrations()

    assert "001_baseline" not in fake_postgres.migrations
    # Transactional rollback undoes CREATE TABLE.
    assert "users" not in fake_postgres.tables
    assert "tasks" not in fake_postgres.tables


def test_empty_postgres_second_startup_idempotent(fake_postgres):
    apply_pending_migrations()
    tables_before = set(fake_postgres.tables)
    migrations_before = set(fake_postgres.migrations)
    apply_pending_migrations()
    assert fake_postgres.tables == tables_before
    assert fake_postgres.migrations == migrations_before


def test_empty_postgres_user_lead_task_survive_restart(fake_postgres):
    apply_pending_migrations()
    fake_postgres.row_counts = {"users": 1, "leads": 1, "tasks": 1}
    apply_pending_migrations()
    assert fake_postgres.row_counts["users"] == 1
    assert fake_postgres.migrations == {
        "001_baseline",
        "002_safe_additive_columns",
        "003_user_business_profile",
        "004_voice_call_lead_link",
        "005_voice_call_recording_fields",
        "006_cleanup_transient_voice_activities",
        "007_backfill_lead_follow_through",
    }


def test_false_stamp_repaired_on_empty_postgres(fake_postgres):
    fake_postgres._migrations.add("001_baseline")
    fake_postgres._tables.add("schema_migrations")
    apply_pending_migrations()
    assert "users" in fake_postgres.tables
    assert "001_baseline" in fake_postgres.migrations
    assert "002_safe_additive_columns" in fake_postgres.migrations


def test_001_and_ledger_commit_atomically(fake_postgres, monkeypatch):
    """If stamp fails after DDL, rollback must drop both tables and ledger."""
    m1 = import_module("migrations.versions.001_baseline")
    real = m1.upgrade_postgres
    monkeypatch.setattr(m1, "upgrade_postgres", real)

    original_stamp = None
    import migrations.runner as runner

    original_stamp = runner._stamp

    def stamp_boom(conn, version):
        if version == "001_baseline":
            raise RuntimeError("stamp failed")
        return original_stamp(conn, version)

    monkeypatch.setattr(runner, "_stamp", stamp_boom)
    with pytest.raises(RuntimeError, match="stamp failed"):
        apply_pending_migrations()
    assert "001_baseline" not in fake_postgres.migrations
    assert "users" not in fake_postgres.tables


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
    import crm_db
    import db

    conn = connect()
    try:
        # Separate admin connection for schema reset only in tests.
        conn._raw.rollback()
        previous = conn._raw.autocommit
        conn._raw.autocommit = True
        try:
            conn._raw.execute("DROP SCHEMA public CASCADE")
            conn._raw.execute("CREATE SCHEMA public")
        finally:
            conn._raw.autocommit = previous
    finally:
        conn.close()

    apply_pending_migrations()
    with db.get_db() as conn:
        verify_baseline_tables(conn)
        assert missing_baseline_tables(conn) == []
        # Confirm runner left autocommit off.
        assert conn._raw.autocommit is False

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
            (
                uid,
                "Lead",
                "+15550001111",
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ),
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
            "004_voice_call_lead_link",
            "005_voice_call_recording_fields",
            "006_cleanup_transient_voice_activities",
            "007_backfill_lead_follow_through",
        }
