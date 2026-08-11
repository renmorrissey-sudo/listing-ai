"""Backfill lead_follow_ups from existing structured evidence.

Historical leads (from before the AI SMS coaching workflow started persisting
real follow-ups) may already have a concrete, structured next-action date on
file that never made it into lead_follow_ups:

  1. leads.next_follow_up_at is set (e.g. from a past voice call outcome) but
     there is no OPEN lead_follow_ups row for that lead.
  2. The lead's most recent lead_insights.raw_json contains a
     suggested_follow_up_at from a past Claude SMS coaching call, but there is
     no OPEN lead_follow_ups row for that lead.

This script reconciles both cases via the canonical crm_db.set_lead_follow_up
writer. It deliberately does NOT infer a follow-up from freeform text alone
(next_action, activity notes, etc.) -- only from these two structured fields,
per the "don't blindly turn every historical record into a follow-up" rule.

Safe to re-run: candidates are re-computed each run and a lead is skipped as
soon as it already has an open follow-up, so applying twice is a no-op the
second time. Always tenant-scoped (every candidate carries its own user_id).

Usage:
    python scripts/backfill_lead_follow_ups.py            # dry run (default)
    python scripts/backfill_lead_follow_ups.py --apply     # write the follow-ups
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import crm_db  # noqa: E402
from db import get_db  # noqa: E402


def _has_open_follow_up(conn, user_id, lead_id) -> bool:
    row = conn.execute(
        """
        SELECT id FROM lead_follow_ups
        WHERE user_id = ? AND lead_id = ? AND status = 'pending'
        LIMIT 1
        """,
        (user_id, lead_id),
    ).fetchone()
    return row is not None


def _candidates_from_next_follow_up_at(conn):
    """Leads with a denormalized next_follow_up_at but no open follow-up row."""
    rows = conn.execute(
        """
        SELECT id AS lead_id, user_id, next_follow_up_at AS due_at,
               follow_up_reason AS reason, follow_up_priority AS priority
        FROM leads
        WHERE next_follow_up_at IS NOT NULL AND next_follow_up_at != ''
        """
    ).fetchall()
    candidates = []
    for raw in rows:
        item = dict(raw)
        if _has_open_follow_up(conn, item["user_id"], item["lead_id"]):
            continue
        candidates.append(
            {
                "user_id": item["user_id"],
                "lead_id": item["lead_id"],
                "due_at": item["due_at"],
                "reason": item.get("reason") or "Follow up",
                "priority": item.get("priority") or "normal",
                "source": "leads.next_follow_up_at",
            }
        )
    return candidates


def _candidates_from_lead_insights(conn):
    """Leads whose most recent Claude coaching insight suggested a follow-up
    date that was never turned into a real lead_follow_ups record."""
    rows = conn.execute(
        """
        SELECT i.lead_id, i.user_id, i.raw_json
        FROM lead_insights i
        INNER JOIN (
            SELECT lead_id, user_id, MAX(id) AS max_id
            FROM lead_insights
            WHERE raw_json IS NOT NULL
            GROUP BY lead_id, user_id
        ) latest ON latest.max_id = i.id
        """
    ).fetchall()
    candidates = []
    for raw in rows:
        item = dict(raw)
        try:
            payload = json.loads(item.get("raw_json") or "{}")
        except (TypeError, ValueError):
            continue
        due_at = payload.get("suggested_follow_up_at")
        if not due_at:
            continue
        if _has_open_follow_up(conn, item["user_id"], item["lead_id"]):
            continue
        reason = (
            payload.get("suggested_follow_up_reason")
            or payload.get("recommended_next_action")
            or "Follow up with lead"
        )
        candidates.append(
            {
                "user_id": item["user_id"],
                "lead_id": item["lead_id"],
                "due_at": due_at,
                "reason": reason,
                "priority": "normal",
                "source": "lead_insights.raw_json",
            }
        )
    return candidates


def find_candidates():
    with get_db() as conn:
        candidates = _candidates_from_next_follow_up_at(
            conn
        ) + _candidates_from_lead_insights(conn)
    # A lead can appear from both sources; prefer the leads.next_follow_up_at
    # candidate (already listed first) since it's the more authoritative field.
    seen = set()
    deduped = []
    for candidate in candidates:
        key = (candidate["user_id"], candidate["lead_id"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Write changes (default is dry-run)"
    )
    args = parser.parse_args()

    candidates = find_candidates()
    if not candidates:
        print("No leads need backfilling.")
        return

    print(
        f"Found {len(candidates)} lead(s) with structured follow-up evidence "
        "but no open lead_follow_ups record:"
    )
    for candidate in candidates:
        print(
            f"  user={candidate['user_id']} lead={candidate['lead_id']} "
            f"due_at={candidate['due_at']} reason={candidate['reason']!r} "
            f"(source: {candidate['source']})"
        )

    if not args.apply:
        print(
            "\nDry run only -- no changes made. Re-run with --apply to create "
            "these follow-ups."
        )
        return

    created, skipped = 0, 0
    for candidate in candidates:
        result, error = crm_db.set_lead_follow_up(
            candidate["user_id"],
            candidate["lead_id"],
            candidate["due_at"],
            candidate["reason"],
            priority=candidate["priority"],
        )
        if error:
            print(f"  SKIP user={candidate['user_id']} lead={candidate['lead_id']}: {error}")
            skipped += 1
            continue
        if result and result.get("created"):
            created += 1
        else:
            skipped += 1
    print(f"\nDone. Created {created} follow-up(s), skipped {skipped}.")


if __name__ == "__main__":
    main()
