"""SimpleTexting multi-tenant SMS: provider, authz, webhooks, campaigns, queue."""

from __future__ import annotations

import uuid
from io import BytesIO
from unittest.mock import MagicMock, patch

import config
import db
import tenant_sms_db as tdb
from sms_authorization import can_send_sms, record_one_to_one_attestation, require_tenant_sender
from sms_providers.factory import get_sms_provider
from sms_providers.simpletexting import SimpleTextingSMSProvider
import simpletexting_webhooks as stwh
from workers.sms_campaign_worker import process_one


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id


def _unique_e164(prefix="555"):
    n = int(uuid.uuid4().hex[:8], 16) % 10_000_000
    return f"+1{prefix}{n:07d}"[:12]


def _enable_st(user_id, number=None):
    number = number or _unique_e164()
    tdb.upsert_tenant_sender(
        user_id,
        sender_number=number,
        sms_provider="simpletexting",
        sms_enabled=True,
        registration_status="verified",
    )
    tdb.accept_sms_terms(user_id, user_id)
    return number


def _lead(user_id, phone="+15551239901", name="Campaign Lead"):
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


def test_factory_selects_simpletexting(monkeypatch):
    monkeypatch.setattr(config, "SMS_PROVIDER", "simpletexting")
    monkeypatch.setattr(config, "SIMPLETEXTING_API_TOKEN", "tok")
    provider = get_sms_provider()
    assert isinstance(provider, SimpleTextingSMSProvider)
    assert provider.is_configured()


def test_simpletexting_send_requires_account_phone(monkeypatch):
    monkeypatch.setattr(config, "SIMPLETEXTING_API_TOKEN", "tok")
    provider = SimpleTextingSMSProvider()
    with patch.object(provider, "_request") as req:
        req.return_value = ({"id": "m1", "status": "submitted"}, 200)
        result = provider.send_sms("+15551230000", "Hi", from_number="+15550001111")
        assert result["provider_message_id"] == "m1"
        body = req.call_args[0][2]
        assert body["accountPhone"] == "15550001111"
        assert body["contactPhone"] == "15551230000"


def test_simpletexting_send_blocks_without_from(monkeypatch):
    monkeypatch.setattr(config, "SIMPLETEXTING_API_TOKEN", "tok")
    provider = SimpleTextingSMSProvider()
    try:
        provider.send_sms("+15551230000", "Hi", from_number=None)
        assert False, "expected error"
    except Exception as exc:
        assert "sender" in str(exc).lower() or "activated" in str(exc).lower()


def test_require_tenant_sender_never_uses_global_st_number(two_users, monkeypatch):
    u1, _ = two_users
    monkeypatch.setattr(config, "SMS_PROVIDER", "simpletexting")
    monkeypatch.setattr(config, "SIMPLETEXTING_API_TOKEN", "tok")
    monkeypatch.setattr(config, "SIMPLETEXTING_PHONE_NUMBER", "+15559998888")
    sender, err = require_tenant_sender(u1)
    assert sender is None
    assert err


def test_can_send_requires_sender_and_attestation(two_users, monkeypatch):
    u1, _ = two_users
    monkeypatch.setattr(config, "SMS_PROVIDER", "simpletexting")
    monkeypatch.setattr(config, "SIMPLETEXTING_API_TOKEN", "tok")
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 0)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 0)
    lead_id, _ = _lead(u1)
    ok, msg = can_send_sms(u1, lead_id, message_body="Hello")
    assert ok is False
    assert "activated" in msg.lower() or "sender" in msg.lower()

    _enable_st(u1)
    ok2, msg2 = can_send_sms(u1, lead_id, message_body="Hello")
    assert ok2 is False
    assert "certif" in msg2.lower() or "consent" in msg2.lower()

    att_id, err = record_one_to_one_attestation(
        u1, lead_id, message_body="Hello", source_page="test"
    )
    assert err is None and att_id
    ok3, _ = can_send_sms(u1, lead_id, message_body="Hello")
    assert ok3 is True


def test_webhook_inbound_routes_by_account_phone(two_users, monkeypatch):
    u1, u2 = two_users
    n1 = _enable_st(u1)
    _enable_st(u2)
    digits = "".join(c for c in n1 if c.isdigit())
    contact = _unique_e164("512")
    payload = {
        "reportId": "r-in-1",
        "type": "INCOMING_MESSAGE",
        "values": {
            "messageId": f"st-msg-{uuid.uuid4().hex[:8]}",
            "text": "Interested",
            "accountPhone": digits,
            "contactPhone": "".join(c for c in contact if c.isdigit()),
        },
    }
    result, status = stwh.handle_inbound(payload)
    assert status == 200
    assert result["ok"] is True
    lead = db.get_lead(result["lead_id"], u1)
    assert lead is not None
    assert db.get_lead_by_phone(u2, contact) is None


def test_webhook_inbound_unknown_destination(two_users):
    payload = {
        "type": "INCOMING_MESSAGE",
        "values": {
            "messageId": f"st-msg-{uuid.uuid4().hex[:8]}",
            "text": "Hi",
            "accountPhone": "15559990000",
            "contactPhone": "15551234445",
        },
    }
    result, status = stwh.handle_inbound(payload)
    assert status == 404
    assert result["ok"] is False


def test_webhook_stop_suppresses(two_users, monkeypatch):
    u1, _ = two_users
    n1 = _enable_st(u1)
    contact = _unique_e164("513")
    lead_id, _ = _lead(u1, contact)
    payload = {
        "reportId": "r-stop",
        "type": "INCOMING_MESSAGE",
        "values": {
            "messageId": f"st-stop-{uuid.uuid4().hex[:8]}",
            "text": "STOP",
            "accountPhone": "".join(c for c in n1 if c.isdigit()),
            "contactPhone": "".join(c for c in contact if c.isdigit()),
        },
    }
    result, status = stwh.handle_inbound(payload)
    assert status == 200
    lead = db.get_lead(lead_id, u1)
    assert lead["opt_out_status"] == "opted_out" or lead["sms_consent_status"] == "opted_out"
    assert tdb.is_suppressed(u1, contact)


def test_unsubscribe_fallback_applies_to_all_owners(two_users):
    u1, u2 = two_users
    contact = _unique_e164("514")
    _lead(u1, contact)
    _lead(u2, contact)
    result, status = stwh.handle_unsubscribe(
        {
            "type": "UNSUBSCRIBE_REPORT",
            "values": {"phone": "".join(c for c in contact if c.isdigit())},
        }
    )
    assert status == 200
    assert result["tenants"] >= 2
    assert tdb.is_suppressed(u1, contact)
    assert tdb.is_suppressed(u2, contact)


def test_webhook_auth_token(app_client, monkeypatch):
    monkeypatch.setattr(config, "SIMPLETEXTING_WEBHOOK_SECRET", "secret-token")
    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    bad = app_client.post("/webhooks/simpletexting/inbound", json={})
    assert bad.status_code == 401
    good = app_client.post(
        "/webhooks/simpletexting/inbound?token=secret-token",
        json={
            "type": "INCOMING_MESSAGE",
            "values": {
                "messageId": "auth-1",
                "text": "x",
                "accountPhone": "19999999999",
                "contactPhone": "15551112222",
            },
        },
    )
    # Unknown destination → 404 after auth
    assert good.status_code == 404


def test_campaign_import_certify_and_queue(two_users, monkeypatch):
    u1, _ = two_users
    monkeypatch.setattr(config, "SMS_PROVIDER", "simpletexting")
    monkeypatch.setattr(config, "SIMPLETEXTING_API_TOKEN", "tok")
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 0)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 0)
    _enable_st(u1)
    cid = tdb.create_campaign(u1, "Pilot blast", campaign_purpose="open_house")
    tdb.update_campaign(
        cid,
        u1,
        message_template="Hi [first_name], reply STOP to opt out.",
        content_fingerprint="fp1",
    )
    sender = tdb.get_active_sender(u1)
    p1, p2 = _unique_e164("701"), _unique_e164("702")
    tdb.replace_campaign_recipients(
        cid,
        u1,
        [
            {
                "lead_id": _lead(u1, p1, "A")[0],
                "phone_number": p1,
                "merge_fields": {"first_name": "A"},
                "eligible": True,
                "exclusion_reason": None,
            },
            {
                "lead_id": _lead(u1, p2, "B")[0],
                "phone_number": p2,
                "merge_fields": {"first_name": "B"},
                "eligible": True,
                "exclusion_reason": None,
            },
        ],
    )
    camp = tdb.get_campaign(cid, u1)
    att = tdb.create_campaign_attestation(
        u1,
        u1,
        cid,
        eligible_count=2,
        excluded_count=0,
        campaign_purpose="open_house",
        message_body=camp["message_template"],
        audience_snapshot_id=camp["audience_snapshot_id"],
        provider="simpletexting",
    )
    tdb.update_campaign(cid, u1, attestation_id=att, status="processing")
    created = tdb.create_jobs_for_campaign(cid, u1)
    assert created == 2

    mock_provider = MagicMock()
    mock_provider.send_sms.return_value = {
        "provider_message_id": "st-camp-1",
        "status": "submitted",
    }
    with patch("workers.sms_campaign_worker.get_sms_provider", return_value=mock_provider):
        assert process_one("worker-test") is True
        mock_provider.send_sms.assert_called()
        call_kwargs = mock_provider.send_sms.call_args.kwargs
        assert call_kwargs.get("from_number") == sender["sender_number"]


def test_tenant_isolation_on_jobs(two_users):
    u1, u2 = two_users
    _enable_st(u1)
    _enable_st(u2)
    c1 = tdb.create_campaign(u1, "T1")
    c2 = tdb.create_campaign(u2, "T2")
    assert tdb.get_campaign(c1, u2) is None
    assert tdb.get_campaign(c2, u1) is None


def test_tracking_redirect(app_client, two_users):
    u1, _ = two_users
    token = tdb.create_tracking_link(u1, "https://example.com/listing")
    resp = app_client.get(f"/r/{token}")
    assert resp.status_code in {302, 301}
    assert "example.com" in (resp.headers.get("Location") or "")


def test_csv_import_route(app_client, two_users, monkeypatch):
    u1, _ = two_users
    monkeypatch.setattr(config, "SMS_PROVIDER", "simpletexting")
    monkeypatch.setattr(config, "SIMPLETEXTING_API_TOKEN", "tok")
    _enable_st(u1)
    _login(app_client, u1)
    cid = tdb.create_campaign(u1, "Import camp")
    csv_bytes = b"first_name,last_name,phone\nAda,Lovelace,+15551238801\n"
    resp = app_client.post(
        f"/crm/sms-campaigns/{cid}",
        data={
            "action": "import_audience",
            "audience_file": (BytesIO(csv_bytes), "audience.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    recipients = tdb.list_campaign_recipients(cid, u1)
    assert len(recipients) == 1
    assert recipients[0]["phone_number"] == "+15551238801"


def test_migration_012_tables_exist(two_users):
    with db.get_db() as conn:
        for table in (
            "tenant_sms_senders",
            "sms_subscriber_attestations",
            "sms_campaign_attestations",
            "sms_suppression_list",
            "sms_campaigns",
            "sms_campaign_recipients",
            "sms_campaign_jobs",
            "sms_link_clicks",
            "sms_terms_acceptances",
            "sms_audit_events",
        ):
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            assert row is not None, table
