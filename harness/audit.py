"""Single append-only audit log for the AI Harness (Phase 1).

Every harness action writes one row here (actor, action, tool, target,
mode, gate decision + reason, cost, request/correlation ids). Rows are
never updated and never deleted by the application — there are
deliberately no update/delete functions in this module. Existing logs
(ask_topai_commands, sms_audit_events, consent_audit_events,
stripe_webhook_events, lead_activities) are not replaced; new harness
actions write here.

Every query is user_id-scoped. All SQL uses ``?`` placeholders;
db_backend.CompatConnection translates them to ``%s`` for Postgres.
Timestamps are UTC ISO strings. No new dependencies.
"""

from __future__ import annotations

from datetime import datetime, timezone

import db


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row(row):
    return dict(row) if row is not None else None


def record_audit(user_id, actor, action, tool=None, target_type=None,
                 target_id=None, mode=None, gate_decision=None,
                 gate_reason=None, cost_cents=0, request_id=None,
                 correlation_id=None):
    """Append one audit row and return its id.

    ``actor`` is 'user' | 'ai' | 'system' | 'admin'. ``gate_decision`` is
    the deterministic policy-gate outcome ('ALLOW' | 'DRAFT' | 'BLOCK' |
    'DEFER') with ``gate_reason`` explaining it; both are None for
    non-gated actions.
    """
    now = _now()
    with db.get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO harness_audit
                (user_id, actor, action, tool, target_type, target_id,
                 mode, gate_decision, gate_reason, cost_cents,
                 request_id, correlation_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                actor,
                action,
                tool,
                target_type,
                target_id,
                mode,
                gate_decision,
                gate_reason,
                int(cost_cents or 0),
                request_id,
                correlation_id,
                now,
            ),
        )
        return cur.lastrowid


def list_audit(user_id, limit=100):
    """List a tenant's audit rows, newest first."""
    with db.get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM harness_audit
            WHERE user_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [_row(r) for r in rows]
