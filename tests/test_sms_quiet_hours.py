"""Quiet hours must be evaluated in the account's timezone, not server UTC."""

from datetime import datetime, timezone

import config
import db
import sms_authorization as sa


def _set_tz(user_id, tz_name):
    db.update_business_profile(user_id, timezone=tz_name)


def test_evening_utc_is_not_quiet_hours_locally(two_users, monkeypatch):
    """23:46 UTC is 17:46 in Denver — well outside the 21:00-08:00 quiet window."""
    u1, _ = two_users
    _set_tz(u1, "America/Denver")
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 21)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 8)
    now = datetime(2026, 8, 10, 23, 46, tzinfo=timezone.utc)
    assert sa._in_quiet_hours(u1, now=now) is False


def test_local_late_night_is_quiet_hours(two_users, monkeypatch):
    """05:30 UTC is 23:30 in Denver — inside the quiet window."""
    u1, _ = two_users
    _set_tz(u1, "America/Denver")
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 21)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 8)
    now = datetime(2026, 8, 11, 5, 30, tzinfo=timezone.utc)
    assert sa._in_quiet_hours(u1, now=now) is True


def test_local_morning_after_window_is_allowed(two_users, monkeypatch):
    """15:00 UTC is 09:00 in Denver — quiet window has ended."""
    u1, _ = two_users
    _set_tz(u1, "America/Denver")
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 21)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 8)
    now = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)
    assert sa._in_quiet_hours(u1, now=now) is False


def test_eastern_account_uses_its_own_clock(two_users, monkeypatch):
    """The same UTC instant can be quiet for one account timezone but not another."""
    u1, u2 = two_users
    _set_tz(u1, "America/New_York")
    _set_tz(u2, "America/Los_Angeles")
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 21)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 8)
    # 01:30 UTC = 21:30 New York (quiet) = 18:30 Los Angeles (allowed).
    now = datetime(2026, 8, 11, 1, 30, tzinfo=timezone.utc)
    assert sa._in_quiet_hours(u1, now=now) is True
    assert sa._in_quiet_hours(u2, now=now) is False


def test_unset_timezone_defaults_to_denver(two_users, monkeypatch):
    u1, _ = two_users
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 21)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 8)
    # 23:46 UTC = 17:46 America/Denver default — allowed.
    now = datetime(2026, 8, 10, 23, 46, tzinfo=timezone.utc)
    assert sa._in_quiet_hours(u1, now=now) is False


def test_equal_start_end_disables_quiet_hours(two_users, monkeypatch):
    u1, _ = two_users
    _set_tz(u1, "America/Denver")
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_START", 0)
    monkeypatch.setattr(config, "SMS_QUIET_HOURS_END", 0)
    now = datetime(2026, 8, 11, 5, 30, tzinfo=timezone.utc)
    assert sa._in_quiet_hours(u1, now=now) is False
