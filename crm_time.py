"""Shared timezone-aware follow-up classification.

All CRM surfaces that bucket follow-ups (Follow-ups page, Leads Calendar,
Dashboard pipeline cards, destination filters, and count APIs) must use these
helpers so card counts always match filtered destination lists.

Timestamps are stored as ISO-8601 TEXT (typically UTC). Classification uses the
authenticated account's IANA timezone (default America/Denver), never the
Railway server timezone and never a browser offset alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "America/Denver"

# Open / pending follow-ups only. Completed and cancelled are excluded from
# overdue / today / this-week / upcoming open counts.
OPEN_FOLLOW_UP_STATUSES = frozenset({"pending"})


def resolve_zone(timezone_name: str | None) -> ZoneInfo:
    name = (timezone_name or "").strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_TIMEZONE)


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_iso_dt(value) -> datetime | None:
    """Parse stored due_at TEXT into an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return ensure_utc(dt)


def to_utc_iso(dt: datetime) -> str:
    return ensure_utc(dt).isoformat()


@dataclass(frozen=True)
class FollowUpWindows:
    """Local calendar boundaries for follow-up classification.

    Boundaries are computed in the account timezone, then exposed in UTC for
    safe comparisons against stored UTC timestamps.
    """

    timezone_name: str
    now_utc: datetime
    now_local: datetime
    start_today_local: datetime
    start_tomorrow_local: datetime
    start_next_week_local: datetime
    start_today_utc: datetime
    start_tomorrow_utc: datetime
    start_next_week_utc: datetime

    @property
    def local_date(self) -> str:
        return self.start_today_local.strftime("%Y-%m-%d")


def compute_follow_up_windows(
    timezone_name: str | None = None, *, now: datetime | None = None
) -> FollowUpWindows:
    """Compute classification windows for an account timezone.

    Monday–Sunday calendar week: start_next_week is local midnight at the
    beginning of the next Monday.
    """
    tz = resolve_zone(timezone_name)
    tz_name = getattr(tz, "key", None) or (timezone_name or DEFAULT_TIMEZONE)
    if now is None:
        now_utc = datetime.now(timezone.utc)
    else:
        now_utc = ensure_utc(now)

    now_local = now_utc.astimezone(tz)
    start_today_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_tomorrow_local = start_today_local + timedelta(days=1)
    # weekday(): Monday=0 … Sunday=6. Days until next Monday (always forward).
    days_until_next_monday = 7 - start_today_local.weekday()
    if days_until_next_monday == 0:
        days_until_next_monday = 7
    start_next_week_local = start_today_local + timedelta(days=days_until_next_monday)

    return FollowUpWindows(
        timezone_name=str(tz_name),
        now_utc=now_utc,
        now_local=now_local,
        start_today_local=start_today_local,
        start_tomorrow_local=start_tomorrow_local,
        start_next_week_local=start_next_week_local,
        start_today_utc=start_today_local.astimezone(timezone.utc),
        start_tomorrow_utc=start_tomorrow_local.astimezone(timezone.utc),
        start_next_week_utc=start_next_week_local.astimezone(timezone.utc),
    )


def local_calendar_date(due_at, timezone_name: str | None = None) -> str:
    """Return YYYY-MM-DD of due_at in the account timezone."""
    dt = parse_iso_dt(due_at)
    if dt is None:
        return str(due_at or "")[:10]
    return dt.astimezone(resolve_zone(timezone_name)).strftime("%Y-%m-%d")


def classify_open_follow_up(due_at, windows: FollowUpWindows) -> str | None:
    """Classify a pending follow-up into overdue | today | upcoming.

    Sections are mutually exclusive:
    - overdue: due_at < start_today
    - today: start_today <= due_at < start_tomorrow
    - upcoming: due_at >= start_tomorrow
    """
    due = parse_iso_dt(due_at)
    if due is None:
        return None
    if due < windows.start_today_utc:
        return "overdue"
    if due < windows.start_tomorrow_utc:
        return "today"
    return "upcoming"


def is_overdue(due_at, windows: FollowUpWindows) -> bool:
    return classify_open_follow_up(due_at, windows) == "overdue"


def is_due_today(due_at, windows: FollowUpWindows) -> bool:
    return classify_open_follow_up(due_at, windows) == "today"


def is_this_week(due_at, windows: FollowUpWindows) -> bool:
    """Pending follow-ups from now until next Monday local midnight.

    Excludes overdue (before start_today) and anything already in the past
    relative to now_local. Does not include dates on/after next Monday.
    """
    due = parse_iso_dt(due_at)
    if due is None:
        return False
    return windows.now_utc <= due < windows.start_next_week_utc


def is_upcoming(due_at, windows: FollowUpWindows) -> bool:
    """True when due_at is at or after the current local instant."""
    due = parse_iso_dt(due_at)
    if due is None:
        return False
    return due >= windows.now_utc


def is_open_status(status) -> bool:
    return str(status or "").strip().lower() in OPEN_FOLLOW_UP_STATUSES


def matches_range(due_at, range_key: str, windows: FollowUpWindows) -> bool:
    """Whether a pending follow-up matches a dashboard/filter range key."""
    key = str(range_key or "").strip().lower()
    bucket = classify_open_follow_up(due_at, windows)
    if key in {"today", "due_today"}:
        return bucket == "today"
    if key == "overdue":
        return bucket == "overdue"
    if key in {"this_week", "this-week", "week"}:
        return is_this_week(due_at, windows)
    if key == "upcoming":
        # Agenda "Upcoming" section = future calendar days (exclusive of today).
        return bucket == "upcoming"
    if key in {"all", "open"}:
        return True
    return False


def local_dt_to_utc_iso(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
    timezone_name: str | None = None,
) -> str:
    """Build a UTC ISO timestamp from a wall-clock time in timezone_name."""
    tz = resolve_zone(timezone_name)
    local = datetime(year, month, day, hour, minute, second, tzinfo=tz)
    return local.astimezone(timezone.utc).isoformat()
