"""Twilio SMS provider with safe, mapped user-facing errors."""

import base64
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

import config

logger = logging.getLogger(__name__)

# Patterns that must never appear in client-facing or casually logged strings.
_SECRET_PATTERNS = [
    re.compile(r"(?i)(auth[_ ]?token|api[_ ]?key[_ ]?secret|password)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(SK[0-9a-f]{10,})\b"),
    re.compile(r"(?i)\b(AC[0-9a-f]{32})\b"),
    re.compile(r"(?i)\b(MG[0-9a-f]{32})\b"),  # messaging service — ok to show configured yes/no, not full SID in errors
]


def redact_secrets(text: str) -> str:
    value = str(text or "")
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[redacted]", value)
    # Extra pass for common token-like assignments
    value = re.sub(r"(?i)(sk|ac|auth|token|secret|sid)[=:\s]+\S+", r"\1=[redacted]", value)
    return value


class SmsProviderError(Exception):
    """Safe SMS failure for API responses. Secrets never belong on this object."""

    def __init__(
        self,
        message,
        *,
        status_code=None,
        provider_code=None,
        more_info=None,
        message_sid=None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.provider_code = provider_code
        self.more_info = more_info  # server-side / logs only
        self.message_sid = message_sid

    def to_public_dict(self):
        payload = {
            "error": str(self),
            "provider": "twilio",
        }
        if self.provider_code is not None:
            payload["provider_code"] = self.provider_code
        if self.status_code is not None:
            payload["http_status"] = self.status_code
        if self.message_sid:
            payload["provider_message_id"] = self.message_sid
        return payload


def map_twilio_error(provider_code, raw_message="", http_status=None):
    """
    Return (user_facing_detail, a2p_hint).
    user_facing_detail is the clause after 'Twilio error N:'.
    """
    code = None
    try:
        if provider_code is not None and str(provider_code).strip() != "":
            code = int(provider_code)
    except (TypeError, ValueError):
        code = None

    lowered = str(raw_message or "").lower()

    if code == 30034:
        return (
            "This Twilio number is not registered with an approved US A2P 10DLC campaign.",
            "blocked_unregistered",
        )
    if code == 21608:
        return (
            "This destination number is not verified for the current Twilio account or account restrictions.",
            None,
        )
    if code in (20003, 20005, 20008) or http_status == 401:
        return ("Twilio rejected the configured credentials.", None)
    if code in (21211, 21401, 21614, 21201):
        return ("Enter a valid mobile phone number, including country code.", None)
    if code in (21606, 21610):
        # 21610 = unsubscribed recipient — still a clear destination/compliance issue
        if code == 21610:
            return ("This recipient has opted out or cannot receive messages.", None)
        return ("Enter a valid mobile phone number, including country code.", None)
    if code in (30002, 30005, 30007) or "insufficient" in lowered or "balance" in lowered:
        return ("The Twilio account does not have sufficient balance.", None)
    if code in (30032, 30033, 30035, 30022, 30023) or (
        "compliance" in lowered
        or "campaign" in lowered
        or "a2p" in lowered
        or "10dlc" in lowered
        or "trust hub" in lowered
        or "kyc" in lowered
        or "pending" in lowered
    ):
        return ("Twilio messaging compliance approval is still pending.", "pending_approval")
    if code == 21408:
        return (
            "Permission to send SMS to this region or number type is not enabled on the Twilio account.",
            None,
        )

    if raw_message:
        cleaned = redact_secrets(raw_message)[:180]
        return (cleaned or "SMS send failed.", None)
    return ("SMS send failed.", None)


def format_user_error(provider_code, detail):
    if provider_code is not None:
        return f"SMS could not be sent. Twilio error {provider_code}: {detail}"
    return f"SMS could not be sent. {detail}"


class TwilioSmsProvider:
    """
    Outbound SMS via Twilio Messages API.
    Prefer Messaging Service SID when configured; otherwise send with From number.
    """

    def __init__(self):
        self.account_sid = (config.TWILIO_ACCOUNT_SID or "").strip()
        self.auth_token = (config.TWILIO_AUTH_TOKEN or "").strip()
        self.from_number = (config.TWILIO_PHONE_NUMBER or "").strip()
        self.messaging_service_sid = (config.TWILIO_MESSAGING_SERVICE_SID or "").strip()

    def credentials_configured(self):
        return bool(
            self.account_sid
            and self.account_sid.startswith("AC")
            and self.auth_token
        )

    def sending_phone_configured(self):
        return bool(self.from_number and re.fullmatch(r"\+[1-9]\d{9,14}", self.from_number))

    def messaging_service_configured(self):
        return bool(
            self.messaging_service_sid
            and self.messaging_service_sid.startswith("MG")
        )

    def is_configured(self):
        return self.credentials_configured() and (
            self.messaging_service_configured() or self.sending_phone_configured()
        )

    def config_error(self):
        """Return a safe configuration error, or None if shapes look usable."""
        if not self.account_sid:
            return "Twilio account SID is missing."
        if not self.account_sid.startswith("AC"):
            return "Twilio account SID looks invalid. It should start with AC."
        if not self.auth_token:
            return "Twilio Auth Token is missing."
        if not self.messaging_service_configured() and not self.sending_phone_configured():
            if self.from_number and not re.fullmatch(r"\+[1-9]\d{9,14}", self.from_number):
                return "Twilio phone number must be in E.164 format."
            if self.messaging_service_sid and not self.messaging_service_sid.startswith("MG"):
                return "Twilio Messaging Service SID looks invalid. It should start with MG."
            return (
                "Configure TWILIO_MESSAGING_SERVICE_SID (preferred) "
                "or TWILIO_PHONE_NUMBER for outbound SMS."
            )
        return None

    def configuration_status(self, *, latest_error_code=None, latest_error_message=None):
        a2p = "unknown"
        if latest_error_code == 30034:
            a2p = "blocked_unregistered"
        elif latest_error_code in (30032, 30033, 30035, 30022, 30023):
            a2p = "pending_approval"
        elif self.messaging_service_configured():
            a2p = "messaging_service_configured_verify_in_twilio"
        elif self.sending_phone_configured():
            a2p = "from_number_configured_verify_a2p_in_twilio"
        elif not self.is_configured():
            a2p = "incomplete_configuration"

        return {
            "credentials_configured": self.credentials_configured(),
            "sending_phone_configured": self.sending_phone_configured(),
            "messaging_service_configured": self.messaging_service_configured(),
            "send_configured": self.is_configured(),
            "uses_messaging_service": self.messaging_service_configured(),
            "latest_twilio_error_code": latest_error_code,
            "latest_twilio_error_message": latest_error_message,
            "a2p_readiness": a2p,
        }

    def send_sms(self, phone_number, message_body, status_callback=None):
        config_error = self.config_error()
        if config_error:
            raise SmsProviderError(config_error)
        if not self.is_configured():
            raise SmsProviderError("SMS sending is not configured yet.")

        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json"
        form = {
            "To": phone_number,
            "Body": message_body,
        }
        # Prefer Messaging Service so sender pool / A2P campaign association is used.
        if self.messaging_service_configured():
            form["MessagingServiceSid"] = self.messaging_service_sid
        else:
            form["From"] = self.from_number
        if status_callback:
            form["StatusCallback"] = status_callback

        payload = urllib.parse.urlencode(form).encode("utf-8")
        request = urllib.request.Request(url, data=payload, method="POST")
        credentials = f"{self.account_sid}:{self.auth_token}".encode("utf-8")
        request.add_header(
            "Authorization", "Basic " + base64.b64encode(credentials).decode("ascii")
        )
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", "TopAI-Real-Estate-Tools/1.0")

        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise _provider_error_from_response(detail, exc.code) from None
        except urllib.error.URLError as exc:
            logger.warning("Twilio SMS network error: %s", redact_secrets(exc))
            raise SmsProviderError(
                "SMS could not be sent. Could not reach the SMS provider. Please try again."
            ) from None

        provider_message_id = result.get("sid")
        if not provider_message_id:
            raise SmsProviderError("SMS could not be sent. SMS provider did not return a message ID.")

        return {
            "provider_message_id": provider_message_id,
            "status": result.get("status") or "queued",
            "to": result.get("to"),
            "from": result.get("from"),
        }


def _provider_error_from_response(detail, http_status):
    provider_code = None
    more_info = None
    message_sid = None
    raw_message = ""
    try:
        data = json.loads(detail or "{}")
        provider_code = data.get("code")
        raw_message = str(data.get("message") or "").strip()
        more_info = data.get("more_info") or data.get("moreInfo")
        message_sid = data.get("sid")
    except Exception:
        raw_message = redact_secrets((detail or "")[:200])

    detail_text, _a2p = map_twilio_error(provider_code, raw_message, http_status=http_status)
    user_message = format_user_error(provider_code, detail_text)

    logger.error(
        "Twilio SMS send failed http=%s code=%s sid=%s more_info=%s message=%s",
        http_status,
        provider_code,
        message_sid,
        redact_secrets(more_info) if more_info else None,
        redact_secrets(raw_message),
    )

    return SmsProviderError(
        user_message,
        status_code=http_status,
        provider_code=provider_code,
        more_info=more_info,
        message_sid=message_sid,
    )


def get_sms_provider():
    if config.SMS_PROVIDER != "twilio":
        raise SmsProviderError(f"Unsupported SMS provider: {config.SMS_PROVIDER}")
    return TwilioSmsProvider()


def sms_status_callback_url():
    base = (config.APP_URL or "").rstrip("/")
    if not base or "localhost" in base:
        return None
    return f"{base}/webhook/sms/status"


def parse_provider_code_from_error_message(error_message):
    match = re.search(r"Twilio error (\d+)", str(error_message or ""))
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None
