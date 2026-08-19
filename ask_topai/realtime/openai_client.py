"""Mint ephemeral OpenAI Realtime client secrets. Permanent API key stays server-side.

Browser voice uses the official Agents SDK (RealtimeAgent / RealtimeSession).
Flask only calls POST https://api.openai.com/v1/realtime/client_secrets.

Never log OPENAI_API_KEY, ephemeral secrets, or Authorization headers.
Never send the permanent API key to the browser.
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
MINT_TIMEOUT_SECONDS = 20
PROBE_TIMEOUT_SECONDS = 20

USER_NOT_CONFIGURED = "Ask TopAI is not configured for live conversation."
USER_AUTH = "OpenAI authentication failed."
USER_FORBIDDEN = "OpenAI authentication failed."
USER_QUOTA = "OpenAI API quota is unavailable."
USER_CONNECT = "Could not establish the realtime audio connection."
USER_NETWORK = "Network connection to Ask TopAI was interrupted."

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


def extract_ephemeral_secret(payload: dict) -> tuple[str | None, object]:
    if not isinstance(payload, dict):
        return None, None
    value = payload.get("value")
    expires = payload.get("expires_at")
    if isinstance(value, str) and value.strip().startswith("ek_"):
        return value.strip(), expires
    nested = payload.get("client_secret")
    if isinstance(nested, dict):
        nested_value = nested.get("value")
        if isinstance(nested_value, str) and nested_value.strip().startswith("ek_"):
            return nested_value.strip(), nested.get("expires_at") or expires
    data = payload.get("data")
    if isinstance(data, dict):
        return extract_ephemeral_secret(data)
    return None, None


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


def looks_like_json_object(text) -> bool:
    if not isinstance(text, str):
        return False
    stripped = text.lstrip()
    return stripped.startswith("{") or stripped.startswith("[")


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
    if not payload.get("message") and raw and not raw.lstrip().startswith("<"):
        payload["message"] = raw[:240]
    payload["message"] = sanitize_text(payload.get("message") or "")
    payload["openai_type"] = sanitize_text(payload.get("openai_type") or "", limit=80) or None
    payload["openai_code"] = sanitize_text(payload.get("openai_code") or "", limit=80) or None
    return payload


def user_message_for_openai_status(status) -> tuple[str, str, int, str]:
    """Return (user_message, code, http_status_to_browser, stage)."""
    if status == 401:
        return USER_AUTH, "openai_401", 502, "openai_auth"
    if status == 403:
        return USER_FORBIDDEN, "openai_403", 502, "openai_auth"
    if status == 404:
        return USER_CONNECT, "openai_404", 502, "model_access"
    if status == 429:
        return USER_QUOTA, "openai_429", 429, "openai_quota"
    if isinstance(status, int) and status >= 500:
        return USER_CONNECT, "openai_5xx", 502, "openai_unavailable"
    return USER_CONNECT, "openai_error", 502, "client_secret"


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
        "Ask TopAI realtime client_secrets HTTP %s request_id=%s type=%s code=%s model=%s "
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
    }


def _raise_http(details: dict, ref: str, model: str, stage: str = "client_secret"):
    message, code, http_status, mapped_stage = user_message_for_openai_status(details["openai_status"])
    _log_openai(
        status=details["openai_status"],
        request_id=details.get("request_id"),
        openai_type=details.get("openai_type"),
        openai_code=details.get("openai_code"),
        model=model,
        ref=ref,
        stage=mapped_stage or stage,
        message=details.get("message"),
    )
    raise RealtimeSessionError(
        message,
        code=code,
        http_status=http_status,
        stage=mapped_stage or stage,
        openai_status=details["openai_status"],
        openai_type=details.get("openai_type"),
        openai_code=details.get("openai_code"),
        request_id=details.get("request_id"),
        ref=ref,
    )


def _post_client_secret(session_obj: dict, api_key: str, safety_id: str) -> tuple[int, dict, str | None]:
    body = json.dumps({"session": session_obj}).encode("utf-8")
    request = urllib.request.Request(
        settings.CLIENT_SECRETS_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "OpenAI-Safety-Identifier": safety_id,
        },
    )
    with urllib.request.urlopen(request, timeout=MINT_TIMEOUT_SECONDS) as response:
        status = int(getattr(response, "status", 0) or 0)
        request_id = _header_get(
            getattr(response, "headers", None),
            "x-request-id",
            "openai-request-id",
            "X-Request-Id",
        )
        raw = _read_response_body(response)
        payload = json.loads(raw) if looks_like_json_object(raw) else {}
        return status, payload if isinstance(payload, dict) else {}, request_id


def mint_ephemeral_secret(session_obj: dict, *, user_id, ref: str | None = None) -> dict:
    """Return {value, expires_at, ref, ...}. Never log the secret or API key."""
    ref = ref or new_ref()
    model = settings.realtime_model()
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
    safety_id = safety_identifier(user_id)
    attempts = [session_obj]
    if session_obj.get("audio", {}).get("input", {}).get("transcription"):
        attempts.append(settings.session_config_without_transcription(
            session_obj.get("instructions") or "",
            session_obj.get("tools") or [],
        ))
    last_status = None
    last_request_id = None
    last_payload: dict = {}
    try:
        for attempt in attempts:
            try:
                status, payload, request_id = _post_client_secret(attempt, api_key, safety_id)
            except urllib.error.HTTPError as exc:
                details = _http_error_payload(exc)
                last_status = details["openai_status"]
                last_request_id = details.get("request_id")
                if details["openai_status"] == 400 and attempt is attempts[0] and len(attempts) > 1:
                    _log_openai(
                        status=400,
                        request_id=details.get("request_id"),
                        openai_type=details.get("openai_type"),
                        openai_code=details.get("openai_code"),
                        model=model,
                        ref=ref,
                        stage="client_secret",
                        message="retrying without transcription",
                    )
                    continue
                _raise_http(details, ref, model)
            value, expires = extract_ephemeral_secret(payload)
            last_status = status
            last_request_id = request_id
            last_payload = payload
            if value:
                logger.info(
                    "Ask TopAI realtime client_secrets HTTP %s request_id=%s type=- code=- model=%s "
                    "key_present=%s ref=%s stage=client_secret message=mint_ok",
                    status,
                    request_id or "-",
                    model,
                    "yes" if settings.key_present() else "no",
                    ref,
                )
                return {
                    "value": value,
                    "expires_at": expires,
                    "ref": ref,
                    "openai_status": status,
                    "request_id": request_id,
                    "model": model,
                }
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
    parsed = parse_openai_error_body(json.dumps(last_payload) if last_payload else "")
    _log_openai(
        status=last_status,
        request_id=last_request_id,
        openai_type=parsed.get("openai_type"),
        openai_code=parsed.get("openai_code"),
        model=model,
        ref=ref,
        stage="client_secret",
        message="ephemeral secret missing from OpenAI response",
    )
    raise RealtimeSessionError(
        USER_CONNECT,
        code="malformed",
        http_status=502,
        stage="client_secret",
        openai_status=last_status,
        request_id=last_request_id,
        ref=ref,
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
        message, code, _http, stage = user_message_for_openai_status(details["openai_status"])
        _log_openai(
            status=details["openai_status"],
            request_id=details["request_id"],
            openai_type=details["openai_type"],
            openai_code=details["openai_code"],
            model=model,
            ref=ref,
            stage=stage,
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
            stage="openai_network",
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
            "message": None if authenticated else USER_CONNECT,
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
