"""Strict write-tool argument validation for the AI Harness (D5).

Every write-tool argument proposed for enqueueing, previewing, approving,
or executing must pass through :func:`validate_tool_arguments` first.
Validation **rejects** with a clear error code — it never silently
replaces an invalid argument with a default.

Known silent-default coercions this validator exists to keep out of the
harness write paths (found during the D5 pass; they live in files owned
by other agents, so they are reported, not edited, here):

* ``crm_constants.normalize_lead_status`` maps *any* unknown status to
  ``"new"`` — an invalid ``update_lead_status`` argument would land as
  a bogus "new" status instead of failing.
* ``ask_topai.schemas._clean_int`` strips non-digits and coerces e.g.
  ``"abc123"`` to ``123`` instead of rejecting a malformed id.
* ``ask_topai.schemas.sanitize_create_task`` silently replaces an
  unknown priority with ``"normal"``; ``sanitize_create_lead`` coerces
  some unknown ``lead_type`` values toward ``"buyer"``.

This module therefore validates strictly: an integer field must be an
``int`` (or a plain digit string such as ``"42"`` — never ``"12x"``,
``1.5``, or ``True``); a status must be a known lead status (legacy
aliases such as ``"hot"`` are accepted and normalised to their
canonical slug, because the codebase defines that mapping explicitly —
that is alias resolution, not a silent default); an enum field must be
one of its known values; a referenced lead/resource must exist **and**
belong to the calling tenant (a lead owned by another tenant is
``cross_tenant_lead``, not ``lead_not_found``).

Validation is not authorization: a *valid* status such as
``"closed_won"`` passes validation here, and the policy gate
(``harness.policy.decide`` / ``autonomy.allowed_auto_status``) still
decides whether it may be auto-applied. Restricted tools
(``harness.tools`` risk class ``restricted``) are rejected outright —
they are never auto-executable in any mode.
"""

from __future__ import annotations

import re
from datetime import datetime

import db

try:
    from crm_constants import (
        LEAD_STATUS_SET,
        LEGACY_STATUS_MAP,
        PRIORITIES,
        SMS_CONSENT_STATUSES,
        SMS_CONSENT_STATUS_LABELS,
    )
except Exception:  # pragma: no cover - constants module always present
    LEAD_STATUS_SET = set()
    LEGACY_STATUS_MAP = {}
    PRIORITIES = ["low", "normal", "high", "urgent"]
    SMS_CONSENT_STATUSES = ()
    SMS_CONSENT_STATUS_LABELS = {}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_INT_RE = re.compile(r"^[+-]?\d+$")

# Lead-scoped write tools: lead_id is required (and must exist for this
# tenant) unless the tool is listed in _LEAD_ID_OPTIONAL.
_LEAD_SCOPED_TOOLS = frozenset(
    {
        "update_lead_status",
        "add_lead_note",
        "update_property_criteria",
        "create_follow_up",
        "create_calendar_event",
        "send_sms",
        "draft_sms",
        "send_email",
        "draft_email",
        "initiate_ai_call",
        "place_call",
        "start_call",
        "send_listings",
        "draft_lead_email",
        "send_lead_email",
        "update_lead_contact_info",
        "update_lead_sms_consent_status",
    }
)

# Tools where lead_id is optional: create_task may be lead-less, and
# reschedule_calendar_event resolves its lead from the appointment.
_LEAD_ID_OPTIONAL_TOOLS = frozenset({"create_task", "reschedule_calendar_event"})

_LEAD_TYPES = frozenset({"buyer", "seller", "renter", "investor", "other"})

_CRITERIA_FIELDS = (
    "price_min",
    "price_max",
    "bedrooms",
    "bathrooms",
    "city",
    "neighborhood",
    "property_type",
    "property_interest",
)

_INT_FIELDS = ("price_min", "price_max", "bedrooms", "bathrooms", "duration_minutes")

# Max outbound SMS body the harness will accept as a single draft/send
# argument (Telnyx multi-segment ceiling is well above this; anything
# longer is malformed input, not a message).
_MAX_BODY_CHARS = 2000
_MAX_SUBJECT_CHARS = 200


class ValidationError(ValueError):
    """A write-tool argument failed strict validation.

    ``code`` is a stable machine-readable reason (e.g.
    ``"missing_lead_id"``, ``"invalid_status"``, ``"cross_tenant_lead"``);
    ``str(exc)`` is the human-readable message. Callers must surface the
    message and refuse the action — never substitute a default.
    """

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _fail(code, message):
    raise ValidationError(code, message)


def coerce_id(value):
    """Strict integer id: ``int`` or a plain digit string -> ``int``.

    Returns ``None`` for anything else (``"12x"``, ``1.5``, ``True``,
    ``None``, ``""``). No digit-extraction, no silent default.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INT_RE.match(value.strip()):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _clean_str(value, limit=None):
    if value is None:
        return None
    if not isinstance(value, str):
        _fail("malformed_parameter", f"expected a string, got {type(value).__name__}")
    text = value.strip()
    if limit is not None:
        text = text[:limit]
    return text or None


def _require_str_field(arguments, *keys, code, label, limit=None):
    for key in keys:
        raw = arguments.get(key)
        if raw is None:
            continue
        text = _clean_str(raw, limit)
        if text:
            return text
    _fail(code, f"{label} is required and must be a non-empty string")


def _parse_iso_datetime(value, code, label):
    if value is None:
        _fail(code, f"{label} is required")
    if not isinstance(value, str):
        _fail("malformed_parameter", f"{label} must be an ISO-8601 datetime string")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        _fail(code, f"{label} is not a valid ISO-8601 datetime: {value!r}")
    return parsed


def _lead_exists_anywhere(lead_id):
    """True when a lead with this id exists for *any* tenant."""
    try:
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT 1 FROM leads WHERE id = ? LIMIT 1", (lead_id,)
            ).fetchone()
        return row is not None
    except Exception:
        return False


def validate_lead_reference(user_id, lead_id, *, required=True):
    """Validate a lead id for *user_id*; return the lead dict.

    Raises :class:`ValidationError` with code ``missing_lead_id`` /
    ``malformed_lead_id`` / ``lead_not_found`` / ``cross_tenant_lead``.
    Returns ``None`` when not required and absent.
    """
    if lead_id is None or lead_id == "":
        if required:
            _fail("missing_lead_id", "lead_id is required")
        return None
    lead_id_int = coerce_id(lead_id)
    if lead_id_int is None:
        _fail("malformed_lead_id", f"lead_id must be an integer, got {lead_id!r}")
    lead = db.get_lead(lead_id_int, user_id)
    if lead is not None:
        return lead
    if _lead_exists_anywhere(lead_id_int):
        _fail(
            "cross_tenant_lead",
            f"lead {lead_id_int} belongs to another tenant",
        )
    _fail("lead_not_found", f"lead {lead_id_int} does not exist")


def validate_inbound_reference(user_id, inbound_id, *, required=True):
    """Validate an inbound sms_messages id for *user_id*.

    Presence and integer shape are required; existence is *not*
    required — the SMS job path falls back to the payload body when the
    row is missing (established behaviour, covered by existing tests),
    so a missing row must not fail a job that can still draft safely.
    Returns the row dict or None.
    """
    if inbound_id is None or inbound_id == "":
        if required:
            _fail("missing_inbound_id", "inbound_id is required")
        return None
    inbound_id_int = coerce_id(inbound_id)
    if inbound_id_int is None:
        _fail(
            "malformed_inbound_id",
            f"inbound_id must be an integer, got {inbound_id!r}",
        )
    try:
        return db.get_sms_message(inbound_id_int, user_id)
    except Exception:
        return None


def validate_status(value):
    """Validate a lead status; return its canonical slug.

    Legacy aliases (``"hot"`` -> ``"qualified"``, ``"closed"`` ->
    ``"closed_won"``, ``"replied"`` -> ``"contacted"``) resolve to their
    canonical slug. Anything else raises ``invalid_status`` — never
    coerced to ``"new"``.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        _fail("missing_status", "status is required")
    if not isinstance(value, str):
        _fail("malformed_parameter", f"status must be a string, got {type(value).__name__}")
    slug = value.strip().lower().replace(" ", "_")
    slug = LEGACY_STATUS_MAP.get(slug, slug)
    if slug not in LEAD_STATUS_SET:
        _fail(
            "invalid_status",
            f"unknown lead status {value!r}; valid statuses: "
            + ", ".join(sorted(LEAD_STATUS_SET)),
        )
    return slug


def _validate_phone(value, *, required, label="phone"):
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            _fail("missing_phone", f"a valid {label} is required")
        return None
    if not isinstance(value, str):
        _fail("malformed_parameter", f"{label} must be a string")
    digits = re.sub(r"\D", "", value)
    if len(digits) < 10 or len(digits) > 15:
        _fail("malformed_phone", f"{label} is not a valid phone number: {value!r}")
    return value.strip()


def _validate_email(value, *, required, label="email"):
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            _fail("missing_email", f"a valid {label} is required")
        return None
    if not isinstance(value, str) or not _EMAIL_RE.match(value.strip()):
        _fail("malformed_email", f"{label} is not a valid email address: {value!r}")
    return value.strip()


def _validate_priority(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str) or value.strip().lower() not in PRIORITIES:
        _fail(
            "invalid_priority",
            f"unknown priority {value!r}; valid priorities: "
            + ", ".join(PRIORITIES),
        )
    return value.strip().lower()


def _validate_int_field(arguments, field):
    """An integer-typed optional field: present-but-malformed rejects."""
    raw = arguments.get(field)
    if raw is None or raw == "":
        return None
    value = coerce_id(raw)
    if value is None:
        _fail("malformed_parameter", f"{field} must be an integer, got {raw!r}")
    return value


def validate_tool_arguments(user_id, tool, arguments, *, check_existence=True):
    """Strictly validate write-tool *arguments* for *user_id*.

    Returns a normalised arguments dict (ids as ``int``, legacy status
    aliases resolved to canonical slugs, strings stripped). Raises
    :class:`ValidationError` on the first invalid argument — callers
    must refuse the action with the error, never substitute a default.

    * Unknown tools raise ``unknown_tool``; restricted tools (never
      auto-executable) raise ``restricted_tool``.
    * Read/control tools are not write tools: they pass through
      unchanged.
    * With ``check_existence=False`` only shape/type checks run (no DB
      reads); the default ``True`` also verifies referenced leads /
      appointments exist and belong to this tenant, so a job enqueued
      with valid args that later became invalid fails at execution time
      when re-validated there.
    """
    from harness import tools as harness_tools

    if not isinstance(arguments, dict):
        _fail("malformed_parameter", "tool arguments must be an object")
    tool = str(tool or "").strip()
    if not tool:
        _fail("unknown_tool", "no tool name given")
    entry = harness_tools.get_tool(tool)
    if entry is None:
        _fail("unknown_tool", f"unknown tool: {tool}")
    if entry.get("risk_class") == "restricted":
        _fail(
            "restricted_tool",
            f"tool {tool} is restricted and never auto-executed",
        )
    if entry.get("kind") != "write":
        # Read/control tools carry no write arguments to validate.
        return dict(arguments)

    args = dict(arguments)
    cleaned = {}

    # Lead reference (required for lead-scoped tools; optional for
    # create_task; reschedule resolves its lead from the appointment).
    if tool in _LEAD_SCOPED_TOOLS or tool in _LEAD_ID_OPTIONAL_TOOLS:
        required = tool in _LEAD_SCOPED_TOOLS
        raw_lead_id = args.get("lead_id")
        if check_existence:
            lead = validate_lead_reference(user_id, raw_lead_id, required=required)
            if lead is not None:
                cleaned["lead_id"] = lead["id"]
        else:
            lead_id_int = coerce_id(raw_lead_id) if raw_lead_id not in (None, "") else None
            if lead_id_int is None and required:
                if raw_lead_id is None or raw_lead_id == "":
                    _fail("missing_lead_id", "lead_id is required")
                _fail("malformed_lead_id", f"lead_id must be an integer, got {raw_lead_id!r}")
            if lead_id_int is not None:
                cleaned["lead_id"] = lead_id_int

    # Per-tool argument rules.
    if tool == "create_lead":
        name = _clean_str(args.get("name") or args.get("lead_name"), 200)
        if not name:
            _fail("missing_name", "a lead name is required")
        cleaned["name"] = name
        cleaned["phone"] = _validate_phone(
            args.get("phone") or args.get("phone_number"), required=True
        )
        if args.get("email") not in (None, ""):
            cleaned["email"] = _validate_email(args.get("email"), required=False)
        lead_type = args.get("lead_type")
        if lead_type not in (None, ""):
            if not isinstance(lead_type, str) or lead_type.strip().lower() not in _LEAD_TYPES:
                _fail(
                    "invalid_lead_type",
                    f"unknown lead_type {lead_type!r}; valid types: "
                    + ", ".join(sorted(_LEAD_TYPES)),
                )
            cleaned["lead_type"] = lead_type.strip().lower()
        for field in _INT_FIELDS:
            value = _validate_int_field(args, field)
            if value is not None:
                cleaned[field] = value

    elif tool == "add_lead_note":
        cleaned["note"] = _require_str_field(
            args, "note", "notes", "text",
            code="missing_note", label="note text", limit=1500,
        )

    elif tool == "create_task":
        cleaned["title"] = _require_str_field(
            args, "title", "task",
            code="missing_title", label="task title", limit=200,
        )
        priority = _validate_priority(args.get("priority"))
        if priority is not None:
            cleaned["priority"] = priority
        if args.get("due_at") not in (None, ""):
            _parse_iso_datetime(args.get("due_at"), "malformed_due_at", "due_at")
            cleaned["due_at"] = args.get("due_at")

    elif tool == "update_property_criteria":
        has_criteria = False
        for field in _CRITERIA_FIELDS:
            raw = args.get(field)
            if raw in (None, "", False):
                continue
            has_criteria = True
            if field in _INT_FIELDS:
                cleaned[field] = _validate_int_field(args, field)
            elif isinstance(raw, str):
                cleaned[field] = raw.strip()
            else:
                _fail("malformed_parameter", f"{field} must be a string or integer")
        if not has_criteria:
            _fail("missing_criteria", "property criteria are required")

    elif tool == "create_follow_up":
        _parse_iso_datetime(args.get("due_at"), "missing_due_at", "due_at")
        cleaned["due_at"] = args.get("due_at")
        priority = _validate_priority(args.get("priority"))
        if priority is not None:
            cleaned["priority"] = priority
        if args.get("reason") not in (None, ""):
            cleaned["reason"] = _clean_str(args.get("reason"), 500)

    elif tool == "update_lead_status":
        cleaned["status"] = validate_status(args.get("status"))

    elif tool in {"create_calendar_event", "reschedule_calendar_event"}:
        if tool == "reschedule_calendar_event":
            raw_appt = args.get("appointment_id")
            appt_id = coerce_id(raw_appt)
            if appt_id is None:
                if raw_appt in (None, ""):
                    _fail("missing_appointment_id", "appointment_id is required")
                _fail(
                    "malformed_appointment_id",
                    f"appointment_id must be an integer, got {raw_appt!r}",
                )
            cleaned["appointment_id"] = appt_id
            if check_existence:
                try:
                    import crm_db

                    appt = crm_db.get_appointment(user_id, appt_id)
                except Exception:
                    appt = None
                if appt is None:
                    _fail(
                        "appointment_not_found",
                        f"appointment {appt_id} does not exist for this tenant",
                    )
        start = _parse_iso_datetime(args.get("start_at"), "missing_start_at", "start_at")
        cleaned["start_at"] = args.get("start_at")
        if args.get("end_at") not in (None, ""):
            end = _parse_iso_datetime(args.get("end_at"), "malformed_end_at", "end_at")
            if end <= start:
                _fail("malformed_parameter", "end_at must be after start_at")
            cleaned["end_at"] = args.get("end_at")
        duration = _validate_int_field(args, "duration_minutes")
        if duration is not None:
            if duration <= 0:
                _fail("malformed_parameter", "duration_minutes must be positive")
            cleaned["duration_minutes"] = duration

    elif tool in {"send_sms", "draft_sms"}:
        cleaned["body"] = _require_str_field(
            args, "body", "message", "text",
            code="missing_body", label="message body", limit=_MAX_BODY_CHARS,
        )

    elif tool in {"send_email", "draft_email", "send_lead_email", "draft_lead_email"}:
        cleaned["subject"] = _require_str_field(
            args, "subject",
            code="missing_subject", label="email subject", limit=_MAX_SUBJECT_CHARS,
        )
        cleaned["body"] = _require_str_field(
            args, "body", "message", "text",
            code="missing_body", label="email body",
        )

    elif tool in {"initiate_ai_call", "place_call", "start_call", "send_listings"}:
        pass  # lead reference above is the whole validation.

    elif tool == "update_lead_contact_info":
        updates = {}
        if args.get("name") not in (None, ""):
            updates["name"] = _clean_str(args.get("name"), 200)
        if args.get("phone") not in (None, "") or args.get("phone_number") not in (None, ""):
            updates["phone"] = _validate_phone(
                args.get("phone") or args.get("phone_number"), required=False
            )
        if args.get("email") not in (None, ""):
            updates["email"] = _validate_email(args.get("email"), required=False)
        if not updates:
            _fail(
                "missing_contact_update",
                "update_lead_contact_info requires at least one of name, phone, email",
            )
        cleaned.update(updates)

    elif tool == "update_lead_sms_consent_status":
        # Restricted in every mode (policy + registry agree); the
        # restricted check above already rejects this tool. This branch
        # is unreachable, kept so a future reclassification still
        # validates the value strictly.
        valid = set(SMS_CONSENT_STATUSES) | set(SMS_CONSENT_STATUS_LABELS)
        raw = args.get("sms_consent_status") or args.get("consent_status") or args.get("status")
        if not isinstance(raw, str) or raw.strip().lower() not in valid:
            _fail(
                "invalid_consent_status",
                f"unknown sms_consent_status {raw!r}; valid values: "
                + ", ".join(sorted(valid)),
            )
        cleaned["sms_consent_status"] = raw.strip().lower()

    # Preserve any remaining caller arguments untouched (no coercion:
    # unknown extra fields are carried as-is; the registry schema, not
    # this validator, decides whether they are meaningful).
    for key, value in args.items():
        cleaned.setdefault(key, value)
    return cleaned


def validate_sms_reply_draft(user_id, draft):
    """Validate an ``sms_reply`` approval draft for *user_id*.

    Returns ``(cleaned_draft, error_code)`` — ``error_code`` is ``None``
    on success. Checks, in order: lead_id present / integer / exists
    for this tenant (cross-tenant distinguished); inbound_id present /
    integer; draft_reply present and non-empty; suggested_lead_status,
    when present, a valid lead status (an invalid suggestion is an
    error here — callers must not silently coerce it to ``"new"`` or
    drop it without recording the rejection).

    The returned draft is a copy with ids normalised to ``int`` and the
    status alias-resolved to its canonical slug.
    """
    draft = dict(draft or {})
    try:
        lead = validate_lead_reference(user_id, draft.get("lead_id"), required=True)
        validate_inbound_reference(user_id, draft.get("inbound_id"), required=True)
        reply = _require_str_field(
            draft, "draft_reply", "body",
            code="missing_draft_reply", label="draft reply", limit=_MAX_BODY_CHARS,
        )
        cleaned = dict(draft)
        cleaned["lead_id"] = lead["id"]
        cleaned["inbound_id"] = coerce_id(draft.get("inbound_id"))
        cleaned["draft_reply"] = reply
        suggested = draft.get("suggested_lead_status")
        if suggested not in (None, ""):
            cleaned["suggested_lead_status"] = validate_status(suggested)
        return cleaned, None
    except ValidationError as exc:
        return None, exc.code
