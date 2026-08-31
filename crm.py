"""Phase 2 CRM pages and JSON APIs. All routes require subscription + ownership."""

import json
import logging
from datetime import datetime, timedelta, timezone

from flask import Blueprint, flash, get_flashed_messages, jsonify, redirect, render_template, request, url_for

import auth
import crm_db
import db
import lead_contact_service
from crm_constants import (
    APPOINTMENT_OUTCOMES,
    APPOINTMENT_TYPES,
    CALENDAR_EVENT_TYPES,
    COMMON_TIMEZONES,
    FOLLOW_UP_CANCEL_REASONS,
    LEAD_STATUS_SET,
    LEAD_STATUSES,
    LEGACY_STATUS_MAP,
    NEEDS_ATTENTION_REASONS,
    PIPELINE_STAGES,
    PRIORITIES,
    TASK_TYPES,
    cancel_reason_label,
    outcome_label,
    normalize_lead_status,
    sms_consent_is_certified,
    sms_consent_label,
    status_label,
)

PIPELINE_STAGE_IDS = {stage_id for stage_id, _label, _members in PIPELINE_STAGES}
ALLOWED_SMS_CONSENT = {"unverified", "verified", "user_certified", "opted_out", "not_permitted"}
ALLOWED_POND = {"claimable", "claimed", "assigned", "unassigned"}
ALLOWED_FOLLOW_UP_RANGES = {
    "today": "today",
    "overdue": "overdue",
    "this_week": "this_week",
    "this-week": "this_week",
    "week": "this_week",
}

crm_bp = Blueprint("crm", __name__)
logger = logging.getLogger(__name__)


def _user_or_redirect():
    user = auth.get_current_user()
    if not user or not auth.user_has_active_subscription(user):
        return None
    return user


def _truthy_arg(value):
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _parse_blocked_arg(*values):
    for value in values:
        raw = str(value or "").strip().lower()
        if raw in {"1", "true", "yes"}:
            return True, "1"
        if raw in {"0", "false", "no"}:
            return False, "0"
    return None, ""


def _parse_dashboard_range_arg(*values):
    """Normalize range/due query values used by dashboard drill-downs."""
    for value in values:
        key = str(value or "").strip().lower()
        if key in ALLOWED_FOLLOW_UP_RANGES:
            return ALLOWED_FOLLOW_UP_RANGES[key]
    return None


def _parse_leads_list_filters(args):
    """Parse /crm/leads query params, including dashboard drill-down aliases.

    Unknown filter values are ignored. Tenant scoping is always enforced by
    filter_leads(user_id=...).
    """
    status_raw = (args.get("status") or "").strip().lower() or None
    stage = (args.get("stage") or "").strip().lower() or None
    if stage and stage not in PIPELINE_STAGE_IDS:
        stage = None

    status = None
    if not stage and status_raw:
        if status_raw in PIPELINE_STAGE_IDS:
            # Dashboard stage cards use status=<stage_id> (e.g. contacting).
            stage = status_raw
        elif status_raw in LEAD_STATUS_SET or status_raw in LEGACY_STATUS_MAP:
            status = status_raw
        # else: ignore unknown status values

    scope = (args.get("scope") or "").strip().lower() or None
    if scope and scope != "active":
        scope = None
    if _truthy_arg(args.get("active")):
        scope = "active"

    source = (args.get("source") or "").strip() or None

    consent_raw = (args.get("consent") or args.get("sms_consent") or "").strip().lower() or None
    review = (args.get("consent_review") or "").strip() or None
    sms_consent = None
    if consent_raw == "review":
        review = "1"
    elif consent_raw in ALLOWED_SMS_CONSENT:
        sms_consent = consent_raw
    if review and not _truthy_arg(review):
        review = None
    elif review:
        review = "1"

    pond = (args.get("pond") or "").strip().lower() or None
    if pond and pond not in ALLOWED_POND:
        pond = None

    external = (args.get("external") or "").strip() or None
    origin = (args.get("origin") or "").strip().lower() or None
    if origin == "external" or _truthy_arg(external):
        external = "1"
    elif external:
        external = None

    sms_blocked, blocked = _parse_blocked_arg(args.get("sms_blocked"), args.get("blocked"))

    batch = (args.get("batch") or "").strip() or None
    import_batch_id = int(batch) if batch and batch.isdigit() else None

    search = (args.get("q") or args.get("search") or "").strip()[:200] or None

    return {
        "status": status,
        "source": source,
        "scope": scope,
        "stage": stage,
        "sms_consent": sms_consent,
        "pond": pond,
        "external": external,
        "blocked": blocked,
        "sms_blocked": sms_blocked,
        "review": review,
        "batch": batch if import_batch_id is not None else "",
        "import_batch_id": import_batch_id,
        "search": search,
    }


def _format_call_duration(seconds):
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return None
    if total < 0:
        return None
    minutes, secs = divmod(total, 60)
    if minutes and secs:
        return f"{minutes} min {secs} sec"
    if minutes:
        return f"{minutes} min"
    return f"{secs} sec"


def _format_activity_when(value):
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return str(value)[:16].replace("T", " ") + " UTC"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone()
    # Example: July 26, 2026 at 5:32 PM (portable across Windows/Unix)
    hour = local.strftime("%I").lstrip("0") or "0"
    return f"{local.strftime('%B')} {local.day}, {local.year} at {hour}:{local.strftime('%M %p')}"


def _email_signature_text(user_id):
    profile = db.get_business_profile(user_id) or {}
    lines = ["Warm regards,"]
    for value in (
        profile.get("agent_name"),
        profile.get("phone_number"),
        profile.get("brokerage_name") or profile.get("company_name"),
    ):
        text = str(value or "").strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def _enrich_lead_activities(user_id, activities):
    """Attach voice recording controls for timeline rendering (auth proxy paths only)."""
    enriched = []
    for activity in activities:
        item = dict(activity)
        try:
            payload = json.loads(item.get("payload_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        item["payload"] = payload

        voice_call_id = payload.get("voice_call_id")
        if voice_call_id and item.get("event_type") in (
            "voice_call_completed",
            "voice_call_updated",
            "voice_call_started",
            "voice_call_failed",
            "voice_call_connected",
            "voice_call_unanswered",
            "voice_call_cancelled",
        ):
            call = db.get_voice_call(voice_call_id, user_id)
            if call:
                has_recording = db.voice_call_has_recording(call)
                recording_status = call.get("recording_status")
                if has_recording:
                    recording_status = "available"
                elif not recording_status and call.get("status") == "completed":
                    recording_status = "unavailable"
                duration = call.get("recording_duration_seconds") or payload.get(
                    "recording_duration_seconds"
                ) or payload.get("duration")
                item["voice"] = {
                    "call_id": call["id"],
                    "has_recording": has_recording,
                    "recording_status": recording_status,
                    "recording_url": (
                        f"/api/voice-calls/{call['id']}/recording" if has_recording else None
                    ),
                    "has_transcript": bool(call.get("transcript")),
                    "transcript_url": (
                        f"/api/voice-calls/{call['id']}/transcript"
                        if call.get("transcript")
                        else None
                    ),
                    "duration_label": _format_call_duration(duration),
                    "when_label": _format_activity_when(
                        call.get("completed_at") or item.get("created_at")
                    ),
                    "summary": call.get("summary") or payload.get("summary"),
                }
            else:
                item["voice"] = {
                    "call_id": voice_call_id,
                    "has_recording": bool(payload.get("has_recording")),
                    "recording_status": payload.get("recording_status") or "unavailable",
                    "recording_url": None,
                    "has_transcript": bool(payload.get("has_transcript")),
                    "transcript_url": None,
                    "duration_label": _format_call_duration(
                        payload.get("recording_duration_seconds") or payload.get("duration")
                    ),
                    "when_label": _format_activity_when(
                        payload.get("completed_at") or item.get("created_at")
                    ),
                    "summary": payload.get("summary"),
                }
        enriched.append(item)
    return enriched


def _parse_insight_suggestions(insight):
    raw = {}
    try:
        raw = json.loads(insight.get("raw_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        "suggested_lead_status": raw.get("suggested_lead_status") or "",
        "suggested_follow_up_at": raw.get("suggested_follow_up_at"),
        "suggested_follow_up_reason": raw.get("suggested_follow_up_reason")
        or insight.get("recommended_action")
        or insight.get("next_best_step")
        or "Follow up",
        "suggested_tasks": raw.get("suggested_tasks") or [],
        "appointment_requested": bool(raw.get("appointment_requested")),
        "appointment_details": raw.get("appointment_details"),
        "draft_reply": raw.get("draft_reply") or insight.get("suggested_reply") or "",
        "recommended_next_action": raw.get("recommended_next_action")
        or insight.get("recommended_action")
        or insight.get("next_best_step")
        or "",
        "confidence": raw.get("confidence", insight.get("confidence_score")),
        "sensitive_topic": bool(raw.get("sensitive_topic")),
    }


def _nav_context(user, active):
    return {
        "email": user["email"],
        "has_billing_portal": bool(user.get("stripe_customer_id")),
        "needs_billing_attention": auth.user_needs_billing_attention(user),
        "active_nav": active,
        "product_name": "TopAI Real Estate Tools",
    }


def _lead_detail_template_kwargs(user, lead_id, *, outcome_draft=None, form_error=None):
    lead = db.get_lead(lead_id, user["id"])
    if not lead:
        return None
    activities = _enrich_lead_activities(
        user["id"],
        crm_db.list_lead_activities(user["id"], lead_id, for_timeline=True),
    )
    tasks = [t for t in crm_db.list_tasks(user["id"], bucket="all") if t.get("lead_id") == lead_id]
    appointments = crm_db.list_appointments(user["id"], lead_id=lead_id)
    needs = [n for n in crm_db.list_needs_attention(user["id"]) if n.get("lead_id") == lead_id]
    messages = db.list_lead_messages(user["id"], lead_id, visible_only=True)
    follow_ups = crm_db.list_lead_follow_ups(user["id"], lead_id, include_completed=True)
    user_timezone = db.get_user_timezone(user["id"])
    windows = crm_db._follow_up_windows(user["id"], timezone_name=user_timezone)
    follow_up_groups = crm_db.group_follow_ups_for_lead(
        follow_ups,
        timezone_name=user_timezone,
        windows=windows,
        user_id=user["id"],
    )
    next_follow_up = None
    for key in ("overdue", "today", "upcoming"):
        if follow_up_groups.get(key):
            next_follow_up = follow_up_groups[key][0]
            break
    open_tasks = [
        t for t in tasks if t.get("status") in {"open", "in_progress"}
    ]
    open_tasks.sort(key=lambda t: t.get("due_at") or "9999")
    next_task = open_tasks[0] if open_tasks else None
    open_appts = [
        a
        for a in appointments
        if a.get("status") in {"scheduled", "confirmed", "proposed", "rescheduled"}
    ]
    open_appts.sort(key=lambda a: a.get("start_at") or "9999")
    next_appointment = open_appts[0] if open_appts else None
    flash_message = request.args.get("notice") or ""
    flash_error = form_error or request.args.get("error") or ""
    for category, message in get_flashed_messages(with_categories=True):
        if category == "error":
            flash_error = flash_error or message
        else:
            flash_message = flash_message or message
    import external_leads_db as xdb

    evidence = xdb.list_consent_evidence(user["id"], lead_id, limit=10)
    audit = xdb.list_consent_audit(user["id"], lead_id, limit=20)
    external_source = None
    if lead.get("external_source_id"):
        external_source = xdb.get_external_lead_source(lead["external_source_id"], user["id"])
    return {
        "lead": lead,
        "activities": activities,
        "tasks": tasks,
        "appointments": appointments,
        "needs": needs,
        "messages": messages,
        "follow_ups": follow_ups,
        "follow_up_groups": follow_up_groups,
        "next_follow_up": next_follow_up,
        "next_task": next_task,
        "next_appointment": next_appointment,
        "statuses": LEAD_STATUSES,
        "task_types": TASK_TYPES,
        "appointment_types": APPOINTMENT_TYPES,
        "appointment_outcomes": APPOINTMENT_OUTCOMES,
        "priorities": PRIORITIES,
        "follow_up_cancel_reasons": FOLLOW_UP_CANCEL_REASONS,
        "cancel_reason_label": cancel_reason_label,
        "user_timezone": db.get_user_timezone(user["id"]),
        "status_label": status_label,
        "sms_consent_label": sms_consent_label,
        "sms_consent_is_certified": sms_consent_is_certified,
        "outcome_label": outcome_label,
        "flash_message": flash_message,
        "flash_error": flash_error,
        "outcome_draft": outcome_draft or {},
        "consent_evidence": evidence,
        "consent_audit": audit,
        "external_source": external_source,
        "historical_sms_name": db.earliest_sms_lead_name(user["id"], lead_id),
        "email_signature": _email_signature_text(user["id"]),
        **_nav_context(user, "leads"),
    }


@crm_bp.route("/crm/dashboard")
def crm_dashboard_alias():
    """Production alias for /dashboard (https://…/crm/dashboard)."""
    qs = request.query_string.decode("utf-8") if request.query_string else ""
    target = "/dashboard"
    if qs:
        target = f"{target}?{qs}"
    return redirect(target)


@crm_bp.route("/crm/leads")
def crm_leads_page():
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("subscriber_app"))
    filters = _parse_leads_list_filters(request.args)
    status = filters["status"]
    source = filters["source"]
    scope = filters["scope"]
    stage = filters["stage"]
    sms_consent = filters["sms_consent"]
    pond = filters["pond"]
    external = filters["external"]
    blocked = filters["blocked"]
    sms_blocked = filters["sms_blocked"]
    review = filters["review"]
    batch = filters["batch"]
    search = filters["search"]
    leads = crm_db.filter_leads(
        user["id"],
        status=status,
        source=source,
        scope=scope,
        stage=stage,
        sms_consent_status=sms_consent,
        sms_sending_blocked=sms_blocked,
        pond_status=pond,
        external_only=external,
        import_batch_id=filters["import_batch_id"],
        consent_review_required=review,
        search=search,
    )
    active_filter = None
    if scope == "active":
        active_filter = "Active leads"
    elif stage:
        for stage_id, label, _members in PIPELINE_STAGES:
            if stage_id == stage:
                active_filter = f"Pipeline stage: {label}"
                break
        active_filter = active_filter or f"Pipeline stage: {stage}"
    elif status:
        active_filter = f"Status: {status_label(status)}"
    if source:
        active_filter = (active_filter + f" · Source: {source}") if active_filter else f"Source: {source}"
    if sms_consent:
        active_filter = (
            (active_filter + f" · Consent: {sms_consent}") if active_filter else f"Consent: {sms_consent}"
        )
    if external:
        active_filter = (active_filter + " · External") if active_filter else "External leads"
    if blocked == "1":
        active_filter = (active_filter + " · SMS blocked") if active_filter else "SMS blocked"
    elif blocked == "0":
        active_filter = (active_filter + " · SMS enabled") if active_filter else "SMS enabled"
    if review:
        active_filter = (
            (active_filter + " · Consent review required")
            if active_filter
            else "Consent review required"
        )
    if pond:
        active_filter = (active_filter + f" · Pond: {pond}") if active_filter else f"Pond: {pond}"
    if search:
        active_filter = (
            (active_filter + f' · Search: "{search}"') if active_filter else f'Search: "{search}"'
        )
    import external_leads_db as xdb

    lead_sources = xdb.list_external_lead_sources(user["id"], active_only=True)
    return render_template(
        "crm_leads.html",
        leads=leads,
        lead_sources=lead_sources,
        statuses=LEAD_STATUSES,
        pipeline_stages=PIPELINE_STAGES,
        status_filter=status or "",
        source_filter=source or "",
        scope_filter=scope or "",
        stage_filter=stage or "",
        sms_consent_filter=sms_consent or "",
        pond_filter=pond or "",
        external_filter=external or "",
        blocked_filter=blocked or "",
        consent_review_filter=review or "",
        batch_filter=batch or "",
        search_filter=search or "",
        active_filter=active_filter,
        result_count=len(leads),
        status_label=status_label,
        sms_consent_label=sms_consent_label,
        has_active_filters=bool(active_filter),
        email_signature=_email_signature_text(user["id"]),
        **_nav_context(user, "leads"),
    )


@crm_bp.route("/crm/leads/<int:lead_id>")
def crm_lead_detail_page(lead_id):
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("subscriber_app"))
    ctx = _lead_detail_template_kwargs(user, lead_id)
    if not ctx:
        return redirect(url_for("crm.crm_leads_page"))
    return render_template("crm_lead_detail.html", **ctx)


@crm_bp.route("/crm/tasks")
def crm_tasks_page():
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("subscriber_app"))
    local_date = (request.args.get("local_date") or "").strip()[:10] or None
    range_key = _parse_dashboard_range_arg(
        request.args.get("due"), request.args.get("range")
    )
    status = (request.args.get("status") or "").strip().lower() or None
    if status and status not in {"open", "in_progress", "completed", "cancelled"}:
        status = None
    overdue = crm_db.list_tasks(user["id"], "overdue", local_date=local_date)
    today = crm_db.list_tasks(user["id"], "today", local_date=local_date)
    upcoming = crm_db.list_tasks(user["id"], "upcoming", local_date=local_date)
    if status == "open":
        open_set = {"open", "in_progress"}
        overdue = [t for t in overdue if t.get("status") in open_set]
        today = [t for t in today if t.get("status") in open_set]
        upcoming = [t for t in upcoming if t.get("status") in open_set]
    if range_key == "today":
        overdue, upcoming = [], []
    elif range_key == "overdue":
        today, upcoming = [], []
    elif range_key == "upcoming":
        overdue, today = [], []
    active_filter = None
    if range_key == "today":
        active_filter = "Tasks due today"
    elif range_key == "overdue":
        active_filter = "Overdue tasks"
    elif range_key:
        active_filter = f"Range: {range_key}"
    return render_template(
        "crm_tasks.html",
        overdue=overdue,
        today=today,
        upcoming=upcoming,
        task_types=TASK_TYPES,
        priorities=PRIORITIES,
        local_date=local_date or "",
        range_filter=range_key or "",
        status_filter=status or "",
        active_filter=active_filter,
        result_count=len(overdue) + len(today) + len(upcoming),
        **_nav_context(user, "tasks"),
    )


@crm_bp.route("/crm/needs-attention")
def crm_needs_attention_page():
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("subscriber_app"))
    local_date = (request.args.get("local_date") or "").strip()[:10] or None
    status = (request.args.get("status") or "open").strip().lower() or "open"
    item_type = (request.args.get("type") or "").strip().lower() or None
    drafts = []
    items = []
    if item_type in {"draft_reply", "draft", "drafts"}:
        drafts = crm_db.list_pending_draft_insights(user["id"])
        active_filter = "Escalated SMS replies"
        result_count = len(drafts)
    else:
        items = crm_db.list_needs_attention(user["id"], local_date=local_date)
        if status and status != "open":
            items = [i for i in items if i.get("status") == status]
        active_filter = "Open Needs Attention" if status == "open" else f"Status: {status}"
        result_count = len(items)
    return render_template(
        "crm_needs_attention.html",
        items=items,
        drafts=drafts,
        reason_labels=NEEDS_ATTENTION_REASONS,
        local_date=local_date or "",
        status_filter=status,
        type_filter=item_type or "",
        active_filter=active_filter,
        result_count=result_count,
        **_nav_context(user, "needs"),
    )


@crm_bp.route("/api/crm/leads")
@auth.subscription_required
def api_list_leads():
    user = auth.get_current_user()
    status = (request.args.get("status") or "").strip() or None
    source = (request.args.get("source") or "").strip() or None
    leads = crm_db.filter_leads(user["id"], status=status, source=source)
    return jsonify({
        "leads": [
            {
                "id": lead["id"],
                "name": lead.get("name"),
                "phone_number": lead.get("phone_number"),
                "email": lead.get("email"),
                "lead_type": lead.get("lead_type"),
                "property_interest": lead.get("property_interest"),
                "status": normalize_lead_status(lead.get("status")),
                "status_label": status_label(lead.get("status")),
                "priority": lead.get("priority") or "normal",
                "next_action": lead.get("next_action"),
                "next_follow_up_at": lead.get("next_follow_up_at") or lead.get("follow_up_at"),
                "follow_up_reason": lead.get("follow_up_reason"),
                "source": lead.get("source"),
                "opt_out_status": lead.get("opt_out_status"),
                "message_count": lead.get("message_count") or 0,
                "updated_at": lead.get("updated_at"),
            }
            for lead in leads
        ],
        "statuses": [{"slug": s, "label": label} for s, label in LEAD_STATUSES],
    })


@crm_bp.route("/api/crm/leads/<int:lead_id>")
@auth.subscription_required
def api_lead_detail(lead_id):
    user = auth.get_current_user()
    lead = db.get_lead(lead_id, user["id"])
    if not lead:
        return jsonify({"error": "Lead not found."}), 404
    return jsonify({
        "lead": {
            **lead,
            "status": normalize_lead_status(lead.get("status")),
            "status_label": status_label(lead.get("status")),
        },
        "activities": crm_db.list_lead_activities(user["id"], lead_id),
        "tasks": [t for t in crm_db.list_tasks(user["id"]) if t.get("lead_id") == lead_id],
        "appointments": crm_db.list_appointments(user["id"], lead_id=lead_id),
        "needs_attention": [
            n for n in crm_db.list_needs_attention(user["id"]) if n.get("lead_id") == lead_id
        ],
    })


@crm_bp.route("/api/crm/leads/<int:lead_id>", methods=["PATCH"])
@auth.subscription_required
def api_patch_lead(lead_id):
    """Explicit identity edit. Never used as the default for duplicate-phone creates."""
    user = auth.get_current_user()
    lead = db.get_lead(lead_id, user["id"])
    if not lead:
        return jsonify({"error": "Lead not found."}), 404
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    if name is not None:
        name = str(name).strip()[:200]
        if not name:
            return jsonify({"error": "Lead name is required."}), 400
    lead_type = data.get("lead_type")
    if lead_type is not None:
        lead_type = str(lead_type).strip()[:80] or None
    property_interest = data.get("property_interest")
    if property_interest is not None:
        property_interest = str(property_interest).strip()[:500] or None
    db.update_lead_contact_fields(
        lead_id,
        user["id"],
        name=name,
        lead_type=lead_type,
        property_interest=property_interest,
    )
    crm_db.add_lead_activity(
        lead_id,
        user["id"],
        "lead_updated",
        "Lead details updated",
        {"fields": [key for key in ("name", "lead_type", "property_interest") if key in data]},
        actor_user_id=user["id"],
    )
    updated = db.get_lead(lead_id, user["id"])
    return jsonify({"ok": True, "lead": updated})


@crm_bp.route("/api/crm/leads/<int:lead_id>/contact", methods=["POST"])
@auth.subscription_required
def api_update_lead_contact(lead_id):
    user = auth.get_current_user()
    data = request.get_json(silent=True) or {}
    updated, error, status_code = lead_contact_service.update_lead_contact_info(
        user["id"],
        lead_id,
        data,
        actor_user_id=user["id"],
        source="crm_api",
    )
    if error:
        return jsonify({"error": error}), status_code
    return jsonify({"ok": True, "lead": updated})


@crm_bp.route("/api/crm/leads/<int:lead_id>/restore-name-from-history", methods=["POST"])
@auth.subscription_required
def api_restore_lead_name_from_history(lead_id):
    from lead_service import restore_lead_name_from_sms_history

    user = auth.get_current_user()
    lead, err = restore_lead_name_from_sms_history(user["id"], lead_id)
    if not lead:
        return jsonify({"error": err or "Lead not found."}), 404
    if err:
        return jsonify({"ok": True, "unchanged": True, "message": err, "lead": lead})
    return jsonify({"ok": True, "restored": True, "lead": lead})


@crm_bp.route("/api/crm/leads/<int:lead_id>/status", methods=["POST"])
@auth.subscription_required
def api_set_lead_status(lead_id):
    user = auth.get_current_user()
    data = request.get_json(silent=True) or {}
    new_status = data.get("status")
    lead, error = crm_db.set_lead_status(
        user["id"], lead_id, new_status, actor_user_id=user["id"], from_automation=False
    )
    if error:
        return jsonify({"error": error}), 400 if lead is None and "not found" not in error.lower() else 404 if "not found" in error.lower() else 400
    if not lead:
        return jsonify({"error": error or "Unable to update status."}), 400
    return jsonify({"ok": True, "lead": lead, "status_label": status_label(lead.get("status"))})


@crm_bp.route("/api/crm/leads/<int:lead_id>/activities")
@auth.subscription_required
def api_lead_activities(lead_id):
    user = auth.get_current_user()
    lead = db.get_lead(lead_id, user["id"])
    if not lead:
        return jsonify({"error": "Lead not found."}), 404
    return jsonify({"activities": crm_db.list_lead_activities(user["id"], lead_id)})


@crm_bp.route("/api/crm/leads/<int:lead_id>/follow-up", methods=["GET", "POST"])
@auth.subscription_required
def api_set_follow_up(lead_id):
    user = auth.get_current_user()
    if request.method == "GET":
        user_timezone = db.get_user_timezone(user["id"])
        windows = crm_db._follow_up_windows(user["id"], timezone_name=user_timezone)
        items = crm_db.list_lead_follow_ups(user["id"], lead_id, include_completed=True)
        groups = crm_db.group_follow_ups_for_lead(
            items,
            timezone_name=user_timezone,
            windows=windows,
            user_id=user["id"],
        )
        return jsonify(
            {
                "follow_ups": items,
                "groups": groups,
                "timezone": user_timezone,
                "local_date": windows.local_date,
            }
        )

    data = request.get_json(silent=True) or {}
    due_at = data.get("due_at")
    # Prefer client-computed local due_at. Quick pick without due_at falls back to UTC.
    quick = data.get("quick_pick")
    if quick and not due_at:
        days = {"tomorrow": 1, "3d": 3, "1w": 7, "2w": 14, "30d": 30}.get(str(quick))
        if days is None:
            return jsonify({"error": "Invalid quick pick."}), 400
        due_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    if not due_at:
        return jsonify({"error": "due_at or quick_pick is required."}), 400
    reason = str(data.get("reason") or "Follow up").strip()[:500]
    priority = data.get("priority") if data.get("priority") in PRIORITIES else "normal"
    replace_existing = data.get("replace_existing")
    if replace_existing is None:
        replace_existing = True
    force_create = bool(data.get("force_create"))
    local_due_label = str(data.get("local_due_label") or "").strip()[:120] or None
    result, error = crm_db.set_lead_follow_up(
        user["id"],
        lead_id,
        due_at,
        reason,
        priority=priority,
        created_by=user["id"],
        replace_existing=bool(replace_existing),
        force_create=force_create,
        local_due_label=local_due_label,
    )
    if error == "conflict":
        return jsonify(result), 409
    if error:
        return jsonify({"error": error}), 404
    return jsonify({"ok": True, **result})


@crm_bp.route("/api/crm/leads/<int:lead_id>/follow-up/complete", methods=["POST"])
@auth.subscription_required
def api_complete_follow_up(lead_id):
    user = auth.get_current_user()
    data = request.get_json(silent=True) or {}
    follow_up_id = data.get("follow_up_id")
    ok, error = crm_db.complete_lead_follow_up(
        user["id"], lead_id, follow_up_id=follow_up_id
    )
    if not ok:
        return jsonify({"error": error}), 404
    crm_db.resolve_needs_attention_by_reason(
        user["id"], lead_id, "follow_up_overdue", "Follow-up completed"
    )
    return jsonify({"ok": True})


@crm_bp.route("/api/crm/leads/<int:lead_id>/follow-up/dismiss", methods=["POST"])
@auth.subscription_required
def api_dismiss_follow_up(lead_id):
    """Legacy dismiss endpoint — prefers structured cancel reason fields."""
    user = auth.get_current_user()
    data = request.get_json(silent=True) or {}
    follow_up_id = data.get("follow_up_id")
    if not follow_up_id:
        return jsonify({"error": "follow_up_id is required."}), 400
    if data.get("cancel_reason_code"):
        result, error = crm_db.cancel_follow_up(
            user["id"],
            follow_up_id,
            cancel_reason_code=data.get("cancel_reason_code"),
            cancel_reason_notes=str(data.get("cancel_reason_notes") or ""),
            cancelled_by_user_id=user["id"],
        )
        if error:
            status = 404 if "not found" in error.lower() else 400
            return jsonify({"error": error}), status
        return jsonify(result)
    ok, error = crm_db.dismiss_follow_up(
        user["id"],
        follow_up_id,
        reason=str(data.get("reason") or "Dismissed")[:500],
    )
    if not ok:
        return jsonify({"error": error}), 400
    return jsonify({"ok": True})


@crm_bp.route("/api/crm/follow-ups", methods=["GET"])
@auth.subscription_required
def api_list_follow_ups():
    user = auth.get_current_user()
    bucket = (request.args.get("bucket") or "all").strip()
    user_timezone = db.get_user_timezone(user["id"])
    windows = crm_db._follow_up_windows(user["id"], timezone_name=user_timezone)
    start_at = (request.args.get("start_at") or "").strip() or None
    end_at = (request.args.get("end_at") or "").strip() or None
    items = crm_db.list_follow_ups(
        user["id"],
        bucket=bucket,
        start_at=start_at,
        end_at=end_at,
        timezone_name=user_timezone,
        windows=windows,
    )
    counts = crm_db.follow_up_dashboard_counts(
        user["id"],
        timezone_name=user_timezone,
        windows=windows,
    )
    return jsonify(
        {
            "follow_ups": items,
            "counts": counts,
            "timezone": user_timezone,
            "local_date": windows.local_date,
        }
    )


@crm_bp.route("/api/crm/follow-ups/<int:follow_up_id>", methods=["GET", "PATCH"])
@auth.subscription_required
def api_follow_up_detail(follow_up_id):
    user = auth.get_current_user()
    if request.method == "GET":
        item = crm_db.get_follow_up(user["id"], follow_up_id)
        if not item:
            return jsonify({"error": "Follow-up not found."}), 404
        return jsonify({"follow_up": item})
    data = request.get_json(silent=True) or {}
    item, error = crm_db.update_follow_up(
        user["id"],
        follow_up_id,
        due_at=data.get("due_at"),
        reason=data.get("reason"),
        priority=data.get("priority"),
        local_due_label=str(data.get("local_due_label") or "").strip()[:120] or None,
    )
    if error:
        return jsonify({"error": error}), 404
    return jsonify({"ok": True, "follow_up": item})


@crm_bp.route("/api/crm/follow-ups/<int:follow_up_id>/complete", methods=["POST"])
@auth.subscription_required
def api_complete_follow_up_by_id(follow_up_id):
    user = auth.get_current_user()
    item = crm_db.get_follow_up(user["id"], follow_up_id)
    if not item:
        return jsonify({"error": "Follow-up not found."}), 404
    ok, error = crm_db.complete_follow_up(user["id"], follow_up_id)
    if not ok:
        return jsonify({"error": error}), 404
    if item.get("lead_id"):
        crm_db.resolve_needs_attention_by_reason(
            user["id"], item["lead_id"], "follow_up_overdue", "Follow-up completed"
        )
    return jsonify({"ok": True})


@crm_bp.route("/api/crm/follow-ups/<int:follow_up_id>/dismiss", methods=["POST"])
@crm_bp.route("/api/crm/follow-ups/<int:follow_up_id>/cancel", methods=["POST"])
@auth.subscription_required
def api_cancel_follow_up_by_id(follow_up_id):
    user = auth.get_current_user()
    data = request.get_json(silent=True) or {}
    code = data.get("cancel_reason_code") or data.get("reason_code")
    notes = data.get("cancel_reason_notes") or data.get("reason") or ""
    if not code and data.get("reason"):
        # Legacy clients sending free-text reason.
        result_ok, error = crm_db.dismiss_follow_up(
            user["id"], follow_up_id, reason=str(data.get("reason"))[:500]
        )
        if not result_ok:
            return jsonify({"error": error}), 400
        return jsonify({"ok": True})
    result, error = crm_db.cancel_follow_up(
        user["id"],
        follow_up_id,
        cancel_reason_code=code,
        cancel_reason_notes=str(notes or ""),
        cancelled_by_user_id=user["id"],
    )
    if error:
        status = 404 if "not found" in error.lower() else 400
        return jsonify({"error": error}), status
    return jsonify(result)


@crm_bp.route("/crm/follow-ups")
def crm_follow_ups_page():
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("subscriber_app"))
    user_timezone = db.get_user_timezone(user["id"])
    windows = crm_db._follow_up_windows(user["id"], timezone_name=user_timezone)
    view = (request.args.get("view") or "agenda").strip().lower()
    if view not in {"agenda", "month", "week"}:
        view = "agenda"
    range_key = _parse_dashboard_range_arg(
        request.args.get("due"), request.args.get("range")
    )
    status = (request.args.get("status") or "").strip().lower() or None
    if status and status not in {"open", "pending", "completed", "cancelled"}:
        status = None

    if range_key in {"today", "overdue", "this_week"} and (
        not status or status in {"open", "pending"}
    ):
        # Same query path as dashboard Pipeline cards and summary counts.
        ranged = crm_db.list_follow_ups_for_dashboard_range(
            user["id"],
            range_key,
            timezone_name=user_timezone,
            windows=windows,
        )
        groups = crm_db.group_follow_ups_for_lead(
            ranged,
            timezone_name=user_timezone,
            windows=windows,
            user_id=user["id"],
        )
        follow_ups = ranged
    else:
        follow_ups = crm_db.list_follow_ups(
            user["id"],
            bucket="all",
            limit=500,
            timezone_name=user_timezone,
            windows=windows,
        )
        if status == "open":
            follow_ups = [f for f in follow_ups if f.get("status") == "pending"]
        groups = crm_db.group_follow_ups_for_lead(
            follow_ups,
            timezone_name=user_timezone,
            windows=windows,
            user_id=user["id"],
        )

    counts = crm_db.follow_up_dashboard_counts(
        user["id"],
        timezone_name=user_timezone,
        windows=windows,
    )
    active_filter = None
    if range_key == "today":
        active_filter = "Follow-ups due today"
    elif range_key == "overdue":
        active_filter = "Overdue follow-ups"
    elif range_key == "this_week":
        active_filter = "Follow-ups due this week"
    return render_template(
        "crm_follow_ups.html",
        follow_ups=follow_ups,
        groups=groups,
        counts=counts,
        view=view,
        priorities=PRIORITIES,
        follow_up_cancel_reasons=FOLLOW_UP_CANCEL_REASONS,
        cancel_reason_label=cancel_reason_label,
        local_date=windows.local_date,
        range_filter=range_key or "",
        status_filter=status or "",
        active_filter=active_filter,
        result_count=len(follow_ups),
        user_timezone=user_timezone,
        **_nav_context(user, "follow-ups"),
    )


@crm_bp.route("/api/crm/calendar/events")
@auth.subscription_required
def api_calendar_events():
    user = auth.get_current_user()
    event_types = [
        t.strip()
        for t in (request.args.get("event_types") or "").split(",")
        if t.strip()
    ]
    statuses = [
        s.strip() for s in (request.args.get("statuses") or "").split(",") if s.strip()
    ]
    priorities = [
        p.strip() for p in (request.args.get("priorities") or "").split(",") if p.strip()
    ]
    include_cancelled = str(request.args.get("include_cancelled") or "").lower() in {
        "1",
        "true",
        "yes",
    }
    include_completed = str(request.args.get("include_completed") or "").lower() in {
        "1",
        "true",
        "yes",
    }
    assigned = request.args.get("assigned_user_id")
    try:
        assigned_user_id = int(assigned) if assigned else None
    except (TypeError, ValueError):
        assigned_user_id = None
    # Agents only see their own tenant data; assigned filter defaults to self unless manager.
    if user.get("role") != "manager":
        assigned_user_id = None  # still scoped by user_id ownership below
    events = crm_db.list_calendar_events(
        user["id"],
        start_at=(request.args.get("start_at") or "").strip() or None,
        end_at=(request.args.get("end_at") or "").strip() or None,
        event_types=event_types or None,
        statuses=statuses or None,
        priorities=priorities or None,
        lead_status=(request.args.get("lead_status") or "").strip() or None,
        lead_source=(request.args.get("lead_source") or "").strip() or None,
        assigned_user_id=assigned_user_id,
        include_cancelled=include_cancelled,
        include_completed=include_completed,
    )
    user_timezone = db.get_user_timezone(user["id"])
    windows = crm_db._follow_up_windows(user["id"], timezone_name=user_timezone)
    summary = crm_db.calendar_summary(
        user["id"],
        timezone_name=user_timezone,
        windows=windows,
    )
    return jsonify(
        {
            "events": events,
            "summary": summary,
            "timezone": user_timezone,
            "local_date": windows.local_date,
        }
    )


@crm_bp.route("/crm/calendar")
def crm_leads_calendar_page():
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("subscriber_app"))
    user_timezone = db.get_user_timezone(user["id"])
    windows = crm_db._follow_up_windows(user["id"], timezone_name=user_timezone)
    local_date = (request.args.get("local_date") or "").strip()[:10] or windows.local_date
    view = (request.args.get("view") or "month").strip().lower()
    if view not in {"month", "week", "day", "agenda"}:
        view = "month"
    include_cancelled = str(request.args.get("include_cancelled") or "").lower() in {
        "1",
        "true",
        "yes",
    }
    include_completed = str(request.args.get("include_completed") or "").lower() in {
        "1",
        "true",
        "yes",
    }
    event_type = (request.args.get("event_type") or request.args.get("event_types") or "").strip()
    date_arg = (request.args.get("date") or "").strip().lower() or None
    range_key = _parse_dashboard_range_arg(
        request.args.get("range"),
        "today" if date_arg == "today" else date_arg,
    )
    day = local_date or windows.local_date
    start_at = end_at = None
    if range_key == "today":
        day = day or windows.local_date
        start_at = windows.start_today_utc.isoformat()
        end_at = (windows.start_tomorrow_utc - timedelta(microseconds=1)).isoformat()
        view = "agenda"
    event_types = [t.strip() for t in event_type.split(",") if t.strip()] or None
    # Dashboard "Appointments today" includes appointment-family event types.
    if event_types == ["appointment"]:
        event_types = [
            "appointment",
            "showing",
            "buyer_consultation",
            "listing_consultation",
            "call",
            "outcome_required",
        ]
    events = crm_db.list_calendar_events(
        user["id"],
        start_at=start_at,
        end_at=end_at,
        event_types=event_types,
        include_cancelled=include_cancelled,
        include_completed=include_completed,
        limit=800,
    )
    # Align with dashboard count_appointments_today (calendar-day prefix of start_at).
    if range_key == "today" and event_type == "appointment":
        day_key = day or crm_db._calendar_day(local_date)
        appts = crm_db.list_appointments_for_range(
            user["id"], range_key="today", local_date=day_key
        )
        events = [
            {
                "id": f"appointment:{a['id']}",
                "source_type": "appointment",
                "source_id": a["id"],
                "event_type": crm_db._map_appointment_event_type(a),
                "lead_id": a.get("lead_id"),
                "lead_name": a.get("lead_name"),
                "title": (a.get("appointment_type") or "appointment").replace("_", " ").title(),
                "start_at": a.get("start_at"),
                "end_at": a.get("end_at") or a.get("start_at"),
                "status": a.get("status"),
                "priority": "normal",
                "assigned_agent": None,
            }
            for a in appts
        ]
    summary = crm_db.calendar_summary(
        user["id"],
        timezone_name=user_timezone,
        windows=windows,
    )
    active_filter = None
    if range_key == "today" and event_type == "appointment":
        active_filter = "Appointments today"
    elif event_type:
        active_filter = f"Event type: {event_type}"
    return render_template(
        "crm_leads_calendar.html",
        events=events,
        summary=summary,
        view=view,
        event_types=CALENDAR_EVENT_TYPES,
        statuses=LEAD_STATUSES,
        priorities=PRIORITIES,
        follow_up_cancel_reasons=FOLLOW_UP_CANCEL_REASONS,
        common_timezones=COMMON_TIMEZONES,
        user_timezone=user_timezone,
        include_cancelled=include_cancelled,
        include_completed=include_completed,
        local_date=local_date or windows.local_date,
        range_filter=range_key or "",
        event_type_filter=event_type or "",
        active_filter=active_filter,
        result_count=len(events),
        **_nav_context(user, "calendar"),
    )


@crm_bp.route("/crm/tools/cleanup-duplicate-follow-ups", methods=["GET", "POST"])
def crm_cleanup_duplicate_follow_ups():
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("subscriber_app"))
    dry_run = True
    if request.method == "POST":
        dry_run = str(request.form.get("dry_run") or "1") != "0"
        report = crm_db.find_duplicate_open_follow_ups(user["id"], dry_run=dry_run)
    else:
        report = crm_db.find_duplicate_open_follow_ups(user["id"], dry_run=True)
    return render_template(
        "crm_cleanup_follow_ups.html",
        report=report,
        **_nav_context(user, "follow-ups"),
    )


@crm_bp.route("/api/crm/tasks", methods=["GET", "POST"])
@auth.subscription_required
def api_tasks():
    user = auth.get_current_user()
    if request.method == "GET":
        bucket = (request.args.get("bucket") or "all").strip()
        local_date = (request.args.get("local_date") or "").strip()[:10] or None
        return jsonify({
            "tasks": crm_db.list_tasks(user["id"], bucket=bucket, local_date=local_date),
            "local_date": local_date,
        })
    data = request.get_json(silent=True) or {}
    task_id, error = crm_db.create_task(user["id"], data)
    if error:
        return jsonify({"error": error}), 400
    return jsonify({"ok": True, "id": task_id}), 201


@crm_bp.route("/api/crm/tasks/<int:task_id>", methods=["GET", "PATCH", "POST"])
@auth.subscription_required
def api_task_detail(task_id):
    user = auth.get_current_user()
    if request.method == "GET":
        task = crm_db.get_task(user["id"], task_id)
        if not task:
            return jsonify({"error": "Task not found."}), 404
        return jsonify({"task": task})
    data = request.get_json(silent=True) or {}
    task, error = crm_db.update_task(user["id"], task_id, data)
    if error:
        status = 404 if "not found" in error.lower() else 400
        return jsonify({"error": error}), status
    return jsonify({"ok": True, "task": task})


@crm_bp.route("/api/crm/tasks/<int:task_id>/complete", methods=["POST"])
@auth.subscription_required
def api_complete_task(task_id):
    user = auth.get_current_user()
    task, error = crm_db.complete_task(user["id"], task_id)
    if error:
        return jsonify({"error": error}), 404
    return jsonify({"ok": True, "task": task})


@crm_bp.route("/api/crm/tasks/<int:task_id>/cancel", methods=["POST"])
@auth.subscription_required
def api_cancel_task(task_id):
    user = auth.get_current_user()
    ok, error = crm_db.cancel_task(user["id"], task_id)
    if error:
        return jsonify({"error": error}), 404
    return jsonify({"ok": True})


@crm_bp.route("/api/crm/appointments", methods=["GET", "POST"])
@auth.subscription_required
def api_appointments():
    user = auth.get_current_user()
    if request.method == "GET":
        lead_id = request.args.get("lead_id", type=int)
        return jsonify({"appointments": crm_db.list_appointments(user["id"], lead_id=lead_id)})
    data = request.get_json(silent=True) or {}
    appt_id, error = crm_db.create_appointment(user["id"], data)
    if error:
        return jsonify({"error": error}), 400
    if data.get("set_status"):
        crm_db.set_lead_status(
            user["id"], data["lead_id"], "appointment_scheduled", actor_user_id=user["id"]
        )
    return jsonify({"ok": True, "id": appt_id}), 201


def _truthy_form_flag(value):
    return str(value or "").strip().lower() in {"1", "true", "on", "yes"}


def _parse_outcome_request():
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return {
            "outcome": data.get("outcome"),
            "outcome_notes": str(data.get("outcome_notes") or ""),
            "next_action": str(data.get("next_action") or ""),
            "apply_lead_status": bool(data.get("apply_lead_status")),
            "apply_follow_up": bool(data.get("apply_follow_up")),
            "apply_task": bool(data.get("apply_task")),
            "apply_needs_attention": bool(data.get("apply_needs_attention")),
        }
    form = request.form
    return {
        "outcome": form.get("outcome"),
        "outcome_notes": str(form.get("outcome_notes") or ""),
        "next_action": str(form.get("next_action") or ""),
        "apply_lead_status": _truthy_form_flag(form.get("apply_lead_status")),
        "apply_follow_up": _truthy_form_flag(form.get("apply_follow_up")),
        "apply_task": _truthy_form_flag(form.get("apply_task")),
        "apply_needs_attention": _truthy_form_flag(form.get("apply_needs_attention")),
    }


@crm_bp.route("/api/crm/appointments/<int:appointment_id>/outcome-preview", methods=["GET", "POST"])
@auth.subscription_required
def api_appointment_outcome_preview(appointment_id):
    user = auth.get_current_user()
    data = request.get_json(silent=True) or {}
    outcome = request.args.get("outcome") or data.get("outcome")
    next_action = request.args.get("next_action") or data.get("next_action") or ""
    logger.info(
        "appointment_outcome_preview route_reached appointment_id=%s outcome_present=%s",
        appointment_id,
        bool(outcome),
    )
    preview, error = crm_db.preview_appointment_outcome(
        user["id"], appointment_id, outcome, next_action=str(next_action or "")
    )
    if error:
        status = 404 if "not found" in error.lower() else 400
        return jsonify({"error": error}), status
    return jsonify({"preview": preview})


@crm_bp.route("/api/crm/appointments/<int:appointment_id>/outcome", methods=["POST"])
@auth.subscription_required
def api_appointment_outcome(appointment_id):
    user = auth.get_current_user()
    payload = _parse_outcome_request()
    logger.info(
        "appointment_outcome_api route_reached appointment_id=%s outcome_present=%s "
        "apply_status=%s apply_follow_up=%s apply_task=%s",
        appointment_id,
        bool(payload.get("outcome")),
        payload.get("apply_lead_status"),
        payload.get("apply_follow_up"),
        payload.get("apply_task"),
    )
    try:
        result, error = crm_db.record_appointment_outcome(
            user["id"],
            appointment_id,
            payload.get("outcome"),
            outcome_notes=payload.get("outcome_notes") or "",
            next_action=payload.get("next_action") or "",
            apply_lead_status=payload.get("apply_lead_status"),
            apply_follow_up=payload.get("apply_follow_up"),
            apply_task=payload.get("apply_task"),
            apply_needs_attention=payload.get("apply_needs_attention"),
        )
    except Exception:
        logger.exception(
            "appointment_outcome_api unexpected_failure appointment_id=%s",
            appointment_id,
        )
        return jsonify(
            {"error": "Could not save appointment outcome. Please try again."}
        ), 500
    if error:
        status = 404 if "not found" in error.lower() else 400
        logger.info(
            "appointment_outcome_api failure appointment_id=%s status=%s",
            appointment_id,
            status,
        )
        return jsonify({"error": error}), status
    logger.info(
        "appointment_outcome_api success appointment_id=%s", appointment_id
    )
    return jsonify(result)


@crm_bp.route(
    "/crm/leads/<int:lead_id>/appointments/<int:appointment_id>/outcome",
    methods=["POST"],
)
def crm_appointment_outcome_form(lead_id, appointment_id):
    """Form POST + redirect (PRG) for Save outcome — avoids silent JS failures."""
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("subscriber_app"))
    lead = db.get_lead(lead_id, user["id"])
    if not lead:
        return redirect(url_for("crm.crm_leads_page"))

    payload = _parse_outcome_request()
    logger.info(
        "appointment_outcome_form route_reached lead_id=%s appointment_id=%s "
        "outcome_present=%s apply_status=%s apply_follow_up=%s apply_task=%s",
        lead_id,
        appointment_id,
        bool(payload.get("outcome")),
        payload.get("apply_lead_status"),
        payload.get("apply_follow_up"),
        payload.get("apply_task"),
    )

    # Ownership: appointment must belong to this authenticated user and this lead.
    owned = None
    for appt in crm_db.list_appointments(user["id"], lead_id=lead_id, limit=200):
        if int(appt["id"]) == int(appointment_id):
            owned = appt
            break
    if not owned:
        ctx = _lead_detail_template_kwargs(
            user, lead_id, form_error="Appointment not found."
        )
        return render_template("crm_lead_detail.html", **ctx), 404

    try:
        result, error = crm_db.record_appointment_outcome(
            user["id"],
            appointment_id,
            payload.get("outcome"),
            outcome_notes=payload.get("outcome_notes") or "",
            next_action=payload.get("next_action") or "",
            apply_lead_status=payload.get("apply_lead_status"),
            apply_follow_up=payload.get("apply_follow_up"),
            apply_task=payload.get("apply_task"),
            apply_needs_attention=payload.get("apply_needs_attention"),
        )
    except Exception:
        logger.exception(
            "appointment_outcome_form unexpected_failure lead_id=%s appointment_id=%s",
            lead_id,
            appointment_id,
        )
        ctx = _lead_detail_template_kwargs(
            user,
            lead_id,
            outcome_draft={appointment_id: payload},
            form_error="Could not save appointment outcome. Please try again.",
        )
        return render_template("crm_lead_detail.html", **ctx), 500

    if error:
        logger.info(
            "appointment_outcome_form failure lead_id=%s appointment_id=%s",
            lead_id,
            appointment_id,
        )
        status = 404 if "not found" in error.lower() else 400
        ctx = _lead_detail_template_kwargs(
            user,
            lead_id,
            outcome_draft={appointment_id: payload},
            form_error=error,
        )
        return render_template("crm_lead_detail.html", **ctx), status

    notice = result.get("confirmation") or "Outcome saved."
    flash(notice, "success")
    logger.info(
        "appointment_outcome_form success lead_id=%s appointment_id=%s",
        lead_id,
        appointment_id,
    )
    return redirect(url_for("crm.crm_lead_detail_page", lead_id=lead_id), code=303)


@crm_bp.route("/api/crm/needs-attention")
@auth.subscription_required
def api_needs_attention():
    user = auth.get_current_user()
    local_date = (request.args.get("local_date") or "").strip()[:10] or None
    return jsonify({"items": crm_db.list_needs_attention(user["id"], local_date=local_date)})


@crm_bp.route("/api/crm/needs-attention/<int:item_id>/resolve", methods=["POST"])
@auth.subscription_required
def api_resolve_needs_attention(item_id):
    user = auth.get_current_user()
    data = request.get_json(silent=True) or {}
    ok, error = crm_db.resolve_needs_attention(
        user["id"], item_id, str(data.get("resolution_reason") or "")
    )
    if error:
        return jsonify({"error": error}), 400
    return jsonify({"ok": True})


@crm_bp.route("/api/crm/notifications")
@auth.subscription_required
def api_notifications():
    user = auth.get_current_user()
    unread_only = request.args.get("unread") == "1"
    return jsonify({"notifications": crm_db.list_notifications(user["id"], unread_only=unread_only)})


@crm_bp.route("/api/crm/notifications/<int:notification_id>/read", methods=["POST"])
@auth.subscription_required
def api_mark_notification_read(notification_id):
    user = auth.get_current_user()
    crm_db.mark_notification_read(user["id"], notification_id)
    return jsonify({"ok": True})


@crm_bp.route("/api/crm/pipeline")
@auth.subscription_required
def api_pipeline():
    user = auth.get_current_user()
    range_key = (request.args.get("range") or "30d").strip()
    since = None
    now = datetime.now(timezone.utc)
    if range_key == "today":
        since = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    elif range_key == "7d":
        since = (now - timedelta(days=7)).isoformat()
    elif range_key == "30d":
        since = (now - timedelta(days=30)).isoformat()
    elif range_key == "custom":
        since = (request.args.get("since") or "").strip() or None
    user_timezone = db.get_user_timezone(user["id"])
    windows = crm_db._follow_up_windows(user["id"], timezone_name=user_timezone)
    metrics = crm_db.get_pipeline_metrics(
        user["id"],
        since_iso=since,
        timezone_name=user_timezone,
        windows=windows,
    )
    return jsonify(
        {
            "metrics": metrics,
            "range": range_key,
            "timezone": user_timezone,
            "local_date": windows.local_date,
        }
    )


@crm_bp.route("/api/crm/insights/<int:insight_id>/approve-status", methods=["POST"])
@auth.subscription_required
def api_approve_suggested_status(insight_id):
    """Agent-approved application of Claude suggested status. Never auto-applied."""
    user = auth.get_current_user()
    insight = db.get_insight(insight_id, user["id"])
    if not insight:
        return jsonify({"error": "Suggestion not found."}), 404
    suggestions = _parse_insight_suggestions(insight)
    data = request.get_json(silent=True) or {}
    status = data.get("status") or suggestions.get("suggested_lead_status")
    if not status:
        return jsonify({"error": "No suggested status to apply."}), 400
    lead, error = crm_db.set_lead_status(
        user["id"], insight["lead_id"], status, actor_user_id=user["id"], from_automation=False
    )
    if error:
        return jsonify({"error": error}), 400
    crm_db.add_lead_activity(
        insight["lead_id"],
        user["id"],
        "insight_status_approved",
        f"Approved Claude status suggestion: {status_label(status)}",
        {"insight_id": insight_id, "status": status},
    )
    return jsonify({"ok": True, "lead": lead})


@crm_bp.route("/api/crm/insights/<int:insight_id>/approve-follow-up", methods=["POST"])
@auth.subscription_required
def api_approve_suggested_follow_up(insight_id):
    user = auth.get_current_user()
    insight = db.get_insight(insight_id, user["id"])
    if not insight:
        return jsonify({"error": "Suggestion not found."}), 404
    suggestions = _parse_insight_suggestions(insight)
    data = request.get_json(silent=True) or {}
    due_at = data.get("due_at") or suggestions.get("suggested_follow_up_at")
    if not due_at:
        return jsonify({"error": "No suggested follow-up time."}), 400
    reason = str(
        data.get("reason") or suggestions.get("suggested_follow_up_reason") or "Follow up"
    )[:500]
    result, error = crm_db.set_lead_follow_up(
        user["id"],
        insight["lead_id"],
        due_at,
        reason,
        created_by=user["id"],
        replace_existing=True,
        local_due_label=str(data.get("local_due_label") or "").strip()[:120] or None,
    )
    if error == "conflict":
        return jsonify(result), 409
    if error:
        return jsonify({"error": error}), 404
    return jsonify({"ok": True, **result})


@crm_bp.route("/api/crm/insights/<int:insight_id>/approve-tasks", methods=["POST"])
@auth.subscription_required
def api_approve_suggested_tasks(insight_id):
    user = auth.get_current_user()
    insight = db.get_insight(insight_id, user["id"])
    if not insight:
        return jsonify({"error": "Suggestion not found."}), 404
    suggestions = _parse_insight_suggestions(insight)
    data = request.get_json(silent=True) or {}
    tasks = data.get("tasks") if isinstance(data.get("tasks"), list) else suggestions.get("suggested_tasks")
    if not tasks:
        return jsonify({"error": "No suggested tasks."}), 400
    created = []
    for item in tasks[:5]:
        payload = dict(item) if isinstance(item, dict) else {"title": str(item)}
        payload["lead_id"] = insight["lead_id"]
        task_id, error = crm_db.create_task(user["id"], payload)
        if not error:
            created.append(task_id)
    crm_db.add_lead_activity(
        insight["lead_id"],
        user["id"],
        "insight_tasks_approved",
        f"Approved {len(created)} Claude task suggestion(s)",
        {"insight_id": insight_id, "task_ids": created},
    )
    return jsonify({"ok": True, "created": created})


@crm_bp.route("/api/crm/insights/<int:insight_id>/approve-appointment", methods=["POST"])
@auth.subscription_required
def api_approve_suggested_appointment(insight_id):
    user = auth.get_current_user()
    insight = db.get_insight(insight_id, user["id"])
    if not insight:
        return jsonify({"error": "Suggestion not found."}), 404
    suggestions = _parse_insight_suggestions(insight)
    data = request.get_json(silent=True) or {}
    details = data.get("appointment_details") or suggestions.get("appointment_details") or {}
    if not isinstance(details, dict):
        details = {}
    start_at = data.get("start_at") or details.get("start_at") or details.get("time")
    if not start_at:
        # Default placeholder for agent to edit: tomorrow 10:00 UTC
        start_at = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0
        ).isoformat()
    payload = {
        "lead_id": insight["lead_id"],
        "appointment_type": data.get("appointment_type")
        or details.get("appointment_type")
        or details.get("type")
        or "phone_call",
        "start_at": start_at,
        "end_at": data.get("end_at") or details.get("end_at"),
        "location": data.get("location") or details.get("location") or "",
        "notes": data.get("notes") or details.get("notes") or "Created from Claude suggestion",
        "set_status": True,
    }
    appt_id, error = crm_db.create_appointment(user["id"], payload)
    if error:
        return jsonify({"error": error}), 400
    crm_db.set_lead_status(
        user["id"], insight["lead_id"], "appointment_scheduled", actor_user_id=user["id"]
    )
    crm_db.resolve_needs_attention_by_reason(
        user["id"], insight["lead_id"], "appointment_requested", "Appointment scheduled"
    )
    return jsonify({"ok": True, "id": appt_id, "start_at": start_at})
