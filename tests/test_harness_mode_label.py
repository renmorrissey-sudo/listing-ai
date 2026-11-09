"""Regression tests: /harness mode label must agree with the status API.

Background (2026-11-08 production discrepancy): the /harness page was
reported to show Mode "Live" ("Live automation is active. New leads and
follow-ups can trigger calls and texts.") while GET /api/harness/status
reported mode "drafts_only", and the page was also reported to say "SMS
is paused". No code in the repo at either deploy could render any of
those strings: the template renders ``{{ mode }}`` from the same
``get_harness_status()`` the API returns, ``policy.get_settings()``
rejects any mode outside drafts_only/supervised/authorized (an invalid
stored mode falls back to drafts_only), and there is no per-channel SMS
pause flag at all -- the only pause is the harness-wide ``paused`` in
``harness_settings``. The reported page content therefore could not have
come from the deployed code, but the page had real gaps the discrepancy
exposed: a hardcoded intro claiming every account runs in Drafts Only,
no SMS-pause or voice state, and no actual outbound-activity counts.

These tests pin the contract so a UI/API disagreement can never ship
silently again: for every mode x paused combination the rendered page
and the JSON API must agree, must use the human label derived from the
authoritative mode value, and must never show a contradictory "live
automation" claim while the tenant is in drafts_only or paused. They
also cover the case where live mode is configured but a channel is
paused (harness pause pauses all automated sending, including SMS).

All sends are mocked / counted from stored rows only; nothing here sends
a real SMS, places a real call, or touches a provider.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import db


def _now():
    return datetime.now(timezone.utc).isoformat()


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        user = db.get_user_by_id(user_id)
        sess["session_version"] = int((user or {}).get("session_version") or 1)


def _set_settings(user_id, mode, paused, ai_enabled=True):
    db.init_db()
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO harness_settings
                (user_id, mode, paused, ai_enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (user_id) DO UPDATE SET
                mode = excluded.mode,
                paused = excluded.paused,
                ai_enabled = excluded.ai_enabled,
                updated_at = excluded.updated_at
            """,
            (user_id, mode, 1 if paused else 0, 1 if ai_enabled else 0, _now(), _now()),
        )


def _seed_audit(user_id, action, gate_decision):
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO harness_audit
                (user_id, actor, action, mode, gate_decision, gate_reason, created_at)
            VALUES (?, 'ai', ?, 'drafts_only', ?, 'test seed', ?)
            """,
            (user_id, action, gate_decision, _now()),
        )


def _seed_sms_sent(user_id):
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO sms_messages
                (user_id, phone_number, message_body, direction, status, created_at)
            VALUES (?, '+13035550100', 'seed sent message', 'outbound', 'sent', ?)
            """,
            (user_id, _now()),
        )


def _seed_call_placed(user_id):
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO voice_calls
                (user_id, provider, direction, phone_number, status, created_at)
            VALUES (?, 'vapi', 'outbound', '+13035550100', 'completed', ?)
            """,
            (user_id, _now()),
        )


MODE_LABELS = {
    "drafts_only": "Drafts Only",
    "supervised": "Supervised",
    "authorized": "Authorized",
}


@pytest.mark.parametrize("mode", ["drafts_only", "supervised", "authorized"])
@pytest.mark.parametrize("paused", [False, True])
def test_harness_page_mode_matches_api_for_every_mode_and_pause(
    app_client, two_users, mode, paused
):
    """Page label == API mode for every mode x paused combination.

    Also asserts the page never claims live automation while the tenant
    is in drafts_only or paused, and never claims SMS is sending while
    paused.
    """
    user_id = two_users[0]
    _set_settings(user_id, mode, paused)
    _login(app_client, user_id)

    api_resp = app_client.get("/api/harness/status")
    assert api_resp.status_code == 200
    api = api_resp.get_json()
    assert api["mode"] == mode
    assert api["paused"] is paused
    assert api["mode_label"] == MODE_LABELS[mode]

    page_resp = app_client.get("/harness")
    assert page_resp.status_code == 200
    html = page_resp.get_data(as_text=True)

    # The rendered label comes from the same authoritative mode value.
    assert f">{MODE_LABELS[mode]}<" in html
    assert f"({mode})" in html

    # SMS and voice state are shown, derived from the same status dict.
    assert "SMS auto-reply configured" in html
    assert "SMS sending paused" in html
    assert "Voice calling configured" in html
    assert "Outbound activity" in html

    if paused:
        assert "SMS sending paused: <strong>yes</strong>" in html
        # A paused tenant must never be told automation will trigger sends.
        assert "can trigger calls and texts" not in html
        assert "will send automatically" not in html
    else:
        assert "SMS sending paused: <strong>no</strong>" in html

    if mode == "drafts_only":
        # The exact misleading claim from the production report.
        assert "Live automation is active" not in html
        assert "voice tools can only draft" in html
        if not paused:
            assert "nothing sends without your approval" in html
    elif mode == "supervised":
        if not paused:
            assert "only approved-template sends execute automatically" in html
    else:  # authorized
        if not paused:
            assert "routine sends and writes execute automatically" in html


def test_harness_page_invalid_stored_mode_renders_drafts_only(
    app_client, two_users
):
    """A stored mode outside the known set (e.g. the literal "live")
    must never reach the page or the API: both fall back to Drafts Only
    and the page must not contain a "Live" label."""
    user_id = two_users[0]
    _set_settings(user_id, "live", False)
    _login(app_client, user_id)

    api = app_client.get("/api/harness/status").get_json()
    assert api["mode"] == "drafts_only"
    assert api["mode_label"] == "Drafts Only"

    html = app_client.get("/harness").get_data(as_text=True)
    assert ">Drafts Only<" in html
    assert "Live automation is active" not in html


def test_harness_outbound_activity_counts_come_from_stored_records(
    app_client, two_users
):
    """Activity counts are recorded activity, not assumptions: seeded
    audit rows / sent messages / placed calls show up in both the API
    and the page, and zero activity shows as zero."""
    user_id = two_users[0]
    _set_settings(user_id, "drafts_only", False)
    _login(app_client, user_id)

    api = app_client.get("/api/harness/status").get_json()
    activity = api["outbound_activity"]
    assert activity["sms_sent_60d"] == 0
    assert activity["sms_blocked_60d"] == 0
    assert activity["sms_send_failed_60d"] == 0
    assert activity["sms_drafted_60d"] == 0
    assert activity["calls_placed_60d"] == 0

    _seed_audit(user_id, "sms_auto_reply_sent", "ALLOW")
    _seed_audit(user_id, "sms_auto_reply_blocked", "BLOCK")
    _seed_audit(user_id, "sms_auto_reply_send_failed", "ALLOW")
    _seed_audit(user_id, "sms_auto_reply_drafted", "DRAFT")
    _seed_sms_sent(user_id)
    _seed_call_placed(user_id)

    api = app_client.get("/api/harness/status").get_json()
    activity = api["outbound_activity"]
    assert activity["sms_sent_60d"] == 1
    assert activity["sms_blocked_60d"] == 1
    assert activity["sms_send_failed_60d"] == 1
    assert activity["sms_drafted_60d"] == 1
    assert activity["sms_messages_sent_60d"] == 1
    assert activity["calls_placed_60d"] == 1

    html = app_client.get("/harness").get_data(as_text=True)
    assert "AI SMS sent (last 60 days): <strong>1</strong>" in html
    assert "Sends blocked by the policy gate (last 60 days): <strong>1</strong>" in html
    assert "AI calls placed (last 60 days): <strong>1</strong>" in html
