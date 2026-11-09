"""Launch-readiness merge fixes (2026-11-08, branch ``topai-launch-readiness``).

Covers the two defects found while merging the six launch-readiness
workstreams, both with mocked providers / synthetic data only:

* migration 034 claim-path index: ``idx_harness_jobs_status_due`` on
  ``harness_jobs(status, next_attempt_at)`` exists after migrations run
  (the load simulation showed the cross-tenant claim full-scanning the
  table as depth grew), and the claim query's predicate matches it;
  RELEASE VARIANT (topai-launch-readiness-release, 2026-11-09): this
  release contains no schema migrations, so 034 is excluded and the
  index is absent by design. The two index tests below are adapted to
  assert the absence and that the claim still works correctly (a full
  scan at 10-customer scale, documented as acceptable in the release
  addendum); the index returns in a future migration-bearing release.
* budget-exhaustion escape in ``process_one_batch``: a job claimed with
  ``attempts`` already over ``MAX_ATTEMPTS`` (e.g. repeatedly reclaimed
  after crashes between claim and dispatch, so ``fail_job`` never ran)
  is dead-lettered instead of dispatched a 6th+ time, and the
  dead-letter alert fires. The 5th claim itself is still a legitimate
  dispatch (a retryable failure there dead-letters via ``fail_job``).
"""

import db
from harness.jobs import enqueue_job
from workers import harness_worker as hw


def test_migration_034_claim_index_absent_by_design():
    """Release variant: 034 is excluded, so the claim index is absent.

    The index file remains in git history (branch
    topai-launch-readiness-final, commit e93cf7e) for a future
    migration-bearing release. The claim still works — see
    test_claim_works_without_034_index below — it just full-scans,
    which is acceptable at 10-customer scale.
    """
    db.init_db()
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = 'harness_jobs'"
        ).fetchall()
    names = {r["name"] for r in rows}
    assert "idx_harness_jobs_status_due" not in names


def test_claim_works_without_034_index():
    """The claim predicate executes correctly without the 034 index.

    Enqueues a due job and an out-of-window job, claims via the real
    worker path, and asserts only the due job is claimed and dispatched.
    The EXPLAIN plan is expected to show a table scan here — that is
    the documented release tradeoff, not a defect.
    """
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
    from harness.jobs import claim_jobs

    claimed = claim_jobs(limit=5, claimed_by="harness-worker-test")
    claimed_ids = [j["id"] for j in claimed]
    assert claimed_ids == [due_id]  # future job is not due, so not claimed
    with db.get_db() as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM harness_jobs "
            "WHERE status = 'pending' "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
            "ORDER BY id LIMIT 1",
            ("2026-11-08T00:00:00+00:00",),
        ).fetchall()
    text = " ".join(str(r) for r in plan)
    assert "SCAN harness_jobs" in text  # documented: no index in this release


def test_budget_exhausted_job_dead_lettered_not_dispatched(monkeypatch):
    """A job claimed over MAX_ATTEMPTS is dead-lettered, never dispatched."""
    db.init_db()
    with db.get_db() as conn:
        conn.execute("DELETE FROM harness_jobs")
        conn.execute("DELETE FROM harness_audit")
    job_id = enqueue_job(
        user_id=1,
        kind="scheduled_follow_up",
        payload={"lead_id": 1, "note": "x"},
    )
    # Simulate 5 crash-reclaims: attempts already at budget, still pending.
    with db.get_db() as conn:
        conn.execute(
            "UPDATE harness_jobs SET attempts = 5 WHERE id = ?", (job_id,)
        )
    dispatched = []
    monkeypatch.setattr(hw, "dispatch_job", lambda job: dispatched.append(job["id"]))
    alerted = []
    monkeypatch.setattr(
        hw, "_alert_dead_letter", lambda job, error: alerted.append(job["id"])
    )
    assert hw.process_one_batch("harness-worker-test", limit=5) is True
    assert dispatched == []  # never executed a 6th time
    with db.get_db() as conn:
        row = dict(
            conn.execute(
                "SELECT status, attempts FROM harness_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        )
    assert row["status"] == "dead_letter"
    assert row["attempts"] == 6  # the claim itself counted, then dead-lettered
    assert alerted == [job_id]


def test_job_under_budget_still_dispatches(monkeypatch):
    """The guard does not change the normal path: attempts < 5 dispatches."""
    db.init_db()
    with db.get_db() as conn:
        conn.execute("DELETE FROM harness_jobs")
        conn.execute("DELETE FROM harness_audit")
    job_id = enqueue_job(
        user_id=1,
        kind="scheduled_follow_up",
        payload={"lead_id": 1, "note": "x"},
    )
    with db.get_db() as conn:
        conn.execute(
            "UPDATE harness_jobs SET attempts = 4 WHERE id = ?", (job_id,)
        )
    dispatched = []
    monkeypatch.setattr(hw, "dispatch_job", lambda job: dispatched.append(job["id"]))
    assert hw.process_one_batch("harness-worker-test", limit=5) is True
    assert dispatched == [job_id]
