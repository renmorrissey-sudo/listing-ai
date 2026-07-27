"""Phase 2 CRM pages and JSON APIs. All routes require subscription + ownership."""

import json
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

import auth
import crm_db
import db
from crm_constants import (
    APPOINTMENT_OUTCOMES,
    APPOINTMENT_TYPES,
    LEAD_STATUSES,
    NEEDS_ATTENTION_REASONS,
    PRIORITIES,
    TASK_TYPES,
    normalize_lead_status,
    status_label,
)

crm_bp = Blueprint("crm", __name__)


def _user_or_redirect():
    user = auth.get_current_user()
    if not user or not auth.user_has_active_subscription(user):
        return None
    return user


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
        "active_nav": active,
        "product_name": "TopAI Real Estate Tools",
    }


@crm_bp.route("/crm/leads")
def crm_leads_page():
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("index"))
    status = (request.args.get("status") or "").strip() or None
    source = (request.args.get("source") or "").strip() or None
    leads = crm_db.filter_leads(user["id"], status=status, source=source)
    return render_template(
        "crm_leads.html",
        leads=leads,
        statuses=LEAD_STATUSES,
        status_filter=status or "",
        source_filter=source or "",
        status_label=status_label,
        **_nav_context(user, "leads"),
    )


@crm_bp.route("/crm/leads/<int:lead_id>")
def crm_lead_detail_page(lead_id):
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("index"))
    lead = db.get_lead(lead_id, user["id"])
    if not lead:
        return redirect(url_for("crm.crm_leads_page"))
    activities = _enrich_lead_activities(
        user["id"],
        crm_db.list_lead_activities(user["id"], lead_id, for_timeline=True),
    )
    tasks = [t for t in crm_db.list_tasks(user["id"], bucket="all") if t.get("lead_id") == lead_id]
    appointments = crm_db.list_appointments(user["id"], lead_id=lead_id)
    needs = [n for n in crm_db.list_needs_attention(user["id"]) if n.get("lead_id") == lead_id]
    messages = db.list_lead_messages(user["id"], lead_id)
    return render_template(
        "crm_lead_detail.html",
        lead=lead,
        activities=activities,
        tasks=tasks,
        appointments=appointments,
        needs=needs,
        messages=messages,
        statuses=LEAD_STATUSES,
        task_types=TASK_TYPES,
        appointment_types=APPOINTMENT_TYPES,
        appointment_outcomes=APPOINTMENT_OUTCOMES,
        priorities=PRIORITIES,
        status_label=status_label,
        **_nav_context(user, "leads"),
    )


@crm_bp.route("/crm/tasks")
def crm_tasks_page():
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("index"))
    local_date = (request.args.get("local_date") or "").strip()[:10] or None
    return render_template(
        "crm_tasks.html",
        overdue=crm_db.list_tasks(user["id"], "overdue", local_date=local_date),
        today=crm_db.list_tasks(user["id"], "today", local_date=local_date),
        upcoming=crm_db.list_tasks(user["id"], "upcoming", local_date=local_date),
        task_types=TASK_TYPES,
        priorities=PRIORITIES,
        local_date=local_date or "",
        **_nav_context(user, "tasks"),
    )


@crm_bp.route("/crm/needs-attention")
def crm_needs_attention_page():
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("index"))
    local_date = (request.args.get("local_date") or "").strip()[:10] or None
    items = crm_db.list_needs_attention(user["id"], local_date=local_date)
    return render_template(
        "crm_needs_attention.html",
        items=items,
        reason_labels=NEEDS_ATTENTION_REASONS,
        local_date=local_date or "",
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


@crm_bp.route("/api/crm/leads/<int:lead_id>/follow-up", methods=["POST"])
@auth.subscription_required
def api_set_follow_up(lead_id):
    user = auth.get_current_user()
    data = request.get_json(silent=True) or {}
    quick = data.get("quick_pick")
    due_at = data.get("due_at")
    if quick and not due_at:
        days = {"tomorrow": 1, "3d": 3, "1w": 7, "2w": 14, "30d": 30}.get(str(quick))
        if days is None:
            return jsonify({"error": "Invalid quick pick."}), 400
        due_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    if not due_at:
        return jsonify({"error": "due_at or quick_pick is required."}), 400
    reason = str(data.get("reason") or "Follow up").strip()[:500]
    priority = data.get("priority") if data.get("priority") in PRIORITIES else "normal"
    follow_up_id, error = crm_db.set_lead_follow_up(
        user["id"], lead_id, due_at, reason, priority=priority, created_by=user["id"]
    )
    if error:
        return jsonify({"error": error}), 404
    return jsonify({"ok": True, "follow_up_id": follow_up_id, "due_at": due_at})


@crm_bp.route("/api/crm/leads/<int:lead_id>/follow-up/complete", methods=["POST"])
@auth.subscription_required
def api_complete_follow_up(lead_id):
    user = auth.get_current_user()
    ok, error = crm_db.complete_lead_follow_up(user["id"], lead_id)
    if not ok:
        return jsonify({"error": error}), 404
    crm_db.resolve_needs_attention_by_reason(user["id"], lead_id, "follow_up_overdue", "Follow-up completed")
    return jsonify({"ok": True})


@crm_bp.route("/api/crm/leads/<int:lead_id>/follow-up/dismiss", methods=["POST"])
@auth.subscription_required
def api_dismiss_follow_up(lead_id):
    user = auth.get_current_user()
    data = request.get_json(silent=True) or {}
    crm_db.dismiss_lead_follow_up(user["id"], lead_id, str(data.get("reason") or "Dismissed")[:500])
    return jsonify({"ok": True})


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


@crm_bp.route("/api/crm/appointments/<int:appointment_id>/outcome", methods=["POST"])
@auth.subscription_required
def api_appointment_outcome(appointment_id):
    user = auth.get_current_user()
    data = request.get_json(silent=True) or {}
    ok, error = crm_db.record_appointment_outcome(
        user["id"],
        appointment_id,
        data.get("outcome"),
        outcome_notes=str(data.get("outcome_notes") or ""),
        next_action=str(data.get("next_action") or ""),
    )
    if error:
        return jsonify({"error": error}), 400
    return jsonify({"ok": True})


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
    metrics = crm_db.get_pipeline_metrics(user["id"], since_iso=since)
    return jsonify({"metrics": metrics, "range": range_key})


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
    follow_up_id, error = crm_db.set_lead_follow_up(
        user["id"], insight["lead_id"], due_at, reason, created_by=user["id"]
    )
    if error:
        return jsonify({"error": error}), 404
    crm_db.add_lead_activity(
        insight["lead_id"],
        user["id"],
        "insight_follow_up_approved",
        "Approved Claude follow-up suggestion",
        {"insight_id": insight_id, "due_at": due_at},
    )
    return jsonify({"ok": True, "follow_up_id": follow_up_id, "due_at": due_at})


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
