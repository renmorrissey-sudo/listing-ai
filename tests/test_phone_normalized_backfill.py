"""phone_normalized backfill (migration 050): pre-existing NULL rows.

The phone_normalized fix (PR #56) set phone_normalized on insert and
on edit, but existing rows created before the fix keep NULL
phone_normalized — including Lead A (id 20, the III-6 test lead),
which is why an inbound from +15551234019 still routes to stray
lead 22 in tenant 1. Migration 050 backfills those rows: NULL
phone_normalized + a phone -> the E.164 normalized value;
un-normalizable -> "" (the migration 047 convention); no phone ->
left untouched (stays NULL).

Red-before-green: the tests import the 050 migration module and call
its upgrade directly (the 035 precedent). Before the fix the module
does not exist, so every test fails with ModuleNotFoundError; after
the fix they pass. The assertions are what discriminate correctness:
a wrong backfill (e.g. "" for no-phone rows, or skipping NULL rows)
fails post-fix.
"""

import importlib
import os
import tempfile
import uuid

import pytest

import config
import db
from migrations.runner import apply_pending_migrations


def _unique_phone():
    """A unique, valid E.164 US number (+1 555 + 7 digits)."""
    return f"+1555{str(uuid.uuid4().int)[-7:]}"


def _formatted(phone):
    """The same number in a common formatted form, "(555) 123-4567"."""
    digits = phone[2:]  # strip "+1" -> 10 digits
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def _m050():
    return importlib.import_module(
        "migrations.versions.050_phone_normalized_backfill"
    )


def _seed_lead(user_id, phone_number, phone_normalized):
    """Insert a lead row directly (bypassing the app create paths,
    which now set phone_normalized) — this is exactly what a
    pre-existing production row looks like."""
    now = "2026-11-25T00:00:00+00:00"
    with db.get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO leads
                (user_id, name, phone_number, phone_normalized,
                 status, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'new', 'sms', ?, ?)
            """,
            (user_id, "Backfill Test Lead", phone_number, phone_normalized, now, now),
        )
        return cur.lastrowid


def _lead_row(lead_id):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT id, phone_number, phone_normalized FROM leads WHERE id = ?",
            (lead_id,),
        ).fetchone()
    return dict(row)


@pytest.fixture
def empty_sqlite_db(monkeypatch):
    """Completely empty SQLite file (no tables) wired as the app database."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["DATABASE_PATH"] = path
    os.environ.pop("DATABASE_URL", None)
    monkeypatch.setattr(config, "DATABASE_PATH", path)
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "DB_ENGINE", "sqlite")
    monkeypatch.setattr(config, "APP_ENV", "test")
    yield path
    try:
        os.remove(path)
    except OSError:
        pass


def test_backfill_normalizes_null_row(two_users):
    """A NULL row with a phone gets the E.164 normalized value — and
    becomes routable (the Lead A scenario)."""
    u1, _ = two_users
    db.init_db()
    phone_e164 = _unique_phone()
    phone_raw = _unique_phone()
    phone_formatted = _formatted(phone_raw)
    lead_e164 = _seed_lead(u1, phone_e164, None)
    lead_fmt = _seed_lead(u1, phone_formatted, None)
    assert _lead_row(lead_e164)["phone_normalized"] is None
    assert _lead_row(lead_fmt)["phone_normalized"] is None

    with db.get_db() as conn:
        _m050().upgrade_sqlite(conn)

    assert _lead_row(lead_e164)["phone_normalized"] == phone_e164
    assert _lead_row(lead_fmt)["phone_normalized"] == phone_raw

    # The routing lookup (owners by phone) must now find these leads.
    owners = db.list_lead_owners_by_phone(phone_e164)
    assert len(owners) == 1
    assert owners[0]["user_id"] == u1
    assert owners[0]["lead_id"] == lead_e164


def test_backfill_unormalizable_becomes_empty_string(two_users):
    """A NULL row whose phone cannot be normalized gets "" (the
    migration 047 convention), not NULL and not an error."""
    u1, _ = two_users
    db.init_db()
    bad_phone = f"ext-x{uuid.uuid4().hex[:8]}"  # letters: un-normalizable
    lead_id = _seed_lead(u1, bad_phone, None)
    assert _lead_row(lead_id)["phone_normalized"] is None

    with db.get_db() as conn:
        _m050().upgrade_sqlite(conn)

    row = _lead_row(lead_id)
    assert row["phone_normalized"] == ""
    assert row["phone_number"] == bad_phone


def test_backfill_leaves_no_phone_row_untouched(two_users):
    """A NULL row with no phone keeps phone_normalized NULL — the
    backfill does not write "" for a missing phone."""
    u1, _ = two_users
    db.init_db()
    lead_id = _seed_lead(u1, "", None)
    assert _lead_row(lead_id)["phone_normalized"] is None

    with db.get_db() as conn:
        _m050().upgrade_sqlite(conn)

    row = _lead_row(lead_id)
    assert row["phone_normalized"] is None
    assert row["phone_number"] == ""


def test_backfill_is_idempotent(two_users):
    """Running the upgrade twice changes nothing the second time (the
    backfill only touches NULL rows; the second run is a no-op)."""
    u1, _ = two_users
    db.init_db()
    phone = _unique_phone()
    lead_id = _seed_lead(u1, phone, None)

    with db.get_db() as conn:
        _m050().upgrade_sqlite(conn)
    assert _lead_row(lead_id)["phone_normalized"] == phone

    with db.get_db() as conn:
        _m050().upgrade_sqlite(conn)
    assert _lead_row(lead_id)["phone_normalized"] == phone


def test_backfill_leaves_already_set_rows_untouched(two_users):
    """A row whose phone_normalized is already set keeps its value —
    the backfill never rewrites a set value. (This is the stray-lead-22
    guard: lead 22 already has phone_normalized set and must not
    change.)"""
    u1, _ = two_users
    db.init_db()
    phone = _unique_phone()
    other = _unique_phone()
    lead_id = _seed_lead(u1, phone, other)

    with db.get_db() as conn:
        _m050().upgrade_sqlite(conn)

    row = _lead_row(lead_id)
    assert row["phone_normalized"] == other
    assert row["phone_number"] == phone


def test_050_applies_and_stamps_via_runner(empty_sqlite_db):
    """050 is in the runner list, applies on a fresh DB, and is stamped."""
    apply_pending_migrations()
    with db.get_db() as conn:
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    assert "050_phone_normalized_backfill" in {row["version"] for row in rows}
