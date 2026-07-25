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
    def __init__(self):
        self.account_sid = config.TWILIO_ACCOUNT_SID
        self.api_key_sid = config.TWILIO_API_KEY_SID
        self.api_key_secret = config.TWILIO_API_KEY_SECRET
        self.from_number = config.TWILIO_PHONE_NUMBER

    def is_configured(self):
        return bool(
            self.account_sid
            and self.api_key_sid
            and self.api_key_secret
            and self.from_number
        )

    def send_sms(self, phone_number, message_body, status_callback=None):
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
        # Twilio API Key auth: username = API Key SID, password = API Key Secret.
        credentials = f"{self.api_key_sid}:{self.api_key_secret}".encode("utf-8")
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
            message = "SMS provider authentication failed."
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
