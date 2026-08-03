"""Toll-free verification status must track process env (not a stale import snapshot)."""

import config
from sms_authorization import (
    get_telnyx_toll_free_verification_status,
    is_telnyx_toll_free_verified,
)


def test_getter_reads_os_environ_after_import(monkeypatch):
    monkeypatch.setenv("TELNYX_TOLL_FREE_VERIFICATION_STATUS", "pending")
    config.TELNYX_TOLL_FREE_VERIFICATION_STATUS = "pending"
    assert get_telnyx_toll_free_verification_status() == "pending"
    assert is_telnyx_toll_free_verified() is False

    monkeypatch.setenv("TELNYX_TOLL_FREE_VERIFICATION_STATUS", "verified")
    # Import-time config snapshot left stale on purpose.
    config.TELNYX_TOLL_FREE_VERIFICATION_STATUS = "pending"
    assert get_telnyx_toll_free_verification_status() == "verified"
    assert is_telnyx_toll_free_verified() is True
    # Getter keeps config aligned after reading env.
    assert config.TELNYX_TOLL_FREE_VERIFICATION_STATUS == "verified"


def test_getter_falls_back_to_config_when_env_unset(monkeypatch):
    monkeypatch.delenv("TELNYX_TOLL_FREE_VERIFICATION_STATUS", raising=False)
    monkeypatch.setattr(config, "TELNYX_TOLL_FREE_VERIFICATION_STATUS", "verified")
    assert get_telnyx_toll_free_verification_status() == "verified"


def test_blank_and_unknown_are_not_verified(monkeypatch):
    for value in ("", "   ", "unknown", "pending", "Waiting For Vendor"):
        monkeypatch.setenv("TELNYX_TOLL_FREE_VERIFICATION_STATUS", value)
        assert is_telnyx_toll_free_verified() is False


def test_health_reports_verified_only_for_telnyx(app_client, monkeypatch):
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "+18888210810")
    monkeypatch.setenv("TELNYX_TOLL_FREE_VERIFICATION_STATUS", "verified")
    data = app_client.get("/health").get_json()
    assert data["toll_free_verification_status"] == "verified"
    assert data["sms_sending_enabled"] is True

    monkeypatch.setattr(config, "SMS_PROVIDER", "twilio")
    monkeypatch.setattr(config, "TWILIO_ACCOUNT_SID", "AC")
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setattr(config, "TWILIO_PHONE_NUMBER", "+15551234567")
    # Env still says verified, but active provider is not Telnyx.
    data = app_client.get("/health").get_json()
    assert data["toll_free_verification_status"] is None
