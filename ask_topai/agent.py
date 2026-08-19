"""Claude tool-use loop for Ask TopAI. Mutations are queued, never executed here."""

from __future__ import annotations

import json
import logging

from anthropic import APIStatusError, APITimeoutError, Anthropic, RateLimitError

import config
from ask_topai import registry, sessions, tools

logger = logging.getLogger(__name__)

MAX_ROUNDS = 6
MAX_TOKENS = 1800

SYSTEM_PROMPT = """You are Ask TopAI, the intelligent CRM assistant for a real-estate agent.
You reason about what the agent wants, look up tenant-scoped CRM data with read tools, and queue write tools for confirmation.

Rules:
- Never invent phone numbers, emails, lead IDs, prices, or property details.
- Never guess among multiple matching leads. Call ask_clarification.
- Never claim a write already happened. Writes run only after the agent clicks Confirm.
- Use selected_lead_id from context when the agent says she/he/them/this lead and no other person is named.
- You may queue several write tools in one turn when the request clearly needs multiple CRM actions.
- If a required field is missing (especially a lead phone for create_lead), call ask_clarification.
- If the agent asks to send email, SMS, listings, place a call, delete data, change consent, or run SQL, call inform_user with kind=unsupported. Explain the intent was understood but that permission is not enabled yet.
- Do not call tools that are not provided to you.
- Do not output JSON to the user. Use tools.

Future capabilities that are NOT available yet: find_matching_listings, create_cma, draft_email, send_email, draft_sms, send_sms, schedule_appointment, initiate_ai_call, create_follow_up, generate_listing_content.
"""


class AskTopAIModelError(RuntimeError):
    """Claude is unavailable or returned an unusable response."""


def is_configured() -> bool:
    return bool((config.ANTHROPIC_API_KEY or "").strip()) and not str(
        config.ANTHROPIC_API_KEY
    ).startswith("test-")


def model_name() -> str:
    return (config.ASK_TOPAI_MODEL or "").strip() or "claude-sonnet-5"


def call_claude(messages: list, *, system: str, tools_spec: list):
    if not is_configured():
        raise AskTopAIModelError("Ask TopAI is not configured.")
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return client.messages.create(
        model=model_name(),
        max_tokens=MAX_TOKENS,
        temperature=0,
        system=system,
        tools=tools_spec,
        messages=messages,
    )


def _block_type(block) -> str:
    return str(getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else "") or "")


def _tool_use_payload(block) -> dict:
    if isinstance(block, dict):
        return {
            "id": block.get("id"),
            "name": block.get("name"),
            "input": block.get("input") if isinstance(block.get("input"), dict) else {},
        }
    return {
        "id": getattr(block, "id", None),
        "name": getattr(block, "name", None),
        "input": getattr(block, "input", None) if isinstance(getattr(block, "input", None), dict) else {},
    }


def _text_from_blocks(content) -> str:
    parts = []
    for block in content or []:
        if _block_type(block) == "text":
            if isinstance(block, dict):
                parts.append(str(block.get("text") or ""))
            else:
                parts.append(str(getattr(block, "text", "") or ""))
    return "\n".join(p for p in parts if p).strip()


def _assistant_content(content) -> list:
    serialized = []
    for block in content or []:
        btype = _block_type(block)
        if btype == "text":
            text = block.get("text") if isinstance(block, dict) else getattr(block, "text", "")
            serialized.append({"type": "text", "text": text or ""})
        elif btype == "tool_use":
            payload = _tool_use_payload(block)
            serialized.append(
                {
                    "type": "tool_use",
                    "id": payload["id"],
                    "name": payload["name"],
                    "input": payload["input"] or {},
                }
            )
    return serialized


def _user_context_text(transcript: str, context: dict) -> str:
    bits = []
    if context.get("lead_id"):
        bits.append(f"selected_lead_id={context['lead_id']}")
    if context.get("lead_name"):
        bits.append(f"selected_lead_name={context['lead_name']}")
    if context.get("page"):
        bits.append(f"page={context['page']}")
    ctx = "; ".join(bits) if bits else "none"
    return (
        f"Agent request: {transcript}\n"
        f"Safe UI context: {ctx}\n"
        "Use selected_lead_id for she/he/them/this lead when no other person is named."
    )


def complete(user_id, transcript, context, *, session_id=None, source="text"):
    """Run Claude with tools. Returns a dict consumed by service.interpret."""
    text = (transcript or "").strip()
    context = context or {}
    source = source if source in {"voice", "text"} else "text"
    history = sessions.load_messages(user_id, session_id)
    combined = sessions.conversation_transcript(history)
    if text:
        combined = f"{combined}\n{text}".strip() if combined else text

    if not text:
        return {
            "status": "needs_clarification",
            "message": "Please say or type what you would like Ask TopAI to do.",
            "commands": [],
            "tools_invoked": [],
            "model": model_name(),
            "session_id": session_id,
            "source": source,
            "grounding_transcript": "",
        }

    messages = list(history)
    messages.append({"role": "user", "content": _user_context_text(text, context)})

    proposed = []
    clarification = None
    inform = None
    invoked = []
    unknown = []
    last_text = ""

    try:
        for _round in range(MAX_ROUNDS):
            response = call_claude(
                messages,
                system=SYSTEM_PROMPT,
                tools_spec=registry.anthropic_tools(),
            )
            content = list(getattr(response, "content", None) or [])
            last_text = _text_from_blocks(content)
            assistant = _assistant_content(content)
            if assistant:
                messages.append({"role": "assistant", "content": assistant})
            stop = getattr(response, "stop_reason", None)
            tool_results = []
            for block in content:
                if _block_type(block) != "tool_use":
                    continue
                payload = _tool_use_payload(block)
                name = str(payload.get("name") or "").strip()
                arguments = payload.get("input") or {}
                tool_id = payload.get("id") or "tool"
                invoked.append(name)
                if not registry.is_enabled(name):
                    unknown.append(name)
                    result = {"error": "That tool is not available."}
                    if registry.is_future_tool(name) or name in {
                        "send_email",
                        "send_sms",
                        "draft_email",
                        "draft_sms",
                    }:
                        inform = {
                            "kind": "unsupported",
                            "message": (
                                "I understand that request, but Ask TopAI doesn't have "
                                "permission to do that yet."
                            ),
                        }
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": json.dumps(result)[:2500],
                        }
                    )
                    continue
                result = _dispatch_tool(
                    name,
                    arguments,
                    user_id=user_id,
                    context=context,
                    transcript=combined,
                    proposed=proposed,
                )
                if name == "ask_clarification":
                    clarification = arguments
                elif name == "inform_user":
                    inform = arguments
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": json.dumps(result)[:2500],
                    }
                )
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
                if stop != "end_turn":
                    continue
            break
    except AskTopAIModelError as exc:
        return _error_result(str(exc), session_id, source, invoked)
    except RateLimitError:
        logger.warning("Ask TopAI Claude rate limited")
        return _error_result(
            "Ask TopAI is busy right now. Please try again in a moment.",
            session_id,
            source,
            invoked,
        )
    except APITimeoutError:
        logger.warning("Ask TopAI Claude timeout")
        return _error_result(
            "Ask TopAI timed out before it could finish. Your request was not changed.",
            session_id,
            source,
            invoked,
        )
    except APIStatusError as exc:
        logger.warning("Ask TopAI Claude HTTP error: %s", getattr(exc, "status_code", None))
        return _error_result(
            "Ask TopAI could not reach Claude. Your CRM data was not changed.",
            session_id,
            source,
            invoked,
        )
    except Exception:
        logger.exception("Ask TopAI Claude failure")
        return _error_result(
            "Ask TopAI had a problem understanding that request. Your CRM data was not changed.",
            session_id,
            source,
            invoked,
        )

    result = _finalize(proposed, clarification, inform, last_text, unknown)
    result["tools_invoked"] = invoked
    result["model"] = model_name()
    result["source"] = source
    result["grounding_transcript"] = combined
    persist_status = "clarifying" if result["status"] == "needs_clarification" else "active"
    pending = {"commands": result.get("commands") or []} if result.get("commands") else None
    result["session_id"] = sessions.save_session(
        user_id,
        session_id or sessions.create_session_key(),
        messages,
        pending=pending,
        status=persist_status,
    )
    return result


def _dispatch_tool(name, arguments, *, user_id, context, transcript, proposed):
    if not registry.is_enabled(name):
        return {"error": "That tool is not available."}
    if registry.is_read_tool(name):
        return tools.dispatch_read(name, arguments, user_id, context)
    if registry.is_write_tool(name):
        queued = tools.queue_write_tool(name, arguments, transcript)
        if queued.get("queued"):
            proposed.append({"action": name, "arguments": queued["arguments"]})
        return {
            "queued": queued.get("queued"),
            "error": queued.get("error"),
            "preview": queued.get("preview"),
        }
    if name == "ask_clarification":
        question = str((arguments or {}).get("question") or "").strip()
        return {"ok": True, "question": question}
    if name == "inform_user":
        return {"ok": True}
    return {"error": "That tool is not available."}


def _finalize(proposed, clarification, inform, last_text, unknown=None):
    unknown = unknown or []
    if clarification and str(clarification.get("question") or "").strip():
        choices = clarification.get("choices") if isinstance(clarification.get("choices"), list) else []
        return {
            "status": "needs_clarification",
            "message": str(clarification.get("question")).strip(),
            "commands": [],
            "choices": choices,
        }
    if proposed:
        return {
            "status": "ok",
            "message": last_text or "Confirm this action plan before I change any CRM data.",
            "commands": proposed,
            "choices": [],
        }
    if unknown and not inform:
        return {
            "status": "unsupported",
            "message": (
                "I understand that request, but Ask TopAI doesn't have permission "
                "to do that yet."
            ),
            "commands": [],
            "choices": [],
        }
    if inform:
        kind = str(inform.get("kind") or "informational").strip().lower()
        message = str(inform.get("message") or "").strip()
        if kind == "unsupported":
            return {
                "status": "unsupported",
                "message": message or "Ask TopAI cannot do that yet.",
                "commands": [],
                "choices": [],
            }
        return {
            "status": "informational",
            "message": message or last_text or "I understood, and no CRM change is needed.",
            "commands": [],
            "choices": [],
        }
    if last_text:
        status = "needs_clarification" if "?" in last_text else "informational"
        if status == "informational" and _looks_unsupported(last_text):
            status = "unsupported"
        return {"status": status, "message": last_text, "commands": [], "choices": []}
    return {
        "status": "needs_clarification",
        "message": "I need a bit more information.",
        "commands": [],
        "choices": [],
    }


def _looks_unsupported(text: str) -> bool:
    lowered = (text or "").lower()
    return "doesn't have" in lowered or "does not have" in lowered or "not yet" in lowered


def _error_result(message, session_id, source, invoked):
    return {
        "status": "error",
        "message": message,
        "commands": [],
        "tools_invoked": invoked or [],
        "model": model_name(),
        "session_id": session_id,
        "source": source,
        "choices": [],
        "grounding_transcript": "",
    }
