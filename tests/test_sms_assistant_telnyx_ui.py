"""Telnyx-focused AI SMS Assistant UI and diagnostics (no secrets)."""

from unittest.mock import MagicMock, patch

import pytest

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
    monkeypatch.setenv("TELNYX_TOLL_FREE_VERIFICATION_STATUS", "pending")
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
    assert data["sms_sending_enabled"] is False
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
    monkeypatch.setenv("TELNYX_TOLL_FREE_VERIFICATION_STATUS", "pending")
    monkeypatch.setattr(config, "TELNYX_TOLL_FREE_VERIFICATION_STATUS", "pending")
    res = app_client.get("/sms/messages")
    assert res.status_code == 200
    data = res.get_json()
    assert data["sms_provider"] == "telnyx"
    assert data["provider_status"]["provider"] == "telnyx"
    assert data["provider_status"]["sms_sending_enabled"] is False
    assert data["sms_sending_enabled"] is False
    assert data["toll_free_verification_blocked"] is True
    assert data["twilio_status"] is None
    assert "SMS sending is unavailable until Telnyx completes toll-free verification." in (
        data.get("verification_block_message") or ""
    )


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


def _persona(user_id):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM voice_personas WHERE user_id IS NULL OR user_id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
    return row["id"]


def _telnyx_ready(monkeypatch, *, verification="verified", trial=False):
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY")
    monkeypatch.setattr(config, "TELNYX_MESSAGING_PROFILE_ID", "profile-1")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "+18888210810")
    monkeypatch.setattr(config, "TELNYX_PUBLIC_KEY", "pk-test")
    monkeypatch.setattr(config, "TELNYX_TRIAL_MODE", trial)
    monkeypatch.setenv("TELNYX_TOLL_FREE_VERIFICATION_STATUS", verification)
    monkeypatch.setattr(config, "TELNYX_TOLL_FREE_VERIFICATION_STATUS", verification)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 0)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 0)
    monkeypatch.setattr(config, "APP_URL", "https://topairealestatetools.com")


def test_pending_status_keeps_sending_disabled(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _telnyx_ready(monkeypatch, verification="pending")
    data = app_client.get("/sms/messages").get_json()
    assert data["sms_sending_enabled"] is False
    assert data["provider_status"]["sms_sending_enabled"] is False
    assert data["provider_status"]["toll_free_verification_status"] == "pending"
    html = app_client.get("/app").get_data(as_text=True)
    assert "SMS sending is unavailable until Telnyx completes toll-free verification." in html
    assert "smsSendConfigured" in html
    assert "smsTollFreeVerified" in html


def test_pending_status_blocks_direct_backend_send(app_client, two_users, monkeypatch):
    import tenant_sms_db as tdb

    u1, _ = two_users
    _login(app_client, u1)
    _telnyx_ready(monkeypatch, verification="pending")
    tdb.accept_sms_terms(u1, u1)
    with patch("sms_providers.telnyx.TelnyxSMSProvider.send_message") as send_mock:
        res = app_client.post(
            "/sms/messages",
            json={
                "persona_id": _persona(u1),
                "lead_name": "Pat",
                "phone_number": "7202891700",
                "message_body": "Hello from TopAI",
                "compliance_confirmed": True,
                "send_now": True,
            },
        )
    assert res.status_code == 403
    assert res.get_json()["error"] == "Toll-free messaging verification is not complete."
    assert not send_mock.called


@pytest.mark.parametrize(
    "status",
    ["", "   ", "rejected", "failed", "unknown", "waiting", "waiting for telnyx", "Waiting For Vendor"],
)
def test_non_verified_statuses_block_sending(app_client, two_users, monkeypatch, status):
    import tenant_sms_db as tdb

    u1, _ = two_users
    _login(app_client, u1)
    _telnyx_ready(monkeypatch, verification=status)
    tdb.accept_sms_terms(u1, u1)
    res = app_client.post(
        "/sms/messages",
        json={
            "persona_id": _persona(u1),
            "lead_name": "Pat",
            "phone_number": "+17202891700",
            "message_body": "Hello from TopAI",
            "compliance_confirmed": True,
            "send_now": True,
        },
    )
    assert res.status_code == 403
    assert "Toll-free messaging verification is not complete." in res.get_json()["error"]


def test_verified_status_enables_sending_flag(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _telnyx_ready(monkeypatch, verification="verified")
    data = app_client.get("/sms/messages").get_json()
    assert data["sms_sending_enabled"] is True
    assert data["toll_free_verification_blocked"] is False
    assert data["provider_status"]["sms_sending_enabled"] is True


def test_unchecked_consent_blocks_even_when_verified(app_client, two_users, monkeypatch):
    import tenant_sms_db as tdb

    u1, _ = two_users
    _login(app_client, u1)
    _telnyx_ready(monkeypatch, verification="verified")
    tdb.accept_sms_terms(u1, u1)
    res = app_client.post(
        "/sms/messages",
        json={
            "persona_id": _persona(u1),
            "lead_name": "Pat",
            "phone_number": "+17202891700",
            "message_body": "Hello from TopAI",
            "compliance_confirmed": False,
            "send_now": True,
        },
    )
    assert res.status_code == 400
    assert "consent" in res.get_json()["error"].lower()


def test_opted_out_recipient_blocks_sending(app_client, two_users, monkeypatch):
    import tenant_sms_db as tdb
    from lead_service import upsert_crm_lead
    from sms_authorization import record_one_to_one_attestation

    u1, _ = two_users
    _login(app_client, u1)
    _telnyx_ready(monkeypatch, verification="verified")
    tdb.accept_sms_terms(u1, u1)
    lead_id, _, _ = upsert_crm_lead(
        u1,
        "+17202891700",
        {"lead_name": "Opt Out", "phone_number": "+17202891700"},
        source="sms",
        touch_sms=True,
        assigned_user_id=u1,
    )
    record_one_to_one_attestation(
        u1, lead_id, message_body="Hello from TopAI", source_page="test"
    )
    db.mark_lead_opt_out(lead_id, u1)
    with patch("sms_providers.telnyx.TelnyxSMSProvider.send_message") as send_mock:
        res = app_client.post(
            "/sms/messages",
            json={
                "persona_id": _persona(u1),
                "lead_name": "Opt Out",
                "phone_number": "+17202891700",
                "message_body": "Hello from TopAI",
                "compliance_confirmed": True,
                "send_now": True,
            },
        )
    assert res.status_code == 403
    assert "opt" in res.get_json()["error"].lower()
    assert not send_mock.called


def test_ten_digit_us_number_normalized_to_e164():
    from lead_service import normalize_phone_e164
    from sms_validation import validate_sms_send_payload

    assert normalize_phone_e164("7202891700") == "+17202891700"
    cleaned, err = validate_sms_send_payload(
        {
            "persona_id": 1,
            "lead_name": "Pat",
            "phone_number": "7202891700",
            "message_body": "Hello from TopAI",
            "compliance_confirmed": True,
            "send_now": True,
        }
    )
    assert err is None
    assert cleaned["phone_number"] == "+17202891700"


def test_malformed_phone_rejected():
    from sms_validation import validate_sms_send_payload

    cleaned, err = validate_sms_send_payload(
        {
            "persona_id": 1,
            "lead_name": "Pat",
            "phone_number": "123",
            "message_body": "Hello",
            "compliance_confirmed": True,
            "send_now": True,
        }
    )
    assert cleaned is None
    assert err and "valid" in err.lower()


def test_verification_errors_never_expose_secrets(app_client, two_users, monkeypatch):
    import tenant_sms_db as tdb

    u1, _ = two_users
    _login(app_client, u1)
    _telnyx_ready(monkeypatch, verification="pending")
    monkeypatch.setattr(config, "TELNYX_API_KEY", "SUPER_SECRET_KEY_VALUE")
    tdb.accept_sms_terms(u1, u1)
    res = app_client.post(
        "/sms/messages",
        json={
            "persona_id": _persona(u1),
            "lead_name": "Pat",
            "phone_number": "+17202891700",
            "message_body": "Hello from TopAI",
            "compliance_confirmed": True,
            "send_now": True,
        },
    )
    body = res.get_data(as_text=True)
    assert res.status_code == 403
    assert "SUPER_SECRET_KEY_VALUE" not in body
    assert "Toll-free messaging verification is not complete." in body