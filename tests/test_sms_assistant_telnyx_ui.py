"""Telnyx-focused AI SMS Assistant UI and diagnostics (no secrets)."""

from unittest.mock import MagicMock, patch

import config
import db
from sms_providers.factory import get_sms_provider
from sms_providers.telnyx import TelnyxSMSProvider


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["session_version"] = 1


def test_sms_provider_telnyx_status_endpoint(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY")
    monkeypatch.setattr(config, "TELNYX_MESSAGING_PROFILE_ID", "profile-1")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "+18888210810")
    monkeypatch.setattr(config, "TELNYX_PUBLIC_KEY", "pk-test")
    monkeypatch.setattr(config, "TELNYX_TRIAL_MODE", True)
    monkeypatch.setattr(config, "APP_URL", "https://topairealestatetools.com")
    monkeypatch.setattr(config, "TELNYX_TOLL_FREE_VERIFICATION_STATUS", "pending")
    res = app_client.get("/sms/status")
    assert res.status_code == 200
    data = res.get_json()
    assert data["provider"] == "telnyx"
    assert data["api_key_configured"] is True
    assert data["messaging_profile_configured"] is True
    assert data["phone_number_configured"] is True
    assert data["public_key_configured"] is True
    assert data["webhook_endpoint_configured"] is True
    assert data["webhook_api_version"] == "V2"
    assert data["toll_free_verification_status"] == "pending"
    assert data["trial_mode"] is True
    assert "TELNYX_API_KEY" not in str(data)
    assert "KEY" not in str(data.values())
    assert "pk-test" not in str(data.values())
    assert "a2p_readiness" not in data
    assert "messaging_service_configured" not in data


def test_sms_messages_returns_provider_status_not_twilio_for_telnyx(
    app_client, two_users, monkeypatch
):
    u1, _ = two_users
    _login(app_client, u1)
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "+18888210810")
    monkeypatch.setattr(config, "APP_URL", "https://topairealestatetools.com")
    res = app_client.get("/sms/messages")
    assert res.status_code == 200
    data = res.get_json()
    assert data["sms_provider"] == "telnyx"
    assert data["provider_status"]["provider"] == "telnyx"
    assert data["twilio_status"] is None


def test_app_page_has_telnyx_status_js_not_twilio_labels(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    html = app_client.get("/app").get_data(as_text=True)
    assert "sms-provider-status" in html
    assert "renderProviderStatus" in html
    assert "Telnyx status" in html
    assert "SMS sending is configured through Telnyx" in html
    assert "Twilio status" not in html
    assert "Messaging Service SID configured" not in html
    assert "A2P readiness" not in html
    assert "Twilio Console" not in html


def test_send_sms_routes_through_telnyx(app_client, two_users, monkeypatch):
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "+18888210810")
    provider = get_sms_provider()
    assert isinstance(provider, TelnyxSMSProvider)
    with patch.object(TelnyxSMSProvider, "send_message") as send_mock:
        send_mock.return_value = {"provider_message_id": "msg-tx-1", "status": "queued"}
        result = provider.send_sms("+15551230001", "Hello from TopAI")
    assert send_mock.called
    assert result["provider_message_id"] == "msg-tx-1"


def test_missing_telnyx_config_disables_send_flag(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "TELNYX_API_KEY", "")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "")
    res = app_client.get("/sms/messages")
    data = res.get_json()
    assert data["send_configured"] is False
    assert data["provider_status"]["api_key_configured"] is False


def test_health_reports_active_provider(app_client, monkeypatch):
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY")
    res = app_client.get("/health")
    data = res.get_json()
    assert data["sms_provider"] == "telnyx"
    assert data["telnyx_configured"] is True
    assert "twilio_configured" in data


def test_diagnostics_never_expose_secrets(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "TELNYX_API_KEY", "SUPER_SECRET_KEY_VALUE")
    monkeypatch.setattr(config, "TELNYX_PUBLIC_KEY", "SUPER_SECRET_PUBLIC")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "+18888210810")
    body = app_client.get("/sms/status").get_data(as_text=True)
    assert "SUPER_SECRET_KEY_VALUE" not in body
    assert "SUPER_SECRET_PUBLIC" not in body
