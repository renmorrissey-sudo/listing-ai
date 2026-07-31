"""Production AI SMS send path: normalization, Postgres-safe upsert, Telnyx payload, errors."""

from unittest.mock import MagicMock, patch

import pytest

import config
import db
import tenant_sms_db as tdb
from lead_service import normalize_phone_e164
from sms_providers.telnyx import TelnyxSMSProvider


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
    monkeypatch.setattr(config, "TELNYX_MESSAGING_PROFILE_ID", "profile-1")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "+18888210810")
    monkeypatch.setattr(config, "TELNYX_PUBLIC_KEY", "pk-test")
    monkeypatch.setattr(config, "TELNYX_TRIAL_MODE", trial)
    monkeypatch.setenv("TELNYX_TOLL_FREE_VERIFICATION_STATUS", verification)
    monkeypatch.setattr(config, "TELNYX_TOLL_FREE_VERIFICATION_STATUS", verification)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 0)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 0)
    monkeypatch.setattr(config, "APP_URL", "https://example.com")  # pragma: allowlist secret


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("3038703107", "+13038703107"),
        ("(303) 870-3107", "+13038703107"),
        ("+1 303 870 3107", "+13038703107"),
        ("+13038703107", "+13038703107"),
        ("1-303-870-3107", "+13038703107"),
    ],
)
def test_us_numbers_normalize_to_e164(raw, expected):
    assert normalize_phone_e164(raw) == expected


def test_invalid_destination_rejected_before_api(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx_ready(monkeypatch)
    tdb.accept_sms_terms(u1, u1)
    with patch.object(TelnyxSMSProvider, "send_message") as send_mock:
        res = app_client.post(
            "/sms/messages",
            json={
                "persona_id": _persona(u1),
                "lead_name": "Pat",
                "phone_number": "123",
                "message_body": "Hello from TopAI",
                "compliance_confirmed": True,
                "send_now": True,
            },
        )
    assert res.status_code == 400
    assert res.get_json()["stage"] == "validation"
    assert not send_mock.called


def test_pending_toll_free_still_blocks(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx_ready(monkeypatch, verification="pending")
    tdb.accept_sms_terms(u1, u1)
    with patch.object(TelnyxSMSProvider, "send_message") as send_mock:
        res = app_client.post(
            "/sms/messages",
            json={
                "persona_id": _persona(u1),
                "lead_name": "Pat",
                "phone_number": "3038703107",
                "message_body": "Hello from TopAI",
                "compliance_confirmed": True,
                "send_now": True,
            },
        )
    assert res.status_code == 403
    assert "toll-free" in res.get_json()["error"].lower()
    assert not send_mock.called


def test_existing_lead_touch_sms_upsert_uses_bind_bool(two_users, monkeypatch):
    """Regression: Postgres rejects CASE WHEN 1; must bind engine-safe bools."""
    from db_backend import bind_bool
    from lead_service import upsert_crm_lead

    u1, _ = two_users
    lid, created, lead = upsert_crm_lead(
        u1, "3038703107", {"lead_name": "First"}, touch_sms=True
    )
    assert created is True
    assert lead["phone_number"] == "+13038703107"

    with patch("db.bind_bool", wraps=bind_bool) as mocked_bind:
        db.update_lead_contact_fields(lid, u1, name="Second", touch_sms=True)
        assert mocked_bind.called
        assert any(c.args[0] is True for c in mocked_bind.call_args_list)

    lid2, created2, lead2 = upsert_crm_lead(
        u1, "(303) 870-3107", {"lead_name": "Second"}, touch_sms=True
    )
    assert created2 is False
    assert lid2 == lid
    assert lead2["phone_number"] == "+13038703107"


def test_verified_send_uses_telnyx_payload_and_saves_id(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx_ready(monkeypatch, verification="verified")
    tdb.accept_sms_terms(u1, u1)

    captured = {}

    def fake_request(self, method, path, body=None):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        captured["auth"] = self.api_key
        return {
            "data": {
                "id": "msg_prod_test_1",
                "status": "queued",
                "to": [{"status": "queued"}],
            }
        }, 200

    with patch.object(TelnyxSMSProvider, "_request", fake_request):
        res = app_client.post(
            "/sms/messages",
            json={
                "persona_id": _persona(u1),
                "lead_name": "Pat",
                "phone_number": "3038703107",
                "message_body": "Hello from TopAI production test",
                "compliance_confirmed": True,
                "send_now": True,
            },
        )
    assert res.status_code == 201, res.get_json()
    data = res.get_json()
    assert data["provider_message_id"] == "msg_prod_test_1"
    assert data["status"] == "queued"
    assert data["to_number"] == "+13038703107"
    assert data["from_number"] == "+18888210810"
    assert captured["method"] == "POST"
    assert captured["path"] == "/messages"
    assert captured["auth"] == "KEY_TEST_ONLY"
    assert captured["auth"] != "pk-test"
    assert captured["body"]["from"] == "+18888210810"
    assert captured["body"]["to"] == "+13038703107"
    assert captured["body"]["text"] == "Hello from TopAI production test"
    assert captured["body"]["messaging_profile_id"] == "profile-1"
    # No Twilio fields required
    assert "TWILIO" not in str(captured)

    # Second send to same number must not crash on existing-lead touch_sms update.
    with patch.object(TelnyxSMSProvider, "_request", fake_request):
        res2 = app_client.post(
            "/sms/messages",
            json={
                "persona_id": _persona(u1),
                "lead_name": "Pat",
                "phone_number": "(303) 870-3107",
                "message_body": "Second Hello from TopAI",
                "compliance_confirmed": True,
                "send_now": True,
            },
        )
    assert res2.status_code == 201, res2.get_json()


def test_telnyx_4xx_recorded_and_shown(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx_ready(monkeypatch)
    tdb.accept_sms_terms(u1, u1)

    def fake_request(self, method, path, body=None):
        import json
        import urllib.error

        raise self._error_from_http(
            json.dumps({"errors": [{"code": "40001", "detail": "Invalid to number"}]}),
            400,
        )

    with patch.object(TelnyxSMSProvider, "_request", fake_request):
        res = app_client.post(
            "/sms/messages",
            json={
                "persona_id": _persona(u1),
                "lead_name": "Pat",
                "phone_number": "3038703107",
                "message_body": "Hello",
                "compliance_confirmed": True,
                "send_now": True,
            },
        )
    assert res.status_code == 503
    data = res.get_json()
    assert data["stage"] == "provider_request"
    assert data["provider_code"] == "40001"
    assert data["correlation_id"]
    assert "KEY_TEST_ONLY" not in str(data)
    assert "pk-test" not in str(data)
    assert data.get("id")  # message row recorded as failed


def test_network_exception_safely_recorded(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx_ready(monkeypatch)
    tdb.accept_sms_terms(u1, u1)

    with patch.object(
        TelnyxSMSProvider, "_request", side_effect=RuntimeError("socket down")
    ):
        res = app_client.post(
            "/sms/messages",
            json={
                "persona_id": _persona(u1),
                "lead_name": "Pat",
                "phone_number": "3038703107",
                "message_body": "Hello",
                "compliance_confirmed": True,
                "send_now": True,
            },
        )
    assert res.status_code == 500
    data = res.get_json()
    assert data["stage"] == "provider_request"
    assert "could not submit" in data["error"].lower()
    assert data["correlation_id"]
    assert "socket down" not in data["error"]
    assert "KEY_TEST_ONLY" not in str(data)


def test_opted_out_remains_blocked(app_client, two_users, monkeypatch):
    from lead_service import upsert_crm_lead
    from sms_authorization import record_one_to_one_attestation

    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx_ready(monkeypatch)
    tdb.accept_sms_terms(u1, u1)
    lead_id, _, _ = upsert_crm_lead(
        u1, "+13038703107", {"lead_name": "Opt"}, touch_sms=True
    )
    record_one_to_one_attestation(
        u1, lead_id, message_body="Hello", source_page="test"
    )
    db.mark_lead_opt_out(lead_id, u1)
    with patch.object(TelnyxSMSProvider, "send_message") as send_mock:
        res = app_client.post(
            "/sms/messages",
            json={
                "persona_id": _persona(u1),
                "lead_name": "Opt",
                "phone_number": "3038703107",
                "message_body": "Hello",
                "compliance_confirmed": True,
                "send_now": True,
            },
        )
    assert res.status_code == 403
    assert "opt" in res.get_json()["error"].lower()
    assert not send_mock.called


def test_tenant_isolation_on_send(app_client, two_users, monkeypatch):
    from lead_service import upsert_crm_lead

    u1, u2 = two_users
    db.update_user_subscription(u1, "active")
    db.update_user_subscription(u2, "active")
    _telnyx_ready(monkeypatch)
    tdb.accept_sms_terms(u1, u1)
    tdb.accept_sms_terms(u2, u2)
    lead_u2, _, _ = upsert_crm_lead(
        u2, "3038703107", {"lead_name": "Other"}, touch_sms=True
    )
    _login(app_client, u1)
    # Sending as u1 creates/uses u1's own lead — never u2's row.
    with patch.object(
        TelnyxSMSProvider,
        "send_message",
        return_value={"provider_message_id": "m1", "status": "queued"},
    ):
        res = app_client.post(
            "/sms/messages",
            json={
                "persona_id": _persona(u1),
                "lead_name": "Mine",
                "phone_number": "3038703107",
                "message_body": "Hello",
                "compliance_confirmed": True,
                "send_now": True,
            },
        )
    assert res.status_code == 201
    assert res.get_json()["lead_id"] != lead_u2
    assert db.get_lead(lead_u2, u1) is None


def test_audit_persist_failure_does_not_claim_false_success(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx_ready(monkeypatch)
    tdb.accept_sms_terms(u1, u1)

    def fake_request(self, method, path, body=None):
        return {"data": {"id": "msg_ok", "status": "queued", "to": [{"status": "queued"}]}}, 200

    with patch.object(TelnyxSMSProvider, "_request", fake_request), patch(
        "sms_outbound.db.update_sms_message_send_result",
        side_effect=RuntimeError("db write failed"),
    ), patch("sms_outbound.db.set_lead_consent", side_effect=RuntimeError("db write failed")), patch(
        "sms_outbound.db.touch_lead_outbound", side_effect=RuntimeError("db write failed")
    ):
        res = app_client.post(
            "/sms/messages",
            json={
                "persona_id": _persona(u1),
                "lead_name": "Pat",
                "phone_number": "3038703107",
                "message_body": "Hello",
                "compliance_confirmed": True,
                "send_now": True,
            },
        )
    # Provider accepted — response remains success with warning, not a false "failed".
    assert res.status_code == 201
    data = res.get_json()
    assert data["provider_message_id"] == "msg_ok"
    assert data.get("warning")
    assert "db write failed" not in str(data)


def test_no_twilio_required_for_telnyx(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx_ready(monkeypatch)
    monkeypatch.setattr(config, "TWILIO_ACCOUNT_SID", "")
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "")
    monkeypatch.setattr(config, "TWILIO_PHONE_NUMBER", "")
    tdb.accept_sms_terms(u1, u1)
    with patch.object(
        TelnyxSMSProvider,
        "send_message",
        return_value={"provider_message_id": "m1", "status": "queued"},
    ):
        res = app_client.post(
            "/sms/messages",
            json={
                "persona_id": _persona(u1),
                "lead_name": "Pat",
                "phone_number": "3038703107",
                "message_body": "Hello",
                "compliance_confirmed": True,
                "send_now": True,
            },
        )
    assert res.status_code == 201
