"""Ask TopAI action policy.

Routine CRM/calendar tools auto-execute from clear spoken/typed intent.
Destructive, financial, and outbound-message tools stay blocked.
"""

from __future__ import annotations

import autonomy
from ask_topai import registry

AUTO_EXECUTE_TOOLS = registry.WRITE_TOOLS

SPOKEN_CONFIRMATION_TOOLS = autonomy.BLOCK_TOOLS

MODE_AUTO = "auto"
MODE_SPOKEN_CONFIRMATION = "spoken_confirmation"
MODE_FORBIDDEN = "forbidden"


def confirmation_mode(tool_name: str) -> str:
    name = (tool_name or "").strip()
    mode = autonomy.tool_mode(name)
    if mode == autonomy.MODE_AUTO:
        return MODE_AUTO
    if name in SPOKEN_CONFIRMATION_TOOLS or registry.is_future_tool(name):
        return MODE_SPOKEN_CONFIRMATION
    return MODE_FORBIDDEN


def requires_spoken_confirmation(tool_name: str) -> bool:
    return confirmation_mode(tool_name) == MODE_SPOKEN_CONFIRMATION


def is_forbidden(tool_name: str) -> bool:
    return confirmation_mode(tool_name) == MODE_FORBIDDEN
