"""Listing Email -> tenant-owned SendGrid Single Send draft workflow."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from cryptography.fernet import Fernet

import config
import db
import email_marketing_db as marketing_db
import listing_generations_db as listing_db
import listing_email_campaigns
from email_campaign_providers.base import EmailCampaignProviderError
from email_campaign_providers.sendgrid import SendGridEmailCampaignProvider
from listing_email_content import parse_listing_email, render_listing_email_html


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["session_version"] = 1


def _enable_encryption(monkeypatch):
    monkeypatch.setattr(
        config,
        "INTEGRATION_CREDENTIAL_ENCRYPTION_KEY",
        Fernet.generate_key().decode(),
    )


def _generation(user_id, address="123 Campaign Way"):
    return listing_db.create_generation(
        user_id,
        display_address=address,
        output_snapshot={
            "listing": "Listing copy",
            "social": "Social copy",
            "email": (
                "Subject: Exact saved subject\n\n"
                "Hello prospect,\n\nThis exact saved body must be used."
            ),
        },
    )


def _connect(user_id, monkeypatch, *, list_ids=None):
    _enable_encryption(monkeypatch)
    marketing_db.connect(user_id, "SG.tenant-secret")
    marketing_db.save_settings(
        user_id,
        sender_id=123,
        sender_name="Agent",
        sender_email="agent@example.com",
        default_list_ids=list_ids if list_ids is not None else ["list-abc"],
        suppression_group_id=456,
        suppression_group_name="Real Estate Marketing",
    )


class _FakeProvider:
    def __init__(self):
        self.calls = []

    def create_draft(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "provider_campaign_id": "single-send-1",
            "provider_status": "draft",
            "has_recipients": bool(kwargs.get("list_ids")),
        }


def test_listing_email_button_is_rendered(app_client, two_users):
    user_id, _ = two_users
    _login(app_client, user_id)
    db.update_user_subscription(user_id, "active")
    response = app_client.get("/app")
    assert b"Add to Email Campaign" in response.data


def test_parser_uses_exact_saved_email_and_html_escapes_content():
    raw = "Subject: Saved subject\n\nHello <script>alert(1)</script>\n\nCall today."
    parsed = parse_listing_email(raw, "1 <Main> St")
    assert parsed == {
        "subject": "Saved subject",
        "body": "Hello <script>alert(1)</script>\n\nCall today.",
    }
    rendered = render_listing_email_html(
        subject=parsed["subject"],
        body=parsed["body"],
        property_address="1 <Main> St",
    )
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "1 &lt;Main&gt; St" in rendered


def test_export_uses_tenant_settings_and_persists_draft(two_users, monkeypatch):
    user_id, _ = two_users
    generation = _generation(user_id)
    db.update_business_profile(
        user_id,
        agent_name="Khristina Morrissey",
        phone_number="720-289-1700",
        brokerage_name="Home Real Estate",
    )
    _connect(user_id, monkeypatch)
    provider = _FakeProvider()
    monkeypatch.setattr(
        listing_email_campaigns,
        "get_provider",
        lambda *args, **kwargs: provider,
    )

    result = listing_email_campaigns.export_listing_email(
        user_id, generation["id"]
    )
    assert result["status"] == "draft_created"
    assert result["provider_campaign_id"] == "single-send-1"
    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call["subject"] == "Exact saved subject"
    assert call["plain_content"] == (
        "Hello prospect,\n\nThis exact saved body must be used.\n\n"
        "Warm regards,\nKhristina Morrissey\n720-289-1700\nHome Real Estate"
    )
    assert "Warm regards," in call["html_content"]
    assert "Khristina Morrissey" in call["html_content"]
    assert "720-289-1700" in call["html_content"]
    assert "Home Real Estate" in call["html_content"]
    assert call["sender_id"] == 123
    assert call["list_ids"] == ["list-abc"]
    assert call["suppression_group_id"] == 456
    stored = marketing_db.latest_for_generation(user_id, generation["id"])
    assert stored["provider_campaign_id"] == "single-send-1"
    assert stored["provider_status"] == "draft"


def test_duplicate_click_one_draft_explicit_another_creates_new(
    two_users, monkeypatch
):
    user_id, _ = two_users
    generation = _generation(user_id, "124 Campaign Way")
    _connect(user_id, monkeypatch)
    provider = _FakeProvider()
    monkeypatch.setattr(
        listing_email_campaigns,
        "get_provider",
        lambda *args, **kwargs: provider,
    )

    first = listing_email_campaigns.export_listing_email(
        user_id, generation["id"]
    )
    second = listing_email_campaigns.export_listing_email(
        user_id, generation["id"]
    )
    third = listing_email_campaigns.export_listing_email(
        user_id, generation["id"], create_another=True
    )

    assert first["status"] == second["status"] == third["status"] == "draft_created"
    assert second["already_exists"] is True
    assert len(provider.calls) == 2
    assert len(marketing_db.list_for_generation(user_id, generation["id"])) == 2


def test_failed_provider_call_creates_safe_failure_state(two_users, monkeypatch):
    user_id, _ = two_users
    generation = _generation(user_id, "125 Campaign Way")
    _connect(user_id, monkeypatch)

    class FailingProvider:
        def create_draft(self, **kwargs):
            raise EmailCampaignProviderError(
                "TopAI couldn't create the SendGrid draft. Try again.",
                error_code="provider_error",
            )

    monkeypatch.setattr(
        listing_email_campaigns,
        "get_provider",
        lambda *args, **kwargs: FailingProvider(),
    )
    result = listing_email_campaigns.export_listing_email(
        user_id, generation["id"]
    )
    assert result["status"] == "failed"
    assert "SG." not in str(result)
    stored = marketing_db.latest_for_generation(user_id, generation["id"])
    assert stored["error_code"] == "provider_error"


def test_tenant_cannot_export_another_tenants_listing(
    app_client, two_users, monkeypatch
):
    owner_id, other_id = two_users
    generation = _generation(owner_id, "126 Campaign Way")
    _connect(other_id, monkeypatch)
    _login(app_client, other_id)
    db.update_user_subscription(other_id, "active")
    response = app_client.post(
        f"/listings/{generation['id']}/email-campaigns", json={}
    )
    assert response.status_code == 404


def test_authenticated_button_route_creates_draft(
    app_client, two_users, monkeypatch
):
    user_id, _ = two_users
    generation = _generation(user_id, "126B Campaign Way")
    _connect(user_id, monkeypatch)
    provider = _FakeProvider()
    monkeypatch.setattr(
        listing_email_campaigns,
        "get_provider",
        lambda *args, **kwargs: provider,
    )
    _login(app_client, user_id)
    db.update_user_subscription(user_id, "active")

    response = app_client.post(
        f"/listings/{generation['id']}/email-campaigns", json={}
    )
    assert response.status_code == 200
    assert response.get_json()["status"] == "draft_created"
    assert len(provider.calls) == 1


def test_api_key_never_reaches_public_dict_or_settings_html(
    app_client, two_users, monkeypatch
):
    user_id, _ = two_users
    _connect(user_id, monkeypatch)
    public = marketing_db.get_connection(user_id)
    assert "api_key" not in public
    assert "api_key_encrypted" not in public
    assert "SG.tenant-secret" not in str(public)

    _login(app_client, user_id)
    db.update_user_subscription(user_id, "active")
    with patch(
        "email_marketing_routes._load_resources",
        return_value=(
            {
                "senders": [],
                "lists": [],
                "suppression_groups": [],
            },
            None,
        ),
    ):
        response = app_client.get("/integrations/email-marketing")
    assert b"SG.tenant-secret" not in response.data


def test_connect_validates_then_encrypts_tenant_api_key(
    app_client, two_users, monkeypatch
):
    user_id, _ = two_users
    _enable_encryption(monkeypatch)
    _login(app_client, user_id)
    db.update_user_subscription(user_id, "active")

    class ValidationProvider:
        def test_connection(self):
            return {
                "connected": True,
                "senders": [
                    {
                        "id": 123,
                        "name": "Agent",
                        "email": "agent@example.com",
                    }
                ],
                "lists": [],
                "suppression_groups": [
                    {
                        "id": 456,
                        "name": "Marketing",
                        "is_default": True,
                    }
                ],
            }

    with patch(
        "email_marketing_routes.get_provider",
        return_value=ValidationProvider(),
    ):
        response = app_client.post(
            "/integrations/email-marketing/connect",
            data={"api_key": "SG.connected-secret"},
        )
    assert response.status_code in (302, 303)
    credentials = marketing_db.get_credentials(user_id)
    assert credentials["api_key"] == "SG.connected-secret"
    assert credentials["sender_id"] == 123
    assert credentials["default_list_ids"] == []


def test_settings_reject_resources_outside_connected_account(
    app_client, two_users, monkeypatch
):
    user_id, _ = two_users
    _connect(user_id, monkeypatch)
    _login(app_client, user_id)
    db.update_user_subscription(user_id, "active")

    class ValidationProvider:
        def test_connection(self):
            return {
                "connected": True,
                "senders": [
                    {
                        "id": 123,
                        "name": "Agent",
                        "email": "agent@example.com",
                    }
                ],
                "lists": [{"id": "list-abc", "name": "Clients"}],
                "suppression_groups": [
                    {"id": 456, "name": "Marketing"}
                ],
            }

    with patch(
        "email_marketing_routes._provider_for_user",
        return_value=ValidationProvider(),
    ):
        response = app_client.post(
            "/integrations/email-marketing/settings",
            data={
                "sender_id": "123",
                "default_list_id": "another-tenant-list",
                "suppression_group_id": "456",
            },
        )
    assert response.status_code in (302, 303)
    assert marketing_db.get_connection(user_id)["default_list_ids"] == [
        "list-abc"
    ]


def test_no_list_never_becomes_all_contacts(two_users, monkeypatch):
    user_id, _ = two_users
    generation = _generation(user_id, "127 Campaign Way")
    _connect(user_id, monkeypatch, list_ids=[])
    provider = _FakeProvider()
    monkeypatch.setattr(
        listing_email_campaigns,
        "get_provider",
        lambda *args, **kwargs: provider,
    )
    result = listing_email_campaigns.export_listing_email(
        user_id, generation["id"]
    )
    assert result["status"] == "draft_created"
    assert result["has_recipients"] is False
    assert provider.calls[0]["list_ids"] == []
    assert "all" not in str(provider.calls[0]).lower()


def test_sendgrid_provider_calls_only_single_send_create_not_schedule(monkeypatch):
    provider = SendGridEmailCampaignProvider("SG.test")
    requests = []

    def fake_request(method, path, *, body=None):
        requests.append((method, path, body))
        return {"id": "draft-id", "status": "draft"}

    monkeypatch.setattr(provider, "_request", fake_request)
    result = provider.create_draft(
        name="Listing",
        subject="Subject",
        html_content="<p>Body</p>",
        plain_content="Body",
        sender_id=1,
        list_ids=["list-1"],
        suppression_group_id=2,
    )
    assert result["provider_status"] == "draft"
    assert [(method, path) for method, path, _ in requests] == [
        ("POST", "/marketing/singlesends")
    ]
    assert "/schedule" not in str(requests)


def test_sendgrid_payload_without_list_omits_send_to_and_all(monkeypatch):
    provider = SendGridEmailCampaignProvider("SG.test")
    captured = {}

    def fake_request(method, path, *, body=None):
        captured.update(body)
        return {"id": "draft-id", "status": "draft"}

    monkeypatch.setattr(provider, "_request", fake_request)
    provider.create_draft(
        name="Listing",
        subject="Subject",
        html_content="<p>Body</p>",
        plain_content="Body",
        sender_id=1,
        list_ids=[],
        suppression_group_id=2,
    )
    assert "send_to" not in captured
    assert "all" not in captured


def test_connection_test_is_read_only(monkeypatch):
    provider = SendGridEmailCampaignProvider("SG.test")
    calls = []

    def fake_request(method, path, *, body=None):
        calls.append((method, path))
        if path == "/verified_senders":
            return {"results": []}
        if path.startswith("/marketing/lists"):
            return {"result": []}
        return []

    monkeypatch.setattr(provider, "_request", fake_request)
    result = provider.test_connection()
    assert result["connected"] is True
    assert calls
    assert all(method == "GET" for method, _ in calls)
    assert all("singlesends" not in path for _, path in calls)


def test_reopened_listing_and_archive_include_campaign_state(
    app_client, two_users, monkeypatch
):
    user_id, _ = two_users
    generation = _generation(user_id, "128 Campaign Way")
    _connect(user_id, monkeypatch)
    provider = _FakeProvider()
    monkeypatch.setattr(
        listing_email_campaigns,
        "get_provider",
        lambda *args, **kwargs: provider,
    )
    listing_email_campaigns.export_listing_email(user_id, generation["id"])

    _login(app_client, user_id)
    db.update_user_subscription(user_id, "active")
    reopened = app_client.get(f"/listings/{generation['id']}")
    assert reopened.status_code == 200
    assert (
        reopened.get_json()["generation"]["email_campaign"]["status"]
        == "draft_created"
    )
    archive = app_client.get("/listings/archive/search")
    matching = [
        item
        for item in archive.get_json()["items"]
        if item["id"] == generation["id"]
    ]
    assert matching[0]["email_campaign"]["status"] == "draft_created"


def test_listing_cleanup_removes_local_export_not_remote_draft(
    two_users, monkeypatch
):
    user_id, _ = two_users
    generation = _generation(user_id, "129 Campaign Way")
    _connect(user_id, monkeypatch)
    provider = _FakeProvider()
    monkeypatch.setattr(
        listing_email_campaigns,
        "get_provider",
        lambda *args, **kwargs: provider,
    )
    listing_email_campaigns.export_listing_email(user_id, generation["id"])
    old = datetime.now(timezone.utc) - timedelta(days=61)
    with db.get_db() as conn:
        conn.execute(
            """
            UPDATE listing_generations SET created_at = ?, expires_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (old.isoformat(), old.isoformat(), generation["id"], user_id),
        )

    listing_db.cleanup_expired()
    assert marketing_db.list_for_generation(user_id, generation["id"]) == []
    # Cleanup deletes no provider object; only the original create call occurred.
    assert len(provider.calls) == 1
