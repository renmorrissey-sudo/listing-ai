"""Migration bootstrap: empty DB, order, false-stamp repair, failed not stamped."""

import os
import tempfile

import pytest

import config
import db
from migrations.runner import (
    REQUIRED_BASELINE_TABLES,
    MigrationBootstrapError,
    apply_pending_migrations,
    verify_baseline_tables,
)


@pytest.fixture
def empty_sqlite_db(monkeypatch):
    """Completely empty SQLite file (no tables) wired as the app database."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["DATABASE_PATH"] = path
    os.environ.pop("DATABASE_URL", None)
    monkeypatch.setattr(config, "DATABASE_PATH", path)
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "DB_ENGINE", "sqlite")
    monkeypatch.setattr(config, "APP_ENV", "test")
    yield path
    try:
        os.remove(path)
    except OSError:
        pass


def _table_names():
    with db.get_db() as conn:
        if conn.engine == "postgres":
            rows = conn.execute(
                """
                SELECT table_name AS name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                ORDER BY table_name
                """
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        return {r["name"] for r in rows}


def _applied():
    names = _table_names()
    if "schema_migrations" not in names:
        return set()
    with db.get_db() as conn:
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        return {r["version"] for r in rows}


def test_empty_database_bootstrap_creates_baseline_tables(empty_sqlite_db):
    assert _table_names() == set()
    apply_pending_migrations()
    names = _table_names()
    for required in REQUIRED_BASELINE_TABLES:
        assert required in names, f"missing {required}"
    assert _applied() == {
        "001_baseline",
        "002_safe_additive_columns",
        "003_user_business_profile",
        "004_voice_call_lead_link",
        "005_voice_call_recording_fields",
        "006_cleanup_transient_voice_activities",
        "007_backfill_lead_follow_through",
    }


def test_migration_order_baseline_before_additive(empty_sqlite_db, monkeypatch):
    from importlib import import_module

    order = []
    m1 = import_module("migrations.versions.001_baseline")
    m2 = import_module("migrations.versions.002_safe_additive_columns")
    m3 = import_module("migrations.versions.003_user_business_profile")
    m4 = import_module("migrations.versions.004_voice_call_lead_link")
    m5 = import_module("migrations.versions.005_voice_call_recording_fields")
    m6 = import_module("migrations.versions.006_cleanup_transient_voice_activities")
    m7 = import_module("migrations.versions.007_backfill_lead_follow_through")

    real_sq1 = m1.upgrade_sqlite
    real_sq2 = m2.upgrade_sqlite
    real_sq3 = m3.upgrade_sqlite
    real_sq4 = m4.upgrade_sqlite
    real_sq5 = m5.upgrade_sqlite
    real_sq6 = m6.upgrade_sqlite
    real_sq7 = m7.upgrade_sqlite

    def wrap(name, real):
        def _inner(conn):
            order.append(name)
            return real(conn)

        return _inner

    monkeypatch.setattr(m1, "upgrade_sqlite", wrap("001", real_sq1))
    monkeypatch.setattr(m2, "upgrade_sqlite", wrap("002", real_sq2))
    monkeypatch.setattr(m3, "upgrade_sqlite", wrap("003", real_sq3))
    monkeypatch.setattr(m4, "upgrade_sqlite", wrap("004", real_sq4))
    monkeypatch.setattr(m5, "upgrade_sqlite", wrap("005", real_sq5))
    monkeypatch.setattr(m6, "upgrade_sqlite", wrap("006", real_sq6))
    monkeypatch.setattr(m7, "upgrade_sqlite", wrap("007", real_sq7))

    apply_pending_migrations()
    assert order == ["001", "002", "003", "004", "005", "006", "007"]


def test_false_001_stamp_without_users_is_repaired(empty_sqlite_db):
    """Reproduce production failure: 001 stamped, users missing → repair + bootstrap."""
    from db_backend import connect

    conn = connect()
    try:
        conn.execute(
            """
            CREATE TABLE schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            ("001_baseline", "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    assert "users" not in _table_names()
    assert "001_baseline" in _applied()

    apply_pending_migrations()

    assert "users" in _table_names()
    assert "tasks" in _table_names()
    assert "leads" in _table_names()
    assert "sms_messages" in _table_names()
    with db.get_db() as conn:
        verify_baseline_tables(conn)
    assert _applied() == {
        "001_baseline",
        "002_safe_additive_columns",
        "003_user_business_profile",
        "004_voice_call_lead_link",
        "005_voice_call_recording_fields",
        "006_cleanup_transient_voice_activities",
        "007_backfill_lead_follow_through",
    }


def test_failed_migration_is_not_marked_applied(empty_sqlite_db, monkeypatch):
    from importlib import import_module

    m1 = import_module("migrations.versions.001_baseline")

    def boom(conn):
        raise RuntimeError("simulated baseline failure before DDL completes")

    monkeypatch.setattr(m1, "upgrade_sqlite", boom)

    with pytest.raises(RuntimeError, match="simulated baseline failure"):
        apply_pending_migrations()

    assert "001_baseline" not in _applied()


def test_second_startup_is_idempotent(empty_sqlite_db):
    apply_pending_migrations()
    first = _table_names()
    first_applied = _applied()
    apply_pending_migrations()
    apply_pending_migrations()
    assert _table_names() == first
    assert _applied() == first_applied


def test_existing_records_survive_later_migrations(empty_sqlite_db):
    apply_pending_migrations()
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO users (email, password_hash, subscription_status, created_at)
            VALUES (?, ?, ?, ?)
            """,
            ("survive@example.com", "hash", "active", "2026-01-01T00:00:00+00:00"),
        )
    apply_pending_migrations()
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT email, subscription_status FROM users WHERE email = ?",
            ("survive@example.com",),
        ).fetchone()
        assert row["email"] == "survive@example.com"
        assert row["subscription_status"] == "active"
        count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        assert int(count) == 1


def test_002_blocked_when_false_stamp_repair_disabled(empty_sqlite_db, monkeypatch):
    """Without repair, missing baseline tables must fail before additive migrations."""
    from db_backend import connect

    conn = connect()
    try:
        conn.execute(
            """
            CREATE TABLE schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            ("001_baseline", "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(
        "migrations.runner._repair_false_baseline_stamp",
        lambda conn, applied: applied,
    )
    with pytest.raises(MigrationBootstrapError, match="marked applied but required tables"):
        apply_pending_migrations()


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="Set TEST_DATABASE_URL to run empty PostgreSQL bootstrap against a real DB",
)
def test_empty_postgres_bootstrap(monkeypatch):
    url = os.environ["TEST_DATABASE_URL"]
    monkeypatch.setattr(config, "DATABASE_URL", url)
    monkeypatch.setattr(config, "DB_ENGINE", "postgres")
    monkeypatch.setattr(config, "APP_ENV", "test")
    os.environ["DATABASE_URL"] = url

    from db_backend import connect
    from migrations.runner import missing_baseline_tables

    conn = connect()
    try:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
    finally:
        conn.close()

    apply_pending_migrations()
    with db.get_db() as conn:
        verify_baseline_tables(conn)
        assert missing_baseline_tables(conn) == []
    assert _applied() == {
        "001_baseline",
        "002_safe_additive_columns",
        "003_user_business_profile",
        "004_voice_call_lead_link",
        "005_voice_call_recording_fields",
        "006_cleanup_transient_voice_activities",
        "007_backfill_lead_follow_through",
    }
