import re


VOICE_FIELD_LIMITS = {
    "lead_name": 120,
    "phone_number": 32,
    "lead_type": 80,
    "property_interest": 500,
    "desired_outcome": 300,
    "call_purpose": 500,
    "lead_context": 1500,
    "notes": 1500,
}

PERSONA_FIELD_LIMITS = {
    "name": 120,
    "persona_type": 80,
    "prompt": 3000,
    "tone": 200,
    "goal": 500,
    "objection_handling_notes": 1500,
}


def _clean_phone(phone_number):
    from lead_service import normalize_phone_e164

    return normalize_phone_e164(phone_number)


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

    lead_id = data.get("lead_id")
    if lead_id in ("", None):
        cleaned["lead_id"] = None
    else:
        try:
            cleaned["lead_id"] = int(lead_id)
        except (TypeError, ValueError):
            return None, "Invalid lead selection."

    if not cleaned.get("lead_name"):
        return None, "Lead name is required."
    if not cleaned.get("lead_context") and cleaned.get("notes"):
        cleaned["lead_context"] = cleaned["notes"]
    if not cleaned.get("call_purpose") and cleaned.get("desired_outcome"):
        cleaned["call_purpose"] = cleaned["desired_outcome"]
    if not cleaned.get("desired_outcome"):
        cleaned["desired_outcome"] = "qualify the lead and request an appointment"

    return cleaned, None


def validate_voice_persona_payload(data):
    if not data:
        return None, "Invalid JSON body."

    cleaned = {}
    for field, limit in PERSONA_FIELD_LIMITS.items():
        value = str(data.get(field, "")).strip()
        cleaned[field] = value[:limit]

    if not cleaned["name"]:
        return None, "Persona name is required."
    if not cleaned["prompt"]:
        return None, "Persona instructions are required."
    if not cleaned["goal"]:
        return None, "Persona goal is required."

    if not cleaned["persona_type"]:
        cleaned["persona_type"] = "custom"
    if not cleaned["tone"]:
        cleaned["tone"] = "professional, helpful, and concise"

    return cleaned, None
