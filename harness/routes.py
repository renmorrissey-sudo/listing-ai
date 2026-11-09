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

from flask import Blueprint, jsonify, render_template, request

import auth
import config
import db

from harness.approvals import (
    approve_approval,
    enqueue_execute_approval_job,
    list_pending_approvals,
    reject_approval,
)

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


# Human labels for the harness modes. The /harness page and the JSON API
# both derive their mode display from this mapping applied to the SAME
# authoritative mode value (``harness_settings.mode`` via
# ``_harness_settings`` / ``policy.get_settings``) -- never from a
# hardcoded string, and never from a second source. A stored mode outside
# the known set is normalized to drafts_only upstream, so "live" (or any
# other stray value) can never render as a "Live" label.
MODE_LABELS = {
    "drafts_only": "Drafts Only",
    "supervised": "Supervised",
    "authorized": "Authorized",
}


def _mode_label(mode):
    """Human label for a mode value; unknown values fail to Drafts Only."""
    return MODE_LABELS.get(mode, "Drafts Only")


def _sms_auto_reply_configured():
    """Platform-level SMS auto-reply readiness (env flag + Claude key).

    This is a platform fact, not a per-tenant setting: it says whether
    the auto-reply path *could* run at all, not whether this tenant's
    sends are allowed -- that is decided by mode/paused + the policy
    gate. Never raises.
    """
    try:
        from sms_ai_agent import auto_reply_configured

        return bool(auto_reply_configured())
    except Exception:
        return False


def _voice_configured():
    """Whether the Vapi voice provider has credentials configured.

    Platform-level only. There is no per-tenant voice on/off switch:
    voice tools are gated by the same harness policy gate as SMS, so in
    Drafts Only they can only draft, never execute. Never raises.
    """
    try:
        from voice_provider import get_voice_provider

        return bool(get_voice_provider().is_configured())
    except Exception:
        return False


def _outbound_activity(user_id):
    """Recorded outbound activity for the tenant, last 60 days.

    Counts come from stored records only -- never from assumptions or
    from zero successful deliveries. SMS sends are counted twice, from
    two independent sources that must agree in the healthy case: the
    harness audit rows the SMS handler writes (``sms_auto_reply_sent`` /
    ``send_failed`` / ``blocked`` / ``drafted``) and the ``sms_messages``
    rows actually marked sent. Voice calls are counted from the
    ``voice_calls`` rows actually created. Missing tables degrade to 0.
    """
    since_60d = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    return {
        "sms_sent_60d": _count(
            user_id,
            "SELECT COUNT(*) AS n FROM harness_audit WHERE user_id = ? AND action = 'sms_auto_reply_sent' AND created_at >= ?",
            (user_id, since_60d),
        ),
        "sms_send_failed_60d": _count(
            user_id,
            "SELECT COUNT(*) AS n FROM harness_audit WHERE user_id = ? AND action = 'sms_auto_reply_send_failed' AND created_at >= ?",
            (user_id, since_60d),
        ),
        "sms_blocked_60d": _count(
            user_id,
            "SELECT COUNT(*) AS n FROM harness_audit WHERE user_id = ? AND action = 'sms_auto_reply_blocked' AND created_at >= ?",
            (user_id, since_60d),
        ),
        "sms_drafted_60d": _count(
            user_id,
            "SELECT COUNT(*) AS n FROM harness_audit WHERE user_id = ? AND action = 'sms_auto_reply_drafted' AND created_at >= ?",
            (user_id, since_60d),
        ),
        "sms_messages_sent_60d": _count(
            user_id,
            "SELECT COUNT(*) AS n FROM sms_messages WHERE user_id = ? AND direction = 'outbound' AND status = 'sent' AND created_at >= ?",
            (user_id, since_60d),
        ),
        "calls_placed_60d": _count(
            user_id,
            "SELECT COUNT(*) AS n FROM voice_calls WHERE user_id = ? AND direction = 'outbound' AND created_at >= ?",
            (user_id, since_60d),
        ),
    }


def get_harness_status(user_id):
    """Read-only status dict for one tenant. Never raises for missing tables.

    This is the SINGLE authoritative status source: both the JSON API
    (``GET /api/harness/status``) and the ``/harness`` page render from
    this dict, so the two can never disagree. ``mode`` is the stored mode
    value, ``mode_label`` its human label from ``MODE_LABELS``.

    ``job_counts`` (M4) reports four overlapping lenses on the tenant's
    harness_jobs, not a partition: a job can count in more than one.
    ``pending`` = status pending (includes retry-queued jobs);
    ``failed`` = pending after at least one failed attempt
    (attempts > 0, awaiting retry); ``retried`` = claimed at least twice
    (attempts >= 2, i.e. actually retried after a failure);
    ``dead_letter`` = terminal failures that will never be retried.

    Channel state is explicit and separate from the mode: ``sms`` reports
    the platform-level auto-reply readiness plus whether sending is
    paused -- there is NO separate per-channel SMS pause flag; the
    harness-wide ``paused`` pauses ALL automated sending, including SMS.
    ``voice`` reports only whether Vapi is configured; voice tools are
    gated by the same policy gate as SMS. ``outbound_activity`` is
    recorded activity (see ``_outbound_activity``).
    """
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
    job_counts = {
        "pending": _count(
            user_id,
            "SELECT COUNT(*) AS n FROM harness_jobs WHERE user_id = ? AND status = 'pending'",
            (user_id,),
        ),
        "failed": _count(
            user_id,
            "SELECT COUNT(*) AS n FROM harness_jobs WHERE user_id = ? AND status = 'pending' AND attempts > 0",
            (user_id,),
        ),
        "retried": _count(
            user_id,
            "SELECT COUNT(*) AS n FROM harness_jobs WHERE user_id = ? AND attempts >= 2",
            (user_id,),
        ),
        "dead_letter": _count(
            user_id,
            "SELECT COUNT(*) AS n FROM harness_jobs WHERE user_id = ? AND status = 'dead_letter'",
            (user_id,),
        ),
    }
    since_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    jobs_24h = _count(
        user_id,
        "SELECT COUNT(*) AS n FROM harness_jobs WHERE user_id = ? AND created_at >= ?",
        (user_id, since_24h),
    )
    return {
        "mode": mode,
        "mode_label": _mode_label(mode),
        "paused": paused,
        "ai_enabled": ai_enabled,
        "pending_approvals": pending_approvals,
        "pending_jobs": pending_jobs,
        "job_counts": job_counts,
        "jobs_24h": jobs_24h,
        "sms": {
            "auto_reply_configured": _sms_auto_reply_configured(),
            "paused": paused,
        },
        "voice": {
            "configured": _voice_configured(),
        },
        "outbound_activity": _outbound_activity(user_id),
    }


@harness_bp.route("/api/harness/status", methods=["GET"])
@auth.subscription_required
def api_harness_status():
    user = auth.get_current_user()
    if not user:
        return jsonify({"error": "Please log in to continue."}), 401
    return jsonify(get_harness_status(user["id"]))


def _approval_error(error):
    """Map an approvals-module error to (JSON body, HTTP status).

    ``not_found`` covers both missing and other-tenant approvals: a
    tenant probing another tenant's approval gets a plain 404 and never
    learns it exists.
    """
    if error == "not_found":
        return jsonify({"error": "Approval not found."}), 404
    if error == "expired":
        return jsonify({"error": "Approval has expired."}), 410
    if error == "invalid_draft":
        return (
            jsonify(
                {"error": "Approval draft failed validation and cannot be approved."}
            ),
            422,
        )
    return jsonify({"error": "Approval is no longer pending."}), 409


@harness_bp.route("/api/harness/approvals", methods=["GET"])
@auth.subscription_required
def api_list_approvals():
    user = auth.get_current_user()
    if not user:
        return jsonify({"error": "Please log in to continue."}), 401
    return jsonify({"approvals": list_pending_approvals(user["id"])})


@harness_bp.route("/api/harness/approvals/<int:approval_id>/approve", methods=["POST"])
@auth.subscription_required
def api_approve_approval(approval_id):
    user = auth.get_current_user()
    if not user:
        return jsonify({"error": "Please log in to continue."}), 401
    approval, error = approve_approval(user["id"], approval_id)
    if error:
        return _approval_error(error)
    # Durable execution: one execute_approval job, idempotent per
    # approval, so the send happens even if the user closes the page.
    # A second approve can never enqueue a second job (approve returns
    # 409 for a non-pending approval, and the key dedupes regardless).
    job_id = enqueue_execute_approval_job(user["id"], approval_id)
    return jsonify({"approval": approval, "job_id": job_id})


@harness_bp.route("/api/harness/approvals/<int:approval_id>/reject", methods=["POST"])
@auth.subscription_required
def api_reject_approval(approval_id):
    user = auth.get_current_user()
    if not user:
        return jsonify({"error": "Please log in to continue."}), 401
    body = request.get_json(silent=True) or {}
    reason = body.get("reason") if isinstance(body, dict) else None
    approval, error = reject_approval(user["id"], approval_id, reason)
    if error:
        return _approval_error(error)
    # Rejection enqueues nothing: no job, no send, ever.
    return jsonify({"approval": approval})


@harness_bp.route("/harness", methods=["GET"])
@auth.login_required
def harness_status_page():
    """AI Automation status page (GET /harness).

    ``login_required`` (not the API's ``subscription_required``) so an
    anonymous visitor is redirected to /login instead of getting a JSON
    401. Read-only except for the approve/reject buttons, which call
    the API routes above. Drafts Only state is shown as-is from
    ``get_harness_status``.
    """
    user = auth.get_current_user()
    if not user:
        return jsonify({"error": "Please log in to continue."}), 401
    status = get_harness_status(user["id"])
    approvals = list_pending_approvals(user["id"])
    recent_audit = []
    try:
        from harness.audit import list_audit

        recent_audit = list_audit(user["id"], limit=10)
    except Exception:
        recent_audit = []
    spend_cents = 0
    try:
        from harness.costs import current_month_spend_cents

        spend_cents = current_month_spend_cents(user["id"])
    except Exception:
        spend_cents = 0
    return render_template(
        "harness_status.html",
        active_nav="ai-automation",
        product_name=config.PRODUCT_NAME,
        mode=status["mode"],
        mode_label=status["mode_label"],
        paused=status["paused"],
        ai_enabled=status["ai_enabled"],
        pending_approvals=status["pending_approvals"],
        pending_jobs=status["pending_jobs"],
        job_counts=status["job_counts"],
        jobs_24h=status["jobs_24h"],
        sms=status["sms"],
        voice=status["voice"],
        outbound_activity=status["outbound_activity"],
        approvals=approvals,
        recent_audit=recent_audit,
        spend_cents=spend_cents,
    )
