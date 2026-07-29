"""Twilio SMS provider error mapping and send behavior."""

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import config
import db
import sms_provider
from sms_provider import (
    SmsProviderError,
    TwilioSmsProvider,
    format_user_error,
    map_twilio_error,
    parse_provider_code_from_error_message,
)


def _http_error(code, payload):
    body = json.dumps(payload).encode("utf-8")
    return urllib.error.HTTPError(
        url="https://api.twilio.com/Messages.json",
        code=code,
        msg="Error",
        hdrs=None,
        fp=io.BytesIO(body),
    )


def _provider(**overrides):
    with patch.object(config, "TWILIO_ACCOUNT_SID", overrides.get("sid", "ACaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")), \
         patch.object(config, "TWILIO_AUTH_TOKEN", overrides.get("token", "auth-token-value")), \
         patch.object(config, "TWILIO_PHONE_NUMBER", overrides.get("from_number", "+15551234567")), \
         patch.object(config, "TWILIO_MESSAGING_SERVICE_SID", overrides.get("msid", "")):
        return TwilioSmsProvider()


def test_map_30034_a2p():
    detail, a2p = map_twilio_error(30034, "Message from an unregistered number")
    assert "A2P 10DLC" in detail
    assert a2p == "blocked_unregistered"
    msg = format_user_error(30034, detail)
    assert "Twilio error 30034" in msg
    assert "A2P" in msg


def test_map_21608_unverified():
    detail, _ = map_twilio_error(21608, "not verified")
    assert "not verified" in detail.lower()


def test_map_auth_failure():
    detail, _ = map_twilio_error(20003, "Authenticate", http_status=401)
    assert "credentials" in detail.lower()


def test_map_invalid_number():
    detail, _ = map_twilio_error(21211, "Invalid")
    assert "valid mobile phone number" in detail.lower()


def test_map_insufficient_balance():
    detail, _ = map_twilio_error(30002, "insufficient funds")
    assert "sufficient balance" in detail.lower()


def test_map_compliance_pending():
    detail, a2p = map_twilio_error(30033, "Campaign pending approval")
    assert "compliance approval is still pending" in detail.lower()
    assert a2p == "pending_approval"


def test_successful_send_uses_from_when_no_messaging_service():
    import urllib.parse

    provider = _provider(msid="")
    fake_resp = MagicMock()
    fake_resp.read.return_value = json.dumps(
        {"sid": "SMxxxxxxxx", "status": "queued", "to": "+15557654321", "from": "+15551234567"}
    ).encode()
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.__exit__.return_value = False

    captured = {}

    def fake_urlopen(req, timeout=20):
        captured["body"] = urllib.parse.unquote(req.data.decode("utf-8"))
        return fake_resp

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = provider.send_sms("+15557654321", "Hello from TopAI")
    assert result["provider_message_id"] == "SMxxxxxxxx"
    assert "From=+15551234567" in captured["body"]
    assert "MessagingServiceSid" not in captured["body"]


def test_successful_send_prefers_messaging_service_sid():
    import urllib.parse

    provider = _provider(msid="MG11111111111111111111111111111111")
    fake_resp = MagicMock()
    fake_resp.read.return_value = json.dumps(
        {"sid": "SMyyyyyyyy", "status": "queued"}
    ).encode()
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.__exit__.return_value = False

    captured = {}

    def fake_urlopen(req, timeout=20):
        captured["body"] = urllib.parse.unquote(req.data.decode("utf-8"))
        return fake_resp

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = provider.send_sms("+15557654321", "Hello")
    assert result["provider_message_id"] == "SMyyyyyyyy"
    assert "MessagingServiceSid=MG11111111111111111111111111111111" in captured["body"]
    assert "From=" not in captured["body"]


def test_send_raises_mapped_30034():
    provider = _provider()
    err = _http_error(
        400,
        {
            "code": 30034,
            "message": "Message from an unregistered number",
            "more_info": "https://www.twilio.com/docs/errors/30034",
            "status": 400,
        },
    )
    with patch("urllib.request.urlopen", side_effect=err):
        try:
            provider.send_sms("+15557654321", "Hi")
            assert False, "expected SmsProviderError"
        except SmsProviderError as exc:
            assert exc.provider_code == 30034
            assert "30034" in str(exc)
            assert "A2P" in str(exc)
            assert "AC" not in str(exc) or "ACaaaa" not in str(exc)
            public = exc.to_public_dict()
            assert "error" in public
            assert public["provider_code"] == 30034
            assert "more_info" not in public
            assert "auth" not in str(exc).lower() or "token" not in str(exc).lower()


def test_send_raises_21608():
    provider = _provider()
    err = _http_error(400, {"code": 21608, "message": "Permission to send an SMS has not been enabled"})
    with patch("urllib.request.urlopen", side_effect=err):
        try:
            provider.send_sms("+15557654321", "Hi")
            assert False
        except SmsProviderError as exc:
            assert exc.provider_code == 21608
            assert "not verified" in str(exc).lower() or "restrictions" in str(exc).lower()


def test_send_raises_auth_failure():
    provider = _provider()
    err = _http_error(401, {"code": 20003, "message": "Authenticate"})
    with patch("urllib.request.urlopen", side_effect=err):
        try:
            provider.send_sms("+15557654321", "Hi")
            assert False
        except SmsProviderError as exc:
            assert "credentials" in str(exc).lower()


def test_send_raises_invalid_number():
    provider = _provider()
    err = _http_error(400, {"code": 21211, "message": "Invalid 'To' Phone Number"})
    with patch("urllib.request.urlopen", side_effect=err):
        try:
            provider.send_sms("+1555", "Hi")
            assert False
        except SmsProviderError as exc:
            assert "valid mobile" in str(exc).lower()


def test_send_raises_insufficient_balance():
    provider = _provider()
    err = _http_error(400, {"code": 30002, "message": "Account does not have sufficient balance"})
    with patch("urllib.request.urlopen", side_effect=err):
        try:
            provider.send_sms("+15557654321", "Hi")
            assert False
        except SmsProviderError as exc:
            assert "sufficient balance" in str(exc).lower()


def test_generic_twilio_error():
    provider = _provider()
    err = _http_error(400, {"code": 29999, "message": "Mysterious carrier rejection"})
    with patch("urllib.request.urlopen", side_effect=err):
        try:
            provider.send_sms("+15557654321", "Hi")
            assert False
        except SmsProviderError as exc:
            assert exc.provider_code == 29999
            assert "Mysterious carrier rejection" in str(exc)
            assert "Twilio error 29999" in str(exc)


def _make_persona(user_id):
    return db.create_voice_persona(
        user_id,
        {
            "name": "Test Agent",
            "persona_type": "professional",
            "prompt": "Be helpful",
            "tone": "professional",
            "goal": "Book appointments",
            "objection_handling_notes": "",
        },
    )


def test_api_returns_mapped_error_not_generic(app_client, two_users):
    import tenant_sms_db as tdb

    u1, _ = two_users
    persona_id = _make_persona(u1)
    tdb.accept_sms_terms(u1, u1)

    with app_client.session_transaction() as sess:
        sess["user_id"] = u1

    err = _http_error(
        400,
        {"code": 30034, "message": "unregistered", "more_info": "https://www.twilio.com/docs/errors/30034"},
    )
    with patch.object(config, "SMS_PROVIDER", "twilio"), \
         patch.object(config, "TWILIO_ACCOUNT_SID", "ACaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"), \
         patch.object(config, "TWILIO_AUTH_TOKEN", "token"), \
         patch.object(config, "TWILIO_PHONE_NUMBER", "+15551234567"), \
         patch.object(config, "TWILIO_MESSAGING_SERVICE_SID", ""), \
         patch("urllib.request.urlopen", side_effect=err):
        res = app_client.post(
            "/sms/messages",
            json={
                "persona_id": persona_id,
                "lead_name": "Pat",
                "phone_number": "+15557654321",
                "message_body": "Hello there",
                "compliance_confirmed": True,
                "send_now": True,
            },
        )
    assert res.status_code == 503
    data = res.get_json()
    assert "30034" in data["error"]
    assert "A2P" in data["error"]
    assert data["provider_code"] == 30034
    assert "Something went wrong" not in data["error"]
    assert "more_info" not in data
    assert "auth-token" not in json.dumps(data).lower()


def test_non_twilio_application_error(app_client, two_users):
    import tenant_sms_db as tdb

    u1, _ = two_users
    persona_id = _make_persona(u1)
    tdb.accept_sms_terms(u1, u1)

    with app_client.session_transaction() as sess:
        sess["user_id"] = u1

    with patch.object(config, "SMS_PROVIDER", "twilio"), \
         patch.object(config, "TWILIO_ACCOUNT_SID", "ACaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"), \
         patch.object(config, "TWILIO_AUTH_TOKEN", "token"), \
         patch.object(config, "TWILIO_PHONE_NUMBER", "+15551234567"), \
         patch("sms_outbound.get_sms_provider") as mock_get:
        mock_provider = MagicMock()
        mock_provider.is_configured.return_value = True
        mock_provider.send_sms.side_effect = RuntimeError("db exploded")
        mock_get.return_value = mock_provider
        with patch("sms_provider.TwilioSmsProvider.is_configured", return_value=True):
            res = app_client.post(
                "/sms/messages",
                json={
                    "persona_id": persona_id,
                    "lead_name": "Pat",
                    "phone_number": "+15557654322",
                    "message_body": "Hello there",
                    "compliance_confirmed": True,
                    "send_now": True,
                },
            )
    # Unexpected provider exceptions surface as 500/503 safe messages
    assert res.status_code in {500, 503}
    data = res.get_json()
    assert "error" in data
    assert "db exploded" not in data["error"]


def test_sms_status_endpoint(app_client, two_users):
    u1, _ = two_users
    with app_client.session_transaction() as sess:
        sess["user_id"] = u1
    with patch.object(config, "SMS_PROVIDER", "twilio"), \
         patch.object(config, "TWILIO_ACCOUNT_SID", "ACaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"), \
         patch.object(config, "TWILIO_AUTH_TOKEN", "token"), \
         patch.object(config, "TWILIO_PHONE_NUMBER", "+15551234567"), \
         patch.object(config, "TWILIO_MESSAGING_SERVICE_SID", "MG11111111111111111111111111111111"):
        res = app_client.get("/sms/status")
    assert res.status_code == 200
    data = res.get_json()
    assert data["credentials_configured"] is True
    assert data["sending_phone_configured"] is True
    assert data["messaging_service_configured"] is True
    assert "a2p_readiness" in data


def test_parse_provider_code():
    assert parse_provider_code_from_error_message(
        "SMS could not be sent. Twilio error 30034: A2P"
    ) == 30034
    assert parse_provider_code_from_error_message("other") is None
