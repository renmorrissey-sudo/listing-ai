"""Backfill leads.phone_normalized for pre-existing NULL rows (050).

The phone_normalized fix (PR #56) set phone_normalized on insert
(``external_leads_db.create_external_lead``) and on edit
(``db.update_lead_contact_info``), but rows created before the fix
keep NULL phone_normalized — including Lead A (id 20, the III-6 test
lead), which is why an inbound from +15551234019 still routes to
stray lead 22 in tenant 1 instead of matching Lead A. This migration
fills those rows: NULL phone_normalized + a phone -> the E.164
normalized value; un-normalizable stored numbers get '' (the
migration 047 convention); rows with no phone are left untouched and
keep NULL.

Data safety
------------
Additive and forward-only. No schema change (the column already
exists, added by 047); no new table; no seed rows; the backfill only
writes phone_normalized on rows where it is currently NULL, and only
where a phone is present. Idempotent: a re-run finds no NULL rows
with a phone and updates nothing. Postcondition checks run INLINE
in the upgrade functions (the runner does not call a migration's
own ``verify()``) so a failure rolls the migration back. The
Postgres checks only read a COUNT(*) probe — the empty-Postgres
test fake answers COUNT(*) with a zero-count row, so the check
passes there; on a real database the count is exact. No LIKE in
the new SQL (equality / IS NULL / TRIM only).

Normalizer
-----------
The E.164 routine below is a VERBATIM copy of 047's migration-local
mirror of ``lead_service.normalize_phone_e164`` — the same routine,
not a new one. Migrations must be self-contained and never import
app code (047 docstring): importing ``db`` / ``lead_service`` from a
migration would circle through the runner (``db.init_db`` imports
the runner, the runner imports the migration), and the migration
must stay runnable against any future code state.

Rollback
--------
Additive. Rollback is a code-only revert — the backfilled values
stay, and no data is lost. No down-migration is run (the release
has no down-migrations by design).
"""

import logging
import re

VERSION = "050_phone_normalized_backfill"

logger = logging.getLogger(__name__)

_E164_RE = re.compile(r"\+[1-9]\d{9,14}\Z")


def _normalize_e164(raw):
    """Migration-local E.164 mirror of lead_service.normalize_phone_e164.

    Verbatim copy of the 047 mirror. Only digits and common
    formatting (+ - ( ) space .) are allowed; anything else (letters,
    extension markers) is un-normalizable and returns "". Kept local
    so the migration never imports app code.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if re.search(r"[^0-9+\-().\s]", text):
        return ""
    if text.count("+") > 1 or ("+" in text[1:]):
        return ""
    digits = re.sub(r"\D", "", text)
    if text.startswith("+"):
        candidate = "+" + digits
    elif len(digits) == 10:
        candidate = "+1" + digits
    elif len(digits) == 11 and digits.startswith("1"):
        candidate = "+" + digits
    else:
        return ""
    return candidate if _E164_RE.fullmatch(candidate) else ""


def _columns_sqlite(conn, table):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    names = set()
    for row in rows:
        try:
            names.add(row["name"])
        except (KeyError, TypeError, IndexError):
            names.add(row[1])
    return names


def _has_column_postgres(conn, table, column):
    row = conn.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        """,
        (table, column),
    ).fetchone()
    return row is not None


def _ensure_phone_normalized_column(conn, dialect):
    """Defensive: 047 added the column; ensure it exists so the
    backfill never fails if 050 ever runs ahead of 047."""
    if dialect == "postgresql":
        if not _has_column_postgres(conn, "leads", "phone_normalized"):
            conn.execute(
                "ALTER TABLE leads ADD COLUMN IF NOT EXISTS "
                "phone_normalized TEXT"
            )
        return
    if "phone_normalized" not in _columns_sqlite(conn, "leads"):
        conn.execute("ALTER TABLE leads ADD COLUMN phone_normalized TEXT")


def _backfill_phone_normalized(conn, dialect):
    """Fill leads.phone_normalized from phone_number (Python-side,
    so both engines share the one routine). Only rows where
    phone_normalized IS NULL; rows with no phone (NULL / empty /
    whitespace phone_number) are left untouched and keep NULL.
    Un-normalizable stored numbers get ''. Returns rows updated."""
    rows = conn.execute(
        "SELECT id, phone_number FROM leads "
        "WHERE phone_normalized IS NULL"
    ).fetchall()
    ph = "%s" if dialect == "postgresql" else "?"
    updated = 0
    for raw in rows:
        try:
            row = dict(raw)
            lead_id = row.get("id")
            phone = row.get("phone_number")
        except (TypeError, ValueError):
            try:
                lead_id, phone = raw[0], raw[1]
            except (TypeError, IndexError):
                continue
        if lead_id is None:
            continue
        if phone is None or not str(phone).strip():
            continue  # no phone: leave phone_normalized NULL
        conn.execute(
            f"UPDATE leads SET phone_normalized = {ph} WHERE id = {ph}",
            (_normalize_e164(phone), lead_id),
        )
        updated += 1
    return updated


def _check_backfill_complete(conn, dialect):
    """Inline postcondition: no row with a non-empty phone_number may
    still have NULL phone_normalized. Raises so the runner rolls the
    migration back.

    Safe against the empty-Postgres test fake: the fake answers a
    COUNT(*) probe with a zero-count row ({"c": 0}), so the check
    passes there; on a real database the count is exact. The check
    only reads row["c"] (dict-style) with a tuple fallback, never
    assuming more than one row.
    """
    row = conn.execute(
        """
        SELECT COUNT(*) AS c FROM leads
        WHERE phone_normalized IS NULL
          AND phone_number IS NOT NULL
          AND TRIM(phone_number) != ''
        """
    ).fetchone()
    try:
        count = int(row["c"])
    except (KeyError, TypeError, IndexError):
        count = int(row[0])
    if count:
        raise RuntimeError(
            "050_phone_normalized_backfill failed: "
            f"{count} lead(s) with a phone still have NULL phone_normalized"
        )


def upgrade_sqlite(conn):
    _ensure_phone_normalized_column(conn, "sqlite")
    updated = _backfill_phone_normalized(conn, "sqlite")
    logger.info(
        "050_phone_normalized_backfill: phone_normalized backfilled=%d",
        updated,
    )
    _check_backfill_complete(conn, "sqlite")


def upgrade_postgres(conn):
    _ensure_phone_normalized_column(conn, "postgresql")
    updated = _backfill_phone_normalized(conn, "postgresql")
    logger.info(
        "050_phone_normalized_backfill: phone_normalized backfilled=%d",
        updated,
    )
    _check_backfill_complete(conn, "postgresql")


def verify(conn, dialect):
    _check_backfill_complete(conn, dialect)


def verify_postgres(conn):
    _check_backfill_complete(conn, "postgresql")


def verify_sqlite(conn):
    _check_backfill_complete(conn, "sqlite")
