"""Telnyx Messaging API V2 provider and webhook tests."""

from __future__ import annotations

import base64
import json
import time
import uuid
from unittest.mock import MagicMock, patch

import config
import db
import tenant_sms_db as tdb
from sms_authorization import can_send_sms, check_telnyx_trial_destination, record_one_to_one_attestation
from sms_providers.factory import get_sms_provider
from sms_providers.telnyx import TelnyxSMSProvider
import telnyx_webhooks as txwh


def _unique_e164(prefix="555"):
    n = int(uuid.uuid4().hex[:8], 16) % 10_000_000
    return f"+1{prefix}{n:07d}"[:12]


def _lead(user_id, phone, name="Lead"):
    from lead_service import upsert_crm_lead

    lead_id, _, lead = upsert_crm_lead(
        user_id,
        phone,
        {"name": name, "phone_number": phone},
        source="test",
        touch_sms=True,
        assigned_user_id=user_id,
    )
    return lead_id, lead


def test_factory_selects_telnyx(monkeypatch):
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY")
    assert isinstance(get_sms_provider(), TelnyxSMSProvider)


def test_telnyx_send_payload(monkeypatch):
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY")
    provider = TelnyxSMSProvider()
    with patch.object(provider, "_request") as req:
        req.return_value = (
            {"data": {"id": "msg-1", "to": [{"status": "queued"}]}},
            200,
        )
        result = provider.send_sms("+15551230000", "Hello", from_number="+15550001111")
        assert result["provider_message_id"] == "msg-1"
        body = req.call_args[0][2]
        assert body["from"] == "+15550001111"
        assert body["to"] == "+15551230000"
        assert body["text"] == "Hello"


def test_trial_blocks_other_destinations(monkeypatch):
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "TELNYX_TRIAL_MODE", True)
    monkeypatch.setattr(config, "TELNYX_VERIFIED_TEST_NUMBER", "+15551239999")
    ok, err = check_telnyx_trial_destination("+15551230000")
    assert ok is False
    assert "verified test" in err.lower()
    ok2, _ = check_telnyx_trial_destination("+15551239999")
    assert ok2 is True


def test_can_send_enforces_trial(two_users, monkeypatch):
    u1, _ = two_users
    monkeypatch.setattr(config, "SMS_PROVIDER", "telnyx")
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY")
    monkeypatch.setattr(config, "TELNYX_TRIAL_MODE", True)
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "+15550001111")
    monkeypatch.setattr(config, "TELNYX_VERIFIED_TEST_NUMBER", "+15551239999")
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 0)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 0)
    monkeypatch.setattr(config, "TELNYX_TOLL_FREE_VERIFICATION_STATUS", "verified")
    tdb.accept_sms_terms(u1, u1)
    lead_id, _ = _lead(u1, "+15551230001")
    record_one_to_one_attestation(u1, lead_id, message_body="Hi", source_page="test")
    ok, msg = can_send_sms(u1, lead_id, message_body="Hi")
    assert ok is False
    assert "verified test" in msg.lower()

    lead2, _ = _lead(u1, "+15551239999")
    record_one_to_one_attestation(u1, lead2, message_body="Hi", source_page="test")
    ok2, _ = can_send_sms(u1, lead2, message_body="Hi")
    assert ok2 is True


def test_webhook_inbound_and_stop(two_users, monkeypatch):
    u1, _ = two_users
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "+15550002222")
    tdb.upsert_tenant_sender(
        u1,
        sender_number="+15550002222",
        sms_provider="telnyx",
        sms_enabled=True,
        registration_status="verified",
    )
    contact = _unique_e164("600")
    payload = {
        "data": {
            "event_type": "message.received",
            "id": f"evt-{uuid.uuid4().hex[:10]}",
            "payload": {
                "id": f"msg-{uuid.uuid4().hex[:10]}",
                "text": "STOP",
                "from": {"phone_number": contact},
                "to": [{"phone_number": "+15550002222"}],
            },
        }
    }
    result, status = txwh.handle_messaging_webhook(payload)
    assert status == 200
    assert result["ok"] is True
    lead = db.get_lead(result["lead_id"], u1)
    assert lead["opt_out_status"] == "opted_out" or lead["sms_consent_status"] == "opted_out"
    assert tdb.is_suppressed(u1, contact)


def test_help_keyword_sends_support_number_reply(two_users, monkeypatch):
    u1, _ = two_users
    monkeypatch.setattr(config, "TELNYX_API_KEY", "KEY")
    help_number = _unique_e164("188")
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", help_number)
    tdb.upsert_tenant_sender(
        u1,
        sender_number=help_number,
        sms_provider="telnyx",
        sms_enabled=True,
        registration_status="verified",
    )
    contact = _unique_e164("610")
    payload = {
        "data": {
            "event_type": "message.received",
            "id": f"evt-help-{uuid.uuid4().hex[:10]}",
            "payload": {
                "id": f"msg-help-{uuid.uuid4().hex[:10]}",
                "text": "HELP",
                "from": {"phone_number": contact},
                "to": [{"phone_number": help_number}],
            },
        }
    }
    with patch.object(TelnyxSMSProvider, "send_message") as send_mock:
        send_mock.return_value = {"provider_message_id": "help-out-1", "status": "queued"}
        result, status = txwh.handle_messaging_webhook(payload)
    assert status == 200
    assert result["ok"] is True
    assert send_mock.called
    kwargs = send_mock.call_args.kwargs
    assert "(888) 821-0810" in kwargs["body"]
    assert kwargs["to_number"] == contact


def test_webhook_delivery_updates_existing(two_users):
    u1, _ = two_users
    mid = db.create_sms_message(
        user_id=u1,
        persona_id=None,
        provider="telnyx",
        data={"lead_name": "X", "phone_number": "+15551230009", "message_body": "Hi"},
        status="queued",
        direction="outbound",
    )
    db.update_sms_message_send_result(mid, provider_message_id="telnyx-mid-1", status="queued")
    payload = {
        "data": {
            "event_type": "message.finalized",
            "id": f"evt-d-{uuid.uuid4().hex[:8]}",
            "payload": {
                "id": "telnyx-mid-1",
                "from": {"phone_number": "+15550001111"},
                "to": [{"phone_number": "+15551230009", "status": "delivered"}],
            },
        }
    }
    result, status = txwh.handle_messaging_webhook(payload)
    assert status == 200
    row = db.get_sms_message_by_provider_id("telnyx-mid-1")
    assert row["status"] == "delivered"


def test_webhook_signature_required_in_production(app_client, monkeypatch):
    monkeypatch.setattr(config, "TELNYX_PUBLIC_KEY", base64.b64encode(b"0" * 32).decode())
    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    res = app_client.post("/webhooks/telnyx/messaging", json={"data": {"event_type": "message.received"}})
    assert res.status_code == 401


def test_webhook_duplicate_event(two_users, monkeypatch):
    u1, _ = two_users
    monkeypatch.setattr(config, "TELNYX_PHONE_NUMBER", "+15550003333")
    tdb.upsert_tenant_sender(
        u1,
        sender_number="+15550003333",
        sms_provider="telnyx",
        sms_enabled=True,
        registration_status="verified",
    )
    evt = f"evt-dup-{uuid.uuid4().hex[:8]}"
    payload = {
        "data": {
            "event_type": "message.received",
            "id": evt,
            "payload": {
                "id": f"msg-{uuid.uuid4().hex[:8]}",
                "text": "Hi",
                "from": {"phone_number": "+15551231111"},
                "to": [{"phone_number": "+15550003333"}],
            },
        }
    }
    r1, s1 = txwh.handle_messaging_webhook(payload)
    r2, s2 = txwh.handle_messaging_webhook(payload)
    assert s1 == 200 and s2 == 200
    assert r2.get("duplicate") is True


def test_migration_013_webhook_table(two_users):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sms_webhook_events'"
        ).fetchone()
        assert row is not None


def test_tenant_isolation_inbound(two_users, monkeypatch):
    u1, u2 = two_users
    tdb.upsert_tenant_sender(
        u1,
        sender_number="+15550004444",
        sms_provider="telnyx",
        sms_enabled=True,
        registration_status="verified",
    )
    tdb.upsert_tenant_sender(
        u2,
        sender_number="+15550005555",
        sms_provider="telnyx",
        sms_enabled=True,
        registration_status="verified",
    )
    contact = _unique_e164("700")
    payload = {
        "data": {
            "event_type": "message.received",
            "id": f"evt-iso-{uuid.uuid4().hex[:8]}",
            "payload": {
                "id": f"msg-iso-{uuid.uuid4().hex[:8]}",
                "text": "Hello",
                "from": {"phone_number": contact},
                "to": [{"phone_number": "+15550004444"}],
            },
        }
    }
    result, status = txwh.handle_messaging_webhook(payload)
    assert status == 200
    assert db.get_lead_by_phone(u1, contact) is not None
    assert db.get_lead_by_phone(u2, contact) is None
