"""Database connection backend: PostgreSQL (production) or SQLite (dev/test only)."""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from urllib.parse import urlparse

import config

_PLACEHOLDER = re.compile(r"\?")


class CompatCursor:
    def __init__(self, cursor, lastrowid=None):
        self._cursor = cursor
        self.lastrowid = lastrowid

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

    def execute(self, sql, params=None):
        if params is None:
            params = ()
        elif isinstance(params, list):
            params = tuple(params)
        sql_exec = sql
        if self.engine == "postgres":
            sql_exec = _PLACEHOLDER.sub("%s", sql)
            cur = self._raw.execute(sql_exec, params)
            lastrowid = None
            stripped = sql_exec.lstrip().upper()
            if stripped.startswith("INSERT"):
                try:
                    row = self._raw.execute("SELECT lastval() AS id").fetchone()
                    if row is not None:
                        lastrowid = row["id"] if isinstance(row, dict) else row[0]
                except Exception:
                    lastrowid = None
            return CompatCursor(cur, lastrowid=lastrowid)

        cur = self._raw.execute(sql_exec, params)
        return CompatCursor(cur, lastrowid=getattr(cur, "lastrowid", None))

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
