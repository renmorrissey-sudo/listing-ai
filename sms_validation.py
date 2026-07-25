import re

from voice_validation import _clean_phone

SMS_FIELD_LIMITS = {
    "lead_name": 120,
    "phone_number": 32,
    "lead_type": 80,
    "property_interest": 500,
    "desired_outcome": 300,
    "notes": 1500,
    "message_body": 480,
    "agent_name": 120,
}


def validate_sms_generate_payload(data):
    if not data:
        return None, "Invalid JSON body."

    cleaned = {}
    for field, limit in SMS_FIELD_LIMITS.items():
        if field == "message_body":
            continue
        value = str(data.get(field, "")).strip()
        cleaned[field] = value[:limit]

    cleaned["phone_number"] = _clean_phone(cleaned["phone_number"])
    if cleaned["phone_number"] and not re.fullmatch(r"\+[1-9]\d{9,14}", cleaned["phone_number"]):
        return None, "Enter a valid phone number with area code."

    persona_id = data.get("persona_id")
    try:
        cleaned["persona_id"] = int(persona_id)
    except (TypeError, ValueError):
        return None, "Select a valid SMS persona."

    if not cleaned.get("lead_name"):
        cleaned["lead_name"] = "there"
    if not cleaned.get("desired_outcome"):
        cleaned["desired_outcome"] = "start a conversation and request a callback or appointment"
    if not cleaned.get("agent_name"):
        cleaned["agent_name"] = "your real estate agent"

    return cleaned, None


def validate_sms_send_payload(data):
    cleaned, error = validate_sms_generate_payload(data)
    if error:
        return None, error

    if not data.get("compliance_confirmed"):
        return None, "Confirm that this lead consented to receive SMS before sending."

    if not cleaned.get("phone_number"):
        return None, "Enter a valid phone number with area code."

    message_body = str(data.get("message_body", "")).strip()[: SMS_FIELD_LIMITS["message_body"]]
    if not message_body:
        return None, "Generate or enter an SMS message before sending."
    if len(message_body) > 480:
        return None, "Keep the SMS under 480 characters."

    cleaned["message_body"] = message_body
    cleaned["send_now"] = bool(data.get("send_now", True))
    return cleaned, None
