"""Ask TopAI action confirmation policy.

CRM tools stay model-independent. This module decides whether a tool may run
from clear spoken/typed intent, or must wait for an extra confirmation turn.
"""

from __future__ import annotations

from ask_topai import registry

# Low-risk internal CRM writes: clear intent is enough during Live Conversation
# and on typed Send. No extra Confirm button.
AUTO_EXECUTE_TOOLS = registry.WRITE_TOOLS

# Higher-impact tools are not enabled yet. When they are registered, the first
# request must only propose the action; a second explicit confirmation
# ("Yes, send them") is required before execute.
SPOKEN_CONFIRMATION_TOOLS = frozenset(
    {
        "send_email",
        "draft_email",
        "send_sms",
        "draft_sms",
        "send_listings",
        "schedule_appointment",
        "initiate_ai_call",
        "place_call",
        "start_call",
        "delete_lead",
        "delete_records",
        "bulk_action",
        "change_consent",
        "change_sms_qualification",
    }
)

MODE_AUTO = "auto"
MODE_SPOKEN_CONFIRMATION = "spoken_confirmation"
MODE_FORBIDDEN = "forbidden"


def confirmation_mode(tool_name: str) -> str:
    name = (tool_name or "").strip()
    if registry.is_read_tool(name) or name in AUTO_EXECUTE_TOOLS:
        return MODE_AUTO
    if name in SPOKEN_CONFIRMATION_TOOLS or registry.is_future_tool(name):
        return MODE_SPOKEN_CONFIRMATION
    return MODE_FORBIDDEN


def requires_spoken_confirmation(tool_name: str) -> bool:
    return confirmation_mode(tool_name) == MODE_SPOKEN_CONFIRMATION


def is_forbidden(tool_name: str) -> bool:
    return confirmation_mode(tool_name) == MODE_FORBIDDEN
