"""Postgres-sensitive regression: LIKE wildcards must be bound values.

db_backend.CompatConnection converts `?` → `%s` when params are present.
A literal `%` in the SQL text (e.g. `LIKE '%' || ?` or `LIKE 'external:%'`)
then breaks psycopg's pyformat parser — the production failure mode for
shared-number inbound SMS routing (find_lead_owner_by_phone) and the
external_only filter_leads path.

These tests run without a live Postgres instance by driving the real
functions through CompatConnection(engine="postgres") with a raw connection
that applies the same %-format check psycopg uses.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from db_backend import CompatConnection


class _PgFormatValidatingCursor:
    """Mimic psycopg: pyformat SQL with an unescaped literal % raises ValueError."""

    def __init__(self):
        self._rows = []

    def execute(self, sql, params=None):
        if params:
            # Same failure class as production: unsupported format character "'".
            sql % tuple(params)
        return self

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    @property
    def rowcount(self):
        return 0


class _PgFormatValidatingRaw:
    def execute(self, sql, params=None):
        cur = _PgFormatValidatingCursor()
        cur.execute(sql, params)
        return cur

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class _PgLastrowidRaw:
    def __init__(self, sequences=None):
        self.sequences = sequences or {}
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        cur = _PgFormatValidatingCursor()
        if sql.startswith("SELECT pg_get_serial_sequence"):
            table = params[0]
            cur._rows = [{"seq": self.sequences.get(table)}]
        elif sql.startswith("SELECT lastval()"):
            cur._rows = [{"id": 42}]
        return cur

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@contextmanager
def _postgres_format_db():
    conn = CompatConnection(_PgFormatValidatingRaw(), "postgres")
    try:
        yield conn
    finally:
        conn.close()


def test_legacy_like_sql_fails_postgres_placeholder_conversion():
    """Guard: the pre-fix SQL shapes must fail pyformat (documents the bug)."""
    old_owner_sql = (
        "SELECT user_id, phone_number FROM leads "
        "WHERE phone_number IS NOT NULL AND phone_number LIKE '%' || %s "
        "ORDER BY updated_at DESC"
    )
    with pytest.raises(ValueError, match="unsupported format character"):
        old_owner_sql % ("1234",)

    old_external_sql = (
        "SELECT l.* FROM leads l WHERE l.user_id = %s "
        "AND (l.external_source_id IS NOT NULL OR l.source LIKE 'external:%') "
        "ORDER BY l.updated_at DESC LIMIT %s"
    )
    with pytest.raises(ValueError, match="unsupported format character"):
        old_external_sql % (1, 200)


def test_postgres_insert_without_id_sequence_does_not_call_lastval():
    raw = _PgLastrowidRaw(sequences={"sms_worker_heartbeats": None})
    conn = CompatConnection(raw, "postgres")

    cur = conn.execute(
        """
        INSERT INTO sms_worker_heartbeats
            (worker_id, status, last_seen_at, metadata_json)
        VALUES (?, ?, ?, ?)
        """,
        ("worker-1", "running", "2026-08-25T13:00:00+00:00", "{}"),
    )

    assert cur.lastrowid is None
    assert not any(sql.startswith("SELECT lastval()") for sql, _ in raw.statements)


def test_postgres_insert_with_id_sequence_still_sets_lastrowid():
    raw = _PgLastrowidRaw(sequences={"users": "public.users_id_seq"})
    conn = CompatConnection(raw, "postgres")

    cur = conn.execute(
        "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
        ("agent@example.com", "hash", "2026-08-25T13:00:00+00:00"),
    )

    assert cur.lastrowid == 42
    assert any(sql.startswith("SELECT lastval()") for sql, _ in raw.statements)


def test_find_lead_owner_by_phone_like_binding_is_postgres_safe(two_users):
    """bc2c178: literal % in LIKE broke every shared-number inbound on Postgres."""
    import db

    u1, _ = two_users
    phone = f"+1303{uuid.uuid4().int % 10_000_000:07d}"

    # Seed via real SQLite; the Postgres-format probe only needs the query to parse.
    from lead_service import upsert_crm_lead

    upsert_crm_lead(u1, phone, {"lead_name": "Pat"}, source="sms")

    with patch.object(db, "get_db", _postgres_format_db):
        # Fake cursor returns no rows — assert we survive placeholder conversion.
        owner = db.find_lead_owner_by_phone(phone)
    assert owner is None


def test_find_lead_owner_by_phone_matches_on_sqlite(two_users):
    """Behavioral: trailing-digit LIKE prefilter still finds the conversation owner."""
    import db
    from lead_service import upsert_crm_lead

    u1, u2 = two_users
    phone = f"+1720{uuid.uuid4().int % 10_000_000:07d}"
    upsert_crm_lead(u2, phone, {"lead_name": "Owner"}, source="sms")

    assert db.find_lead_owner_by_phone(phone) == u2
    assert db.find_lead_owner_by_phone(phone.replace("+1", "")) == u2
    assert db.find_lead_owner_by_phone("+19995550000") is None
    assert db.find_lead_owner_by_phone("short") is None


def test_filter_leads_external_only_like_binding_is_postgres_safe(two_users):
    """Same placeholder hazard existed on filter_leads(external_only=True)."""
    import crm_db

    u1, _ = two_users
    with patch.object(crm_db, "get_db", _postgres_format_db):
        rows = crm_db.filter_leads(u1, external_only=True, limit=50)
    assert rows == []


def test_filter_leads_external_only_matches_on_sqlite(two_users):
    """Behavioral: external_only still matches source LIKE 'external:%' and external_source_id."""
    import crm_db
    import db
    from lead_service import upsert_crm_lead

    u1, _ = two_users

    def _phone():
        return f"+1808{uuid.uuid4().int % 10_000_000:07d}"

    lid_ext, _, _ = upsert_crm_lead(
        u1, _phone(), {"lead_name": "Ext"}, source="external:zillow"
    )
    lid_plain, _, _ = upsert_crm_lead(
        u1, _phone(), {"lead_name": "Plain"}, source="sms"
    )
    lid_id, _, _ = upsert_crm_lead(
        u1, _phone(), {"lead_name": "HasExtId"}, source="manual"
    )
    with db.get_db() as conn:
        conn.execute(
            "UPDATE leads SET external_source_id = ? WHERE id = ? AND user_id = ?",
            ("src-1", lid_id, u1),
        )

    external_ids = {r["id"] for r in crm_db.filter_leads(u1, external_only=True, limit=100)}
    assert lid_ext in external_ids
    assert lid_id in external_ids
    assert lid_plain not in external_ids
