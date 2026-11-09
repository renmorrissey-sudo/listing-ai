"""Add precise fractional-cent cost tracking and a spend-reservation table (035).

Defect 2 remediation (authorized 2026-11-09 by Ren; staging migration
testing only — production migration is NOT authorized yet).

Problem
-------
``_anthropic_cost_cents`` returned ``int(round(cost))``, so each
~0.3-cent model call rounded to 0 cents before recording. The monthly
total never accumulated for short calls, so the $5 spending cap could
not trip. The 029 ``harness_costs`` table has only ``cost_cents
INTEGER`` and no model/input-output split, so there is no existing
column that can hold a fractional-cent charge and no way to recompute
one precisely at read time. A schema change is genuinely required.

Changes
-------
1. ``harness_costs.cost_micros INTEGER`` — fixed-point micro-cents
   (1 cent = 1,000,000 micros). Nullable, additive, backfilled from
   ``cost_cents * 1000000`` so existing rows keep their exact value.
   New rows write BOTH ``cost_cents`` (rounded, display/compat) and
   ``cost_micros`` (precise). Aggregation and cap enforcement use
   ``cost_micros`` when the column exists and degrade gracefully to
   ``cost_cents`` when it does not (same pattern as 033).
2. ``harness_spend_reservations`` — per-operation spend reservations
   used for atomic cap enforcement under concurrency. A worker reserves
   an upper-bound estimate before a billable action (model call or
   provider send) and releases it after the actual cost is recorded.
   The reservation check (committed + reserved + estimate <= cap) and
   the reservation insert run in one locked transaction, so two
   concurrent workers for the same tenant cannot both pass the cap
   check and then both spend. Reservations carry a correlation_id and
   a created_at so stale ones (a crashed worker) can be reclaimed.

Data safety
------------
Additive and forward-only. ``ADD COLUMN IF NOT EXISTS`` / column
checks on both engines; a new table created ``IF NOT EXISTS``; no
rows are modified except the ``cost_micros`` backfill (which is a
pure multiplication of the existing integer value and is idempotent
— re-running it on an already-backfilled row is a no-op because the
backfill only touches rows where ``cost_micros IS NULL``). No column
or table is removed, no index is dropped. Idempotent: the upgrade
functions check first and no-op when the column/table already exists.

Rollback
--------
The change is additive and nullable. Rollback is a code-only revert
(``git revert`` of the code commit) — the column and table stay,
unused, and no data is lost. No down-migration is run (the release
has no down-migrations by design). If the column/table must be
removed later, that is a separate, explicitly authorized migration.

Compatibility
-------------
- 032 baseline (release branch): 035 applies cleanly on top of 032
  (``harness_costs`` exists from 029; the new table is independent).
- 033/034 (previously excluded): 035 does not touch the ``model`` /
  ``correlation_id`` columns (033) or the ``harness_jobs`` index
  (034). If 033 is applied before or after 035, the columns coexist
  (033 adds ``model``/``correlation_id``; 035 adds ``cost_micros``).
  The cost code detects each column independently, so any combination
  of 033/035 present or absent degrades gracefully.
"""

VERSION = "035_harness_cost_precise_micros"

RESERVATIONS_TABLE = "harness_spend_reservations"
RESERVATIONS_INDEX = "idx_harness_spend_reservations_user_period"


def _columns_sqlite(conn, table):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    names = set()
    for row in rows:
        try:
            names.add(row["name"])
        except (KeyError, TypeError, IndexError):
            names.add(row[1])
    return names


def _has_column_postgres(conn, table, column):
    row = conn.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        """,
        (table, column),
    ).fetchone()
    return row is not None


def _table_exists_sqlite(conn, table):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _table_exists_postgres(conn, table):
    row = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        (table,),
    ).fetchone()
    return row is not None


def upgrade_sqlite(conn):
    if "cost_micros" not in _columns_sqlite(conn, "harness_costs"):
        conn.execute("ALTER TABLE harness_costs ADD COLUMN cost_micros INTEGER")
    # Backfill: only rows that have not been backfilled yet (idempotent).
    conn.execute(
        """
        UPDATE harness_costs
        SET cost_micros = cost_cents * 1000000
        WHERE cost_micros IS NULL
        """
    )
    if not _table_exists_sqlite(conn, RESERVATIONS_TABLE):
        conn.execute(
            f"""
            CREATE TABLE {RESERVATIONS_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                period TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                reserved_micros INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, period, correlation_id)
            )
            """
        )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {RESERVATIONS_INDEX}
        ON {RESERVATIONS_TABLE}(user_id, period)
        """
    )


def upgrade_postgres(conn):
    conn.execute(
        "ALTER TABLE harness_costs ADD COLUMN IF NOT EXISTS cost_micros BIGINT"
    )
    conn.execute(
        """
        UPDATE harness_costs
        SET cost_micros = cost_cents * 1000000
        WHERE cost_micros IS NULL
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {RESERVATIONS_TABLE} (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            period TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            reserved_micros BIGINT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, period, correlation_id)
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {RESERVATIONS_INDEX}
        ON {RESERVATIONS_TABLE}(user_id, period)
        """
    )


def _verify_sqlite(conn):
    if "cost_micros" not in _columns_sqlite(conn, "harness_costs"):
        raise RuntimeError(
            "035_harness_cost_precise_micros failed: "
            "harness_costs.cost_micros missing"
        )
    # Every row must be backfilled (no NULL cost_micros remains).
    row = conn.execute(
        "SELECT COUNT(*) AS remaining FROM harness_costs WHERE cost_micros IS NULL"
    ).fetchone()
    remaining = row["remaining"] if row is not None else 0
    if remaining:
        raise RuntimeError(
            "035_harness_cost_precise_micros failed: "
            f"{remaining} harness_costs rows still have NULL cost_micros"
        )
    if not _table_exists_sqlite(conn, RESERVATIONS_TABLE):
        raise RuntimeError(
            "035_harness_cost_precise_micros failed: "
            f"{RESERVATIONS_TABLE} missing"
        )


def _verify_postgres(conn):
    if not _has_column_postgres(conn, "harness_costs", "cost_micros"):
        raise RuntimeError(
            "035_harness_cost_precise_micros failed: "
            "harness_costs.cost_micros missing"
        )
    row = conn.execute(
        "SELECT COUNT(*) AS remaining FROM harness_costs WHERE cost_micros IS NULL"
    ).fetchone()
    remaining = row["remaining"] if row is not None else 0
    if remaining:
        raise RuntimeError(
            "035_harness_cost_precise_micros failed: "
            f"{remaining} harness_costs rows still have NULL cost_micros"
        )
    if not _table_exists_postgres(conn, RESERVATIONS_TABLE):
        raise RuntimeError(
            "035_harness_cost_precise_micros failed: "
            f"{RESERVATIONS_TABLE} missing"
        )


def verify(conn, dialect):
    if dialect == "postgresql":
        _verify_postgres(conn)
    else:
        _verify_sqlite(conn)


def verify_postgres(conn):
    _verify_postgres(conn)
