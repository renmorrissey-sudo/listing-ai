"""One-click Post orchestrator: only configured+enabled+ready channels are
called, idempotency prevents duplicate posts, partial failures are recorded
per-provider, retry never reposts succeeded channels, and posting can never
cross tenants."""

from unittest.mock import patch

import config
import listing_generations_db as listing_db
import listing_publish
import social_connections_db as social_db
from cryptography.fernet import Fernet


def _enable_encryption(monkeypatch):
    monkeypatch.setattr(config, "SOCIAL_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())


def _make_generation(user_id, address="1 Publish St"):
    return listing_db.create_generation(
        user_id,
        display_address=address,
        output_snapshot={"listing": "L", "social": "1. [LINKEDIN] Great home!", "email": "E"},
        social_content={"baseCaption": "Great home!", "linkedin": "Great home!"},
    )


def _connect_and_enable(user_id, provider="linkedin", account="urn:li:person:one"):
    conn = social_db.upsert_connection(
        user_id, provider, external_account_id=account, access_token="tok-" + account
    )
    social_db.set_default_enabled(user_id, conn["id"], True)
    return conn


def _make_ready(monkeypatch, provider="linkedin"):
    if provider == "linkedin":
        monkeypatch.setattr(config, "LINKEDIN_CLIENT_ID", "id")
        monkeypatch.setattr(config, "LINKEDIN_CLIENT_SECRET", "secret")


def test_publish_only_calls_configured_enabled_ready_channels(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    _make_ready(monkeypatch)
    u1, _ = two_users
    generation = _make_generation(u1)
    _connect_and_enable(u1)

    with patch(
        "social_providers.linkedin.LinkedInProvider.publish_text",
        return_value={"provider_post_id": "urn:li:share:123", "provider_post_url": "https://linkedin.com/x"},
    ) as mock_publish:
        result = listing_publish.publish_listing(u1, generation["id"])

    assert mock_publish.call_count == 1
    assert len(result["results"]) == 1
    assert result["results"][0]["status"] == "published"
    assert result["results"][0]["provider"] == "linkedin"


def test_unconfigured_channel_is_not_called(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    # LinkedIn intentionally left unconfigured (not "ready").
    monkeypatch.setattr(config, "LINKEDIN_CLIENT_ID", "")
    monkeypatch.setattr(config, "LINKEDIN_CLIENT_SECRET", "")
    u1, _ = two_users
    generation = _make_generation(u1)
    _connect_and_enable(u1)

    with patch("social_providers.linkedin.LinkedInProvider.publish_text") as mock_publish:
        result = listing_publish.publish_listing(u1, generation["id"])

    mock_publish.assert_not_called()
    # Not-ready providers are excluded from the default one-click target set
    # entirely (readiness is checked before any publish attempt is made).
    assert result["results"] == []


def test_post_not_enabled_if_no_channels_connected(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    _make_ready(monkeypatch)
    u1, _ = two_users
    generation = _make_generation(u1)
    result = listing_publish.publish_listing(u1, generation["id"])
    assert result["results"] == []
    assert listing_publish.default_target_connections(u1) == []


def test_generation_alone_never_triggers_publication(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    _make_ready(monkeypatch)
    u1, _ = two_users
    _connect_and_enable(u1)
    with patch("social_providers.linkedin.LinkedInProvider.publish_text") as mock_publish:
        _make_generation(u1)
    mock_publish.assert_not_called()


def test_duplicate_click_cannot_create_duplicate_posts(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    _make_ready(monkeypatch)
    u1, _ = two_users
    generation = _make_generation(u1)
    _connect_and_enable(u1)

    with patch(
        "social_providers.linkedin.LinkedInProvider.publish_text",
        return_value={"provider_post_id": "urn:li:share:999", "provider_post_url": "https://linkedin.com/y"},
    ) as mock_publish:
        op_id = social_db.new_operation_id()
        first = listing_publish.publish_listing(u1, generation["id"], operation_id=op_id)
        second = listing_publish.publish_listing(u1, generation["id"], operation_id=op_id)

    assert mock_publish.call_count == 1
    assert first["results"][0]["status"] == "published"
    assert second["results"][0]["status"] == "published"

    publications = social_db.list_publications_for_generation(u1, generation["id"])
    assert len(publications) == 1


def test_new_operation_id_allows_intentional_repost(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    _make_ready(monkeypatch)
    u1, _ = two_users
    generation = _make_generation(u1)
    _connect_and_enable(u1)

    with patch(
        "social_providers.linkedin.LinkedInProvider.publish_text",
        return_value={"provider_post_id": "urn:li:share:1", "provider_post_url": "https://linkedin.com/1"},
    ) as mock_publish:
        listing_publish.publish_listing(u1, generation["id"])
        listing_publish.publish_listing(u1, generation["id"])

    assert mock_publish.call_count == 2
    publications = social_db.list_publications_for_generation(u1, generation["id"])
    assert len(publications) == 2


def test_partial_success_is_correctly_recorded(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    monkeypatch.setattr(config, "LINKEDIN_CLIENT_ID", "id")
    monkeypatch.setattr(config, "LINKEDIN_CLIENT_SECRET", "secret")
    monkeypatch.setattr(config, "META_APP_REVIEW_APPROVED", True)
    monkeypatch.setattr(config, "FACEBOOK_APP_ID", "id")
    monkeypatch.setattr(config, "FACEBOOK_APP_SECRET", "secret")

    u1, _ = two_users
    generation = _make_generation(u1)
    _connect_and_enable(u1, provider="linkedin", account="urn:li:person:ok")
    _connect_and_enable(u1, provider="facebook", account="fb-page-1")

    def _fake_publish(self, *, credentials, caption, listing_generation):
        if self.name == "linkedin":
            return {"provider_post_id": "urn:li:share:1", "provider_post_url": "https://linkedin.com/1"}
        raise RuntimeError("Facebook API is down")

    with patch("social_providers.linkedin.LinkedInProvider.publish_text", autospec=True, side_effect=_fake_publish), \
        patch("social_providers.facebook.FacebookProvider.publish_text", autospec=True, side_effect=_fake_publish):
        result = listing_publish.publish_listing(u1, generation["id"])

    by_provider = {r["provider"]: r for r in result["results"]}
    assert by_provider["linkedin"]["status"] == "published"
    assert by_provider["facebook"]["status"] == "failed"
    assert by_provider["facebook"]["error_summary"]


def test_retry_failed_provider_does_not_repost_successful_providers(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    monkeypatch.setattr(config, "LINKEDIN_CLIENT_ID", "id")
    monkeypatch.setattr(config, "LINKEDIN_CLIENT_SECRET", "secret")
    monkeypatch.setattr(config, "META_APP_REVIEW_APPROVED", True)
    monkeypatch.setattr(config, "FACEBOOK_APP_ID", "id")
    monkeypatch.setattr(config, "FACEBOOK_APP_SECRET", "secret")

    u1, _ = two_users
    generation = _make_generation(u1)
    _connect_and_enable(u1, provider="linkedin", account="urn:li:person:ok")
    _connect_and_enable(u1, provider="facebook", account="fb-page-1")

    linkedin_mock = patch(
        "social_providers.linkedin.LinkedInProvider.publish_text",
        return_value={"provider_post_id": "urn:li:share:1", "provider_post_url": "https://linkedin.com/1"},
    )
    facebook_mock = patch(
        "social_providers.facebook.FacebookProvider.publish_text", side_effect=RuntimeError("down")
    )
    with linkedin_mock as li_mock, facebook_mock:
        listing_publish.publish_listing(u1, generation["id"])
    assert li_mock.call_count == 1

    with linkedin_mock as li_mock_retry, patch(
        "social_providers.facebook.FacebookProvider.publish_text",
        return_value={"provider_post_id": "fb-post-1", "provider_post_url": "https://fb.com/1"},
    ) as fb_retry_mock:
        retry_result = listing_publish.retry_publish(u1, generation["id"], "facebook")

    li_mock_retry.assert_not_called()
    assert fb_retry_mock.call_count == 1
    assert retry_result["status"] == "published"

    publications = social_db.list_publications_for_generation(u1, generation["id"])
    statuses = {p["provider"]: p["status"] for p in publications}
    assert statuses["linkedin"] == "published"
    assert statuses["facebook"] == "published"


def test_provider_post_id_is_stored(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    _make_ready(monkeypatch)
    u1, _ = two_users
    generation = _make_generation(u1)
    _connect_and_enable(u1)
    with patch(
        "social_providers.linkedin.LinkedInProvider.publish_text",
        return_value={"provider_post_id": "urn:li:share:abc123", "provider_post_url": "https://linkedin.com/abc123"},
    ):
        listing_publish.publish_listing(u1, generation["id"])
    publications = social_db.list_publications_for_generation(u1, generation["id"])
    assert publications[0]["provider_post_id"] == "urn:li:share:abc123"


def test_archive_displays_publishing_status(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    _make_ready(monkeypatch)
    u1, _ = two_users
    generation = _make_generation(u1)
    _connect_and_enable(u1)
    with patch(
        "social_providers.linkedin.LinkedInProvider.publish_text",
        return_value={"provider_post_id": "urn:li:share:1", "provider_post_url": "https://linkedin.com/1"},
    ):
        listing_publish.publish_listing(u1, generation["id"])

    annotated = listing_publish.annotate_publish_status(u1, [generation])
    assert annotated[0]["publish_status"]["linkedin"] == "published"


def test_social_posting_cannot_occur_across_tenants(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    _make_ready(monkeypatch)
    u1, u2 = two_users
    generation = _make_generation(u1)
    other_conn = _connect_and_enable(u2, account="urn:li:person:other")

    with patch("social_providers.linkedin.LinkedInProvider.publish_text") as mock_publish:
        # Attempting to target tenant 2's connection id while acting as tenant 1
        # must resolve to zero targets, not tenant 2's account.
        result = listing_publish.publish_listing(u1, generation["id"], connection_ids=[other_conn["id"]])

    mock_publish.assert_not_called()
    assert result["results"] == []


def test_expired_social_token_results_in_useful_reconnect_state(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    _make_ready(monkeypatch)
    u1, _ = two_users
    generation = _make_generation(u1)
    conn = _connect_and_enable(u1)
    # Simulate a token that was cleared out (e.g. after a prior disconnect/expiry).
    social_db.disconnect(u1, conn["id"])
    with patch("social_providers.linkedin.LinkedInProvider.publish_text") as mock_publish:
        result = listing_publish.publish_listing(u1, generation["id"])
    mock_publish.assert_not_called()
    assert result["results"] == []  # disconnected -> no longer a default-enabled target


def test_instagram_reports_not_available_without_attempting_a_call(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    monkeypatch.setattr(config, "META_APP_REVIEW_APPROVED", False)
    u1, _ = two_users
    generation = _make_generation(u1)
    conn = social_db.upsert_connection(
        u1, "instagram", external_account_id="ig-1", access_token="tok"
    )
    social_db.set_default_enabled(u1, conn["id"], True)

    with patch("social_providers.instagram.InstagramProvider.publish_text") as mock_publish:
        result = listing_publish.publish_listing(u1, generation["id"])

    mock_publish.assert_not_called()
    assert result["results"] == []


def test_instagram_explicitly_selected_reports_not_available_instead_of_calling(two_users, monkeypatch):
    """Via the per-post channel-selection dropdown (explicit connection_ids), an
    unready provider still resolves to a clear 'failed' result rather than a
    silent no-op or an attempted network call."""
    _enable_encryption(monkeypatch)
    monkeypatch.setattr(config, "META_APP_REVIEW_APPROVED", False)
    u1, _ = two_users
    generation = _make_generation(u1)
    conn = social_db.upsert_connection(u1, "instagram", external_account_id="ig-1", access_token="tok")

    with patch("social_providers.instagram.InstagramProvider.publish_text") as mock_publish:
        result = listing_publish.publish_listing(u1, generation["id"], connection_ids=[conn["id"]])

    mock_publish.assert_not_called()
    assert len(result["results"]) == 1
    assert result["results"][0]["status"] == "failed"
    assert "isn't available" in result["results"][0]["error_summary"]


def test_x_provider_is_never_ready_and_never_called(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    u1, _ = two_users
    generation = _make_generation(u1)
    conn = social_db.upsert_connection(u1, "x", external_account_id="x-1", access_token="tok")
    social_db.set_default_enabled(u1, conn["id"], True)

    with patch("social_providers.x.XProvider.publish_text") as mock_publish:
        result = listing_publish.publish_listing(u1, generation["id"])

    mock_publish.assert_not_called()
    assert result["results"] == []
