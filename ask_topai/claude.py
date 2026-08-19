"""Server-side Anthropic client for Ask TopAI. Never logs or returns API keys."""

from __future__ import annotations

import logging

from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    Anthropic,
    RateLimitError,
)

import config

logger = logging.getLogger(__name__)

HEALTH_PROMPT = "Reply with the single word pong and nothing else."
REQUEST_TIMEOUT_SECONDS = 60.0
HEALTH_MAX_TOKENS = 32
DEFAULT_MAX_TOKENS = 4096

USER_NOT_CONFIGURED = (
    "Ask TopAI is not fully configured yet. Please contact your administrator."
)
USER_AUTH_FAILED = (
    "Ask TopAI is not fully configured yet. Please contact your administrator."
)
USER_UNAVAILABLE = (
    "Claude is temporarily unavailable. Your request was not executed. Please try again."
)
USER_RATE_LIMIT = (
    "Ask TopAI is temporarily busy. Your request was not executed. Please try again shortly."
)
USER_TIMEOUT = (
    "Claude is temporarily unavailable. Your request was not executed. Please try again."
)
USER_NETWORK = (
    "Ask TopAI could not reach Claude. Your CRM data was not changed."
)
USER_MALFORMED = (
    "Ask TopAI could not process that request. Your CRM data was not changed."
)
USER_GENERIC = (
    "Ask TopAI had a problem understanding that request. Your CRM data was not changed."
)


class AskTopAIModelError(RuntimeError):
    """Claude is unavailable or returned an unusable response."""

    def __init__(self, message, *, code="error"):
        super().__init__(message)
        self.code = code
        self.user_message = message


def key_configured() -> bool:
    raw = (config.ANTHROPIC_API_KEY or "").strip()
    return bool(raw) and not raw.startswith("test-")


def model_name() -> str:
    return (config.ASK_TOPAI_MODEL or "").strip() or "claude-sonnet-5"


def _key_present_log() -> str:
    return "yes" if bool((config.ANTHROPIC_API_KEY or "").strip()) else "no"


def build_client() -> Anthropic:
    if not key_configured():
        logger.warning(
            "Ask TopAI Anthropic client: ANTHROPIC_API_KEY configured: %s",
            _key_present_log(),
        )
        raise AskTopAIModelError(USER_NOT_CONFIGURED, code="not_configured")
    logger.info(
        "Ask TopAI Anthropic client: ANTHROPIC_API_KEY configured: yes model=%s",
        model_name(),
    )
    return Anthropic(api_key=config.ANTHROPIC_API_KEY, timeout=REQUEST_TIMEOUT_SECONDS)


def _create_kwargs(*, max_tokens: int, system=None, tools=None, messages: list):
    """Sonnet 5 rejects non-default sampling params (temperature/top_p/top_k → HTTP 400)."""
    kwargs = {
        "model": model_name(),
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system is not None:
        kwargs["system"] = system
    if tools:
        kwargs["tools"] = tools
    return kwargs


def create_message(*, messages: list, system=None, tools=None, max_tokens=DEFAULT_MAX_TOKENS):
    client = build_client()
    kwargs = _create_kwargs(
        max_tokens=max_tokens,
        system=system,
        tools=tools,
        messages=messages,
    )
    # Adaptive thinking is on by default for Sonnet 5; disable for CRM tool-use latency.
    try:
        return client.messages.create(
            **kwargs,
            extra_body={"thinking": {"type": "disabled"}},
        )
    except TypeError:
        return client.messages.create(**kwargs)


def _status_code(exc) -> int | None:
    code = getattr(exc, "status_code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def _error_body(exc) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error") if isinstance(body.get("error"), dict) else body
        text = str(err.get("message") or err.get("type") or body) if isinstance(err, dict) else str(body)
        return text[:400]
    message = getattr(exc, "message", None) or str(exc or "")
    return str(message)[:400]


def classify_exception(exc: BaseException) -> AskTopAIModelError:
    if isinstance(exc, AskTopAIModelError):
        return exc
    if isinstance(exc, RateLimitError):
        logger.warning("Ask TopAI Claude rate limited")
        return AskTopAIModelError(USER_RATE_LIMIT, code="rate_limit")
    if isinstance(exc, APITimeoutError):
        logger.warning("Ask TopAI Claude timeout")
        return AskTopAIModelError(USER_TIMEOUT, code="timeout")
    if isinstance(exc, APIConnectionError):
        logger.warning("Ask TopAI Claude network/DNS failure: %s", type(exc).__name__)
        return AskTopAIModelError(USER_NETWORK, code="network")
    if isinstance(exc, APIStatusError):
        code = _status_code(exc)
        body = _error_body(exc)
        lowered = body.lower()
        logger.warning(
            "Ask TopAI Claude HTTP error status=%s body=%s ANTHROPIC_API_KEY configured: %s model=%s",
            code,
            body,
            _key_present_log(),
            model_name(),
        )
        if code in {401, 403}:
            return AskTopAIModelError(USER_AUTH_FAILED, code="auth_failed")
        if code == 404 or "not_found" in lowered:
            return AskTopAIModelError(USER_UNAVAILABLE, code="model_unavailable")
        if code == 429:
            return AskTopAIModelError(USER_RATE_LIMIT, code="rate_limit")
        if code in {408, 503, 504, 529}:
            return AskTopAIModelError(USER_UNAVAILABLE, code="unavailable")
        if code == 400:
            return AskTopAIModelError(USER_MALFORMED, code="malformed")
        return AskTopAIModelError(USER_NETWORK, code="http_error")
    logger.exception("Ask TopAI Claude application exception")
    return AskTopAIModelError(USER_GENERIC, code="exception")


def ping() -> dict:
    """Minimal Claude round-trip. No CRM reads or writes. Safe for deploy checks."""
    configured = key_configured()
    present = bool((config.ANTHROPIC_API_KEY or "").strip())
    logger.info("Ask TopAI health: ANTHROPIC_API_KEY configured: %s", "yes" if present else "no")
    result = {
        "ok": False,
        "configured": configured,
        "model": model_name(),
        "code": "not_configured",
        "message": USER_NOT_CONFIGURED,
    }
    if not configured:
        return result
    try:
        response = create_message(
            messages=[{"role": "user", "content": HEALTH_PROMPT}],
            max_tokens=HEALTH_MAX_TOKENS,
        )
        text = ""
        for block in getattr(response, "content", None) or []:
            btype = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else "")
            if btype == "text":
                text += (block.get("text") if isinstance(block, dict) else getattr(block, "text", "")) or ""
        text = (text or "").strip()
        if not text:
            result["code"] = "malformed_response"
            result["message"] = USER_MALFORMED
            return result
        result.update(
            {
                "ok": True,
                "code": "ok",
                "message": "Claude connectivity succeeded.",
            }
        )
        return result
    except Exception as exc:
        mapped = classify_exception(exc)
        result["code"] = mapped.code
        result["message"] = mapped.user_message
        return result


def _main():
    result = ping()
    print(f"ANTHROPIC_API_KEY configured: {_key_present_log()}")
    print(f"model={result.get('model')}")
    print(f"ok={result.get('ok')} code={result.get('code')}")
    print(result.get("message") or "")
    raise SystemExit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    _main()
