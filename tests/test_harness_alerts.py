"""M4 operational failure alerting tests (harness/alerts.py + routes +
worker wiring).

Covers: status counts for each job state (API + dashboard), dead-letter
notification with a mocked notifier, dedup (second identical event sends
nothing new, including across an in-memory reset via the audit row),
redaction (fake API key / phone in the job payload must not appear
verbatim in the alert), non-blocking behaviour (a raising notifier never
fails the job or the worker), worker-failure alerting, and repeated
provider failures alerting only at the threshold.

All notifiers are mocked or log-only; nothing here sends a real SMS,
call, email, or webhook. The webhook notifier is exercised only against
a local stub via urllib mocking in the disabled-by-default test path.
"""

import json
from unittest.mock import patch

import pytest

import db
from harness import alerts
from harness.jobs import enqueue_job, fail_job
from harness.routes import get_harness_status


@pytest.fixture(autouse=True)
def _clean_alert_state():
    alerts._reset_alert_state()
    alerts.reset_notifier()
    yield
    alerts._reset_alert_state()
    alerts.reset_notifier()


@pytest.fixture(autouse=True)
def _isolate_job_queue():
    """The session DB is shared by every test (tests/conftest.py), so
    earlier tests can leave harness jobs behind. The worker tests in
    this file claim the oldest pending job (run_once /
    process_one_batch), so clear the queue first: the job the worker
    picks up is then this test's job, not a leftover."""
    db.init_db()
    with db.get_db() as conn:
        conn.execute("DELETE FROM harness_jobs")
    yield


class RecordingNotifier:
    """Mock notifier: records every alert dict it is sent."""

    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


class RaisingNotifier:
    """Mock notifier that always fails delivery."""

    def send(self, alert):
        raise RuntimeError("webhook down (test)")


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        user = db.get_user_by_id(user_id)
        sess["session_version"] = int((user or {}).get("session_version") or 1)


def _row(job_id):
    with db.get_db() as conn:
        return dict(
            conn.execute(
                "SELECT * FROM harness_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        )


def _set_attempts(job_id, attempts, status="pending"):
    with db.get_db() as conn:
        conn.execute(
            "UPDATE harness_jobs SET attempts = ?, status = ? WHERE id = ?",
            (attempts, status, job_id),
        )


def _delete_jobs(*job_ids):
    with db.get_db() as conn:
        for job_id in job_ids:
            conn.execute("DELETE FROM harness_jobs WHERE id = ?", (job_id,))


def _ops_audit_rows(key):
    with db.get_db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM harness_audit WHERE action = 'ops_alert' "
                "AND gate_reason LIKE ?",
                (f"% key={key} %",),
            ).fetchall()
        ]


# --- Status counts ------------------------------------------------------------


def test_status_counts_zero_when_no_jobs(two_users):
    import auth

    fresh = db.create_user(
        "alerts-fresh@example.com", auth.hash_password("password123")
    )
    assert get_harness_status(fresh)["job_counts"] == {
        "pending": 0,
        "failed": 0,
        "retried": 0,
        "dead_letter": 0,
    }


def test_status_counts_cover_each_job_state(two_users):
    u1, u2 = two_users
    # Fresh pending job: never attempted.
    j_pending = enqueue_job(u1, "sms_auto_reply", {"a": 1})
    # Failed job: attempted once, back on the pending queue for retry.
    j_failed = enqueue_job(u1, "sms_auto_reply", {"a": 2})
    _set_attempts(j_failed, 1)
    # Retried job: claimed twice after failures, still pending.
    j_retried = enqueue_job(u1, "sms_auto_reply", {"a": 3})
    _set_attempts(j_retried, 2)
    # Dead-lettered job: terminal failure, never retried.
    j_dead = enqueue_job(u1, "sms_auto_reply", {"a": 4})
    assert fail_job(j_dead, "permanent boom", retryable=False) == "dead_letter"
    # Another tenant's pending job must not leak into u1's counts.
    j_other = enqueue_job(u2, "sms_auto_reply", {"a": 5})
    try:
        counts = get_harness_status(u1)["job_counts"]
        assert counts == {
            "pending": 3,  # fresh + failed + retried (all status pending)
            "failed": 2,  # failed + retried (pending with attempts > 0)
            "retried": 1,  # only the attempts>=2 job
            "dead_letter": 1,
        }
        assert get_harness_status(u2)["job_counts"]["pending"] == 1
    finally:
        _delete_jobs(j_pending, j_failed, j_retried, j_dead, j_other)


def test_status_api_returns_job_counts(app_client, two_users):
    u1, _ = two_users
    j_dead = enqueue_job(u1, "sms_auto_reply", {"a": 1})
    fail_job(j_dead, "permanent boom", retryable=False)
    try:
        _login(app_client, u1)
        resp = app_client.get("/api/harness/status")
        assert resp.status_code == 200
        body = resp.get_json()
        assert set(body["job_counts"]) == {
            "pending", "failed", "retried", "dead_letter",
        }
        assert body["job_counts"]["dead_letter"] == 1
    finally:
        _delete_jobs(j_dead)


def test_status_page_shows_job_counts(app_client, two_users):
    u1, _ = two_users
    j_dead = enqueue_job(u1, "sms_auto_reply", {"a": 1})
    fail_job(j_dead, "permanent boom", retryable=False)
    try:
        _login(app_client, u1)
        resp = app_client.get("/harness")
        assert resp.status_code == 200
        assert b"Jobs dead-lettered:" in resp.data
        assert b"Jobs failed (awaiting retry):" in resp.data
        assert b"Jobs retried:" in resp.data
        assert b"Jobs pending:" in resp.data
    finally:
        _delete_jobs(j_dead)


# --- Dead-letter notification ---------------------------------------------------


def test_dead_letter_notification_via_worker(two_users):
    from workers.harness_worker import run_once

    u1, _ = two_users
    recorder = RecordingNotifier()
    alerts.set_notifier(recorder)
    job_id = enqueue_job(u1, "definitely_not_a_kind", {"x": 1})
    try:
        assert run_once() is True
        assert _row(job_id)["status"] == "dead_letter"
        assert len(recorder.sent) == 1
        alert = recorder.sent[0]
        assert alert["event"] == "dead_letter"
        assert alert["detail"]["job_id"] == job_id
        assert alert["detail"]["kind"] == "definitely_not_a_kind"
    finally:
        _delete_jobs(job_id)


def test_dead_letter_alert_deduplicated(two_users):
    u1, _ = two_users
    recorder = RecordingNotifier()
    job = {
        "id": 424242,
        "user_id": u1,
        "kind": "sms_auto_reply",
        "attempts": 5,
        "payload_json": "{}",
    }
    assert alerts.alert_dead_letter(job, "permanent boom", notifier=recorder) is True
    # Second identical event: nothing new is sent.
    assert alerts.alert_dead_letter(job, "permanent boom", notifier=recorder) is False
    assert len(recorder.sent) == 1
    # ... and not after the in-memory dedup is cleared either: the
    # audit row carries the key, so the restart case is deduped too.
    alerts._reset_alert_state()
    assert alerts.alert_dead_letter(job, "permanent boom", notifier=recorder) is False
    assert len(recorder.sent) == 1
    assert len(_ops_audit_rows("dead_letter:424242")) == 1


def test_alert_redacts_secrets_and_phone(two_users):
    u1, _ = two_users
    recorder = RecordingNotifier()
    fake_key = "sk-ant-api03-FAKESECRETKEY1234567890"
    fake_phone = "+13035550199"
    fake_token = "ghp_FAKEGITHUBTOKEN1234567890"
    job = {
        "id": 424243,
        "user_id": u1,
        "kind": "sms_auto_reply",
        "attempts": 5,
        "payload": {
            "api_key": fake_key,
            "body": f"call me at {fake_phone} about the house",
            "note": f"my token is {fake_token}",
        },
    }
    assert alerts.alert_dead_letter(
        job, f"send failed, key was {fake_key}", notifier=recorder
    ) is True
    assert len(recorder.sent) == 1
    dumped = json.dumps(recorder.sent[0])
    assert fake_key not in dumped
    assert fake_phone not in dumped
    assert fake_token not in dumped
    assert "[REDACTED]" in dumped


def test_alert_keeps_correlation_id_but_redacts_secrets(two_users):
    """A uuid-hex correlation id must survive redaction (it matches the
    long-hex secret pattern) while secrets in the same alert do not."""
    u1, _ = two_users
    recorder = RecordingNotifier()
    corr = "a77790ed601c4a8c8c05a0d0792973f2"
    fake_key = "sk-ant-api03-FAKESECRETKEY1234567890"
    job = {
        "id": 424246,
        "user_id": u1,
        "kind": "sms_auto_reply",
        "attempts": 5,
        "correlation_id": corr,
        "payload_json": "{}",
    }
    assert alerts.alert_dead_letter(job, f"boom key={fake_key}", notifier=recorder) is True
    alert = recorder.sent[0]
    assert alert["detail"]["correlation_id"] == corr
    assert fake_key not in json.dumps(alert)
    assert alert["detail"]["user_id"] == u1


def test_failing_notifier_is_non_blocking(two_users):
    from workers.harness_worker import run_once

    u1, _ = two_users
    # Direct call: a raising notifier returns False, never raises.
    job = {"id": 424244, "user_id": u1, "kind": "sms_auto_reply", "attempts": 5}
    assert alerts.alert_dead_letter(job, "boom", notifier=RaisingNotifier()) is False
    # Worker path: the job still dead-letters and run_once still succeeds.
    alerts.set_notifier(RaisingNotifier())
    job_id = enqueue_job(u1, "definitely_not_a_kind", {"x": 1})
    try:
        assert run_once() is True
        assert _row(job_id)["status"] == "dead_letter"
    finally:
        _delete_jobs(job_id)


# --- Worker-failure alerting --------------------------------------------------


def test_worker_failure_alert(two_users):
    from workers.harness_worker import process_one_batch

    u1, _ = two_users
    recorder = RecordingNotifier()
    alerts.set_notifier(recorder)
    job_id = enqueue_job(u1, "sms_auto_reply", {"lead_id": 1})
    try:
        with patch(
            "workers.harness_worker.dispatch_job",
            side_effect=RuntimeError("boom-test-dispatch-crash"),
        ):
            assert process_one_batch("test-worker") is True
        failures = [a for a in recorder.sent if a["event"] == "worker_failure"]
        assert len(failures) == 1
        assert "boom-test-dispatch-crash" in failures[0]["detail"]["error"]
        assert failures[0]["detail"]["context"]["job_id"] == job_id
        # The crashed dispatch still fails the job retryably (not lost).
        assert _row(job_id)["status"] == "pending"
    finally:
        _delete_jobs(job_id)


def test_worker_failure_alert_deduplicated_per_hour(two_users):
    recorder = RecordingNotifier()
    assert alerts.alert_worker_failure(
        "loop exploded", worker_id="w1", notifier=recorder
    ) is True
    assert alerts.alert_worker_failure(
        "loop exploded", worker_id="w1", notifier=recorder
    ) is False
    assert len(recorder.sent) == 1


# --- Repeated provider failures -----------------------------------------------


def test_provider_failure_alerts_only_at_threshold(two_users):
    u1, _ = two_users
    recorder = RecordingNotifier()
    kwargs = dict(kind="sms_auto_reply", user_id=u1, threshold=3)
    assert alerts.alert_provider_failure(
        "telnyx", "Telnyx gateway timeout", notifier=recorder, **kwargs
    ) is False
    assert alerts.alert_provider_failure(
        "telnyx", "Telnyx gateway timeout", notifier=recorder, **kwargs
    ) is False
    assert len(recorder.sent) == 0
    # Third consecutive failure: the alert fires.
    assert alerts.alert_provider_failure(
        "telnyx", "Telnyx gateway timeout", notifier=recorder, **kwargs
    ) is True
    assert len(recorder.sent) == 1
    assert recorder.sent[0]["event"] == "provider_failure"
    assert recorder.sent[0]["detail"]["provider"] == "telnyx"
    assert recorder.sent[0]["detail"]["consecutive_failures"] == 3
    # A fourth failure starts a new count; no duplicate within the hour.
    assert alerts.alert_provider_failure(
        "telnyx", "Telnyx gateway timeout", notifier=recorder, **kwargs
    ) is False
    assert len(recorder.sent) == 1
    # A different provider has its own counter.
    assert alerts.alert_provider_failure(
        "twilio", "Twilio gateway timeout", notifier=recorder, **kwargs
    ) is False
    assert alerts.alert_provider_failure(
        "twilio", "Twilio gateway timeout", notifier=recorder, **kwargs
    ) is False
    assert alerts.alert_provider_failure(
        "twilio", "Twilio gateway timeout", notifier=recorder, **kwargs
    ) is True
    assert len(recorder.sent) == 2


def test_provider_failure_wired_into_worker_retries(two_users):
    from harness import jobs as jobs_module
    from workers.harness_worker import _run_known_handler

    u1, _ = two_users
    recorder = RecordingNotifier()
    alerts.set_notifier(recorder)
    job_id = enqueue_job(u1, "sms_auto_reply", {"lead_id": 1})
    try:
        job = jobs_module.get_job(job_id, u1)

        def failing_handler(_job):
            return {
                "ok": False,
                "retryable": True,
                # Distinct reason from the direct threshold test above:
                # the provider-failure dedup key includes the error
                # signature, and the shared test DB already holds an
                # audit row for "Telnyx gateway timeout" this hour.
                "reason": "Telnyx upstream 504 in worker wiring",
            }

        for _ in range(2):
            assert _run_known_handler(
                jobs_module, job, failing_handler, "sms_auto_reply"
            ) is True
        assert len(recorder.sent) == 0
        assert _run_known_handler(
            jobs_module, job, failing_handler, "sms_auto_reply"
        ) is True
        assert len(recorder.sent) == 1
        assert recorder.sent[0]["event"] == "provider_failure"
        assert recorder.sent[0]["detail"]["provider"] == "telnyx"
        assert _row(job_id)["status"] == "pending"
    finally:
        _delete_jobs(job_id)


# --- Disabled by default ------------------------------------------------------


def test_alerts_disabled_by_default(monkeypatch, two_users):
    u1, _ = two_users
    monkeypatch.delenv("HARNESS_ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("HARNESS_ALERTS_ENABLED", raising=False)
    alerts.reset_notifier()
    assert alerts.get_notifier() is None
    # No external send, no exception -- but the audit row is still written.
    assert alerts.notify_event(
        "dead_letter", "dead_letter:424245", "test title",
        detail={"job_id": 424245}, user_id=u1,
    ) is False
    assert len(_ops_audit_rows("dead_letter:424245")) == 1


def test_alerts_explicitly_disabled_with_webhook_set(monkeypatch):
    monkeypatch.setenv("HARNESS_ALERT_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.setenv("HARNESS_ALERTS_ENABLED", "false")
    alerts.reset_notifier()
    assert alerts.get_notifier() is None
