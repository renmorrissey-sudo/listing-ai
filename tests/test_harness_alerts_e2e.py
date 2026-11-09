"""End-to-end alerting proof (M4 hardening).

Unlike tests/test_harness_alerts.py, which injects mock notifiers, these
tests prove real delivery: a local HTTP server *we control* acts as the
webhook receiver, ``HARNESS_ALERT_WEBHOOK_URL`` points at it (test env
only), and real failed-job / retry / dead-letter events are driven
through the harness worker/queue. Every assertion reads the actual
bytes the listener received — including the HTTP status the listener
returned — so a PASS here means the webhook path works, not just that
a mock was called.

Nothing here leaves the loopback interface: no real SMS, call, email,
or external webhook. The listener binds 127.0.0.1 on an ephemeral port.
"""

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import db
from harness import alerts
from harness.jobs import enqueue_job, get_job


@pytest.fixture(autouse=True)
def _clean_alert_state():
    alerts._reset_alert_state()
    alerts.reset_notifier()
    yield
    alerts._reset_alert_state()
    alerts.reset_notifier()


@pytest.fixture(autouse=True)
def _isolate_job_queue():
    db.init_db()
    with db.get_db() as conn:
        conn.execute("DELETE FROM harness_jobs")
    yield


class _Listener:
    """A controllable local webhook receiver.

    ``received`` holds one dict per POST: {"path", "headers", "body",
    "status_returned"}. ``fail_first_n`` makes the listener answer 500
    for the first N requests (then 200), to prove failed-delivery
    retry behaviour. ``strict`` makes the listener enforce the
    documented Slack/Discord incoming-webhook contract (a payload must
    carry a non-empty ``text`` for Slack and ``content`` for Discord,
    else HTTP 400) — the pre-fix payload shape fails this, the fixed
    shape passes it. ``status_returned`` records what the listener
    actually answered, so tests assert delivery status codes, not
    assumptions.
    """

    def __init__(self, strict=False):
        self.received = []
        self.fail_first_n = 0
        self.strict = strict
        self._lock = threading.Lock()
        listener = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server naming
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                with listener._lock:
                    status = 200
                    if len(listener.received) < listener.fail_first_n:
                        status = 500
                    elif listener.strict:
                        try:
                            body = json.loads(raw.decode("utf-8"))
                        except (ValueError, UnicodeDecodeError):
                            body = {}
                        if not body.get("text") or not body.get("content"):
                            status = 400
                    listener.received.append(
                        {
                            "path": self.path,
                            "headers": dict(self.headers),
                            "body": raw,
                            "status_returned": status,
                        }
                    )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    b'{"ok": true}'
                    if status == 200
                    else b'{"ok": false, "error": "no_text_or_content"}'
                )

            def log_message(self, *args):  # silence stderr per request
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/hook"

    def payloads(self):
        return [json.loads(r["body"].decode("utf-8")) for r in self.received]


@pytest.fixture
def webhook(monkeypatch):
    """Point the real env-configured notifier at a local listener."""
    with _Listener() as listener:
        monkeypatch.setenv("HARNESS_ALERT_WEBHOOK_URL", listener.url)
        monkeypatch.delenv("HARNESS_ALERTS_ENABLED", raising=False)
        monkeypatch.delenv("HARNESS_ALERT_PROVIDER_FAILURE_THRESHOLD", raising=False)
        alerts.reset_notifier()
        assert alerts.get_notifier() is not None
        yield listener


def _row(job_id):
    with db.get_db() as conn:
        return dict(
            conn.execute(
                "SELECT * FROM harness_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        )


def _delete_jobs(*job_ids):
    with db.get_db() as conn:
        for job_id in job_ids:
            conn.execute("DELETE FROM harness_jobs WHERE id = ?", (job_id,))


def _ops_audit_rows(key=None, action="ops_alert"):
    with db.get_db() as conn:
        if key is None:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM harness_audit WHERE action = ?", (action,)
                ).fetchall()
            ]
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM harness_audit WHERE action = ? "
                "AND gate_reason LIKE ?",
                (action, f"% key={key} %"),
            ).fetchall()
        ]


# --- Real delivery through the worker ----------------------------------------


def test_e2e_dead_letter_delivers_real_http_post(webhook, two_users):
    """Unknown-kind job -> worker dead-letters it -> one real HTTP POST."""
    from workers.harness_worker import run_once

    u1, _ = two_users
    job_id = enqueue_job(u1, "definitely_not_a_kind", {"x": 1})
    try:
        assert run_once() is True
        assert _row(job_id)["status"] == "dead_letter"
        assert len(webhook.received) == 1
        req = webhook.received[0]
        assert req["status_returned"] == 200
        assert req["headers"].get("Content-Type") == "application/json"
        alert = json.loads(req["body"].decode("utf-8"))
        assert alert["event"] == "dead_letter"
        assert alert["detail"]["job_id"] == job_id
        assert alert["detail"]["kind"] == "definitely_not_a_kind"
        # Diagnostics: tenant scope + failure reason + correlation id.
        assert alert["detail"]["user_id"] == u1
        assert alert["detail"]["correlation_id"] == _row(job_id)["correlation_id"]
        assert alert["detail"]["correlation_id"]
        assert "unknown_kind" in (alert["detail"]["error"] or "")
        assert alert["detail"]["attempts"] == 1
        assert alert["timestamp"]
    finally:
        _delete_jobs(job_id)


def test_e2e_provider_failure_delivers_after_threshold(webhook, two_users):
    """Three retryable Telnyx failures via the worker -> one real POST."""
    from harness import jobs as jobs_module
    from workers.harness_worker import _run_known_handler

    u1, _ = two_users
    token = uuid.uuid4().hex[:8]
    job_id = enqueue_job(u1, "sms_auto_reply", {"lead_id": 1})
    try:
        job = get_job(job_id, u1)

        def failing_handler(_job):
            return {
                "ok": False,
                "retryable": True,
                "reason": f"Telnyx upstream 504 e2e-{token}",
            }

        for _ in range(2):
            assert _run_known_handler(
                jobs_module, job, failing_handler, "sms_auto_reply"
            ) is True
        assert len(webhook.received) == 0
        assert _run_known_handler(
            jobs_module, job, failing_handler, "sms_auto_reply"
        ) is True
        assert len(webhook.received) == 1
        assert webhook.received[0]["status_returned"] == 200
        alert = webhook.payloads()[0]
        assert alert["event"] == "provider_failure"
        assert alert["detail"]["provider"] == "telnyx"
        assert alert["detail"]["consecutive_failures"] == 3
        assert alert["detail"]["user_id"] == u1
        assert alert["detail"]["correlation_id"] == _row(job_id)["correlation_id"]
        assert _row(job_id)["status"] == "pending"
    finally:
        _delete_jobs(job_id)


def test_e2e_worker_failure_delivers_real_http_post(webhook, two_users):
    """A dispatch crash -> one real worker_failure POST with job context."""
    from unittest.mock import patch

    from workers.harness_worker import process_one_batch

    u1, _ = two_users
    job_id = enqueue_job(u1, "sms_auto_reply", {"lead_id": 1})
    try:
        with patch(
            "workers.harness_worker.dispatch_job",
            side_effect=RuntimeError("boom-test-e2e-dispatch-crash"),
        ):
            assert process_one_batch("test-worker") is True
        failures = [
            a for a in webhook.payloads() if a["event"] == "worker_failure"
        ]
        assert len(failures) == 1
        assert webhook.received[0]["status_returned"] == 200
        assert "boom-test-e2e-dispatch-crash" in failures[0]["detail"]["error"]
        assert failures[0]["detail"]["context"]["job_id"] == job_id
        assert failures[0]["detail"]["correlation_id"] == _row(job_id)["correlation_id"]
        assert _row(job_id)["status"] == "pending"
    finally:
        _delete_jobs(job_id)


def test_e2e_retry_exhaustion_dead_letters_and_alerts(webhook, two_users):
    """5th retryable failure exhausts MAX_ATTEMPTS -> dead_letter + POST."""
    from harness import jobs as jobs_module
    from workers.harness_worker import _run_known_handler

    u1, _ = two_users
    token = uuid.uuid4().hex[:8]
    job_id = enqueue_job(u1, "sms_auto_reply", {"lead_id": 1})
    try:
        job = get_job(job_id, u1)

        def failing_handler(_job):
            return {
                "ok": False,
                "retryable": True,
                "reason": f"Telnyx upstream 504 exhaustion-{token}",
            }

        for _ in range(5):
            # Simulate the claim that precedes each dispatch: claiming
            # bumps attempts, and fail_job dead-letters once attempts
            # reaches MAX_ATTEMPTS (5) on a retryable failure.
            with db.get_db() as conn:
                conn.execute(
                    "UPDATE harness_jobs SET attempts = attempts + 1,"
                    " status = 'pending', next_attempt_at = NULL"
                    " WHERE id = ?",
                    (job_id,),
                )
            job = get_job(job_id, u1)
            assert _run_known_handler(
                jobs_module, job, failing_handler, "sms_auto_reply"
            ) is True
            if _row(job_id)["status"] == "dead_letter":
                break
        assert _row(job_id)["status"] == "dead_letter"
        dead = [a for a in webhook.payloads() if a["event"] == "dead_letter"]
        assert len(dead) == 1
        assert dead[0]["detail"]["job_id"] == job_id
        assert dead[0]["detail"]["attempts"] == 5
    finally:
        _delete_jobs(job_id)


# --- Redaction in the real payload path ---------------------------------------


def test_e2e_redaction_in_real_webhook_payload(webhook, two_users):
    """Secrets / phone / email / street address must not appear verbatim
    in the actual HTTP body the webhook receives."""
    u1, _ = two_users
    fake_key = "sk-ant-api03-FAKESECRETKEY1234567890"
    fake_phone = "+13035550199"
    fake_email = "lead-person-99@example.com"
    fake_address = "1462 Sonata Ct"
    fake_token = "ghp_FAKEGITHUBTOKEN1234567890"
    job = {
        "id": 424251,
        "user_id": u1,
        "kind": "sms_auto_reply",
        "attempts": 5,
        "correlation_id": "e2e-corr-" + uuid.uuid4().hex[:12],
        "payload": {
            "api_key": fake_key,
            "body": f"call me at {fake_phone} ({fake_email}) about {fake_address}",
            "note": f"my token is {fake_token}",
            "address": fake_address,
        },
    }
    error = (
        f"send failed for {fake_phone} at {fake_address}, "
        f"key was {fake_key}, contact {fake_email}"
    )
    assert alerts.alert_dead_letter(job, error) is True
    assert len(webhook.received) == 1
    assert webhook.received[0]["status_returned"] == 200
    raw = webhook.received[0]["body"].decode("utf-8")
    for secret in (fake_key, fake_phone, fake_email, fake_address, fake_token):
        assert secret not in raw, f"leaked verbatim into webhook payload: {secret!r}"
    assert "[REDACTED]" in raw
    # Payload values never leave as values — only key names travel.
    alert = json.loads(raw)
    assert alert["detail"]["payload_keys"] == ["address", "api_key", "body", "note"]


# --- Dedup + restart recovery ---------------------------------------------------


def test_e2e_dedup_same_failure_twice_one_delivery(webhook, two_users):
    """Same dead-letter twice -> one POST; 'restart' (memory cleared)
    -> still one POST (durable audit row); a NEW failure still delivers."""
    u1, _ = two_users
    job = {
        "id": 424252,
        "user_id": u1,
        "kind": "sms_auto_reply",
        "attempts": 5,
        "correlation_id": "e2e-corr-" + uuid.uuid4().hex[:12],
        "payload_json": "{}",
    }
    assert alerts.alert_dead_letter(job, "permanent boom") is True
    assert alerts.alert_dead_letter(job, "permanent boom") is False
    assert len(webhook.received) == 1
    # Simulated restart: in-memory dedup is gone, durable row remains.
    alerts._reset_alert_state()
    assert alerts.alert_dead_letter(job, "permanent boom") is False
    assert len(webhook.received) == 1
    assert len(_ops_audit_rows("dead_letter:424252")) == 1
    # Alerts resume after restart: a genuinely new failure still sends.
    job2 = dict(job, id=424253, correlation_id="e2e-corr-" + uuid.uuid4().hex[:12])
    assert alerts.alert_dead_letter(job2, "permanent boom") is True
    assert len(webhook.received) == 2


def test_e2e_provider_counter_resets_on_restart(webhook, two_users):
    """Documented consequence of the in-memory failure counter: after a
    restart, 3 fresh consecutive failures are needed before the next
    provider alert, even if the provider never recovered."""
    u1, _ = two_users
    token = uuid.uuid4().hex[:8]
    kwargs = dict(kind="sms_auto_reply", user_id=u1, threshold=3)
    for _ in range(2):
        assert alerts.alert_provider_failure(
            "telnyx", f"Telnyx gateway timeout restart-{token}", **kwargs
        ) is False
    alerts._reset_alert_state()  # simulated restart
    assert alerts.alert_provider_failure(
        "telnyx", f"Telnyx gateway timeout restart-{token}", **kwargs
    ) is False
    assert len(webhook.received) == 0
    assert alerts.alert_provider_failure(
        "telnyx", f"Telnyx gateway timeout restart-{token}", **kwargs
    ) is False
    assert alerts.alert_provider_failure(
        "telnyx", f"Telnyx gateway timeout restart-{token}", **kwargs
    ) is True
    assert len(webhook.received) == 1


# --- Slack/Discord payload shape -----------------------------------------------


def test_e2e_payload_carries_slack_discord_text_fields(webhook, two_users):
    """Every delivered alert carries the same redacted one-liner under
    both ``text`` (Slack) and ``content`` (Discord)."""
    u1, _ = two_users
    job = {
        "id": 424256,
        "user_id": u1,
        "kind": "sms_auto_reply",
        "attempts": 5,
        "correlation_id": "e2e-corr-" + uuid.uuid4().hex[:12],
        "payload_json": "{}",
    }
    assert alerts.alert_dead_letter(job, "boom") is True
    assert len(webhook.received) == 1
    alert = webhook.payloads()[0]
    assert alert["text"]
    assert alert["content"] == alert["text"]
    assert "Harness job 424256 dead-lettered" in alert["text"]
    assert "dead_letter:424256" in alert["text"]
    assert len(alert["text"]) < 2000  # inside Discord's content limit


def test_e2e_strict_slack_discord_contract_accepts_payload(
    monkeypatch, two_users
):
    """A listener enforcing the documented Slack/Discord contract
    (non-empty text AND content, else HTTP 400) accepts the alert —
    the pre-fix payload shape (no text/content) would be rejected."""
    u1, _ = two_users
    with _Listener(strict=True) as listener:
        monkeypatch.setenv("HARNESS_ALERT_WEBHOOK_URL", listener.url)
        monkeypatch.delenv("HARNESS_ALERTS_ENABLED", raising=False)
        alerts.reset_notifier()
        job = {
            "id": 424257,
            "user_id": u1,
            "kind": "sms_auto_reply",
            "attempts": 5,
            "correlation_id": "e2e-corr-" + uuid.uuid4().hex[:12],
            "payload_json": "{}",
        }
        assert alerts.alert_dead_letter(job, "boom") is True
        assert len(listener.received) == 1
        assert listener.received[0]["status_returned"] == 200


def test_e2e_strict_listener_rejects_payload_without_text_fields():
    """Control for the strict listener: a body with neither text nor
    content is rejected with 400, as Slack/Discord document."""
    import urllib.request

    with _Listener(strict=True) as listener:
        req = urllib.request.Request(
            listener.url,
            data=json.dumps({"event": "dead_letter"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("expected HTTP 400 from strict listener")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
        assert listener.received[0]["status_returned"] == 400


# --- Failed delivery: durable trace + retry ------------------------------------


def test_e2e_failed_delivery_retries_after_webhook_recovers(webhook, two_users):
    """Listener answers 500 -> alert returns False, no ops_alert row is
    written (so the event is NOT marked sent), a durable failed-delivery
    row IS written, and the same event delivers once the listener
    recovers."""
    u1, _ = two_users
    webhook.fail_first_n = 1
    job = {
        "id": 424254,
        "user_id": u1,
        "kind": "sms_auto_reply",
        "attempts": 5,
        "correlation_id": "e2e-corr-" + uuid.uuid4().hex[:12],
        "payload_json": "{}",
    }
    assert alerts.alert_dead_letter(job, "boom") is False
    assert len(webhook.received) == 1
    assert webhook.received[0]["status_returned"] == 500
    assert _ops_audit_rows("dead_letter:424254") == []
    assert len(_ops_audit_rows("dead_letter:424254", action="ops_alert_failed")) == 1
    # Listener recovered: the same event retries and delivers.
    assert alerts.alert_dead_letter(job, "boom") is True
    assert len(webhook.received) == 2
    assert webhook.received[1]["status_returned"] == 200
    assert len(_ops_audit_rows("dead_letter:424254")) == 1


# --- Spend alerts reach the webhook ----------------------------------------------


def test_e2e_spend_alert_reaches_webhook(webhook, two_users):
    """A spend-limit alert is not audit-only: with a webhook configured
    it is also POSTed (deduped per tenant/level/month)."""
    u1, _ = two_users
    audit_id = alerts.record_spend_alert(u1, 5000, 5000, "limit")
    assert audit_id is not None
    assert len(webhook.received) == 1
    assert webhook.received[0]["status_returned"] == 200
    alert = webhook.payloads()[0]
    assert alert["event"] == "spend_alert"
    assert alert["detail"]["user_id"] == u1
    assert alert["detail"]["level"] == "limit"
    assert alert["detail"]["spend_cents"] == 5000
    assert alert["detail"]["cap_cents"] == 5000
    # Second call in the same month: deduped, no second POST.
    assert alerts.record_spend_alert(u1, 5100, 5000, "limit") is not None
    assert len(webhook.received) == 1


def test_e2e_spend_alert_audit_only_when_no_webhook(monkeypatch, two_users):
    """With no webhook configured, a spend alert still writes its audit
    row but creates no ops_alert row and sends nothing."""
    u1, _ = two_users
    monkeypatch.delenv("HARNESS_ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("HARNESS_ALERTS_ENABLED", raising=False)
    alerts.reset_notifier()
    before = len(_ops_audit_rows())
    assert alerts.record_spend_alert(u1, 5000, 5000, "limit") is not None
    assert len(_ops_audit_rows()) == before


# --- Disabled by default ---------------------------------------------------------


def test_e2e_no_external_send_when_no_webhook_configured(monkeypatch, two_users):
    """No webhook URL -> get_notifier() is None and a dead-letter alert
    writes only the audit row; nothing is POSTed anywhere."""
    u1, _ = two_users
    monkeypatch.delenv("HARNESS_ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("HARNESS_ALERTS_ENABLED", raising=False)
    alerts.reset_notifier()
    assert alerts.get_notifier() is None
    with _Listener() as listener:  # a listener exists, but nothing points at it
        job = {
            "id": 424255,
            "user_id": u1,
            "kind": "sms_auto_reply",
            "attempts": 5,
            "payload_json": "{}",
        }
        assert alerts.alert_dead_letter(job, "boom") is False
        assert listener.received == []
        assert len(_ops_audit_rows("dead_letter:424255")) == 1


# --- Redaction unit checks + dedup-set cap ---------------------------------------


def test_redact_value_street_address_and_false_positives():
    """Street addresses are redacted; ordinary numbers/phrases are not."""
    assert alerts._redact_value("meet at 1462 Sonata Ct tomorrow") == (
        "meet at [REDACTED] tomorrow"
    )
    assert alerts._redact_value("8533 S Miller Ct, Littleton") == (
        "[REDACTED], Littleton"
    )
    assert alerts._redact_value("123 Main Street, Apt 4") == "[REDACTED], Apt 4"
    # No house number + suffix: untouched.
    assert alerts._redact_value("Main Street is closed") == "Main Street is closed"
    # Ordinary numbers and error codes: untouched.
    assert alerts._redact_value("attempt 3 failed, error 500") == (
        "attempt 3 failed, error 500"
    )
    assert alerts._redact_value("Telnyx upstream 504") == "Telnyx upstream 504"
    # Secrets + phone + email still redacted in the same pass.
    assert "[REDACTED]" in alerts._redact_value(
        "key sk-ant-api03-FAKESECRETKEY1234567890 call +13035550199"
    )


def test_sent_keys_capped_and_durable_backstop_survives(two_users):
    """Over 5000 in-memory dedup keys, non-hour keys are dropped; the
    audit row still dedups, so no duplicate alert is ever sent."""
    u1, _ = two_users

    class RecordingNotifier:
        def __init__(self):
            self.sent = []

        def send(self, alert):
            self.sent.append(alert)

    for i in range(5001):
        alerts._sent_keys.add(f"dead_letter:dead_letter:{900000 + i}")
    alerts._prune_hour_keys()
    assert len(alerts._sent_keys) <= 5000
    # A dropped key whose audit row exists still does not re-send.
    # (910000 is outside the stuffed 900000-905000 range, so its first
    # send is not suppressed by the stuffed in-memory keys.)
    recorder = RecordingNotifier()
    job = {
        "id": 910000,
        "user_id": u1,
        "kind": "sms_auto_reply",
        "attempts": 5,
        "payload_json": "{}",
    }
    assert alerts.alert_dead_letter(job, "boom", notifier=recorder) is True
    assert len(recorder.sent) == 1
    alerts._sent_keys.discard("dead_letter:dead_letter:910000")
    assert alerts.alert_dead_letter(job, "boom", notifier=recorder) is False
    assert len(recorder.sent) == 1
