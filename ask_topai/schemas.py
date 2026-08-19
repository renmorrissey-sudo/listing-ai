"""Explicit Phase 1 tool schemas. AI output is untrusted and validated here."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from crm_constants import PRIORITIES
from lead_service import format_phone_display, normalize_phone_e164
from sms_validation import validate_e164_phone

ALLOWED_ACTIONS = frozenset(
    {
        "create_lead",
        "add_lead_note",
        "create_task",
        "update_property_criteria",
    }
)

BLOCKED_ACTIONS = frozenset(
    {
        "send_sms",
        "send_email",
        "send_listings",
        "place_call",
        "start_call",
        "delete_lead",
        "change_consent",
        "change_sms_qualification",
        "create_appointment",
        "sql",
        "execute_sql",
        "raw_query",
    }
)

CREATE_LEAD_FIELDS = (
    "name",
    "phone",
    "email",
    "lead_type",
    "property_interest",
    "desired_outcome",
    "notes",
    "price_min",
    "price_max",
    "bedrooms",
    "bathrooms",
    "city",
    "neighborhood",
    "property_type",
)

CRITERIA_FIELDS = (
    "price_min",
    "price_max",
    "bedrooms",
    "bathrooms",
    "city",
    "neighborhood",
    "property_type",
    "property_interest",
    "replace",
)

NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def transcript_digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def _clean_str(value, limit=500):
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _clean_int(value):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        digits = re.sub(r"[^\d]", "", str(value))
        return int(digits) if digits else None


def phone_grounded(transcript: str, phone: str) -> bool:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) >= 10:
        digits = digits[-10:]
    return bool(digits) and digits in transcript_digits(transcript)


def email_grounded(transcript: str, email: str) -> bool:
    return bool(email) and email.lower() in (transcript or "").lower()


def number_grounded(transcript: str, value) -> bool:
    if value is None:
        return True
    try:
        number = int(value)
    except (TypeError, ValueError):
        return False
    text = (transcript or "").lower()
    digits = transcript_digits(transcript)
    if str(number) in digits:
        return True
    compact = text.replace(",", "").replace(" ", "").replace("$", "")
    if str(number) in compact:
        return True
    if number >= 1000 and number % 1000 == 0:
        thousands = number // 1000
        if str(thousands) in digits and (
            "thousand" in text or f"{thousands}k" in compact
        ):
            return True
    if number >= 1_000_000 and number % 1_000_000 == 0:
        millions = number // 1_000_000
        if str(millions) in digits and "million" in text:
            return True
    return False


def count_grounded(transcript: str, value) -> bool:
    if value is None:
        return True
    try:
        number = int(value)
    except (TypeError, ValueError):
        return False
    text = (transcript or "").lower()
    if str(number) in text:
        return True
    for word, n in NUMBER_WORDS.items():
        if n == number and re.search(rf"\b{word}\b", text):
            return True
    return False


def alias_location_fields(arguments: dict) -> dict:
    """Map location(s)/neighborhoods/property_types onto existing scalar fields."""
    args = dict(arguments or {})

    def _first(value):
        if isinstance(value, (list, tuple)):
            return str(value[0]).strip() if value else None
        if value in (None, ""):
            return None
        return str(value).strip() or None

    location = args.pop("location", None) or args.pop("locations", None)
    if location and not args.get("city"):
        args["city"] = _first(location)
    neighborhoods = args.pop("neighborhoods", None)
    if neighborhoods and not args.get("neighborhood"):
        args["neighborhood"] = _first(neighborhoods)
    property_types = args.pop("property_types", None)
    if property_types and not args.get("property_type"):
        args["property_type"] = _first(property_types)
    return args


def drop_ungrounded(transcript: str, arguments: dict) -> dict:
    """Remove invented phones, emails, prices, and counts not present in the request."""
    out = dict(arguments or {})
    phone = out.get("phone") or out.get("phone_number")
    if phone and not phone_grounded(transcript, str(phone)):
        out.pop("phone", None)
        out.pop("phone_number", None)
    email = out.get("email")
    if email and not email_grounded(transcript, str(email)):
        out.pop("email", None)
    for key in ("price_min", "price_max"):
        if key in out and not number_grounded(transcript, out.get(key)):
            out.pop(key, None)
    for key in ("bedrooms", "bathrooms"):
        if key in out and not count_grounded(transcript, out.get(key)):
            out.pop(key, None)
    return out


def format_money(value):
    if value is None:
        return None
    try:
        return f"${int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def build_property_interest(arguments: dict) -> str | None:
    existing = _clean_str(arguments.get("property_interest"), 500)
    parts = []
    bedrooms = arguments.get("bedrooms")
    property_type = _clean_str(arguments.get("property_type"), 80)
    city = _clean_str(arguments.get("city"), 80)
    neighborhood = _clean_str(arguments.get("neighborhood"), 80)
    if bedrooms:
        parts.append(f"{bedrooms} bedroom")
    if property_type:
        parts.append(property_type)
    place = neighborhood or city
    if place:
        parts.append(f"in {place}")
    if arguments.get("price_max") is not None:
        parts.append(f"under {format_money(arguments.get('price_max'))}")
    elif arguments.get("price_min") is not None:
        parts.append(f"from {format_money(arguments.get('price_min'))}")
    built = " ".join(parts).strip()
    if existing and built and existing.lower() not in built.lower():
        return f"{existing}; {built}"[:500]
    return existing or built or None


def parse_due_at(arguments: dict, *, transcript: str = "") -> str | None:
    """Resolve due_at from ISO, due_date, or simple relative phrases in the transcript."""
    raw = _clean_str(arguments.get("due_at") or arguments.get("due"), 40)
    if raw:
        try:
            datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return raw
        except ValueError:
            pass
    due_date = _clean_str(arguments.get("due_date"), 40)
    due_time = _clean_str(arguments.get("due_time"), 20)
    text = f"{due_date or ''} {due_time or ''} {transcript or ''}".lower()
    now = datetime.now(timezone.utc)
    target = None
    if "tomorrow" in text:
        target = now + timedelta(days=1)
    else:
        weekdays = [
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        ]
        for idx, name in enumerate(weekdays):
            if re.search(rf"\b{name}\b", text):
                days = (idx - now.weekday()) % 7
                if days == 0:
                    days = 7
                target = now + timedelta(days=days)
                break
    if target is None and due_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", due_date):
        target = datetime.fromisoformat(due_date).replace(tzinfo=timezone.utc)
    if target is None:
        return None
    hour = 15
    if "morning" in text:
        hour = 9
    elif "afternoon" in text:
        hour = 15
    elif "evening" in text or "night" in text:
        hour = 18
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", due_time or "")
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        mer = (match.group(3) or "").lower()
        if mer == "pm" and hour < 12:
            hour += 12
        if mer == "am" and hour == 12:
            hour = 0
        return target.replace(hour=hour, minute=minute, second=0, microsecond=0).isoformat()
    return target.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


def sanitize_create_lead(arguments: dict, transcript: str) -> tuple[dict, str | None]:
    args = drop_ungrounded(transcript, arguments)
    name = _clean_str(args.get("name") or args.get("lead_name"), 200)
    phone_raw = args.get("phone") or args.get("phone_number")
    phone, phone_err = validate_e164_phone(phone_raw) if phone_raw else (None, None)
    if phone_raw and phone_err:
        return {}, phone_err
    if phone_raw and not phone_grounded(transcript, str(phone_raw)):
        phone = None
    lead_type = _clean_str(args.get("lead_type"), 80)
    if lead_type:
        lead_type = lead_type.lower()
        if lead_type not in {"buyer", "seller", "renter", "investor", "other"}:
            lead_type = "buyer" if "buy" in lead_type else lead_type
    cleaned = {
        "name": name,
        "phone": phone,
        "email": _clean_str(args.get("email"), 200),
        "lead_type": lead_type,
        "desired_outcome": _clean_str(args.get("desired_outcome"), 300),
        "notes": _clean_str(args.get("notes"), 1500),
        "price_min": _clean_int(args.get("price_min")),
        "price_max": _clean_int(args.get("price_max")),
        "bedrooms": _clean_int(args.get("bedrooms")),
        "bathrooms": _clean_int(args.get("bathrooms")),
        "city": _clean_str(args.get("city"), 80),
        "neighborhood": _clean_str(args.get("neighborhood"), 80),
        "property_type": _clean_str(args.get("property_type"), 80),
    }
    cleaned = {k: v for k, v in cleaned.items() if v not in (None, "")}
    cleaned["property_interest"] = build_property_interest({**args, **cleaned})
    if not cleaned.get("name"):
        return cleaned, "A lead name is required."
    if not cleaned.get("phone"):
        return cleaned, "A valid mobile phone number is required."
    return cleaned, None


def sanitize_add_note(arguments: dict) -> tuple[dict, str | None]:
    note = _clean_str(arguments.get("note") or arguments.get("notes") or arguments.get("text"), 1500)
    lead_id = _clean_int(arguments.get("lead_id"))
    lead_name = _clean_str(arguments.get("lead_name") or arguments.get("name"), 200)
    cleaned = {"note": note, "lead_id": lead_id, "lead_name": lead_name}
    if not note:
        return cleaned, "Note text is required."
    return cleaned, None


def sanitize_create_task(arguments: dict, transcript: str) -> tuple[dict, str | None]:
    title = _clean_str(arguments.get("title") or arguments.get("task"), 200)
    description = _clean_str(arguments.get("description"), 2000)
    lead_id = _clean_int(arguments.get("lead_id"))
    lead_name = _clean_str(arguments.get("lead_name") or arguments.get("name"), 200)
    priority = _clean_str(arguments.get("priority"), 20)
    if priority and priority not in PRIORITIES:
        priority = "normal"
    due_at = parse_due_at(arguments, transcript=transcript)
    cleaned = {
        "title": title,
        "description": description,
        "lead_id": lead_id,
        "lead_name": lead_name,
        "priority": priority or "normal",
        "due_at": due_at,
    }
    if not title:
        return cleaned, "Task title is required."
    return cleaned, None


def sanitize_update_criteria(arguments: dict, transcript: str) -> tuple[dict, str | None]:
    args = drop_ungrounded(transcript, arguments)
    cleaned = {
        "lead_id": _clean_int(args.get("lead_id")),
        "lead_name": _clean_str(args.get("lead_name") or args.get("name"), 200),
        "price_min": _clean_int(args.get("price_min")),
        "price_max": _clean_int(args.get("price_max")),
        "bedrooms": _clean_int(args.get("bedrooms")),
        "bathrooms": _clean_int(args.get("bathrooms")),
        "city": _clean_str(args.get("city"), 80),
        "neighborhood": _clean_str(args.get("neighborhood"), 80),
        "property_type": _clean_str(args.get("property_type"), 80),
        "property_interest": _clean_str(args.get("property_interest"), 500),
        "replace": bool(args.get("replace") is True or str(args.get("replace") or "").lower() in {"true", "1", "yes"}),
    }
    cleaned = {k: v for k, v in cleaned.items() if v not in (None, "", False) or k == "replace"}
    has_criteria = any(
        cleaned.get(k) not in (None, "", False)
        for k in CRITERIA_FIELDS
        if k != "replace"
    )
    if not has_criteria:
        return cleaned, "Property criteria are required."
    return cleaned, None


def sanitize_command(command: dict, transcript: str) -> tuple[dict | None, str | None]:
    if not isinstance(command, dict):
        return None, "Invalid command."
    action = str(command.get("action") or "").strip()
    if action in BLOCKED_ACTIONS or action not in ALLOWED_ACTIONS:
        return None, "Ask TopAI cannot do that yet. Phase 1 supports creating leads, adding notes, creating tasks, and updating property criteria."
    arguments = command.get("arguments") if isinstance(command.get("arguments"), dict) else {}
    arguments = alias_location_fields(arguments)
    if action == "create_lead":
        cleaned, err = sanitize_create_lead(arguments, transcript)
    elif action == "add_lead_note":
        cleaned, err = sanitize_add_note(arguments)
    elif action == "create_task":
        cleaned, err = sanitize_create_task(arguments, transcript)
    else:
        cleaned, err = sanitize_update_criteria(arguments, transcript)
    return {"action": action, "arguments": cleaned}, err


def preview_rows(command: dict) -> list[tuple[str, str]]:
    action = command.get("action")
    args = command.get("arguments") or {}
    rows = []
    if action == "create_lead":
        rows.append(("Action", "Create Lead"))
        if args.get("name"):
            rows.append(("Name", args["name"]))
        if args.get("phone"):
            rows.append(("Phone", format_phone_display(args["phone"])))
        if args.get("lead_type"):
            rows.append(("Type", str(args["lead_type"]).title()))
        if args.get("email"):
            rows.append(("Email", args["email"]))
        if args.get("bedrooms"):
            rows.append(("Bedrooms", str(args["bedrooms"])))
        if args.get("city") or args.get("neighborhood"):
            rows.append(("Location", args.get("neighborhood") or args.get("city")))
        if args.get("price_max") is not None:
            rows.append(("Maximum", format_money(args["price_max"])))
        if args.get("property_interest"):
            rows.append(("Criteria", args["property_interest"]))
        if args.get("notes"):
            rows.append(("Notes", args["notes"]))
    elif action == "add_lead_note":
        rows.append(("Action", "Add Note"))
        if args.get("lead_name"):
            rows.append(("Lead", args["lead_name"]))
        rows.append(("Note", args.get("note") or ""))
    elif action == "create_task":
        rows.append(("Action", "Create Task"))
        rows.append(("Title", args.get("title") or ""))
        if args.get("lead_name"):
            rows.append(("Lead", args["lead_name"]))
        if args.get("due_at"):
            rows.append(("Due", args["due_at"][:16].replace("T", " ")))
        if args.get("priority"):
            rows.append(("Priority", args["priority"]))
    elif action == "update_property_criteria":
        rows.append(("Action", "Save Property Criteria"))
        if args.get("lead_name"):
            rows.append(("Lead", args["lead_name"]))
        interest = build_property_interest(args)
        if interest:
            rows.append(("Criteria", interest))
    return rows
