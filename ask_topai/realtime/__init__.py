"""Realtime voice conversation for Ask TopAI.

The browser uses the OpenAI Agents SDK (RealtimeAgent / RealtimeSession) over
WebRTC. Flask mints a short-lived client secret with the server-side
OPENAI_API_KEY via POST /v1/realtime/client_secrets and executes CRM tools.
"""

from ask_topai.realtime import settings

__all__ = ["settings"]
