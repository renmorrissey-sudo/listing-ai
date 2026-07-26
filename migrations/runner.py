"""Apply forward-only pending migrations. Never resets or downgrades."""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from importlib import import_module

import config
from db_backend import connect

logger = logging.getLogger(__name__)

# Ordered, reviewed migration modules (additive only).
MIGRATION_MODULES = [
    "migrations.versions.001_baseline",
    "migrations.versions.002_safe_additive_columns",
    "migrations.versions.003_user_business_profile",
]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _ensure_migrations_table(conn):
    if conn.engine == "postgres":
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
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
        row = conn.execute(
            """
            SELECT 1 AS ok
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = ?
            LIMIT 1
            """,
            (table_name,),
        ).fetchone()
        return bool(row)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return bool(row)


def _stamp(conn, version: str):
    conn.execute(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        (version, _now()),
    )


def _acquire_lock(conn):
    if conn.engine == "postgres":
        conn.execute("SELECT pg_advisory_lock(?)", (872364001,))


def _release_lock(conn):
    if conn.engine == "postgres":
        conn.execute("SELECT pg_advisory_unlock(?)", (872364001,))


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


def apply_pending_migrations():
    """Run only unapplied forward migrations. Safe for production startup."""
    if config.ALLOW_DESTRUCTIVE_DB_RESET and config.APP_ENV in {"production", "staging"}:
        print("FATAL: Refusing to run migrations with ALLOW_DESTRUCTIVE_DB_RESET in production/staging.", file=sys.stderr)
        sys.exit(1)

    conn = connect()
    try:
        _acquire_lock(conn)
        _ensure_migrations_table(conn)
        applied = _applied_versions(conn)

        # Legacy SQLite/Postgres DBs created before schema_migrations: stamp baseline
        # without rebuilding so existing paid-user rows are preserved.
        if "001_baseline" not in applied and _table_exists(conn, "users") and _table_exists(conn, "tasks"):
            logger.info("Stamping 001_baseline on existing database (data preserved).")
            _stamp(conn, "001_baseline")
            applied.add("001_baseline")
            conn.commit()

        for module_name in MIGRATION_MODULES:
            mod = import_module(module_name)
            version = mod.VERSION
            if version in applied:
                continue
            logger.info("Applying migration %s", version)
            if conn.engine == "postgres":
                mod.upgrade_postgres(conn)
            else:
                mod.upgrade_sqlite(conn)
            _stamp(conn, version)
            conn.commit()
            applied.add(version)
            logger.info("Applied migration %s", version)

        log_migration_state(applied)
    finally:
        try:
            _release_lock(conn)
        except Exception:
            pass
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config.validate_database_config()
    apply_pending_migrations()
    print("Migrations complete.", file=sys.stderr)


if __name__ == "__main__":
    main()
