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

    def __init__(self, rowcount=0):
        self._rows = []
        self._rowcount = rowcount

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
        # psycopg: number of rows affected by the last INSERT/UPDATE/DELETE.
        return self._rowcount


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
    """Fake psycopg connection modelling lastval() session semantics.

    Real Postgres: SELECT lastval() errors ("lastval is not yet defined in
    this session") when no sequence value has been generated yet in the
    session, and that error aborts the transaction — every later statement
    then fails with InFailedSqlTransaction until rollback.
    """

    def __init__(self, id_defaults=None, insert_rowcount=1):
        self.id_defaults = id_defaults or {}
        self.insert_rowcount = insert_rowcount
        self.statements = []
        self._lastval_defined = False
        self._aborted = False

    def execute(self, sql, params=None):
        if self._aborted:
            raise Exception(
                "current transaction is aborted, commands ignored until "
                "end of transaction block"
            )
        self.statements.append((sql, params))
        cur = _PgFormatValidatingCursor()
        if "information_schema.columns" in sql:
            table = params[0]
            if table in self.id_defaults:
                cur._rows = [{"default_value": self.id_defaults[table]}]
        elif sql.startswith("SELECT lastval()"):
            if not self._lastval_defined:
                self._aborted = True
                raise Exception("lastval is not yet defined in this session")
            cur._rows = [{"id": 42}]
        elif sql.strip().upper().startswith("INSERT"):
            cur._rowcount = self.insert_rowcount
            if self.insert_rowcount > 0:
                self._lastval_defined = True
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
    raw = _PgLastrowidRaw()
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
    raw = _PgLastrowidRaw(
        id_defaults={"users": "nextval('users_id_seq'::regclass)"}
    )
    conn = CompatConnection(raw, "postgres")

    cur = conn.execute(
        "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
        ("agent@example.com", "hash", "2026-08-25T13:00:00+00:00"),
    )

    assert cur.lastrowid == 42
    assert any(sql.startswith("SELECT lastval()") for sql, _ in raw.statements)


def test_postgres_insert_select_zero_rows_does_not_call_lastval():
    """029 regression: seed INSERT ... SELECT inserts zero rows on an empty DB.

    lastval() errors when no sequence value was generated yet in the
    session ("lastval is not yet defined in this session") and aborts the
    transaction; the error is swallowed in CompatConnection.execute, so the
    failure only surfaces later at _stamp as InFailedSqlTransaction.
    CompatConnection must not call lastval() when nothing was inserted,
    and the transaction must stay usable afterwards.
    """
    raw = _PgLastrowidRaw(
        id_defaults={"harness_settings": "nextval('harness_settings_id_seq'::regclass)"},
        insert_rowcount=0,
    )
    conn = CompatConnection(raw, "postgres")

    cur = conn.execute(
        """
        INSERT INTO harness_settings
            (user_id, mode, paused, ai_enabled, created_at, updated_at)
        SELECT id, 'drafts_only', 0, 1, ?, ?
        FROM users
        WHERE NOT EXISTS (
            SELECT 1 FROM harness_settings hs WHERE hs.user_id = users.id
        )
        ON CONFLICT (user_id) DO NOTHING
        """,
        ("2026-11-08T00:00:00+00:00", "2026-11-08T00:00:00+00:00"),
    )

    assert cur.lastrowid is None
    assert cur.rowcount == 0
    assert not any(sql.startswith("SELECT lastval()") for sql, _ in raw.statements)
    # Transaction not poisoned: the runner's follow-up _stamp INSERT still runs.
    conn.execute(
        "INSERT INTO schema_migrations (version) VALUES (?)",
        ("029_harness_foundation",),
    )


def test_fake_postgres_lastval_without_insert_aborts_transaction():
    """Harness fidelity: lastval() with no prior insert errors and poisons
    the transaction, as real Postgres does (InFailedSqlTransaction)."""
    raw = _PgLastrowidRaw(insert_rowcount=0)
    with pytest.raises(Exception, match="lastval is not yet defined"):
        raw.execute("SELECT lastval() AS id")
    with pytest.raises(Exception, match="current transaction is aborted"):
        raw.execute("SELECT 1")


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
