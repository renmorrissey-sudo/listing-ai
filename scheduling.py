"""Account scheduling preferences and CRM-backed calendar intelligence.

Internal availability here is TopAI appointments on the agent account,
using the agent's timezone and account-level scheduling settings. The
connected calendar (Google first, via calendar_providers) is consulted
at confirm time for collisions and written on confirmation — see
harness/appointment_requests.py. Inbound booking requests never book
here: they create a pending appointment request for the agent to
confirm (Phase III-4).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import crm_db
import crm_time
import db
from crm_constants import APPOINTMENT_TYPES

DEFAULTS = {
    "default_duration_minutes": 30,
    "business_hours_start": "08:00",
    "business_hours_end": "18:00",
    "business_days": "0,1,2,3,4",  # Monday–Friday
    "min_notice_minutes": 60,
    "buffer_minutes": 15,
}

WEEKDAYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]

ACTIVE_APPT_STATUSES = {"proposed", "scheduled", "confirmed", "rescheduled"}


def get_settings(user_id) -> dict:
    with db.get_db() as conn:
        try:
            row = conn.execute(
                "SELECT * FROM user_scheduling_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        except Exception:
            row = None
    data = dict(DEFAULTS)
    if row:
        row = dict(row)
        data.update({k: row[k] for k in DEFAULTS if k in row and row[k] not in (None, "")})
    data["timezone_name"] = db.get_user_timezone(user_id) or crm_time.DEFAULT_TIMEZONE
    data["default_duration_minutes"] = int(data["default_duration_minutes"] or 30)
    data["min_notice_minutes"] = int(data["min_notice_minutes"] or 60)
    data["buffer_minutes"] = int(data["buffer_minutes"] or 15)
    return data


def upsert_settings(user_id, updates: dict | None = None):
    current = get_settings(user_id)
    merged = {k: current[k] for k in DEFAULTS}
    for key, value in (updates or {}).items():
        if key in DEFAULTS and value not in (None, ""):
            merged[key] = value
    now = datetime.now(timezone.utc).isoformat()
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO user_scheduling_settings
                (user_id, default_duration_minutes, business_hours_start, business_hours_end,
                 business_days, min_notice_minutes, buffer_minutes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                default_duration_minutes = excluded.default_duration_minutes,
                business_hours_start = excluded.business_hours_start,
                business_hours_end = excluded.business_hours_end,
                business_days = excluded.business_days,
                min_notice_minutes = excluded.min_notice_minutes,
                buffer_minutes = excluded.buffer_minutes,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                int(merged["default_duration_minutes"]),
                str(merged["business_hours_start"])[:8],
                str(merged["business_hours_end"])[:8],
                str(merged["business_days"])[:40],
                int(merged["min_notice_minutes"]),
                int(merged["buffer_minutes"]),
                now,
            ),
        )
    return get_settings(user_id)


def _parse_hhmm(value: str):
    text = str(value or "08:00")
    match = re.match(r"^(\d{1,2}):(\d{2})", text)
    if not match:
        return 8, 0
    return int(match.group(1)), int(match.group(2))


def _business_day_set(settings) -> set[int]:
    days = set()
    for part in str(settings.get("business_days") or "0,1,2,3,4").split(","):
        part = part.strip()
        if part.isdigit():
            days.add(int(part))
    return days or {0, 1, 2, 3, 4}


def _to_utc(dt_local: datetime) -> datetime:
    return crm_time.ensure_utc(dt_local)


def _parse_iso(value) -> datetime | None:
    return crm_time.parse_iso_dt(value)


def busy_intervals(user_id, *, exclude_appointment_id=None):
    intervals = []
    for appt in crm_db.list_appointments(user_id, limit=500):
        if appt.get("id") == exclude_appointment_id:
            continue
        if (appt.get("status") or "") not in ACTIVE_APPT_STATUSES:
            continue
        start = _parse_iso(appt.get("start_at"))
        end = _parse_iso(appt.get("end_at")) if appt.get("end_at") else None
        if not start:
            continue
        if not end:
            end = start + timedelta(minutes=30)
        intervals.append((start, end, appt))
    return intervals


def _overlaps(start, end, intervals, buffer_minutes=0):
    pad = timedelta(minutes=int(buffer_minutes or 0))
    for busy_start, busy_end, _appt in intervals:
        if start < (busy_end + pad) and end > (busy_start - pad):
            return True
    return False


def _slot_payload(start_utc: datetime, end_utc: datetime, tz: ZoneInfo, timezone_name: str):
    local = start_utc.astimezone(tz)
    return {
        "start_at": start_utc.isoformat(),
        "end_at": end_utc.isoformat(),
        "local_start": local.strftime("%Y-%m-%d %H:%M"),
        "local_label": local.strftime("%A %b %d, %I:%M %p").replace(" 0", " "),
        "timezone": timezone_name,
    }


def find_available_slots(
    user_id,
    *,
    after=None,
    before=None,
    duration_minutes=None,
    limit=8,
    exclude_appointment_id=None,
):
    settings = get_settings(user_id)
    tz = crm_time.resolve_zone(settings["timezone_name"])
    duration = int(duration_minutes or settings["default_duration_minutes"] or 30)
    buffer_minutes = int(settings["buffer_minutes"] or 0)
    min_notice = int(settings["min_notice_minutes"] or 0)
    now = crm_time.ensure_utc(datetime.now())
    earliest = now + timedelta(minutes=min_notice)
    if after:
        after_dt = _parse_iso(after) or after
        if isinstance(after_dt, datetime):
            earliest = max(earliest, crm_time.ensure_utc(after_dt))
    latest = None
    if before:
        before_dt = _parse_iso(before) or before
        if isinstance(before_dt, datetime):
            latest = crm_time.ensure_utc(before_dt)
    start_hour, start_minute = _parse_hhmm(settings["business_hours_start"])
    end_hour, end_minute = _parse_hhmm(settings["business_hours_end"])
    workdays = _business_day_set(settings)
    busy = busy_intervals(user_id, exclude_appointment_id=exclude_appointment_id)
    slots = []
    local_cursor = earliest.astimezone(tz).replace(second=0, microsecond=0)
    remainder = local_cursor.minute % 15
    if remainder:
        local_cursor += timedelta(minutes=(15 - remainder))
    for _ in range(21 * 24 * 4):
        if len(slots) >= limit:
            break
        if latest and crm_time.ensure_utc(local_cursor) > latest:
            break
        if local_cursor.weekday() in workdays:
            open_dt = local_cursor.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
            close_dt = local_cursor.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
            if open_dt <= local_cursor < close_dt:
                start_utc = _to_utc(local_cursor)
                end_utc = start_utc + timedelta(minutes=duration)
                if end_utc.astimezone(tz) <= close_dt and start_utc >= earliest:
                    if not _overlaps(start_utc, end_utc, busy, buffer_minutes):
                        slots.append(_slot_payload(start_utc, end_utc, tz, settings["timezone_name"]))
        local_cursor += timedelta(minutes=15)
        if local_cursor.date() > (earliest.astimezone(tz).date() + timedelta(days=21)):
            break
    return slots


def get_calendar_availability(user_id, *, start_at=None, end_at=None, date=None):
    settings = get_settings(user_id)
    tz = crm_time.resolve_zone(settings["timezone_name"])
    if date:
        day = crm_time.parse_iso_dt(str(date)[:10] + "T12:00:00+00:00") or crm_time.ensure_utc(datetime.now())
        local = day.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        start = _to_utc(local)
        end = _to_utc(local + timedelta(days=1))
    else:
        start = _parse_iso(start_at) or crm_time.ensure_utc(datetime.now())
        end = _parse_iso(end_at) or (start + timedelta(days=1))
    busy = []
    for busy_start, busy_end, appt in busy_intervals(user_id):
        if busy_start < end and busy_end > start:
            busy.append(
                {
                    "appointment_id": appt.get("id"),
                    "lead_id": appt.get("lead_id"),
                    "lead_name": appt.get("lead_name"),
                    "start_at": busy_start.isoformat(),
                    "end_at": busy_end.isoformat(),
                    "status": appt.get("status"),
                }
            )
    slots = find_available_slots(user_id, after=start.isoformat(), before=end.isoformat(), limit=12)
    return {
        "timezone": settings["timezone_name"],
        "duration_minutes": settings["default_duration_minutes"],
        "busy": busy,
        "open_slots": slots,
    }


def get_existing_appointment(user_id, lead_id=None, appointment_id=None):
    if appointment_id:
        appt = crm_db.get_appointment(user_id, appointment_id)
        if appt:
            return appt
        return None
    if not lead_id:
        return None
    now = crm_time.ensure_utc(datetime.now())
    upcoming = []
    for appt in crm_db.list_appointments(user_id, lead_id=lead_id, limit=50):
        if (appt.get("status") or "") not in ACTIVE_APPT_STATUSES:
            continue
        start = _parse_iso(appt.get("start_at"))
        if start and start >= now - timedelta(hours=1):
            upcoming.append(appt)
    upcoming.sort(key=lambda row: row.get("start_at") or "")
    return upcoming[0] if upcoming else None


def create_calendar_event(user_id, data: dict):
    start = _parse_iso(data.get("start_at"))
    if not start:
        return None, "A start time is required.", None
    settings = get_settings(user_id)
    duration = int(data.get("duration_minutes") or settings["default_duration_minutes"] or 30)
    end = _parse_iso(data.get("end_at")) or (start + timedelta(minutes=duration))
    if _overlaps(start, end, busy_intervals(user_id), settings["buffer_minutes"]):
        alternatives = find_available_slots(
            user_id,
            after=start.isoformat(),
            duration_minutes=duration,
            limit=3,
        )
        return None, "That time conflicts with an existing appointment.", alternatives
    appt_type = data.get("appointment_type") if data.get("appointment_type") in APPOINTMENT_TYPES else "phone_call"
    appt_id, err = crm_db.create_appointment(
        user_id,
        {
            "lead_id": data.get("lead_id"),
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
            "appointment_type": appt_type,
            "location": data.get("location"),
            "notes": data.get("notes"),
            "status": "scheduled",
        },
    )
    if err:
        return None, err, None
    crm_db.set_lead_status(
        user_id, data.get("lead_id"), "appointment_scheduled", from_automation=True
    )
    return crm_db.get_appointment(user_id, appt_id), None, None


def reschedule_calendar_event(user_id, appointment_id, data: dict):
    existing = crm_db.get_appointment(user_id, appointment_id)
    if not existing:
        return None, "Appointment not found.", None
    start = _parse_iso(data.get("start_at"))
    if not start:
        return None, "A start time is required.", None
    settings = get_settings(user_id)
    duration = int(data.get("duration_minutes") or settings["default_duration_minutes"] or 30)
    if existing.get("end_at") and existing.get("start_at"):
        prev_start = _parse_iso(existing["start_at"])
        prev_end = _parse_iso(existing["end_at"])
        if prev_start and prev_end:
            duration = max(15, int((prev_end - prev_start).total_seconds() // 60))
    end = _parse_iso(data.get("end_at")) or (start + timedelta(minutes=duration))
    if _overlaps(
        start,
        end,
        busy_intervals(user_id, exclude_appointment_id=appointment_id),
        settings["buffer_minutes"],
    ):
        alternatives = find_available_slots(
            user_id,
            after=start.isoformat(),
            duration_minutes=duration,
            exclude_appointment_id=appointment_id,
            limit=3,
        )
        return None, "That time conflicts with an existing appointment.", alternatives
    updated, err = crm_db.update_appointment(
        user_id,
        appointment_id,
        {
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
            "status": "scheduled",
            "notes": data.get("notes") if data.get("notes") is not None else existing.get("notes"),
        },
    )
    if err:
        return None, err, None
    crm_db.set_lead_status(
        user_id, existing.get("lead_id"), "appointment_scheduled", from_automation=True
    )
    return updated, None, None


def _next_weekday(local_now: datetime, weekday_name: str) -> datetime:
    idx = WEEKDAYS.index(weekday_name)
    days = (idx - local_now.weekday()) % 7
    if days == 0 and local_now.hour >= 18:
        days = 7
    return (local_now + timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)


def parse_requested_window(details, *, timezone_name, now=None):
    """Turn Claude appointment_details / free text into (after, before) UTC ISO."""
    after, before, exact, _provenance = parse_requested_window_with_provenance(
        details, timezone_name=timezone_name, now=now
    )
    return after, before, exact


def parse_requested_window_with_provenance(details, *, timezone_name, now=None):
    """Turn Claude appointment_details / free text into
    (after, before, exact, provenance).

    ``provenance`` records how the slot was derived so the agent can see
    whether it was stated or guessed: {kind: "exact" | "window" | "day" |
    "missing", raw_text, day, time_text, in_past}. ``kind`` is "exact"
    when an hour was parsed with no "after"/"afternoon"/"around" in the
    stated time — the coach's window hint field does not count (Phase
    III-4 fix); "window" when only a window (afternoon/morning/evening/
    after N) was parsed; "day" when only a day was named; "missing"
    when nothing was parseable. ``in_past`` flags a parsed slot in the
    past — the parser never silently rolls it forward to the next day.
    """
    tz = crm_time.resolve_zone(timezone_name)
    now = crm_time.ensure_utc(now or datetime.now()).astimezone(tz)
    details = details or {}
    if not isinstance(details, dict):
        details = {}
    blob = json.dumps(details).lower()
    raw_text = str(details.get("text") or "")[:500]
    time_text = str(details.get("time") or details.get("start") or "")
    day = None
    for name in WEEKDAYS:
        if re.search(rf"\b{name}\b", blob) or str(details.get("day") or "").lower() == name:
            day = name
            break
    if "tomorrow" in blob:
        local_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif day:
        local_day = _next_weekday(now, day)
    elif details.get("date"):
        parsed = crm_time.parse_iso_dt(str(details.get("date"))[:10] + "T12:00:00+00:00")
        local_day = parsed.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0) if parsed else now.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    else:
        local_day = None

    hour = None
    minute = 0
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", time_text, re.I)
    if not match:
        match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", blob, re.I)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        mer = (match.group(3) or "").lower()
        if mer == "pm" and hour < 12:
            hour += 12
        if mer == "am" and hour == 12:
            hour = 0
    after_hour = None
    if "afternoon" in blob:
        after_hour = 12 if hour is None else hour
    elif "morning" in blob:
        after_hour = 8 if hour is None else hour
    elif "evening" in blob:
        after_hour = 17 if hour is None else hour
    elif re.search(r"\bafter\s+(\d{1,2})", blob):
        after_m = re.search(r"\bafter\s+(\d{1,2})\s*(am|pm)?", blob)
        if after_m:
            after_hour = int(after_m.group(1))
            mer = (after_m.group(2) or "").lower()
            if mer == "pm" and after_hour < 12:
                after_hour += 12

    if local_day is None and hour is None and after_hour is None:
        return None, None, False, {
            "kind": "missing",
            "raw_text": raw_text,
            "day": day,
            "time_text": time_text,
            "in_past": False,
        }

    if local_day is None:
        local_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if hour is not None and now.hour > hour:
            local_day += timedelta(days=1)

    # Phase III-4 fix: exactness comes from the STATED time, not the
    # coach's window hint. The details blob carries a "window" field
    # (e.g. "afternoon") that the coach prompt's example teaches the
    # model to fill alongside an exact hour — testing the whole blob
    # downgraded an exact hour to a 3-hour window, and confirm books
    # the stored slot, so a straight Confirm would have booked a
    # 3-hour appointment. A vague word only downgrades exactness when
    # it is in the lead's own words (details "text") or the parsed
    # time field itself.
    stated = f"{time_text} {raw_text}".lower()
    exact = (
        hour is not None
        and "after" not in stated
        and "afternoon" not in stated
        and "around" not in stated
    )
    if hour is not None:
        start_local = local_day.replace(hour=hour, minute=minute)
        after = _to_utc(start_local)
        before = _to_utc(start_local + timedelta(minutes=30 if exact else 180))
        kind = "exact" if exact else "window"
        return after.isoformat(), before.isoformat(), exact, {
            "kind": kind,
            "raw_text": raw_text,
            "day": day,
            "time_text": time_text,
            "in_past": bool(after < crm_time.ensure_utc(now)),
        }
    if after_hour is not None:
        start_local = local_day.replace(hour=after_hour, minute=0)
        after = _to_utc(start_local)
        before = _to_utc(local_day.replace(hour=18, minute=0))
        return after.isoformat(), before.isoformat(), False, {
            "kind": "window",
            "raw_text": raw_text,
            "day": day,
            "time_text": time_text,
            "in_past": bool(after < crm_time.ensure_utc(now)),
        }
    after = _to_utc(local_day.replace(hour=8, minute=0))
    before = _to_utc(local_day.replace(hour=18, minute=0))
    return after.isoformat(), before.isoformat(), False, {
        "kind": "day",
        "raw_text": raw_text,
        "day": day,
        "time_text": time_text,
        "in_past": bool(after < crm_time.ensure_utc(now)),
    }


def format_slot_sms(slot: dict) -> str:
    return slot.get("local_label") or slot.get("local_start") or ""


def _appointment_type_from_details(details, existing):
    """Appointment type for a request: the existing appointment's type
    on a reschedule, else the parsed type when valid, else phone_call."""
    if existing and existing.get("appointment_type") in APPOINTMENT_TYPES:
        return existing["appointment_type"]
    candidate = str((details or {}).get("appointment_type") or (details or {}).get("type") or "")
    if candidate in APPOINTMENT_TYPES:
        return candidate
    return "phone_call"


def create_appointment_request(user_id, lead, analysis: dict | None, *,
                               source_ref_type="sms", source_ref_id=None,
                               raw_request_text=None):
    """Create a pending appointment request from inbound intent — never book.

    Phase III-4 replacement for ``handle_inbound_scheduling`` (removed):
    BOTH auto-book branches are gone — the exact ±900s match and the
    vague-window ``slots[0]`` book. A booking request creates a pending
    ``harness_appointment_requests`` row; the calendar event is created
    and the lead is told it is booked ONLY after the agent confirms.
    Returns {"action": "requested" | "offer" | "none", "request": row|None,
    "alternatives": [...], "message": str|None, "provenance": {...}}.
    The "offer" action is only the slot-offer text — no booking behind it.
    """
    from harness.appointment_requests import (
        build_confirmation_request_text,
        create_request,
    )

    analysis = analysis or {}
    if not analysis.get("appointment_requested") and not _looks_like_reschedule(analysis):
        return {"action": "none"}
    lead_id = lead["id"]
    settings = get_settings(user_id)
    timezone_name = settings["timezone_name"]
    details = analysis.get("appointment_details") if isinstance(analysis.get("appointment_details"), dict) else {}
    if raw_request_text:
        details = dict(details)
        details.setdefault("text", raw_request_text)
    after, before, exact, provenance = parse_requested_window_with_provenance(
        details, timezone_name=timezone_name
    )
    existing = get_existing_appointment(user_id, lead_id=lead_id)
    reschedule = bool(existing) and (
        _looks_like_reschedule(analysis) or str(analysis.get("intent") or "").lower().find("reschedul") >= 0
    )
    appointment_type = _appointment_type_from_details(details, existing if reschedule else None)
    reschedules_id = (existing or {}).get("id") if reschedule else None

    # No time parseable, or day-only: a pending request with a NULL
    # slot; the agent picks the exact slot at confirm time. Never dropped.
    if not after or provenance.get("kind") in ("day", "missing"):
        request, _action = create_request(
            user_id,
            lead_id,
            channel="sms" if source_ref_type == "sms" else "voice",
            source_ref_type=source_ref_type,
            source_ref_id=source_ref_id,
            raw_request_text=raw_request_text,
            requested_start_at=None,
            requested_end_at=None,
            provenance=provenance,
            appointment_type=appointment_type,
            reschedules_appointment_id=reschedules_id,
            timezone_name=timezone_name,
        )
        return {
            "action": "requested",
            "request": request,
            "message": build_confirmation_request_text(
                None, None, timezone_name, provenance.get("kind")
            ),
            "provenance": provenance,
        }

    # A time was parsed: hold the request when the window has open
    # slots. The request holds the PARSED slot (the agent picks the
    # exact slot at confirm time for window kinds); it never books.
    slots = find_available_slots(
        user_id,
        after=after,
        before=before,
        limit=6,
        exclude_appointment_id=reschedules_id,
    )
    if slots:
        slot_start = _parse_iso(after)
        slot_end = _parse_iso(before)
        request, _action = create_request(
            user_id,
            lead_id,
            channel="sms" if source_ref_type == "sms" else "voice",
            source_ref_type=source_ref_type,
            source_ref_id=source_ref_id,
            raw_request_text=raw_request_text,
            requested_start_at=slot_start.isoformat() if slot_start else None,
            requested_end_at=slot_end.isoformat() if slot_end else None,
            provenance=provenance,
            appointment_type=appointment_type,
            reschedules_appointment_id=reschedules_id,
            timezone_name=timezone_name,
        )
        return {
            "action": "requested",
            "request": request,
            "message": build_confirmation_request_text(
                slot_start.isoformat() if slot_start else None,
                slot_end.isoformat() if slot_end else None,
                timezone_name,
                provenance.get("kind"),
            ),
            "provenance": provenance,
        }

    # Nothing open in the requested window: offer nearby alternatives.
    nearby = find_available_slots(
        user_id,
        after=after,
        limit=3,
        exclude_appointment_id=reschedules_id,
    )
    if nearby:
        labels = " or ".join(format_slot_sms(s) for s in nearby[:2])
        if exact:
            message = f"That time is booked, but {labels} are available. Would either work?"
        else:
            message = f"That window is booked, but {labels} are available. Would either work?"
        return {
            "action": "offer",
            "alternatives": nearby,
            "message": message,
            "provenance": provenance,
        }
    return {"action": "none", "provenance": provenance}


def _looks_like_reschedule(analysis: dict) -> bool:
    blob = f"{analysis.get('intent') or ''} {analysis.get('summary') or ''}".lower()
    return "reschedul" in blob or "can't meet" in blob or "cannot meet" in blob or "move" in blob
