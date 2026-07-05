import re


VOICE_FIELD_LIMITS = {
    "lead_name": 120,
    "phone_number": 32,
    "lead_type": 80,
    "property_interest": 500,
    "desired_outcome": 300,
    "notes": 1500,
}


def _clean_phone(phone_number):
    raw = str(phone_number or "").strip()
    if not raw:
        return ""
    if raw.startswith("+"):
        digits = "+" + re.sub(r"\D", "", raw[1:])
    else:
        digits_only = re.sub(r"\D", "", raw)
        if len(digits_only) == 10:
            digits = "+1" + digits_only
        elif len(digits_only) == 11 and digits_only.startswith("1"):
            digits = "+" + digits_only
        else:
            digits = "+" + digits_only
    return digits


def validate_voice_call_payload(data):
    if not data:
        return None, "Invalid JSON body."

    if not data.get("compliance_confirmed"):
        return None, "Confirm that this lead consented to be contacted before starting a call."

    cleaned = {}
    for field, limit in VOICE_FIELD_LIMITS.items():
        value = str(data.get(field, "")).strip()
        cleaned[field] = value[:limit]

    cleaned["phone_number"] = _clean_phone(cleaned["phone_number"])
    if not re.fullmatch(r"\+[1-9]\d{9,14}", cleaned["phone_number"]):
        return None, "Enter a valid phone number with area code."

    persona_id = data.get("persona_id")
    try:
        cleaned["persona_id"] = int(persona_id)
    except (TypeError, ValueError):
        return None, "Select a valid calling persona."

    if not cleaned.get("lead_name"):
        cleaned["lead_name"] = "the lead"
    if not cleaned.get("desired_outcome"):
        cleaned["desired_outcome"] = "qualify the lead and request an appointment"

    return cleaned, None
