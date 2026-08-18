"""TCPA-style SMS quiet hours: 9:00 PM–8:00 AM in the recipient's local time.

This is application-enforced (not Telnyx). When the recipient timezone can be
inferred from a US/Canada NANP area code, that zone is used; otherwise the
account timezone (default America/Denver) is used.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import config
import db
from crm_time import DEFAULT_TIMEZONE, resolve_zone, to_utc_iso

QUIET_HOURS_MSG = (
    "This recipient is currently within SMS quiet hours. "
    "You can schedule this message for the next permitted sending time."
)

# US/Canada NANP NPA → IANA zone. Unknown NPAs fall back to the account timezone.
_NPA_GROUPS = {
    "America/New_York": (
        "201,202,203,207,212,215,216,220,223,234,239,240,248,267,272,276,301,302,"
        "304,305,313,321,330,332,339,347,351,352,386,401,404,407,410,412,413,419,"
        "434,440,443,445,470,475,478,484,508,513,516,517,518,540,551,561,567,570,"
        "571,578,581,582,585,586,603,607,609,610,614,616,617,631,640,646,667,678,"
        "680,681,703,704,706,716,717,718,724,727,732,734,740,743,754,757,762,772,"
        "774,781,786,802,803,804,810,813,814,828,835,838,843,845,848,854,856,857,"
        "859,860,862,863,864,878,904,908,910,912,914,917,919,929,934,937,941,947,"
        "954,959,973,978,980,984,989"
    ),
    "America/Chicago": (
        "205,210,214,217,218,219,225,228,251,254,256,262,270,281,309,312,314,316,"
        "318,319,320,325,331,334,337,346,361,364,409,414,417,430,432,469,479,501,"
        "504,507,512,515,531,534,539,557,563,573,580,608,612,615,618,620,629,630,"
        "636,641,651,659,660,662,682,708,712,713,715,726,731,737,769,773,785,"
        "806,815,816,817,830,832,847,870,872,901,903,913,918,920,930,936,938,940,"
        "945,952,956,972,979,985"
    ),
    "America/Denver": (
        "303,307,385,406,435,505,575,719,720,801,970"
    ),
    "America/Phoenix": "480,520,602,623,928",
    "America/Los_Angeles": (
        "206,209,213,253,279,310,323,341,360,408,415,424,442,458,503,509,510,530,"
        "541,559,562,619,626,628,650,657,661,669,702,707,714,725,747,760,775,805,"
        "818,820,831,858,909,916,925,949,951,971"
    ),
    "America/Anchorage": "907",
    "Pacific/Honolulu": "808",
    "America/Puerto_Rico": "787,939",
    "America/Toronto": "226,249,289,343,365,416,437,519,548,613,647,705,807,905",
    "America/Winnipeg": "204,431,584",
    "America/Regina": "306,639",
    "America/Edmonton": "368,403,587,780,825",
    "America/Vancouver": "236,250,604,672,778",
    "America/Halifax": "782,902",
    "America/St_Johns": "709",
}

_NPA_TO_TZ: dict[str, str] = {}
for _tz, _npas in _NPA_GROUPS.items():
    for _npa in _npas.replace(" ", "").split(","):
        if _npa:
            _NPA_TO_TZ[_npa] = _tz


def is_quiet_hours_block(message) -> bool:
    return "quiet hours" in str(message or "").lower()


def _digits(phone) -> str:
    return "".join(ch for ch in str(phone or "") if ch.isdigit())


def npa_from_phone(phone) -> str | None:
    digits = _digits(phone)
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:4]
    if len(digits) == 10:
        return digits[0:3]
    return None


def timezone_for_phone(phone) -> str | None:
    npa = npa_from_phone(phone)
    if not npa:
        return None
    return _NPA_TO_TZ.get(npa)


def timezone_for_sms(user_id, *, phone=None, lead=None) -> tuple[str, str]:
    """Return (iana_name, source) where source is recipient_area_code or account."""
    candidate = phone or (lead or {}).get("phone_number")
    recipient_tz = timezone_for_phone(candidate)
    if recipient_tz:
        return recipient_tz, "recipient_area_code"
    account_tz = (db.get_user_timezone(user_id) if user_id else None) or DEFAULT_TIMEZONE
    return account_tz, "account"


def _window():
    return int(config.SMS_QUIET_HOURS_START), int(config.SMS_QUIET_HOURS_END)


def _aware_utc(now) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def in_quiet_hours(user_id, now=None, *, phone=None, lead=None) -> bool:
    start, end = _window()
    if start == end:
        return False
    tz_name, _source = timezone_for_sms(user_id, phone=phone, lead=lead)
    hour = _aware_utc(now).astimezone(resolve_zone(tz_name)).hour
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def next_permitted_send_at(user_id, now=None, *, phone=None, lead=None) -> datetime:
    """UTC datetime when sending becomes allowed. Returns now if already allowed."""
    now_utc = _aware_utc(now)
    if not in_quiet_hours(user_id, now=now_utc, phone=phone, lead=lead):
        return now_utc
    tz_name, _source = timezone_for_sms(user_id, phone=phone, lead=lead)
    local = now_utc.astimezone(resolve_zone(tz_name))
    start, end = _window()
    target = local.replace(hour=end, minute=0, second=0, microsecond=0)
    if start > end and local.hour >= start:
        target = target + timedelta(days=1)
    if target <= local:
        target = target + timedelta(days=1)
    return target.astimezone(timezone.utc)


def format_local_send_time(dt_utc, tz_name) -> str:
    local = _aware_utc(dt_utc).astimezone(resolve_zone(tz_name))
    hour = local.strftime("%I").lstrip("0") or "12"
    return (
        f"{local.strftime('%A, %B ')}{local.day}, {local.year} "
        f"at {hour}:{local.strftime('%M %p')} ({tz_name})"
    )


def quiet_hours_schedule_info(user_id, now=None, *, phone=None, lead=None) -> dict:
    tz_name, source = timezone_for_sms(user_id, phone=phone, lead=lead)
    send_at = next_permitted_send_at(user_id, now=now, phone=phone, lead=lead)
    return {
        "error": QUIET_HOURS_MSG,
        "error_category": "quiet_hours",
        "can_schedule": True,
        "scheduled_for": to_utc_iso(send_at),
        "scheduled_for_local": format_local_send_time(send_at, tz_name),
        "timezone": tz_name,
        "timezone_source": source,
    }
