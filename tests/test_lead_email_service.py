from cryptography.fernet import Fernet

import config
import crm_db
import db
import email_marketing_db
import lead_email_service
from email_campaign_providers.sendgrid import SendGridEmailCampaignProvider


def _enable_encryption(monkeypatch):
    monkeypatch.setattr(
        config,
        "INTEGRATION_CREDENTIAL_ENCRYPTION_KEY",
        Fernet.generate_key().decode(),
    )


def _lead(user_id, email="lead@example.com"):
    lead_id = db.create_lead_record(
        user_id,
        "+13035550101",
        name="Email Lead",
        status="new",
        source="manual",
    )
    with db.get_db() as conn:
        conn.execute(
            "UPDATE leads SET email = ? WHERE id = ? AND user_id = ?",
            (email, lead_id, user_id),
        )
    return lead_id


def _connect_sendgrid(user_id, monkeypatch):
    _enable_encryption(monkeypatch)
    email_marketing_db.connect(user_id, "SG.tenant-secret")
    email_marketing_db.save_settings(
        user_id,
        sender_id=123,
        sender_name="Agent",
        sender_email="agent@example.com",
        default_list_ids=["list-abc"],
        suppression_group_id=456,
        suppression_group_name="Real Estate Marketing",
    )


def test_sendgrid_direct_send_uses_mail_send(monkeypatch):
    captured = {}

    def fake_request(self, method, path, *, body=None):
        captured.update({"method": method, "path": path, "body": body})
        return {}

    monkeypatch.setattr(SendGridEmailCampaignProvider, "_request", fake_request)
    provider = SendGridEmailCampaignProvider("SG.test")
    result = provider.send_email(
        to_email="lead@example.com",
        subject="Follow up",
        html_content="<p>Hello</p>",
        plain_content="Hello",
        sender_name="Agent",
        sender_email="agent@example.com",
    )

    assert captured["method"] == "POST"
    assert captured["path"] == "/mail/send"
    assert captured["body"]["personalizations"][0]["to"][0]["email"] == "lead@example.com"
    assert captured["body"]["from"]["email"] == "agent@example.com"
    assert result["provider_status"] == "sent"


def test_lead_email_service_sends_and_logs_timeline(two_users, monkeypatch):
    user_id, _ = two_users
    lead_id = _lead(user_id)
    _connect_sendgrid(user_id, monkeypatch)
    sent = {}

    def fake_send(self, **kwargs):
        sent.update(kwargs)
        return {"provider_message_id": "msg-1", "provider_status": "sent"}

    monkeypatch.setattr(SendGridEmailCampaignProvider, "send_email", fake_send)

    result, error = lead_email_service.send_lead_email(
        user_id,
        lead_id,
        subject="Checking in",
        body="Hello there",
    )

    assert error is None
    assert result["ok"] is True
    assert result["to_email"] == "lead@example.com"
    assert sent["sender_email"] == "agent@example.com"
    activities = crm_db.list_lead_activities(user_id, lead_id)
    assert activities[0]["event_type"] == "email_sent"


def test_lead_email_service_requires_valid_lead_email(two_users):
    user_id, _ = two_users
    lead_id = _lead(user_id, email="")

    result, error = lead_email_service.send_lead_email(
        user_id,
        lead_id,
        subject="Checking in",
        body="Hello there",
    )

    assert result is None
    assert "valid email" in error
