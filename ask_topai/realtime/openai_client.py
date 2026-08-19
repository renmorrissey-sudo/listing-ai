"""Mint ephemeral OpenAI Realtime client secrets. Permanent API key stays server-side."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from ask_topai.realtime import settings

logger = logging.getLogger(__name__)

USER_NOT_CONFIGURED = (
    "Ask TopAI Live Conversation is not configured yet. "
    "Please contact your administrator."
)
USER_UNAVAILABLE = (
    "Ask TopAI could not start a live conversation right now. Please try again."
)
USER_AUTH = (
    "Ask TopAI Live Conversation is not fully configured yet. "
    "Please contact your administrator."
)


class RealtimeSessionError(RuntimeError):
    def __init__(self, message, *, code="error", http_status=503):
        super().__init__(message)
        self.code = code
        self.user_message = message
        self.http_status = http_status


def safety_identifier(user_id) -> str:
    import hashlib

    raw = f"ask-topai:{user_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def extract_ephemeral_secret(payload: dict) -> tuple[str | None, object]:
    if not isinstance(payload, dict):
        return None, None
    value = payload.get("value")
    expires = payload.get("expires_at")
    if isinstance(value, str) and value.strip():
        return value.strip(), expires
    nested = payload.get("client_secret")
    if isinstance(nested, dict) and isinstance(nested.get("value"), str):
        return nested["value"].strip(), nested.get("expires_at") or expires
    data = payload.get("data")
    if isinstance(data, dict):
        return extract_ephemeral_secret(data)
    return None, None


def _post_client_secret(session_obj: dict, api_key: str, safety_id: str) -> dict:
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
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:400]
        except Exception:
            detail = str(exc.reason or "")
        logger.warning(
            "Ask TopAI realtime client_secret HTTP %s OPENAI_API_KEY present=%s model=%s body=%s",
            exc.code,
            "yes" if settings.key_present() else "no",
            settings.realtime_model(),
            detail,
        )
        if exc.code in {401, 403}:
            raise RealtimeSessionError(USER_AUTH, code="auth_failed", http_status=503) from exc
        if exc.code == 429:
            raise RealtimeSessionError(USER_UNAVAILABLE, code="rate_limit", http_status=503) from exc
        raise RealtimeSessionError(USER_UNAVAILABLE, code="http_error", http_status=503) from exc
    except urllib.error.URLError as exc:
        logger.warning("Ask TopAI realtime client_secret network error: %s", type(exc).__name__)
        raise RealtimeSessionError(USER_UNAVAILABLE, code="network", http_status=503) from exc


def mint_ephemeral_secret(session_obj: dict, *, user_id) -> dict:
    """Return {value, expires_at} for the browser. Never log the secret or API key."""
    if not settings.is_configured():
        logger.warning(
            "Ask TopAI realtime: OPENAI_API_KEY present=%s",
            "yes" if settings.key_present() else "no",
        )
        raise RealtimeSessionError(USER_NOT_CONFIGURED, code="not_configured", http_status=503)
    api_key = settings.openai_api_key()
    safety_id = safety_identifier(user_id)
    try:
        payload = _post_client_secret(session_obj, api_key, safety_id)
        value, expires = extract_ephemeral_secret(payload)
    except RealtimeSessionError:
        value, expires = None, None
        payload = {}
    if not value:
        slim = settings.session_config_without_transcription(
            session_obj.get("instructions") or "",
            session_obj.get("tools") or [],
        )
        payload = _post_client_secret(slim, api_key, safety_id)
        value, expires = extract_ephemeral_secret(payload)
    if not value:
        logger.warning("Ask TopAI realtime client_secret missing ephemeral value")
        raise RealtimeSessionError(USER_UNAVAILABLE, code="malformed", http_status=503)
    return {"value": value, "expires_at": expires}
