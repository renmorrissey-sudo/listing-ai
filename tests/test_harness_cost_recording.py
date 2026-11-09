"""Cost-ledger recording robustness (defect 3 remediation).

Root cause: ``_insert_cost_row`` detected the 033 schema by TRYING
the 033 INSERT and falling back on exception. That works on SQLite
(a failed statement does not abort the transaction) but NOT on
Postgres: a failed statement aborts the whole transaction, so the
fallback INSERT in the same transaction failed with "current
transaction is aborted" and the cost row was lost. Live evidence: the
staging SMS-send cost warning, and the coach cost rows never landing
(the $0.00 spend after 4 real calls).

The fix detects the schema from the live schema (PRAGMA /
information_schema) BEFORE inserting, so no statement ever fails for
a missing column. These tests pin that behavior by simulating
Postgres's transaction-abort semantics on the SQLite test DB: once a
statement fails, every later statement on that connection fails until
rollback. The old code loses the row under that simulation; the new
code records it.
"""

import pytest

import db
from harness import costs


class _PostgresLikeConnection:
    """Wrap a real connection with Postgres transaction-abort semantics.

    Any statement that fails aborts the transaction: every later
    statement on this connection raises "current transaction is
    aborted" until rollback(). Statements referencing the 033 columns
    (model / correlation_id) fail, because the test DB runs the 029
    schema. This is the exact failure mode that lost cost rows live.
    """

    def __init__(self, real):
        self._real = real
        self._aborted = False

    def __getattr__(self, name):
        return getattr(self._real, name)

    def execute(self, sql, params=None):
        if self._aborted:
            raise Exception(
                "current transaction is aborted, commands ignored "
                "until end of transaction block"
            )
        lowered = str(sql or "").lower()
        if "model" in lowered or "correlation_id" in lowered:
            # The 029 schema has neither column.
            self._aborted = True
            raise Exception('no such column: model (Postgres: column "model" does not exist)')
        return self._real.execute(sql, params) if params is not None else self._real.execute(sql)

    def rollback(self):
        self._aborted = False
        return self._real.rollback()

    def commit(self):
        self._aborted = False
        return self._real.commit()


@pytest.fixture
def postgres_like_db(monkeypatch):
    """Route db.get_db() through a Postgres-abort-simulating wrapper."""
    import contextlib

    real_get_db = db.get_db

    @contextlib.contextmanager
    def _wrapped():
        with real_get_db() as real_conn:
            yield _PostgresLikeConnection(real_conn)

    monkeypatch.setattr(db, "get_db", _wrapped)
    yield


def test_record_cost_survives_postgres_abort_semantics(
    two_users, postgres_like_db
):
    """Defect 3: on a Postgres-like connection, recording a cost row
    must succeed even though the 033 columns are absent — the code
    must detect the schema first, not fail-and-retry inside the
    aborted transaction."""
    u1, _ = two_users
    row_id = costs.record_cost(
        u1, provider="anthropic", tool="sms_coach", units=1200,
        unit_type="tokens", cost_cents=1, model="claude-sonnet-5",
        correlation_id="coach:123",
    )
    assert row_id is not None
    rows = costs.list_costs(u1)
    assert len(rows) == 1
    assert rows[0]["cost_cents"] == 1
    assert rows[0]["units"] == 1200


def test_record_sms_send_records_row_on_postgres_like_connection(
    two_users, postgres_like_db
):
    """The live failure path: a successful mock send's cost record
    must land (the staging warning came from this call)."""
    u1, _ = two_users
    row_id = costs.record_sms_send(
        u1, provider="mock", tool="sms_ai_reply",
        result={"status": "mock_sent", "provider_message_id": "mock-1"},
        correlation_id="ai-reply:424242",
    )
    assert row_id is not None
    rows = costs.list_costs(u1)
    assert len(rows) == 1
    assert rows[0]["tool"] == "sms_ai_reply"
    assert rows[0]["cost_cents"] == 0  # mock sends cost nothing


def test_record_cost_once_writes_on_029_schema(two_users, postgres_like_db):
    """record_cost_once must also survive: its correlation_id dedup
    SELECT must not run when the column does not exist."""
    u1, _ = two_users
    row_id = costs.record_cost_once(
        u1, provider="vapi", tool="vapi_call_duration", units=2,
        unit_type="minutes", cost_cents=20, correlation_id="vapi-call-1",
    )
    assert row_id is not None
    assert len(costs.list_costs(u1)) == 1


def test_schema_detection_reports_029_columns(two_users):
    """The detection helper reports False on the 029 schema and True
    once the 033 columns are added (cache cleared between). The added
    columns are dropped again so the shared test DB stays on 029."""
    costs._COSTS_COLUMNS_CACHE.clear()
    with db.get_db() as conn:
        assert costs._harness_costs_has_extended_columns(conn) is False
    # Add the 033 columns the way migration 033 does, then re-detect.
    try:
        with db.get_db() as conn:
            conn.execute("ALTER TABLE harness_costs ADD COLUMN model TEXT")
            conn.execute("ALTER TABLE harness_costs ADD COLUMN correlation_id TEXT")
        costs._COSTS_COLUMNS_CACHE.clear()
        with db.get_db() as conn:
            assert costs._harness_costs_has_extended_columns(conn) is True
        # And a row recorded with the columns present stores them.
        u1, _ = two_users
        row_id = costs.record_cost(
            u1, provider="anthropic", tool="sms_coach", units=100,
            unit_type="tokens", cost_cents=2, model="claude-sonnet-5",
            correlation_id="coach:999",
        )
        assert row_id is not None
        rows = costs.list_costs(u1)
        assert rows[0]["model"] == "claude-sonnet-5"
        assert rows[0]["correlation_id"] == "coach:999"
    finally:
        # Restore the 029 schema for the rest of the session.
        with db.get_db() as conn:
            conn.execute("ALTER TABLE harness_costs DROP COLUMN correlation_id")
            conn.execute("ALTER TABLE harness_costs DROP COLUMN model")
        costs._COSTS_COLUMNS_CACHE.clear()
