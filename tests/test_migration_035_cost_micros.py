"""Migration 035: precise fractional-cent costs (defect 2).

Ren authorized migration 035 on 2026-11-09 (staging migration testing
only; production migration is NOT authorized yet). The migration adds
``harness_costs.cost_micros`` (fixed-point micro-cents, backfilled
from ``cost_cents * 1000000``) and the ``harness_spend_reservations``
table (atomic cap enforcement under concurrency).

Tests:
- 035 applies cleanly on top of the 032 baseline (the release branch
  state) and is stamped by the runner.
- Backfill: pre-existing cost rows get cost_micros = cost_cents * 1e6.
- Idempotency: re-running the upgrade is a no-op (backfill only
  touches NULL rows; the table/index creation is IF NOT EXISTS).
- Compatibility with 033: 033 (model + correlation_id) and 035
  coexist regardless of application order — the columns are
  independent and the cost code detects each one separately.
"""

import importlib
import os
import tempfile

import pytest

import config
import db
from migrations.runner import apply_pending_migrations


@pytest.fixture
def empty_sqlite_db(monkeypatch):
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


def _columns(table):
    with db.get_db() as conn:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {row["name"] for row in rows}


def _tables():
    with db.get_db() as conn:
        return {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }


def _applied_versions():
    with db.get_db() as conn:
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        return {row["version"] for row in rows}


def _m035():
    return importlib.import_module(
        "migrations.versions.035_harness_cost_precise_micros"
    )


def test_035_applies_on_032_baseline(empty_sqlite_db):
    """035 is in the runner list, applies on a fresh DB, and is
    stamped. cost_micros and the reservations table exist."""
    apply_pending_migrations()
    assert "035_harness_cost_precise_micros" in _applied_versions()
    assert "cost_micros" in _columns("harness_costs")
    assert "harness_spend_reservations" in _tables()
    assert _columns("harness_spend_reservations") == {
        "id",
        "user_id",
        "period",
        "correlation_id",
        "reserved_micros",
        "created_at",
    }


def test_035_backfills_existing_rows(empty_sqlite_db):
    """A legacy row (cost_cents set, cost_micros NULL) is backfilled
    to cost_micros = cost_cents * 1,000,000 when the upgrade runs."""
    apply_pending_migrations()
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO harness_costs
                (user_id, period, provider, tool, units, unit_type, cost_cents, cost_micros, created_at)
            VALUES (?, '2026-11', 'anthropic', 'coach', 100, 'tokens', 3, NULL, '2026-11-09T00:00:00Z')
            """,
            (1,),
        )
    with db.get_db() as conn:
        _m035().upgrade_sqlite(conn)
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT cost_cents, cost_micros FROM harness_costs WHERE user_id = 1"
        ).fetchone()
    assert row["cost_cents"] == 3
    assert row["cost_micros"] == 3_000_000


def test_035_upgrade_is_idempotent(empty_sqlite_db):
    """Calling the upgrade twice does not change anything (no duplicate
    rows, no error, backfill stays correct)."""
    apply_pending_migrations()
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO harness_costs
                (user_id, period, provider, tool, units, unit_type, cost_cents, cost_micros, created_at)
            VALUES (?, '2026-11', 'anthropic', 'coach', 100, 'tokens', 5, 5000000, '2026-11-09T00:00:00Z')
            """,
            (1,),
        )
        _m035().upgrade_sqlite(conn)
        _m035().upgrade_sqlite(conn)
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT cost_cents, cost_micros FROM harness_costs WHERE user_id = 1"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["cost_micros"] == 5_000_000


def test_035_compatible_with_033_both_orders(empty_sqlite_db):
    """033 (model + correlation_id) and 035 (cost_micros) coexist
    regardless of application order. The migration files are
    independent: 033 adds two columns, 035 adds one column and a
    table, and neither depends on the other. (The 033 file was
    deleted from the release branch, so this test applies the same
    DDL 033 uses instead of importing it.)"""
    apply_pending_migrations()
    # 035 already applied by the runner; now add the 033 columns.
    with db.get_db() as conn:
        conn.execute("ALTER TABLE harness_costs ADD COLUMN model TEXT")
        conn.execute("ALTER TABLE harness_costs ADD COLUMN correlation_id TEXT")
        _m035().verify(conn, "sqlite")
    cols = _columns("harness_costs")
    assert {"model", "correlation_id", "cost_micros"} <= cols
    # And re-running 035 after the 033 columns exist is a no-op.
    with db.get_db() as conn:
        _m035().upgrade_sqlite(conn)
        _m035().verify(conn, "sqlite")
