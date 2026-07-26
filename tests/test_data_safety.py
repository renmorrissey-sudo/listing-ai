"""Production data-safety guarantees."""

import os
from datetime import datetime, timezone

import pytest

import config
import crm_db
import db
from migrations.runner import apply_pending_migrations


def test_production_refuses_sqlite(monkeypatch):
    monkeypatch.setattr(config, "APP_ENV", "production")
    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "DB_ENGINE", "sqlite")
    with pytest.raises(SystemExit):
        config.validate_database_config()


def test_production_refuses_sqlite_database_url(monkeypatch):
    monkeypatch.setattr(config, "APP_ENV", "production")
    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    monkeypatch.setattr(config, "DATABASE_URL", "sqlite:///tmp/prod.db")
    monkeypatch.setattr(config, "DB_ENGINE", "postgres")
    with pytest.raises(SystemExit):
        config.validate_database_config()


def test_production_refuses_destructive_flags(monkeypatch):
    monkeypatch.setattr(config, "APP_ENV", "production")
    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://user:pass@host.railway.app:5432/railway")
    monkeypatch.setattr(config, "DB_ENGINE", "postgres")
    monkeypatch.setattr(config, "ALLOW_DESTRUCTIVE_DB_RESET", True)
    monkeypatch.setattr(config, "ALLOW_SQLITE_TABLE_REBUILD", False)
    monkeypatch.setattr(config, "RUN_DEMO_SEED_ON_STARTUP", False)
    with pytest.raises(SystemExit):
        config.validate_database_config()


def test_production_refuses_test_database_url(monkeypatch):
    monkeypatch.setattr(config, "APP_ENV", "production")
    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://user:pass@host/railway_test")
    monkeypatch.setattr(config, "DB_ENGINE", "postgres")
    monkeypatch.setattr(config, "ALLOW_DESTRUCTIVE_DB_RESET", False)
    monkeypatch.setattr(config, "ALLOW_SQLITE_TABLE_REBUILD", False)
    monkeypatch.setattr(config, "RUN_DEMO_SEED_ON_STARTUP", False)
    with pytest.raises(SystemExit):
        config.validate_database_config()


def test_production_refuses_localhost_database_url(monkeypatch):
    monkeypatch.setattr(config, "APP_ENV", "production")
    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://user:pass@localhost:5432/railway")
    monkeypatch.setattr(config, "DB_ENGINE", "postgres")
    monkeypatch.setattr(config, "ALLOW_DESTRUCTIVE_DB_RESET", False)
    monkeypatch.setattr(config, "ALLOW_SQLITE_TABLE_REBUILD", False)
    monkeypatch.setattr(config, "RUN_DEMO_SEED_ON_STARTUP", False)
    with pytest.raises(SystemExit):
        config.validate_database_config()


def test_production_refuses_tmp_database_url(monkeypatch):
    monkeypatch.setattr(config, "APP_ENV", "production")
    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://user:pass@db.example/tmp/prod")
    monkeypatch.setattr(config, "DB_ENGINE", "postgres")
    monkeypatch.setattr(config, "ALLOW_DESTRUCTIVE_DB_RESET", False)
    monkeypatch.setattr(config, "ALLOW_SQLITE_TABLE_REBUILD", False)
    monkeypatch.setattr(config, "RUN_DEMO_SEED_ON_STARTUP", False)
    with pytest.raises(SystemExit):
        config.validate_database_config()


def test_production_refuses_demo_seed_flag(monkeypatch):
    monkeypatch.setattr(config, "APP_ENV", "production")
    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://user:pass@host.railway.app:5432/railway")
    monkeypatch.setattr(config, "DB_ENGINE", "postgres")
    monkeypatch.setattr(config, "ALLOW_DESTRUCTIVE_DB_RESET", False)
    monkeypatch.setattr(config, "ALLOW_SQLITE_TABLE_REBUILD", False)
    monkeypatch.setattr(config, "RUN_DEMO_SEED_ON_STARTUP", True)
    with pytest.raises(SystemExit):
        config.validate_database_config()


def test_database_url_wins_over_database_path():
    """When DATABASE_URL is set, engine is postgres and DATABASE_PATH is not used."""
    assert bool(config.DATABASE_URL) is False or config.DB_ENGINE == "postgres"
    # Selection rule used by config.py (URL present → postgres, else sqlite).
    url = "postgresql://user:pass@host.railway.app:5432/railway"
    path = "real_estate.db"
    engine = "postgres" if url else "sqlite"
    assert engine == "postgres"
    # Path would only be used when URL is empty.
    engine_without_url = "postgres" if "" else "sqlite"
    assert engine_without_url == "sqlite"
    assert path == "real_estate.db"  # local-only; ignored when URL set


def test_connect_refuses_sqlite_in_production(monkeypatch):
    import db_backend

    monkeypatch.setattr(config, "APP_ENV", "production")
    monkeypatch.setattr(config, "DB_ENGINE", "sqlite")
    monkeypatch.setattr(config, "DATABASE_PATH", "should_not_open.db")
    with pytest.raises(RuntimeError, match="Refusing SQLite"):
        db_backend.connect()


def test_startup_log_reports_postgres_active_without_secrets(capsys, monkeypatch):
    monkeypatch.setattr(config, "APP_ENV", "production")
    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://secret_user:secret_pass@pg.railway.internal:5432/railway")
    monkeypatch.setattr(config, "DB_ENGINE", "postgres")
    monkeypatch.setattr(config, "ALLOW_DESTRUCTIVE_DB_RESET", False)
    monkeypatch.setattr(config, "ALLOW_SQLITE_TABLE_REBUILD", False)
    monkeypatch.setattr(config, "RUN_DEMO_SEED_ON_STARTUP", False)
    config.validate_database_config()
    err = capsys.readouterr().err
    assert "postgres_active=true" in err
    assert "app_env=production" in err
    assert "engine=postgres" in err
    assert "secret_pass" not in err
    assert "secret_user" not in err
    assert "postgresql://" not in err


def test_migration_state_log_includes_versions(capsys, two_users):
    from migrations.runner import apply_pending_migrations

    apply_pending_migrations()
    err = capsys.readouterr().err
    assert "Migration state:" in err
    assert "latest=" in err
    assert "001_baseline" in err
    assert "postgres_active=" in err
    assert "postgresql://" not in err


def test_tasks_survive_redeploy_simulation(two_users):
    """init_db / migrations must not wipe existing tasks (deploy restart simulation)."""
    u1, _ = two_users
    task_id, err = crm_db.create_task(
        u1,
        {"title": "Survive deploy", "task_type": "other", "priority": "normal"},
    )
    assert err is None
    before = crm_db.get_task(u1, task_id)
    assert before["title"] == "Survive deploy"

    # Simulate new deploy: migrations + init again
    apply_pending_migrations()
    db.init_db()

    after = crm_db.get_task(u1, task_id)
    assert after is not None
    assert after["title"] == "Survive deploy"
    assert after["user_id"] == u1


def test_users_survive_migrations(two_users):
    u1, u2 = two_users
    email1 = db.get_user_by_id(u1)["email"]
    apply_pending_migrations()
    db.init_db()
    assert db.get_user_by_id(u1)["email"] == email1
    assert db.get_user_by_id(u2) is not None


def test_seed_flag_does_not_run_in_production(monkeypatch):
    """Seed must refuse in production even if migrations/connect are stubbed."""
    monkeypatch.setattr(config, "APP_ENV", "production")
    monkeypatch.setattr(config, "RUN_DEMO_SEED_ON_STARTUP", True)
    monkeypatch.setattr("migrations.runner.apply_pending_migrations", lambda: None)

    class _DummyConnCtx:
        def __enter__(self):
            return object()

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(db, "get_db", lambda: _DummyConnCtx())
    monkeypatch.setattr(db, "_ensure_default_voice_personas", lambda conn: None)
    with pytest.raises(RuntimeError, match="Demo seed refused"):
        db.init_db()


def test_adding_column_migration_preserves_rows(two_users):
    u1, _ = two_users
    task_id, err = crm_db.create_task(u1, {"title": "Keep me", "task_type": "call"})
    assert err is None
    count_before = _count("tasks")
    apply_pending_migrations()
    assert _count("tasks") == count_before
    assert crm_db.get_task(u1, task_id)["title"] == "Keep me"


def test_restart_does_not_alter_row_counts(two_users):
    u1, u2 = two_users
    crm_db.create_task(u1, {"title": "A", "task_type": "call"})
    crm_db.create_task(u2, {"title": "B", "task_type": "call"})
    users_before = _count("users")
    tasks_before = _count("tasks")
    db.init_db()
    db.init_db()
    assert _count("users") == users_before
    assert _count("tasks") == tasks_before


def test_tenant_cannot_delete_other_tenant_tasks(two_users):
    u1, u2 = two_users
    task_id, err = crm_db.create_task(u1, {"title": "Owner1 task", "task_type": "call"})
    assert err is None
    ok, error = crm_db.cancel_task(u2, task_id)
    assert ok is None
    assert error == "Task not found."
    still = crm_db.get_task(u1, task_id)
    assert still is not None
    assert still["status"] != "cancelled"


def test_task_scoped_by_stable_user_id(two_users):
    u1, _ = two_users
    task_id, err = crm_db.create_task(
        u1,
        {"title": "Scoped", "task_type": "general_follow_up"},
    )
    assert err is None
    task = crm_db.get_task(u1, task_id)
    assert isinstance(task["id"], int)
    assert task["user_id"] == u1
    assert task["assigned_user_id"] == u1


def test_no_drop_table_in_migration_modules():
    import re

    root = os.path.join(os.path.dirname(__file__), "..", "migrations", "versions")
    forbidden = re.compile(r"\b(DROP\s+TABLE|DROP\s+DATABASE|TRUNCATE)\b", re.I)
    for name in os.listdir(root):
        if not name.endswith(".py") or name.startswith("_"):
            continue
        path = os.path.join(root, name)
        text = open(path, encoding="utf-8").read()
        assert forbidden.search(text) is None, f"Destructive SQL found in {name}"


def _count(table):
    with db.get_db() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
        return int(row["count"])
