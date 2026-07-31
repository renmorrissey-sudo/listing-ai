"""Regression tests for Telnyx outbound SMS send path (no live provider calls)."""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

import config
import db
import tenant_sms_db as tdb
from lead_service import normalize_phone_e164, upsert_crm_lead
from sms_authorization import (
    can_send_sms,
    check_telnyx_toll_free_send_allowed,
    record_one_to_one_attestation,
)
from sms_outbound import send_authorized_sms
from sms_providers.base import SmsProviderError
from sms_providers.telnyx import TelnyxSMSProvider
from sms_validation import validate_sms_send_payload


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


def _telnyx_ready(monkeypatch, *, verification="verified", trial=False):
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY_TEST_ONLY")
    monkeypatch.setattr(config, "TELNYX_PUBLIC_KEY", "PUBLIC_KEY_MUST_NOT_SEND")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "+18888210810")
    monkeypatch.setattr(config, "TELNYX_MESSAGING_PROFILE_ID", "profile-test")
    monkeypatch.setattr(config, "TELNYX_TOLL_FREE_VERIFICATION_STATUS", verification)
    monkeypatch.setattr(config, "TELNYX_TRIAL_MODE", trial)
    monkeypatch.setattr(config, "TELNYX_API_BASE", "https://api.telnyx.com/v2")
    monkeypatch.setattr(config, "APP_URL", "https://example.test")
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 0)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 0)


def test_e164_normalization_production_destination():
    assert normalize_phone_e164("3038703107") == "+13038703107"
    cleaned, err = validate_sms_send_payload(
        {
            "persona_id": 1,
            "lead_name": "Test",
            "phone_number": "3038703107",
            "message_body": "Hello from TopAI",
            "compliance_confirmed": True,
            "send_now": True,
        }
    )
    assert err is None
    assert cleaned["phone_number"] == "+13038703107"


def test_telnyx_uses_api_key_not_public_key_and_correct_endpoint(monkeypatch):
    _telnyx_ready(monkeypatch)
    provider = TelnyxSMSProvider()
    captured = {}

    class _Resp:
        status = 200

        def read(self):
            return json.dumps({"data": {"id": "msg-abc", "to": [{"status": "queued"}]}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = provider.send_message(
            to_number="+13038703107",
            body="Hello from TopAI",
            from_number="+18888210810",
        )

    assert captured["url"] == "https://api.telnyx.com/v2/messages"
    assert captured["method"] == "POST"
    assert captured["headers"]["authorization"] == "Bearer KEY_TEST_ONLY"
    assert "PUBLIC_KEY" not in captured["headers"]["authorization"]
    assert captured["headers"]["content-type"] == "application/json"
    assert captured["body"] == {
        "from": "+18888210810",
        "to": "+13038703107",
        "text": "Hello from TopAI",
        "type": "SMS",
        "messaging_profile_id": "profile-test",
    }
    assert result["provider_message_id"] == "msg-abc"
    assert result["http_status"] == 200


def test_telnyx_api_rejection_maps_safe_error(monkeypatch):
    _telnyx_ready(monkeypatch)
    provider = TelnyxSMSProvider()

    def fake_urlopen(req, timeout=30):
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=MagicMock(
                read=MagicMock(
                    return_value=json.dumps(
                        {"errors": [{"code": "10005", "detail": "Invalid API key"}]}
                    ).encode()
                )
            ),
        )

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(SmsProviderError) as excinfo:
            provider.send_message(
                to_number="+13038703107",
                body="Hello",
                from_number="+18888210810",
            )
    err = excinfo.value
    assert err.status_code == 401
    assert err.provider_code == "10005"
    assert "authentication" in str(err).lower()
    assert "KEY_TEST_ONLY" not in str(err)
    assert "Invalid API key" not in str(err) or "authentication" in str(err).lower()


def test_telnyx_network_exception(monkeypatch):
    _telnyx_ready(monkeypatch)
    provider = TelnyxSMSProvider()

    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("connection refused"),
    ):
        with pytest.raises(SmsProviderError) as excinfo:
            provider.send_message(
                to_number="+13038703107",
                body="Hello",
                from_number="+18888210810",
            )
    assert excinfo.value.retryable is True
    assert "could not reach" in str(excinfo.value).lower()


def test_update_lead_contact_fields_uses_bind_bool_for_postgres(monkeypatch, two_users):
    """Postgres CASE WHEN rejects integer 0/1; send path must bind bools."""
    from db_backend import bind_bool as real_bind_bool

    u1, _ = two_users
    lead_id, _, _ = upsert_crm_lead(
        u1,
        "3038703107",
        {"lead_name": "Existing", "phone_number": "3038703107"},
        source="sms",
        touch_sms=True,
        assigned_user_id=u1,
    )

    calls = []

    def spy_bind_bool(value):
        out = real_bind_bool(value)
        calls.append((bool(value), out))
        return out

    monkeypatch.setattr("db.bind_bool", spy_bind_bool)
    db.update_lead_contact_fields(lead_id, u1, touch_sms=True, name="Existing")
    assert calls, "update_lead_contact_fields must call bind_bool for CASE WHEN binds"
    assert (True, 1) in calls or any(flag is True for flag, _ in calls)

    # Under Postgres engine, bind_bool returns native bool (not 0/1).
    monkeypatch.setattr(config, "DB_ENGINE", "postgres")
    assert real_bind_bool(True) is True
    assert real_bind_bool(False) is False
    assert real_bind_bool(True) is not 1
    assert real_bind_bool(False) is not 0

    # Existing-lead upsert (the production failure path) still succeeds on SQLite.
    monkeypatch.setattr(config, "DB_ENGINE", "sqlite")
    lead_id2, created, lead = upsert_crm_lead(
        u1,
        "3038703107",
        {"lead_name": "Existing", "phone_number": "3038703107", "message_body": "Hi"},
        source="sms",
        touch_sms=True,
        assigned_user_id=u1,
    )
    assert created is False
    assert lead_id2 == lead_id
    assert lead["phone_number"] == "+13038703107"


def test_existing_lead_send_reaches_telnyx_after_upsert(app_client, two_users, monkeypatch):
    """Reproduce the failed production path: existing lead + POST /sms/messages."""
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx_ready(monkeypatch, verification="verified")
    tdb.accept_sms_terms(u1, u1)

    # Pre-create lead (draft / prior attempt) so send hits update_lead_contact_fields.
    upsert_crm_lead(
        u1,
        "3038703107",
        {"lead_name": "Prod Test", "phone_number": "3038703107"},
        source="sms",
        touch_sms=True,
        assigned_user_id=u1,
    )

    with patch.object(TelnyxSMSProvider, "send_message") as send_mock:
        send_mock.return_value = {
            "provider_message_id": "telnyx-msg-1",
            "status": "queued",
            "http_status": 200,
        }
        res = app_client.post(
            "/sms/messages",
            json={
                "persona_id": _persona(u1),
                "lead_name": "Prod Test",
                "phone_number": "3038703107",
                "message_body": "Hello from TopAI production test",
                "compliance_confirmed": True,
                "send_now": True,
            },
        )

    assert res.status_code == 201, res.get_data(as_text=True)
    data = res.get_json()
    assert data["provider_message_id"] == "telnyx-msg-1"
    assert data["normalized_destination"] == "+13038703107"
    assert data["from_number"] == "+18888210810"
    assert data["reached_provider"] is True
    assert data["correlation_id"]
    assert data["stage"] == "complete"
    assert send_mock.called
    kwargs = send_mock.call_args.kwargs
    assert kwargs["to_number"] == "+13038703107"
    assert kwargs["from_number"] == "+18888210810"
    assert "Hello from TopAI" in kwargs["body"]


def test_db_audit_record_failure_before_provider(two_users, monkeypatch):
    u1, _ = two_users
    _telnyx_ready(monkeypatch, verification="verified")
    tdb.accept_sms_terms(u1, u1)
    lead_id, _, _ = upsert_crm_lead(
        u1,
        "+13038703107",
        {"lead_name": "Audit Fail"},
        source="sms",
        touch_sms=True,
        assigned_user_id=u1,
    )
    with patch("db.create_sms_message", side_effect=RuntimeError("db down")):
        with patch.object(TelnyxSMSProvider, "send_message") as send_mock:
            result, err, status = send_authorized_sms(
                u1,
                lead_id,
                "Hello",
                source_page="test",
                compliance_confirmed=True,
            )
    assert status == 500
    assert err and "internal" in err.lower()
    assert result["reached_provider"] is False
    assert result["stage"] == "db_record"
    assert result["correlation_id"]
    assert not send_mock.called


def test_verified_status_permits_submission(monkeypatch):
    _telnyx_ready(monkeypatch, verification="verified")
    ok, err = check_telnyx_toll_free_send_allowed()
    assert ok is True
    assert err is None


def test_pending_status_blocks_submission(monkeypatch):
    _telnyx_ready(monkeypatch, verification="pending")
    ok, err = check_telnyx_toll_free_send_allowed()
    assert ok is False
    assert "verification" in (err or "").lower()


def test_consent_and_opt_out_enforcement(two_users, monkeypatch):
    u1, _ = two_users
    _telnyx_ready(monkeypatch, verification="verified")
    tdb.accept_sms_terms(u1, u1)
    lead_id, _, _ = upsert_crm_lead(
        u1,
        "+13038703107",
        {"lead_name": "Consent"},
        source="sms",
        touch_sms=True,
        assigned_user_id=u1,
    )

    # No attestation / certification → blocked.
    ok, msg = can_send_sms(u1, lead_id, message_body="Hi there")
    assert ok is False
    assert "consent" in msg.lower() or "certif" in msg.lower()

    record_one_to_one_attestation(
        u1, lead_id, message_body="Hi there", source_page="test"
    )
    ok2, _ = can_send_sms(u1, lead_id, message_body="Hi there")
    assert ok2 is True

    db.mark_lead_opt_out(lead_id, u1)
    ok3, msg3 = can_send_sms(u1, lead_id, message_body="Hi there")
    assert ok3 is False
    assert "opt" in msg3.lower()


def test_no_twilio_credential_required_for_telnyx_send(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx_ready(monkeypatch, verification="verified")
    monkeypatch.setattr(config, "TWILIO_ACCOUNT_SID", "")
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "")
    monkeypatch.setattr(config, "TWILIO_PHONE_NUMBER", "")
    monkeypatch.setattr(config, "TWILIO_MESSAGING_SERVICE_SID", "")
    tdb.accept_sms_terms(u1, u1)

    with patch.object(TelnyxSMSProvider, "send_message") as send_mock:
        send_mock.return_value = {
            "provider_message_id": "msg-no-twilio",
            "status": "queued",
            "http_status": 200,
        }
        res = app_client.post(
            "/sms/messages",
            json={
                "persona_id": _persona(u1),
                "lead_name": "No Twilio",
                "phone_number": "3038703107",
                "message_body": "Hello without Twilio",
                "compliance_confirmed": True,
                "send_now": True,
            },
        )
    assert res.status_code == 201, res.get_data(as_text=True)
    assert send_mock.called
    assert "TWILIO" not in str(send_mock.call_args)


def test_api_success_response_includes_diagnostics(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx_ready(monkeypatch, verification="verified")
    tdb.accept_sms_terms(u1, u1)
    with patch.object(TelnyxSMSProvider, "send_message") as send_mock:
        send_mock.return_value = {
            "provider_message_id": "msg-ok",
            "status": "queued",
            "http_status": 200,
        }
        res = app_client.post(
            "/sms/messages",
            json={
                "persona_id": _persona(u1),
                "lead_name": "OK",
                "phone_number": "+13038703107",
                "message_body": "Success path",
                "compliance_confirmed": True,
                "send_now": True,
            },
        )
    data = res.get_json()
    assert res.status_code == 201
    assert data["provider_message_id"] == "msg-ok"
    assert data["stage"] == "complete"
    assert data["reached_provider"] is True
    assert data["correlation_id"]


def test_lead_upsert_failure_never_reaches_provider(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx_ready(monkeypatch, verification="verified")
    tdb.accept_sms_terms(u1, u1)
    with patch("app.upsert_crm_lead", side_effect=RuntimeError("pg case when")):
        with patch.object(TelnyxSMSProvider, "send_message") as send_mock:
            res = app_client.post(
                "/sms/messages",
                json={
                    "persona_id": _persona(u1),
                    "lead_name": "Boom",
                    "phone_number": "3038703107",
                    "message_body": "Should fail before provider",
                    "compliance_confirmed": True,
                    "send_now": True,
                },
            )
    assert res.status_code == 500
    data = res.get_json()
    assert data["reached_provider"] is False
    assert data["stage"] == "lead_upsert"
    assert data["normalized_destination"] == "+13038703107"
    assert data["correlation_id"]
    assert "Something went wrong" not in data["error"] or data["correlation_id"]
    assert not send_mock.called
