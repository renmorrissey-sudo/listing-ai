"""Ask TopAI Live Conversation: ephemeral client secret minting and staged errors."""

from __future__ import annotations

import io
import json
import urllib.error
from email.message import EmailMessage
from pathlib import Path

from ask_topai.realtime.openai_client import (
    USER_AUTH,
    USER_CONNECT,
    USER_NOT_CONFIGURED,
    USER_QUOTA,
    extract_ephemeral_secret,
    sanitize_text,
)
from tests.test_ask_topai import _login

FAKE_KEY = "sk-proj-TESTKEYNOTREAL0001"
EK = "ek_test_ephemeral_not_a_real_key"


def _headers(**kwargs):
    msg = EmailMessage()
    for key, value in kwargs.items():
        msg[key.replace("_", "-")] = str(value)
    return msg


class _Resp:
    def __init__(self, body, status=200, headers=None):
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status
        self.headers = headers or _headers(Content_Type="application/json", x_request_id="req_ok")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(url, code, body, **headers):
    raw = body.encode("utf-8") if isinstance(body, str) else body
    return urllib.error.HTTPError(url, code, "error", _headers(**headers), io.BytesIO(raw))


def _openai_ready(monkeypatch, key=FAKE_KEY):
    monkeypatch.setattr("ask_topai.realtime.settings.openai_api_key", lambda: key)
    monkeypatch.setattr("ask_topai.realtime.settings.is_configured", lambda: True)
    monkeypatch.setattr("ask_topai.realtime.settings.key_present", lambda: True)


def _openai_missing(monkeypatch):
    monkeypatch.setattr("ask_topai.realtime.settings.openai_api_key", lambda: "")
    monkeypatch.setattr("ask_topai.realtime.settings.is_configured", lambda: False)
    monkeypatch.setattr("ask_topai.realtime.settings.key_present", lambda: False)


def _install_openai(monkeypatch, captured, *, secrets=None, models=None, secrets_error=None, models_error=None):
    def fake(req, timeout=None):
        captured.append(req)
        url = req.full_url
        if "/v1/realtime/sessions" in url or "/realtime/sessions" in url:
            raise AssertionError(f"deprecated OpenAI endpoint used: {url}")
        if req.get_header("OpenAI-Beta") or req.get_header("OpenAI-beta"):
            raise AssertionError("obsolete OpenAI-Beta realtime header sent")
        if url.startswith("https://api.openai.com/v1/models"):
            if models_error:
                raise models_error
            return models or _Resp(
                json.dumps({"data": [{"id": "gpt-realtime-2.1"}]}),
                200,
                _headers(Content_Type="application/json", x_request_id="req_models"),
            )
        if url.startswith("https://api.openai.com/v1/realtime/client_secrets"):
            assert req.get_method() == "POST"
            body = (req.data or b"").decode("utf-8")
            payload = json.loads(body)
            assert payload["session"]["type"] == "realtime"
            assert payload["session"]["model"] == "gpt-realtime-2.1"
            auth = req.get_header("Authorization") or ""
            assert auth.startswith("Bearer ")
            if secrets_error:
                raise secrets_error
            return secrets or _Resp(
                json.dumps({"value": EK, "expires_at": 1_700_000_000, "session": payload["session"]}),
                200,
                _headers(Content_Type="application/json", x_request_id="req_secret"),
            )
        raise AssertionError(f"unexpected OpenAI URL: {url}")

    monkeypatch.setattr("ask_topai.realtime.openai_client.urllib.request.urlopen", fake)
    return captured


def test_extract_ephemeral_secret_shapes():
    assert extract_ephemeral_secret({"value": EK})[0] == EK
    assert extract_ephemeral_secret({"client_secret": {"value": EK, "expires_at": 9}})[0] == EK
    assert extract_ephemeral_secret({"value": "sk-nope"})[0] is None


def test_sanitize_redacts_secrets():
    dirty = f"Incorrect API key provided: {FAKE_KEY} Bearer abc.def"
    cleaned = sanitize_text(dirty)
    assert FAKE_KEY not in cleaned
    assert "Bearer abc.def" not in cleaned
    assert "[redacted]" in cleaned


def test_ephemeral_client_secret_success(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _openai_ready(monkeypatch)
    captured = []
    _install_openai(monkeypatch, captured)
    res = app_client.post("/api/ask-topai/live/session", json={"context": {"page": "/crm/leads"}})
    assert res.status_code == 200
    data = res.get_json()
    body = res.get_data(as_text=True)
    assert data["client_secret"]["value"] == EK
    assert data["ref"]
    assert FAKE_KEY not in body
    assert "OPENAI_API_KEY" not in body
    assert captured[0].full_url == "https://api.openai.com/v1/realtime/client_secrets"


def test_missing_openai_api_key(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _openai_missing(monkeypatch)
    called = []

    def boom(req, timeout=None):
        called.append(req)
        raise AssertionError("OpenAI should not be called without a key")

    monkeypatch.setattr("ask_topai.realtime.openai_client.urllib.request.urlopen", boom)
    res = app_client.post("/api/ask-topai/live/session", json={})
    assert res.status_code == 503
    data = res.get_json()
    assert data["code"] == "not_configured"
    assert data["error"] == USER_NOT_CONFIGURED
    assert data["stage"] == "missing_key"
    assert data["ref"]
    assert called == []


def _assert_openai_status(app_client, two_users, monkeypatch, status, expected_code, expected_message, expected_http):
    u1, _ = two_users
    _login(app_client, u1)
    _openai_ready(monkeypatch)
    error_body = json.dumps(
        {
            "error": {
                "message": f"Incorrect API key provided: {FAKE_KEY}",
                "type": "invalid_request_error",
                "code": "invalid_api_key",
            }
        }
    )
    _install_openai(
        monkeypatch,
        [],
        secrets_error=_http_error(
            "https://api.openai.com/v1/realtime/client_secrets",
            status,
            error_body,
            x_request_id=f"req_{status}",
            Content_Type="application/json",
        ),
    )
    res = app_client.post("/api/ask-topai/live/session", json={})
    assert res.status_code == expected_http
    data = res.get_json()
    body = res.get_data(as_text=True)
    assert data["ok"] is False
    assert data["code"] == expected_code
    assert data["openai_status"] == status
    assert data["error"] == expected_message
    assert data["ref"]
    assert FAKE_KEY not in body
    assert "Bearer " not in body
    assert "client_secret" not in data


def test_invalid_api_key_401(app_client, two_users, monkeypatch):
    _assert_openai_status(app_client, two_users, monkeypatch, 401, "openai_401", USER_AUTH, 502)


def test_openai_403(app_client, two_users, monkeypatch):
    _assert_openai_status(app_client, two_users, monkeypatch, 403, "openai_403", USER_AUTH, 502)


def test_openai_429(app_client, two_users, monkeypatch):
    _assert_openai_status(app_client, two_users, monkeypatch, 429, "openai_429", USER_QUOTA, 429)


def test_openai_5xx(app_client, two_users, monkeypatch):
    _assert_openai_status(app_client, two_users, monkeypatch, 503, "openai_5xx", USER_CONNECT, 502)


def test_malformed_openai_response(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _openai_ready(monkeypatch)
    _install_openai(
        monkeypatch,
        [],
        secrets=_Resp(json.dumps({"session": {"type": "realtime"}}), 200),
    )
    res = app_client.post("/api/ask-topai/live/session", json={})
    assert res.status_code == 502
    data = res.get_json()
    assert data["code"] == "malformed"
    assert data["error"] == USER_CONNECT
    assert "client_secret" not in data


def test_session_unauthenticated_is_json_not_html(app_client):
    res = app_client.post("/api/ask-topai/live/session", json={})
    assert res.status_code == 401
    assert res.content_type.startswith("application/json")
    data = res.get_json()
    assert "Please log in" in (data.get("error") or "")
    assert "<html" not in res.get_data(as_text=True).lower()


def test_bundle_has_agents_sdk_and_no_permanent_key(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    res = app_client.get("/static/ask_topai/realtime.js")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    assert "AskTopAIRealtime" in body
    assert "RealtimeAgent/RealtimeSession" in body
    assert "OPENAI_API_KEY" not in body
    assert "sk-proj-" not in body
    assert FAKE_KEY not in body


def test_widget_uses_sdk_connect_and_clean_disconnect(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    html = app_client.get("/crm/leads").get_data(as_text=True)
    assert "AskTopAIRealtime.connect" in html
    assert "live.realtime.close" in html
    assert "endLive(!!live.sessionId)" in html
    assert "Microphone access is blocked." in html
    assert "Could not establish the realtime audio connection." in html
    assert "setRemoteDescription" not in html
    assert "RTCPeerConnection" not in html


def test_no_permanent_key_on_frontend_pages(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    html = app_client.get("/crm/leads").get_data(as_text=True)
    assert "OPENAI_API_KEY" not in html
    assert FAKE_KEY not in html
    assert "sk-proj-" not in html


def test_diagnostics_missing_key(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _openai_missing(monkeypatch)
    res = app_client.get("/api/ask-topai/live/diagnostics")
    assert res.status_code == 503
    data = res.get_json()
    assert data["openai_api_key_present"] is False
    assert data["realtime_model"] == "gpt-realtime-2.1"
    assert data["client_secret_created"] is False


def test_diagnostics_secret_probe(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _openai_ready(monkeypatch)
    captured = []
    _install_openai(monkeypatch, captured)
    res = app_client.get("/api/ask-topai/live/diagnostics?probe=secret")
    assert res.status_code == 200
    data = res.get_json()
    assert data["openai_authenticated"] is True
    assert data["client_secret_created"] is True
    assert data["client_secret_prefix"] == "ek_"
    assert EK not in res.get_data(as_text=True)
    assert FAKE_KEY not in res.get_data(as_text=True)
    assert any("client_secrets" in req.full_url for req in captured)


def test_no_deprecated_sessions_endpoint_in_source():
    root = Path(__file__).resolve().parents[1]
    banned = ("/v1/realtime/sessions", "OpenAI-Beta: realtime=v1")
    for path in (root / "ask_topai").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{token} still in {path}"
    widget = (root / "templates" / "ask_topai_widget.html").read_text(encoding="utf-8")
    assert "/api/ask-topai/live/webrtc" not in widget
    assert "WebRTC connection failed" not in widget
    assert "Ask TopAI could not start a live conversation" not in widget
