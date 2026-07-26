"""Apply forward-only pending migrations. Never resets or downgrades.

The runner exclusively owns PostgreSQL transaction boundaries:
  begin (implicit) → migration DDL/DML → verify → stamp → commit
On failure: rollback and do not record the migration.
Individual migration modules must not toggle autocommit or commit/rollback.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from importlib import import_module

import config
from db_backend import connect

logger = logging.getLogger(__name__)


class MigrationBootstrapError(RuntimeError):
    """Baseline schema missing or migration bookkeeping is inconsistent."""


# Ordered, reviewed migration modules (additive only).
MIGRATION_MODULES = [
    "migrations.versions.001_baseline",
    "migrations.versions.002_safe_additive_columns",
    "migrations.versions.003_user_business_profile",
]

# Required after 001_baseline. App has no separate accounts/tenants/subscriptions
# or SMS conversations tables — those concepts live on users / leads / sms_messages.
REQUIRED_BASELINE_TABLES = (
    "schema_migrations",
    "users",
    "voice_personas",
    "voice_calls",
    "tool_usage",
    "sms_messages",
    "leads",
    "lead_follow_ups",
    "lead_insights",
    "lead_activities",
    "tasks",
    "appointments",
    "needs_attention",
    "notifications",
)

APP_DATA_TABLES = tuple(t for t in REQUIRED_BASELINE_TABLES if t != "schema_migrations")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _ensure_migrations_table(conn):
    if conn.engine == "postgres":
        from migrations.pg_ddl import pg_execute

        pg_execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """,
        )
    else:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )


def _applied_versions(conn):
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version ASC").fetchall()
    return {r["version"] if isinstance(r, dict) else r[0] for r in rows}


def _table_exists(conn, table_name: str) -> bool:
    if conn.engine == "postgres":
        from migrations.pg_ddl import pg_table_exists

        return pg_table_exists(conn, table_name)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return bool(row)


def missing_baseline_tables(conn):
    return [name for name in REQUIRED_BASELINE_TABLES if not _table_exists(conn, name)]


def verify_baseline_tables(conn):
    """Fail loudly if 001 did not create required tables."""
    missing = missing_baseline_tables(conn)
    if not missing:
        return
    raise MigrationBootstrapError(
        "Migration bootstrap failed: required baseline tables are missing after "
        f"001_baseline: {', '.join(missing)}. "
        "Refusing to run additive migrations (002+)."
    )


def _database_is_empty_of_app_data(conn) -> bool:
    """True when no application base tables exist (or they exist but have zero rows)."""
    existing_app_tables = [t for t in APP_DATA_TABLES if _table_exists(conn, t)]
    if not existing_app_tables:
        return True
    for table in existing_app_tables:
        row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
        count = int(row["c"] if isinstance(row, dict) else row[0])
        if count > 0:
            return False
    return True


def _stamp(conn, version: str):
    if conn.engine == "postgres":
        from migrations.pg_ddl import pg_execute

        pg_execute(
            conn,
            "INSERT INTO schema_migrations (version, applied_at) VALUES (%s, %s)",
            (version, _now()),
        )
    else:
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, _now()),
        )


def _clear_migration_bookkeeping(conn):
    """Delete only schema_migrations rows — never DROP user tables or data."""
    if conn.engine == "postgres":
        from migrations.pg_ddl import pg_execute

        pg_execute(conn, "DELETE FROM schema_migrations")
    else:
        conn.execute("DELETE FROM schema_migrations")


def _repair_false_baseline_stamp(conn, applied):
    """If 001 is stamped but base tables are missing on an empty DB, clear ledger."""
    if "001_baseline" not in applied:
        return applied

    missing = [t for t in missing_baseline_tables(conn) if t != "schema_migrations"]
    if not missing:
        return applied

    if not _database_is_empty_of_app_data(conn):
        raise MigrationBootstrapError(
            "001_baseline is marked applied but required tables are missing "
            f"({', '.join(missing)}), and the database is not empty. "
            "Refusing automatic repair to avoid damaging existing data."
        )

    logger.error(
        "False migration stamp on empty database: 001_baseline recorded but tables "
        "missing (%s). Clearing schema_migrations only, then re-running baseline.",
        ", ".join(missing),
    )
    print(
        "REPAIR: empty database has false 001_baseline stamp; clearing "
        "schema_migrations rows and re-applying baseline.",
        file=sys.stderr,
    )
    _clear_migration_bookkeeping(conn)
    conn.commit()
    return set()


def _acquire_lock(conn):
    """Session-level advisory lock (survives commits; does not require autocommit)."""
    if conn.engine == "postgres":
        from migrations.pg_ddl import pg_execute

        pg_execute(conn, "SELECT pg_advisory_lock(%s)", (872364001,))
        conn.commit()


def _release_lock(conn):
    if conn.engine == "postgres":
        from migrations.pg_ddl import pg_execute

        try:
            pg_execute(conn, "SELECT pg_advisory_unlock(%s)", (872364001,))
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass


def _expected_versions():
    versions = []
    for module_name in MIGRATION_MODULES:
        mod = import_module(module_name)
        versions.append(mod.VERSION)
    return versions


def log_migration_state(applied):
    """Safe always-on summary: env, engine, postgres flag, migration versions. No secrets."""
    expected = _expected_versions()
    applied_ordered = [v for v in expected if v in applied]
    pending = [v for v in expected if v not in applied]
    postgres_active = config.DB_ENGINE == "postgres" and bool(config.DATABASE_URL)
    latest = applied_ordered[-1] if applied_ordered else "none"
    message = (
        "Migration state: "
        f"app_env={config.APP_ENV} engine={config.DB_ENGINE} "
        f"postgres_active={'true' if postgres_active else 'false'} "
        f"latest={latest} "
        f"applied={','.join(applied_ordered) or 'none'} "
        f"pending={','.join(pending) or 'none'}"
    )
    logger.info(message)
    print(message, file=sys.stderr)


def _verify_baseline_on_fresh_connection():
    """Post-commit check: tables must be visible on a new connection."""
    verify_conn = connect()
    try:
        verify_baseline_tables(verify_conn)
    finally:
        verify_conn.close()


def _apply_migration(conn, mod):
    """One migration, one transaction: DDL → verify → stamp → commit (or rollback)."""
    version = mod.VERSION
    try:
        if conn.engine == "postgres":
            mod.upgrade_postgres(conn)
        else:
            mod.upgrade_sqlite(conn)

        if version == "001_baseline":
            verify_baseline_tables(conn)

        _stamp(conn, version)
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise

    # After successful commit only — other sessions must see baseline tables.
    if version == "001_baseline":
        _verify_baseline_on_fresh_connection()


def apply_pending_migrations():
    """Run only unapplied forward migrations. Safe for production startup."""
    if config.ALLOW_DESTRUCTIVE_DB_RESET and config.APP_ENV in {"production", "staging"}:
        print(
            "FATAL: Refusing to run migrations with ALLOW_DESTRUCTIVE_DB_RESET "
            "in production/staging.",
            file=sys.stderr,
        )
        sys.exit(1)

    conn = connect()
    try:
        # Runner owns transaction mode. Never leave this True during migrations.
        if conn.engine == "postgres" and hasattr(conn._raw, "autocommit"):
            if conn._raw.autocommit:
                conn._raw.autocommit = False

        _acquire_lock(conn)
        _ensure_migrations_table(conn)
        conn.commit()

        applied = _applied_versions(conn)
        # Reading applied versions opens a transaction; commit before repair/apply.
        conn.commit()

        applied = _repair_false_baseline_stamp(conn, applied)

        if "001_baseline" in applied:
            missing = [t for t in missing_baseline_tables(conn) if t != "schema_migrations"]
            if missing:
                raise MigrationBootstrapError(
                    "001_baseline is marked applied but required tables are still "
                    f"missing: {', '.join(missing)}"
                )
            conn.commit()

        for module_name in MIGRATION_MODULES:
            mod = import_module(module_name)
            version = mod.VERSION
            if version in applied:
                continue
            logger.info("Applying migration %s", version)
            print(f"Applying migration {version}", file=sys.stderr)
            _apply_migration(conn, mod)
            applied.add(version)
            logger.info("Applied migration %s", version)
            print(f"Applied migration {version}", file=sys.stderr)

        verify_baseline_tables(conn)
        conn.commit()
        log_migration_state(applied)
    except MigrationBootstrapError:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            _release_lock(conn)
        except Exception:
            pass
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config.validate_database_config()
    try:
        apply_pending_migrations()
    except MigrationBootstrapError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        sys.exit(1)
    print("Migrations complete.", file=sys.stderr)


if __name__ == "__main__":
    main()
