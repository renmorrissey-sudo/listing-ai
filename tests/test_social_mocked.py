"""Social providers against mocked platform responses (Phase A).

Unlike test_social_publish.py (which patches `publish_text` and only
proves orchestration), these tests drive the REAL provider code —
request construction, headers, payload shape, error mapping — against
a scripted ``urllib.request.urlopen`` transport. No real network call
is ever made: the script raises AssertionError on any URL it was not
told to expect, and the suite runs with no provider credentials.

Covered per provider (where a client exists): formatting (request
shape, caption truncation, platform-section fallback), authentication
failures (401/403 → needs_reconnect), rate limits (429 → retryable),
rejected posts (400 → not retryable), network failures (retryable),
retries (a 429 then a success on a fresh operation), duplicate
prevention (same operation_id never re-calls the provider), and status
reporting (post id/url stored on success, error summary on failure,
per-provider partial results).
"""

import io
import json
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

import pytest

import config
import listing_generations_db as listing_db
import listing_publish
import social_connections_db as social_db
from cryptography.fernet import Fernet
from social_content import caption_for_platform, parse_platform_sections
from social_providers import registry
from social_providers.base import SocialProviderError, SocialProviderNotAvailable
from social_providers.facebook import FacebookProvider
from social_providers.instagram import InstagramProvider
from social_providers.linkedin import LinkedInProvider
from social_providers.tiktok import TikTokProvider
from social_providers.x import XProvider


# ---------------------------------------------------------------------------
# Scripted transport
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body=b"{}", headers=None):
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(url, code, body=b'{"error": {"message": "denied"}}'):
    return urllib.error.HTTPError(url, code, "error", {}, io.BytesIO(body))


class _UrlopenScript:
    """Scripted urllib.request.urlopen. Rules are (method, url-substring,
    result) matched in order; a result may be a list, consumed one item
    per call (for fail-then-succeed sequences). Any unscripted call
    raises AssertionError, so a test can never silently hit the network."""

    def __init__(self):
        self.rules = []
        self.calls = []  # (method, url, data-bytes-or-None)

    def add(self, method, substr, result):
        self.rules.append((method, substr, result))
        return self

    def __call__(self, request, timeout=None):
        if isinstance(request, urllib.request.Request):
            url = request.full_url
            method = request.get_method()
            data = request.data
        else:
            url = str(request)
            method = "GET"
            data = None
        self.calls.append((method, url, data))
        for m, substr, result in self.rules:
            if (m is None or m == method) and substr in url:
                if isinstance(result, list):
                    result = result.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"unexpected urlopen call {method} {url}")

    def __enter__(self):
        self._patcher = patch("urllib.request.urlopen", side_effect=self)
        self._patcher.start()
        return self

    def __exit__(self, *exc):
        self._patcher.stop()
        return False


def _json_response(body: dict, headers=None):
    return _FakeResponse(json.dumps(body).encode("utf-8"), headers or {})


# ---------------------------------------------------------------------------
# Fixture helpers (mirroring test_social_publish.py)
# ---------------------------------------------------------------------------


def _enable_encryption(monkeypatch):
    monkeypatch.setattr(config, "SOCIAL_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())


def _make_generation(user_id, address="1 Mocked St"):
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


def _creds(provider="linkedin", account="urn:li:person:one"):
    return {"access_token": "tok-" + account, "external_account_id": account}


# ---------------------------------------------------------------------------
# Formatting: request shape, truncation, section fallback
# ---------------------------------------------------------------------------


def test_linkedin_publish_builds_exact_posts_api_request():
    provider = LinkedInProvider()
    script = _UrlopenScript().add(
        "POST", "api.linkedin.com/rest/posts",
        _json_response({}, {"x-restli-id": "urn:li:share:123"}),
    )
    with script:
        result = provider.publish_text(
            credentials=_creds(), caption="Great home!", listing_generation={}
        )
    assert result == {
        "provider_post_id": "urn:li:share:123",
        "provider_post_url": "https://www.linkedin.com/feed/update/urn%3Ali%3Ashare%3A123/",
    }
    assert len(script.calls) == 1
    method, url, data = script.calls[0]
    assert method == "POST"
    assert url == "https://api.linkedin.com/rest/posts"
    body = json.loads(data.decode("utf-8"))
    assert body["author"] == "urn:li:person:one"
    assert body["commentary"] == "Great home!"
    assert body["visibility"] == "PUBLIC"
    assert body["lifecycleState"] == "PUBLISHED"
    assert body["distribution"]["feedDistribution"] == "MAIN_FEED"


def test_linkedin_publish_sends_version_headers():
    provider = LinkedInProvider()
    captured = {}

    def _capture(request, timeout=None):
        captured["headers"] = dict(request.headers)
        return _json_response({}, {"x-restli-id": "urn:li:share:1"})

    with patch("urllib.request.urlopen", side_effect=_capture):
        provider.publish_text(credentials=_creds(), caption="Hi", listing_generation={})
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers["linkedin-version"] == "202401"
    assert headers["x-restli-protocol-version"] == "2.0.0"
    assert headers["authorization"] == "Bearer tok-urn:li:person:one"
    assert headers["content-type"] == "application/json"


def test_linkedin_caption_truncated_to_3000_chars():
    provider = LinkedInProvider()
    long_caption = "x" * 3500
    seen = {}

    def _capture(request, timeout=None):
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _json_response({}, {"x-restli-id": "urn:li:share:1"})

    with patch("urllib.request.urlopen", side_effect=_capture):
        provider.publish_text(credentials=_creds(), caption=long_caption, listing_generation={})
    assert len(seen["body"]["commentary"]) == 3000


def test_facebook_publish_builds_exact_feed_request():
    provider = FacebookProvider()
    script = _UrlopenScript().add(
        "POST", "graph.facebook.com/v21.0/fb-page-1/feed",
        _json_response({"id": "fb-post-1"}),
    )
    with script:
        result = provider.publish_text(
            credentials=_creds("facebook", "fb-page-1"),
            caption="Great home on Facebook!",
            listing_generation={},
        )
    assert result == {
        "provider_post_id": "fb-post-1",
        "provider_post_url": "https://www.facebook.com/fb-post-1",
    }
    method, url, data = script.calls[0]
    assert method == "POST"
    assert url == "https://graph.facebook.com/v21.0/fb-page-1/feed"
    form = dict(urllib.parse.parse_qsl(data.decode("utf-8")))
    assert form["message"] == "Great home on Facebook!"
    assert form["access_token"] == "tok-fb-page-1"


def test_facebook_message_truncated_to_63000_chars():
    provider = FacebookProvider()
    long_caption = "y" * 64000
    seen = {}

    def _capture(request, timeout=None):
        seen["form"] = dict(urllib.parse.parse_qsl(request.data.decode("utf-8")))
        return _json_response({"id": "fb-post-1"})

    with patch("urllib.request.urlopen", side_effect=_capture):
        provider.publish_text(
            credentials=_creds("facebook", "fb-page-1"),
            caption=long_caption,
            listing_generation={},
        )
    assert len(seen["form"]["message"]) == 63000


def test_caption_fallback_linkedin_uses_facebook_section_then_base():
    assert caption_for_platform({"facebook": "FB", "baseCaption": "Base"}, "linkedin") == "FB"
    assert caption_for_platform({"baseCaption": "Base"}, "linkedin") == "Base"
    assert caption_for_platform({"linkedin": "LI", "facebook": "FB"}, "linkedin") == "LI"
    assert caption_for_platform({}, "linkedin") == ""


def test_parse_platform_sections_parses_all_tags_and_ignores_unknown():
    text = (
        "1. [INSTAGRAM] Insta copy\n"
        "2. [FACEBOOK] FB copy\n"
        "3. [X/TWITTER] X copy\n"
        "4. [LINKEDIN] LI copy\n"
        "5. [TIKTOK] TT copy\n"
        "6. [PINTEREST] Not a channel"
    )
    sections = parse_platform_sections(text)
    assert sections == {
        "instagram": "Insta copy",
        "facebook": "FB copy",
        "x": "X copy",
        "linkedin": "LI copy",
        "tiktok": "TT copy",
    }
    assert parse_platform_sections("") == {}
    assert parse_platform_sections(None) == {}


# ---------------------------------------------------------------------------
# Authentication failures
# ---------------------------------------------------------------------------


def test_linkedin_publish_401_needs_reconnect():
    provider = LinkedInProvider()
    script = _UrlopenScript().add(
        "POST", "api.linkedin.com/rest/posts",
        _http_error("https://api.linkedin.com/rest/posts", 401),
    )
    with script, pytest.raises(SocialProviderError) as excinfo:
        provider.publish_text(credentials=_creds(), caption="Hi", listing_generation={})
    assert excinfo.value.needs_reconnect is True
    assert excinfo.value.retryable is False
    assert excinfo.value.provider == "linkedin"


def test_linkedin_publish_403_needs_reconnect():
    provider = LinkedInProvider()
    script = _UrlopenScript().add(
        "POST", "api.linkedin.com/rest/posts",
        _http_error("https://api.linkedin.com/rest/posts", 403),
    )
    with script, pytest.raises(SocialProviderError) as excinfo:
        provider.publish_text(credentials=_creds(), caption="Hi", listing_generation={})
    assert excinfo.value.needs_reconnect is True


def test_facebook_publish_401_needs_reconnect():
    provider = FacebookProvider()
    script = _UrlopenScript().add(
        "POST", "graph.facebook.com",
        _http_error("https://graph.facebook.com/v21.0/fb-page-1/feed", 401),
    )
    with script, pytest.raises(SocialProviderError) as excinfo:
        provider.publish_text(
            credentials=_creds("facebook", "fb-page-1"),
            caption="Hi",
            listing_generation={},
        )
    assert excinfo.value.needs_reconnect is True


def test_linkedin_exchange_code_success():
    provider = LinkedInProvider()
    script = (
        _UrlopenScript()
        .add("POST", "oauth/v2/accessToken",
             _json_response({"access_token": "li-token", "expires_in": 5184000}))
        .add("GET", "api.linkedin.com/v2/userinfo",
             _json_response({"sub": "abc123", "name": "Test Member"}))
    )
    with script:
        result = provider.exchange_code(code="auth-code", redirect_uri="https://x/callback")
    assert result["access_token"] == "li-token"
    assert result["external_account_id"] == "urn:li:person:abc123"
    assert result["display_name"] == "Test Member"
    assert result["expires_at"] is not None


def test_linkedin_exchange_code_missing_access_token():
    provider = LinkedInProvider()
    script = _UrlopenScript().add(
        "POST", "oauth/v2/accessToken", _json_response({})
    )
    with script, pytest.raises(SocialProviderError):
        provider.exchange_code(code="auth-code", redirect_uri="https://x/callback")


def test_linkedin_exchange_code_401_on_userinfo_needs_reconnect():
    provider = LinkedInProvider()
    script = (
        _UrlopenScript()
        .add("POST", "oauth/v2/accessToken",
             _json_response({"access_token": "li-token", "expires_in": 100}))
        .add("GET", "api.linkedin.com/v2/userinfo",
             _http_error("https://api.linkedin.com/v2/userinfo", 401))
    )
    with script, pytest.raises(SocialProviderError) as excinfo:
        provider.exchange_code(code="auth-code", redirect_uri="https://x/callback")
    assert excinfo.value.needs_reconnect is True


def test_facebook_exchange_code_success_returns_page_token():
    provider = FacebookProvider()
    script = (
        _UrlopenScript()
        .add("GET", "fb_exchange_token",
             _json_response({"access_token": "long-token", "expires_in": 5184000}))
        .add("GET", "oauth/access_token", _json_response({"access_token": "short-token"}))
        .add("GET", "me/accounts",
             _json_response({"data": [{"id": "fb-page-1", "name": "Test Page",
                                      "access_token": "page-token"}]}))
    )
    with script:
        result = provider.exchange_code(code="auth-code", redirect_uri="https://x/callback")
    assert result["access_token"] == "page-token"
    assert result["external_account_id"] == "fb-page-1"
    assert result["display_name"] == "Test Page"


def test_facebook_exchange_code_no_pages():
    provider = FacebookProvider()
    script = (
        _UrlopenScript()
        .add("GET", "fb_exchange_token", _json_response({"access_token": "long-token"}))
        .add("GET", "oauth/access_token", _json_response({"access_token": "short-token"}))
        .add("GET", "me/accounts", _json_response({"data": []}))
    )
    with script, pytest.raises(SocialProviderError) as excinfo:
        provider.exchange_code(code="auth-code", redirect_uri="https://x/callback")
    assert "No Facebook Page" in str(excinfo.value)


def test_tiktok_exchange_code_success_and_missing_open_id():
    provider = TikTokProvider()
    script = _UrlopenScript().add(
        "POST", "open.tiktokapis.com/v2/oauth/token",
        _json_response({"access_token": "tt-token", "open_id": "open-1",
                        "expires_in": 86400, "scope": "user.info.basic,video.publish"}),
    )
    with script:
        result = provider.exchange_code(code="auth-code", redirect_uri="https://x/callback")
    assert result["access_token"] == "tt-token"
    assert result["external_account_id"] == "open-1"

    script2 = _UrlopenScript().add(
        "POST", "open.tiktokapis.com/v2/oauth/token",
        _json_response({"access_token": "tt-token"}),
    )
    with script2, pytest.raises(SocialProviderError):
        provider.exchange_code(code="auth-code", redirect_uri="https://x/callback")


# ---------------------------------------------------------------------------
# Rate limits, rejected posts, network failures, retries
# ---------------------------------------------------------------------------


def test_linkedin_publish_429_is_retryable():
    provider = LinkedInProvider()
    script = _UrlopenScript().add(
        "POST", "api.linkedin.com/rest/posts",
        _http_error("https://api.linkedin.com/rest/posts", 429),
    )
    with script, pytest.raises(SocialProviderError) as excinfo:
        provider.publish_text(credentials=_creds(), caption="Hi", listing_generation={})
    assert excinfo.value.retryable is True
    assert excinfo.value.needs_reconnect is False


def test_linkedin_publish_500_is_retryable():
    provider = LinkedInProvider()
    script = _UrlopenScript().add(
        "POST", "api.linkedin.com/rest/posts",
        _http_error("https://api.linkedin.com/rest/posts", 500),
    )
    with script, pytest.raises(SocialProviderError) as excinfo:
        provider.publish_text(credentials=_creds(), caption="Hi", listing_generation={})
    assert excinfo.value.retryable is True


def test_linkedin_publish_400_is_rejected_not_retryable():
    provider = LinkedInProvider()
    script = _UrlopenScript().add(
        "POST", "api.linkedin.com/rest/posts",
        _http_error("https://api.linkedin.com/rest/posts", 400,
                    b'{"message": "Invalid post content"}'),
    )
    with script, pytest.raises(SocialProviderError) as excinfo:
        provider.publish_text(credentials=_creds(), caption="Hi", listing_generation={})
    assert excinfo.value.retryable is False
    assert excinfo.value.needs_reconnect is False


def test_linkedin_publish_network_failure_is_retryable():
    provider = LinkedInProvider()
    script = _UrlopenScript().add(
        "POST", "api.linkedin.com/rest/posts",
        urllib.error.URLError("connection refused"),
    )
    with script, pytest.raises(SocialProviderError) as excinfo:
        provider.publish_text(credentials=_creds(), caption="Hi", listing_generation={})
    assert excinfo.value.retryable is True


def test_facebook_publish_429_is_retryable_and_400_is_not():
    provider = FacebookProvider()
    script429 = _UrlopenScript().add(
        "POST", "graph.facebook.com",
        _http_error("https://graph.facebook.com/v21.0/fb-page-1/feed", 429),
    )
    with script429, pytest.raises(SocialProviderError) as excinfo:
        provider.publish_text(
            credentials=_creds("facebook", "fb-page-1"), caption="Hi", listing_generation={}
        )
    assert excinfo.value.retryable is True

    script400 = _UrlopenScript().add(
        "POST", "graph.facebook.com",
        _http_error("https://graph.facebook.com/v21.0/fb-page-1/feed", 400),
    )
    with script400, pytest.raises(SocialProviderError) as excinfo:
        provider.publish_text(
            credentials=_creds("facebook", "fb-page-1"), caption="Hi", listing_generation={}
        )
    assert excinfo.value.retryable is False


def test_orchestrator_rate_limit_failure_then_retry_succeeds(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    _make_ready(monkeypatch, "linkedin")
    u1, _ = two_users
    generation = _make_generation(u1)
    _connect_and_enable(u1)

    script = _UrlopenScript().add(
        "POST", "api.linkedin.com/rest/posts",
        [
            _http_error("https://api.linkedin.com/rest/posts", 429),
            _json_response({}, {"x-restli-id": "urn:li:share:7"}),
        ],
    )
    with script:
        first = listing_publish.publish_listing(u1, generation["id"])
        assert first["results"][0]["status"] == "failed"
        retry = listing_publish.retry_publish(u1, generation["id"], "linkedin")
        assert retry["status"] == "published"
    assert len([c for c in script.calls if "rest/posts" in c[1]]) == 2

    publications = social_db.list_publications_for_generation(u1, generation["id"])
    statuses = sorted(p["status"] for p in publications)
    assert statuses == ["failed", "published"]


def test_orchestrator_duplicate_operation_does_not_recall_provider(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    _make_ready(monkeypatch, "facebook")
    u1, _ = two_users
    generation = _make_generation(u1)
    _connect_and_enable(u1, provider="facebook", account="fb-page-1")

    script = _UrlopenScript().add(
        "POST", "graph.facebook.com/v21.0/fb-page-1/feed",
        _json_response({"id": "fb-post-9"}),
    )
    with script:
        op_id = social_db.new_operation_id()
        listing_publish.publish_listing(u1, generation["id"], operation_id=op_id)
        listing_publish.publish_listing(u1, generation["id"], operation_id=op_id)
    assert len(script.calls) == 1
    publications = social_db.list_publications_for_generation(u1, generation["id"])
    assert len(publications) == 1
    assert publications[0]["status"] == "published"


def test_orchestrator_auth_failure_marks_connection_needs_reconnect(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    _make_ready(monkeypatch, "linkedin")
    u1, _ = two_users
    generation = _make_generation(u1)
    conn = _connect_and_enable(u1)

    script = _UrlopenScript().add(
        "POST", "api.linkedin.com/rest/posts",
        _http_error("https://api.linkedin.com/rest/posts", 401),
    )
    with script:
        result = listing_publish.publish_listing(u1, generation["id"])
    assert result["results"][0]["status"] == "failed"
    assert "Reconnect" in result["results"][0]["error_summary"]
    assert social_db.get_connection_by_id(u1, conn["id"])["status"] == "needs_reconnect"


def test_partial_failure_status_reporting(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    _make_ready(monkeypatch, "linkedin", "facebook")
    u1, _ = two_users
    generation = _make_generation(u1)
    _connect_and_enable(u1, provider="linkedin", account="urn:li:person:ok")
    _connect_and_enable(u1, provider="facebook", account="fb-page-1")

    script = (
        _UrlopenScript()
        .add("POST", "api.linkedin.com/rest/posts",
             _json_response({}, {"x-restli-id": "urn:li:share:1"}))
        .add("POST", "graph.facebook.com/v21.0/fb-page-1/feed",
             _http_error("https://graph.facebook.com/v21.0/fb-page-1/feed", 500))
    )
    with script:
        result = listing_publish.publish_listing(u1, generation["id"])

    by_provider = {r["provider"]: r for r in result["results"]}
    assert by_provider["linkedin"]["status"] == "published"
    assert by_provider["linkedin"]["provider_post_url"] == (
        "https://www.linkedin.com/feed/update/urn%3Ali%3Ashare%3A1/"
    )
    assert by_provider["facebook"]["status"] == "failed"
    assert by_provider["facebook"]["error_summary"]

    publications = social_db.list_publications_for_generation(u1, generation["id"])
    stored = {p["provider"]: p for p in publications}
    assert stored["linkedin"]["provider_post_id"] == "urn:li:share:1"
    assert stored["linkedin"]["provider_post_url"].startswith("https://www.linkedin.com/")
    assert stored["facebook"]["error_summary"]

    annotated = listing_publish.annotate_publish_status(u1, [generation])
    assert annotated[0]["publish_status"] == {"linkedin": "published", "facebook": "failed"}


def test_retry_does_not_repost_successful_providers_with_mocked_transport(two_users, monkeypatch):
    _enable_encryption(monkeypatch)
    _make_ready(monkeypatch, "linkedin", "facebook")
    u1, _ = two_users
    generation = _make_generation(u1)
    _connect_and_enable(u1, provider="linkedin", account="urn:li:person:ok")
    _connect_and_enable(u1, provider="facebook", account="fb-page-1")

    script = (
        _UrlopenScript()
        .add("POST", "api.linkedin.com/rest/posts",
             _json_response({}, {"x-restli-id": "urn:li:share:1"}))
        .add("POST", "graph.facebook.com/v21.0/fb-page-1/feed",
             [_http_error("https://graph.facebook.com/v21.0/fb-page-1/feed", 500),
              _json_response({"id": "fb-post-2"})])
    )
    with script:
        listing_publish.publish_listing(u1, generation["id"])
        retry = listing_publish.retry_publish(u1, generation["id"], "facebook")
    assert retry["status"] == "published"
    linkedin_calls = [c for c in script.calls if "rest/posts" in c[1]]
    assert len(linkedin_calls) == 1  # the successful LinkedIn post was never re-sent


# ---------------------------------------------------------------------------
# Structurally unavailable channels + scaffolding
# ---------------------------------------------------------------------------


def test_instagram_publish_text_always_unavailable():
    provider = InstagramProvider()
    with pytest.raises(SocialProviderNotAvailable) as excinfo:
        provider.publish_text(credentials=_creds("instagram", "ig-1"),
                              caption="Hi", listing_generation={})
    assert "photo" in str(excinfo.value).lower()
    assert provider.readiness()["state"] in ("setup_required", "live")


def test_tiktok_publish_text_always_unavailable():
    provider = TikTokProvider()
    with pytest.raises(SocialProviderNotAvailable) as excinfo:
        provider.publish_text(credentials=_creds("tiktok", "tt-1"),
                              caption="Hi", listing_generation={})
    assert "video" in str(excinfo.value).lower()


def test_x_provider_is_scaffolding_only():
    provider = registry.get_provider("x")
    assert isinstance(provider, XProvider)
    assert provider.is_app_configured() is False
    assert provider.readiness() == {
        "state": "coming_soon",
        "ready": False,
        "reason": "X has no free posting tier (pay-per-post billing). Not enabled yet.",
    }
    assert registry.is_ready("x") is False
    for fn in (
        lambda: provider.get_authorize_url(state="s", redirect_uri="https://x/cb"),
        lambda: provider.exchange_code(code="c", redirect_uri="https://x/cb"),
        lambda: provider.publish_text(credentials={}, caption="Hi", listing_generation={}),
    ):
        with pytest.raises(SocialProviderNotAvailable):
            fn()


def test_registry_readiness_states_without_credentials(monkeypatch):
    monkeypatch.setattr(config, "LINKEDIN_CLIENT_ID", "")
    monkeypatch.setattr(config, "LINKEDIN_CLIENT_SECRET", "")
    monkeypatch.setattr(config, "FACEBOOK_APP_ID", "")
    monkeypatch.setattr(config, "FACEBOOK_APP_SECRET", "")
    monkeypatch.setattr(config, "META_APP_REVIEW_APPROVED", False)
    monkeypatch.setattr(config, "TIKTOK_CLIENT_KEY", "")
    monkeypatch.setattr(config, "TIKTOK_CLIENT_SECRET", "")
    monkeypatch.setattr(config, "TIKTOK_AUDIT_APPROVED", False)
    assert registry.readiness_for("linkedin")["state"] == "setup_required"
    assert registry.readiness_for("facebook")["state"] == "setup_required"
    assert registry.readiness_for("instagram")["state"] == "setup_required"
    assert registry.readiness_for("tiktok")["state"] == "setup_required"
    assert registry.readiness_for("x")["state"] == "coming_soon"
    assert registry.readiness_for("nope")["state"] == "unknown"


def test_facebook_readiness_gated_on_meta_review(monkeypatch):
    monkeypatch.setattr(config, "FACEBOOK_APP_ID", "id")
    monkeypatch.setattr(config, "FACEBOOK_APP_SECRET", "secret")
    monkeypatch.setattr(config, "META_APP_REVIEW_APPROVED", False)
    assert FacebookProvider().readiness()["state"] == "setup_required"
    monkeypatch.setattr(config, "META_APP_REVIEW_APPROVED", True)
    assert FacebookProvider().readiness() == {"state": "live", "ready": True, "reason": None}


def test_tiktok_readiness_gated_on_audit(monkeypatch):
    monkeypatch.setattr(config, "TIKTOK_CLIENT_KEY", "key")
    monkeypatch.setattr(config, "TIKTOK_CLIENT_SECRET", "secret")
    monkeypatch.setattr(config, "TIKTOK_AUDIT_APPROVED", False)
    assert TikTokProvider().readiness()["state"] == "setup_required"
    monkeypatch.setattr(config, "TIKTOK_AUDIT_APPROVED", True)
    readiness = TikTokProvider().readiness()
    assert readiness["state"] == "live"
    assert "video" in readiness["publish_note"]
