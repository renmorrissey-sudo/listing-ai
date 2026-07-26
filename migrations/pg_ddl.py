"""Raw PostgreSQL DDL helpers for migrations (bypass CompatConnection quirks).

These helpers execute SQL on the underlying psycopg connection but must never
toggle autocommit or manage transactions — that is owned by migrations.runner.
"""

from __future__ import annotations


def pg_execute(conn, sql: str, params=None):
    """Execute SQL on the underlying psycopg connection within the current txn."""
    raw = getattr(conn, "_raw", conn)
    if params is None:
        cur = raw.execute(sql)
    else:
        cur = raw.execute(sql, params)
    # Consume/close so the connection is ready for the next command.
    try:
        cur.fetchall()
    except Exception:
        pass
    try:
        cur.close()
    except Exception:
        pass
    return cur


def pg_table_exists(conn, table_name: str) -> bool:
    """Return True only for a real base table in public schema."""
    raw = getattr(conn, "_raw", conn)
    cur = raw.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relname = %s
              AND c.relkind = 'r'
        ) AS ok
        """,
        (table_name,),
    )
    row = cur.fetchone()
    try:
        cur.close()
    except Exception:
        pass
    if row is None:
        return False
    if isinstance(row, dict):
        return bool(row.get("ok"))
    return bool(row[0])


def pg_index_exists(conn, index_name: str) -> bool:
    raw = getattr(conn, "_raw", conn)
    cur = raw.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relname = %s
              AND c.relkind = 'i'
        ) AS ok
        """,
        (index_name,),
    )
    row = cur.fetchone()
    try:
        cur.close()
    except Exception:
        pass
    if row is None:
        return False
    if isinstance(row, dict):
        return bool(row.get("ok"))
    return bool(row[0])
