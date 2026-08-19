"""Realtime voice provider/configuration. Swap models here, not in CRM tools."""

from __future__ import annotations

import copy

import config

PROVIDER_NAME = "openai_realtime"
DEFAULT_MODEL = "gpt-realtime-2.1"
DEFAULT_VOICE = "marin"
CALLS_URL = "https://api.openai.com/v1/realtime/calls"
WEBRTC_PATH = "/api/ask-topai/live/webrtc"
ICE_SERVERS = [{"urls": "stun:stun.l.google.com:19302"}]


def provider_name() -> str:
    return PROVIDER_NAME


def realtime_model() -> str:
    return (getattr(config, "ASK_TOPAI_REALTIME_MODEL", None) or "").strip() or DEFAULT_MODEL


def realtime_voice() -> str:
    return (getattr(config, "ASK_TOPAI_REALTIME_VOICE", None) or "").strip() or DEFAULT_VOICE


def openai_api_key() -> str:
    return (getattr(config, "OPENAI_API_KEY", None) or "").strip()


def is_configured() -> bool:
    key = openai_api_key()
    return bool(key) and not key.startswith("test-")


def key_present() -> bool:
    return bool(openai_api_key())


def session_config(instructions: str, tools: list) -> dict:
    """OpenAI Realtime session object. Isolated so a GPT-Live adapter can differ."""
    return {
        "type": "realtime",
        "model": realtime_model(),
        "instructions": instructions,
        "tool_choice": "auto",
        "tools": tools,
        "audio": {
            "input": {
                "turn_detection": {
                    "type": "semantic_vad",
                    "create_response": True,
                    "interrupt_response": True,
                },
                "transcription": {"model": "gpt-4o-mini-transcribe"},
            },
            "output": {"voice": realtime_voice()},
        },
    }


def session_config_without_transcription(instructions: str, tools: list) -> dict:
    cfg = copy.deepcopy(session_config(instructions, tools))
    cfg.get("audio", {}).get("input", {}).pop("transcription", None)
    return cfg


def slim_session_config() -> dict:
    """Minimal session object for diagnostics. No CRM tools. No user instructions."""
    return {
        "type": "realtime",
        "model": realtime_model(),
        "audio": {"output": {"voice": realtime_voice()}},
    }


def public_client_config() -> dict:
    """Safe values for the browser. Never includes OPENAI_API_KEY or OpenAI URLs."""
    return {
        "provider": provider_name(),
        "model": realtime_model(),
        "webrtc_url": WEBRTC_PATH,
        "ice_servers": ICE_SERVERS,
    }
