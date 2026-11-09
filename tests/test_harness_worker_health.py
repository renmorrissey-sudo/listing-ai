"""Worker health monitoring tests (Phase 1 hardening, final round).

A dead worker cannot alert its own death, so the detection must run in
the WEB process from durable DB state: the heartbeat row (fresh within
120 s) plus stuck/overdue job counts. These tests pin the contract:

* Fresh heartbeat -> healthy; stale (>120 s) or never-seen -> stale.
* A job claimed long ago by a worker with no fresh heartbeat is stuck;
  the same job on a live worker (fresh heartbeat) is a long job, not stuck.
* Pending jobs due long ago count as overdue (no worker picking them up).
* Stale / stuck-job alerts fire once per UTC hour (dedup), survive a
  simulated restart (durable audit row), and a recovery clears the
  stale flag and emits exactly one worker_recovered alert.
* GET /api/harness/health works with the HARNESS_HEALTH_TOKEN (free
  external monitor path) and requires login without it; the /harness
  page renders the health verdict from the same dict as the API.
* get_harness_status() stays side-effect free: reading status never alerts.

All sends use a recording mock notifier; nothing here sends a real SMS,
places a real call, or touches a provider.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest

import db

from harness import alerts as alerts_mod
from harness import worker_health as wh


@pytest.fixture(autouse=True)
def _isolate_worker_health_state():
    """The session DB is shared across test files: clear this file's
    state before each test so leftover jobs and alert rows from an
    earlier test cannot leak into the next one (a leftover claimed job
    would be claimed instead of the test's own job; a leftover
    worker_stale ops_alert row would suppress the next test's alert
    via the durable dedup backstop). Only worker-health ops_alert rows
    are touched — no other test file reads them."""
    db.init_db()
    with db.get_db() as conn:
        conn.execute("DELETE FROM harness_jobs")
        conn.execute(
            """
            DELETE FROM harness_audit
            WHERE action = 'ops_alert'
              AND (gate_reason LIKE '% event=worker_stale %'
                   OR gate_reason LIKE '% event=worker_recovered %'
                   OR gate_reason LIKE '% event=stuck_jobs %')
            """
        )
        conn.execute("DELETE FROM sms_worker_heartbeats WHERE worker_id LIKE 'harness-%'")
    alerts_mod._reset_alert_state()
    yield
    alerts_mod._reset_alert_state()


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _clear_heartbeats():
    with db.get_db() as conn:
        conn.execute("DELETE FROM sms_worker_heartbeats WHERE worker_id LIKE 'harness-%'")


def _age_heartbeat(worker_id, seconds):
    with db.get_db() as conn:
        conn.execute(
            "UPDATE sms_worker_heartbeats SET last_seen_at = ? WHERE worker_id = ?",
            (_iso(_now() - timedelta(seconds=seconds)), worker_id),
        )


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        user = db.get_user_by_id(user_id)
        sess["session_version"] = int((user or {}).get("session_version") or 1)


class _RecordingNotifier:
    """Collects alerts instead of POSTing them anywhere."""

    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


def _install_notifier():
    notifier = _RecordingNotifier()
    alerts_mod.set_notifier(notifier)
    alerts_mod._reset_alert_state()
    return notifier


def _uninstall_notifier():
    alerts_mod.reset_notifier()
    alerts_mod._reset_alert_state()


def _enqueue_and_claim(user_id, claimed_by, kind="scheduled_follow_up"):
    from harness.jobs import claim_jobs, enqueue_job

    job_id = enqueue_job(user_id, kind, {"lead_id": 1})
    claimed = claim_jobs(limit=1, claimed_by=claimed_by)
    assert claimed and claimed[0]["id"] == job_id
    return job_id


def _age_job(job_id, *, claimed_seconds=None, created_seconds=None, next_attempt_seconds=None):
    with db.get_db() as conn:
        if claimed_seconds is not None:
            conn.execute(
                "UPDATE harness_jobs SET claimed_at = ? WHERE id = ?",
                (_iso(_now() - timedelta(seconds=claimed_seconds)), job_id),
            )
        if created_seconds is not None:
            conn.execute(
                "UPDATE harness_jobs SET created_at = ? WHERE id = ?",
                (_iso(_now() - timedelta(seconds=created_seconds)), job_id),
            )
        if next_attempt_seconds is not None:
            conn.execute(
                "UPDATE harness_jobs SET next_attempt_at = ? WHERE id = ?",
                (_iso(_now() - timedelta(seconds=next_attempt_seconds)), job_id),
            )


# --- Heartbeat freshness ----------------------------------------------------


def test_fresh_heartbeat_reports_healthy(two_users):
    """A fresh harness heartbeat: running, not stale, healthy."""
    import workers.harness_worker as hw

    u1, _ = two_users
    _clear_heartbeats()
    try:
        health = wh.check_worker_health(u1)
        assert health["worker"]["never_seen"] is True
        assert health["worker"]["stale"] is True
        assert health["healthy"] is False

        hw._touch_heartbeat("harness-test-1", "running")
        health = wh.check_worker_health(u1)
        assert health["worker"]["running"] is True
        assert health["worker"]["stale"] is False
        assert health["worker"]["never_seen"] is False
        assert health["worker"]["worker_id"] == "harness-test-1"
        assert health["worker"]["age_seconds"] is not None
        assert 0 <= health["worker"]["age_seconds"] <= 120
        assert health["stuck_jobs"] == 0
        assert health["overdue_jobs"] == 0
        assert health["healthy"] is True
    finally:
        _clear_heartbeats()


def test_stale_heartbeat_reports_stale(two_users):
    """Heartbeat older than the 120 s window: stale, not running, unhealthy."""
    import workers.harness_worker as hw

    u1, _ = two_users
    _clear_heartbeats()
    try:
        hw._touch_heartbeat("harness-test-1", "running")
        _age_heartbeat("harness-test-1", 121)
        health = wh.check_worker_health(u1)
        assert health["worker"]["running"] is False
        assert health["worker"]["stale"] is True
        assert health["worker"]["never_seen"] is False
        assert health["worker"]["age_seconds"] >= 121
        assert health["healthy"] is False

        # A stopped heartbeat is stale too, however fresh the row is.
        hw._touch_heartbeat("harness-test-1", "stopped")
        assert wh.check_worker_health(u1)["worker"]["running"] is False
        assert wh.check_worker_health(u1)["worker"]["stale"] is True
    finally:
        _clear_heartbeats()


def test_sms_worker_heartbeat_never_makes_harness_look_alive(two_users):
    """An unprefixed (SMS campaign) worker row must not count as a
    harness heartbeat — the harness worker is a distinct service."""
    import tenant_sms_db as tdb

    u1, _ = two_users
    _clear_heartbeats()
    try:
        tdb.touch_worker_heartbeat("somehost-123", status="running")
        health = wh.check_worker_health(u1)
        assert health["worker"]["never_seen"] is True
        assert health["healthy"] is False
    finally:
        _clear_heartbeats()
        with db.get_db() as conn:
            conn.execute(
                "DELETE FROM sms_worker_heartbeats WHERE worker_id = ?",
                ("somehost-123",),
            )


# --- Stuck / overdue jobs ---------------------------------------------------


def test_stuck_job_detected_when_owner_dead(two_users):
    """A job claimed 16 min ago by a worker with no heartbeat row is
    stuck: its owner is gone and no live process will recover it."""
    u1, _ = two_users
    _clear_heartbeats()
    try:
        job_id = _enqueue_and_claim(u1, "harness-dead-1")
        _age_job(job_id, claimed_seconds=16 * 60)
        count, ids = wh.count_stuck_jobs(u1)
        assert count == 1
        assert ids == [job_id]
        health = wh.check_worker_health(u1)
        assert health["stuck_jobs"] == 1
        assert health["healthy"] is False
    finally:
        _clear_heartbeats()


def test_long_job_on_live_worker_is_not_stuck(two_users):
    """The same 16-minute-old claim on a worker WITH a fresh heartbeat
    is a long job, not a stuck one — the reclaim guard must agree."""
    import workers.harness_worker as hw

    u1, _ = two_users
    _clear_heartbeats()
    try:
        hw._touch_heartbeat("harness-live-1", "running")
        job_id = _enqueue_and_claim(u1, "harness-live-1")
        _age_job(job_id, claimed_seconds=16 * 60)
        count, ids = wh.count_stuck_jobs(u1)
        assert count == 0
        assert ids == []
        assert wh.check_worker_health(u1)["stuck_jobs"] == 0
    finally:
        _clear_heartbeats()


def test_stuck_jobs_scoped_per_tenant(two_users):
    """Stuck counts in the status API are per-tenant: tenant B's stuck
    job never shows up in tenant A's health."""
    u1, u2 = two_users
    _clear_heartbeats()
    try:
        _enqueue_and_claim(u2, "harness-dead-1")
        job_id = _enqueue_and_claim(u2, "harness-dead-1")
        _age_job(job_id, claimed_seconds=16 * 60)
        assert wh.check_worker_health(u1)["stuck_jobs"] == 0
        assert wh.check_worker_health(u2)["stuck_jobs"] == 1
        # Platform-wide (health endpoint under token auth) sees it.
        assert wh.check_worker_health(None)["stuck_jobs"] == 1
    finally:
        _clear_heartbeats()


def test_overdue_pending_job_detected(two_users):
    """A pending job due 16 min ago (backoff elapsed, or never given a
    backoff) means no worker is picking work up. A fresh pending job
    and a future-scheduled retry are not overdue."""
    from harness.jobs import enqueue_job

    u1, _ = two_users
    # Fresh pending job: not overdue.
    fresh_id = enqueue_job(u1, "scheduled_follow_up", {"lead_id": 1})
    assert wh.count_overdue_jobs(u1) == 0
    # Aged with no backoff: overdue.
    _age_job(fresh_id, created_seconds=16 * 60)
    assert wh.count_overdue_jobs(u1) == 1
    # A retry scheduled in the future is not overdue even if old.
    future_id = enqueue_job(u1, "scheduled_follow_up", {"lead_id": 2})
    with db.get_db() as conn:
        conn.execute(
            "UPDATE harness_jobs SET next_attempt_at = ? WHERE id = ?",
            (_iso(_now() + timedelta(minutes=30)), future_id),
        )
    assert wh.count_overdue_jobs(u1) == 1
    # A retry whose backoff elapsed 16 min ago is overdue.
    _age_job(future_id, next_attempt_seconds=16 * 60)
    assert wh.count_overdue_jobs(u1) == 2


# --- Alerting: dedup, recovery ----------------------------------------------


def test_stale_alert_sent_once_then_deduped(two_users):
    """A stale worker alerts once; repeat checks within the hour do not
    re-send (hour-scoped dedup, in-memory + durable audit row)."""
    import workers.harness_worker as hw

    u1, _ = two_users
    _clear_heartbeats()
    notifier = _install_notifier()
    try:
        hw._touch_heartbeat("harness-test-1", "running")
        _age_heartbeat("harness-test-1", 3600)
        health = wh.check_worker_health(u1)
        assert health["worker"]["stale"] is True
        result = wh.maybe_alert_worker_health(health, u1)
        assert result["worker_stale"] is True
        assert len(notifier.sent) == 1
        assert notifier.sent[0]["event"] == "worker_stale"
        assert "1462" not in str(notifier.sent[0])
        # Repeat checks: no re-send.
        assert wh.maybe_alert_worker_health(health, u1)["worker_stale"] is False
        assert wh.maybe_alert_worker_health(wh.check_worker_health(u1), u1)[
            "worker_stale"
        ] is False
        assert len(notifier.sent) == 1
    finally:
        _uninstall_notifier()
        _clear_heartbeats()


def test_stuck_job_alert_sent_once(two_users):
    """A stuck job alerts once with its job id in the detail, and a
    repeat check does not re-send."""
    u1, _ = two_users
    _clear_heartbeats()
    notifier = _install_notifier()
    try:
        job_id = _enqueue_and_claim(u1, "harness-dead-1")
        _age_job(job_id, claimed_seconds=16 * 60)
        health = wh.check_worker_health(u1)
        assert health["stuck_jobs"] == 1
        result = wh.maybe_alert_worker_health(health, u1)
        assert result["stuck_jobs"] is True
        stuck_alerts = [a for a in notifier.sent if a["event"] == "stuck_jobs"]
        assert len(stuck_alerts) == 1
        assert stuck_alerts[0]["detail"]["stuck_job_ids"] == [job_id]
        # The stale-worker alert fires too (no heartbeat at all), but the
        # stuck alert itself is not repeated.
        assert wh.maybe_alert_worker_health(health, u1)["stuck_jobs"] is False
        assert len([a for a in notifier.sent if a["event"] == "stuck_jobs"]) == 1
    finally:
        _uninstall_notifier()
        _clear_heartbeats()


def test_alert_dedup_survives_simulated_restart(two_users):
    """After the in-memory dedup set is cleared (simulated web restart),
    a repeat stale check still does not re-send: the durable ops_alert
    audit row is the backstop."""
    import workers.harness_worker as hw

    u1, _ = two_users
    _clear_heartbeats()
    notifier = _install_notifier()
    try:
        hw._touch_heartbeat("harness-test-1", "running")
        _age_heartbeat("harness-test-1", 3600)
        health = wh.check_worker_health(u1)
        assert wh.maybe_alert_worker_health(health, u1)["worker_stale"] is True
        assert len(notifier.sent) == 1
        alerts_mod._reset_alert_state()  # simulated restart
        assert wh.maybe_alert_worker_health(health, u1)["worker_stale"] is False
        assert len(notifier.sent) == 1
    finally:
        _uninstall_notifier()
        _clear_heartbeats()


def test_recovery_clears_stale_and_alerts_once(two_users):
    """Fresh heartbeat after a stale episode: the stale flag clears,
    exactly one worker_recovered alert fires, and the episode is closed
    (no second recovery alert, and the unrecovered-episode flag clears)."""
    import workers.harness_worker as hw

    u1, _ = two_users
    _clear_heartbeats()
    notifier = _install_notifier()
    try:
        hw._touch_heartbeat("harness-test-1", "running")
        _age_heartbeat("harness-test-1", 3600)
        assert wh.maybe_alert_worker_health(wh.check_worker_health(u1), u1)[
            "worker_stale"
        ] is True
        assert wh._has_unrecovered_stale_alert() is True

        # Worker recovers: fresh heartbeat.
        hw._touch_heartbeat("harness-test-1", "running")
        health = wh.check_worker_health(u1)
        assert health["healthy"] is True
        assert health["worker"]["stale"] is False
        result = wh.maybe_alert_worker_health(health, u1)
        assert result["worker_recovered"] is True
        recovered = [a for a in notifier.sent if a["event"] == "worker_recovered"]
        assert len(recovered) == 1
        # Repeat recovery checks: no second recovery alert; episode closed.
        assert wh.maybe_alert_worker_health(health, u1)["worker_recovered"] is False
        assert wh._has_unrecovered_stale_alert() is False
        assert len([a for a in notifier.sent if a["event"] == "worker_recovered"]) == 1
    finally:
        _uninstall_notifier()
        _clear_heartbeats()


def test_no_recovery_alert_without_prior_stale(two_users):
    """A healthy worker that was never stale produces no recovery alert
    — recovery is only meaningful after an alerted stale episode."""
    import workers.harness_worker as hw

    u1, _ = two_users
    _clear_heartbeats()
    notifier = _install_notifier()
    try:
        hw._touch_heartbeat("harness-test-1", "running")
        health = wh.check_worker_health(u1)
        assert health["healthy"] is True
        result = wh.maybe_alert_worker_health(health, u1)
        assert result == {"worker_stale": False, "stuck_jobs": False, "worker_recovered": False}
        assert notifier.sent == []
    finally:
        _uninstall_notifier()
        _clear_heartbeats()


def test_get_harness_status_never_alerts(two_users):
    """Reading status is side-effect free: a stale worker reported by
    get_harness_status must not send any alert — alerting happens only
    via the health endpoint and the /harness page route."""
    import workers.harness_worker as hw
    from harness.routes import get_harness_status

    u1, _ = two_users
    _clear_heartbeats()
    notifier = _install_notifier()
    try:
        hw._touch_heartbeat("harness-test-1", "running")
        _age_heartbeat("harness-test-1", 3600)
        status = get_harness_status(u1)
        assert status["worker_health"]["worker"]["stale"] is True
        assert status["worker_health"]["healthy"] is False
        assert notifier.sent == []
    finally:
        _uninstall_notifier()
        _clear_heartbeats()


# --- Status API / page / health endpoint ------------------------------------


def test_status_api_includes_worker_health(two_users):
    """get_harness_status carries the worker_health dict, and the legacy
    worker dict keeps its exact historical shape."""
    import workers.harness_worker as hw
    from harness.routes import get_harness_status

    u1, _ = two_users
    _clear_heartbeats()
    try:
        status = get_harness_status(u1)
        assert status["worker"] == {"running": False, "last_seen_at": None, "worker_id": None}
        assert status["worker_health"]["worker"]["never_seen"] is True
        assert status["worker_health"]["healthy"] is False
        hw._touch_heartbeat("harness-test-1", "running")
        status = get_harness_status(u1)
        assert status["worker"]["running"] is True
        assert status["worker_health"]["healthy"] is True
    finally:
        _clear_heartbeats()


def test_health_endpoint_token_auth(app_client, two_users):
    """GET /api/harness/health with a valid HARNESS_HEALTH_TOKEN needs
    no login (the free external monitor path); a wrong token is 401;
    without the token env var the endpoint requires login."""
    u1, _ = two_users
    _clear_heartbeats()
    try:
        os.environ["HARNESS_HEALTH_TOKEN"] = "test-health-token-not-real"
        resp = app_client.get("/api/harness/health?token=wrong-token")
        assert resp.status_code == 401
        resp = app_client.get(
            "/api/harness/health",
            headers={"X-Harness-Health-Token": "test-health-token-not-real"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["healthy"] is False  # no heartbeat recorded
        assert body["worker"]["never_seen"] is True
        assert "alerts" in body
        # Token auth sees platform-wide counts (no tenant scoping).
        resp = app_client.get("/api/harness/health?token=test-health-token-not-real")
        assert resp.status_code == 200
    finally:
        os.environ.pop("HARNESS_HEALTH_TOKEN", None)
        _clear_heartbeats()


def test_health_endpoint_requires_login_without_token(app_client, two_users):
    """Without HARNESS_HEALTH_TOKEN set, the health endpoint requires a
    login, like the status API; a signed-in tenant gets tenant-scoped
    counts."""
    u1, _ = two_users
    os.environ.pop("HARNESS_HEALTH_TOKEN", None)
    _clear_heartbeats()
    try:
        resp = app_client.get("/api/harness/health")
        assert resp.status_code == 401
        _login(app_client, u1)
        resp = app_client.get("/api/harness/health")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["healthy"] is False
        assert body["stuck_jobs"] == 0
    finally:
        _clear_heartbeats()


def test_health_endpoint_triggers_alert_once(app_client, two_users):
    """Hitting the health endpoint with a stale worker sends the alert
    (web-side detection); a second hit does not re-send."""
    import workers.harness_worker as hw

    u1, _ = two_users
    _clear_heartbeats()
    notifier = _install_notifier()
    try:
        hw._touch_heartbeat("harness-test-1", "running")
        _age_heartbeat("harness-test-1", 3600)
        _login(app_client, u1)
        resp = app_client.get("/api/harness/health")
        assert resp.status_code == 200
        assert resp.get_json()["alerts"]["worker_stale"] is True
        assert len([a for a in notifier.sent if a["event"] == "worker_stale"]) == 1
        resp = app_client.get("/api/harness/health")
        assert resp.status_code == 200
        assert resp.get_json()["alerts"]["worker_stale"] is False
        assert len([a for a in notifier.sent if a["event"] == "worker_stale"]) == 1
    finally:
        _uninstall_notifier()
        _clear_heartbeats()


def test_status_page_renders_worker_health(app_client, two_users):
    """The /harness page renders the same health verdict as the API:
    'needs attention' + stale note when the heartbeat is stale,
    'healthy' when fresh — never a hardcoded claim."""
    import workers.harness_worker as hw

    u1, _ = two_users
    _clear_heartbeats()
    try:
        _login(app_client, u1)
        html = app_client.get("/harness").get_data(as_text=True)
        assert "needs attention" in html
        assert "heartbeat stale" in html
        hw._touch_heartbeat("harness-test-1", "running")
        html = app_client.get("/harness").get_data(as_text=True)
        assert "healthy" in html
        assert "heartbeat stale" not in html
    finally:
        _clear_heartbeats()


def test_status_page_load_triggers_health_alert(app_client, two_users):
    """Loading /harness with a stale worker fires the web-side alert
    once (page loads are a second detection path besides the health
    endpoint)."""
    import workers.harness_worker as hw

    u1, _ = two_users
    _clear_heartbeats()
    notifier = _install_notifier()
    try:
        hw._touch_heartbeat("harness-test-1", "running")
        _age_heartbeat("harness-test-1", 3600)
        _login(app_client, u1)
        resp = app_client.get("/harness")
        assert resp.status_code == 200
        assert len([a for a in notifier.sent if a["event"] == "worker_stale"]) == 1
        resp = app_client.get("/harness")
        assert resp.status_code == 200
        assert len([a for a in notifier.sent if a["event"] == "worker_stale"]) == 1
    finally:
        _uninstall_notifier()
        _clear_heartbeats()
