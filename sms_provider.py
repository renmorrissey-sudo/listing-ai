import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request

import config


class SmsProviderError(Exception):
    def __init__(self, message, *, status_code=None, provider_code=None):
        super().__init__(message)
        self.status_code = status_code
        self.provider_code = provider_code


class TwilioSmsProvider:
    """
    TEMPORARY diagnostic auth: outbound SMS uses Account SID + Auth Token.
    Do not use API Key SID/Secret for outbound sends during this diagnostic.
    Webhook signature validation still uses TWILIO_AUTH_TOKEN via RequestValidator.
    """

    def __init__(self):
        self.account_sid = (config.TWILIO_ACCOUNT_SID or "").strip()
        self.auth_token = (config.TWILIO_AUTH_TOKEN or "").strip()
        self.from_number = (config.TWILIO_PHONE_NUMBER or "").strip()

    def is_configured(self):
        return bool(self.account_sid and self.auth_token and self.from_number)

    def config_error(self):
        """Return a safe configuration error, or None if shapes look usable."""
        if not self.account_sid:
            return "Twilio account SID is missing."
        if not self.account_sid.startswith("AC"):
            return "Twilio account SID looks invalid. It should start with AC."
        if not self.auth_token:
            return "Twilio Auth Token is missing."
        if not self.from_number:
            return "Twilio phone number is missing."
        if not re.fullmatch(r"\+[1-9]\d{9,14}", self.from_number):
            return "Twilio phone number must be in E.164 format."
        return None

    def send_sms(self, phone_number, message_body, status_callback=None):
        config_error = self.config_error()
        if config_error:
            raise SmsProviderError(config_error)
        if not self.is_configured():
            raise SmsProviderError("SMS sending is not configured yet.")

        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json"
        form = {
            "To": phone_number,
            "From": self.from_number,
            "Body": message_body,
        }
        if status_callback:
            form["StatusCallback"] = status_callback

        payload = urllib.parse.urlencode(form).encode("utf-8")
        request = urllib.request.Request(url, data=payload, method="POST")
        # TEMP diagnostic: Account SID + Auth Token Basic auth (not API keys).
        credentials = f"{self.account_sid}:{self.auth_token}".encode("utf-8")
        request.add_header("Authorization", "Basic " + base64.b64encode(credentials).decode("ascii"))
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", "TopAI-Real-Estate-Tools/1.0")

        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise _provider_error_from_response(detail, exc.code) from exc
        except urllib.error.URLError as exc:
            raise SmsProviderError("Could not reach the SMS provider. Please try again.") from exc

        provider_message_id = result.get("sid")
        if not provider_message_id:
            raise SmsProviderError("SMS provider did not return a message ID.")

        return {
            "provider_message_id": provider_message_id,
            "status": result.get("status") or "queued",
            "to": result.get("to"),
            "from": result.get("from"),
        }


def _provider_error_from_response(detail, http_status):
    provider_code = None
    message = "SMS send failed."
    try:
        data = json.loads(detail or "{}")
        provider_code = data.get("code")
        raw_message = str(data.get("message") or "").strip()
        # Keep only a short safe message; never echo credentials or tokens.
        if provider_code == 21211:
            message = "Invalid destination phone number."
        elif provider_code == 21608:
            message = "Destination number is not verified for this Twilio trial account."
        elif provider_code == 21614:
            message = "Destination phone number is not a valid mobile number."
        elif provider_code in (20003, 20005):
            lowered = raw_message.lower()
            if "compliance" in lowered or "kyc" in lowered or "trust hub" in lowered:
                message = (
                    "Twilio blocked messaging until your primary compliance profile "
                    "is approved in Trust Hub (KYC). Complete that in Twilio Console, "
                    "then retry sending."
                )
            elif raw_message:
                cleaned = re.sub(r"(?i)(sk|ac|auth|token|secret)[=:\s]+\S+", "[redacted]", raw_message)
                message = cleaned[:180]
            else:
                message = (
                    "Twilio rejected the request (auth or permissions). "
                    "Confirm account credentials and Trust Hub compliance status."
                )
        elif raw_message:
            cleaned = re.sub(r"(?i)(sk|ac|auth|token|secret)[=:\s]+\S+", "[redacted]", raw_message)
            message = cleaned[:180]
    except Exception:
        pass
    return SmsProviderError(message, status_code=http_status, provider_code=provider_code)


def get_sms_provider():
    if config.SMS_PROVIDER != "twilio":
        raise SmsProviderError(f"Unsupported SMS provider: {config.SMS_PROVIDER}")
    return TwilioSmsProvider()


def sms_status_callback_url():
    base = (config.APP_URL or "").rstrip("/")
    if not base or "localhost" in base:
        return None
    return f"{base}/webhook/sms/status"
