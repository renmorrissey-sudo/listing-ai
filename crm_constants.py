"""Phase 2 CRM constants and stage mappings."""

LEAD_STATUSES = [
    ("new", "New"),
    ("attempting_contact", "Attempting Contact"),
    ("contacted", "Contacted"),
    ("qualified", "Qualified"),
    ("appointment_scheduled", "Appointment Scheduled"),
    ("appointment_completed", "Appointment Completed"),
    ("nurture", "Nurture"),
    ("under_contract", "Under Contract"),
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

TASK_STATUSES = ["open", "in_progress", "completed", "cancelled"]
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
    "other",
]

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
    "opt_out": "Opt-out request received",
    "call_failed": "AI call failed or did not connect",
    "review_call_outcome": "Review AI call outcome",
}

PROTECTED_RESOLVE_REASONS = {"opt_out", "delivery_failed", "sensitive_topic"}

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
