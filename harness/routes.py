"""Harness HTTP API (Phase 1): one read-only status route.

GET /api/harness/status -- login + subscription required, same decorators
as other subscriber routes. Returns the current tenant's AI Automation
status as JSON. All counts are user_id-scoped. Missing harness tables (the
Phase 1 migration not yet applied) degrade to defaults/zeroes instead of
erroring.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify

import auth
import db

logger = logging.getLogger(__name__)

harness_bp = Blueprint("harness", __name__)


def _harness_settings(user_id):
    """(mode, paused, ai_enabled) for a tenant; Drafts Only defaults.

    Prefers the sibling-owned ``harness.policy.get_settings``; falls back
    to a direct ``harness_settings`` read when that module is absent.
    """
    try:
        from harness.policy import get_settings

        settings = get_settings(user_id) or {}
        return (
            str(settings.get("mode") or "drafts_only").strip().lower() or "drafts_only",
            bool(settings.get("paused")),
            bool(settings.get("ai_enabled", True)),
        )
    except Exception:
        pass
    mode, paused, ai_enabled = "drafts_only", False, True
    try:
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT mode, paused, ai_enabled FROM harness_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
    except Exception:
        row = None
    if row:
        mode = (row["mode"] or "drafts_only").strip().lower() or "drafts_only"
        paused = bool(row["paused"])
        ai_enabled = bool(row["ai_enabled"]) if row["ai_enabled"] is not None else True
    return mode, paused, ai_enabled


def _count(user_id, sql, params):
    # NOTE: db rows are keyed by column name only (no integer indexing),
    # so every count query must alias COUNT(*) AS n.
    try:
        with db.get_db() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["n"]) if row and row["n"] is not None else 0
    except Exception:
        # Harness tables do not exist yet (migration 029 not applied).
        return 0


def get_harness_status(user_id):
    """Read-only status dict for one tenant. Never raises for missing tables."""
    mode, paused, ai_enabled = _harness_settings(user_id)
    pending_approvals = _count(
        user_id,
        "SELECT COUNT(*) AS n FROM harness_approvals WHERE user_id = ? AND status = 'pending'",
        (user_id,),
    )
    pending_jobs = _count(
        user_id,
        "SELECT COUNT(*) AS n FROM harness_jobs WHERE user_id = ? AND status IN ('pending', 'claimed', 'running')",
        (user_id,),
    )
    since_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    jobs_24h = _count(
        user_id,
        "SELECT COUNT(*) AS n FROM harness_jobs WHERE user_id = ? AND created_at >= ?",
        (user_id, since_24h),
    )
    return {
        "mode": mode,
        "paused": paused,
        "ai_enabled": ai_enabled,
        "pending_approvals": pending_approvals,
        "pending_jobs": pending_jobs,
        "jobs_24h": jobs_24h,
    }


@harness_bp.route("/api/harness/status", methods=["GET"])
@auth.subscription_required
def api_harness_status():
    user = auth.get_current_user()
    if not user:
        return jsonify({"error": "Please log in to continue."}), 401
    return jsonify(get_harness_status(user["id"]))
