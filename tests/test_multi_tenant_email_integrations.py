"""Generic customer-owned email integrations and provider contracts."""

import base64
import importlib
import sqlite3
from email import message_from_bytes
from unittest.mock import patch

from cryptography.fernet import Fernet

import crm_db
import config
import db
import email_marketing_db as marketing_db
import lead_email_service
import listing_email_campaigns
from email_campaign_providers import constant_contact, gmail, mailchimp, microsoft
from email_campaign_providers.gmail import GmailEmailProvider
from email_campaign_providers.mailchimp import MailchimpEmailProvider
from email_campaign_providers.microsoft import MicrosoftEmailProvider
from email_campaign_providers.sendgrid import SendGridEmailCampaignProvider
from email_campaign_providers.registry import provider_catalog


def _login(client, user_id):
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["session_version"] = 1


def _enable_encryption(monkeypatch):
    monkeypatch.setattr(
        config,
        "INTEGRATION_CREDENTIAL_ENCRYPTION_KEY",
        Fernet.generate_key().decode(),
    )


def _add_sendgrid(user_id, key, account_id, *, is_default=False):
    item = marketing_db.upsert_integration(
        user_id,
        "sendgrid",
        integration_kind="marketing",
        auth_type="api_key",
        external_account_id=account_id,
        external_account_email=f"{account_id}@example.com",
        display_name=account_id,
        credential=key,
        sender_id=123,
        sender_name="Agent",
        sender_email="agent@example.com",
        settings={
            "default_list_ids": [],
            "suppression_group_id": 456,
        },
    )
    if is_default:
        marketing_db.set_default_integration(user_id, item["id"])
    return item


def test_migration_backfills_sendgrid_ciphertext_without_reencrypting():
    migration = importlib.import_module(
        "migrations.versions.021_email_integrations"
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE email_marketing_connections (
            id INTEGER PRIMARY KEY, user_id INTEGER, provider TEXT,
            api_key_encrypted TEXT, status TEXT, sender_id INTEGER,
            sender_name TEXT, sender_email TEXT, default_list_ids_json TEXT,
            suppression_group_id INTEGER, suppression_group_name TEXT,
            last_tested_at TEXT, last_error_summary TEXT,
            created_at TEXT, updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE listing_email_campaigns (
            id INTEGER PRIMARY KEY, user_id INTEGER, listing_generation_id INTEGER
        )
        """
    )
    conn.execute(
        """
        INSERT INTO email_marketing_connections VALUES (
            7, 10, 'sendgrid', 'unchanged-ciphertext', 'connected', 12,
            'Agent', 'agent@example.com', '["list-1"]', 44, 'Marketing',
            NULL, NULL, '2026-01-01T00:00:00+00:00',
            '2026-01-01T00:00:00+00:00'
        )
        """
    )
    migration.upgrade_sqlite(conn)
    row = dict(conn.execute("SELECT * FROM email_integrations").fetchone())
    assert row["credential_encrypted"] == "unchanged-ciphertext"
    assert row["external_account_id"] == "legacy-sendgrid-7"
    assert row["is_default"] == 1


def test_credentials_are_encrypted_and_public_rows_are_safe(two_users, monkeypatch):
    user_id, other_id = two_users
    _enable_encryption(monkeypatch)
    item = _add_sendgrid(user_id, "SG.customer-only", "customer")

    public = marketing_db.get_integration(user_id, item["id"])
    assert "credential" not in public
    assert "credential_encrypted" not in public
    assert "SG.customer-only" not in str(public)
    assert marketing_db.get_integration(other_id, item["id"]) is None
    assert (
        marketing_db.get_integration_credentials(user_id, item["id"])[
            "credential"
        ]
        == "SG.customer-only"
    )


def test_default_selection_and_disconnect_are_tenant_scoped(two_users, monkeypatch):
    user_id, other_id = two_users
    _enable_encryption(monkeypatch)
    first = _add_sendgrid(user_id, "SG.first", "first")
    second = _add_sendgrid(user_id, "SG.second", "second")
    foreign = _add_sendgrid(other_id, "SG.foreign", "foreign")

    assert marketing_db.set_default_integration(user_id, second["id"])
    assert marketing_db.get_default_integration(user_id)["id"] == second["id"]
    assert not marketing_db.set_default_integration(user_id, foreign["id"])
    assert not marketing_db.disconnect_integration(user_id, foreign["id"])
    assert marketing_db.get_integration(other_id, foreign["id"])["status"] == "connected"
    assert marketing_db.disconnect_integration(user_id, second["id"])
    assert marketing_db.get_default_integration(user_id)["id"] == first["id"]


def test_oauth_state_is_single_use_and_cannot_cross_tenants(two_users):
    user_id, other_id = two_users
    state = marketing_db.create_oauth_state(
        user_id,
        "gmail",
        redirect_uri="https://app.example/callback",
        code_verifier="pkce-secret",
    )
    assert marketing_db.consume_oauth_state(state, "gmail", other_id) is None
    consumed = marketing_db.consume_oauth_state(state, "gmail", user_id)
    assert consumed["code_verifier"] == "pkce-secret"
    assert marketing_db.consume_oauth_state(state, "gmail", user_id) is None


def test_oauth_token_rotation_preserves_encryption_boundary(two_users, monkeypatch):
    user_id, _ = two_users
    _enable_encryption(monkeypatch)
    item = marketing_db.upsert_integration(
        user_id,
        "gmail",
        integration_kind="personal",
        auth_type="oauth",
        external_account_id="google-user-1",
        external_account_email="agent@example.com",
        access_token="old-access",
        refresh_token="old-refresh",
        token_expires_at="2026-01-01T00:00:00+00:00",
    )
    marketing_db.update_integration_tokens(
        user_id,
        item["id"],
        access_token="new-access",
        refresh_token="rotated-refresh",
        token_expires_at="2030-01-01T00:00:00+00:00",
    )
    public = marketing_db.get_integration(user_id, item["id"])
    assert "access_token" not in str(public)
    assert "refresh_token" not in str(public)
    internal = marketing_db.get_integration_credentials(user_id, item["id"])
    assert internal["access_token"] == "new-access"
    assert internal["refresh_token"] == "rotated-refresh"


def test_provider_catalog_distinguishes_personal_and_marketing(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_EMAIL_CLIENT_ID", "google-id")
    monkeypatch.setattr(config, "GOOGLE_EMAIL_CLIENT_SECRET", "google-secret")
    catalog = {item["provider"]: item for item in provider_catalog()}
    assert catalog["gmail"]["integration_kind"] == "personal"
    assert catalog["gmail"]["supports_recipientless_draft"] is True
    assert catalog["gmail"]["supports_direct_send"] is True
    assert catalog["gmail"]["ready"] is True
    assert catalog["microsoft"]["integration_kind"] == "personal"
    assert catalog["microsoft"]["supports_direct_send"] is True
    assert catalog["sendgrid"]["integration_kind"] == "marketing"
    assert catalog["sendgrid"]["supports_direct_send"] is True
    assert catalog["mailchimp"]["requires_list"] is True


def test_gmail_creates_recipientless_rfc_mime_draft(monkeypatch):
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(kwargs["json_body"])
        return {"id": "gmail-draft-1"}

    monkeypatch.setattr(gmail, "request_json", fake_request)
    provider = GmailEmailProvider(
        client_id="id", client_secret="secret", access_token="token"
    )
    result = provider.create_draft(
        subject="Listing subject",
        html_content="<p>Listing body</p>",
        plain_content="Listing body",
        sender_email="agent@example.com",
    )
    raw = captured["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    message = message_from_bytes(decoded)
    assert message["Subject"] == "Listing subject"
    assert message["To"] is None
    assert result["has_recipients"] is False


def test_microsoft_requests_mail_send_and_creates_no_draft_recipients(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {"id": "outlook-draft-1"}

    monkeypatch.setattr(microsoft, "request_json", fake_request)
    provider = MicrosoftEmailProvider(
        client_id="id", client_secret="secret", access_token="token"
    )
    auth_url = provider.get_authorization_url(
        redirect_uri="https://app.example/callback", state="state"
    )
    assert "Mail.ReadWrite" in auth_url
    assert "Mail.Send" in auth_url
    result = provider.create_draft(
        subject="Subject",
        html_content="<p>Body</p>",
        plain_content="Body",
    )
    assert calls[-1][2]["json_body"]["toRecipients"] == []
    assert result["has_recipients"] is False


def test_gmail_sends_rfc_mime_to_lead(monkeypatch):
    captured = {}

    def fake_request(method, url, **kwargs):
        captured["url"] = url
        captured.update(kwargs["json_body"])
        return {"id": "gmail-message-1"}

    monkeypatch.setattr(gmail, "request_json", fake_request)
    provider = GmailEmailProvider(
        client_id="id", client_secret="secret", access_token="token"
    )
    result = provider.send_email(
        to_email="lead@example.com",
        subject="Follow up",
        html_content="<p>Hello</p>",
        plain_content="Hello",
        sender_email="agent@example.com",
    )
    raw = captured["raw"]
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    message = message_from_bytes(decoded)
    assert captured["url"].endswith("/messages/send")
    assert message["To"] == "lead@example.com"
    assert message["Subject"] == "Follow up"
    assert result["provider_message_id"] == "gmail-message-1"


def test_microsoft_sends_to_lead_and_saves_sent_item(monkeypatch):
    captured = {}

    def fake_request(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["body"] = kwargs["json_body"]
        return {}

    monkeypatch.setattr(microsoft, "request_json", fake_request)
    provider = MicrosoftEmailProvider(
        client_id="id", client_secret="secret", access_token="token"
    )
    result = provider.send_email(
        to_email="lead@example.com",
        subject="Follow up",
        html_content="<p>Hello</p>",
        plain_content="Hello",
    )
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/me/sendMail")
    assert captured["body"]["message"]["toRecipients"][0]["emailAddress"]["address"] == "lead@example.com"
    assert captured["body"]["saveToSentItems"] is True
    assert result["provider_status"] == "sent"


def test_sendgrid_direct_send_uses_mail_send(monkeypatch):
    captured = {}

    def fake_request(self, method, path, *, body=None, action="draft"):
        captured.update({"method": method, "path": path, "body": body, "action": action})
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
    assert captured["action"] == "send"
    assert captured["body"]["personalizations"][0]["to"][0]["email"] == "lead@example.com"
    assert captured["body"]["from"]["email"] == "agent@example.com"
    assert result["provider_status"] == "sent"


def test_lead_email_service_sends_through_default_integration(two_users, monkeypatch):
    user_id, _ = two_users
    _enable_encryption(monkeypatch)
    db.update_business_profile(
        user_id,
        agent_name="Ada Agent",
        phone_number="(303) 555-0199",
        brokerage_name="Ada Realty",
    )
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
            ("lead@example.com", lead_id, user_id),
        )
    integration = marketing_db.upsert_integration(
        user_id,
        "gmail",
        integration_kind="personal",
        auth_type="oauth",
        external_account_id="google-user-1",
        external_account_email="agent@example.com",
        access_token="access",
        refresh_token="refresh",
        token_expires_at="2030-01-01T00:00:00+00:00",
    )
    marketing_db.set_default_integration(user_id, integration["id"])

    sent = {}

    class FakeProvider:
        def send_email(self, **kwargs):
            sent.update(kwargs)
            return {"provider_message_id": "msg-1", "provider_status": "sent"}

    monkeypatch.setattr(lead_email_service, "get_provider", lambda *a, **k: FakeProvider())

    result, error = lead_email_service.send_lead_email(
        user_id,
        lead_id,
        subject="Checking in",
        body="Hello there",
    )

    assert error is None
    assert result["ok"] is True
    assert sent["to_email"] == "lead@example.com"
    assert sent["sender_email"] == "agent@example.com"
    assert sent["plain_content"].startswith("Hi Email,")
    assert sent["plain_content"].endswith("Warm regards,\nAda Agent\n(303) 555-0199\nAda Realty")
    assert "Hi Email," in sent["html_content"]
    assert "Ada Realty" in sent["html_content"]
    activities = crm_db.list_lead_activities(user_id, lead_id)
    assert activities[0]["event_type"] == "email_sent"


def test_lead_email_service_requires_lead_email(two_users):
    user_id, _ = two_users
    lead_id = db.create_lead_record(
        user_id,
        "+13035550101",
        name="No Email",
        status="new",
        source="manual",
    )

    result, error = lead_email_service.send_lead_email(
        user_id,
        lead_id,
        subject="Checking in",
        body="Hello there",
    )

    assert result is None
    assert "valid email" in error


def test_mailchimp_creates_campaign_then_content_without_sending(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs.get("json_body")))
        if method == "POST":
            return {"id": "campaign-1", "status": "save"}
        return {}

    monkeypatch.setattr(mailchimp, "request_json", fake_request)
    provider = MailchimpEmailProvider(
        client_id="id",
        client_secret="secret",
        access_token="token",
        api_endpoint="https://us1.api.mailchimp.com",
    )
    result = provider.create_draft(
        name="Campaign",
        subject="Subject",
        html_content="<p>Body</p>",
        plain_content="Body",
        sender_name="Agent",
        sender_email="agent@example.com",
        list_ids=["audience-1"],
    )
    assert [call[0] for call in calls] == ["POST", "PUT"]
    assert calls[0][1].endswith("/campaigns")
    assert calls[1][1].endswith("/campaigns/campaign-1/content")
    assert all("send" not in call[1] and "schedule" not in call[1] for call in calls)
    assert result["provider_status"] == "save"


def test_constant_contact_refresh_returns_rotated_refresh_token(monkeypatch):
    monkeypatch.setattr(
        constant_contact,
        "request_json",
        lambda *args, **kwargs: {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 7200,
        },
    )
    provider = constant_contact.ConstantContactEmailProvider(
        client_id="id", client_secret="secret", access_token="old"
    )
    result = provider.refresh_access_token("old-refresh")
    assert result["refresh_token"] == "new-refresh"
    assert result["token_expires_at"]


def test_options_endpoint_returns_only_authenticated_users_accounts(
    app_client, two_users, monkeypatch
):
    user_id, other_id = two_users
    _enable_encryption(monkeypatch)
    own = _add_sendgrid(user_id, "SG.own", "own")
    _add_sendgrid(other_id, "SG.foreign", "foreign")
    _login(app_client, user_id)
    response = app_client.get("/integrations/email-marketing/options")
    assert response.status_code == 200
    assert [item["id"] for item in response.get_json()["items"]] == [own["id"]]
    assert "SG.own" not in response.get_data(as_text=True)


def test_settings_ui_explains_provider_types_and_never_renders_secret(
    app_client, two_users, monkeypatch
):
    user_id, _ = two_users
    _enable_encryption(monkeypatch)
    item = _add_sendgrid(user_id, "SG.hidden-value", "settings")
    _login(app_client, user_id)

    class FakeProvider:
        def test_connection(self):
            return {
                "senders": [
                    {"id": 123, "name": "Agent", "email": "agent@example.com"}
                ],
                "lists": [],
                "suppression_groups": [
                    {"id": 456, "name": "Marketing"}
                ],
            }

    with patch(
        "email_marketing_routes._provider_for_integration",
        return_value=(
            marketing_db.get_integration_credentials(user_id, item["id"]),
            FakeProvider(),
        ),
    ):
        response = app_client.get(
            f"/integrations/email-marketing?integration_id={item['id']}"
        )
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Personal / low-volume email" in html
    assert "Marketing campaigns / bulk email" in html
    assert "recipientless" in html
    assert "SG.hidden-value" not in html


def test_export_override_and_idempotency_are_scoped_per_integration(
    two_users, monkeypatch
):
    user_id, _ = two_users
    _enable_encryption(monkeypatch)
    first = _add_sendgrid(user_id, "SG.first", "first", is_default=True)
    second = _add_sendgrid(user_id, "SG.second", "second")
    generation = listing_email_campaigns.listing_db.create_generation(
        user_id,
        display_address="10 Provider Choice Lane",
        output_snapshot={"email": "Subject: Choice\n\nBody"},
    )
    created_with_keys = []

    class FakeProvider:
        def __init__(self, key):
            self.key = key

        def create_draft(self, **kwargs):
            created_with_keys.append(self.key)
            return {
                "provider_campaign_id": f"draft-{len(created_with_keys)}",
                "provider_status": "draft",
                "has_recipients": False,
            }

    def fake_provider(*args, **kwargs):
        return FakeProvider(kwargs["api_key"])

    monkeypatch.setattr(listing_email_campaigns, "get_provider", fake_provider)
    one = listing_email_campaigns.export_listing_email(
        user_id, generation["id"], integration_id=first["id"]
    )
    duplicate = listing_email_campaigns.export_listing_email(
        user_id, generation["id"], integration_id=first["id"]
    )
    two = listing_email_campaigns.export_listing_email(
        user_id, generation["id"], integration_id=second["id"]
    )
    assert created_with_keys == ["SG.first", "SG.second"]
    assert duplicate["already_exists"] is True
    assert one["email_integration_id"] != two["email_integration_id"]


def test_customer_export_never_falls_back_to_platform_sendgrid(
    two_users, monkeypatch
):
    user_id, _ = two_users
    monkeypatch.setattr(config, "SENDGRID_API_KEY", "SG.platform-system-key")
    generation = listing_email_campaigns.listing_db.create_generation(
        user_id,
        display_address="11 Separation Street",
        output_snapshot={"email": "Subject: Separate\n\nBody"},
    )
    try:
        listing_email_campaigns.export_listing_email(user_id, generation["id"])
    except ValueError as exc:
        assert "Connect an email account" in str(exc)
    else:
        raise AssertionError("Export must not use platform SENDGRID_API_KEY")
