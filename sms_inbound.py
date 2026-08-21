"""Inbound Twilio SMS processing: match tenant/lead, store, coach (never auto-send)."""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone

import config
import crm_db
import crm_time
import db
import sms_coach
from lead_service import SMS_SOURCE, upsert_crm_lead
from sms_prompts import build_inbound_reply_analysis_prompt
from sms_validation import validate_e164_phone

logger = logging.getLogger(__name__)

OPT_OUT_KEYWORDS = frozenset(
    {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
)
OPT_IN_KEYWORDS = frozenset({"start", "unstop", "yes"})
HELP_KEYWORDS = frozenset({"help", "info"})


def classify_compliance_keyword(body: str) -> str | None:
    """Return 'opt_out' | 'opt_in' | 'help' | None for a whole-message keyword."""
    text = re.sub(r"[^a-z\s]", " ", (body or "").strip().lower())
    tokens = [t for t in text.split() if t]
    if not tokens:
        return None
    # Twilio Advanced Opt-Out typically matches the whole body as a single keyword.
    joined = " ".join(tokens)
    if joined in OPT_OUT_KEYWORDS or (len(tokens) == 1 and tokens[0] in OPT_OUT_KEYWORDS):
        return "opt_out"
    if joined in OPT_IN_KEYWORDS or (len(tokens) == 1 and tokens[0] in OPT_IN_KEYWORDS):
        return "opt_in"
    if joined in HELP_KEYWORDS or (len(tokens) == 1 and tokens[0] in HELP_KEYWORDS):
        return "help"
    return None


def configured_destination_numbers():
    """E.164 numbers this deployment accepts as Twilio To: values."""
    numbers = set()
    primary = (config.TWILIO_PHONE_NUMBER or "").strip()
    if primary:
        cleaned, err = validate_e164_phone(primary)
        if not err and cleaned:
            numbers.add(cleaned)
    return numbers


def destination_allowed(to_number: str, messaging_service_sid: str | None = None) -> bool:
    allowed = configured_destination_numbers()
    if to_number and to_number in allowed:
        return True
    configured_msid = (config.TWILIO_MESSAGING_SERVICE_SID or "").strip()
    if (
        messaging_service_sid
        and configured_msid
        and messaging_service_sid.strip() == configured_msid
        and to_number
        and allowed
        and to_number in allowed
    ):
        return True
    # Messaging Service inbound still sets To to the long code; require phone match.
    return False


def extract_media(form) -> tuple[int, list[str]]:
    try:
        num_media = int(form.get("NumMedia") or 0)
    except (TypeError, ValueError):
        num_media = 0
    urls = []
    for i in range(max(0, min(num_media, 10))):
        url = str(form.get(f"MediaUrl{i}") or "").strip()
        if url:
            urls.append(url[:2000])
    return num_media, urls


def parse_inbound_form(form) -> dict:
    from_number, from_error = validate_e164_phone(form.get("From"))
    to_number, to_error = validate_e164_phone(form.get("To"))
    body = str(form.get("Body") or "").strip()[:1500]
    message_sid = str(form.get("MessageSid") or "").strip() or None
    messaging_service_sid = str(form.get("MessagingServiceSid") or "").strip() or None
    num_media, media_urls = extract_media(form)
    sms_status = str(form.get("SmsStatus") or form.get("MessageStatus") or "").strip() or None
    return {
        "from_number": from_number,
        "from_error": from_error,
        "to_number": to_number,
        "to_error": to_error,
        "body": body,
        "message_sid": message_sid,
        "messaging_service_sid": messaging_service_sid,
        "num_media": num_media,
        "media_urls": media_urls,
        "sms_status": sms_status,
    }


def process_inbound_sms(payload: dict, *, defer_coach: bool = True, app=None) -> dict:
    """
    Persist inbound SMS and enqueue autonomous AI analysis/reply.
    Opt-out and compliance still block sending. Returns a small result dict.
    """
    result = {
        "ok": False,
        "duplicate": False,
        "ignored": None,
        "owner_id": None,
        "lead_id": None,
        "inbound_id": None,
        "keyword": None,
    }

    if payload.get("from_error") or payload.get("to_error"):
        result["ignored"] = "invalid_phone"
        logger.info(
            "Inbound SMS ignored: invalid_phone sid=%s",
            payload.get("message_sid"),
        )
        return result

    from_number = payload["from_number"]
    to_number = payload["to_number"]
    body = payload.get("body") or ""
    num_media = int(payload.get("num_media") or 0)
    message_sid = payload.get("message_sid")

    if not body and num_media <= 0:
        result["ignored"] = "empty_body"
        logger.info("Inbound SMS ignored: empty_body sid=%s", message_sid)
        return result

    if not destination_allowed(to_number, payload.get("messaging_service_sid")):
        result["ignored"] = "unknown_destination"
        logger.warning(
            "Inbound SMS ignored: unknown_destination sid=%s to_match=%s",
            message_sid,
            bool(to_number),
        )
        return result

    if message_sid:
        existing = db.get_sms_message_by_provider_id(message_sid)
        if existing:
            result["ok"] = True
            result["duplicate"] = True
            result["owner_id"] = existing.get("user_id")
            result["lead_id"] = existing.get("lead_id")
            result["inbound_id"] = existing.get("id")
            logger.info(
                "Inbound SMS duplicate sid=%s inbound_id=%s",
                message_sid,
                existing.get("id"),
            )
            return result

    owner_id = db.find_sms_user_by_phone(from_number)
    if not owner_id:
        result["ignored"] = "unknown_sender"
        logger.info("Inbound SMS ignored: unknown_sender sid=%s", message_sid)
        return result

    seed = db.last_outbound_seed_for_phone(owner_id, from_number) or {}
    lead_id, _created, lead = upsert_crm_lead(
        owner_id, from_number, seed, source=SMS_SOURCE, touch_sms=False
    )
    lead = lead or db.get_lead(lead_id, owner_id)
    keyword = classify_compliance_keyword(body)
    opted_out = keyword == "opt_out"

    store_body = body if body else "[media message]"
    inbound_id, duplicate = db.create_inbound_sms_message(
        user_id=owner_id,
        phone_number=from_number,
        message_body=store_body,
        provider_message_id=message_sid,
        lead_id=lead_id,
        lead_name=(lead or {}).get("name"),
        opt_out_status="opted_out" if opted_out else ((lead or {}).get("opt_out_status") or "active"),
        to_number=to_number,
        media_urls=payload.get("media_urls") or [],
        num_media=num_media,
        status_meta=payload.get("sms_status"),
    )
    if duplicate:
        result["ok"] = True
        result["duplicate"] = True
        result["owner_id"] = owner_id
        result["lead_id"] = lead_id
        result["inbound_id"] = inbound_id
        return result

    now = datetime.now(timezone.utc).isoformat()
    result.update(
        {
            "ok": True,
            "owner_id": owner_id,
            "lead_id": lead_id,
            "inbound_id": inbound_id,
            "keyword": keyword,
        }
    )

    if keyword == "opt_out":
        import external_leads_db as xdb

        xdb.apply_opt_out_consent(lead_id, owner_id, source="sms_keyword")
        crm_db.add_lead_activity(
            lead_id,
            owner_id,
            "opt_out",
            "Lead opted out via SMS keyword",
            {"message_id": inbound_id, "keyword": "STOP"},
        )
        crm_db.upsert_needs_attention(
            owner_id,
            lead_id,
            "opt_out",
            priority="urgent",
            source_ref_type="sms",
            source_ref_id=inbound_id,
        )
        logger.info(
            "Inbound SMS processed sid=%s tenant=%s lead=%s keyword=opt_out",
            message_sid,
            owner_id,
            lead_id,
        )
    elif keyword == "opt_in":
        db.clear_lead_sms_opt_out(lead_id, owner_id)
        crm_db.add_lead_activity(
            lead_id,
            owner_id,
            "sms_opt_in",
            "Lead requested to resume SMS (START)",
            {"message_id": inbound_id},
        )
        crm_db.upsert_needs_attention(
            owner_id,
            lead_id,
            "unreviewed_inbound",
            priority="normal",
            source_ref_type="sms",
            source_ref_id=inbound_id,
        )
        logger.info(
            "Inbound SMS processed sid=%s tenant=%s lead=%s keyword=opt_in",
            message_sid,
            owner_id,
            lead_id,
        )
    elif keyword == "help":
        # Do not send a HELP reply from TopAI — Twilio Advanced Opt-Out owns that SMS.
        crm_db.add_lead_activity(
            lead_id,
            owner_id,
            "sms_help",
            "Lead requested HELP via SMS",
            {"message_id": inbound_id},
        )
        crm_db.upsert_needs_attention(
            owner_id,
            lead_id,
            "unreviewed_inbound",
            priority="normal",
            source_ref_type="sms",
            source_ref_id=inbound_id,
        )
        logger.info(
            "Inbound SMS processed sid=%s tenant=%s lead=%s keyword=help",
            message_sid,
            owner_id,
            lead_id,
        )
    else:
        db.update_lead_from_analysis(lead_id, owner_id, last_inbound_at=now)
        if (lead or {}).get("opt_out_status") != "opted_out":
            crm_db.set_lead_status(
                owner_id, lead_id, "contacted", from_automation=True
            )
        crm_db.add_lead_activity(
            lead_id,
            owner_id,
            "sms_inbound",
            "Inbound SMS received",
            {
                "message_id": inbound_id,
                "num_media": num_media,
            },
        )
        logger.info(
            "Inbound SMS processed sid=%s tenant=%s lead=%s keyword=none",
            message_sid,
            owner_id,
            lead_id,
        )

    # Claude analysis + autonomous reply are deferred so Twilio gets a fast ack.
    opted_out_flag = keyword == "opt_out"
    if defer_coach:
        _schedule_coach(
            app,
            owner_id,
            lead_id,
            inbound_id,
            store_body,
            opted_out=opted_out_flag,
            receiving_number=to_number,
        )
    elif opted_out_flag:
        analyze_inbound_and_coach(
            owner_id, lead_id, inbound_id, store_body, opted_out=True
        )
    else:
        from sms_ai_agent import process_inbound_ai

        process_inbound_ai(owner_id, lead_id, inbound_id, store_body, to_number)

    return result


def _schedule_coach(app, user_id, lead_id, inbound_id, body, opted_out=False, receiving_number=None):
    def run():
        try:
            if opted_out:
                if app is not None:
                    with app.app_context():
                        analyze_inbound_and_coach(
                            user_id, lead_id, inbound_id, body, opted_out=True
                        )
                else:
                    analyze_inbound_and_coach(
                        user_id, lead_id, inbound_id, body, opted_out=True
                    )
                return
            from sms_ai_agent import process_inbound_ai, schedule_inbound_ai

            if app is not None:
                schedule_inbound_ai(app, user_id, lead_id, inbound_id, body, receiving_number)
            else:
                process_inbound_ai(user_id, lead_id, inbound_id, body, receiving_number)
        except Exception:
            logger.exception(
                "Deferred inbound AI failed inbound_id=%s lead_id=%s",
                inbound_id,
                lead_id,
            )

    threading.Thread(target=run, name=f"sms-ai-{inbound_id}", daemon=True).start()


def _ensure_sms_follow_up(user_id, lead_id, analysis):
    """Persist a real lead_follow_ups record when Claude determined a concrete
    future action, mirroring the voice-call path (lead_service.ensure_lead_follow_through).

    Without this, `leads.next_action` is only freeform text with no scheduling
    record behind it, so it never shows up on /crm/follow-ups, the Leads
    Calendar's follow-up bucket, or Needs Attention overdue checks.
    """
    follow_up_at = analysis.get("suggested_follow_up_at")
    due = crm_time.parse_iso_dt(follow_up_at)
    if not due:
        return None
    due_at = crm_time.to_utc_iso(due)
    reason = (
        analysis.get("suggested_follow_up_reason")
        or analysis.get("recommended_next_action")
        or analysis.get("recommended_action")
        or "Follow up with lead"
    )
    priority = "high" if analysis.get("appointment_requested") else "normal"
    result, error = crm_db.set_lead_follow_up(
        user_id,
        lead_id,
        due_at,
        reason,
        priority=priority,
    )
    if error:
        logger.warning(
            "Could not schedule SMS follow-up lead_id=%s error=%s", lead_id, error
        )
    return result


def analyze_inbound_and_coach(user_id, lead_id, inbound_id, inbound_body, opted_out=False):
    """
    Claude analysis of an inbound SMS. Stores insight + CRM captures.
    Routine replies are auto-executed by sms_ai_agent; this function does not send.
    """
    result = {"analysis": None, "insight_id": None, "suggested_id": None, "error": None}
    lead = db.get_lead(lead_id, user_id)
    if not lead:
        result["error"] = "lead_missing"
        return result

    conversation = db.list_lead_messages(user_id, lead_id)
    if opted_out:
        insight_id = db.create_lead_insight(
            lead_id,
            user_id,
            inbound_id,
            {
                "summary": "Lead opted out.",
                "intent": "opt_out",
                "next_best_step": "Do not send further SMS.",
                "recommended_action": "Do not send further SMS. Lead opted out.",
                "suggested_reply": "",
                "home_value_pitch": None,
                "confidence_score": 1.0,
                "requires_manual_review": True,
                "escalation_topics": [],
                "raw_json": None,
            },
            model="system",
        )
        crm_db.apply_coach_queue_flags(
            user_id,
            lead_id,
            {"requires_manual_review": True, "needs_attention_reasons": ["opt_out"]},
            insight_id=insight_id,
        )
        result["insight_id"] = insight_id
        result["error"] = "opted_out"
        return result

    if not sms_coach.is_configured():
        insight_id = db.create_lead_insight(
            lead_id,
            user_id,
            inbound_id,
            {
                "summary": "Lead replied. Claude analysis is not configured.",
                "intent": "unknown",
                "next_best_step": "Review the inbound message and reply manually.",
                "recommended_action": "Open the conversation and draft a reply.",
                "suggested_reply": "",
                "home_value_pitch": None,
                "confidence_score": 0.0,
                "requires_manual_review": True,
                "escalation_topics": [],
                "raw_json": None,
            },
            model="none",
        )
        crm_db.apply_coach_queue_flags(
            user_id, lead_id, {"requires_manual_review": True}, insight_id=insight_id
        )
        result["insight_id"] = insight_id
        result["error"] = "not_configured"
        return result

    try:
        analysis = sms_coach.analyze_inbound_reply(
            build_inbound_reply_analysis_prompt(lead, conversation, inbound_body)
        )
    except sms_coach.SmsCoachError:
        logger.warning("Claude inbound analysis failed inbound_id=%s", inbound_id)
        insight_id = db.create_lead_insight(
            lead_id,
            user_id,
            inbound_id,
            {
                "summary": "Lead replied. Automatic analysis failed; review manually.",
                "intent": "unknown",
                "next_best_step": "Review the inbound message and reply manually.",
                "recommended_action": "Open the conversation and draft a reply.",
                "suggested_reply": "",
                "home_value_pitch": None,
                "confidence_score": 0.0,
                "requires_manual_review": True,
                "escalation_topics": [],
                "raw_json": None,
            },
            model=config.CLAUDE_MODEL,
        )
        crm_db.apply_coach_queue_flags(
            user_id, lead_id, {"requires_manual_review": True}, insight_id=insight_id
        )
        result["insight_id"] = insight_id
        result["error"] = "coach_failed"
        return result

    db.update_lead_from_analysis(
        lead_id,
        user_id,
        next_action=analysis.get("recommended_next_action")
        or analysis.get("recommended_action")
        or analysis.get("next_best_step"),
        last_inbound_at=datetime.now(timezone.utc).isoformat(),
    )
    _ensure_sms_follow_up(user_id, lead_id, analysis)

    import autonomy

    autonomy.apply_inbound_side_effects(user_id, lead_id, analysis, source="ai_sms")

    auto_send = autonomy.should_auto_send_sms(analysis, lead)
    suggested_id = None
    draft = analysis.get("draft_reply") or analysis.get("suggested_reply")
    if draft and not auto_send:
        suggested_id = db.create_sms_message(
            user_id=user_id,
            persona_id=None,
            provider=config.SMS_PROVIDER,
            data={
                "lead_name": lead.get("name"),
                "phone_number": lead.get("phone_number"),
                "lead_type": lead.get("lead_type"),
                "property_interest": lead.get("property_interest"),
                "message_body": draft,
                "notes": "Escalated AI reply — agent follow-up required",
            },
            status="suggested",
            lead_id=lead_id,
            direction="suggested",
            consent_status="unknown",
            opt_out_status=lead.get("opt_out_status") or "active",
        )

    insight_status = "processed" if auto_send else "pending"
    insight_id = db.create_lead_insight(
        lead_id,
        user_id,
        inbound_id,
        analysis,
        suggested_message_id=suggested_id,
        model=config.CLAUDE_MODEL,
        status=insight_status,
    )
    crm_db.add_lead_activity(
        lead_id,
        user_id,
        "insight_created",
        "AI SMS analyzed inbound message",
        {
            "insight_id": insight_id,
            "suggested_lead_status": analysis.get("suggested_lead_status"),
            "confidence": analysis.get("confidence_score") or analysis.get("confidence"),
            "auto_execute": auto_send,
        },
    )
    crm_db.apply_coach_queue_flags(
        user_id, lead_id, analysis, insight_id=insight_id, auto_handled=auto_send
    )
    db.record_tool_usage(user_id, "ai_sms", "inbound_analyzed")
    result["analysis"] = analysis
    result["insight_id"] = insight_id
    result["suggested_id"] = suggested_id
    result["auto_execute"] = auto_send
    return result
