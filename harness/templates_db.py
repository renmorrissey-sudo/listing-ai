"""Approved message templates store (Phase III-5).

A per-tenant store of agent-approved outbound message templates. The
AI may use an approved template verbatim or with its named slots
filled; it may NOT invent new outbound message templates or present
invented text as approved.

State machine (enforced here, server-side):
    draft -> approved -> archived
    any edit to an approved template resets it to draft
    archived -> draft is allowed (rework); archived -> approved is
    disallowed.

Only status 'approved' templates are ever usable, and only for their
own tenant and channel. The III-4 deterministic texts stay OUT of
this store (no seed rows in migration 049).

Slot allowlist (fixed; a body slot outside it is rejected at
creation/edit time): lead_name, agent_name, brokerage_name,
company_name, property_interest, appointment_time, follow_up_reason,
qualification_budget, qualification_timeline. A slot with no value
for the lead/context at fill time means the template is NOT used
(fail closed) — the caller falls back to free compose.

No LIKE in the SQL (equality only). No new dependency.
"""

import logging
import re
import sqlite3
from datetime import datetime, timezone

import db
from harness.audit import record_audit

logger = logging.getLogger(__name__)

MAX_BODY_CHARS = 480
TEMPLATE_LIST_CAP = 20
AUDIT_REASON_CAP = 500

CHANNELS = ("sms", "voice")
STATUSES = ("draft", "approved", "archived")

SLOT_ALLOWLIST = frozenset(
    {
        "lead_name",
        "agent_name",
        "brokerage_name",
        "company_name",
        "property_interest",
        "appointment_time",
        "follow_up_reason",
        "qualification_budget",
        "qualification_timeline",
    }
)

_SLOT_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

_UNKNOWN = "unknown"


class TemplateError(Exception):
    """A template-store failure (400-style). ``code`` is stable."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# Slot extraction / validation
# --------------------------------------------------------------------------


def extract_slots(body):
    """Named slots referenced by *body* (``{slot}``)."""
    return set(_SLOT_RE.findall(str(body or "")))


def _validate_body(body):
    body = str(body or "").strip()
    if not body:
        raise TemplateError("invalid_body", "Template body is required.")
    if len(body) > MAX_BODY_CHARS:
        raise TemplateError(
            "body_too_long",
            f"Template body exceeds {MAX_BODY_CHARS} characters.",
        )
    unknown = extract_slots(body) - SLOT_ALLOWLIST
    if unknown:
        raise TemplateError(
            "unknown_slot",
            "Unknown template slot(s): " + ", ".join(sorted(unknown)),
        )
    return body


def _validate_channel(channel):
    channel = str(channel or "sms").strip().lower()
    if channel not in CHANNELS:
        raise TemplateError(
            "invalid_channel", f"Template channel must be one of {CHANNELS}."
        )
    return channel


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _is_unique_violation(error):
    """DB-agnostic unique-violation check (SQLite IntegrityError and
    the Postgres psycopg2 error both carry 'unique' in the message)."""
    if isinstance(error, sqlite3.IntegrityError):
        return True
    return "unique" in str(error).lower()


def _duplicate_name(name):
    return TemplateError(
        "duplicate_name",
        f"A template named '{name}' already exists for this tenant.",
    )


def _audit_reason(template_id, name, verb):
    reason = f'template #{template_id} "{name}" {verb}'
    return reason[:AUDIT_REASON_CAP]


def _record_transition(user_id, action, template_id, name, verb):
    record_audit(
        user_id,
        actor="user",
        action=action,
        tool="message_template",
        target_type="message_template",
        target_id=template_id,
        gate_reason=_audit_reason(template_id, name, verb),
    )


# --------------------------------------------------------------------------
# CRUD + state machine
# --------------------------------------------------------------------------


def create_template(user_id, name, channel="sms", purpose="", body="",
                    created_by=None):
    """Create a template in draft state. Returns the row dict."""
    name = str(name or "").strip()
    if not name:
        raise TemplateError("invalid_name", "Template name is required.")
    channel = _validate_channel(channel)
    body = _validate_body(body)
    purpose = str(purpose or "").strip()
    now = _now_iso()
    if get_template_by_name(user_id, name) is not None:
        raise _duplicate_name(name)
    try:
        with db.get_db() as conn:
            cur = conn.execute(
                """
                INSERT INTO harness_message_templates
                    (user_id, name, channel, purpose, body, status,
                     created_by, approved_by, approved_at,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'draft', ?, NULL, NULL, ?, ?)
                """,
                (
                    user_id, name, channel, purpose, body,
                    created_by if created_by is not None else user_id,
                    now, now,
                ),
            )
            template_id = cur.lastrowid
    except Exception as exc:
        if _is_unique_violation(exc):
            raise _duplicate_name(name)
        raise
    _record_transition(user_id, "message_template_created", template_id, name, "created")
    return get_template(user_id, template_id)


def get_template_by_name(user_id, name):
    """Tenant-scoped lookup by exact name."""
    with db.get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM harness_message_templates
            WHERE user_id = ? AND name = ?
            """,
            (user_id, str(name or "").strip()),
        ).fetchone()
    return dict(row) if row else None


def get_template(user_id, template_id):
    """Tenant-scoped read. Returns the row dict, or None (never
    reveals another tenant's row)."""
    try:
        template_id = int(template_id)
    except (TypeError, ValueError):
        return None
    with db.get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM harness_message_templates
            WHERE id = ? AND user_id = ?
            """,
            (template_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def list_templates(user_id, channel=None):
    """All templates for the tenant, most recently updated first."""
    with db.get_db() as conn:
        if channel:
            rows = conn.execute(
                """
                SELECT * FROM harness_message_templates
                WHERE user_id = ? AND channel = ?
                ORDER BY updated_at DESC, id DESC
                """,
                (user_id, _validate_channel(channel)),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM harness_message_templates
                WHERE user_id = ?
                ORDER BY updated_at DESC, id DESC
                """,
                (user_id,),
            ).fetchall()
    return [dict(r) for r in rows]


def list_approved_templates(user_id, channel=None, limit=TEMPLATE_LIST_CAP):
    """Approved templates for the tenant, most recently approved
    first, capped at TEMPLATE_LIST_CAP (20). The store is uncapped;
    the cap applies to this prompt-facing list only."""
    with db.get_db() as conn:
        if channel:
            rows = conn.execute(
                """
                SELECT * FROM harness_message_templates
                WHERE user_id = ? AND status = 'approved' AND channel = ?
                ORDER BY approved_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, _validate_channel(channel), int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM harness_message_templates
                WHERE user_id = ? AND status = 'approved'
                ORDER BY approved_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, int(limit)),
            ).fetchall()
    return [dict(r) for r in rows]


def _require_template(user_id, template_id):
    row = get_template(user_id, template_id)
    if row is None:
        raise TemplateError("not_found", "Template not found.")
    return row


def approve_template(user_id, template_id, approved_by=None):
    """draft -> approved. Records the approver and time. Archived ->
    approved is disallowed; approving an already-approved template
    is a no-op (returns the row)."""
    row = _require_template(user_id, template_id)
    if row["status"] == "archived":
        raise TemplateError(
            "invalid_transition",
            "An archived template cannot be approved directly; "
            "move it back to draft first.",
        )
    if row["status"] == "approved":
        return row
    now = _now_iso()
    with db.get_db() as conn:
        conn.execute(
            """
            UPDATE harness_message_templates
            SET status = 'approved', approved_by = ?, approved_at = ?,
                updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                approved_by if approved_by is not None else user_id,
                now, now, row["id"], user_id,
            ),
        )
    _record_transition(
        user_id, "message_template_approved", row["id"], row["name"], "approved"
    )
    return get_template(user_id, template_id)


def archive_template(user_id, template_id):
    """draft/approved -> archived. The approval record (approved_by/at)
    is kept as audit history."""
    row = _require_template(user_id, template_id)
    if row["status"] == "archived":
        return row
    now = _now_iso()
    with db.get_db() as conn:
        conn.execute(
            """
            UPDATE harness_message_templates
            SET status = 'archived', updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (now, row["id"], user_id),
        )
    _record_transition(
        user_id, "message_template_archived", row["id"], row["name"], "archived"
    )
    return get_template(user_id, template_id)


def unarchive_template(user_id, template_id):
    """archived -> draft (rework). Clears the stale approval record."""
    row = _require_template(user_id, template_id)
    if row["status"] != "archived":
        raise TemplateError(
            "invalid_transition",
            "Only an archived template can be moved back to draft.",
        )
    now = _now_iso()
    with db.get_db() as conn:
        conn.execute(
            """
            UPDATE harness_message_templates
            SET status = 'draft', approved_by = NULL, approved_at = NULL,
                updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (now, row["id"], user_id),
        )
    _record_transition(
        user_id, "message_template_unarchived", row["id"], row["name"],
        "moved back to draft",
    )
    return get_template(user_id, template_id)


def edit_template(user_id, template_id, name=None, channel=None,
                  purpose=None, body=None):
    """Edit a template. ANY edit resets an approved template to draft
    and clears the approval record — approval attaches to the exact
    text. Editing an archived template also moves it to draft
    (rework)."""
    row = _require_template(user_id, template_id)
    new_name = str(name).strip() if name is not None else row["name"]
    new_channel = (
        _validate_channel(channel) if channel is not None else row["channel"]
    )
    new_purpose = (
        str(purpose).strip() if purpose is not None else row["purpose"]
    )
    new_body = _validate_body(body) if body is not None else row["body"]
    if not new_name:
        raise TemplateError("invalid_name", "Template name is required.")
    existing = get_template_by_name(user_id, new_name)
    if existing is not None and existing["id"] != row["id"]:
        raise _duplicate_name(new_name)
    now = _now_iso()
    try:
        with db.get_db() as conn:
            conn.execute(
                """
                UPDATE harness_message_templates
                SET name = ?, channel = ?, purpose = ?, body = ?,
                    status = 'draft', approved_by = NULL,
                    approved_at = NULL, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    new_name, new_channel, new_purpose, new_body,
                    now, row["id"], user_id,
                ),
            )
    except Exception as exc:
        if _is_unique_violation(exc):
            raise _duplicate_name(new_name)
        raise
    _record_transition(
        user_id, "message_template_edited", row["id"], new_name,
        "edited (reset to draft)",
    )
    return get_template(user_id, template_id)


# --------------------------------------------------------------------------
# Compose-time resolution + slot filling
# --------------------------------------------------------------------------


def resolve_template_for_compose(user_id, template_id, channel="sms"):
    """Resolve a claimed template id (or name) for composing. Returns
    the row dict only when the template exists for this tenant, is for
    this channel, and is status 'approved'. Any failure returns None
    and logs (fail closed) — the caller falls back to free compose.

    A claim may be an integer id or an exact template name. The Ask
    TopAI path already resolves names to ids before composing; the
    inbound path accepts name claims directly so a name claim does
    not silently fail to resolve when the template exists. A name
    lookup is tenant-scoped and exact, and the resolved row passes
    the same channel/status checks as an id lookup."""
    if template_id is None:
        return None
    row = get_template(user_id, template_id)
    if row is None and isinstance(template_id, str) and template_id.strip():
        # Name claim: exact-name, tenant-scoped lookup.
        row = get_template_by_name(user_id, template_id)
    if row is None:
        logger.warning(
            "template claim rejected: template %s not found for tenant %s",
            template_id, user_id,
        )
        return None
    if row["channel"] != _validate_channel(channel):
        logger.warning(
            "template claim rejected: template %s is channel %s, not %s",
            template_id, row["channel"], channel,
        )
        return None
    if row["status"] != "approved":
        logger.warning(
            "template claim rejected: template %s is %s, not approved",
            template_id, row["status"],
        )
        return None
    return row


def build_slot_values(lead=None, profile=None, context=None):
    """Slot -> value for the slots that have one. A slot with no value
    is simply absent (the caller treats that as 'template not used').

    The qualification slots treat the 'unknown' sentinel as no value.
    """
    lead = lead or {}
    profile = profile or {}
    context = context or {}

    def _clean(value):
        value = str(value or "").strip()
        return value or None

    def _qualification(value):
        value = _clean(value)
        if value is None or value.lower() == _UNKNOWN:
            return None
        return value

    values = {
        "lead_name": _clean(lead.get("name")),
        "agent_name": _clean(profile.get("agent_name")),
        "brokerage_name": _clean(profile.get("brokerage_name")),
        "company_name": _clean(profile.get("company_name")),
        "property_interest": _clean(lead.get("property_interest")),
        "appointment_time": _clean(context.get("appointment_time")),
        "follow_up_reason": _clean(context.get("follow_up_reason")),
        "qualification_budget": _qualification(lead.get("qualification_budget")),
        "qualification_timeline": _qualification(lead.get("qualification_timeline")),
    }
    return {k: v for k, v in values.items() if v is not None}


def fill_template(template_row, lead=None, profile=None, context=None):
    """Deterministic server-side slot filling.

    Returns ``(filled_text, None)`` on success, or ``(None, reason)``
    when the template cannot be used: a slot has no value, or a
    leftover ``{placeholder}`` survives filling. The filled text is
    NOT truncated here — the caller applies the normal draft cap
    (``[:480]`` in the inbound handler; the follow-up body cap at
    schedule time).
    """
    body = template_row["body"]
    values = build_slot_values(lead=lead, profile=profile, context=context)
    for slot in sorted(extract_slots(body)):
        if slot not in values:
            return None, f"missing slot value: {slot}"

    def _replace(match):
        return values.get(match.group(1), match.group(0))

    filled = _SLOT_RE.sub(_replace, body)
    leftover = extract_slots(filled)
    if leftover:
        return None, "leftover placeholder(s): " + ", ".join(sorted(leftover))
    return filled, None


def resolve_and_fill(user_id, template_id, lead=None, profile=None,
                     context=None, channel="sms"):
    """One call for the compose path: resolve + fill. Returns
    ``(template_row, filled_text)`` or ``(None, None)`` when the
    template is not usable (any failure -> free compose)."""
    row = resolve_template_for_compose(user_id, template_id, channel=channel)
    if row is None:
        return None, None
    filled, _reason = fill_template(row, lead=lead, profile=profile, context=context)
    if filled is None:
        logger.warning(
            "template %s not used for tenant %s: %s",
            template_id, user_id, _reason,
        )
        return None, None
    return row, filled
