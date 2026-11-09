"""Defect 2 regression tests: precise fractional-cent costs and the
atomic spend reservation (migration 035).

Ren authorized migration 035 on 2026-11-09. The cost ledger now
stores exact fixed-point micro-cents (1 cent = 1,000,000 micros) in
``harness_costs.cost_micros`` and enforces the spending cap on the
precise cumulative total. ``reserve_spend`` closes the concurrency
race: the check (committed + reserved + estimate <= cap) and the
reservation insert run in one locked transaction, so two workers for
the same tenant cannot both pass and then both spend.

All tests use synthetic data only; no provider calls are made.
"""

import threading
from datetime import datetime, timedelta, timezone

import pytest

import db
from harness import costs


def _set_cap(monkeypatch, cents):
    monkeypatch.setenv("HARNESS_MONTHLY_SPEND_CAP_CENTS", str(cents))


def test_sub_cent_calls_accumulate_nonzero_cost(two_users):
    """Multiple inexpensive model calls accumulate a nonzero internal
    cost. Previously each ~0.3-cent call rounded to 0 cents and the
    total stayed 0 forever."""
    u1, _ = two_users
    # Sonnet: 100 input tokens * 300 + 100 output tokens * 1500
    # = 30,000 + 150,000 = 180,000 micros = 0.18 cents per call.
    for _ in range(5):
        costs.record_anthropic_usage(
            u1, "coach",
            {"input_tokens": 100, "output_tokens": 100},
            model="claude-sonnet-5",
        )
    totals = costs.monthly_totals(u1, costs._current_period())
    assert totals["total_micros"] == 5 * 180_000, totals
    assert totals["total_cents"] == 1  # 0.9 cents rounds to 1 cent
    assert costs.current_month_spend_micros(u1) == 900_000


def test_exact_micros_for_anthropic_call(two_users):
    """The micro-cent cost is exact integer arithmetic (no rounding)."""
    u1, _ = two_users
    row_id = costs.record_anthropic_usage(
        u1, "coach",
        {"input_tokens": 1000, "output_tokens": 1000},
        model="claude-sonnet-5",
    )
    row = next(r for r in costs.list_costs(u1) if r["id"] == row_id)
    # 1000*300 + 1000*1500 = 1,800,000 micros = 1.8 cents.
    assert row["cost_micros"] == 1_800_000
    assert row["cost_cents"] == 2  # display rounds to nearest cent


def test_cap_trips_at_exact_micros_boundary(two_users, monkeypatch):
    """The cap trips exactly when the precise spend reaches it, not
    before (a sub-cent total below the cap does not trip) and not
    after (a total at the cap trips)."""
    u1, _ = two_users
    _set_cap(monkeypatch, 1)  # 1 cent = 1,000,000 micros
    # 0.9 cents of spend: below the cap, not tripped.
    costs.record_anthropic_usage(
        u1, "coach",
        {"input_tokens": 1000, "output_tokens": 1000},
        model="claude-sonnet-5",
    )  # 1,800,000 micros = 1.8 cents -- over the 1-cent cap
    assert costs.is_spend_cap_tripped(u1)
    # A fresh tenant with 0.5 cents of spend is not tripped.
    u2 = two_users[1]
    costs.record_anthropic_usage(
        u2, "coach",
        {"input_tokens": 100, "output_tokens": 100},
        model="claude-sonnet-5",
    )  # 180,000 micros = 0.18 cents
    assert not costs.is_spend_cap_tripped(u2)
    # Add enough to reach exactly the cap: 0.18 + 0.82 = 1.0 cents.
    costs.record_anthropic_usage(
        u2, "coach",
        {"input_tokens": 100, "output_tokens": 480},
        model="claude-sonnet-5",
    )  # 100*300 + 480*1500 = 30,000 + 720,000 = 750,000 micros
    # 180,000 + 750,000 = 930,000 micros = 0.93 cents -- still under.
    assert not costs.is_spend_cap_tripped(u2)
    costs.record_anthropic_usage(
        u2, "coach",
        {"input_tokens": 100, "output_tokens": 27},
        model="claude-sonnet-5",
    )  # 100*300 + 27*1500 = 30,000 + 40,500 = 70,500 micros
    # 930,000 + 70,500 = 1,000,500 micros = 1.0005 cents -- at/over cap.
    assert costs.is_spend_cap_tripped(u2)


def test_reserve_spend_blocks_when_cap_would_be_exceeded(two_users, monkeypatch):
    """A reservation that would push the tenant over the cap is refused."""
    u1, _ = two_users
    _set_cap(monkeypatch, 1)  # 1 cent
    assert costs.reserve_spend(u1, 900_000, "corr-a") is True
    # 900,000 reserved + 200,000 more would exceed 1,000,000.
    assert costs.reserve_spend(u1, 200_000, "corr-b") is False
    # But a smaller reservation that fits is allowed.
    assert costs.reserve_spend(u1, 100_000, "corr-c") is True
    assert costs.reserve_spend(u1, 1, "corr-d") is False  # exactly at cap
    assert costs.release_reservation(u1, "corr-a") is True
    assert costs.release_reservation(u1, "corr-c") is True


def test_reserve_spend_is_idempotent_for_same_correlation(two_users, monkeypatch):
    """Re-reserving with the same correlation_id (a retry) does not
    double-count the reservation."""
    u1, _ = two_users
    _set_cap(monkeypatch, 1)
    assert costs.reserve_spend(u1, 600_000, "corr-retry") is True
    assert costs.reserve_spend(u1, 600_000, "corr-retry") is True
    # Only 600,000 is reserved, so a 400,000 reservation still fits.
    assert costs.reserve_spend(u1, 400_000, "corr-other") is True
    assert costs.reserve_spend(u1, 1, "corr-third") is False
    assert costs.release_reservation(u1, "corr-retry") is True
    assert costs.release_reservation(u1, "corr-other") is True


def test_release_after_failed_spend_frees_budget(two_users, monkeypatch):
    """When a billable action fails after reserving, releasing the
    reservation frees the budget for the next attempt."""
    u1, _ = two_users
    _set_cap(monkeypatch, 1)
    assert costs.reserve_spend(u1, 900_000, "corr-fail") is True
    assert costs.is_spend_cap_tripped(u1) is False  # 0.9 < 1.0
    # The action fails; release the reservation.
    assert costs.release_reservation(u1, "corr-fail") is True
    assert costs.reserved_spend_micros(u1) == 0
    # The full budget is available again.
    assert costs.reserve_spend(u1, 1_000_000, "corr-full") is True
    assert costs.release_reservation(u1, "corr-full") is True


def test_concurrent_reservations_cannot_bypass_cap(two_users, monkeypatch):
    """Two threads reserving simultaneously for the same tenant cannot
    both pass when only one fits. This is the defect-2 requirement 5
    race: a plain recheck of committed totals would let both through."""
    u1, _ = two_users
    _set_cap(monkeypatch, 1)  # 1 cent = 1,000,000 micros
    results = {}

    def _reserve(name):
        results[name] = costs.reserve_spend(u1, 600_000, f"corr-{name}")

    t1 = threading.Thread(target=_reserve, args=("a",))
    t2 = threading.Thread(target=_reserve, args=("b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    # Exactly one of the two 600,000-micros reservations fits in a
    # 1,000,000-micros cap; the other must be refused.
    assert sorted(results.values()) == [False, True], results
    assert costs.reserved_spend_micros(u1) == 600_000
    assert costs.release_reservation(u1, "corr-a") or costs.release_reservation(u1, "corr-b")


def test_reservations_are_tenant_isolated(two_users, monkeypatch):
    """One tenant's reservations and spend do not affect another
    tenant's cap."""
    u1, u2 = two_users
    _set_cap(monkeypatch, 1)
    assert costs.reserve_spend(u1, 1_000_000, "corr-u1") is True
    # u2's cap is unaffected by u1's reservation.
    assert costs.reserve_spend(u2, 1_000_000, "corr-u2") is True
    assert costs.is_spend_cap_tripped(u1) is True
    assert costs.is_spend_cap_tripped(u2) is True  # both reserved their own cap
    assert costs.release_reservation(u1, "corr-u1") is True
    assert costs.is_spend_cap_tripped(u1) is False
    assert costs.is_spend_cap_tripped(u2) is True
    assert costs.release_reservation(u2, "corr-u2") is True


def test_expired_reservations_are_reclaimed(two_users, monkeypatch):
    """A reservation left behind by a crashed worker is reclaimed
    (it does not permanently reduce the tenant's budget)."""
    u1, _ = two_users
    _set_cap(monkeypatch, 1)
    assert costs.reserve_spend(u1, 900_000, "corr-stale") is True
    # Age the reservation past the TTL by rewriting its created_at.
    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    with db.get_db() as conn:
        conn.execute(
            """
            UPDATE harness_spend_reservations
            SET created_at = ? WHERE correlation_id = 'corr-stale'
            """,
            (old,),
        )
    # A new reservation reclaims the stale one in the same transaction.
    assert costs.reserve_spend(u1, 900_000, "corr-fresh") is True
    assert costs.reserved_spend_micros(u1) == 900_000
    # The explicit cleanup also works.
    assert costs.release_reservation(u1, "corr-fresh") is True
    assert costs.reserve_spend(u1, 500_000, "corr-old2") is True
    with db.get_db() as conn:
        conn.execute(
            """
            UPDATE harness_spend_reservations
            SET created_at = ? WHERE correlation_id = 'corr-old2'
            """,
            (old,),
        )
    assert costs.cleanup_expired_reservations(u1) == 1
    assert costs.reserved_spend_micros(u1) == 0


def test_spend_cap_status_includes_reservations(two_users, monkeypatch):
    """spend_cap_status reports the committed spend, the reserved
    spend, and trips when committed + reserved reaches the cap."""
    u1, _ = two_users
    _set_cap(monkeypatch, 1)
    status = costs.spend_cap_status(u1)
    assert status["tripped"] is False
    assert status["reserved_micros"] == 0
    assert costs.reserve_spend(u1, 600_000, "corr-status") is True
    status = costs.spend_cap_status(u1)
    assert status["reserved_micros"] == 600_000
    assert status["tripped"] is False  # 0.6 < 1.0
    assert costs.reserve_spend(u1, 400_000, "corr-status2") is True
    status = costs.spend_cap_status(u1)
    assert status["tripped"] is True  # 0.6 + 0.4 = 1.0 = cap
    assert costs.release_reservation(u1, "corr-status") is True
    assert costs.release_reservation(u1, "corr-status2") is True


def test_cap_disabled_when_non_positive(two_users, monkeypatch):
    """A non-positive cap disables enforcement (reservations are a no-op)."""
    u1, _ = two_users
    _set_cap(monkeypatch, 0)
    assert costs.reserve_spend(u1, 10**12, "corr-nocap") is True
    assert costs.is_spend_cap_tripped(u1) is False


def test_code_rollback_safe_on_pre_035_schema(two_users, monkeypatch):
    """Code rollback safety (Decision 1, item 8): if the code is
    reverted to the pre-035 version while the schema still has the
    035 column/table, nothing breaks — and conversely, if the
    schema is rolled back (035 column/table removed) while the new
    code runs, every path degrades gracefully to the 029 column set
    and the plain committed-spend cap check. This test simulates
    the second direction: drop cost_micros + the reservations table
    and verify recording, aggregation, cap checks, and reservations
    all still work (falling back, not crashing)."""
    u1, _ = two_users
    _set_cap(monkeypatch, 100)
    # Record a cost on the 035 schema first (proves the column works).
    costs.record_anthropic_usage(
        u1, "coach",
        {"input_tokens": 100, "output_tokens": 100},
        model="claude-sonnet-5",
    )
    assert costs.current_month_spend_micros(u1) == 180_000
    # Simulate a schema rollback: drop the 035 column and table.
    # The shared test DB is restored in the finally below so later
    # tests still see the 035 schema.
    costs._COSTS_COLUMNS_CACHE.clear()
    try:
        with db.get_db() as conn:
            conn.execute("ALTER TABLE harness_costs DROP COLUMN cost_micros")
            conn.execute("DROP TABLE IF EXISTS harness_spend_reservations")
        costs._COSTS_COLUMNS_CACHE.clear()
        # Every path still works on the 029 column set.
        assert costs.current_month_spend_micros(u1) == 0  # cost_cents was 0 (rounded)
        totals = costs.monthly_totals(u1, costs._current_period())
        assert totals["total_cents"] == 0
        assert totals["total_micros"] == 0
        # Recording still works (cost_cents only).
        row_id = costs.record_cost(
            u1, "anthropic", "coach", 100, "tokens", cost_cents=2
        )
        assert row_id is not None
        assert costs.current_month_spend_cents(u1) == 2
        # Cap checks still work (plain committed-spend check).
        assert costs.is_spend_cap_tripped(u1) is False
        assert costs.check_spend_cap(u1)[0] is True
        # Reservations fall back to the plain check (no table to record
        # in, so consecutive calls do NOT accumulate — each is checked
        # against committed spend alone). Allowed when under the cap,
        # refused when the estimate would exceed it.
        assert costs.reserve_spend(u1, 50_000_000, "corr-rollback") is True
        assert costs.reserve_spend(u1, 99_000_000, "corr-rollback2") is False
        assert costs.reserved_spend_micros(u1) == 0  # no table, no reservations
        assert costs.release_reservation(u1, "corr-rollback") is False
    finally:
        # Restore the 035 schema for the shared test DB.
        import importlib

        m035 = importlib.import_module(
            "migrations.versions.035_harness_cost_precise_micros"
        )
        with db.get_db() as conn:
            m035.upgrade_sqlite(conn)
        costs._COSTS_COLUMNS_CACHE.clear()
