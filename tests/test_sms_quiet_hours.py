"""Quiet hours must be evaluated in the recipient timezone when known.

This restriction is application-enforced (not Telnyx). Default window is
9:00 PM–8:00 AM local. Unknown recipient NPAs fall back to the account timezone.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import config
import db
import sms_authorization as sa
import sms_quiet_hours as qh
import tenant_sms_db as tdb
from workers.sms_campaign_worker import process_due_scheduled_messages, process_one


QUIET_NOW = datetime(2026, 8, 11, 5, 30, tzinfo=timezone.utc)  # 23:30 America/Denver
OPEN_NOW = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)  # 09:00 America/Denver


def _set_tz(user_id, tz_name):
    db.update_business_profile(user_id, timezone=tz_name)


def _freeze(monkeypatch, dt):
    def _aware(now=None):
        value = dt if now is None else now
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    monkeypatch.setattr(qh, "_aware_utc", _aware)


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["session_version"] = 1


def _persona(user_id):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM voice_personas WHERE user_id IS NULL OR user_id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
    return row["id"]


def _telnyx_ready(monkeypatch):
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY_TEST_ONLY")
    monkeypatch.setattr(config, "TELNYX_MESSAGING_PROFILE_ID", "profile-1")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "+18888210810")
    monkeypatch.setattr(config, "TELNYX_PUBLIC_KEY", "pk-test")
    monkeypatch.setattr(config, "TELNYX_TRIAL_MODE", False)
    monkeypatch.setattr(config, "TELNYX_TOLL_FREE_VERIFICATION_STATUS", "verified")
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 21)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 8)
    monkeypatch.setattr(config, "APP_URL", "https://example.com")


def _send_payload(user_id, phone="3038703107", **extra):
    body = {
        "persona_id": _persona(user_id),
        "lead_name": "Pat",
        "phone_number": phone,
        "message_body": extra.pop("message_body", "Hello from TopAI"),
        "compliance_confirmed": True,
        "send_now": True,
    }
    body.update(extra)
    return body


def test_evening_utc_is_not_quiet_hours_locally(two_users, monkeypatch):
    """23:46 UTC is 17:46 in Denver — well outside the 21:00-08:00 quiet window."""
    u1, _ = two_users
    _set_tz(u1, "America/Denver")
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 21)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 8)
    now = datetime(2026, 8, 10, 23, 46, tzinfo=timezone.utc)
    assert sa._in_quiet_hours(u1, now=now) is False


def test_local_late_night_is_quiet_hours(two_users, monkeypatch):
    """05:30 UTC is 23:30 in Denver — inside the quiet window."""
    u1, _ = two_users
    _set_tz(u1, "America/Denver")
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 21)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 8)
    now = datetime(2026, 8, 11, 5, 30, tzinfo=timezone.utc)
    assert sa._in_quiet_hours(u1, now=now) is True


def test_local_morning_after_window_is_allowed(two_users, monkeypatch):
    """15:00 UTC is 09:00 in Denver — quiet window has ended."""
    u1, _ = two_users
    _set_tz(u1, "America/Denver")
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 21)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 8)
    now = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)
    assert sa._in_quiet_hours(u1, now=now) is False


def test_eastern_account_uses_its_own_clock(two_users, monkeypatch):
    """The same UTC instant can be quiet for one account timezone but not another."""
    u1, u2 = two_users
    _set_tz(u1, "America/New_York")
    _set_tz(u2, "America/Los_Angeles")
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 21)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 8)
    # 01:30 UTC = 21:30 New York (quiet) = 18:30 Los Angeles (allowed).
    now = datetime(2026, 8, 11, 1, 30, tzinfo=timezone.utc)
    assert sa._in_quiet_hours(u1, now=now) is True
    assert sa._in_quiet_hours(u2, now=now) is False


def test_unset_timezone_defaults_to_denver(two_users, monkeypatch):
    u1, _ = two_users
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 21)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 8)
    # 23:46 UTC = 17:46 America/Denver default — allowed.
    now = datetime(2026, 8, 10, 23, 46, tzinfo=timezone.utc)
    assert sa._in_quiet_hours(u1, now=now) is False


def test_equal_start_end_disables_quiet_hours(two_users, monkeypatch):
    u1, _ = two_users
    _set_tz(u1, "America/Denver")
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 0)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 0)
    now = datetime(2026, 8, 11, 5, 30, tzinfo=timezone.utc)
    assert sa._in_quiet_hours(u1, now=now) is False


def test_recipient_area_code_overrides_account_timezone(two_users, monkeypatch):
    """Account is Pacific, recipient is 212 (Eastern) — use Eastern quiet hours."""
    u1, _ = two_users
    _set_tz(u1, "America/Los_Angeles")
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 21)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 8)
    now = datetime(2026, 8, 11, 1, 30, tzinfo=timezone.utc)
    assert sa._in_quiet_hours(u1, now=now) is False
    assert sa._in_quiet_hours(u1, now=now, phone="+12125551212") is True


def test_quiet_hours_boundary_0800_is_allowed(two_users, monkeypatch):
    u1, _ = two_users
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 21)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 8)
    phone = "+12125550100"
    assert qh.in_quiet_hours(
        u1, now=datetime(2026, 8, 11, 11, 59, tzinfo=timezone.utc), phone=phone
    )
    assert not qh.in_quiet_hours(
        u1, now=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc), phone=phone
    )


def test_next_permitted_send_is_0800_local_after_overnight_window(two_users, monkeypatch):
    u1, _ = two_users
    _set_tz(u1, "America/Denver")
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 21)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 8)
    send_at = qh.next_permitted_send_at(u1, now=QUIET_NOW, phone="+13038703107")
    from crm_time import resolve_zone

    denver = send_at.astimezone(resolve_zone("America/Denver"))
    assert denver.hour == 8
    assert denver.minute == 0
    assert denver.date().isoformat() == "2026-08-11"


def test_compose_ui_offers_schedule_sms_action(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    html = app_client.get("/app").get_data(as_text=True)
    assert "Schedule SMS" in html
    assert "sms-quiet-hours" in html
    assert "SMS cannot be sent during quiet hours for this account." not in html


def test_manual_sms_during_permitted_hours_sends(app_client, two_users, monkeypatch):
    from sms_providers.telnyx import TelnyxSMSProvider

    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx_ready(monkeypatch)
    _freeze(monkeypatch, OPEN_NOW)
    tdb.accept_sms_terms(u1, u1)
    with patch.object(
        TelnyxSMSProvider,
        "send_message",
        return_value={"provider_message_id": "m-open", "status": "queued"},
    ) as send_mock:
        res = app_client.post("/sms/messages", json=_send_payload(u1))
    assert res.status_code == 201
    data = res.get_json()
    assert data.get("scheduled") is not True
    assert send_mock.called
    row = db.get_sms_message(data["id"], u1)
    assert row["status"] in {"queued", "sent", "submitted"}


def test_manual_sms_during_quiet_hours_offers_schedule(app_client, two_users, monkeypatch):
    from sms_providers.telnyx import TelnyxSMSProvider

    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx_ready(monkeypatch)
    _freeze(monkeypatch, QUIET_NOW)
    tdb.accept_sms_terms(u1, u1)
    with patch.object(TelnyxSMSProvider, "send_message") as send_mock:
        res = app_client.post("/sms/messages", json=_send_payload(u1))
    assert res.status_code == 409
    data = res.get_json()
    assert data["error_category"] == "quiet_hours"
    assert data["can_schedule"] is True
    assert "This recipient is currently within SMS quiet hours" in data["error"]
    assert "SMS cannot be sent during quiet hours for this account." not in data["error"]
    assert data["scheduled_for"]
    assert "8:00 AM" in data["scheduled_for_local"]
    assert "America/Denver" in data["scheduled_for_local"]
    assert not send_mock.called


def test_manual_sms_schedules_for_next_permitted_time(app_client, two_users, monkeypatch):
    from sms_providers.telnyx import TelnyxSMSProvider

    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx_ready(monkeypatch)
    _freeze(monkeypatch, QUIET_NOW)
    tdb.accept_sms_terms(u1, u1)
    with patch.object(TelnyxSMSProvider, "send_message") as send_mock:
        res = app_client.post(
            "/sms/messages",
            json=_send_payload(u1, schedule_if_quiet=True),
        )
    assert res.status_code == 201
    data = res.get_json()
    assert data["scheduled"] is True
    assert data["status"] == "scheduled"
    assert "8:00 AM" in data["scheduled_for_local"]
    assert not send_mock.called
    row = db.get_sms_message(data["id"], u1)
    assert row["status"] == "scheduled"
    assert row["message_body"] == "Hello from TopAI"
    assert not db.is_visible_conversation_sms(row)
    # Keep this fixture from becoming due on the shared test DB wall clock.
    db.schedule_sms_message(data["id"], "2099-01-01T00:00:00+00:00")


def test_scheduled_delivery_at_next_permitted_time_no_duplicate(app_client, two_users, monkeypatch):
    from sms_providers.telnyx import TelnyxSMSProvider

    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx_ready(monkeypatch)
    _freeze(monkeypatch, QUIET_NOW)
    tdb.accept_sms_terms(u1, u1)
    with patch.object(TelnyxSMSProvider, "send_message"):
        res = app_client.post(
            "/sms/messages",
            json=_send_payload(u1, schedule_if_quiet=True),
        )
    message_id = res.get_json()["id"]
    db.schedule_sms_message(message_id, "2020-01-01T00:00:00+00:00")
    _freeze(monkeypatch, OPEN_NOW)
    with patch.object(
        TelnyxSMSProvider,
        "send_message",
        return_value={"provider_message_id": "m-due", "status": "queued"},
    ) as send_mock:
        assert process_due_scheduled_messages() is True
        assert send_mock.call_count == 1
        assert process_due_scheduled_messages() is False
        assert send_mock.call_count == 1
    row = db.get_sms_message(message_id, u1)
    assert row["status"] == "queued"
    assert row["provider_message_id"] == "m-due"


def test_automated_sms_deferred_during_quiet_hours_no_duplicate(two_users, monkeypatch):
    import sms_ai_agent
    import sms_coach
    import telnyx_webhooks as txwh
    from sms_providers.telnyx import TelnyxSMSProvider
    from tests.test_telnyx_inbound_ai_workflow import (
        _analysis,
        _inbound_payload,
        _lead,
        _unique_e164,
    )

    u1, _ = two_users
    account = _unique_e164("888")
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", account)
    monkeypatch.setattr(config, "TELNYX_TRIAL_MODE", False)
    monkeypatch.setattr(config, "TELNYX_TOLL_FREE_VERIFICATION_STATUS", "verified")
    monkeypatch.setattr(config, "SMS_AI_AUTO_REPLY_ENABLED", True)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 21)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 8)
    tdb.upsert_tenant_sender(
        u1,
        sender_number=account,
        sms_provider="telnyx",
        sms_enabled=True,
        registration_status="verified",
    )
    _freeze(monkeypatch, QUIET_NOW)
    contact = _unique_e164("303")
    lead_id, _ = _lead(u1, contact)
    result, _ = txwh.handle_messaging_webhook(_inbound_payload(contact, account, text="Hi"))
    inbound_id = result["message_id"]

    with patch.object(sms_coach, "analyze_inbound_reply", return_value=_analysis()), \
         patch.object(TelnyxSMSProvider, "send_message") as send_mock:
        outcome = sms_ai_agent.process_inbound_ai(u1, lead_id, inbound_id, "Hi", account)
        assert outcome["scheduled"] is True
        assert not send_mock.called
        row = db.get_sms_message(outcome["message_id"], u1)
        assert row["status"] == "scheduled"
        db.schedule_sms_message(outcome["message_id"], "2020-01-01T00:00:00+00:00")
        _freeze(monkeypatch, OPEN_NOW)
        send_mock.return_value = {"provider_message_id": "ai-due", "status": "queued"}
        assert process_due_scheduled_messages() is True
        assert send_mock.call_count == 1
        assert process_due_scheduled_messages() is False
        assert send_mock.call_count == 1
        again = sms_ai_agent.process_inbound_ai(u1, lead_id, inbound_id, "Hi", account)
        assert again["reason"] == "already_replied"
        assert send_mock.call_count == 1


def test_campaign_quiet_hours_defers_to_next_window(two_users, monkeypatch):
    from tests.test_simpletexting_sms import _enable_st, _lead, _unique_e164

    u1, _ = two_users
    monkeypatch.setattr(config, "SMS_PROVIDER", "simpletexting")
    monkeypatch.setattr(config, "SIMPLETEXTING_API_TOKEN", "tok")
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 21)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 8)
    _freeze(monkeypatch, QUIET_NOW)
    _enable_st(u1)
    cid = tdb.create_campaign(u1, "Quiet blast", campaign_purpose="open_house")
    tdb.update_campaign(
        cid,
        u1,
        message_template="Hi [first_name], reply STOP to opt out.",
        content_fingerprint="fp-quiet",
    )
    phone = _unique_e164("303")
    lead_id, _ = _lead(u1, phone, "Quiet")
    tdb.replace_campaign_recipients(
        cid,
        u1,
        [
            {
                "lead_id": lead_id,
                "phone_number": phone,
                "merge_fields": {"first_name": "Quiet"},
                "eligible": True,
                "exclusion_reason": None,
            }
        ],
    )
    camp = tdb.get_campaign(cid, u1)
    att = tdb.create_campaign_attestation(
        u1,
        u1,
        cid,
        eligible_count=1,
        excluded_count=0,
        campaign_purpose="open_house",
        message_body=camp["message_template"],
        audience_snapshot_id=camp["audience_snapshot_id"],
        provider="simpletexting",
    )
    tdb.update_campaign(cid, u1, attestation_id=att, status="processing")
    assert tdb.create_jobs_for_campaign(cid, u1) == 1
    mock_provider = MagicMock()
    with patch("workers.sms_campaign_worker.get_sms_provider", return_value=mock_provider):
        assert process_one("worker-quiet") is True
        assert not mock_provider.send_sms.called
    with db.get_db() as conn:
        job = conn.execute(
            "SELECT * FROM sms_campaign_jobs WHERE campaign_id = ?",
            (cid,),
        ).fetchone()
    job = dict(job)
    assert job["status"] == "pending"
    assert job["next_attempt_at"]
    expected = qh.next_permitted_send_at(u1, now=QUIET_NOW, phone=phone)
    assert job["next_attempt_at"].startswith(expected.isoformat()[:16])
    _freeze(monkeypatch, OPEN_NOW)
    mock_provider.send_sms.return_value = {
        "provider_message_id": "camp-1",
        "status": "submitted",
    }
    with patch("workers.sms_campaign_worker.get_sms_provider", return_value=mock_provider):
        assert process_one("worker-quiet") is True
        assert mock_provider.send_sms.call_count == 1
        assert process_one("worker-quiet") is False
        assert mock_provider.send_sms.call_count == 1


def test_opt_out_still_blocks_during_quiet_hours(app_client, two_users, monkeypatch):
    from lead_service import upsert_crm_lead
    from sms_authorization import record_one_to_one_attestation
    from sms_providers.telnyx import TelnyxSMSProvider

    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx_ready(monkeypatch)
    _freeze(monkeypatch, QUIET_NOW)
    tdb.accept_sms_terms(u1, u1)
    lead_id, _, _ = upsert_crm_lead(u1, "+13038703107", {"lead_name": "Opt"}, touch_sms=True)
    record_one_to_one_attestation(u1, lead_id, message_body="Hello", source_page="test")
    db.mark_lead_opt_out(lead_id, u1)
    with patch.object(TelnyxSMSProvider, "send_message") as send_mock:
        res = app_client.post("/sms/messages", json=_send_payload(u1, phone="3038703107"))
    assert res.status_code == 403
    data = res.get_json()
    assert data.get("error_category") != "quiet_hours"
    assert "opt" in data["error"].lower()
    assert not send_mock.called


def test_missing_consent_still_blocks_during_permitted_hours(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx_ready(monkeypatch)
    _freeze(monkeypatch, OPEN_NOW)
    tdb.accept_sms_terms(u1, u1)
    res = app_client.post(
        "/sms/messages",
        json={
            "persona_id": _persona(u1),
            "lead_name": "Pat",
            "phone_number": "3038703107",
            "message_body": "Hello",
            "send_now": True,
        },
    )
    assert res.status_code == 400
    assert "consent" in res.get_json()["error"].lower()
