"""Central Ask TopAI tool registry.

Enabled Phase 1 tools are the only ones Claude can invoke. Future tools are
declared here so they can be enabled later without redesigning the agent loop.
"""

from __future__ import annotations

WRITE_TOOLS = frozenset(
    {
        "create_lead",
        "add_lead_note",
        "create_task",
        "update_property_criteria",
        "create_follow_up",
        "update_lead_status",
        "create_calendar_event",
        "reschedule_calendar_event",
    }
)

READ_TOOLS = frozenset(
    {
        "find_lead",
        "get_lead_context",
        "list_lead_tasks",
        "list_open_leads",
        "get_calendar_availability",
        "find_available_slots",
        "get_existing_appointment",
    }
)

CONTROL_TOOLS = frozenset({"ask_clarification", "inform_user"})

ENABLED_TOOLS = WRITE_TOOLS | READ_TOOLS | CONTROL_TOOLS

FUTURE_TOOLS = (
    "find_matching_listings",
    "create_cma",
    "draft_email",
    "send_email",
    "draft_sms",
    "send_sms",
    "initiate_ai_call",
    "generate_listing_content",
)


def _obj(properties: dict, required=None, extra=None):
    schema = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    if extra:
        schema.update(extra)
    return schema


def anthropic_tools() -> list[dict]:
    """Anthropic Messages API tool definitions for enabled tools only."""
    return [
        {
            "name": "find_lead",
            "description": (
                "Find CRM leads that belong to this agent. Search by name, phone, "
                "and/or email. Use before notes, tasks, or criteria updates when "
                "the lead is not already selected. Never guess among multiple matches."
            ),
            "input_schema": _obj(
                {
                    "name": {"type": "string", "description": "Lead name or partial name."},
                    "phone": {"type": "string", "description": "Phone number if the user provided one."},
                    "email": {"type": "string", "description": "Email if the user provided one."},
                }
            ),
        },
        {
            "name": "get_lead_context",
            "description": (
                "Load a single tenant-scoped lead: status, contact, property criteria, "
                "notes, upcoming tasks/follow-ups, and SMS qualification. Requires lead_id."
            ),
            "input_schema": _obj(
                {"lead_id": {"type": "integer", "description": "CRM lead id."}},
                required=["lead_id"],
            ),
        },
        {
            "name": "list_lead_tasks",
            "description": "List open tasks for one tenant-scoped lead.",
            "input_schema": _obj(
                {"lead_id": {"type": "integer", "description": "CRM lead id."}},
                required=["lead_id"],
            ),
        },
        {
            "name": "list_open_leads",
            "description": (
                "Get the exact count of this agent's currently open CRM leads and, "
                "when useful, a bounded list of them. Always use this tool when the "
                "agent asks how many leads are open, current, or active."
            ),
            "input_schema": _obj(
                {
                    "limit": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "Number of lead summaries to return. Use 0 for count only.",
                    }
                }
            ),
        },
        {
            "name": "create_lead",
            "description": (
                "Create a new CRM lead now. Requires a name and a valid mobile phone. "
                "Do not invent a phone or email."
            ),
            "input_schema": _obj(
                {
                    "name": {"type": "string"},
                    "phone": {"type": "string"},
                    "email": {"type": "string"},
                    "lead_type": {
                        "type": "string",
                        "enum": ["buyer", "seller", "renter", "investor", "other"],
                    },
                    "property_interest": {"type": "string"},
                    "desired_outcome": {"type": "string"},
                    "notes": {"type": "string"},
                    "price_min": {"type": "integer"},
                    "price_max": {"type": "integer"},
                    "bedrooms": {"type": "integer"},
                    "bathrooms": {"type": "integer"},
                    "city": {"type": "string"},
                    "neighborhood": {"type": "string"},
                    "location": {"type": "string"},
                    "locations": {"type": "array", "items": {"type": "string"}},
                    "neighborhoods": {"type": "array", "items": {"type": "string"}},
                    "property_type": {"type": "string"},
                    "property_types": {"type": "array", "items": {"type": "string"}},
                }
            ),
        },
        {
            "name": "add_lead_note",
            "description": (
                "Append a note to an existing lead now. Prefer lead_id from context "
                "or find_lead. Do not guess if several leads match a name."
            ),
            "input_schema": _obj(
                {
                    "lead_id": {"type": "integer"},
                    "lead_name": {"type": "string"},
                    "note": {"type": "string"},
                }
            ),
        },
        {
            "name": "create_task",
            "description": (
                "Create a task or reminder for the agent now. Attach lead_id when the reminder "
                "is about a specific person."
            ),
            "input_schema": _obj(
                {
                    "lead_id": {"type": "integer"},
                    "lead_name": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "due_date": {"type": "string", "description": "ISO date or relative phrase like tomorrow or Friday."},
                    "due_time": {"type": "string", "description": "Optional time such as 3pm or afternoon."},
                    "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]},
                }
            ),
        },
        {
            "name": "update_property_criteria",
            "description": (
                "Merge buyer/seller search criteria onto an existing lead now. "
                "Only include fields the user asked to change. Do not clear unspecified fields."
            ),
            "input_schema": _obj(
                {
                    "lead_id": {"type": "integer"},
                    "lead_name": {"type": "string"},
                    "price_min": {"type": "integer"},
                    "price_max": {"type": "integer"},
                    "bedrooms": {"type": "integer"},
                    "bathrooms": {"type": "integer"},
                    "city": {"type": "string"},
                    "neighborhood": {"type": "string"},
                    "location": {"type": "string"},
                    "locations": {"type": "array", "items": {"type": "string"}},
                    "neighborhoods": {"type": "array", "items": {"type": "string"}},
                    "property_type": {"type": "string"},
                    "property_types": {"type": "array", "items": {"type": "string"}},
                    "property_interest": {"type": "string"},
                }
            ),
        },
        {
            "name": "create_follow_up",
            "description": (
                "Schedule a follow-up on a lead now. Use when the agent asks to follow up "
                "on a specific date or if someone hasn't responded."
            ),
            "input_schema": _obj(
                {
                    "lead_id": {"type": "integer"},
                    "lead_name": {"type": "string"},
                    "due_date": {"type": "string"},
                    "due_time": {"type": "string"},
                    "reason": {"type": "string"},
                    "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]},
                }
            ),
        },
        {
            "name": "update_lead_status",
            "description": (
                "Advance a lead to a routine CRM status such as contacted, qualified, "
                "appointment_scheduled, or nurture. Never use for closed, lost, or do_not_contact."
            ),
            "input_schema": _obj(
                {
                    "lead_id": {"type": "integer"},
                    "lead_name": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": [
                            "new",
                            "attempting_contact",
                            "contacted",
                            "qualified",
                            "appointment_scheduled",
                            "nurture",
                        ],
                    },
                }
            ),
        },
        {
            "name": "get_calendar_availability",
            "description": "Inspect the agent's TopAI calendar (appointments) for a date or window.",
            "input_schema": _obj(
                {
                    "date": {"type": "string", "description": "YYYY-MM-DD in the agent's timezone."},
                    "start_at": {"type": "string"},
                    "end_at": {"type": "string"},
                }
            ),
        },
        {
            "name": "find_available_slots",
            "description": (
                "Find open appointment slots on the agent's calendar using account "
                "duration, business hours, buffer, and minimum notice."
            ),
            "input_schema": _obj(
                {
                    "after": {"type": "string", "description": "ISO start of search window."},
                    "before": {"type": "string", "description": "ISO end of search window."},
                    "duration_minutes": {"type": "integer"},
                }
            ),
        },
        {
            "name": "get_existing_appointment",
            "description": "Load the lead's upcoming TopAI appointment, if any.",
            "input_schema": _obj(
                {
                    "lead_id": {"type": "integer"},
                    "lead_name": {"type": "string"},
                    "appointment_id": {"type": "integer"},
                }
            ),
        },
        {
            "name": "create_calendar_event",
            "description": (
                "Schedule an appointment on the agent's TopAI calendar now. Inspect "
                "availability first. Do not book over an existing event."
            ),
            "input_schema": _obj(
                {
                    "lead_id": {"type": "integer"},
                    "lead_name": {"type": "string"},
                    "start_at": {"type": "string", "description": "ISO-8601 start time."},
                    "end_at": {"type": "string"},
                    "appointment_type": {"type": "string"},
                    "location": {"type": "string"},
                    "notes": {"type": "string"},
                }
            ),
        },
        {
            "name": "reschedule_calendar_event",
            "description": (
                "Move an existing appointment to a new time. Updates the current event; "
                "does not create a duplicate."
            ),
            "input_schema": _obj(
                {
                    "appointment_id": {"type": "integer"},
                    "lead_id": {"type": "integer"},
                    "lead_name": {"type": "string"},
                    "start_at": {"type": "string"},
                    "end_at": {"type": "string"},
                }
            ),
        },
        {
            "name": "ask_clarification",
            "description": (
                "Ask the agent a follow-up question when required information is missing "
                "or several leads could match. Do not guess."
            ),
            "input_schema": _obj(
                {
                    "question": {"type": "string"},
                    "choices": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": ["integer", "string"]},
                                "label": {"type": "string"},
                            },
                            "additionalProperties": False,
                        },
                    },
                },
                required=["question"],
            ),
        },
        {
            "name": "inform_user",
            "description": (
                "Give a natural-language reply that is not a CRM mutation: an answer, "
                "or a refusal for a capability that is not yet authorized (email, SMS send, calling)."
            ),
            "input_schema": _obj(
                {
                    "kind": {
                        "type": "string",
                        "enum": ["informational", "unsupported"],
                    },
                    "message": {"type": "string"},
                },
                required=["kind", "message"],
            ),
        },
    ]


def openai_tools() -> list[dict]:
    """OpenAI Realtime/Responses function tools for enabled CRM tools only."""
    tools = []
    for item in anthropic_tools():
        name = item.get("name")
        if name in CONTROL_TOOLS:
            continue
        tools.append(
            {
                "type": "function",
                "name": name,
                "description": item.get("description") or "",
                "parameters": item.get("input_schema") or {"type": "object", "properties": {}},
            }
        )
    return tools


def is_write_tool(name: str) -> bool:
    return name in WRITE_TOOLS


def is_read_tool(name: str) -> bool:
    return name in READ_TOOLS


def is_enabled(name: str) -> bool:
    return name in ENABLED_TOOLS


def is_future_tool(name: str) -> bool:
    return name in FUTURE_TOOLS
