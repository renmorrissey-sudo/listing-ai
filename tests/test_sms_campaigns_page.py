"""Bulk SMS campaigns page: HTML load, auth, Telnyx verification gating."""

from unittest.mock import patch

import config
import db
import tenant_sms_db as tdb
from sms_authorization import NO_SENDER_MSG, require_tenant_sender


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["session_version"] = 1


def _telnyx(monkeypatch, *, verification="pending", worker=False, trial=False):
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "+18888210810")
    monkeypatch.setattr(config, "TELNYX_MESSAGING_PROFILE_ID", "profile-1")
    monkeypatch.setattr(config, "TELNYX_TOLL_FREE_VERIFICATION_STATUS", verification)
    monkeypatch.setattr(config, "TELNYX_TRIAL_MODE", trial)
    monkeypatch.setattr(
        config,
        "SMS_CAMPAIGN_WORKER_AVAILABLE",
        "true" if worker else "false",
    )


def test_unauthenticated_redirects_to_login(app_client):
    res = app_client.get("/crm/sms-campaigns", follow_redirects=False)
    assert res.status_code in {302, 303}
    loc = res.headers.get("Location") or ""
    assert "/login" in loc
    assert "next=" in loc
    assert "sms-campaigns" in loc


def test_telnyx_configured_pending_does_not_say_not_activated(
    app_client, two_users, monkeypatch
):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx(monkeypatch, verification="pending", worker=False, trial=False)
    res = app_client.get("/crm/sms-campaigns")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "SMS is not activated" not in html
    assert "Telnyx SMS is configured. Bulk sending will become available after toll-free verification is approved." in html
    assert "Assigned sender: +18888210810" in html
    assert "Bulk sending enabled: no" in html
    sender = tdb.get_active_sender(u1)
    assert sender is not None
    assert sender["sender_number"] == "+18888210810"


def test_missing_assigned_sender_shows_correct_warning(two_users, monkeypatch):
    u1, _ = two_users
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "TELNYX_API_KEY", "")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "")
    monkeypatch.setattr(config, "TELNYX_MESSAGING_PROFILE_ID", "")
    sender, err = require_tenant_sender(u1)
    assert sender is None
    assert err == NO_SENDER_MSG


def test_healthy_worker_clears_worker_warning(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx(monkeypatch, verification="pending", worker=False)
    monkeypatch.setattr(config, "SMS_CAMPAIGN_WORKER_AVAILABLE", "")
    tdb.touch_worker_heartbeat("test-worker-1", status="running")
    res = app_client.get("/crm/sms-campaigns")
    html = res.get_data(as_text=True)
    assert "Campaign processing worker is currently unavailable." not in html
    assert "Campaign worker: running" in html


def test_stopped_worker_blocks_launch_not_page(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx(monkeypatch, verification="verified", worker=False)
    tdb.accept_sms_terms(u1, u1)
    res = app_client.get("/crm/sms-campaigns")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Campaign processing worker is currently unavailable." in html
    assert "Bulk sending enabled: no" in html
    cid = tdb.create_campaign(u1, "Launch block")
    res_post = app_client.post(
        f"/crm/sms-campaigns/{cid}",
        data={"action": "launch"},
        follow_redirects=True,
    )
    body = res_post.get_data(as_text=True)
    assert "worker" in body.lower()


def test_accepting_terms_clears_only_terms_warning(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx(monkeypatch, verification="pending", worker=True)
    before = app_client.get("/crm/sms-campaigns").get_data(as_text=True)
    assert "Accept" in before and "SMS terms" in before
    res = app_client.post(
        "/crm/sms-diagnostics",
        data={"action": "accept_terms"},
        follow_redirects=True,
    )
    assert res.status_code == 200
    assert tdb.has_accepted_sms_terms(u1)
    after = app_client.get("/crm/sms-campaigns").get_data(as_text=True)
    assert "Accept SMS terms before sending." not in after
    assert "Telnyx SMS is configured. Bulk sending will become available after toll-free verification is approved." in after
    assert "Bulk sending enabled: no" in after


def test_pending_verification_always_blocks_launch(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx(monkeypatch, verification="pending", worker=True)
    tdb.accept_sms_terms(u1, u1)
    cid = tdb.create_campaign(u1, "Pending block")
    res_post = app_client.post(
        f"/crm/sms-campaigns/{cid}",
        data={"action": "launch"},
        follow_redirects=True,
    )
    body = res_post.get_data(as_text=True)
    assert "Toll-free messaging verification is not complete." in body or (
        "toll-free verification" in body.lower()
    )


def test_verified_alone_does_not_bypass_worker_or_terms(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx(monkeypatch, verification="verified", worker=False)
    html = app_client.get("/crm/sms-campaigns").get_data(as_text=True)
    assert "Bulk sending enabled: no" in html
    assert "SMS terms: not accepted" in html
    assert "Campaign processing worker is currently unavailable." in html


def test_no_twilio_fields_required_for_telnyx(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx(monkeypatch, verification="pending")
    monkeypatch.setattr(config, "TWILIO_ACCOUNT_SID", "")
    monkeypatch.setattr(config, "TWILIO_MESSAGING_SERVICE_SID", "")
    html = app_client.get("/crm/sms-campaigns").get_data(as_text=True)
    assert "messaging_service_sid" not in html.lower()
    assert "TWILIO" not in html
    assert "Provider: Telnyx" in html


def test_secrets_never_appear_on_campaigns_page(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx(monkeypatch, verification="pending")
    monkeypatch.setattr(config, "TELNYX_API_KEY", "SUPER_SECRET_KEY_VALUE")
    monkeypatch.setattr(config, "TELNYX_PUBLIC_KEY", "SUPER_SECRET_PUBLIC")
    body = app_client.get("/crm/sms-campaigns").get_data(as_text=True)
    assert "SUPER_SECRET_KEY_VALUE" not in body
    assert "SUPER_SECRET_PUBLIC" not in body


def test_new_campaign_unauthenticated_redirects_to_login(app_client):
    res = app_client.get("/crm/sms-campaigns/new", follow_redirects=False)
    assert res.status_code in {302, 303}
    loc = res.headers.get("Location") or ""
    assert "/login" in loc
    assert "next=" in loc
    assert "sms-campaigns/new" in loc


def test_new_campaign_get_returns_200_html_when_pending(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx(monkeypatch, verification="pending", worker=False)
    res = app_client.get("/crm/sms-campaigns/new")
    assert res.status_code == 200
    assert "text/html" in (res.headers.get("Content-Type") or "")
    html = res.get_data(as_text=True)
    assert "Create campaign" in html
    assert "Provider: Telnyx" in html
    assert "Toll-free verification: pending" in html
    assert "Telnyx SMS is configured. Bulk sending will become available after toll-free verification is approved." in html
    assert "Launch controls: disabled" in html
    assert "Bulk sending enabled: no" in html
    assert "Save draft" in html
    assert "Traceback" not in html
    assert "SUPER_SECRET" not in html


def test_new_campaign_missing_worker_and_sender_do_not_crash(
    app_client, two_users, monkeypatch
):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx(monkeypatch, verification="pending", worker=False)
    monkeypatch.setattr(config, "TELNYX_API_KEY", "")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "")
    monkeypatch.setattr(config, "TELNYX_MESSAGING_PROFILE_ID", "")
    monkeypatch.setattr(config, "SMS_CAMPAIGN_WORKER_AVAILABLE", "")
    with patch("tenant_sms_db.get_campaign_worker_health", return_value={
        "state": "unknown",
        "last_seen_at": None,
        "worker_id": None,
        "message": "Campaign processing worker is currently unavailable.",
    }):
        res = app_client.get("/crm/sms-campaigns/new")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Campaign processing worker is currently unavailable." in html
    assert "Create campaign" in html


def test_new_campaign_terms_blocker_visible(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx(monkeypatch, verification="pending", worker=True)
    assert not tdb.has_accepted_sms_terms(u1)
    html = app_client.get("/crm/sms-campaigns/new").get_data(as_text=True)
    assert "Accept" in html and "SMS terms" in html
    assert "SMS terms: not accepted" in html


def test_new_campaign_post_creates_draft_without_sending(
    app_client, two_users, monkeypatch
):
    u1, u2 = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx(monkeypatch, verification="pending", worker=False)
    before = tdb.list_campaigns(u1)
    res = app_client.post(
        "/crm/sms-campaigns/new",
        data={"title": "Draft only", "campaign_purpose": "real_estate_follow_up"},
        follow_redirects=False,
    )
    assert res.status_code in {302, 303}
    loc = res.headers.get("Location") or ""
    assert "/crm/sms-campaigns/" in loc
    campaigns = tdb.list_campaigns(u1)
    assert len(campaigns) == len(before) + 1
    created = campaigns[0]
    assert created["title"] == "Draft only"
    assert created["status"] == "draft"
    # Tenant isolation: other user cannot see it
    assert tdb.get_campaign(created["id"], u2) is None
    assert tdb.list_campaigns(u2) == []


def test_new_campaign_zero_prior_campaigns_empty_state_ok(
    app_client, two_users, monkeypatch
):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx(monkeypatch, verification="pending")
    assert tdb.list_campaigns(u1) == []
    res = app_client.get("/crm/sms-campaigns/new")
    assert res.status_code == 200
    assert "Create campaign" in res.get_data(as_text=True)


def test_create_campaign_uses_bind_bool_for_postgres(monkeypatch, two_users):
    """Regression for prod e522df439886: Postgres BOOLEAN rejects smallint 0/1."""
    from db_backend import bind_bool

    u1, _ = two_users
    monkeypatch.setattr(config, "DB_ENGINE", "postgres")
    assert bind_bool(False) is False
    assert bind_bool(True) is True
    # Exercise the same conversion create_campaign uses for test_mode.
    assert bind_bool(None) is False
    with patch("tenant_sms_db.get_db") as mock_get_db:
        conn = mock_get_db.return_value.__enter__.return_value
        cur = conn.execute.return_value
        cur.lastrowid = 99
        cid = tdb.create_campaign(u1, "PG bool")
        assert cid == 99
        args = conn.execute.call_args[0]
        params = args[1]
        # test_mode is the 10th bound value in the INSERT tuple
        assert params[9] is False


def test_new_campaign_create_failure_returns_html_not_json(
    app_client, two_users, monkeypatch
):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx(monkeypatch, verification="pending")
    with patch("tenant_sms_db.create_campaign", side_effect=RuntimeError("db down")):
        res = app_client.post(
            "/crm/sms-campaigns/new",
            data={"title": "Boom", "campaign_purpose": "real_estate_follow_up"},
        )
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    assert "text/html" in (res.headers.get("Content-Type") or "")
    assert "Could not create the campaign draft" in body
    assert "Traceback" not in body
    assert "db down" not in body
    assert tdb.list_campaigns(u1) == []


def test_new_campaign_pending_does_not_launch_or_send(
    app_client, two_users, monkeypatch
):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx(monkeypatch, verification="pending", worker=True)
    tdb.accept_sms_terms(u1, u1)
    res = app_client.post(
        "/crm/sms-campaigns/new",
        data={"title": "No send", "campaign_purpose": "real_estate_follow_up"},
        follow_redirects=True,
    )
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "disabled" in html.lower()
    campaigns = tdb.list_campaigns(u1)
    assert campaigns and campaigns[0]["status"] == "draft"
    # Attempt launch on detail — must remain blocked
    cid = campaigns[0]["id"]
    launch = app_client.post(
        f"/crm/sms-campaigns/{cid}",
        data={"action": "launch"},
        follow_redirects=True,
    )
    body = launch.get_data(as_text=True)
    assert "toll-free" in body.lower()
    assert tdb.get_campaign(cid, u1)["status"] == "draft"
