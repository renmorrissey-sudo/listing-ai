"""AI SMS Assistant outbound status / success UX / Telnyx delivery webhook tests."""

from __future__ import annotations

import uuid
from unittest.mock import patch

import config
import db
import telnyx_webhooks as txwh
import tenant_sms_db as tdb
from sms_status_model import (
    format_phone_display,
    latest_outbound_diagnostics,
    normalize_provider_status,
)


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
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY")
    monkeypatch.setattr(config, "TELNYX_MESSAGING_PROFILE_ID", "profile-1")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "+18888210810")
    monkeypatch.setattr(config, "TELNYX_PUBLIC_KEY", "pk-test")
    monkeypatch.setattr(config, "TELNYX_TRIAL_MODE", trial)
    monkeypatch.setattr(config, "TELNYX_TOLL_FREE_VERIFICATION_STATUS", verification)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 0)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 0)
    monkeypatch.setattr(config, "APP_URL", "https://example.com")  # pragma: allowlist secret


def _accept_terms(user_id):
    tdb.accept_sms_terms(user_id, user_id)


def test_normalize_queued_not_delivered():
    assert normalize_provider_status("queued") == "queued"
    assert normalize_provider_status("delivered") == "delivered"
    assert normalize_provider_status("queued") != "delivered"


def test_phone_display_readable_while_e164_stored():
    assert format_phone_display("+13038703107") == "+1 (303) 870-3107"
    assert format_phone_display("3038703107") == "+1 (303) 870-3107"


def test_empty_account_shows_no_sms_sent_yet(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _telnyx_ready(monkeypatch)
    data = app_client.get("/sms/messages").get_json()
    status = data["provider_status"]
    assert status["has_outbound"] is False
    assert status["empty_state_message"] == "No SMS has been sent yet."
    assert status["latest_send_status"] is None
    assert status.get("latest_telnyx_error_code") is None
    # UI must not hard-code "none" when empty — empty_state_message is the signal.
    html = app_client.get("/app").get_data(as_text=True)
    assert "No SMS has been sent yet." in html
    assert "Latest Telnyx send status: ${status.latest_send_status || 'none'}" not in html
    assert "error code: none" not in html.lower() or "Latest Telnyx error code:" not in html


def test_successful_telnyx_send_persists_and_status_not_none(
    app_client, two_users, monkeypatch
):
    u1, _ = two_users
    _login(app_client, u1)
    _telnyx_ready(monkeypatch)
    _accept_terms(u1)
    with patch("sms_providers.telnyx.TelnyxSMSProvider.send_message") as send_mock:
        send_mock.return_value = {
            "provider_message_id": "telnyx-success-mid-1",
            "status": "queued",
            "to": "+13038703107",
            "from": "+18888210810",
            "segments": 1,
        }
        res = app_client.post(
            "/sms/messages",
            json={
                "persona_id": _persona(u1),
                "lead_name": "Denver Lead",
                "phone_number": "3038703107",
                "message_body": "Hello from TopAI status test",
                "compliance_confirmed": True,
                "send_now": True,
            },
        )
    assert res.status_code == 201
    body = res.get_json()
    assert body["status"] == "queued"
    assert body["provider_message_id"] == "telnyx-success-mid-1"
    assert body["to_number"] == "+13038703107"
    assert body["to_number_display"] == "+1 (303) 870-3107"
    assert body["success_notice"]["kind"] == "submitted"
    assert "submitted successfully" in body["success_notice"]["title"].lower()
    assert body["status"] != "delivered"
    assert send_mock.called

    row = db.get_sms_message_by_provider_id("telnyx-success-mid-1")
    assert row is not None
    assert row["user_id"] == u1
    assert row["status"] == "queued"
    assert row["phone_number"] == "+13038703107"
    assert row["from_number"] == "+18888210810"
    assert row["submitted_at"]
    assert not row.get("delivered_at")

    status = app_client.get("/sms/status").get_json()
    assert status["has_outbound"] is True
    assert status["latest_send_status"] == "queued"
    assert status["latest_sms_status"] == "queued"
    assert status["latest_sms_message_id"] == "telnyx-success-mid-1"
    assert status["latest_sms_destination"] == "+13038703107"
    assert status["latest_sms_destination_display"] == "+1 (303) 870-3107"
    assert status["latest_telnyx_error_code"] is None
    assert status["latest_correlation_id"] is None

    messages = app_client.get("/sms/messages").get_json()
    assert messages["provider_status"]["latest_send_status"] == "queued"
    assert messages["provider_status"]["latest_sms_message_id"] == "telnyx-success-mid-1"
    assert messages["provider_status"].get("latest_telnyx_error_code") is None


def test_queued_does_not_display_as_delivered(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _telnyx_ready(monkeypatch)
    _accept_terms(u1)
    with patch("sms_providers.telnyx.TelnyxSMSProvider.send_message") as send_mock:
        # Even if a buggy provider returned delivered on accept, outbound path normalizes.
        send_mock.return_value = {
            "provider_message_id": "telnyx-mid-queued-guard",
            "status": "delivered",
            "to": "+13038703107",
            "from": "+18888210810",
        }
        res = app_client.post(
            "/sms/messages",
            json={
                "persona_id": _persona(u1),
                "lead_name": "Guard",
                "phone_number": "+13038703107",
                "message_body": "Hello from TopAI",
                "compliance_confirmed": True,
                "send_now": True,
            },
        )
    assert res.status_code == 201
    assert res.get_json()["status"] == "queued"
    row = db.get_sms_message_by_provider_id("telnyx-mid-queued-guard")
    assert row["status"] == "queued"
    assert not row.get("delivered_at")


def test_delivered_webhook_updates_message(two_users):
    u1, _ = two_users
    mid = db.create_sms_message(
        user_id=u1,
        persona_id=None,
        provider="telnyx",
        data={
            "lead_name": "X",
            "phone_number": "+13038703107",
            "message_body": "Hi",
        },
        status="queued",
        direction="outbound",
    )
    db.update_sms_message_send_result(
        mid,
        provider_message_id="telnyx-mid-deliv-1",
        status="queued",
        to_number="+13038703107",
        from_number="+18888210810",
    )
    payload = {
        "data": {
            "event_type": "message.finalized",
            "id": f"evt-d-{uuid.uuid4().hex[:8]}",
            "payload": {
                "id": "telnyx-mid-deliv-1",
                "from": {"phone_number": "+18888210810"},
                "to": [{"phone_number": "+13038703107", "status": "delivered"}],
            },
        }
    }
    result, status = txwh.handle_messaging_webhook(payload)
    assert status == 200
    assert result["status"] == "delivered"
    row = db.get_sms_message_by_provider_id("telnyx-mid-deliv-1")
    assert row["status"] == "delivered"
    assert row["delivered_at"]
    assert row["user_id"] == u1


def test_duplicate_delivery_webhook_idempotent(two_users):
    u1, _ = two_users
    mid = db.create_sms_message(
        user_id=u1,
        persona_id=None,
        provider="telnyx",
        data={"lead_name": "X", "phone_number": "+13038703108", "message_body": "Hi"},
        status="queued",
        direction="outbound",
    )
    db.update_sms_message_send_result(
        mid, provider_message_id="telnyx-mid-dup-1", status="queued"
    )
    evt = f"evt-dup-deliv-{uuid.uuid4().hex[:8]}"
    payload = {
        "data": {
            "event_type": "message.finalized",
            "id": evt,
            "payload": {
                "id": "telnyx-mid-dup-1",
                "from": {"phone_number": "+18888210810"},
                "to": [{"phone_number": "+13038703108", "status": "delivered"}],
            },
        }
    }
    r1, s1 = txwh.handle_messaging_webhook(payload)
    r2, s2 = txwh.handle_messaging_webhook(payload)
    assert s1 == 200 and s2 == 200
    assert r2.get("duplicate") is True
    row = db.get_sms_message_by_provider_id("telnyx-mid-dup-1")
    assert row["status"] == "delivered"


def test_failed_delivery_safe_red_fields(two_users, app_client):
    u1, _ = two_users
    _login(app_client, u1)
    mid = db.create_sms_message(
        user_id=u1,
        persona_id=None,
        provider="telnyx",
        data={"lead_name": "X", "phone_number": "+13038703109", "message_body": "Hi"},
        status="queued",
        direction="outbound",
    )
    db.update_sms_message_send_result(
        mid, provider_message_id="telnyx-mid-fail-1", status="queued"
    )
    payload = {
        "data": {
            "event_type": "message.delivery_failed",
            "id": f"evt-f-{uuid.uuid4().hex[:8]}",
            "payload": {
                "id": "telnyx-mid-fail-1",
                "from": {"phone_number": "+18888210810"},
                "to": [
                    {
                        "phone_number": "+13038703109",
                        "status": "delivery_failed",
                        "errors": [{"code": "40001", "detail": "carrier rejected"}],
                    }
                ],
            },
        }
    }
    result, status = txwh.handle_messaging_webhook(payload)
    assert status == 200
    assert result["status"] == "delivery_failed"
    row = db.get_sms_message_by_provider_id("telnyx-mid-fail-1")
    assert row["status"] == "delivery_failed"
    assert row["failure_code"] == "40001"
    assert row["failed_at"]
    assert "Authorization" not in (row.get("error_message") or "")
    assert "api_key" not in (row.get("error_message") or "").lower()

    diag = app_client.get("/sms/status").get_json()
    assert diag["latest_sms_status"] == "delivery_failed"
    assert diag["latest_telnyx_error_code"] == "40001"
    assert diag["latest_correlation_id"] is None or diag["latest_correlation_id"]


def test_error_row_hidden_when_no_error(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _telnyx_ready(monkeypatch)
    mid = db.create_sms_message(
        user_id=u1,
        persona_id=None,
        provider="telnyx",
        data={"lead_name": "Ok", "phone_number": "+13038703110", "message_body": "Hi"},
        status="queued",
        direction="outbound",
    )
    db.update_sms_message_send_result(
        mid,
        provider_message_id="telnyx-mid-ok-1",
        status="queued",
        to_number="+13038703110",
    )
    status = app_client.get("/sms/status").get_json()
    assert status["latest_telnyx_error_code"] is None
    assert status["latest_error_code"] is None
    assert status["latest_correlation_id"] is None
    html = app_client.get("/app").get_data(as_text=True)
    assert "if (errCode != null && errCode !== '')" in html
    assert "Latest Telnyx error code: ${" not in html


def test_latest_status_no_longer_none_when_record_exists(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _telnyx_ready(monkeypatch)
    mid = db.create_sms_message(
        user_id=u1,
        persona_id=None,
        provider="telnyx",
        data={"lead_name": "Ok", "phone_number": "+13038703111", "message_body": "Hi"},
        status="queued",
        direction="outbound",
    )
    db.update_sms_message_send_result(
        mid, provider_message_id="telnyx-mid-exists-1", status="sent"
    )
    data = app_client.get("/sms/messages").get_json()["provider_status"]
    assert data["latest_send_status"] == "sent"
    assert data["latest_send_status"] != "none"
    assert data["has_outbound"] is True


def test_tenant_isolation_latest_status(app_client, two_users, monkeypatch):
    u1, u2 = two_users
    _telnyx_ready(monkeypatch)
    mid = db.create_sms_message(
        user_id=u1,
        persona_id=None,
        provider="telnyx",
        data={"lead_name": "Private", "phone_number": "+13038703112", "message_body": "Secret"},
        status="queued",
        direction="outbound",
    )
    db.update_sms_message_send_result(
        mid,
        provider_message_id="telnyx-mid-tenant-1",
        status="delivered",
        to_number="+13038703112",
    )
    _login(app_client, u2)
    status = app_client.get("/sms/status").get_json()
    assert status["has_outbound"] is False
    assert status.get("latest_sms_message_id") is None
    assert status.get("latest_sms_destination") != "+13038703112"
    # Browser-supplied tenant id must not override session.
    status2 = app_client.get("/sms/status?tenant_id=1&user_id=1").get_json()
    assert status2["has_outbound"] is False


def test_successful_send_clears_previous_error_notice(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _telnyx_ready(monkeypatch)
    _accept_terms(u1)
    fail_id = db.create_sms_message(
        user_id=u1,
        persona_id=None,
        provider="telnyx",
        data={"lead_name": "Old", "phone_number": "+13038703113", "message_body": "Hi"},
        status="provider_error",
        direction="outbound",
    )
    db.update_sms_message_send_result(
        fail_id,
        status="provider_error",
        error_message="SMS could not be submitted.",
        failure_code="40100",
        correlation_id="abc123olderr",
    )
    with patch("sms_providers.telnyx.TelnyxSMSProvider.send_message") as send_mock:
        send_mock.return_value = {
            "provider_message_id": "telnyx-mid-clear-err",
            "status": "queued",
            "to": "+13038703113",
            "from": "+18888210810",
        }
        res = app_client.post(
            "/sms/messages",
            json={
                "persona_id": _persona(u1),
                "lead_name": "Old",
                "phone_number": "+13038703113",
                "message_body": "Hello again from TopAI",
                "compliance_confirmed": True,
                "send_now": True,
            },
        )
    assert res.status_code == 201
    status = app_client.get("/sms/status").get_json()
    assert status["latest_sms_status"] == "queued"
    assert status["latest_sms_message_id"] == "telnyx-mid-clear-err"
    assert status["latest_telnyx_error_code"] is None
    assert status["latest_correlation_id"] is None


def test_double_click_guard_in_ui(app_client, two_users, monkeypatch):
    u1, _ = two_users
    db.update_user_subscription(u1, "active")
    _login(app_client, u1)
    _telnyx_ready(monkeypatch)
    html = app_client.get("/app").get_data(as_text=True)
    assert "smsSendInFlight" in html
    assert "if (smsSendInFlight) return;" in html
    assert "label.textContent = 'Submitted'" in html
    assert "showSmsSuccessNotice" in html
    assert "hideSmsError()" in html
    assert "startSmsStatusPolling" in html
    assert "success-msg" in html
    assert "sms-success" in html


def test_no_real_sms_during_tests(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _telnyx_ready(monkeypatch)
    _accept_terms(u1)
    with patch("sms_providers.telnyx.TelnyxSMSProvider._request") as req:
        with patch("sms_providers.telnyx.TelnyxSMSProvider.send_message") as send_mock:
            send_mock.return_value = {
                "provider_message_id": "mock-only",
                "status": "queued",
                "to": "+13038703114",
                "from": "+18888210810",
            }
            res = app_client.post(
                "/sms/messages",
                json={
                    "persona_id": _persona(u1),
                    "lead_name": "Mock",
                    "phone_number": "+13038703114",
                    "message_body": "Mock send",
                    "compliance_confirmed": True,
                    "send_now": True,
                },
            )
    assert res.status_code == 201
    assert not req.called
    assert send_mock.called


def test_diagnostics_helper_hides_error_without_failure():
    diag = latest_outbound_diagnostics(
        {
            "id": 1,
            "status": "queued",
            "phone_number": "+13038703107",
            "provider_message_id": "m1",
            "submitted_at": "2026-07-31T12:00:00+00:00",
            "failure_code": None,
            "error_message": None,
            "correlation_id": "should-hide",
        }
    )
    assert diag["latest_telnyx_error_code"] is None
    assert diag["latest_correlation_id"] is None
    assert diag["latest_sms_destination_display"] == "+1 (303) 870-3107"
