"""Phase 2 CRM constants and stage mappings."""

from datetime import datetime, timedelta, timezone

LEAD_STATUSES = [
    ("new", "New"),
    ("attempting_contact", "Attempting Contact"),
    ("contacted", "Contacted"),
    ("qualified", "Qualified"),
    ("appointment_scheduled", "Appointment Scheduled"),
    ("appointment_completed", "Appointment Completed"),
    ("nurture", "Nurture"),
    ("under_contract", "Active Client"),
    ("closed_won", "Closed Won"),
    ("closed_lost", "Closed Lost"),
    ("do_not_contact", "Do Not Contact"),
]

LEAD_STATUS_SET = {slug for slug, _ in LEAD_STATUSES}

LEGACY_STATUS_MAP = {
    "replied": "contacted",
    "hot": "qualified",
    "closed": "closed_won",
}

PIPELINE_STAGES = [
    ("new", "New", {"new"}),
    ("contacting", "Contacting", {"attempting_contact", "contacted"}),
    ("engaged", "Engaged", {"nurture"}),
    ("qualified", "Qualified", {"qualified"}),
    ("appointment", "Appointment", {"appointment_scheduled", "appointment_completed"}),
    ("active_client", "Active Client", {"under_contract"}),
    ("closed", "Closed", {"closed_won", "closed_lost", "do_not_contact"}),
]

PRIORITIES = ["low", "normal", "high", "urgent"]

FOLLOW_UP_CANCEL_REASONS = [
    ("duplicate_follow_up", "Duplicate follow-up"),
    ("no_longer_needed", "No longer needed"),
    ("lead_requested_no_further_contact", "Lead requested no further contact"),
    ("lead_not_ready", "Lead is not ready"),
    ("lead_no_longer_qualified", "Lead is no longer qualified"),
    ("appointment_already_scheduled", "Appointment already scheduled"),
    ("handled_another_way", "Follow-up handled another way"),
    ("created_by_mistake", "Created by mistake"),
    ("other", "Other"),
]
FOLLOW_UP_CANCEL_REASON_SET = {code for code, _ in FOLLOW_UP_CANCEL_REASONS}
FOLLOW_UP_CANCEL_REASON_LABELS = {code: label for code, label in FOLLOW_UP_CANCEL_REASONS}

# Unified Leads Calendar event kinds (stable filter values).
CALENDAR_EVENT_TYPES = [
    ("follow_up", "Follow-ups"),
    ("task", "Tasks"),
    ("appointment", "Appointments"),
    ("showing", "Showing appointments"),
    ("buyer_consultation", "Buyer consultations"),
    ("listing_consultation", "Listing consultations"),
    ("call", "Calls"),
    ("sms_follow_up", "SMS follow-ups"),
    ("outcome_required", "Outcome-required reminders"),
]
CALENDAR_EVENT_TYPE_SET = {code for code, _ in CALENDAR_EVENT_TYPES}

COMMON_TIMEZONES = [
    "America/Denver",
    "America/Phoenix",
    "America/Los_Angeles",
    "America/Chicago",
    "America/New_York",
    "America/Anchorage",
    "Pacific/Honolulu",
    "UTC",
]

TASK_STATUSES = ["open", "in_progress", "completed", "cancelled"]
# Statuses treated as "open"/active work in the Tasks views.
TASK_OPEN_STATUSES = ["open", "in_progress"]
# Allowed status filters on the Tasks page (validated against URL input).
TASK_STATUS_FILTERS = ["open", "completed", "all"]
# Allowed completion-date range presets for the Completed tasks view.
TASK_COMPLETION_RANGES = ["today", "last_7_days", "last_30_days", "custom", "all"]
TASK_TYPES = [
    "call",
    "send_sms",
    "send_email",
    "schedule_showing",
    "prepare_cma",
    "buyer_consultation",
    "listing_consultation",
    "review_documents",
    "general_follow_up",
    "other",
]

APPOINTMENT_TYPES = [
    "phone_call",
    "video_meeting",
    "buyer_consultation",
    "listing_consultation",
    "property_showing",
    "open_house_follow_up",
    "other",
]
APPOINTMENT_STATUSES = [
    "proposed",
    "scheduled",
    "confirmed",
    "completed",
    "cancelled",
    "no_show",
    "rescheduled",
]
APPOINTMENT_OUTCOMES = [
    "qualified_opportunity",
    "follow_up_required",
    "showing_scheduled",
    "listing_appointment_scheduled",
    "buyer_agreement_signed",
    "listing_agreement_signed",
    "not_ready",
    "not_qualified",
    "lost_to_another_agent",
    "no_response",
    "no_show",
    "other",
]

# Suggestions only — never applied unless the agent explicitly approves.
# Fields are separate from appointment.status and appointment.outcome.
APPOINTMENT_OUTCOME_SUGGESTIONS = {
    "qualified_opportunity": {
        "appointment_status": "completed",
        "lead_status": "qualified",
        "next_action": "Schedule buyer consultation",
        "follow_up_business_days": 1,
        "follow_up_label": "Within 1 business day",
        "create_task_type": "buyer_consultation",
        "create_task_title": "Schedule buyer consultation",
        "needs_attention": False,
    },
    "follow_up_required": {
        "appointment_status": "completed",
        "lead_status": "contacted",
        "next_action": "Complete the agreed follow-up from the appointment",
        "follow_up_business_days": 2,
        "follow_up_label": "Within 2 business days",
        "create_task_type": "general_follow_up",
        "create_task_title": "Appointment follow-up",
        "needs_attention": False,
    },
    "showing_scheduled": {
        "appointment_status": "completed",
        "lead_status": "appointment_scheduled",
        "next_action": "Confirm showing details and send reminder",
        "follow_up_business_days": 1,
        "follow_up_label": "Before the showing",
        "create_task_type": "schedule_showing",
        "create_task_title": "Confirm upcoming showing",
        "needs_attention": False,
    },
    "listing_appointment_scheduled": {
        "appointment_status": "completed",
        "lead_status": "appointment_scheduled",
        "next_action": "Prepare listing appointment materials",
        "follow_up_business_days": 1,
        "follow_up_label": "Before the listing appointment",
        "create_task_type": "listing_consultation",
        "create_task_title": "Prepare for listing appointment",
        "needs_attention": False,
    },
    "buyer_agreement_signed": {
        "appointment_status": "completed",
        "lead_status": "under_contract",
        "next_action": "Upload buyer agreement and start active-client checklist",
        "follow_up_business_days": 1,
        "follow_up_label": "Within 1 business day",
        "create_task_type": "review_documents",
        "create_task_title": "Process signed buyer agreement",
        "needs_attention": False,
    },
    "listing_agreement_signed": {
        "appointment_status": "completed",
        "lead_status": "under_contract",
        "next_action": "Upload listing agreement and launch listing checklist",
        "follow_up_business_days": 1,
        "follow_up_label": "Within 1 business day",
        "create_task_type": "review_documents",
        "create_task_title": "Process signed listing agreement",
        "needs_attention": False,
    },
    "not_ready": {
        "appointment_status": "completed",
        "lead_status": "nurture",
        "next_action": "Add nurture touchpoint and check back later",
        "follow_up_business_days": 7,
        "follow_up_label": "Within 1 week",
        "create_task_type": "general_follow_up",
        "create_task_title": "Nurture follow-up",
        "needs_attention": False,
    },
    "not_qualified": {
        "appointment_status": "completed",
        "lead_status": "closed_lost",
        "next_action": "Close lead and note disqualification reason",
        "follow_up_business_days": None,
        "follow_up_label": None,
        "create_task_type": None,
        "create_task_title": None,
        "needs_attention": False,
    },
    "lost_to_another_agent": {
        "appointment_status": "completed",
        "lead_status": "closed_lost",
        "next_action": "Close lead and capture competitor notes",
        "follow_up_business_days": None,
        "follow_up_label": None,
        "create_task_type": None,
        "create_task_title": None,
        "needs_attention": False,
    },
    "no_response": {
        "appointment_status": "completed",
        "lead_status": "attempting_contact",
        "next_action": "Retry contact after no response at appointment",
        "follow_up_business_days": 1,
        "follow_up_label": "Within 1 business day",
        "create_task_type": "call",
        "create_task_title": "Retry contact after no response",
        "needs_attention": False,
    },
    "no_show": {
        "appointment_status": "no_show",
        "lead_status": "attempting_contact",
        "next_action": "Reschedule after no-show and confirm interest",
        "follow_up_business_days": 1,
        "follow_up_label": "Within 1 business day",
        "create_task_type": "call",
        "create_task_title": "Follow up after appointment no-show",
        "needs_attention": True,
    },
    "other": {
        "appointment_status": "completed",
        "lead_status": "appointment_completed",
        "next_action": "Review appointment notes and choose next step",
        "follow_up_business_days": 2,
        "follow_up_label": "Within 2 business days",
        "create_task_type": None,
        "create_task_title": None,
        "needs_attention": False,
    },
}

NEEDS_ATTENTION_REASONS = {
    "unreviewed_inbound": "New inbound SMS not reviewed",
    "draft_awaiting_approval": "Draft reply awaiting approval",
    "high_intent": "High buying/selling intent",
    "appointment_requested": "Appointment requested",
    "sensitive_topic": "Sensitive topic requires manual handling",
    "low_confidence": "Low AI confidence",
    "follow_up_overdue": "Follow-up overdue",
    "task_overdue": "Task overdue",
    "no_first_contact": "No first contact within response target",
    "delivery_failed": "Message failed or undelivered",
    "appointment_outcome_missing": "Appointment outcome not recorded",
    "appointment_no_show": "Appointment no-show — follow up required",
    "opt_out": "Opt-out request received",
    "consent_review_required": "Consent review required",
    "call_failed": "AI call failed or did not connect",
    "review_call_outcome": "Review AI call outcome",
}

PROTECTED_RESOLVE_REASONS = {"opt_out", "delivery_failed", "sensitive_topic", "consent_review_required"}

SMS_CONSENT_STATUSES = (
    "unverified",
    "verified",
    "opted_out",
    "revoked",
    "not_permitted",
)

CONSENT_METHODS = (
    "direct_web_form",
    "verbal",
    "text_keyword",
    "external_platform",
    "paper_form",
    "qr_code",
    "other",
)

EVIDENCE_TYPES = (
    "source_record",
    "screenshot",
    "document",
    "URL",
    "disclosure_text",
    "verbal_attestation",
    "platform_metadata",
    "other",
)

EXTERNAL_SOURCE_CATEGORIES = (
    "portal_inquiry",
    "brokerage_lead_pond",
    "IDX",
    "referral_platform",
    "CRM",
    "predictive_prospect",
    "prospecting_list",
    "CSV",
    "email_parser",
    "webhook",
    "API",
    "manual",
    "other",
)

POND_STATUSES = ("unassigned", "claimable", "claimed", "assigned")

CONSENT_CONFIRMATION_STATEMENT = (
    "I confirm that this consumer expressly agreed to receive SMS messages from the "
    "identified real estate professional or brokerage for the stated purpose, and that "
    "the consent evidence is accurate and retained."
)

VERBAL_CONSENT_SCRIPT = (
    "May I send you conversational text messages regarding the real estate information we "
    "discussed, including property information, appointment scheduling, reminders, and "
    "follow-up? Message frequency varies. Message and data rates may apply. Reply STOP to "
    "opt out or HELP for help. Consent is not required to work with me."
)

CONFIDENCE_THRESHOLD = 0.55
FIRST_RESPONSE_HOURS = 24


def normalize_lead_status(status):
    value = (status or "new").strip().lower().replace(" ", "_")
    value = LEGACY_STATUS_MAP.get(value, value)
    if value not in LEAD_STATUS_SET:
        return "new"
    return value


def stage_for_status(status):
    slug = normalize_lead_status(status)
    for stage_id, _label, members in PIPELINE_STAGES:
        if slug in members:
            return stage_id
    return "new"


def status_label(status):
    slug = normalize_lead_status(status)
    for item_slug, label in LEAD_STATUSES:
        if item_slug == slug:
            return label
    return slug.replace("_", " ").title()


def outcome_label(outcome):
    return str(outcome or "").replace("_", " ").strip().title()


def cancel_reason_label(code):
    return FOLLOW_UP_CANCEL_REASON_LABELS.get(
        code, str(code or "").replace("_", " ").strip().title()
    )


def appointment_status_label(status):
    return str(status or "").replace("_", " ").strip().title()


def next_business_day_iso(business_days=1, hour_utc=15):
    """Return an ISO timestamp N business days ahead (Mon–Fri), UTC."""
    try:
        days = int(business_days)
    except (TypeError, ValueError):
        return None
    if days is None or days <= 0:
        return None
    dt = datetime.now(timezone.utc)
    added = 0
    while added < days:
        dt += timedelta(days=1)
        if dt.weekday() < 5:
            added += 1
    return dt.replace(hour=hour_utc, minute=0, second=0, microsecond=0).isoformat()


def build_appointment_outcome_suggestion(outcome, *, current_lead_status=None, next_action_override=None):
    """Build agent-facing suggestions for an appointment outcome. Never auto-applies."""
    outcome = str(outcome or "").strip().lower()
    if outcome not in APPOINTMENT_OUTCOME_SUGGESTIONS:
        return None
    base = dict(APPOINTMENT_OUTCOME_SUGGESTIONS[outcome])
    suggested_status = base.get("lead_status")
    follow_up_at = next_business_day_iso(base.get("follow_up_business_days"))
    next_action = str(next_action_override or base.get("next_action") or "").strip()
    return {
        "outcome": outcome,
        "outcome_label": outcome_label(outcome),
        "appointment_status": base.get("appointment_status") or "completed",
        "appointment_status_label": appointment_status_label(
            base.get("appointment_status") or "completed"
        ),
        "suggested_lead_status": suggested_status,
        "suggested_lead_status_label": status_label(suggested_status) if suggested_status else None,
        "current_lead_status": normalize_lead_status(current_lead_status)
        if current_lead_status
        else None,
        "current_lead_status_label": status_label(current_lead_status)
        if current_lead_status
        else None,
        "suggested_next_action": next_action or None,
        "suggested_follow_up_at": follow_up_at,
        "suggested_follow_up_label": base.get("follow_up_label"),
        "suggested_task_type": base.get("create_task_type"),
        "suggested_task_title": base.get("create_task_title"),
        "needs_attention": bool(base.get("needs_attention")),
        "lead_status_would_change": bool(
            suggested_status
            and current_lead_status
            and normalize_lead_status(current_lead_status) != suggested_status
        ),
    }
