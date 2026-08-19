"""Realtime voice conversation for Ask TopAI.

Audio travels browser ↔ OpenAI over WebRTC. The browser posts its SDP offer
to TopAI; Flask forwards it to POST /v1/realtime/calls with the server-side
OPENAI_API_KEY and returns the SDP answer. CRM tools stay model-independent —
change ASK_TOPAI_REALTIME_MODEL without redesigning tools or the UI.
"""

from ask_topai.realtime import settings

__all__ = ["settings"]
