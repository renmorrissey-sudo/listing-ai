"""Spend alerts for the AI Harness (D2).

No external service in this step — no Slack, PagerDuty, or Sentry. An
alert is a row in the append-only ``harness_audit`` log plus a structured
log line, so it is visible in the same place as every other harness
decision. Callers (currently :func:`harness.policy.decide`) invoke
:func:`record_spend_alert` when a tenant's month-to-date spend crosses
80% of the monthly cap (``level="warning"``) or reaches 100%
(``level="limit"``).

Dedup: at most one alert per tenant per level per UTC month is written.
The gate calls ``decide()`` on every proposed action, so without this the
audit log would gain one row per gated action while a tenant sits over a
threshold. The level and period are embedded in ``gate_reason`` so the
dedup query can match on them; the spend/cap figures in the reason are
the values observed when the alert first fired that month.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import db

logger = logging.getLogger(__name__)

# Levels record_spend_alert understands.
LEVELS = ("warning", "limit")

# gate_decision recorded on the audit row for each level.
_LEVEL_GATE_DECISION = {"warning": "DRAFT", "limit": "BLOCK"}


def _current_period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def record_spend_alert(user_id, spend_cents, cap_cents, level) -> int | None:
    """Record a spend alert and return its ``harness_audit`` row id.

    ``level`` is ``"warning"`` (spend crossed 80% of the cap) or
    ``"limit"`` (spend reached 100%). Writes one audit row
    (``actor="system"``, ``action="spend_alert"``,
    ``gate_decision="DRAFT"`` for a warning / ``"BLOCK"`` for the limit)
    and a structured log line. Returns ``None`` without raising when the
    level is unknown, ``user_id`` is None, the audit table is missing, or
    an identical alert for this tenant/level/month already exists — an
    alert failure must never break the policy gate.
    """
    if level not in LEVELS:
        logger.warning("spend alert ignored: unknown level %r", level)
        return None
    if user_id is None:
        return None
    period = _current_period()
    gate_reason = (
        f"spend_alert level={level} period={period} "
        f"spend_cents={int(spend_cents or 0)} cap_cents={int(cap_cents or 0)}"
    )
    try:
        from harness.audit import record_audit

        with db.get_db() as conn:
            existing = conn.execute(
                """
                SELECT id FROM harness_audit
                WHERE user_id = ? AND action = 'spend_alert'
                  AND gate_reason LIKE ? AND created_at LIKE ?
                LIMIT 1
                """,
                (user_id, f"%level={level}%", f"{period}%"),
            ).fetchone()
        if existing:
            return int(dict(existing)["id"])
        audit_id = record_audit(
            user_id,
            actor="system",
            action="spend_alert",
            tool="spend_alert",
            mode=None,
            gate_decision=_LEVEL_GATE_DECISION[level],
            gate_reason=gate_reason,
            cost_cents=int(spend_cents or 0),
        )
    except Exception as exc:  # audit table missing, DB down, etc.
        logger.warning("spend alert could not be recorded: %s", exc)
        return None
    logger.warning(
        "harness spend_alert user_id=%s level=%s period=%s spend_cents=%s cap_cents=%s",
        user_id, level, period, int(spend_cents or 0), int(cap_cents or 0),
    )
    return audit_id
