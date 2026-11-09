"""Independent E2E re-verification of harness failure alerting (launch blocker 2).

Run:  ~/workspace/topai-video-redo/listing-ai/.venv/bin/python \
           scripts/verify_alerting_e2e.py
from ~/workspace/worktrees/launch-alerting.

Nothing leaves loopback: a local HTTP listener on 127.0.0.1 acts as the
webhook receiver, HARNESS_ALERT_WEBHOOK_URL points at it, and all
secrets / phones / addresses used here are invented for this run.
Every assertion reads the actual bytes the listener received.

Scenarios: (A) dead-letter via the real worker, (B) provider failure
after threshold via the real handler path, (C) retry exhaustion ->
dead-letter, (D) spend warning + limit fan-out + dedup, (E) worker
dispatch crash, (F) redaction torture payload (secrets, phone, email,
street address, message body, uuid-hex correlation id), (G) dedup +
simulated-restart durability, (H) failed delivery (HTTP 500) ->
ops_alert_failed row + retry after recovery.

Writes evidence to docs/alerting-e2e-evidence-2026-11-08.json and
exits non-zero on any failure.
"""

import json
import os
import re
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --- env before any app import (mirrors tests/conftest.py) -------------------
_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["APP_ENV"] = "test"
os.environ["ENV"] = "test"
os.environ["DATABASE_PATH"] = _db_path
os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key-not-real")
os.environ.setdefault("FLASK_SECRET_KEY", "test-flask-secret")
os.environ["SUBSCRIPTION_REQUIRED"] = "false"
os.environ["RUN_DEMO_SEED_ON_STARTUP"] = "false"
os.environ.pop("HARNESS_ALERTS_ENABLED", None)
os.environ.pop("HARNESS_ALERT_PROVIDER_FAILURE_THRESHOLD", None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import auth  # noqa: E402
import db  # noqa: E402
from harness import alerts  # noqa: E402
from harness.jobs import enqueue_job, get_job  # noqa: E402

# --- invented values for this run (NOT real credentials / people) ------------
FAKE_KEY = "sk-ant-api03-VERIFYFAKE7777777777"
FAKE_PHONE = "+17205550142"
FAKE_EMAIL = "verify-lead-42@example.org"
FAKE_ADDR1 = "9876 Willow Bend Dr"
FAKE_ADDR2 = "1462 Sonata Ct"
FAKE_TOKEN = "ghp_VERIFYFAKE0123456789"
FAKE_BEARER = "Bearer VERIFYFAKEBearerToken0123456789"
FORBIDDEN = [FAKE_KEY, FAKE_PHONE, FAKE_EMAIL, FAKE_ADDR1, FAKE_ADDR2,
             FAKE_TOKEN, FAKE_BEARER, "VERIFYFAKEBearerToken0123456789"]
PHONE_RE = re.compile(r"\+?1?[\s.\-]?\(?\d{3}\)?[\s.\-]*\d{3}[\s.\-]*\d{4}\b")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
ADDR_RE = re.compile(
    r"\b\d{1,6}\s+(?:[A-Za-z0-9.'#\-]+\s+){0,4}?"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr"
    r"|Court|Ct|Place|Pl|Way|Terrace|Trail|Parkway|Pkwy|Circle|Cir"
    r"|Highway|Hwy)\b\.?", re.IGNORECASE)

RESULTS = {"scenarios": {}, "payloads": []}
FAILURES = []


def check(name, cond, detail=""):
    RESULTS["scenarios"][name] = {"pass": bool(cond), "detail": detail}
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        FAILURES.append(name)


class Listener:
    def __init__(self):
        self.received = []
        self.fail_next = 0
        self._lock = threading.Lock()
        listener = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                with listener._lock:
                    status = 200
                    if listener.fail_next > 0:
                        listener.fail_next -= 1
                        status = 500
                    listener.received.append({
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": raw.decode("utf-8"),
                        "status": status,
                    })
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}' if status == 200
                                 else b'{"ok": false}')

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)

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
        return [json.loads(r["body"]) for r in self.received]

    def of_event(self, event):
        return [p for p in self.payloads() if p.get("event") == event]


def inspect_payloads(tag, payloads, forbidden=FORBIDDEN):
    """Field-by-field leak scan over captured payloads; returns (ok, notes)."""
    notes = []
    ok = True
    for i, payload in enumerate(payloads):
        raw = json.dumps(payload)
        for secret in forbidden:
            if secret in raw:
                ok = False
                notes.append(f"payload[{i}] leaks verbatim: {secret!r}")
        # Regex scan for phone/email/address shapes anywhere in the raw JSON.
        for m in PHONE_RE.finditer(raw):
            # The listener port / counts can look numeric; only flag
            # plausible full numbers (10+ digits).
            digits = re.sub(r"\D", "", m.group(0))
            if len(digits) >= 10:
                ok = False
                notes.append(f"payload[{i}] phone-shaped: {m.group(0)!r}")
        for m in EMAIL_RE.finditer(raw):
            ok = False
            notes.append(f"payload[{i}] email-shaped: {m.group(0)!r}")
        for m in ADDR_RE.finditer(raw):
            ok = False
            notes.append(f"payload[{i}] address-shaped: {m.group(0)!r}")
        # Slack/Discord contract: non-empty text AND content.
        if not payload.get("text") or not payload.get("content"):
            ok = False
            notes.append(f"payload[{i}] missing text/content")
        if payload.get("text") != payload.get("content"):
            ok = False
            notes.append(f"payload[{i}] text != content")
    return ok, "; ".join(notes)


def ops_rows(action, key=None):
    with db.get_db() as conn:
        if key is None:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM harness_audit WHERE action = ?", (action,)).fetchall()]
        return [dict(r) for r in conn.execute(
            "SELECT * FROM harness_audit WHERE action = ? AND gate_reason LIKE ?",
            (action, f"% key={key} %")).fetchall()]


def main():
    db.init_db()
    suffix = uuid.uuid4().hex[:10]
    u1 = db.create_user(f"verify1-{suffix}@example.com",
                        auth.hash_password("password123"))
    alerts._reset_alert_state()
    alerts.reset_notifier()

    with Listener() as listener:
        os.environ["HARNESS_ALERT_WEBHOOK_URL"] = listener.url
        alerts.reset_notifier()
        assert alerts.get_notifier() is not None, "notifier did not resolve"

        # --- A: dead-letter via the real worker ------------------------------
        print("A. dead-letter via real worker (unknown kind)")
        from workers.harness_worker import run_once
        job_a = enqueue_job(u1, "definitely_not_a_kind", {"x": 1})
        assert run_once() is True
        with db.get_db() as conn:
            row_a = dict(conn.execute(
                "SELECT * FROM harness_jobs WHERE id = ?", (job_a,)).fetchone())
        check("A1 worker dead-letters unknown kind",
              row_a["status"] == "dead_letter", f"status={row_a['status']}")
        dead = listener.of_event("dead_letter")
        check("A2 exactly 1 dead_letter POST, HTTP 200",
              len(dead) == 1 and listener.received[0]["status"] == 200,
              f"posts={len(listener.received)}")
        check("A3 correlation_id verbatim (uuid4 hex, not [REDACTED])",
              dead and dead[0]["detail"]["correlation_id"] == row_a["correlation_id"]
              and dead[0]["detail"]["correlation_id"] != "[REDACTED]",
              f"corr={dead[0]['detail']['correlation_id'] if dead else None}")
        check("A4 payload keys only, no values",
              dead and dead[0]["detail"].get("payload_keys") == ["x"],
              f"keys={dead[0]['detail'].get('payload_keys') if dead else None}")

        # --- B: provider failure after threshold (real handler path) ---------
        print("B. provider failure x3 via real handler path")
        from harness import jobs as jobs_module
        from workers.harness_worker import _run_known_handler
        token = uuid.uuid4().hex[:8]
        job_b = enqueue_job(u1, "sms_auto_reply", {"lead_id": 1})
        job = get_job(job_b, u1)

        def failing(_job):
            return {"ok": False, "retryable": True,
                    "reason": f"Telnyx upstream 504 verify-{token}"}

        for _ in range(2):
            _run_known_handler(jobs_module, job, failing, "sms_auto_reply")
        check("B1 no POST before threshold (2 failures)",
              len(listener.of_event("provider_failure")) == 0)
        _run_known_handler(jobs_module, job, failing, "sms_auto_reply")
        prov = listener.of_event("provider_failure")
        check("B2 exactly 1 provider_failure POST after 3rd failure",
              len(prov) == 1, f"posts={len(prov)}")
        check("B3 provider=telnyx, consecutive_failures=3",
              prov and prov[0]["detail"]["provider"] == "telnyx"
              and prov[0]["detail"]["consecutive_failures"] == 3)

        # --- C: retry exhaustion -> dead-letter ------------------------------
        print("C. retry exhaustion (5 attempts) -> dead-letter")
        job_c = enqueue_job(u1, "sms_auto_reply", {"lead_id": 1})
        for _ in range(5):
            with db.get_db() as conn:
                conn.execute(
                    "UPDATE harness_jobs SET attempts = attempts + 1,"
                    " status = 'pending', next_attempt_at = NULL WHERE id = ?",
                    (job_c,))
            job = get_job(job_c, u1)
            _run_known_handler(jobs_module, job, failing, "sms_auto_reply")
            with db.get_db() as conn:
                st = dict(conn.execute(
                    "SELECT status FROM harness_jobs WHERE id = ?",
                    (job_c,)).fetchone())["status"]
            if st == "dead_letter":
                break
        check("C1 job reaches dead_letter after exhaustion", st == "dead_letter",
              f"status={st}")
        dead_c = [p for p in listener.of_event("dead_letter")
                  if p["detail"]["job_id"] == job_c]
        check("C2 exactly 1 dead_letter POST for exhausted job",
              len(dead_c) == 1 and dead_c[0]["detail"]["attempts"] == 5)

        # --- D: spend warning + limit fan-out + dedup ------------------------
        print("D. spend alerts (warning + limit) fan out + dedup")
        base = len(listener.received)
        assert alerts.record_spend_alert(u1, 4000, 5000, "warning") is not None
        assert alerts.record_spend_alert(u1, 5000, 5000, "limit") is not None
        spend = [p for p in listener.payloads()[base:]
                 if p["event"] == "spend_alert"]
        check("D1 warning + limit each POST once (2 POSTs)",
              len(spend) == 2, f"posts={len(spend)}")
        check("D2 levels/spend/cap correct",
              spend and {p["detail"]["level"] for p in spend} == {"warning", "limit"}
              and all(p["detail"]["spend_cents"] in (4000, 5000) for p in spend))
        alerts.record_spend_alert(u1, 5100, 5000, "limit")
        check("D3 repeat limit in same month deduped (no new POST)",
              len(listener.of_event("spend_alert")) == 2)

        # --- E: worker dispatch crash ----------------------------------------
        print("E. worker dispatch crash")
        from unittest.mock import patch
        from workers.harness_worker import process_one_batch
        job_e = enqueue_job(u1, "sms_auto_reply", {"lead_id": 1})
        with patch("workers.harness_worker.dispatch_job",
                   side_effect=RuntimeError("boom-verify-dispatch-crash")):
            assert process_one_batch("test-worker-verify") is True
        wf = listener.of_event("worker_failure")
        check("E1 exactly 1 worker_failure POST",
              len(wf) == 1, f"posts={len(wf)}")
        check("E2 crash text + job context carried",
              wf and "boom-verify-dispatch-crash" in (wf[0]["detail"]["error"] or "")
              and wf[0]["detail"]["context"]["job_id"] == job_e)

        # --- F: redaction torture --------------------------------------------
        print("F. redaction torture payload")
        corr_f = uuid.uuid4().hex  # 32-hex: redact_text alone would kill this
        job_f = {
            "id": 424991,
            "user_id": u1,
            "kind": "sms_auto_reply",
            "attempts": 5,
            "correlation_id": corr_f,
            "payload": {
                "api_key": FAKE_KEY,
                "body": f"Hi, call me at {FAKE_PHONE} about {FAKE_ADDR1}",
                "note": f"token {FAKE_TOKEN} bearer {FAKE_BEARER}",
                "address": FAKE_ADDR1,
                "lead_email": FAKE_EMAIL,
            },
        }
        error_f = (f"send failed for {FAKE_PHONE} at {FAKE_ADDR2}, "
                   f"key was {FAKE_KEY}, contact {FAKE_EMAIL}")
        before = len(listener.received)
        assert alerts.alert_dead_letter(job_f, error_f) is True
        assert len(listener.received) == before + 1
        raw_f = listener.received[-1]["body"]
        payload_f = json.loads(raw_f)
        ok, notes = inspect_payloads("F", [payload_f])
        check("F1 no forbidden value verbatim in payload", ok, notes)
        check("F2 [REDACTED] present in payload", "[REDACTED]" in raw_f)
        check("F3 correlation_id (32-hex) passes through verbatim",
              payload_f["detail"]["correlation_id"] == corr_f,
              f"corr={payload_f['detail']['correlation_id']}")
        check("F4 payload values dropped to key names only",
              payload_f["detail"].get("payload_keys") == sorted(job_f["payload"]),
              f"keys={payload_f['detail'].get('payload_keys')}")
        check("F5 error field redacted, no address/phone/key",
              all(s not in (payload_f["detail"]["error"] or "")
                  for s in (FAKE_PHONE, FAKE_ADDR2, FAKE_KEY, FAKE_EMAIL)))

        # --- G: dedup + simulated restart ------------------------------------
        print("G. dedup + simulated restart")
        job_g = {"id": 424992, "user_id": u1, "kind": "sms_auto_reply",
                 "attempts": 5, "correlation_id": uuid.uuid4().hex,
                 "payload_json": "{}"}
        base = len(listener.received)
        assert alerts.alert_dead_letter(job_g, "permanent boom") is True
        assert alerts.alert_dead_letter(job_g, "permanent boom") is False
        check("G1 same dead-letter twice -> 1 POST",
              len(listener.received) == base + 1)
        alerts._reset_alert_state()  # simulated restart: memory gone
        assert alerts.alert_dead_letter(job_g, "permanent boom") is False
        check("G2 after simulated restart still 1 POST (durable audit row)",
              len(listener.received) == base + 1)
        check("G3 exactly 1 ops_alert audit row for the key",
              len(ops_rows("ops_alert", "dead_letter:424992")) == 1)
        job_g2 = dict(job_g, id=424993, correlation_id=uuid.uuid4().hex)
        assert alerts.alert_dead_letter(job_g2, "permanent boom") is True
        check("G4 genuinely new failure still delivers after restart",
              len(listener.received) == base + 2)

        # --- H: failed delivery -> durable trace + retry ----------------------
        print("H. failed delivery (HTTP 500) -> ops_alert_failed + retry")
        listener.fail_next = 1
        job_h = {"id": 424994, "user_id": u1, "kind": "sms_auto_reply",
                 "attempts": 5, "correlation_id": uuid.uuid4().hex,
                 "payload_json": "{}"}
        base = len(listener.received)
        assert alerts.alert_dead_letter(job_h, "boom") is False
        check("H1 500 delivery returns False",
              listener.received[-1]["status"] == 500)
        check("H2 no ops_alert row after failed delivery (not marked sent)",
              ops_rows("ops_alert", "dead_letter:424994") == [])
        check("H3 exactly 1 durable ops_alert_failed row",
              len(ops_rows("ops_alert_failed", "dead_letter:424994")) == 1)
        assert alerts.alert_dead_letter(job_h, "boom") is True
        check("H4 retry after recovery delivers (HTTP 200)",
              listener.received[-1]["status"] == 200
              and len(listener.received) == base + 2)
        check("H5 exactly 1 ops_alert row after successful retry",
              len(ops_rows("ops_alert", "dead_letter:424994")) == 1)

        # --- global inspection over ALL captured payloads --------------------
        print("Global payload inspection (all captured POSTs)")
        all_payloads = listener.payloads()
        ok, notes = inspect_payloads("global", all_payloads)
        check("ALL no leaks in any captured payload", ok, notes)
        check("ALL every POST returned 200 except the deliberate 500",
              all(r["status"] in (200, 500) for r in listener.received)
              and sum(1 for r in listener.received if r["status"] == 500) == 1,
              f"statuses={[r['status'] for r in listener.received]}")
        check("ALL every payload carries text + content",
              all(p.get("text") and p.get("content") == p.get("text")
                  for p in all_payloads))

        RESULTS["payloads"] = all_payloads
        RESULTS["summary"] = {
            "total_posts": len(listener.received),
            "http_200": sum(1 for r in listener.received if r["status"] == 200),
            "http_500_deliberate": sum(1 for r in listener.received
                                       if r["status"] == 500),
            "events": {e: len(listener.of_event(e)) for e in
                       ("dead_letter", "provider_failure", "worker_failure",
                        "spend_alert")},
            "failures": FAILURES,
        }

    out = ROOT / "docs" / "alerting-e2e-evidence-2026-11-08.json"
    out.write_text(json.dumps(RESULTS, indent=2))
    print(f"\nEvidence written to {out}")
    print(f"Total POSTs: {RESULTS['summary']['total_posts']} "
          f"(200: {RESULTS['summary']['http_200']}, "
          f"deliberate 500: {RESULTS['summary']['http_500_deliberate']})")
    print(f"Events: {RESULTS['summary']['events']}")
    if FAILURES:
        print(f"FAILURES: {FAILURES}")
        return 1
    print("ALL SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
