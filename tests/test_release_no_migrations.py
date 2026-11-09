"""Migration-free release guarantees (branch ``topai-launch-readiness-release``).

Ren requires this release to contain no database schema migrations, so
migrations 033 (harness_costs model + correlation_id columns) and 034
(harness_jobs claim-path index) are excluded: their files are deleted
and the runner list ends at 032. The files remain in git history
(branch topai-launch-readiness-final, commit e93cf7e) for a future
migration-bearing release.

These tests pin what the release DOES still guarantee on the 029
column set, and pin the two known degradations explicitly so they are
visible tradeoffs, not silent surprises:

* cost recording still works — every path records provider / tool /
  units / unit_type / cost_cents, and the spend cap still trips on
  those totals. ``model`` and ``correlation_id`` are dropped at write
  time (``harness.costs._insert_cost_row`` falls back to the 029
  column set); per-model breakdown and per-event traceability return
  when 033 lands.
* idempotent true-up dedup is lost — without the correlation_id
  column, ``record_cost_once`` cannot detect a redelivery, so a
  duplicate Vapi true-up inserts a second row. This over-counts spend,
  so it trips the cap sooner: the degradation fails safe.
* the job claim still works without the 034 index — it full-scans
  ``harness_jobs``, which is acceptable at 10-customer scale (the load
  simulation showed the scan only bites at ~1,000-tenant depths).

All tests use synthetic data only; no provider calls are made.
"""

import db
from harness import costs
from harness.jobs import claim_jobs, enqueue_job
from migrations.runner import MIGRATION_MODULES


def test_runner_list_ends_at_032():
    """No 033/034 migration is listed or present in the release."""
    assert MIGRATION_MODULES[-1].endswith("032_tenant_sms_senders_scoped_unique")
    assert len(MIGRATION_MODULES) == 32
    assert not any("033_" in m or "034_" in m for m in MIGRATION_MODULES)


def test_migration_033_034_files_absent():
    """The migration files themselves are deleted from the release."""
    import os

    versions = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "migrations", "versions",
    )
    names = os.listdir(versions)
    assert not any(n.startswith("033_") for n in names)
    assert not any(n.startswith("034_") for n in names)


def test_record_cost_degrades_to_029_columns(two_users):
    """Cost rows are recorded exactly, minus model/correlation_id."""
    u1, _ = two_users
    row_id = costs.record_cost(
        u1, "anthropic", "ask_topai", 1500, "tokens", cost_cents=5,
        model="claude-sonnet-5", correlation_id="req-abc",
    )
    assert row_id is not None
    row = next(r for r in costs.list_costs(u1) if r["id"] == row_id)
    assert row["provider"] == "anthropic"
    assert row["tool"] == "ask_topai"
    assert row["units"] == 1500
    assert row["unit_type"] == "tokens"
    assert row["cost_cents"] == 5
    assert "model" not in row
    assert "correlation_id" not in row


def test_spend_cap_still_enforced_on_029_columns(two_users, monkeypatch):
    """Cap enforcement uses the recorded totals, so it still trips."""
    u1, _ = two_users
    monkeypatch.setenv("HARNESS_MONTHLY_SPEND_CAP_CENTS", "100")
    allowed, reason = costs.check_spend_cap(u1)
    assert allowed is True and reason is None
    costs.record_cost(
        u1, "anthropic", "seed", 1000, "tokens", cost_cents=5000,
        model="claude-opus-4-6", correlation_id="cap-1",
    )
    assert costs.is_spend_cap_tripped(u1)
    assert costs.spend_cap_status(u1)["spent_cents"] == 5000
    allowed, reason = costs.check_spend_cap(u1)
    assert allowed is False
    assert "spend limit" in reason


def test_monthly_totals_and_list_costs_work_on_029_columns(two_users):
    """Aggregation and listing read only 029 columns."""
    u1, _ = two_users
    costs.record_cost(u1, "anthropic", "ask_topai", 1000, "tokens", cost_cents=3)
    costs.record_cost(u1, "telnyx", "sms_send", 2, "segments", cost_cents=2)
    totals = costs.monthly_totals(u1, costs._current_period())
    assert totals["total_cents"] == 5
    assert totals["by_provider"] == {"anthropic": 3, "telnyx": 2}
    assert totals["by_tool"] == {"ask_topai": 3, "sms_send": 2}
    assert totals["cost_basis"] == "estimate"
    assert len(costs.list_costs(u1)) == 2


def test_record_cost_once_dedup_lost_on_pre033_schema(two_users):
    """Duplicate true-ups double-record without the correlation column.

    Pinned explicitly: on the pre-033 schema the dedup lookup has no
    column to query, so a redelivered Vapi true-up inserts again.
    Over-counting fails safe (the cap trips sooner); dedup returns
    when 033 lands in a future release.
    """
    u1, _ = two_users
    first = costs.record_cost_once(
        u1, "vapi", "vapi_call_duration", 2, "minutes",
        cost_cents=20, correlation_id="dup-1",
    )
    assert first is not None
    second = costs.record_cost_once(
        u1, "vapi", "vapi_call_duration", 2, "minutes",
        cost_cents=20, correlation_id="dup-1",
    )
    assert second is not None  # NOT deduped on the pre-033 schema
    assert second != first
    rows = [r for r in costs.list_costs(u1) if r["tool"] == "vapi_call_duration"]
    assert len(rows) == 2
    assert sum(r["cost_cents"] for r in rows) == 40


def test_claim_works_without_034_index():
    """Due jobs are claimed and future jobs are not, without the index."""
    db.init_db()
    with db.get_db() as conn:
        conn.execute("DELETE FROM harness_jobs")
        conn.execute("DELETE FROM harness_audit")
    due_id = enqueue_job(
        user_id=1, kind="scheduled_follow_up",
        payload={"lead_id": 1, "note": "due"},
    )
    future_id = enqueue_job(
        user_id=1, kind="scheduled_follow_up",
        payload={"lead_id": 1, "note": "future"},
    )
    with db.get_db() as conn:
        conn.execute(
            "UPDATE harness_jobs SET next_attempt_at = ? WHERE id = ?",
            ("2999-01-01T00:00:00+00:00", future_id),
        )
    claimed = claim_jobs(limit=5, claimed_by="harness-worker-test")
    assert [j["id"] for j in claimed] == [due_id]
