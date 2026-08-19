"""Realtime voice conversation for Ask TopAI.

Audio travels browser ↔ OpenAI over WebRTC. Flask only mints ephemeral
credentials and executes validated CRM tools. The tool layer does not depend
on a specific realtime model — change ASK_TOPAI_REALTIME_MODEL (or a future
GPT-Live adapter in settings.py) without redesigning CRM tools or the UI.
"""

from ask_topai.realtime import settings

__all__ = ["settings"]
