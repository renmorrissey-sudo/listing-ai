"""Harness HTTP API (Phase 1): one read-only status route.

GET /api/harness/status -- login + subscription required, same decorators
as other subscriber routes. Returns the current tenant's AI Automation
status as JSON. All counts are user_id-scoped. Missing harness tables (the
Phase 1 migration not yet applied) degrade to defaults/zeroes instead of
erroring.
"""

from __future__ import annotations

import hmac
import logging
import os
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, render_template, request

import auth
import config
import db

from harness.worker_health import check_worker_health, get_worker_heartbeat, maybe_alert_worker_health

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

    Mock vs real (defect 4): mocked sends are NEVER counted as real
    customer communications. ``sms_sent_60d`` / ``sms_messages_sent_60d``
    count only real provider sends (audit ``sms_auto_reply_sent`` /
    message status ``sent``). Mocked executions are tracked in the
    separate test metrics ``sms_mock_sent_60d`` (message status
    ``mock_sent``) and ``approvals_executed_60d`` (audit
    ``approval_executed``), so a mock send is visible in the execution
    metrics while real outbound customer communications stay zero.
    ``approvals_blocked_60d`` counts deterministic declines that
    settled as blocked (defect 1 item 8). ``sms_delivered_60d`` counts
    confirmed deliveries (message status ``delivered``, set by the
    provider delivery callback).
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
        "sms_delivered_60d": _count(
            user_id,
            "SELECT COUNT(*) AS n FROM sms_messages WHERE user_id = ? AND direction = 'outbound' AND status = 'delivered' AND created_at >= ?",
            (user_id, since_60d),
        ),
        # Mock / test metrics — never real customer communications.
        "sms_mock_sent_60d": _count(
            user_id,
            "SELECT COUNT(*) AS n FROM sms_messages WHERE user_id = ? AND direction = 'outbound' AND status = 'mock_sent' AND created_at >= ?",
            (user_id, since_60d),
        ),
        "approvals_executed_60d": _count(
            user_id,
            "SELECT COUNT(*) AS n FROM harness_audit WHERE user_id = ? AND action = 'approval_executed' AND created_at >= ?",
            (user_id, since_60d),
        ),
        "approvals_blocked_60d": _count(
            user_id,
            "SELECT COUNT(*) AS n FROM harness_audit WHERE user_id = ? AND action = 'approval_blocked' AND created_at >= ?",
            (user_id, since_60d),
        ),
        "calls_placed_60d": _count(
            user_id,
            "SELECT COUNT(*) AS n FROM voice_calls WHERE user_id = ? AND direction = 'outbound' AND created_at >= ?",
            (user_id, since_60d),
        ),
    }


def _harness_worker_heartbeat():
    """Liveness of the harness worker service. Never raises.

    Delegates to ``harness.worker_health.get_worker_heartbeat`` (same
    freshest ``harness-%`` row, same 120 s freshness window) but keeps
    this function's historical return shape — ``{"running",
    "last_seen_at", "worker_id"}`` — so existing callers and tests are
    unaffected. Staleness detail, stuck jobs, and the overall health
    verdict live in the ``worker_health`` key of ``get_harness_status``
    instead (see ``harness.worker_health.check_worker_health``).
    """
    heartbeat = get_worker_heartbeat()
    return {
        "running": bool(heartbeat["running"]),
        "last_seen_at": heartbeat["last_seen_at"],
        "worker_id": heartbeat["worker_id"],
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
    recorded activity (see ``_outbound_activity``). ``worker`` reports
    harness-worker liveness from its durable heartbeat row (see
    ``_harness_worker_heartbeat``): ``running`` is True only when a
    ``harness-<hostname>-<pid>`` heartbeat is fresh (<= 120 s old).
    ``worker_health`` (see ``harness.worker_health.check_worker_health``)
    adds the staleness verdict, stuck-job and overdue-job counts for
    this tenant, and the overall ``healthy`` flag. This function itself
    stays side-effect free — it reports health but never alerts; the
    web-side alert check runs in ``GET /api/harness/health`` and in
    the ``/harness`` page route.
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
        "worker": _harness_worker_heartbeat(),
        "worker_health": check_worker_health(user_id),
        "voice": {
            "configured": _voice_configured(),
        },
        "outbound_activity": _outbound_activity(user_id),
    }


def _health_token_valid():
    """True when the request carries the HARNESS_HEALTH_TOKEN.

    The token (env ``HARNESS_HEALTH_TOKEN``) lets a free external
    uptime monitor poll ``GET /api/harness/health`` without a login
    session, so a dead deployment is still detected when nobody is
    signed in. Constant-time comparison; when the env var is unset,
    token auth is unavailable and the endpoint requires login.
    Accepts ``?token=...`` or the ``X-Harness-Health-Token`` header.
    The token is a bearer credential for a read-only health read only
    — it grants no other access and must never be reused elsewhere.
    """
    expected = (os.environ.get("HARNESS_HEALTH_TOKEN") or "").strip()
    if not expected:
        return False
    supplied = (
        request.args.get("token")
        or request.headers.get("X-Harness-Health-Token")
        or ""
    )
    return hmac.compare_digest(str(supplied), expected)


@harness_bp.route("/api/harness/health", methods=["GET"])
def api_harness_health():
    """Worker-health check for operators and external watchers.

    Token-authenticated (``HARNESS_HEALTH_TOKEN``) callers get the
    platform-wide health (no tenant); otherwise login is required and
    the stuck/overdue counts are scoped to the signed-in tenant.
    Runs the web-side health alert check on every call — the web
    process is alive precisely when the worker may not be, and dedup
    (hour-scoped, durable) keeps repeat polls from re-sending. Never
    fails the endpoint on an alert error. Returns 503 with
    ``healthy: false`` only when the health check itself cannot run.
    """
    if _health_token_valid():
        user_id = None
    else:
        user = auth.get_current_user()
        if not user:
            return jsonify({"error": "Please log in to continue."}), 401
        user_id = user["id"]
    try:
        health = check_worker_health(user_id)
    except Exception:
        logger.exception("harness health check failed")
        return jsonify({"healthy": False, "error": "health check failed"}), 503
    try:
        health["alerts"] = maybe_alert_worker_health(health, user_id)
    except Exception:
        logger.exception("harness health alert check failed")
        health["alerts"] = {
            "worker_stale": False,
            "stuck_jobs": False,
            "worker_recovered": False,
        }
    return jsonify(health)


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


@harness_bp.route("/api/harness/settings/mode", methods=["POST"])
@auth.subscription_required
def api_set_harness_mode():
    """Set a tenant's harness mode. Admin-only, audited.

    Body: ``{"mode": "<drafts_only|supervised|authorized>", "user_id": <optional>}``.
    ``user_id`` defaults to the caller; an admin may set any tenant's
    mode, anyone else gets 403 even for their own account — mode is
    never self-service (a tenant who wants to stop automation uses
    the pause toggle instead). In staging the stored mode is still
    recorded, but ``get_settings`` reports it as drafts_only (the
    staging mode lock); the response carries both values so the UI
    cannot mistake a stored mode for the effective one.
    """
    from harness.policy import is_harness_admin, set_mode

    user = auth.get_current_user()
    if not user:
        return jsonify({"error": "Please log in to continue."}), 401
    if not is_harness_admin(user):
        return jsonify({"error": "Mode changes require a harness administrator."}), 403
    body = request.get_json(silent=True) or {}
    mode = body.get("mode")
    target_user_id = body.get("user_id") or user["id"]
    try:
        target_user_id = int(target_user_id)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid user."}), 400
    settings, error = set_mode(
        target_user_id, mode, actor="admin", actor_user_id=user["id"]
    )
    if error == "invalid_mode":
        return jsonify({"error": "Invalid mode."}), 400
    if error == "invalid_user":
        return jsonify({"error": "User not found."}), 404
    if error or settings is None:
        return jsonify({"error": "Could not change mode."}), 500
    return jsonify(
        {
            "mode": settings.get("mode"),
            "stored_mode": str(mode or "").strip().lower(),
            "paused": bool(settings.get("paused")),
            "ai_enabled": bool(settings.get("ai_enabled", True)),
            "user_id": target_user_id,
        }
    )


@harness_bp.route("/api/harness/settings/pause", methods=["POST"])
@auth.subscription_required
def api_set_harness_paused():
    """Pause / resume harness automation for a tenant. Audited.

    Body: ``{"paused": true|false, "user_id": <optional>}``.
    ``user_id`` defaults to the caller. A tenant may pause only their
    own account; an admin may pause any tenant. ``paused`` must be a
    JSON boolean — anything else is a 400.
    """
    from harness.policy import is_harness_admin, set_paused

    user = auth.get_current_user()
    if not user:
        return jsonify({"error": "Please log in to continue."}), 401
    body = request.get_json(silent=True) or {}
    paused = body.get("paused")
    if not isinstance(paused, bool):
        return jsonify({"error": "paused must be true or false."}), 400
    target_user_id = body.get("user_id") or user["id"]
    try:
        target_user_id = int(target_user_id)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid user."}), 400
    if target_user_id != user["id"] and not is_harness_admin(user):
        return (
            jsonify({"error": "You can only pause your own account."}),
            403,
        )
    actor = "admin" if is_harness_admin(user) else "user"
    settings, error = set_paused(
        target_user_id, paused, actor=actor, actor_user_id=user["id"]
    )
    if error == "invalid_paused":
        return jsonify({"error": "paused must be true or false."}), 400
    if error == "invalid_user":
        return jsonify({"error": "User not found."}), 404
    if error or settings is None:
        return jsonify({"error": "Could not change pause state."}), 500
    return jsonify(
        {
            "mode": settings.get("mode"),
            "paused": bool(settings.get("paused")),
            "ai_enabled": bool(settings.get("ai_enabled", True)),
            "user_id": target_user_id,
        }
    )


# --- SMS suppression list + governed removal (Phase II Batch 1, item 5) ------


@harness_bp.route("/api/sms/suppression", methods=["GET"])
@auth.subscription_required
def api_list_suppressions():
    """List the current tenant's suppressed numbers."""
    import tenant_sms_db as tdb

    user = auth.get_current_user()
    if not user:
        return jsonify({"error": "Please log in to continue."}), 401
    return jsonify({"suppressions": tdb.list_suppressions(user["id"])})


@harness_bp.route("/api/sms/suppression/removals", methods=["GET"])
@auth.subscription_required
def api_list_suppression_removals():
    """List suppression-removal requests.

    A tenant sees their own requests. A harness admin with
    ``?scope=all`` sees every tenant's (pending by default);
    ``?status=pending|approved|rejected`` filters.
    """
    import tenant_sms_db as tdb

    from harness.policy import is_harness_admin

    user = auth.get_current_user()
    if not user:
        return jsonify({"error": "Please log in to continue."}), 401
    status = (request.args.get("status") or "").strip().lower() or None
    if status and status not in ("pending", "approved", "rejected"):
        return jsonify({"error": "Invalid status."}), 400
    scope_all = (request.args.get("scope") or "").strip().lower() == "all"
    if scope_all and not is_harness_admin(user):
        return jsonify({"error": "Listing all requests requires a harness administrator."}), 403
    user_id = None if scope_all else user["id"]
    return jsonify(
        {"removals": tdb.list_suppression_removals(user_id, status=status)}
    )


@harness_bp.route("/api/sms/suppression/removals", methods=["POST"])
@auth.subscription_required
def api_request_suppression_removal():
    """Request removal of one of the tenant's suppressed numbers.

    Body: ``{"phone_number": "<E.164>", "reason": "<why it should come off>"}``.
    Creates a pending request (migration 036) — nothing is removed
    here. Removal happens only when a harness admin approves it via
    the review route, and the reviewer must differ from the requester.
    """
    import tenant_sms_db as tdb

    from sms_validation import validate_e164_phone

    user = auth.get_current_user()
    if not user:
        return jsonify({"error": "Please log in to continue."}), 401
    body = request.get_json(silent=True) or {}
    phone, phone_error = validate_e164_phone(body.get("phone_number") or "")
    if phone_error:
        return jsonify({"error": phone_error}), 400
    req, error = tdb.request_suppression_removal(
        user["id"], phone, requested_by=user["id"], reason=body.get("reason") or ""
    )
    if error == "reason_required":
        return jsonify({"error": "A reason is required to request removal."}), 400
    if error == "not_suppressed":
        return jsonify({"error": "That number is not on your suppression list."}), 400
    if error == "already_pending":
        return jsonify({"error": "A removal request for that number is already pending.", "removal": req}), 409
    if error or req is None:
        return jsonify({"error": "Could not create the removal request."}), 500
    return jsonify({"removal": req}), 201


@harness_bp.route("/api/sms/suppression/removals/<int:request_id>/review", methods=["POST"])
@auth.subscription_required
def api_review_suppression_removal(request_id):
    """Approve or reject a pending suppression-removal request. Admin-only.

    Body: ``{"approve": true|false, "note": "<optional>"}``. The
    reviewer must differ from the requester (two-person rule) — an
    admin cannot approve their own request (409 ``self_review``).
    On approval the suppression row is deleted and the decision is
    written to the SMS audit trail; on rejection the number stays
    suppressed.
    """
    import tenant_sms_db as tdb

    from harness.policy import is_harness_admin

    user = auth.get_current_user()
    if not user:
        return jsonify({"error": "Please log in to continue."}), 401
    if not is_harness_admin(user):
        return jsonify({"error": "Reviewing removal requests requires a harness administrator."}), 403
    body = request.get_json(silent=True) or {}
    approve = body.get("approve")
    if not isinstance(approve, bool):
        return jsonify({"error": "approve must be true or false."}), 400
    req, error = tdb.review_suppression_removal(
        request_id,
        reviewer_user_id=user["id"],
        approve=approve,
        note=body.get("note") or "",
    )
    if error == "not_found":
        return jsonify({"error": "Removal request not found."}), 404
    if error == "already_reviewed":
        return jsonify({"error": "That request has already been reviewed."}), 409
    if error == "self_review":
        return (
            jsonify({"error": "You cannot review your own removal request — a second administrator must review it."}),
            409,
        )
    if error or req is None:
        return jsonify({"error": "Could not review the removal request."}), 500
    return jsonify({"removal": req})


def _enrich_appointment_requests(user_id, requests):
    """Add the display fields the inbox confirm card needs: the slot
    label and the exact confirmation text the agent approves (verbatim
    — the same text the confirm action sends)."""
    from harness.appointment_requests import (
        build_booking_confirmation_text,
        format_slot_label,
    )

    try:
        timezone_name = db.get_user_timezone(user_id)
    except Exception:
        timezone_name = None
    enriched = []
    for req in requests or []:
        item = dict(req)
        item["slot_label"] = format_slot_label(
            req.get("requested_start_at"), timezone_name
        )
        item["confirmation_text"] = (
            build_booking_confirmation_text(req.get("requested_start_at"), timezone_name)
            if req.get("requested_start_at")
            else ""
        )
        enriched.append(item)
    return enriched


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


@harness_bp.route(
    "/api/harness/appointment-requests/<int:request_id>/reconfirm", methods=["POST"]
)
@auth.subscription_required
def api_reconfirm_appointment_request(request_id):
    """Create a fresh ``appointment_request`` approval for a request,
    optionally with a new slot (the agent picks the exact slot at
    confirm time for window / missing-time requests, or after a
    collision refusal). A slot override records the new slot on the
    same request row. 404 when the request does not belong to the
    tenant — a probe never learns it exists."""
    from harness.appointment_requests import create_confirm_approval

    user = auth.get_current_user()
    if not user:
        return jsonify({"error": "Please log in to continue."}), 401
    body = request.get_json(silent=True) or {}
    slot_start_at = body.get("slot_start_at") if isinstance(body, dict) else None
    slot_end_at = body.get("slot_end_at") if isinstance(body, dict) else None
    approval_id, error = create_confirm_approval(
        user["id"], request_id, slot_start_at=slot_start_at, slot_end_at=slot_end_at
    )
    if error:
        if error == "not_found":
            return jsonify({"error": "Appointment request not found."}), 404
        if error == "confirm_in_progress":
            return jsonify({"error": "A confirm is already in progress."}), 409
        return jsonify({"error": error}), 400
    return jsonify({"approval_id": approval_id})


def _is_harness_admin(user):
    """Whether the signed-in user may change harness modes."""
    try:
        from harness.policy import is_harness_admin

        return bool(is_harness_admin(user))
    except Exception:
        return False


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
    # Web-side health alert check: the web process is alive precisely
    # when the worker may not be. Deduped (hour-scoped, durable), so a
    # page reload never re-sends; failures here must never break the page.
    try:
        maybe_alert_worker_health(status.get("worker_health") or {}, user["id"])
    except Exception:
        logger.exception("harness health alert check failed")
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
        is_harness_admin=_is_harness_admin(user),
        pending_approvals=status["pending_approvals"],
        pending_jobs=status["pending_jobs"],
        job_counts=status["job_counts"],
        jobs_24h=status["jobs_24h"],
        sms=status["sms"],
        voice=status["voice"],
        outbound_activity=status["outbound_activity"],
        worker=status["worker"],
        worker_health=status["worker_health"],
        approvals=approvals,
        recent_audit=recent_audit,
        spend_cents=spend_cents,
    )


# --- Unified inbox (Phase II Priority 2, M2) --------------------------------
#
# GET /harness/inbox renders the subscriber inbox: SMS conversations per
# lead (most recent first), the pending approval queue, unread state,
# search/filter, and a per-conversation human-takeover flag. The page
# itself is login_required (like /harness); the JSON APIs below are
# subscription_required (like the other harness APIs). Every query is
# user_id-scoped — a tenant touching another tenant's conversation sees
# it as not found and never learns it exists.
#
# Approval text on this page comes from the single stored draft:
# list_pending_approvals -> serialize_approval reads draft_json's
# draft_reply verbatim. The template renders that value and nothing
# else — no re-derived or cached copy.


@harness_bp.route("/harness/inbox", methods=["GET"])
@auth.login_required
def harness_inbox_page():
    """Subscriber inbox page (GET /harness/inbox)."""
    from harness import inbox as inbox_mod

    user = auth.get_current_user()
    if not user:
        return jsonify({"error": "Please log in to continue."}), 401
    user_id = user["id"]
    query = (request.args.get("q") or "").strip() or None
    unread_only = request.args.get("unread") in {"1", "true", "yes"}
    lead_id_raw = request.args.get("lead_id")
    selected = None
    selected_lead_id = None
    if lead_id_raw:
        try:
            selected_lead_id = int(lead_id_raw)
        except (TypeError, ValueError):
            selected_lead_id = None
    conversations = inbox_mod.list_sms_conversations(
        user_id, limit=50, query=query, unread_only=unread_only
    )
    if selected_lead_id:
        selected = inbox_mod.get_conversation(user_id, selected_lead_id)
        if selected is not None:
            # Opening a thread marks it read, clearing its unread badge.
            inbox_mod.mark_conversation_read(user_id, selected_lead_id)
            selected = inbox_mod.get_conversation(user_id, selected_lead_id)
            # Pending appointment requests ride on the conversation so
            # the confirm card renders with the exact text to approve.
            from harness.appointment_requests import list_pending_requests

            selected["pending_appointment_requests"] = _enrich_appointment_requests(
                user_id, list_pending_requests(user_id, selected_lead_id)
            )
    approvals = list_pending_approvals(user_id)
    return render_template(
        "harness_inbox.html",
        active_nav="ai-automation",
        product_name=config.PRODUCT_NAME,
        conversations=conversations,
        selected=selected,
        selected_lead_id=selected_lead_id,
        approvals=approvals,
        query=query or "",
        unread_only=unread_only,
    )


@harness_bp.route("/api/harness/inbox/conversations", methods=["GET"])
@auth.subscription_required
def api_list_inbox_conversations():
    """List the signed-in tenant's SMS conversations (JSON)."""
    from harness import inbox as inbox_mod

    user = auth.get_current_user()
    if not user:
        return jsonify({"error": "Please log in to continue."}), 401
    query = (request.args.get("q") or "").strip() or None
    unread_only = request.args.get("unread") in {"1", "true", "yes"}
    return jsonify(
        {
            "conversations": inbox_mod.list_sms_conversations(
                user["id"], limit=50, query=query, unread_only=unread_only
            )
        }
    )


@harness_bp.route(
    "/api/harness/inbox/conversations/<int:lead_id>", methods=["GET"]
)
@auth.subscription_required
def api_get_inbox_conversation(lead_id):
    """One conversation thread (JSON); 404 when not owned by the tenant."""
    from harness import inbox as inbox_mod

    user = auth.get_current_user()
    if not user:
        return jsonify({"error": "Please log in to continue."}), 401
    conversation = inbox_mod.get_conversation(user["id"], lead_id)
    if conversation is None:
        return jsonify({"error": "Conversation not found."}), 404
    from harness.appointment_requests import list_pending_requests

    conversation["pending_appointment_requests"] = _enrich_appointment_requests(
        user["id"], list_pending_requests(user["id"], lead_id)
    )
    return jsonify({"conversation": conversation})


@harness_bp.route(
    "/api/harness/inbox/conversations/<int:lead_id>/takeover",
    methods=["POST"],
)
@auth.subscription_required
def api_set_inbox_takeover(lead_id):
    """Set or clear the human-takeover flag for one conversation.

    Body: {"active": true|false}. The change is audited
    (harness_takeover_set / harness_takeover_cleared). 404 when the
    lead is not owned by the signed-in tenant.
    """
    from harness import inbox as inbox_mod

    user = auth.get_current_user()
    if not user:
        return jsonify({"error": "Please log in to continue."}), 401
    body = request.get_json(silent=True) or {}
    active = body.get("active")
    if not isinstance(active, bool):
        return jsonify({"error": "Body must include active (true/false)."}), 400
    state = inbox_mod.set_human_takeover(user["id"], lead_id, active)
    if state is None:
        return jsonify({"error": "Conversation not found."}), 404
    return jsonify({"conversation": state})


@harness_bp.route(
    "/api/harness/inbox/conversations/<int:lead_id>/read", methods=["POST"]
)
@auth.subscription_required
def api_mark_inbox_conversation_read(lead_id):
    """Mark one conversation read (sets last_read_at to now)."""
    from harness import inbox as inbox_mod

    user = auth.get_current_user()
    if not user:
        return jsonify({"error": "Please log in to continue."}), 401
    state = inbox_mod.mark_conversation_read(user["id"], lead_id)
    if state is None:
        return jsonify({"error": "Conversation not found."}), 404
    return jsonify({"conversation": state})
