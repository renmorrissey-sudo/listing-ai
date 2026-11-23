"""Execute confirmed Phase 1 tools through existing CRM services."""

from __future__ import annotations

import json
from datetime import timedelta

import crm_db
import db
from ask_topai.audit import SOURCE, add_lead_audit
from ask_topai.schemas import build_property_interest
from external_leads.ingest import ingest_external_lead
from lead_service import duplicate_phone_message


def find_leads_by_name(user_id, name: str) -> list[dict]:
    query = (name or "").strip()
    if not query:
        return []
    leads = crm_db.filter_leads(user_id, search=query, limit=50)
    lowered = query.lower()
    exact = [lead for lead in leads if (lead.get("name") or "").strip().lower() == lowered]
    if exact:
        return exact
    return [
        lead
        for lead in leads
        if lowered in (lead.get("name") or "").lower()
    ]


def resolve_lead(user_id, arguments: dict, context: dict | None):
    """Return (lead, error_message, choices). Never guesses among multiple names."""
    context = context or {}
    lead_id = arguments.get("lead_id") or context.get("lead_id")
    lead_name = arguments.get("lead_name")
    named = bool(arguments.get("lead_name"))
    if named:
        matches = find_leads_by_name(user_id, lead_name)
        if not matches:
            return None, f"I could not find a lead named {lead_name}.", []
        if len(matches) > 1:
            choices = [
                {
                    "id": lead["id"],
                    "name": lead.get("name") or "Lead",
                    "phone_number": lead.get("phone_number"),
                }
                for lead in matches[:8]
            ]
            names = ", ".join(f"{c['name']} ({c['phone_number']})" for c in choices)
            return None, f"I found multiple leads named {lead_name}. Which one did you mean? {names}", choices
        return matches[0], None, []
    if lead_id:
        lead = db.get_lead(int(lead_id), user_id)
        if not lead:
            return None, "Lead not found.", []
        return lead, None, []
    return None, "Which lead should I use?", []


def _criteria_dict(lead) -> dict:
    raw = (lead or {}).get("property_criteria_json")
    if not raw:
        interest = (lead or {}).get("property_interest")
        return {"property_interest": interest} if interest else {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def merge_property_criteria(existing: dict, incoming: dict, *, replace: bool) -> dict:
    base = {} if replace else dict(existing or {})
    for key, value in (incoming or {}).items():
        if key in {"lead_id", "lead_name", "replace"}:
            continue
        if value in (None, ""):
            continue
        base[key] = value
    return base


def _save_criteria(user_id, lead_id, merged: dict):
    interest = build_property_interest(merged) or merged.get("property_interest")
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).isoformat()
    with db.get_db() as conn:
        conn.execute(
            """
            UPDATE leads
            SET property_criteria_json = ?,
                property_interest = COALESCE(?, property_interest),
                updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (json.dumps(merged)[:2000], interest, stamp, lead_id, user_id),
        )
    db.update_lead_contact_fields(lead_id, user_id, property_interest=interest)


def append_note(user_id, lead_id, note: str):
    text = (note or "").strip()[:1500]
    if not text:
        return None, "Note text is required."
    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return None, "Lead not found."
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).isoformat()
    with db.get_db() as conn:
        conn.execute(
            """
            UPDATE leads
            SET notes = CASE
                    WHEN notes IS NULL OR notes = '' THEN ?
                    ELSE notes || ' | ' || ?
                END,
                updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (text, text, stamp, lead_id, user_id),
        )
    crm_db.add_lead_activity(
        lead_id,
        user_id,
        "note_added",
        "Note added",
        {"source": SOURCE, "note": text[:400]},
        actor_user_id=user_id,
    )
    return db.get_lead(lead_id, user_id), None


def execute_create_lead(user_id, arguments: dict):
    payload = {
        "full_name": arguments.get("name"),
        "phone": arguments.get("phone"),
        "email": arguments.get("email"),
        "lead_type": arguments.get("lead_type") or "buyer",
        "notes": arguments.get("notes") or arguments.get("desired_outcome"),
        "property_interest": arguments.get("property_interest"),
        "source": "ask_topai",
    }
    result = ingest_external_lead(
        user_id,
        payload,
        method="ask_topai",
        actor_user_id=user_id,
        allow_identity_update=False,
    )
    if result.get("error"):
        return None, result["error"], None
    if result.get("action") != "created":
        existing = db.get_lead(result.get("lead_id"), user_id) or {}
        return None, duplicate_phone_message(existing), existing
    lead = db.get_lead(result["lead_id"], user_id)
    incoming = {
        k: arguments.get(k)
        for k in (
            "price_min",
            "price_max",
            "bedrooms",
            "bathrooms",
            "city",
            "neighborhood",
            "property_type",
            "property_interest",
        )
        if arguments.get(k) not in (None, "")
    }
    if incoming:
        _save_criteria(user_id, lead["id"], incoming)
        lead = db.get_lead(lead["id"], user_id)
    add_lead_audit(
        user_id,
        lead["id"],
        f"Ask TopAI created lead {lead.get('name')}",
        {"action": "create_lead", "source": SOURCE},
    )
    return lead, None, None


def execute_add_note(user_id, arguments: dict, context: dict | None):
    lead, err, choices = resolve_lead(user_id, arguments, context)
    if err:
        return None, err, choices
    updated, note_err = append_note(user_id, lead["id"], arguments.get("note"))
    if note_err:
        return None, note_err, None
    add_lead_audit(
        user_id,
        lead["id"],
        f"Ask TopAI added a note to {lead.get('name')}",
        {"action": "add_lead_note", "source": SOURCE},
    )
    return updated, None, None


def execute_create_task(user_id, arguments: dict, context: dict | None):
    lead = None
    if arguments.get("lead_id") or arguments.get("lead_name") or (context or {}).get("lead_id"):
        lead, err, choices = resolve_lead(user_id, arguments, context)
        if err:
            return None, err, choices
    payload = {
        "title": arguments.get("title"),
        "description": arguments.get("description") or "",
        "due_at": arguments.get("due_at"),
        "priority": arguments.get("priority") or "normal",
        "lead_id": lead["id"] if lead else None,
    }
    task_id, error = crm_db.create_task(user_id, payload)
    if error:
        return None, error, None
    task = crm_db.get_task(user_id, task_id)
    if lead:
        add_lead_audit(
            user_id,
            lead["id"],
            f"Ask TopAI created task: {arguments.get('title')}",
            {"action": "create_task", "task_id": task_id, "source": SOURCE},
        )
    return task, None, None


def execute_update_criteria(user_id, arguments: dict, context: dict | None):
    lead, err, choices = resolve_lead(user_id, arguments, context)
    if err:
        return None, err, choices
    merged = merge_property_criteria(
        _criteria_dict(lead),
        arguments,
        replace=bool(arguments.get("replace")),
    )
    _save_criteria(user_id, lead["id"], merged)
    updated = db.get_lead(lead["id"], user_id)
    add_lead_audit(
        user_id,
        lead["id"],
        f"Ask TopAI updated property criteria for {lead.get('name')}",
        {"action": "update_property_criteria", "source": SOURCE},
    )
    return updated, None, None


def execute_create_follow_up(user_id, arguments: dict, context: dict | None):
    lead, err, choices = resolve_lead(user_id, arguments, context)
    if err:
        return None, err, choices
    result, error = crm_db.set_lead_follow_up(
        user_id,
        lead["id"],
        arguments.get("due_at"),
        arguments.get("reason") or "Follow up",
        priority=arguments.get("priority") or "normal",
        created_by=user_id,
    )
    if error:
        return None, error, None
    add_lead_audit(
        user_id,
        lead["id"],
        f"Ask TopAI — Follow-up created for {lead.get('name')}",
        {"action": "create_follow_up", "source": SOURCE, "due_at": arguments.get("due_at")},
    )
    payload = dict(result or {})
    payload["name"] = lead.get("name")
    payload["due_at"] = (result or {}).get("due_at") or arguments.get("due_at")
    return payload, None, None


def execute_schedule_follow_up_sms(user_id, arguments: dict, context: dict | None):
    """Schedule a follow-up SMS send (Phase III-1).

    The ONLY scheduling surface for follow-up sends: the coach's
    follow-up suggestion stays a CRM record and never auto-schedules.
    The caller supplies the exact body, which is stored verbatim in
    the pre-created sms_messages draft row and sent verbatim at the
    scheduled time through the harness gate. Returns the job id and
    the effective fire time, or a clear error (consent missing, caps
    hit, bad input) with nothing enqueued.
    """
    lead, err, choices = resolve_lead(user_id, arguments, context)
    if err:
        return None, err, choices
    body = (
        arguments.get("body")
        or arguments.get("message")
        or arguments.get("text")
        or ""
    )
    scheduled_at = arguments.get("scheduled_at") or arguments.get("due_at")
    if not scheduled_at:
        return None, "I need a time to send the follow-up SMS.", None
    reason = arguments.get("reason") or "Follow up"
    follow_up_id = arguments.get("follow_up_id")
    try:
        from harness.followup_sends import (
            FollowUpScheduleError,
            schedule_follow_up_sms,
        )
        from harness.validation import ValidationError

        result = schedule_follow_up_sms(
            user_id,
            lead["id"],
            follow_up_id,
            body,
            scheduled_at,
            reason,
        )
    except FollowUpScheduleError as exc:
        return None, str(exc.message), None
    except ValidationError as exc:
        return None, str(exc.message), None
    add_lead_audit(
        user_id,
        lead["id"],
        f"Ask TopAI — Follow-up SMS scheduled for {lead.get('name')}",
        {
            "action": "schedule_follow_up_sms",
            "source": SOURCE,
            "scheduled_at": result.get("scheduled_at"),
            "job_id": result.get("job_id"),
            "reason": reason,
        },
    )
    payload = dict(result)
    payload["name"] = lead.get("name")
    payload["reason"] = reason
    return payload, None, None


def execute_update_status(user_id, arguments: dict, context: dict | None):
    from autonomy import allowed_auto_status

    lead, err, choices = resolve_lead(user_id, arguments, context)
    if err:
        return None, err, choices
    status = allowed_auto_status(arguments.get("status") or "")
    if not status:
        return None, "That status cannot be applied automatically.", None
    if status in {"qualified", "unqualified", "qualifying", "nurture"}:
        # Phase III-2: a qualification-lane target only applies when
        # the evaluator yields it — the model/agent may not write a
        # machine state by assertion. The transition table still
        # applies (no skipping qualifying).
        from harness.qualification import evaluate_lead, request_status_change

        evaluation = evaluate_lead(user_id, lead["id"])
        if not evaluation:
            return None, "Lead not found.", None
        if evaluation["derived_state"] != status:
            return None, (
                f"That status does not follow the qualification rules — "
                f"the evaluator yields {evaluation['derived_state']} "
                f"(score {evaluation['score']}). Set qualification fields instead."
            ), None
        updated, error = request_status_change(
            user_id, lead["id"], status, actor="ai", trigger="agent_action",
            gate_reason=(
                f"Ask TopAI status matches evaluator: {status} "
                f"(score {evaluation['score']})"
            ),
        )
        if error:
            return None, error, None
        add_lead_audit(
            user_id,
            lead["id"],
            f"Ask TopAI updated {lead.get('name')} to {status}",
            {"action": "update_lead_status", "source": SOURCE, "status": status},
        )
        return updated, None, None
    updated, error = crm_db.set_lead_status(user_id, lead["id"], status, from_automation=True)
    if error:
        return None, error, None
    add_lead_audit(
        user_id,
        lead["id"],
        f"Ask TopAI updated {lead.get('name')} to {status}",
        {"action": "update_lead_status", "source": SOURCE, "status": status},
    )
    return updated, None, None


def _agent_booking_slot(user_id, arguments: dict, existing=None):
    """(start_iso, end_iso) for an agent booking, derived the same way
    :mod:`scheduling` derives it — or (None, None) when no start parses."""
    import crm_time
    import scheduling

    start = crm_time.parse_iso_dt(arguments.get("start_at"))
    if not start:
        return None, None
    settings = scheduling.get_settings(user_id)
    duration = int(
        arguments.get("duration_minutes") or settings["default_duration_minutes"] or 30
    )
    if existing and existing.get("end_at") and existing.get("start_at"):
        prev_start = crm_time.parse_iso_dt(existing["start_at"])
        prev_end = crm_time.parse_iso_dt(existing["end_at"])
        if prev_start and prev_end:
            duration = max(15, int((prev_end - prev_start).total_seconds() // 60))
    end = crm_time.parse_iso_dt(arguments.get("end_at")) or (
        start + timedelta(minutes=duration)
    )
    return start.isoformat(), end.isoformat()


def _sync_agent_booking(user_id, lead, appt):
    """Write an agent-created appointment to the connected calendar.

    The agent's own booking does not require a calendar (§8e): no
    connection means no sync, silently. A sync failure leaves the
    booking standing — timeline entry + Needs Attention item, no
    rollback. Returns the external event id (or None).
    """
    from harness.appointment_requests import sync_agent_booking_to_calendar

    external_event_id, sync_error = sync_agent_booking_to_calendar(
        user_id, appt, lead_name=lead.get("name")
    )
    if sync_error:
        crm_db.add_lead_activity(
            lead["id"],
            user_id,
            "appointment_calendar_sync_failed",
            f"Calendar sync failed: {sync_error}",
            {
                "appointment_id": (appt or {}).get("id"),
                "start_at": (appt or {}).get("start_at"),
                "external_event_id": None,
            },
            actor_user_id=user_id,
        )
        crm_db.upsert_needs_attention(
            user_id,
            lead["id"],
            "appointment_sync_failed",
            priority="high",
            source_ref_type="appointment",
            source_ref_id=(appt or {}).get("id"),
            reason_text=f"Calendar sync failed ({sync_error}) — retry from the appointment.",
        )
    return external_event_id


def execute_create_calendar_event(user_id, arguments: dict, context: dict | None):
    import scheduling

    lead, err, choices = resolve_lead(user_id, arguments, context)
    if err:
        return None, err, choices
    # Phase III-4: the shared confirm-time collision check (internal
    # appointments + other pending/confirmed requests + external
    # calendar when connected) runs before the internal write. An
    # unreadable external calendar refuses the booking — it never
    # silently skips the check.
    slot_start, slot_end = _agent_booking_slot(user_id, arguments)
    if slot_start and slot_end:
        try:
            from harness.appointment_requests import (
                CalendarUnavailableError,
                check_agent_booking_collisions,
            )

            collisions = check_agent_booking_collisions(user_id, slot_start, slot_end)
        except CalendarUnavailableError as exc:
            return None, f"Calendar unavailable: {exc} — please retry.", None
        if collisions:
            first = collisions[0]
            return None, (
                f"That time conflicts with {first['source']} {first['summary']} "
                f"({first['start_at']})."
            ), None
    payload = dict(arguments or {})
    payload["lead_id"] = lead["id"]
    appt, error, alternatives = scheduling.create_calendar_event(user_id, payload)
    if error:
        if alternatives:
            labels = ", ".join(scheduling.format_slot_sms(s) for s in alternatives[:3])
            return None, f"{error} Nearby times: {labels}. Which works?", None
        return None, error, None
    external_event_id = _sync_agent_booking(user_id, lead, appt)
    add_lead_audit(
        user_id,
        lead["id"],
        f"Ask TopAI — Appointment scheduled with {lead.get('name')}",
        {
            "action": "create_calendar_event",
            "source": SOURCE,
            "appointment_id": (appt or {}).get("id"),
            "start_at": (appt or {}).get("start_at"),
            "external_event_id": external_event_id,
        },
    )
    return appt, None, None


def execute_reschedule_calendar_event(user_id, arguments: dict, context: dict | None):
    import scheduling

    appt_id = arguments.get("appointment_id")
    lead = None
    if not appt_id:
        lead, err, choices = resolve_lead(user_id, arguments, context)
        if err:
            return None, err, choices
        existing = scheduling.get_existing_appointment(user_id, lead_id=lead["id"])
        if not existing:
            return None, "I could not find an existing appointment to reschedule.", None
        appt_id = existing["id"]
    else:
        existing = scheduling.get_existing_appointment(user_id, appointment_id=appt_id)
        if not existing:
            return None, "Appointment not found.", None
        lead = db.get_lead(existing["lead_id"], user_id)
    slot_start, slot_end = _agent_booking_slot(user_id, arguments, existing=existing)
    if slot_start and slot_end:
        try:
            from harness.appointment_requests import (
                CalendarUnavailableError,
                check_agent_booking_collisions,
            )

            collisions = check_agent_booking_collisions(
                user_id, slot_start, slot_end, exclude_appointment_id=appt_id
            )
        except CalendarUnavailableError as exc:
            return None, f"Calendar unavailable: {exc} — please retry.", None
        if collisions:
            first = collisions[0]
            return None, (
                f"That time conflicts with {first['source']} {first['summary']} "
                f"({first['start_at']})."
            ), None
    updated, error, alternatives = scheduling.reschedule_calendar_event(user_id, appt_id, arguments)
    if error:
        if alternatives:
            labels = ", ".join(scheduling.format_slot_sms(s) for s in alternatives[:3])
            return None, f"{error} Nearby times: {labels}. Which works?", None
        return None, error, None
    if lead:
        external_event_id = _sync_agent_booking(user_id, lead, updated)
        add_lead_audit(
            user_id,
            lead["id"],
            f"Ask TopAI — Appointment rescheduled for {lead.get('name')}",
            {
                "action": "reschedule_calendar_event",
                "source": SOURCE,
                "appointment_id": appt_id,
                "start_at": (updated or {}).get("start_at"),
                "external_event_id": external_event_id,
            },
        )
    return updated, None, None


def execute_command(user_id, command: dict, context: dict | None):
    action = command.get("action")
    arguments = command.get("arguments") or {}
    if action == "create_lead":
        return "create_lead", *execute_create_lead(user_id, arguments)
    if action == "add_lead_note":
        return "add_lead_note", *execute_add_note(user_id, arguments, context)
    if action == "create_task":
        return "create_task", *execute_create_task(user_id, arguments, context)
    if action == "update_property_criteria":
        return "update_property_criteria", *execute_update_criteria(user_id, arguments, context)
    if action == "create_follow_up":
        return "create_follow_up", *execute_create_follow_up(user_id, arguments, context)
    if action == "schedule_follow_up_sms":
        return "schedule_follow_up_sms", *execute_schedule_follow_up_sms(user_id, arguments, context)
    if action == "update_lead_status":
        return "update_lead_status", *execute_update_status(user_id, arguments, context)
    if action == "create_calendar_event":
        return "create_calendar_event", *execute_create_calendar_event(user_id, arguments, context)
    if action == "reschedule_calendar_event":
        return "reschedule_calendar_event", *execute_reschedule_calendar_event(user_id, arguments, context)
    return action, None, "That command is not allowed.", None


def success_message(action: str, payload) -> str:
    if action == "create_lead":
        return f"Lead created: {(payload or {}).get('name') or 'Lead'}"
    if action == "add_lead_note":
        return f"Note added to {(payload or {}).get('name') or 'lead'}"
    if action == "create_task":
        title = (payload or {}).get("title") or "Task"
        due = (payload or {}).get("due_at")
        if due:
            return f"Task created: {title} — due {str(due)[:16].replace('T', ' ')}"
        return f"Task created: {title}"
    if action == "update_property_criteria":
        return f"Property criteria updated for {(payload or {}).get('name') or 'lead'}"
    if action == "create_follow_up":
        due = (payload or {}).get("due_at") or ""
        return f"Follow-up scheduled{((' for ' + str(due)[:16].replace('T', ' ')) if due else '')}."
    if action == "schedule_follow_up_sms":
        when = str((payload or {}).get("scheduled_at") or "")[:16].replace("T", " ")
        return f"Follow-up SMS scheduled for {(payload or {}).get('name') or 'the lead'}{(' at ' + when) if when else ''}."
    if action == "update_lead_status":
        return f"Updated {(payload or {}).get('name') or 'lead'} to {((payload or {}).get('status') or 'the new status')}."
    if action == "create_calendar_event":
        lead = (payload or {}).get("lead_name") or "the lead"
        start = str((payload or {}).get("start_at") or "")[:16].replace("T", " ")
        return f"Appointment scheduled with {lead} for {start}."
    if action == "reschedule_calendar_event":
        start = str((payload or {}).get("start_at") or "")[:16].replace("T", " ")
        return f"Appointment moved to {start}."
    return "Done."
