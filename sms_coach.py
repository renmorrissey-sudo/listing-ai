"""Inbound SMS coaching via Anthropic Claude. Never logs or returns API keys."""

import json
import re

from anthropic import Anthropic

import config

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
            max_tokens=900,
            temperature=0.3,
            system=(
                "You are a real estate SMS coach for agents. "
                "Return ONLY valid JSON with the requested keys. No markdown."
            ),
            messages=[{"role": "user", "content": prompt_text}],
        )
    except Exception as exc:
        raise SmsCoachError("Claude analysis request failed.") from exc

    try:
        content = message.content[0].text
    except (IndexError, AttributeError, TypeError) as exc:
        raise SmsCoachError("Claude returned an unexpected response.") from exc

    return _parse_analysis_json(content)


def _parse_analysis_json(content):
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

    allowed_status = {"new", "contacted", "replied", "nurture", "hot", "closed", "do_not_contact"}
    status = str(data.get("lead_status") or "replied").strip().lower()
    if status not in allowed_status:
        status = "replied"

    try:
        follow_up_days = int(data.get("follow_up_days") or 30)
    except (TypeError, ValueError):
        follow_up_days = 30
    follow_up_days = max(1, min(180, follow_up_days))

    try:
        confidence = float(data.get("confidence_score") or 0.5)
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

    requires_manual = bool(data.get("requires_manual_review")) or bool(topics) or confidence < 0.55
    if status == "do_not_contact":
        requires_manual = True

    suggested = str(data.get("suggested_reply") or "").strip()[:480]
    pitch = str(data.get("home_value_pitch") or "").strip()[:480] or None

    return {
        "summary": str(data.get("summary") or "").strip()[:800],
        "intent": str(data.get("intent") or data.get("inferred_intent") or "").strip()[:400],
        "next_best_step": str(data.get("next_best_step") or "").strip()[:500],
        "recommended_action": str(data.get("recommended_action") or "").strip()[:500],
        "suggested_reply": suggested,
        "home_value_pitch": pitch,
        "confidence_score": confidence,
        "requires_manual_review": requires_manual,
        "escalation_topics": topics,
        "lead_status": status,
        "follow_up_days": follow_up_days,
        "raw_json": json.dumps({
            "summary": data.get("summary"),
            "intent": data.get("intent") or data.get("inferred_intent"),
            "recommended_action": data.get("recommended_action"),
            "confidence_score": confidence,
            "escalation_topics": topics,
            "requires_manual_review": requires_manual,
            "lead_status": status,
            "follow_up_days": follow_up_days,
        })[:4000],
    }
