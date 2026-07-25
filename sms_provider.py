import base64
import json
import urllib.error
import urllib.parse
import urllib.request

import config


class SmsProviderError(Exception):
    pass


class TwilioSmsProvider:
    def __init__(self):
        self.account_sid = config.SMS_TWILIO_ACCOUNT_SID
        self.auth_token = config.SMS_TWILIO_AUTH_TOKEN
        self.from_number = config.SMS_FROM_NUMBER

    def is_configured(self):
        return bool(self.account_sid and self.auth_token and self.from_number)

    def send_sms(self, phone_number, message_body):
        if not self.is_configured():
            raise SmsProviderError(
                "SMS sending is not configured yet. Add Twilio credentials and an SMS from-number in Railway."
            )

        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json"
        payload = urllib.parse.urlencode(
            {
                "To": phone_number,
                "From": self.from_number,
                "Body": message_body,
            }
        ).encode("utf-8")
        request = urllib.request.Request(url, data=payload, method="POST")
        credentials = f"{self.account_sid}:{self.auth_token}".encode("utf-8")
        request.add_header("Authorization", "Basic " + base64.b64encode(credentials).decode("ascii"))
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        request.add_header("User-Agent", "TopAI-Real-Estate-Tools/1.0")

        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise SmsProviderError(f"SMS provider rejected the message: {detail}") from exc
        except urllib.error.URLError as exc:
            raise SmsProviderError("Could not reach the SMS provider. Please try again.") from exc

        provider_message_id = result.get("sid")
        if not provider_message_id:
            raise SmsProviderError("SMS provider did not return a message ID.")
        return {
            "provider_message_id": provider_message_id,
            "status": result.get("status") or "sent",
            "raw": result,
        }


def get_sms_provider():
    if config.SMS_PROVIDER != "twilio":
        raise SmsProviderError(f"Unsupported SMS provider: {config.SMS_PROVIDER}")
    return TwilioSmsProvider()
