"""Staging-only social dry-run layer (Phase A).

Proves: the dry-run is off by default and refuses to enable outside
staging even with the flag set; when enabled in staging it records the
exact payload a provider WOULD send (URL, method, headers with secrets
redacted, body) plus an audit row, without any network call; gated
channels record the gate reason instead of a payload; a repeated
dry-run operation returns the recorded payload; a dry-run never masks
a real publication status; and the HTTP route refuses outside staging.
"""

import json
import socket
from unittest.mock import patch

import pytest

import config
import db
import listing_generations_db as listing_db
import listing_publish
import social_connections_db as social_db
import social_dry_run
from cryptography.fernet import Fernet
from social_dry_run import SocialDryRunNotAvailable, dry_run_publish, get_dry_run_record


def _enable_encryption(monkeypatch):
    monkeypatch.setattr(config, "SOCIAL_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())


def _enable_dry_run(monkeypatch):
    monkeypatch.setattr(config, "IS_STAGING", True)
    monkeypatch.setattr(config, "SOCIAL_DRY_RUN", True)


def _make_generation(user_id, address="1 Dry Run St"):
    return listing_db.create_generation(
        user_id,
        display_address=address,
        output_snapshot={"listing": "L", "social": "1. [LINKEDIN] Great home!", "email": "E"},
        social_content={
            "baseCaption": "Great home!",
            "linkedin": "Great home!",
            "facebook": "Great home on Facebook!",
        },
    )


def _connect_and_enable(user_id, provider="linkedin", account="urn:li:person:one"):
    conn = social_db.upsert_connection(
        user_id, provider, external_account_id=account, access_token="tok-" + account
    )
    social_db.set_default_enabled(user_id, conn["id"], True)
    return conn


def _make_ready(monkeypatch, *providers):
    if "linkedin" in providers:
        monkeypatch.setattr(config, "LINKEDIN_CLIENT_ID", "id")
        monkeypatch.setattr(config, "LINKEDIN_CLIENT_SECRET", "secret")
    if "facebook" in providers or "instagram" in providers:
        monkeypatch.setattr(config, "FACEBOOK_APP_ID", "id")
        monkeypatch.setattr(config, "FACEBOOK_APP_SECRET", "secret")
        monkeypatch.setattr(config, "META_APP_REVIEW_APPROVED", True)
    if "tiktok" in providers:
        monkeypatch.setattr(config, "TIKTOK_CLIENT_KEY", "key")
        monkeypatch.setattr(config, "TIKTOK_CLIENT_SECRET", "secret")
        monkeypatch.setattr(config, "TIKTOK_AUDIT_APPROVED", True)


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["session_version"] = 1


# ---------------------------------------------------------------------------
# Gating: off by default, refuses outside staging
# ---------------------------------------------------------------------------


def test_dry_run_disabled_by_default(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    u1, _ = two_users
    generation = _make_generation(u1)
    # Test env: APP_ENV=test, so IS_STAGING is False and the flag is unset.
    assert social_dry_run.is_dry_run_enabled() is False
    with pytest.raises(SocialDryRunNotAvailable):
        dry_run_publish(u1, generation["id"])
    assert social_db.list_publications_for_generation(u1, generation["id"]) == []


def test_dry_run_refuses_in_production_even_with_flag_set(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    u1, _ = two_users
    generation = _make_generation(u1)
    _connect_and_enable(u1)
    monkeypatch.setattr(config, "IS_STAGING", False)
    monkeypatch.setattr(config, "APP_ENV", "production")
    monkeypatch.setattr(config, "SOCIAL_DRY_RUN", True)
    assert social_dry_run.is_dry_run_enabled() is False
    with pytest.raises(SocialDryRunNotAvailable):
        dry_run_publish(u1, generation["id"])
    # No DB assertion here: APP_ENV=production makes this environment
    # refuse SQLite connections, and the raise above precedes any DB
    # access in dry_run_publish, so no row could have been created.


def test_dry_run_route_refuses_outside_staging(app_client, two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    u1, _ = two_users
    generation = _make_generation(u1)
    _connect_and_enable(u1)
    _login(app_client, u1)
    res = app_client.post(f"/listings/{generation['id']}/post/dry-run", json={})
    assert res.status_code == 403
    assert social_db.list_publications_for_generation(u1, generation["id"]) == []


# ---------------------------------------------------------------------------
# Payload recording (staging, no network)
# ---------------------------------------------------------------------------


def test_dry_run_records_exact_linkedin_payload_without_network(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    _enable_dry_run(monkeypatch)
    _make_ready(monkeypatch, "linkedin")
    u1, _ = two_users
    generation = _make_generation(u1)
    _connect_and_enable(u1)

    def _no_network(*args, **kwargs):
        raise AssertionError("a real network call was attempted")

    with patch.object(socket.socket, "connect", _no_network):
        result = dry_run_publish(u1, generation["id"])

    assert result["dry_run"] is True
    assert result["operation_id"].startswith("dryrun-")
    assert len(result["results"]) == 1
    entry = result["results"][0]
    assert entry["provider"] == "linkedin"
    assert entry["status"] == "dry_run"
    payload = entry["payload"]
    assert payload["method"] == "POST"
    assert payload["url"] == "https://api.linkedin.com/rest/posts"
    assert payload["body"]["author"] == "urn:li:person:one"
    assert payload["body"]["commentary"] == "Great home!"
    assert payload["body"]["visibility"] == "PUBLIC"
    assert payload["caption"] == "Great home!"
    headers = {k.lower(): v for k, v in payload["headers"].items()}
    assert headers["authorization"] == "[redacted]"
    assert headers["linkedin-version"] == "202401"
    # No secret value anywhere in the recorded payload.
    assert "tok-urn:li:person:one" not in json.dumps(payload)

    publications = social_db.list_publications_for_generation(u1, generation["id"])
    assert len(publications) == 1
    row = publications[0]
    assert row["status"] == "dry_run"
    assert row["provider_post_id"] is None
    assert row["provider_post_url"] is None
    stored = json.loads(row["dry_run_payload_json"])
    assert stored == payload
    assert "tok-urn:li:person:one" not in row["dry_run_payload_json"]


def test_dry_run_records_gate_reason_for_unready_channels(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    _enable_dry_run(monkeypatch)
    # Nothing configured: LinkedIn/Facebook/Instagram/TikTok all
    # setup_required; X is coming_soon.
    u1, _ = two_users
    generation = _make_generation(u1)
    _connect_and_enable(u1, provider="linkedin", account="urn:li:person:one")
    _connect_and_enable(u1, provider="facebook", account="fb-page-1")
    _connect_and_enable(u1, provider="instagram", account="ig-1")
    _connect_and_enable(u1, provider="tiktok", account="tt-1")
    _connect_and_enable(u1, provider="x", account="x-1")

    def _no_network(*args, **kwargs):
        raise AssertionError("a real network call was attempted")

    with patch.object(socket.socket, "connect", _no_network):
        result = dry_run_publish(u1, generation["id"])

    by_provider = {r["provider"]: r for r in result["results"]}
    assert set(by_provider) == {"linkedin", "facebook", "instagram", "tiktok", "x"}
    for provider, entry in by_provider.items():
        assert entry["status"] == "dry_run"
        assert entry["payload"] is None
        assert entry["note"]
    assert "not configured" in by_provider["linkedin"]["note"]
    assert "not configured" in by_provider["facebook"]["note"]
    assert "not configured" in by_provider["instagram"]["note"]
    assert "not configured" in by_provider["tiktok"]["note"]
    assert "pay-per-post" in by_provider["x"]["note"]

    publications = social_db.list_publications_for_generation(u1, generation["id"])
    assert len(publications) == 5
    assert all(p["status"] == "dry_run" for p in publications)
    assert all(p["dry_run_payload_json"] is None for p in publications)
    assert all(p["provider_post_id"] is None for p in publications)


def test_dry_run_unavailable_channel_records_media_reason(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    _enable_dry_run(monkeypatch)
    _make_ready(monkeypatch, "instagram", "tiktok")
    u1, _ = two_users
    generation = _make_generation(u1)
    _connect_and_enable(u1, provider="instagram", account="ig-1")
    _connect_and_enable(u1, provider="tiktok", account="tt-1")

    def _no_network(*args, **kwargs):
        raise AssertionError("a real network call was attempted")

    with patch.object(socket.socket, "connect", _no_network):
        result = dry_run_publish(u1, generation["id"])

    by_provider = {r["provider"]: r for r in result["results"]}
    assert by_provider["instagram"]["payload"] is None
    assert "photo" in by_provider["instagram"]["note"].lower()
    assert by_provider["tiktok"]["payload"] is None
    assert "video" in by_provider["tiktok"]["note"].lower()


def test_dry_run_missing_credentials_records_reconnect_reason(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    _enable_dry_run(monkeypatch)
    _make_ready(monkeypatch, "linkedin")
    u1, _ = two_users
    generation = _make_generation(u1)
    conn = _connect_and_enable(u1)
    # Clear the stored token directly: the connection stays 'connected'
    # (so it is still a dry-run target) but has no credentials.
    with db.get_db() as conn_db:
        conn_db.execute(
            "UPDATE social_connections SET access_token_encrypted = NULL WHERE id = ?",
            (conn["id"],),
        )

    result = dry_run_publish(u1, generation["id"])
    assert len(result["results"]) == 1
    assert result["results"][0]["status"] == "dry_run"
    assert result["results"][0]["payload"] is None
    assert "Reconnect" in result["results"][0]["note"]
    assert social_db.get_connection_by_id(u1, conn["id"])["status"] == "needs_reconnect"


def test_dry_run_is_idempotent_for_same_operation(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    _enable_dry_run(monkeypatch)
    _make_ready(monkeypatch, "linkedin")
    u1, _ = two_users
    generation = _make_generation(u1)
    _connect_and_enable(u1)

    op_id = "op-dry-run-repeat"
    first = dry_run_publish(u1, generation["id"], operation_id=op_id)
    second = dry_run_publish(u1, generation["id"], operation_id=op_id)
    assert first["results"][0]["payload"] == second["results"][0]["payload"]
    assert first["operation_id"] == second["operation_id"]
    publications = social_db.list_publications_for_generation(u1, generation["id"])
    assert len(publications) == 1
    assert publications[0]["status"] == "dry_run"


def test_dry_run_never_masks_a_real_publication_status(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    _enable_dry_run(monkeypatch)
    _make_ready(monkeypatch, "linkedin")
    u1, _ = two_users
    generation = _make_generation(u1)
    _connect_and_enable(u1)

    # Real publish first (publish_text patched, as in test_social_publish.py).
    with patch(
        "social_providers.linkedin.LinkedInProvider.publish_text",
        return_value={"provider_post_id": "urn:li:share:1",
                      "provider_post_url": "https://linkedin.com/1"},
    ):
        published = listing_publish.publish_listing(u1, generation["id"])
    assert published["results"][0]["status"] == "published"

    # A later dry-run of the same generation must not change the status
    # the archive view reports for the channel.
    dry_run_publish(u1, generation["id"])
    annotated = listing_publish.annotate_publish_status(u1, [generation])
    assert annotated[0]["publish_status"] == {"linkedin": "published"}


def test_dry_run_records_facebook_payload_with_token_redacted(two_users, monkeypatch):
    """Facebook puts the access token in the POST body (unlike LinkedIn's
    header) — the recorded payload must redact it there too."""
    _enable_encryption(monkeypatch)
    _enable_dry_run(monkeypatch)
    _make_ready(monkeypatch, "facebook")
    u1, _ = two_users
    generation = _make_generation(u1)
    _connect_and_enable(u1, provider="facebook", account="fb-page-1")

    def _no_network(*args, **kwargs):
        raise AssertionError("a real network call was attempted")

    with patch.object(socket.socket, "connect", _no_network):
        result = dry_run_publish(u1, generation["id"])

    assert len(result["results"]) == 1
    payload = result["results"][0]["payload"]
    assert payload["method"] == "POST"
    assert payload["url"] == "https://graph.facebook.com/v21.0/fb-page-1/feed"
    assert payload["body"]["message"] == "Great home on Facebook!"
    assert payload["body"]["access_token"] == "[redacted]"
    assert "tok-fb-page-1" not in json.dumps(payload)
    row = social_db.list_publications_for_generation(u1, generation["id"])[0]
    assert row["status"] == "dry_run"
    assert "tok-fb-page-1" not in row["dry_run_payload_json"]


def test_dry_run_with_explicit_connection_ids_targets_only_those(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    _enable_dry_run(monkeypatch)
    _make_ready(monkeypatch, "linkedin", "facebook")
    u1, _ = two_users
    generation = _make_generation(u1)
    li_conn = _connect_and_enable(u1, provider="linkedin", account="urn:li:person:one")
    _connect_and_enable(u1, provider="facebook", account="fb-page-1")

    result = dry_run_publish(u1, generation["id"], connection_ids=[li_conn["id"]])
    assert [r["provider"] for r in result["results"]] == ["linkedin"]


def test_dry_run_route_records_payload_in_staging(app_client, two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    _enable_dry_run(monkeypatch)
    _make_ready(monkeypatch, "linkedin")
    u1, _ = two_users
    generation = _make_generation(u1)
    _connect_and_enable(u1)
    _login(app_client, u1)

    def _no_network(*args, **kwargs):
        raise AssertionError("a real network call was attempted")

    with patch.object(socket.socket, "connect", _no_network):
        res = app_client.post(f"/listings/{generation['id']}/post/dry-run", json={})
    assert res.status_code == 200
    data = res.get_json()
    assert data["dry_run"] is True
    assert data["results"][0]["provider"] == "linkedin"
    assert data["results"][0]["payload"]["url"] == "https://api.linkedin.com/rest/posts"


def test_get_dry_run_record_is_tenant_scoped(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    _enable_dry_run(monkeypatch)
    _make_ready(monkeypatch, "linkedin")
    u1, u2 = two_users
    generation = _make_generation(u1)
    _connect_and_enable(u1)

    result = dry_run_publish(u1, generation["id"])
    publication_id = result["results"][0]["publication_id"]
    record = get_dry_run_record(u1, publication_id)
    assert record is not None
    assert record["status"] == "dry_run"
    assert record["payload"]["url"] == "https://api.linkedin.com/rest/posts"
    # Another tenant cannot read it.
    assert get_dry_run_record(u2, publication_id) is None
    assert get_dry_run_record(u1, publication_id + 99999) is None
