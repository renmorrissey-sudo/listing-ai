FIELD_LIMITS = {
    "address": 300,
    "price": 50,
    "beds": 10,
    "baths": 10,
    "sqft": 50,
    "year_built": 10,
    "garage": 50,
    "pool": 50,
    "features": 2000,
    "neighborhood": 2000,
    "area": 300,
    "property_type": 100,
    "situation": 100,
    "agent_name": 100,
    "key_benefit": 2000,
}


def truncate_fields(data, fields):
    cleaned = {}
    for field in fields:
        value = data.get(field)
        if value is None:
            cleaned[field] = value
            continue
        text = str(value).strip()
        limit = FIELD_LIMITS.get(field)
        if limit and len(text) > limit:
            text = text[:limit]
        cleaned[field] = text
    return cleaned


def validate_listing_payload(data):
    if not data:
        return None, "Invalid JSON body."
    required = ["address", "price", "beds", "baths", "sqft"]
    missing = [f for f in required if not str(data.get(f, "")).strip()]
    if missing:
        return None, f"Missing required fields: {', '.join(missing)}"
    fields = list(FIELD_LIMITS.keys())
    return truncate_fields(data, fields), None


def validate_script_payload(data):
    if not data:
        return None, "Invalid JSON body."
    if not str(data.get("area", "")).strip():
        return None, "Target area is required"
    fields = ["area", "property_type", "situation", "agent_name", "key_benefit"]
    return truncate_fields(data, fields), None
