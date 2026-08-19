"""Ask TopAI Live Conversation: GA Realtime WebRTC handshake."""

from __future__ import annotations

import io
import json
import urllib.error
from email.message import EmailMessage

from ask_topai.realtime.openai_client import (
    USER_AUTH,
    USER_FORBIDDEN,
    USER_GENERIC,
    USER_QUOTA,
    looks_like_html,
    looks_like_sdp,
    sanitize_text,
)
from tests.test_ask_topai import _login

FAKE_KEY = "sk-proj-TESTKEYNOTREAL0001"
MIN_OFFER = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
    "c=IN IP4 0.0.0.0\r\n"
)
MIN_ANSWER = (
    "v=0\r\n"
    "o=- 1 1 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "a=setup:active\r\n"
)


def _headers(**kwargs):
    msg = EmailMessage()
    for key, value in kwargs.items():
        msg[key.replace("_", "-")] = str(value)
    return msg


class _Resp:
    def __init__(self, body, status=201, headers=None):
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status
        self.headers = headers or _headers(Content_Type="application/sdp", x_request_id="req_ok")

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


def _install_openai(monkeypatch, captured, *, calls=None, models=None, calls_error=None, models_error=None):
    def fake(req, timeout=None):
        captured.append(req)
        url = req.full_url
        if "/v1/realtime/sessions" in url or "/realtime/sessions" in url or "client_secrets" in url:
            raise AssertionError(f"deprecated OpenAI endpoint used: {url}")
        if req.get_header("OpenAI-Beta") or req.get_header("OpenAI-beta"):
            raise AssertionError("obsolete OpenAI-Beta realtime header sent")
        if url.startswith("https://api.openai.com/v1/models"):
            if models_error:
                raise models_error
            payload = models
            if payload is None:
                payload = _Resp(
                    json.dumps({"data": [{"id": "gpt-realtime-2.1"}]}),
                    200,
                    _headers(Content_Type="application/json", x_request_id="req_models"),
                )
            return payload
        if url.startswith("https://api.openai.com/v1/realtime/calls"):
            assert req.get_method() == "POST"
            ctype = req.get_header("Content-type") or ""
            assert ctype.startswith("multipart/form-data")
            assert b'name="sdp"' in (req.data or b"")
            assert b'name="session"' in (req.data or b"")
            assert b'"type":"realtime"' in (req.data or b"")
            assert b"gpt-realtime-2.1" in (req.data or b"")
            auth = req.get_header("Authorization") or ""
            assert auth.startswith("Bearer ")
            assert FAKE_KEY not in (req.data or b"").decode("utf-8", errors="replace")
            if calls_error:
                raise calls_error
            return calls or _Resp(MIN_ANSWER, 201, _headers(Content_Type="application/sdp", x_request_id="req_calls", Location="/v1/realtime/calls/rtc_test"))
        raise AssertionError(f"unexpected OpenAI URL: {url}")

    monkeypatch.setattr("ask_topai.realtime.openai_client.urllib.request.urlopen", fake)
    return captured


def _post_webrtc(client, sdp=MIN_OFFER, **headers):
    return client.post(
        "/api/ask-topai/live/webrtc",
        data=sdp,
        content_type="application/sdp",
        headers=headers,
    )


def test_looks_like_sdp_and_html():
    assert looks_like_sdp(MIN_OFFER)
    assert looks_like_sdp("\n  v=0\no=- 1 1 IN IP4 0.0.0.0\n")
    assert not looks_like_sdp("{")
    assert not looks_like_sdp("<html>nope</html>")
    assert not looks_like_sdp("")
    assert looks_like_html("<!DOCTYPE html><html></html>")
    assert looks_like_html("<html><body>login</body></html>")
    assert not looks_like_html(MIN_OFFER)


def test_sanitize_redacts_secrets():
    dirty = f"Incorrect API key provided: {FAKE_KEY} Bearer abc.def"
    cleaned = sanitize_text(dirty)
    assert FAKE_KEY not in cleaned
    assert "Bearer abc.def" not in cleaned
    assert "[redacted]" in cleaned


def test_valid_sdp_session(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _openai_ready(monkeypatch)
    captured = []
    _install_openai(monkeypatch, captured)
    res = _post_webrtc(
        app_client,
        headers={"X-Ask-TopAI-Session": "live-sdp-1", "X-Ask-TopAI-Page": "/crm/leads"},
    )
    assert res.status_code == 200
    assert res.content_type.startswith("application/sdp")
    body = res.get_data(as_text=True)
    assert looks_like_sdp(body)
    assert body.lstrip().startswith("v=0")
    assert res.headers.get("X-Ask-TopAI-Session-Id")
    assert res.headers.get("X-Ask-TopAI-Ref")
    assert FAKE_KEY not in body
    assert "OPENAI_API_KEY" not in body
    assert "Authorization" not in body
    assert captured and captured[0].full_url == "https://api.openai.com/v1/realtime/calls"


def test_webrtc_trailing_slash(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _openai_ready(monkeypatch)
    _install_openai(monkeypatch, [])
    res = app_client.post(
        "/api/ask-topai/live/webrtc/",
        data=MIN_OFFER,
        content_type="application/sdp",
    )
    assert res.status_code == 200
    assert looks_like_sdp(res.get_data(as_text=True))


def test_missing_openai_api_key(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _openai_missing(monkeypatch)
    called = []

    def boom(req, timeout=None):
        called.append(req)
        raise AssertionError("OpenAI should not be called without a key")

    monkeypatch.setattr("ask_topai.realtime.openai_client.urllib.request.urlopen", boom)
    res = _post_webrtc(app_client)
    assert res.status_code == 503
    data = res.get_json()
    assert data["ok"] is False
    assert data["code"] == "not_configured"
    assert data["openai_api_key_present"] is False
    assert data["model"] == "gpt-realtime-2.1"
    assert data["ref"]
    assert called == []
    assert FAKE_KEY not in res.get_data(as_text=True)


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
        calls_error=_http_error(
            "https://api.openai.com/v1/realtime/calls",
            status,
            error_body,
            x_request_id=f"req_{status}",
            Content_Type="application/json",
        ),
    )
    res = _post_webrtc(app_client)
    assert res.status_code == expected_http
    assert res.content_type.startswith("application/json")
    data = res.get_json()
    body = res.get_data(as_text=True)
    assert data["ok"] is False
    assert data["code"] == expected_code
    assert data["openai_status"] == status
    assert data["error"] == expected_message
    assert not looks_like_sdp(body)
    assert FAKE_KEY not in body
    assert "ek_" not in body
    assert "Bearer " not in body


def test_openai_401(app_client, two_users, monkeypatch):
    _assert_openai_status(app_client, two_users, monkeypatch, 401, "openai_401", USER_AUTH, 502)


def test_openai_403(app_client, two_users, monkeypatch):
    _assert_openai_status(app_client, two_users, monkeypatch, 403, "openai_403", USER_FORBIDDEN, 502)


def test_openai_429(app_client, two_users, monkeypatch):
    _assert_openai_status(app_client, two_users, monkeypatch, 429, "openai_429", USER_QUOTA, 429)


def test_openai_5xx(app_client, two_users, monkeypatch):
    _assert_openai_status(app_client, two_users, monkeypatch, 503, "openai_5xx", USER_GENERIC, 502)


def test_openai_non_sdp_success_is_structured_error(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _openai_ready(monkeypatch)
    _install_openai(
        monkeypatch,
        [],
        calls=_Resp(
            json.dumps({"error": {"message": f"not sdp {FAKE_KEY}", "type": "invalid_request_error"}}),
            201,
            _headers(Content_Type="application/json", x_request_id="req_json"),
        ),
    )
    res = _post_webrtc(app_client)
    assert res.status_code == 502
    data = res.get_json()
    body = res.get_data(as_text=True)
    assert data["code"] == "invalid_sdp"
    assert not looks_like_sdp(body)
    assert FAKE_KEY not in body
    assert data["error"] == USER_GENERIC


def test_openai_html_is_not_returned_as_sdp(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _openai_ready(monkeypatch)
    _install_openai(
        monkeypatch,
        [],
        calls=_Resp("<html><body>OpenAI error</body></html>", 200, _headers(Content_Type="text/html")),
    )
    res = _post_webrtc(app_client)
    assert res.status_code == 502
    data = res.get_json()
    body = res.get_data(as_text=True)
    assert data["ok"] is False
    assert not looks_like_sdp(body)
    assert "<html" not in body.lower() or data["error"] == USER_GENERIC


def test_invalid_backend_offer_never_calls_openai(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _openai_ready(monkeypatch)
    called = []

    def boom(req, timeout=None):
        called.append(req)
        raise AssertionError("should not call OpenAI")

    monkeypatch.setattr("ask_topai.realtime.openai_client.urllib.request.urlopen", boom)
    res = _post_webrtc(app_client, sdp='{"error":"nope"}')
    assert res.status_code == 400
    assert res.get_json()["code"] == "invalid_offer"
    assert called == []


def test_html_offer_rejected(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _openai_ready(monkeypatch)
    res = app_client.post(
        "/api/ask-topai/live/webrtc",
        data="<!DOCTYPE html><html><body>login</body></html>",
        content_type="text/html",
    )
    assert res.status_code in {400, 415}
    data = res.get_json()
    assert data["ok"] is False
    assert not looks_like_sdp(res.get_data(as_text=True))


def test_json_content_type_rejected(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _openai_ready(monkeypatch)
    res = app_client.post(
        "/api/ask-topai/live/webrtc",
        data=json.dumps({"sdp": MIN_OFFER}),
        content_type="application/json",
    )
    assert res.status_code == 415
    assert res.get_json()["ok"] is False


def test_authentication_redirect_is_json_not_sdp(app_client):
    res = _post_webrtc(app_client)
    assert res.status_code == 401
    assert res.content_type.startswith("application/json")
    data = res.get_json()
    body = res.get_data(as_text=True)
    assert "Please log in" in (data.get("error") or "")
    assert not looks_like_sdp(body)
    assert "<html" not in body.lower()
    assert res.status_code != 302


def test_widget_never_sets_remote_description_with_invalid_sdp(app_client, two_users):
    u1, _ = two_users
    _login(app_client, u1)
    html = app_client.get("/crm/leads").get_data(as_text=True)
    idx = html.index("setRemoteDescription")
    before = html[:idx]
    assert "looksLikeSdp(answer.sdp)" in before
    assert "readWebrtcAnswer" in before
    assert "WebRTC connection failed" not in html
    assert "Authorization: 'Bearer '" not in html
    assert "client_secret" not in html
    assert "endLive(!!live.sessionId)" in html
    assert "setConn('Listening'" in html or 'setConn("Listening"' in html
    assert "connectionState" in html
    assert "iceConnectionState" in html
    assert "signalingState" in html


def test_diagnostics_missing_key(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _openai_missing(monkeypatch)
    res = app_client.get("/api/ask-topai/live/diagnostics")
    assert res.status_code == 503
    data = res.get_json()
    assert data["openai_api_key_present"] is False
    assert data["realtime_model"] == "gpt-realtime-2.1"
    assert data["openai_authenticated"] is False
    assert FAKE_KEY not in res.get_data(as_text=True)


def test_diagnostics_auth_success(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _openai_ready(monkeypatch)
    _install_openai(monkeypatch, [])
    res = app_client.get("/api/ask-topai/live/diagnostics")
    assert res.status_code == 200
    data = res.get_json()
    assert data["openai_authenticated"] is True
    assert data["realtime_model"] == "gpt-realtime-2.1"
    assert data["model_listed"] is True
    assert FAKE_KEY not in res.get_data(as_text=True)


def test_diagnostics_calls_probe_sdp(app_client, two_users, monkeypatch):
    u1, _ = two_users
    _login(app_client, u1)
    _openai_ready(monkeypatch)
    captured = []
    _install_openai(monkeypatch, captured)
    res = app_client.get("/api/ask-topai/live/diagnostics?probe=calls")
    assert res.status_code == 200
    data = res.get_json()
    assert data["calls_probe"]["ok"] is True
    assert data["calls_probe"]["sdp_answer"] is True
    assert any("/v1/realtime/calls" in req.full_url for req in captured)
    assert FAKE_KEY not in res.get_data(as_text=True)


def test_no_deprecated_sessions_endpoint_in_source():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    banned = (
        "/v1/realtime/sessions",
        "OpenAI-Beta: realtime=v1",
        "mint_ephemeral_secret",
        "CLIENT_SECRETS_URL",
    )
    for path in (root / "ask_topai").rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".html", ".js"}:
            continue
        text = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{token} still in {path}"
    widget = (root / "templates" / "ask_topai_widget.html").read_text(encoding="utf-8")
    for token in banned + ("https://api.openai.com/v1/realtime/calls", "WebRTC connection failed"):
        assert token not in widget
