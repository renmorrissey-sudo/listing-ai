"""Bulk SMS campaigns page: HTML load, auth, Telnyx verification gating."""

from unittest.mock import patch

import config
import db
import tenant_sms_db as tdb


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["session_version"] = 1


def _telnyx(monkeypatch, *, verification="pending", worker=False):
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "+18888210810")
    monkeypatch.setattr(config, "TELNYX_MESSAGING_PROFILE_ID", "profile-1")
    monkeypatch.setattr(config, "TELNYX_TOLL_FREE_VERIFICATION_STATUS", verification)
    monkeypatch.setattr(config, "TELNYX_TRIAL_MODE", True)
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


def test_authenticated_campaigns_page_returns_html(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx(monkeypatch, verification="pending", worker=False)
    res = app_client.get("/crm/sms-campaigns")
    assert res.status_code == 200
    assert "text/html" in (res.content_type or "")
    html = res.get_data(as_text=True)
    assert "Bulk SMS" in html
    assert "No campaigns yet." in html
    assert "Bulk SMS sending is unavailable until Telnyx completes toll-free verification." in html
    assert "Campaign processing worker is currently unavailable." in html
    assert "Bulk sending enabled: no" in html
    assert "Toll-free verification: pending" in html
    assert "Provider: Telnyx" in html
    assert "messaging_service_sid" not in html.lower()
    assert "TWILIO_ACCOUNT_SID" not in html
    assert "KEY" not in html


def test_pending_verification_disables_launch_controls(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx(monkeypatch, verification="pending", worker=True)
    cid = tdb.create_campaign(u1, "Draft campaign")
    res = app_client.get(f"/crm/sms-campaigns/{cid}")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "disabled" in html
    assert "Bulk SMS sending is unavailable until Telnyx completes toll-free verification." in html
    # Launch POST must still be blocked server-side
    res_post = app_client.post(
        f"/crm/sms-campaigns/{cid}",
        data={"action": "launch"},
        follow_redirects=True,
    )
    assert res_post.status_code == 200
    body = res_post.get_data(as_text=True)
    assert "Toll-free messaging verification is not complete." in body or (
        "unavailable until Telnyx completes toll-free verification." in body
    )


def test_verified_status_can_enable_controls_when_worker_present(
    app_client, two_users, monkeypatch
):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx(monkeypatch, verification="verified", worker=True)
    res = app_client.get("/crm/sms-campaigns")
    html = res.get_data(as_text=True)
    assert res.status_code == 200
    assert "Bulk sending enabled: yes" in html
    assert "Bulk SMS sending is unavailable until Telnyx completes toll-free verification." not in html


def test_missing_worker_does_not_crash_page(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx(monkeypatch, verification="verified", worker=False)
    res = app_client.get("/crm/sms-campaigns")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Campaign processing worker is currently unavailable." in html
    assert "Bulk sending enabled: no" in html


def test_get_active_sender_uses_engine_safe_bool(two_users, monkeypatch):
    u1, _ = two_users
    tdb.upsert_tenant_sender(
        u1,
        sender_number="+18888210810",
        sms_provider="telnyx",
        sms_enabled=True,
        registration_status="verified",
    )
    sender = tdb.get_active_sender(u1)
    assert sender is not None
    assert sender["sender_number"] == "+18888210810"


def test_crm_html_error_not_json_on_unexpected(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx(monkeypatch, verification="pending")
    with patch("sms_campaigns.tdb.list_campaigns", side_effect=RuntimeError("db down")):
        # list_campaigns is caught inside route — force require sender crash path instead
        pass
    with patch(
        "sms_campaigns._safe_require_sender",
        side_effect=RuntimeError("boom"),
    ):
        # _safe_require_sender itself catches; patch _bulk_status_context after list
        with patch(
            "sms_campaigns._bulk_status_context",
            side_effect=RuntimeError("status boom"),
        ):
            res = app_client.get("/crm/sms-campaigns")
    assert res.status_code == 500
    assert "text/html" in (res.content_type or "")
    body = res.get_data(as_text=True)
    assert "Something went wrong" in body
    assert "SUPER_SECRET" not in body
    assert "status boom" not in body


def test_diagnostics_page_loads_with_telnyx_pending(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx(monkeypatch, verification="pending", worker=False)
    res = app_client.get("/crm/sms-diagnostics")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "SMS diagnostics" in html
    assert "Toll-free verification: pending" in html
    assert "Bulk sending enabled: no" in html
