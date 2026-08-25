"""Database connection backend: PostgreSQL (production) or SQLite (dev/test only)."""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from urllib.parse import urlparse

import config

_PLACEHOLDER = re.compile(r"\?")
_INSERT_TABLE = re.compile(
    r"^\s*INSERT\s+INTO\s+(?:ONLY\s+)?(?P<table>(?:\"[^\"]+\"|[A-Za-z_][\w$]*)(?:\.(?:\"[^\"]+\"|[A-Za-z_][\w$]*))?)",
    re.IGNORECASE,
)


class CompatCursor:
    def __init__(self, cursor, lastrowid=None):
        self._cursor = cursor
        self.lastrowid = lastrowid

    @property
    def rowcount(self):
        return getattr(self._cursor, "rowcount", -1)

    def fetchone(self):
        row = self._cursor.fetchone()
        return _normalize_row(row)

    def fetchall(self):
        return [_normalize_row(r) for r in self._cursor.fetchall()]

    def __iter__(self):
        for row in self._cursor:
            yield _normalize_row(row)


class CompatConnection:
    def __init__(self, raw, engine: str):
        self._raw = raw
        self.engine = engine  # "postgres" | "sqlite"
        self._pg_id_sequence_cache = {}

    def execute(self, sql, params=None):
        if isinstance(params, list):
            params = tuple(params)
        sql_exec = sql
        if self.engine == "postgres":
            # Only convert placeholders when bind params are provided.
            # DDL with params=() previously went through pyformat binding and did
            # not reliably persist CREATE TABLE in production.
            if params:
                sql_exec = _PLACEHOLDER.sub("%s", sql)
                cur = self._raw.execute(sql_exec, params)
            else:
                cur = self._raw.execute(sql)
            lastrowid = None
            if params and self._postgres_insert_has_id_sequence(sql):
                try:
                    row = self._raw.execute("SELECT lastval() AS id").fetchone()
                    if row is not None:
                        lastrowid = row["id"] if isinstance(row, dict) else row[0]
                except Exception:
                    lastrowid = None
            return CompatCursor(cur, lastrowid=lastrowid)

        if params is None:
            params = ()
        cur = self._raw.execute(sql_exec, params)
        return CompatCursor(cur, lastrowid=getattr(cur, "lastrowid", None))

    def _postgres_insert_has_id_sequence(self, sql: str) -> bool:
        match = _INSERT_TABLE.match(sql or "")
        if not match:
            return False
        table = match.group("table")
        cached = self._pg_id_sequence_cache.get(table)
        if cached is not None:
            return cached
        try:
            row = self._raw.execute(
                """
                SELECT column_default AS default_value
                FROM information_schema.columns
                WHERE table_name = %s
                  AND column_name = 'id'
                  AND table_schema = ANY(current_schemas(false))
                ORDER BY CASE WHEN table_schema = current_schema() THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (_unqualified_table_name(table),),
            ).fetchone()
            default_value = row["default_value"] if isinstance(row, dict) else row[0]
            has_sequence = bool(default_value and "nextval(" in str(default_value).lower())
        except Exception:
            has_sequence = False
        self._pg_id_sequence_cache[table] = has_sequence
        return has_sequence

    def commit(self):
        self._raw.commit()

    def rollback(self):
        if hasattr(self._raw, "rollback"):
            self._raw.rollback()

    def close(self):
        self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()
        return False


def _normalize_row(row):
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    if isinstance(row, sqlite3.Row):
        return dict(row)
    # tuple fallback
    return row


def _unqualified_table_name(table: str) -> str:
    name = (table or "").split(".")[-1]
    if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
        return name[1:-1].replace('""', '"')
    return name


def connect():
    """Open a CompatConnection for the configured backend.

    When DATABASE_URL is set, config.DB_ENGINE is postgres and DATABASE_PATH
    is ignored (development-only SQLite path).
    """
    if config.DB_ENGINE == "postgres":
        import psycopg
        from psycopg.rows import dict_row

        # Never fall back to SQLite when Postgres is configured.
        raw = psycopg.connect(config.DATABASE_URL, row_factory=dict_row)
        return CompatConnection(raw, "postgres")

    if config.APP_ENV in {"production", "staging"}:
        raise RuntimeError(
            "Refusing SQLite connection in production/staging. "
            "Set DATABASE_URL to Railway PostgreSQL."
        )

    raw = sqlite3.connect(config.DATABASE_PATH)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    return CompatConnection(raw, "sqlite")


@contextmanager
def connection():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def database_host_is_ephemeral(url: str) -> bool:
    if not url:
        return True
    lowered = url.lower().strip()
    if lowered.startswith("sqlite:"):
        return True
    parsed = urlparse(lowered)
    host = (parsed.hostname or "").lower()
    if host in {"", "localhost", "127.0.0.1", "0.0.0.0", "::1"}:
        return True
    if host.endswith(".local") or host.endswith(".internal") and "railway" not in host:
        # Railway private networking uses *.railway.internal — allow that.
        if "railway.internal" in host:
            return False
        if host.endswith(".local"):
            return True
    if "/tmp" in lowered or "temp" in (parsed.path or "").lower():
        return True
    return False


def looks_like_test_database(url: str) -> bool:
    lowered = (url or "").lower()
    markers = ("_test", "/test", "test_", "pytest", "ci_test")
    return any(m in lowered for m in markers)


def bind_bool(value):
    """Return an engine-safe boolean bind value (bool for Postgres, 0/1 for SQLite)."""
    flag = bool(value)
    if config.DB_ENGINE == "postgres":
        return flag
    return 1 if flag else 0


def sql_is_true(column: str) -> str:
    """Portable SQL fragment that is true when a boolean/0-1 column is enabled."""
    if config.DB_ENGINE == "postgres":
        return f"{column} IS TRUE"
    return f"{column} = 1"
