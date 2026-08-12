"""Social Media Connections: OAuth CSRF state, tenant scoping, token
secrecy, disconnect, and default-channel persistence."""

import config
import db
import social_connections_db as social_db
from cryptography.fernet import Fernet


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["session_version"] = 1


def _enable_encryption(monkeypatch):
    monkeypatch.setattr(config, "SOCIAL_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())


def test_oauth_state_round_trip_is_valid(two_users):
    u1, _ = two_users
    state = social_db.create_oauth_state(u1, "linkedin", redirect_uri="https://x/callback")
    consumed = social_db.consume_oauth_state(state, "linkedin")
    assert consumed is not None
    assert consumed["user_id"] == u1


def test_oauth_state_is_single_use(two_users):
    u1, _ = two_users
    state = social_db.create_oauth_state(u1, "linkedin")
    assert social_db.consume_oauth_state(state, "linkedin") is not None
    assert social_db.consume_oauth_state(state, "linkedin") is None


def test_oauth_state_provider_mismatch_is_rejected(two_users):
    u1, _ = two_users
    state = social_db.create_oauth_state(u1, "linkedin")
    assert social_db.consume_oauth_state(state, "facebook") is None


def test_oauth_state_unknown_token_is_rejected(two_users):
    assert social_db.consume_oauth_state("not-a-real-state", "linkedin") is None


def test_connection_belongs_to_tenant(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    u1, u2 = two_users
    conn = social_db.upsert_connection(
        u1, "linkedin", external_account_id="urn:li:person:abc", access_token="secret-token"
    )
    assert social_db.get_connection_by_id(u1, conn["id"]) is not None
    assert social_db.get_connection_by_id(u2, conn["id"]) is None
    assert conn["id"] not in {c["id"] for c in social_db.list_connections(u2)}


def test_tokens_never_appear_in_public_connection_dicts(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    u1, _ = two_users
    conn = social_db.upsert_connection(
        u1,
        "linkedin",
        external_account_id="urn:li:person:abc",
        access_token="super-secret-access",
        refresh_token="super-secret-refresh",
    )
    assert "access_token_encrypted" not in conn
    assert "refresh_token_encrypted" not in conn
    assert "super-secret-access" not in str(conn)
    assert "super-secret-refresh" not in str(conn)

    listed = social_db.list_connections(u1)[0]
    assert "access_token_encrypted" not in listed
    assert "super-secret-access" not in str(listed)


def test_tokens_never_appear_in_connections_page_html(app_client, two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    u1, _ = two_users
    social_db.upsert_connection(
        u1, "linkedin", external_account_id="urn:li:person:abc", access_token="super-secret-access"
    )
    _login(app_client, u1)
    db.update_user_subscription(u1, "active")
    res = app_client.get("/social/connections")
    assert res.status_code == 200
    assert b"super-secret-access" not in res.data


def test_get_connection_credentials_decrypts_for_internal_use_only(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    u1, _ = two_users
    conn = social_db.upsert_connection(
        u1, "linkedin", external_account_id="urn:li:person:abc", access_token="round-trip-token"
    )
    creds = social_db.get_connection_credentials(u1, conn["id"])
    assert creds["access_token"] == "round-trip-token"


def test_disconnect_deactivates_and_clears_tokens(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    u1, _ = two_users
    conn = social_db.upsert_connection(
        u1, "linkedin", external_account_id="urn:li:person:abc", access_token="tok"
    )
    assert social_db.disconnect(u1, conn["id"]) is True
    refreshed = social_db.get_connection_by_id(u1, conn["id"])
    assert refreshed["status"] == "disconnected"
    assert social_db.get_connection_credentials(u1, conn["id"]) is None
    assert conn["id"] not in {c["id"] for c in social_db.list_default_enabled_connections(u1)}


def test_default_channel_selection_persists(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    u1, _ = two_users
    conn = social_db.upsert_connection(
        u1, "linkedin", external_account_id="urn:li:person:abc", access_token="tok"
    )
    assert social_db.list_default_enabled_connections(u1) == []
    social_db.set_default_enabled(u1, conn["id"], True)
    enabled = social_db.list_default_enabled_connections(u1)
    assert len(enabled) == 1
    assert enabled[0]["id"] == conn["id"]
    social_db.set_default_enabled(u1, conn["id"], False)
    assert social_db.list_default_enabled_connections(u1) == []


def test_default_channels_route_only_toggles_own_tenant_connections(app_client, two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    u1, u2 = two_users
    conn1 = social_db.upsert_connection(
        u1, "linkedin", external_account_id="urn:li:person:one", access_token="tok"
    )
    conn2 = social_db.upsert_connection(
        u2, "linkedin", external_account_id="urn:li:person:two", access_token="tok"
    )
    _login(app_client, u1)
    db.update_user_subscription(u1, "active")
    res = app_client.post(
        "/social/connections/default-channels",
        data={"enabled_connection_ids": [str(conn1["id"]), str(conn2["id"])]},
    )
    assert res.status_code in (302, 303)
    assert social_db.get_connection_by_id(u1, conn1["id"])["default_enabled"]
    # Tenant 1 cannot enable tenant 2's connection via this route.
    assert not any(c["id"] == conn2["id"] for c in social_db.list_default_enabled_connections(u2))


def test_connect_requires_login(app_client):
    res = app_client.get("/social/connections/linkedin/connect")
    assert res.status_code in (302, 303)
    assert "/login" in res.headers.get("Location", "")


def test_connect_redirects_to_provider_authorize_url_when_configured(app_client, two_users, monkeypatch):
    monkeypatch.setattr(config, "LINKEDIN_CLIENT_ID", "client-id")
    monkeypatch.setattr(config, "LINKEDIN_CLIENT_SECRET", "client-secret")
    u1, _ = two_users
    _login(app_client, u1)
    db.update_user_subscription(u1, "active")
    res = app_client.get("/social/connections/linkedin/connect")
    assert res.status_code in (302, 303)
    location = res.headers.get("Location", "")
    assert "linkedin.com/oauth/v2/authorization" in location
    assert "state=" in location


def test_connect_blocked_when_provider_not_configured(app_client, two_users, monkeypatch):
    monkeypatch.setattr(config, "LINKEDIN_CLIENT_ID", "")
    monkeypatch.setattr(config, "LINKEDIN_CLIENT_SECRET", "")
    u1, _ = two_users
    _login(app_client, u1)
    db.update_user_subscription(u1, "active")
    res = app_client.get("/social/connections/linkedin/connect", follow_redirects=True)
    assert res.status_code == 200
    assert b"not configured" in res.data or b"isn&#39;t available" in res.data or b"isn't available" in res.data


def test_callback_rejects_forged_state(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    db.update_user_subscription(u1, "active")
    res = app_client.get(
        "/social/connections/linkedin/callback?state=forged-state&code=abc", follow_redirects=True
    )
    assert res.status_code == 200
    assert b"expired" in res.data.lower() or b"already used" in res.data.lower()
