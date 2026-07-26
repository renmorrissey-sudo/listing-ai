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
    monkeypatch.setattr(config, "APP_ENV", "production")
    monkeypatch.setattr(config, "RUN_DEMO_SEED_ON_STARTUP", True)
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
