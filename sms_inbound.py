"""Inbound Twilio SMS processing: match tenant/lead, store, coach (never auto-send)."""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone

import config
import crm_db
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
    Persist inbound SMS and enqueue Claude coaching.
    Never sends an outbound SMS. Returns a small result dict for logging/tests.
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
        db.mark_lead_opt_out(lead_id, owner_id)
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
        crm_db.upsert_needs_attention(
            owner_id,
            lead_id,
            "unreviewed_inbound",
            priority="high",
            source_ref_type="sms",
            source_ref_id=inbound_id,
        )
        logger.info(
            "Inbound SMS processed sid=%s tenant=%s lead=%s keyword=none",
            message_sid,
            owner_id,
            lead_id,
        )

    # Claude analysis is deferred so Twilio gets a fast empty TwiML ack.
    # Never auto-sends outbound SMS from this path.
    opted_out_flag = keyword == "opt_out"
    if defer_coach:
        _schedule_coach(
            app, owner_id, lead_id, inbound_id, store_body, opted_out=opted_out_flag
        )
    else:
        analyze_inbound_and_coach(
            owner_id, lead_id, inbound_id, store_body, opted_out=opted_out_flag
        )

    return result


def _schedule_coach(app, user_id, lead_id, inbound_id, body, opted_out=False):
    def run():
        try:
            if app is not None:
                with app.app_context():
                    analyze_inbound_and_coach(
                        user_id, lead_id, inbound_id, body, opted_out=opted_out
                    )
            else:
                analyze_inbound_and_coach(
                    user_id, lead_id, inbound_id, body, opted_out=opted_out
                )
        except Exception:
            logger.exception(
                "Deferred inbound coach failed inbound_id=%s lead_id=%s",
                inbound_id,
                lead_id,
            )

    threading.Thread(target=run, name=f"sms-coach-{inbound_id}", daemon=True).start()


def analyze_inbound_and_coach(user_id, lead_id, inbound_id, inbound_body, opted_out=False):
    """Claude coaching. Stores recommendations + draft only — never auto-sends."""
    lead = db.get_lead(lead_id, user_id)
    if not lead:
        return

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
        return

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
        return

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
        return

    note_bits = [
        analysis.get("summary"),
        analysis.get("intent"),
        analysis.get("recommended_next_action") or analysis.get("next_best_step"),
    ]
    notes = " | ".join(bit for bit in note_bits if bit)[:1500] or None
    db.update_lead_from_analysis(
        lead_id,
        user_id,
        notes=notes,
        next_action=analysis.get("recommended_next_action")
        or analysis.get("recommended_action")
        or analysis.get("next_best_step"),
        last_inbound_at=datetime.now(timezone.utc).isoformat(),
    )

    suggested_id = None
    draft = analysis.get("draft_reply") or analysis.get("suggested_reply")
    if draft:
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
                "notes": "Claude suggested reply pending agent approval",
            },
            status="suggested",
            lead_id=lead_id,
            direction="suggested",
            consent_status="unknown",
            opt_out_status=lead.get("opt_out_status") or "active",
        )

    insight_id = db.create_lead_insight(
        lead_id,
        user_id,
        inbound_id,
        analysis,
        suggested_message_id=suggested_id,
        model=config.CLAUDE_MODEL,
    )
    crm_db.add_lead_activity(
        lead_id,
        user_id,
        "insight_created",
        "Claude coaching recommendation ready for review",
        {
            "insight_id": insight_id,
            "suggested_lead_status": analysis.get("suggested_lead_status"),
            "confidence": analysis.get("confidence_score") or analysis.get("confidence"),
        },
    )
    crm_db.apply_coach_queue_flags(user_id, lead_id, analysis, insight_id=insight_id)
    db.record_tool_usage(user_id, "ai_sms", "inbound_analyzed")
