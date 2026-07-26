"""Apply forward-only pending migrations. Never resets or downgrades."""

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


def _now():
    return datetime.now(timezone.utc).isoformat()


def _ensure_migrations_table(conn):
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


def _stamp(conn, version: str):
    conn.execute(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        (version, _now()),
    )


def _clear_migration_bookkeeping(conn):
    """Delete only schema_migrations rows — never DROP user tables or data."""
    conn.execute("DELETE FROM schema_migrations")


def _repair_false_baseline_stamp(conn, applied):
    """If 001 is stamped but base tables are missing, clear bookkeeping and rerun.

    Safe for the empty Railway Postgres cutover where a broken deploy recorded
    001_baseline without creating users. Does not DROP TABLES or DELETE app data.
    """
    if "001_baseline" not in applied:
        return applied

    missing = missing_baseline_tables(conn)
    # schema_migrations itself is allowed to exist; ignore it for "empty" check.
    critical_missing = [t for t in missing if t != "schema_migrations"]
    if not critical_missing:
        return applied

    logger.error(
        "False migration stamp detected: 001_baseline is recorded but tables "
        "are missing (%s). Clearing migration bookkeeping only, then re-running "
        "baseline. No user tables will be dropped.",
        ", ".join(critical_missing),
    )
    print(
        "FATAL-REPAIR: 001_baseline was marked applied without baseline tables "
        f"({', '.join(critical_missing)}). Clearing schema_migrations rows and "
        "re-applying baseline.",
        file=sys.stderr,
    )
    _clear_migration_bookkeeping(conn)
    conn.commit()
    return set()


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


def _apply_migration(conn, mod):
    """Run one migration in a transaction; stamp only after success (+ baseline verify)."""
    version = mod.VERSION
    try:
        # Explicit transaction boundary (psycopg: BEGIN when not in a txn).
        try:
            conn.execute("BEGIN")
        except Exception:
            # Already in a transaction — continue.
            pass

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
        if conn.engine == "postgres" and hasattr(conn._raw, "autocommit"):
            conn._raw.autocommit = False

        _acquire_lock(conn)
        _ensure_migrations_table(conn)
        conn.commit()

        applied = _applied_versions(conn)
        applied = _repair_false_baseline_stamp(conn, applied)

        # Never skip 001 when baseline tables are missing (even if stamped).
        if "001_baseline" in applied:
            missing = [t for t in missing_baseline_tables(conn) if t != "schema_migrations"]
            if missing:
                raise MigrationBootstrapError(
                    "001_baseline is marked applied but required tables are still "
                    f"missing: {', '.join(missing)}"
                )

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

            if version == "001_baseline":
                # Belt-and-suspenders before 002 runs.
                verify_baseline_tables(conn)

        # Final guard before app boot.
        verify_baseline_tables(conn)
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
