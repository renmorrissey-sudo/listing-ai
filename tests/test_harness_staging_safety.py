"""Staging safety guards (Phase I hardening, area 2).

Proves staging cannot accidentally send a real SMS or charge a real
card:

* ``HARNESS_MOCK_PROVIDERS`` intercepts every SMS send at the provider
  factory (on by default in staging) — the mock records sends and
  never touches the network.
* ``config.validate_staging_safety()`` refuses to boot staging with a
  live Stripe key, the production Telnyx number, or a real Telnyx key
  with neither mock providers nor trial mode.
* ``harness.policy`` forces ``drafts_only`` in staging (stored settings
  and context-supplied modes), with the sole carve-out being a human
  approval executing — which still re-checks the gate and sends only
  through the mock provider in staging.

Deterministic only: no model, no provider, no network.
"""

import os
import subprocess
import sys

import pytest

import config
from harness import policy
from harness.policy import decide, get_settings


def _set_settings(user_id, mode="drafts_only", paused=False, ai_enabled=True):
    import db

    with db.get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO harness_settings
                (user_id, mode, paused, ai_enabled)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, mode, 1 if paused else 0, 1 if ai_enabled else 0),
        )


@pytest.fixture
def staging(monkeypatch):
    """Pretend this process is staging (APP_ENV=test in the suite).

    Only IS_STAGING is flipped: the guards under test read IS_STAGING,
    and flipping APP_ENV would make the SQLite test database refuse
    connections (db_backend requires Postgres in staging/production).
    """
    monkeypatch.setattr(config, "IS_STAGING", True)
    yield
    # monkeypatch reverts automatically.


# --- Mock provider -----------------------------------------------------------


def test_factory_returns_mock_provider_when_mock_enabled(monkeypatch):
    from sms_providers.factory import get_sms_provider
    from sms_providers.mock import MockSMSProvider

    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", True)
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY_fake_production_key")
    provider = get_sms_provider()
    assert isinstance(provider, MockSMSProvider)
    assert provider.is_configured() is True


def test_mock_provider_records_send_and_never_sends(monkeypatch):
    from sms_providers.factory import get_sms_provider
    from sms_providers.mock import clear_sent_messages, sent_messages

    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", True)
    clear_sent_messages()
    provider = get_sms_provider()
    result = provider.send_message(
        to_number="+13035551234", body="staging demo", from_number="+18888210810"
    )
    assert result["status"] == "mock_sent"
    assert result["provider_message_id"].startswith("mock-")
    recorded = sent_messages()
    assert len(recorded) == 1
    assert recorded[0]["to"] == "+13035551234"
    assert recorded[0]["body"] == "staging demo"
    assert recorded[0]["status"] == "mock_sent"


def test_mock_provider_get_message_status(monkeypatch):
    from sms_providers.factory import get_sms_provider
    from sms_providers.mock import clear_sent_messages

    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", True)
    clear_sent_messages()
    provider = get_sms_provider()
    result = provider.send_message(to_number="+13035551234", body="hello")
    status = provider.get_message_status(result["provider_message_id"])
    assert status["status"] == "mock_sent"
    assert provider.get_message_status("mock-does-not-exist")["status"] == "unknown"


def test_factory_returns_real_provider_when_mock_disabled(monkeypatch):
    from sms_providers.factory import get_sms_provider
    from sms_providers.telnyx import TelnyxSMSProvider

    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", False)
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    assert isinstance(get_sms_provider(), TelnyxSMSProvider)


def _config_mock_flag(env_overrides):
    """Read config.HARNESS_MOCK_PROVIDERS in a fresh interpreter."""
    env = dict(os.environ)
    env.pop("HARNESS_MOCK_PROVIDERS", None)
    env.update(env_overrides)
    proc = subprocess.run(
        [sys.executable, "-c", "import config; print(config.HARNESS_MOCK_PROVIDERS)"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip() == "True"


def test_mock_providers_default_on_in_staging():
    assert _config_mock_flag({"APP_ENV": "staging"}) is True


def test_mock_providers_default_off_outside_staging():
    assert _config_mock_flag({"APP_ENV": "production"}) is False
    assert _config_mock_flag({"APP_ENV": "development"}) is False


def test_mock_providers_explicit_false_wins_in_staging():
    assert (
        _config_mock_flag(
            {"APP_ENV": "staging", "HARNESS_MOCK_PROVIDERS": "false"}
        )
        is False
    )


# --- Boot-time staging safety -------------------------------------------------


def test_staging_safety_inactive_outside_staging(monkeypatch):
    monkeypatch.setattr(config, "IS_STAGING", False)
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_live_fake")
    assert config.validate_staging_safety() == []


def test_staging_safety_refuses_live_stripe_key(staging, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_live_fake")
    errors = config.validate_staging_safety()
    assert any("Stripe" in e for e in errors)


def test_staging_safety_allows_stripe_test_key(staging, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_fake")
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", True)
    assert config.validate_staging_safety() == []


def test_staging_safety_refuses_production_telnyx_number(staging, monkeypatch):
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "+18888210810")
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", True)
    errors = config.validate_staging_safety()
    assert any("production Telnyx number" in e for e in errors)


def test_staging_safety_refuses_real_key_without_mock_or_trial(staging, monkeypatch):
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY_fake")
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", False)
    monkeypatch.setattr(config, "TELNYX_TRIAL_MODE", False)
    errors = config.validate_staging_safety()
    assert any("trial mode" in e for e in errors)


def test_staging_safety_requires_verified_number_in_trial_mode(staging, monkeypatch):
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY_fake")
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", False)
    monkeypatch.setattr(config, "TELNYX_TRIAL_MODE", True)
    monkeypatch.setattr(config, "TELNYX_VERIFIED_TEST_NUMBER", "")
    errors = config.validate_staging_safety()
    assert any("VERIFIED_TEST_NUMBER" in e for e in errors)


def test_staging_safety_clean_configs_pass(staging, monkeypatch):
    # Mock mode (the staging default): a real Telnyx key is harmless
    # because the factory never constructs the real provider.
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY_fake")
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", True)
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", None)
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "")
    assert config.validate_staging_safety() == []
    # Trial-mode fallback: key + trial on + verified number configured.
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", False)
    monkeypatch.setattr(config, "TELNYX_TRIAL_MODE", True)
    monkeypatch.setattr(config, "TELNYX_VERIFIED_TEST_NUMBER", "+13035551234")
    assert config.validate_staging_safety() == []


# --- Staging drafts_only lock ---------------------------------------------------


def test_staging_get_settings_forces_drafts_only(staging, two_users):
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    settings = get_settings(u1)
    assert settings["mode"] == "drafts_only"
    # The stored row is untouched — only the reported mode is locked.
    import db

    with db.get_db() as conn:
        row = conn.execute(
            "SELECT mode FROM harness_settings WHERE user_id = ?", (u1,)
        ).fetchone()
    assert row["mode"] == "authorized"


def test_staging_get_settings_no_user_still_drafts_only(staging):
    assert get_settings(None)["mode"] == "drafts_only"


def test_staging_decide_ignores_context_authorized_mode(staging, two_users):
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    decision, reason = decide(
        u1, "send_sms", "send_sms", {"mode": "authorized", "template_approved": True}
    )
    assert decision == "DRAFT"
    assert reason


def test_staging_decide_honours_human_approval_carve_out(staging, two_users):
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    decision, _ = decide(
        u1,
        "send_sms",
        "send_sms",
        {"mode": "authorized", "human_approval": True},
    )
    assert decision == "ALLOW"


def test_nonstaging_decide_still_honours_context_mode(two_users):
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    decision, _ = decide(u1, "send_sms", "send_sms", {"mode": "authorized"})
    assert decision == "ALLOW"


def test_staging_sms_handler_fallback_path_forces_drafts_only(
    staging, two_users, monkeypatch
):
    from harness import sms_handler

    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    # Force the direct-DB-read fallback by breaking the policy import.
    monkeypatch.setattr(
        policy, "get_settings", lambda user_id: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    mode, paused, _ai_enabled = sms_handler.get_harness_mode(u1)
    assert mode == "drafts_only"
    assert paused is False


def test_approval_execution_context_marks_human_approval(two_users):
    from harness.approvals import _execution_gate_context

    u1, _ = two_users
    context = _execution_gate_context(
        u1, {"phone_number": "+13035551234", "sms_consent_status": "verified"}
    )
    assert context["human_approval"] is True
    assert context["mode"] == "authorized"

# --- Re-verification probes (2026-11-08, launch-staging-verify) ---------------
#
# Second-pass probes over the staging guards. Each probe constructs the
# nastiest staging config it can (real keys for every channel, mock off,
# worker processes instead of the web process) and asserts the config is
# refused or mock-routed. Probes that failed on arrival are marked HOLE
# with the fix that closed them; they stay as regression tests.
#
# HOLE W1 — workers never ran the boot guard: only app.py called
# config.validate_config(), so workers/harness_worker.py and
# workers/sms_campaign_worker.py booted in staging with a live Stripe
# key / production Telnyx number / real keys and no complaint. Fix:
# both workers call config.validate_config() at the start of main().
#
# HOLE W2 — Twilio / SimpleTexting keys were unchecked: the boot guard
# only looked at the Telnyx key, so SMS_PROVIDER=twilio (or
# simpletexting) with real credentials and mock off booted fine and
# could send real SMS. Fix: refuse real credentials for the *active*
# SMS provider when mock providers are off (Telnyx trial mode is a
# Telnyx-only restriction and does not apply to the other providers).
#
# HOLE W3 — SendGrid / SMTP only warned: a SENDGRID_API_KEY (or SMTP
# host) in staging printed a stderr warning and booted, leaving real
# email reachable from the staging UI (password reset via
# email_service.send_email). Fix: refuse both at boot in staging.
# There is no SendGrid test key; staging email stays unset until an
# email mock exists.
#
# HOLE W4 — Vapi / OpenAI keys were unchecked: VOICE_PROVIDER_API_KEY
# lets any logged-in staging user place a real outbound call via
# POST /voice/calls, and OPENAI_API_KEY lets them mint a real metered
# realtime session via POST /api/ask-topai/live/session — both outside
# the harness gate. Fix: refuse both at boot in staging.
#
# HOLE W5 — approved mock sends could never execute: the documented
# staging config set SMS_AI_AUTO_REPLY_ENABLED=false (which makes
# process_inbound_ai return "auto_reply_disabled" even for an approved
# execution) and left TELNYX_TOLL_FREE_VERIFICATION_STATUS unset
# (which makes _auto_reply_allowed return "toll_free_not_verified"
# for SMS_PROVIDER=telnyx). The §4 demo step "approve -> mock send"
# could therefore never pass. Fix: (a) doc now leaves
# SMS_AI_AUTO_REPLY_ENABLED at its default (true) in staging — the
# harness drafts_only lock, not this flag, is the control that stops
# unapproved sends; (b) _auto_reply_allowed skips the Telnyx
# delivery checks (toll-free verification, trial destination) when
# the mock provider is on, because the mock never delivers — consent,
# suppression, quiet-hours, rate-limit and escalation checks still
# apply in full.


# --- Boot guard: other SMS providers ------------------------------------------


def test_staging_safety_refuses_twilio_keys_when_mock_off(staging, monkeypatch):
    monkeypatch.setattr(config, "SMS_PROVIDER", "twilio")
    monkeypatch.setattr(config, "TWILIO_ACCOUNT_SID", "AC_fake_sid")
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "fake_token")
    monkeypatch.setattr(config, "TWILIO_PHONE_NUMBER", "+13035551234")
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", False)
    errors = config.validate_staging_safety()
    assert any("Twilio" in e for e in errors)


def test_staging_safety_refuses_simpletexting_token_when_mock_off(staging, monkeypatch):
    monkeypatch.setattr(config, "SMS_PROVIDER", "simpletexting")
    monkeypatch.setattr(config, "SIMPLETEXTING_API_TOKEN", "fake_token")
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", False)
    errors = config.validate_staging_safety()
    assert any("SimpleTexting" in e for e in errors)


def test_staging_safety_allows_other_provider_keys_when_mock_on(staging, monkeypatch):
    # With the mock on, the factory never constructs a real provider, so
    # real keys for any provider are inert (same principle as the Telnyx
    # key in test_staging_safety_clean_configs_pass).
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", True)
    monkeypatch.setattr(config, "SMS_PROVIDER", "twilio")
    monkeypatch.setattr(config, "TWILIO_ACCOUNT_SID", "AC_fake_sid")
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "fake_token")
    monkeypatch.setattr(config, "SIMPLETEXTING_API_TOKEN", "fake_token")
    assert config.validate_staging_safety() == []


def test_staging_safety_other_provider_keys_inert_when_not_selected(staging, monkeypatch):
    # Twilio keys sitting in the environment while Telnyx is the active
    # provider (mock off, trial on, verified number set) must not trip
    # the guard: nothing in that config can reach Twilio.
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", False)
    monkeypatch.setattr(config, "TELNYX_TRIAL_MODE", True)
    monkeypatch.setattr(config, "TELNYX_VERIFIED_TEST_NUMBER", "+13035551234")
    monkeypatch.setattr(config, "TWILIO_ACCOUNT_SID", "AC_fake_sid")
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "fake_token")
    assert config.validate_staging_safety() == []


# --- Boot guard: email / voice / realtime -------------------------------------


def test_staging_safety_refuses_sendgrid_key(staging, monkeypatch):
    monkeypatch.setattr(config, "SENDGRID_API_KEY", "SG.fake")
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", True)
    errors = config.validate_staging_safety()
    assert any("SendGrid" in e for e in errors)


def test_staging_safety_refuses_smtp_config(staging, monkeypatch):
    monkeypatch.setattr(config, "SENDGRID_API_KEY", "")
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config, "SMTP_FROM_EMAIL", "noreply@example.com")
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", True)
    errors = config.validate_staging_safety()
    assert any("SMTP" in e for e in errors)


def test_staging_safety_refuses_vapi_key(staging, monkeypatch):
    monkeypatch.setattr(config, "VOICE_PROVIDER_API_KEY", "fake_vapi_key")
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", True)
    errors = config.validate_staging_safety()
    assert any("Vapi" in e or "VOICE_PROVIDER_API_KEY" in e for e in errors)


def test_staging_safety_refuses_openai_key(staging, monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-fake")
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", True)
    errors = config.validate_staging_safety()
    assert any("OpenAI" in e or "OPENAI_API_KEY" in e for e in errors)


# --- Workers run the boot guard ------------------------------------------------


def test_harness_worker_main_calls_validate_config(monkeypatch):
    import workers.harness_worker as hw

    calls = []
    monkeypatch.setattr(
        "config.validate_config", lambda: calls.append("validate_config")
    )
    monkeypatch.setattr(hw.db, "init_db", lambda: None)
    monkeypatch.setattr(hw, "_touch_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(hw, "_RUNNING", False)
    hw.main()
    assert calls == ["validate_config"]


def test_sms_campaign_worker_main_calls_validate_config(monkeypatch):
    import workers.sms_campaign_worker as cw

    calls = []
    monkeypatch.setattr(
        "config.validate_config", lambda: calls.append("validate_config")
    )
    monkeypatch.setattr(cw.db, "init_db", lambda: None)
    monkeypatch.setattr(cw.tdb, "touch_worker_heartbeat", lambda *a, **k: True)
    monkeypatch.setattr(cw, "_RUNNING", False)
    cw.main()
    assert calls == ["validate_config"]


def _staging_worker_env(extra):
    """Env for a real staging worker subprocess: plausible staging
    Postgres URL, dummy Anthropic/Flask keys, plus `extra`."""
    from pathlib import Path

    env = dict(os.environ)
    env.update(
        {
            "APP_ENV": "staging",
            "DATABASE_URL": "postgresql://staging_user:pass@staging-db.railway.internal:5432/topai_staging",
            "ANTHROPIC_API_KEY": "dummy-anthropic-key",
            "FLASK_SECRET_KEY": "dummy-flask-secret",
        }
    )
    env.update(extra)
    return env


def test_harness_worker_process_refuses_live_stripe_key_in_staging():
    """End-to-end HOLE W1: the harness worker process itself must exit
    non-zero on a live Stripe key in staging — not boot and process
    jobs. Pre-fix this hangs (worker boots, loops) and the timeout
    fires instead of the clean exit."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    env = _staging_worker_env({"STRIPE_SECRET_KEY": "sk_live_probe"})
    proc = subprocess.run(
        [sys.executable, "-m", "workers.harness_worker"],
        env=env,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 1, proc.stderr[-2000:]
    assert "FATAL" in proc.stderr
    assert "Stripe" in proc.stderr


# --- Mock provider: network isolation ------------------------------------------


def test_mock_provider_makes_no_network_calls(monkeypatch):
    """The mock must record the send even with every network path
    rigged to explode: it never touches the network, so no HTTP call
    can leak a staging send to a real provider."""
    import http.client
    import socket
    import urllib.request

    from sms_providers.factory import get_sms_provider
    from sms_providers.mock import clear_sent_messages, sent_messages

    def _boom(*args, **kwargs):
        raise AssertionError("network call attempted by mock provider")

    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", True)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    monkeypatch.setattr(http.client.HTTPConnection, "request", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(socket.socket, "connect", _boom)
    clear_sent_messages()
    provider = get_sms_provider()
    result = provider.send_message(
        to_number="+13035551234", body="staging demo", from_number="+18888210810"
    )
    assert result["status"] == "mock_sent"
    recorded = sent_messages()
    assert len(recorded) == 1
    assert recorded[0]["to"] == "+13035551234"
    assert recorded[0]["body"] == "staging demo"


def test_mock_provider_source_imports_no_network_libraries():
    """Static backstop for the test above: sms_providers/mock.py must
    not import any network library at all, so no future edit can
    silently add a real call behind the mock's back."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[1].joinpath(
        "sms_providers", "mock.py"
    ).read_text()
    for lib in ("urllib", "requests", "httpx", "socket", "http.client", "smtplib"):
        assert f"import {lib}" not in source, lib


# --- Staging drafts_only lock at the job level + approval carve-out ------------
#
# These run the real send path (patched coach only — no Anthropic call)
# through the mock provider, under the documented staging config:
# mock on, trial mode on, toll-free status UNSET, verified test number
# deliberately DIFFERENT from the lead's number (so both the toll-free
# and the trial-destination skips in _auto_reply_allowed are exercised).


def _staging_mock_config(monkeypatch, *, auto_reply=True):
    monkeypatch.setattr(config, "HARNESS_MOCK_PROVIDERS", True)
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY_fake")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "")
    monkeypatch.setattr(config, "TELNYX_TRIAL_MODE", True)
    monkeypatch.setattr(config, "TELNYX_VERIFIED_TEST_NUMBER", "+13035559999")
    monkeypatch.setattr(config, "TELNYX_TOLL_FREE_VERIFICATION_STATUS", "")
    monkeypatch.setattr(config, "SMS_AI_AUTO_REPLY_ENABLED", auto_reply)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 0)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 0)


def _staging_lead(user_id, phone):
    from lead_service import upsert_crm_lead

    lead_id, _, _ = upsert_crm_lead(
        user_id,
        phone,
        {"name": "Staging Lead", "phone_number": phone},
        source="test",
        touch_sms=True,
        assigned_user_id=user_id,
    )
    return lead_id


def _staging_approval(user_id, lead_id, inbound_id=424242, draft_extra=None):
    import json as _json
    from datetime import datetime, timezone

    import db

    draft = {
        "draft_reply": "Hi! Yes, it is still available.",
        "lead_id": lead_id,
        "inbound_id": inbound_id,
    }
    if draft_extra:
        draft.update(draft_extra)
    with db.get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO harness_approvals
                (user_id, job_id, kind, draft_json, status, created_at)
            VALUES (?, ?, 'sms_reply', ?, 'pending', ?)
            """,
            (
                user_id,
                None,
                _json.dumps(draft),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return cur.lastrowid


def _staging_analysis():
    return {
        "summary": "Lead asked about availability.",
        "intent": "question",
        "draft_reply": "Hi! Yes, it is still available.",
        "suggested_reply": "Hi! Yes, it is still available.",
        "sensitive_topic": False,
        "requires_manual_review": False,
        "escalation_topics": [],
        "suggested_lead_status": "contacted",
        "appointment_details": None,
        "appointment_requested": False,
    }


def _approval_row(approval_id):
    import db

    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM harness_approvals WHERE id = ?", (approval_id,)
        ).fetchone()
    return dict(row) if row else None


class _GateOpen:
    """Force the execution-time gate inputs open (no quiet hours, no
    rate limit, not suppressed) so execute tests are time-independent.
    Mirrors tests/test_harness_approvals.py::_gate_open."""

    def __enter__(self):
        from unittest.mock import patch

        self._patches = [
            patch("sms_quiet_hours.in_quiet_hours", return_value=False),
            patch("sms_authorization._rate_limited", return_value=False),
            patch("tenant_sms_db.is_suppressed", return_value=False),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


def test_staging_sms_auto_reply_job_drafts_and_never_sends(
    staging, two_users, monkeypatch
):
    """Job-level drafts_only lock: stored mode 'authorized', but in
    staging the job drafts, creates a pending approval, and the mock
    records zero sends."""
    import uuid
    from unittest.mock import patch

    from harness.jobs import enqueue_job
    from harness.sms_handler import process_sms_auto_reply_job
    from sms_providers.mock import clear_sent_messages, sent_messages

    import db

    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    lead_phone = "+13035550101"
    lead_id = _staging_lead(u1, lead_phone)
    clear_sent_messages()
    job_id = enqueue_job(
        u1,
        "sms_auto_reply",
        {
            "lead_id": lead_id,
            "inbound_id": 424242,
            "body": "Hi, is the house still available?",
            "receiving_number": "+18880000000",
        },
        idempotency_key=f"sms-auto-reply-staging-{uuid.uuid4().hex[:8]}",
    )
    with db.get_db() as conn:
        job = dict(
            conn.execute(
                "SELECT * FROM harness_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        )
    with patch(
        "sms_inbound.analyze_inbound_and_coach",
        return_value={
            "analysis": _staging_analysis(),
            "insight_id": None,
            "error": None,
        },
    ):
        result = process_sms_auto_reply_job(job)
    assert result["decision"] == "DRAFT"
    assert result["sent"] is False
    assert result["approval_id"] is not None
    assert _approval_row(result["approval_id"])["status"] == "pending"
    assert sent_messages() == []


def test_staging_approved_execution_sends_exactly_one_mock_send(
    staging, two_users, monkeypatch
):
    """Human-approval carve-out, end to end through the real send path:
    approve -> execute -> exactly one mock send, approval executed.
    HOLE W5: pre-fix this failed — the documented staging config made
    process_inbound_ai refuse with toll_free_not_verified (and, with
    the old SMS_AI_AUTO_REPLY_ENABLED=false doc value, with
    auto_reply_disabled)."""
    from unittest.mock import patch

    from harness.approvals import (
        approve_approval,
        execute_approved,
    )
    from sms_providers.mock import clear_sent_messages, sent_messages

    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    lead_phone = "+13035550102"
    lead_id = _staging_lead(u1, lead_phone)
    approval_id = _staging_approval(u1, lead_id)
    assert approve_approval(u1, approval_id) is not None
    clear_sent_messages()
    with _GateOpen(), patch(
        "sms_inbound.analyze_inbound_and_coach",
        return_value={
            "analysis": _staging_analysis(),
            "insight_id": None,
            "error": None,
        },
    ):
        result = execute_approved(u1, approval_id)
    assert result["sent"] is True, result
    assert result["decision"] == "ALLOW"
    assert _approval_row(approval_id)["status"] == "executed"
    recorded = sent_messages()
    assert len(recorded) == 1
    assert recorded[0]["to"] == lead_phone
    assert recorded[0]["body"] == "Hi! Yes, it is still available."
    assert recorded[0]["status"] == "mock_sent"


def test_staging_approved_execution_blocked_when_lead_opts_out(
    staging, two_users, monkeypatch
):
    """Carve-out re-check: the lead opts out after approval — the
    execution-time policy re-check blocks the send, the approval ends
    blocked, and the mock records zero sends."""
    from unittest.mock import patch

    from harness.approvals import approve_approval, execute_approved
    from sms_providers.mock import clear_sent_messages, sent_messages

    import db

    _staging_mock_config(monkeypatch)
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    lead_id = _staging_lead(u1, "+13035550103")
    approval_id = _staging_approval(u1, lead_id)
    assert approve_approval(u1, approval_id) is not None
    db.mark_lead_opt_out(lead_id, u1)
    clear_sent_messages()
    with _GateOpen(), patch(
        "sms_inbound.analyze_inbound_and_coach",
        return_value={
            "analysis": _staging_analysis(),
            "insight_id": None,
            "error": None,
        },
    ):
        result = execute_approved(u1, approval_id)
    assert result["sent"] is False
    assert result["decision"] == "BLOCK"
    assert _approval_row(approval_id)["status"] == "blocked"
    assert sent_messages() == []


def test_staging_auto_reply_flag_off_blocks_approved_execution(
    staging, two_users, monkeypatch
):
    """Locks in the HOLE W5 doc correction: with
    SMS_AI_AUTO_REPLY_ENABLED=false (the old documented staging
    value), even an approved execution cannot send — the flag gates
    process_inbound_ai itself. Staging must therefore leave the flag
    at its default (true); the harness drafts_only lock is the control
    that stops unapproved sends. ``auto_reply_disabled`` is a
    deterministic decline, so the approval settles as blocked (never
    sent, never retried) rather than reverting to approved."""
    from unittest.mock import patch

    from harness.approvals import approve_approval, execute_approved
    from sms_providers.mock import clear_sent_messages, sent_messages

    _staging_mock_config(monkeypatch, auto_reply=False)
    u1, _ = two_users
    _set_settings(u1, mode="authorized", paused=False)
    lead_id = _staging_lead(u1, "+13035550104")
    approval_id = _staging_approval(u1, lead_id)
    assert approve_approval(u1, approval_id) is not None
    clear_sent_messages()
    with _GateOpen(), patch(
        "sms_inbound.analyze_inbound_and_coach",
        return_value={
            "analysis": _staging_analysis(),
            "insight_id": None,
            "error": None,
        },
    ):
        result = execute_approved(u1, approval_id)
    assert result["sent"] is False
    assert result["reason"] == "auto_reply_disabled"
    assert result["retryable"] is False
    assert _approval_row(approval_id)["status"] == "blocked"
    assert sent_messages() == []
