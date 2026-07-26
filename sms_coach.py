"""Inbound SMS coaching via Anthropic Claude. Never logs or returns API keys."""

import json
import re
from datetime import datetime, timedelta, timezone

from anthropic import Anthropic

import config
from crm_constants import CONFIDENCE_THRESHOLD, LEAD_STATUS_SET, normalize_lead_status

ESCALATION_TOPICS = {
    "legal",
    "financing",
    "negotiation",
    "fair_housing",
    "complaint",
    "uncertain_property_fact",
}


class SmsCoachError(Exception):
    pass


def is_configured():
    return bool((config.ANTHROPIC_API_KEY or "").strip())


def analyze_inbound_reply(prompt_text):
    """Use Claude to analyze an inbound lead SMS. API key stays server-side only."""
    if not is_configured():
        raise SmsCoachError("Claude analysis is not configured.")

    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        message = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1200,
            temperature=0.3,
            system=(
                "You are a real estate SMS coach for agents. "
                "Return ONLY valid JSON with the requested keys. No markdown. "
                "Never claim to send messages, change Do Not Contact, confirm appointments, "
                "or make legal/financing/property claims."
            ),
            messages=[{"role": "user", "content": prompt_text}],
        )
    except Exception as exc:
        raise SmsCoachError("Claude analysis request failed.") from exc

    try:
        content = message.content[0].text
    except (IndexError, AttributeError, TypeError) as exc:
        raise SmsCoachError("Claude returned an unexpected response.") from exc

    return validate_coach_response(content)


def validate_coach_response(content):
    """Server-side schema validation. Do not trust model output blindly."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise SmsCoachError("Claude did not return JSON.")
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise SmsCoachError("Claude JSON must be an object.")

    confidence_raw = data.get("confidence", data.get("confidence_score", 0.5))
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    raw_topics = data.get("escalation_topics") or []
    if isinstance(raw_topics, str):
        raw_topics = [raw_topics]
    topics = []
    for topic in raw_topics:
        cleaned = str(topic or "").strip().lower().replace(" ", "_").replace("-", "_")
        if cleaned in ESCALATION_TOPICS and cleaned not in topics:
            topics.append(cleaned)

    sensitive = bool(data.get("sensitive_topic")) or bool(topics)
    requires_manual = (
        bool(data.get("requires_manual_review"))
        or sensitive
        or confidence < CONFIDENCE_THRESHOLD
    )

    suggested_status = data.get("suggested_lead_status") or data.get("lead_status") or ""
    suggested_status = str(suggested_status).strip().lower().replace(" ", "_")
    if suggested_status and suggested_status not in LEAD_STATUS_SET:
        suggested_status = normalize_lead_status(suggested_status)
    # Model may recommend DNC but automation must never apply it; keep as suggestion only.
    if suggested_status not in LEAD_STATUS_SET:
        suggested_status = ""

    follow_up_at = data.get("suggested_follow_up_at")
    if follow_up_at in ("", "null", None):
        follow_up_at = None
    elif isinstance(follow_up_at, str):
        follow_up_at = follow_up_at.strip()[:40] or None
    else:
        follow_up_at = None

    if not follow_up_at:
        try:
            days = int(data.get("follow_up_days") or 0)
        except (TypeError, ValueError):
            days = 0
        if 1 <= days <= 180:
            follow_up_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

    suggested_tasks = data.get("suggested_tasks") or []
    if not isinstance(suggested_tasks, list):
        suggested_tasks = []
    clean_tasks = []
    for item in suggested_tasks[:5]:
        if isinstance(item, str) and item.strip():
            clean_tasks.append({"title": item.strip()[:200], "task_type": "general_follow_up"})
        elif isinstance(item, dict) and str(item.get("title") or "").strip():
            clean_tasks.append({
                "title": str(item.get("title")).strip()[:200],
                "task_type": str(item.get("task_type") or "general_follow_up")[:60],
                "due_at": item.get("due_at"),
            })

    appointment_details = data.get("appointment_details")
    if appointment_details is not None and not isinstance(appointment_details, dict):
        appointment_details = None

    reasons = data.get("needs_attention_reasons") or []
    if not isinstance(reasons, list):
        reasons = []
    reasons = [str(r).strip().lower().replace(" ", "_")[:80] for r in reasons if str(r).strip()][:10]

    draft = str(data.get("draft_reply") or data.get("suggested_reply") or "").strip()[:480]
    recommended = str(
        data.get("recommended_next_action")
        or data.get("recommended_action")
        or data.get("next_best_step")
        or ""
    ).strip()[:500]

    return {
        # Phase 2 canonical fields
        "summary": str(data.get("summary") or "").strip()[:800],
        "intent": str(data.get("intent") or "").strip()[:400],
        "recommended_next_action": recommended,
        "draft_reply": draft,
        "confidence": confidence,
        "sensitive_topic": sensitive,
        "suggested_lead_status": suggested_status,
        "suggested_follow_up_at": follow_up_at,
        "suggested_follow_up_reason": str(data.get("suggested_follow_up_reason") or "").strip()[:500],
        "suggested_tasks": clean_tasks,
        "appointment_requested": bool(data.get("appointment_requested")),
        "appointment_details": appointment_details,
        "needs_attention_reasons": reasons,
        # Backward-compatible aliases used by Phase 1 persistence
        "next_best_step": recommended,
        "recommended_action": recommended,
        "suggested_reply": draft,
        "home_value_pitch": str(data.get("home_value_pitch") or "").strip()[:480] or None,
        "confidence_score": confidence,
        "requires_manual_review": requires_manual,
        "escalation_topics": topics,
        "lead_status": suggested_status or "contacted",
        "follow_up_days": 30,
        "raw_json": json.dumps({
            "summary": data.get("summary"),
            "intent": data.get("intent"),
            "recommended_next_action": recommended,
            "draft_reply": draft,
            "confidence": confidence,
            "sensitive_topic": sensitive,
            "suggested_lead_status": suggested_status,
            "suggested_follow_up_at": follow_up_at,
            "suggested_follow_up_reason": str(data.get("suggested_follow_up_reason") or "").strip()[:500],
            "suggested_tasks": clean_tasks,
            "appointment_requested": bool(data.get("appointment_requested")),
            "appointment_details": appointment_details,
            "needs_attention_reasons": reasons,
            "home_value_pitch": str(data.get("home_value_pitch") or "").strip()[:480] or None,
        })[:4000],
    }
