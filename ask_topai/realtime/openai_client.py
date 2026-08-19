"""OpenAI Realtime GA client: server-side WebRTC SDP handshake.

The browser posts its SDP offer to TopAI. This module forwards it to
POST https://api.openai.com/v1/realtime/calls as multipart fields `sdp` and
`session`, authenticated with the server-side OPENAI_API_KEY.

Never log OPENAI_API_KEY, ephemeral secrets, or Authorization headers.
Never return OpenAI error JSON to the browser as if it were SDP.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import urllib.error
import urllib.request

from ask_topai.realtime import settings

logger = logging.getLogger(__name__)

MODELS_URL = "https://api.openai.com/v1/models"
CALLS_TIMEOUT_SECONDS = 30
PROBE_TIMEOUT_SECONDS = 20

USER_NOT_CONFIGURED = (
    "Ask TopAI Live Conversation is not configured yet. "
    "Please contact your administrator."
)
USER_GENERIC = "TopAI could not establish the Realtime session."
USER_AUTH = "OpenAI authentication failed. Live Conversation was not started."
USER_FORBIDDEN = "OpenAI did not authorize this Realtime session. Live Conversation was not started."
USER_NOT_FOUND = "TopAI could not establish the Realtime session."
USER_QUOTA = "OpenAI API quota is unavailable. Live Conversation was not started."
USER_UNAVAILABLE = "TopAI could not establish the Realtime session."
USER_INVALID_OFFER = "TopAI could not establish the Realtime session."
USER_INVALID_ANSWER = "TopAI could not establish the Realtime session."
USER_NETWORK = "TopAI could not establish the Realtime session."

_SECRET_RE = re.compile(
    r"(?i)\b(sk-[A-Za-z0-9_-]{8,}|ek_[A-Za-z0-9_-]{8,}|Bearer\s+\S+)"
)


class RealtimeSessionError(RuntimeError):
    def __init__(
        self,
        message,
        *,
        code="error",
        http_status=503,
        stage=None,
        openai_status=None,
        openai_type=None,
        openai_code=None,
        request_id=None,
        ref=None,
    ):
        super().__init__(message)
        self.code = code
        self.user_message = message
        self.http_status = http_status
        self.stage = stage or code
        self.openai_status = openai_status
        self.openai_type = openai_type
        self.openai_code = openai_code
        self.request_id = request_id
        self.ref = ref or new_ref()


def new_ref() -> str:
    return "live-" + secrets.token_hex(4)


def safety_identifier(user_id) -> str:
    import hashlib

    raw = f"ask-topai:{user_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def sanitize_text(value, *, limit=240) -> str:
    text = "" if value is None else str(value)
    text = _SECRET_RE.sub("[redacted]", text)
    text = text.replace("\n", " ").replace("\r", " ")
    return text[:limit]


def looks_like_sdp(text) -> bool:
    if not isinstance(text, str):
        return False
    stripped = text.lstrip("\ufeff \t\r\n")
    return stripped.startswith("v=0")


def looks_like_html(text) -> bool:
    if not isinstance(text, str) or not text.strip():
        return False
    head = text.lstrip().lower()[:400]
    return head.startswith("<!doctype") or head.startswith("<html") or "<html" in head[:200]


def looks_like_json_object(text) -> bool:
    if not isinstance(text, str):
        return False
    stripped = text.lstrip()
    return stripped.startswith("{") or stripped.startswith("[")


def _header_get(headers, *names):
    if not headers:
        return None
    getter = getattr(headers, "get", None)
    lowered = {}
    if isinstance(headers, dict):
        lowered = {str(key).lower(): value for key, value in headers.items()}
    for name in names:
        value = None
        if getter:
            try:
                value = getter(name) or getter(name.lower())
            except Exception:
                value = None
        if not value and lowered:
            value = lowered.get(name.lower())
        if value:
            return str(value)
    return None


def encode_multipart(fields: dict[str, tuple[bytes, str]]) -> tuple[bytes, str]:
    """Encode multipart/form-data. fields: name -> (body_bytes, content_type)."""
    boundary = "----TopAIRealtime" + secrets.token_hex(16)
    chunks: list[bytes] = []
    for name, (body, content_type) in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("ascii"))
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"\r\n'.encode("ascii")
        )
        chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode("ascii"))
        chunks.append(body)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def parse_openai_error_body(raw: str) -> dict:
    payload = {}
    if looks_like_json_object(raw):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            err = parsed.get("error")
            if isinstance(err, dict):
                payload = {
                    "openai_type": err.get("type"),
                    "openai_code": err.get("code"),
                    "message": err.get("message"),
                }
            elif isinstance(parsed.get("message"), str):
                payload = {"message": parsed.get("message")}
    if not payload.get("message"):
        if raw and not looks_like_html(raw) and not looks_like_sdp(raw):
            payload["message"] = raw[:240]
    payload["message"] = sanitize_text(payload.get("message") or "")
    payload["openai_type"] = sanitize_text(payload.get("openai_type") or "", limit=80) or None
    payload["openai_code"] = sanitize_text(payload.get("openai_code") or "", limit=80) or None
    return payload


def user_message_for_openai_status(status) -> tuple[str, str, int]:
    """Return (user_message, code, http_status_to_browser). TopAI 401 is login, not OpenAI."""
    if status == 401:
        return USER_AUTH, "openai_401", 502
    if status == 403:
        return USER_FORBIDDEN, "openai_403", 502
    if status == 404:
        return USER_NOT_FOUND, "openai_404", 502
    if status == 429:
        return USER_QUOTA, "openai_429", 429
    if isinstance(status, int) and status >= 500:
        return USER_UNAVAILABLE, "openai_5xx", 502
    return USER_GENERIC, "openai_error", 502


def _log_openai(
    *,
    status,
    request_id,
    openai_type,
    openai_code,
    model,
    ref,
    stage,
    message="",
    extra="",
):
    logger.warning(
        "Ask TopAI realtime calls HTTP %s request_id=%s type=%s code=%s model=%s "
        "key_present=%s ref=%s stage=%s message=%s%s",
        status if status is not None else "-",
        request_id or "-",
        openai_type or "-",
        openai_code or "-",
        model,
        "yes" if settings.key_present() else "no",
        ref,
        stage,
        sanitize_text(message) or "-",
        f" {extra}" if extra else "",
    )


def _read_response_body(response) -> str:
    raw = response.read() if response is not None else b""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw or "")


def _extract_sdp_answer(body: str) -> str | None:
    if looks_like_sdp(body):
        return body.lstrip("\ufeff \t\r\n") if body.lstrip("\ufeff \t\r\n").startswith("v=0") else body
    if looks_like_json_object(body):
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict) and looks_like_sdp(parsed.get("sdp") or ""):
            return parsed.get("sdp")
    return None


def _http_error_payload(exc: urllib.error.HTTPError) -> dict:
    request_id = _header_get(exc.headers, "x-request-id", "openai-request-id", "X-Request-Id")
    try:
        raw = _read_response_body(exc)
    except Exception:
        raw = ""
    parsed = parse_openai_error_body(raw)
    return {
        "openai_status": int(getattr(exc, "code", 0) or 0),
        "request_id": request_id,
        "openai_type": parsed.get("openai_type"),
        "openai_code": parsed.get("openai_code"),
        "message": parsed.get("message") or sanitize_text(getattr(exc, "reason", "") or ""),
        "body_is_sdp": looks_like_sdp(raw),
        "body_is_html": looks_like_html(raw),
        "body_is_json": looks_like_json_object(raw),
    }


def create_webrtc_call(sdp: str, session_obj: dict, *, user_id, ref: str | None = None) -> dict:
    """POST multipart sdp+session to OpenAI Realtime GA. Return {sdp, ref, ...}."""
    ref = ref or new_ref()
    model = settings.realtime_model()
    if not looks_like_sdp(sdp):
        raise RealtimeSessionError(
            USER_INVALID_OFFER,
            code="invalid_offer",
            http_status=400,
            stage="invalid_offer",
            ref=ref,
        )
    if not settings.is_configured():
        logger.warning(
            "Ask TopAI realtime: OPENAI_API_KEY present=%s model=%s ref=%s stage=missing_key",
            "yes" if settings.key_present() else "no",
            model,
            ref,
        )
        raise RealtimeSessionError(
            USER_NOT_CONFIGURED,
            code="not_configured",
            http_status=503,
            stage="missing_key",
            ref=ref,
        )
    api_key = settings.openai_api_key()
    session_json = json.dumps(session_obj, separators=(",", ":")).encode("utf-8")
    body, content_type = encode_multipart(
        {
            "sdp": (sdp.encode("utf-8"), "application/sdp"),
            "session": (session_json, "application/json"),
        }
    )
    request = urllib.request.Request(
        settings.CALLS_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": content_type,
            "OpenAI-Safety-Identifier": safety_identifier(user_id),
            "Accept": "application/sdp, application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=CALLS_TIMEOUT_SECONDS) as response:
            status = int(getattr(response, "status", 0) or 0)
            request_id = _header_get(
                getattr(response, "headers", None),
                "x-request-id",
                "openai-request-id",
                "X-Request-Id",
            )
            location = _header_get(getattr(response, "headers", None), "location", "Location")
            raw = _read_response_body(response)
    except urllib.error.HTTPError as exc:
        details = _http_error_payload(exc)
        message, code, http_status = user_message_for_openai_status(details["openai_status"])
        _log_openai(
            status=details["openai_status"],
            request_id=details["request_id"],
            openai_type=details["openai_type"],
            openai_code=details["openai_code"],
            model=model,
            ref=ref,
            stage="openai_http",
            message=details["message"],
        )
        raise RealtimeSessionError(
            message,
            code=code,
            http_status=http_status,
            stage="openai_http",
            openai_status=details["openai_status"],
            openai_type=details["openai_type"],
            openai_code=details["openai_code"],
            request_id=details["request_id"],
            ref=ref,
        ) from exc
    except urllib.error.URLError as exc:
        _log_openai(
            status=None,
            request_id=None,
            openai_type=type(exc).__name__,
            openai_code=None,
            model=model,
            ref=ref,
            stage="openai_network",
            message=type(exc).__name__,
        )
        raise RealtimeSessionError(
            USER_NETWORK,
            code="network",
            http_status=503,
            stage="openai_network",
            ref=ref,
        ) from exc

    answer = _extract_sdp_answer(raw)
    if not (200 <= status < 300) or not answer:
        parsed = parse_openai_error_body(raw)
        _log_openai(
            status=status,
            request_id=request_id,
            openai_type=parsed.get("openai_type"),
            openai_code=parsed.get("openai_code"),
            model=model,
            ref=ref,
            stage="openai_invalid_sdp",
            message=parsed.get("message") or "non-SDP OpenAI response",
            extra="body_html=%s body_json=%s" % (
                "yes" if looks_like_html(raw) else "no",
                "yes" if looks_like_json_object(raw) else "no",
            ),
        )
        if 200 <= status < 300:
            raise RealtimeSessionError(
                USER_INVALID_ANSWER,
                code="invalid_sdp",
                http_status=502,
                stage="openai_invalid_sdp",
                openai_status=status,
                request_id=request_id,
                ref=ref,
            )
        message, code, http_status = user_message_for_openai_status(status)
        raise RealtimeSessionError(
            message,
            code=code,
            http_status=http_status,
            stage="openai_http",
            openai_status=status,
            openai_type=parsed.get("openai_type"),
            openai_code=parsed.get("openai_code"),
            request_id=request_id,
            ref=ref,
        )

    logger.info(
        "Ask TopAI realtime calls HTTP %s request_id=%s type=- code=- model=%s "
        "key_present=%s ref=%s stage=openai_sdp_ok message=-",
        status,
        request_id or "-",
        model,
        "yes" if settings.key_present() else "no",
        ref,
    )
    call_id = None
    if location and "/" in location:
        call_id = location.rstrip("/").split("/")[-1]
    return {
        "sdp": answer,
        "ref": ref,
        "call_id": call_id,
        "request_id": request_id,
        "openai_status": status,
        "model": model,
    }


# Minimal SDP used only by the authenticated diagnostics probe. Not a CRM mutation.
PROBE_SDP = (
    "v=0\r\n"
    "o=- 3906902512 3906902512 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "a=group:BUNDLE 0\r\n"
    "a=ice-options:trickle\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "a=rtcp:9 IN IP4 0.0.0.0\r\n"
    "a=ice-ufrag:topai\r\n"
    "a=ice-pwd:topaitopaitopaitopaitopai12\r\n"
    "a=fingerprint:sha-256 00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF\r\n"
    "a=setup:actpass\r\n"
    "a=mid:0\r\n"
    "a=sendrecv\r\n"
    "a=rtcp-mux\r\n"
    "a=rtpmap:111 opus/48000/2\r\n"
    "a=fmtp:111 minptime=10;useinbandfec=1\r\n"
)


def probe_openai_auth(*, user_id, ref: str | None = None) -> dict:
    """GET /v1/models to verify OPENAI_API_KEY. Never logs the key. No CRM writes."""
    ref = ref or new_ref()
    model = settings.realtime_model()
    result = {
        "ok": False,
        "ref": ref,
        "openai_status": None,
        "request_id": None,
        "openai_authenticated": False,
        "model": model,
        "model_listed": None,
        "openai_api_key_present": settings.key_present(),
        "openai_configured": settings.is_configured(),
        "openai_type": None,
        "openai_code": None,
        "message": None,
    }
    if not settings.is_configured():
        result["message"] = USER_NOT_CONFIGURED
        logger.warning(
            "Ask TopAI realtime probe HTTP - request_id=- type=- code=- model=%s "
            "key_present=%s ref=%s stage=missing_key message=-",
            model,
            "yes" if settings.key_present() else "no",
            ref,
        )
        return result
    api_key = settings.openai_api_key()
    request = urllib.request.Request(
        MODELS_URL,
        method="GET",
        headers={
            "Authorization": f"Bearer {api_key}",
            "OpenAI-Safety-Identifier": safety_identifier(user_id),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT_SECONDS) as response:
            status = int(getattr(response, "status", 0) or 0)
            request_id = _header_get(
                getattr(response, "headers", None),
                "x-request-id",
                "openai-request-id",
                "X-Request-Id",
            )
            raw = _read_response_body(response)
    except urllib.error.HTTPError as exc:
        details = _http_error_payload(exc)
        message, code, _http = user_message_for_openai_status(details["openai_status"])
        _log_openai(
            status=details["openai_status"],
            request_id=details["request_id"],
            openai_type=details["openai_type"],
            openai_code=details["openai_code"],
            model=model,
            ref=ref,
            stage="openai_auth_probe",
            message=details["message"],
        )
        result.update(
            {
                "openai_status": details["openai_status"],
                "request_id": details["request_id"],
                "openai_type": details["openai_type"],
                "openai_code": details["openai_code"],
                "message": message,
                "code": code,
            }
        )
        return result
    except urllib.error.URLError as exc:
        _log_openai(
            status=None,
            request_id=None,
            openai_type=type(exc).__name__,
            openai_code=None,
            model=model,
            ref=ref,
            stage="openai_auth_probe",
            message=type(exc).__name__,
        )
        result["message"] = USER_NETWORK
        result["code"] = "network"
        return result

    listed = None
    if looks_like_json_object(raw):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("data"), list):
            ids = {
                item.get("id")
                for item in parsed["data"]
                if isinstance(item, dict) and item.get("id")
            }
            listed = model in ids
    authenticated = 200 <= status < 300
    result.update(
        {
            "ok": authenticated,
            "openai_status": status,
            "request_id": request_id,
            "openai_authenticated": authenticated,
            "model_listed": listed,
            "message": None if authenticated else USER_GENERIC,
        }
    )
    logger.info(
        "Ask TopAI realtime probe HTTP %s request_id=%s type=- code=- model=%s "
        "key_present=%s ref=%s stage=openai_auth_probe message=authenticated=%s model_listed=%s",
        status,
        request_id or "-",
        model,
        "yes" if settings.key_present() else "no",
        ref,
        "yes" if authenticated else "no",
        "yes" if listed else "no" if listed is False else "unknown",
    )
    return result
