"""PostgreSQL/SQLite-backed SMS campaign worker (no Redis/Celery)."""

from __future__ import annotations

import logging
import os
import signal
import socket
import time
from datetime import datetime, timedelta, timezone

import config
import db
import tenant_sms_db as tdb
from sms_authorization import can_send_sms, require_tenant_sender
from sms_providers import SmsProviderError, get_sms_provider

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sms_campaign_worker")

_RUNNING = True

# Listing Generator 60-day retention cleanup piggybacks on this worker's idle
# loop rather than adding a second Railway process. Defense-in-depth #2 —
# application read queries already filter expires_at > now (#1); this is the
# hard-delete sweep. A daily cadence is enough for a 60-day retention window.
_LAST_LISTING_CLEANUP = None
LISTING_CLEANUP_INTERVAL = timedelta(hours=24)


def _handle_signal(signum, frame):
    global _RUNNING
    logger.info("Shutdown signal received (%s)", signum)
    _RUNNING = False


def _now():
    return datetime.now(timezone.utc)


def _maybe_cleanup_expired_listings():
    global _LAST_LISTING_CLEANUP
    now = _now()
    if _LAST_LISTING_CLEANUP is not None and now - _LAST_LISTING_CLEANUP < LISTING_CLEANUP_INTERVAL:
        return
    _LAST_LISTING_CLEANUP = now
    try:
        import listing_generations_db as listing_db
        import social_connections_db as social_db

        deleted = listing_db.cleanup_expired()
        social_db.cleanup_expired_oauth_states()
        if deleted:
            logger.info("Listing retention cleanup removed %s expired listing_generations row(s)", deleted)
    except Exception:
        logger.exception("Listing retention cleanup failed")


def _render(template, merge_fields, defaults=None):
    import re

    defaults = defaults or {}
    fields = {**(defaults or {}), **(merge_fields or {})}
    # normalize keys
    norm = {str(k).lower(): v for k, v in fields.items()}

    def repl(m):
        return str(norm.get(m.group(1).lower()) or "")

    return re.sub(r"\[([a-zA-Z0-9_]+)\]", repl, template or "")


def _wrap_tracking_links(user_id, text, *, campaign_id=None, lead_id=None):
    import re

    base = (config.APP_URL or "").rstrip("/")
    if not base or "localhost" in base:
        return text

    def repl(match):
        url = match.group(0)
        token = tdb.create_tracking_link(
            user_id, url, campaign_id=campaign_id, lead_id=lead_id
        )
        return f"{base}/r/{token}"

    return re.sub(r"https?://[^\s]+", repl, text or "")


def _is_ai_generated_row(row):
    val = (row or {}).get("ai_generated")
    return val in (True, 1, "1", "t", "true", "TRUE")


def _reschedule_quiet_hours(row):
    from sms_quiet_hours import next_permitted_send_at

    lead = db.get_lead(row.get("lead_id"), row.get("user_id")) or {}
    send_at = next_permitted_send_at(
        row.get("user_id"),
        phone=row.get("phone_number") or lead.get("phone_number"),
        lead=lead,
    )
    db.schedule_sms_message(row["id"], send_at.isoformat())


def _send_scheduled_ai_reply(row):
    """Deliver a quiet-hours-deferred AI auto-reply using the same gates as live auto-reply."""
    from sms_consent import outbound_sms_blocked_for_phone
    from sms_provider import sms_status_callback_url
    from sms_providers import SmsProviderError, get_sms_provider
    from sms_quiet_hours import in_quiet_hours

    user_id = row.get("user_id")
    lead_id = row.get("lead_id")
    lead = db.get_lead(lead_id, user_id) or {}
    phone = row.get("phone_number") or lead.get("phone_number")
    body = row.get("message_body") or ""
    if (lead.get("opt_out_status") or "active") == "opted_out":
        db.update_sms_message_send_result(
            row["id"], status="failed", error_message="This lead opted out. Do not send SMS."
        )
        return
    if tdb.is_suppressed(user_id, phone) or outbound_sms_blocked_for_phone(phone):
        db.update_sms_message_send_result(
            row["id"], status="failed", error_message="SMS cannot be sent due to consent or opt-out restrictions."
        )
        return
    if in_quiet_hours(user_id, phone=phone, lead=lead):
        _reschedule_quiet_hours(row)
        return
    provider = get_sms_provider()
    from_number = None
    sender, _err = require_tenant_sender(user_id)
    if sender:
        from_number = sender.get("sender_number")
    try:
        result = provider.send_sms(
            phone,
            body,
            status_callback=sms_status_callback_url(),
            from_number=from_number or None,
        )
    except SmsProviderError as exc:
        db.update_sms_message_send_result(
            row["id"], status="failed", error_message=str(exc)[:500]
        )
        return
    except Exception:
        logger.exception("scheduled AI SMS send failed message_id=%s", row.get("id"))
        db.update_sms_message_send_result(
            row["id"],
            status="failed",
            error_message="AI reply could not be sent due to an internal error.",
        )
        return
    db.update_sms_message_send_result(
        row["id"],
        provider_message_id=str(result.get("provider_message_id") or "") or None,
        status=result.get("status") or "queued",
    )
    db.touch_lead_outbound(lead_id, user_id)


def process_due_scheduled_messages(limit=10):
    """Send quiet-hours-deferred one-to-one / AI SMS. Returns True if any work ran."""
    from sms_outbound import send_authorized_sms
    from sms_quiet_hours import is_quiet_hours_block

    rows = db.list_due_scheduled_sms(limit=limit)
    if not rows:
        return False
    worked = False
    for row in rows:
        if not db.claim_scheduled_sms(row["id"]):
            continue
        worked = True
        lead_id = row.get("lead_id")
        user_id = row.get("user_id")
        body = row.get("message_body") or ""
        if not lead_id or not user_id:
            db.update_sms_message_send_result(
                row["id"],
                status="failed",
                error_message="Missing lead for scheduled SMS.",
            )
            continue
        if _is_ai_generated_row(row):
            _send_scheduled_ai_reply(row)
            continue
        _result, err, _status = send_authorized_sms(
            user_id,
            lead_id,
            body,
            source_page="scheduled_quiet_hours",
            compliance_confirmed=True,
            persona_id=row.get("persona_id"),
            message_id=row["id"],
            skip_quiet_hours=False,
        )
        if err and is_quiet_hours_block(err):
            _reschedule_quiet_hours(row)
            continue
        if err:
            db.update_sms_message_send_result(
                row["id"], status="failed", error_message=str(err)[:500]
            )
    return worked


def process_one(worker_id: str) -> bool:
    job = tdb.claim_next_job(worker_id)
    if not job:
        return False

    campaign = tdb.get_campaign(job["campaign_id"], job["user_id"])
    if not campaign or campaign.get("status") not in {"processing", "scheduled"}:
        tdb.update_job(job["id"], status="cancelled")
        return True

    # Promote scheduled → processing when due
    if campaign.get("status") == "scheduled":
        scheduled_at = campaign.get("scheduled_at")
        if scheduled_at and scheduled_at > _now().isoformat():
            tdb.update_job(job["id"], status="pending", claimed_at=None, claimed_by=None)
            return True
        tdb.update_campaign(job["campaign_id"], job["user_id"], status="processing", started_at=_now().isoformat())

    sender, sender_err = require_tenant_sender(job["user_id"])
    if sender_err:
        tdb.update_job(
            job["id"],
            status="failed_permanent",
            failure_message=sender_err,
            failed_at=_now().isoformat(),
        )
        return True

    lead_id = job.get("lead_id")
    body = campaign.get("message_template") or ""
    # Load merge fields from recipient
    with db.get_db() as conn:
        recip = conn.execute(
            "SELECT * FROM sms_campaign_recipients WHERE id = ?",
            (job["recipient_id"],),
        ).fetchone()
    merge = {}
    if recip and recip["merge_fields_json"]:
        import json

        try:
            merge = json.loads(recip["merge_fields_json"])
        except Exception:
            merge = {}
    defaults = {}
    if campaign.get("merge_defaults_json"):
        import json

        try:
            defaults = json.loads(campaign["merge_defaults_json"])
        except Exception:
            defaults = {}
    message_body = _render(body, merge, defaults)[:480]
    message_body = _wrap_tracking_links(
        job["user_id"],
        message_body,
        campaign_id=job["campaign_id"],
        lead_id=lead_id,
    )[:480]

    if not lead_id:
        tdb.update_job(job["id"], status="failed_permanent", failure_message="Missing lead", failed_at=_now().isoformat())
        return True

    allowed, msg = can_send_sms(
        job["user_id"],
        lead_id,
        campaign_id=job["campaign_id"],
        message_purpose=campaign.get("campaign_purpose") or "campaign",
        message_body=campaign.get("message_template") or message_body,
        require_attestation_record=True,
    )
    if not allowed:
        status = "opted_out" if "opt" in (msg or "").lower() else "suppressed"
        if "quiet" in (msg or "").lower():
            from sms_quiet_hours import next_permitted_send_at

            lead = db.get_lead(lead_id, job["user_id"]) or {}
            send_at = next_permitted_send_at(
                job["user_id"],
                phone=lead.get("phone_number") or (recip["phone_number"] if recip else None),
                lead=lead,
            )
            tdb.update_job(
                job["id"],
                status="pending",
                next_attempt_at=send_at.isoformat(),
                claimed_at=None,
                claimed_by=None,
                failure_message=msg,
            )
            return True
        if "rate" in (msg or "").lower():
            delay = min(2 ** int(job.get("attempts") or 1), 60)
            tdb.update_job(
                job["id"],
                status="pending",
                next_attempt_at=(_now() + timedelta(minutes=delay)).isoformat(),
                claimed_at=None,
                claimed_by=None,
                failure_message=msg,
            )
            return True
        tdb.update_job(job["id"], status=status, failure_message=msg, failed_at=_now().isoformat())
        return True

    tdb.update_job(job["id"], status="sending")
    provider = get_sms_provider()
    try:
        result = provider.send_sms(
            job["phone_number"],
            message_body,
            from_number=sender.get("sender_number"),
        )
        sms_id = db.create_sms_message(
            user_id=job["user_id"],
            persona_id=None,
            provider=config.SMS_PROVIDER,
            data={
                "lead_name": merge.get("full_name") or "Campaign contact",
                "phone_number": job["phone_number"],
                "message_body": message_body,
            },
            status=result.get("status") or "submitted",
            lead_id=lead_id,
            direction="outbound",
            consent_status="confirmed",
            opt_out_status="active",
        )
        db.update_sms_message_send_result(
            sms_id,
            provider_message_id=result["provider_message_id"],
            status=result.get("status") or "submitted",
        )
        tdb.update_job(
            job["id"],
            status="submitted",
            provider_message_id=result["provider_message_id"],
            sms_message_id=sms_id,
            submitted_at=_now().isoformat(),
        )
        tdb.append_sms_audit(
            job["user_id"],
            "message_sent",
            campaign_id=job["campaign_id"],
            lead_id=lead_id,
            metadata={"job_id": job["id"], "provider_message_id": result["provider_message_id"]},
        )
    except SmsProviderError as exc:
        attempts = int(job.get("attempts") or 1)
        if getattr(exc, "retryable", False) and attempts < config.SMS_MAX_RETRIES:
            delay = min(2 ** attempts, 120)
            tdb.update_job(
                job["id"],
                status="pending",
                next_attempt_at=(_now() + timedelta(minutes=delay)).isoformat(),
                claimed_at=None,
                claimed_by=None,
                failure_code=str(getattr(exc, "provider_code", "") or ""),
                failure_message=str(exc)[:500],
            )
        else:
            tdb.update_job(
                job["id"],
                status="failed_permanent",
                failure_code=str(getattr(exc, "provider_code", "") or ""),
                failure_message=str(exc)[:500],
                failed_at=_now().isoformat(),
            )
            tdb.append_sms_audit(
                job["user_id"],
                "message_failed",
                campaign_id=job["campaign_id"],
                lead_id=lead_id,
                metadata={"error": str(exc)[:200]},
            )
    except Exception:
        logger.exception("Unexpected worker send failure job=%s", job["id"])
        tdb.update_job(
            job["id"],
            status="failed_retryable",
            failure_message="internal_error",
            next_attempt_at=(_now() + timedelta(minutes=5)).isoformat(),
        )

    _maybe_complete_campaign(job["campaign_id"], job["user_id"])
    return True


def _maybe_complete_campaign(campaign_id, user_id):
    stats = tdb.count_jobs_by_status(campaign_id, user_id)
    pending = stats.get("pending", 0) + stats.get("claimed", 0) + stats.get("sending", 0)
    if pending:
        return
    submitted = stats.get("submitted", 0) + stats.get("delivered", 0)
    failed = (
        stats.get("failed_permanent", 0)
        + stats.get("failed_retryable", 0)
        + stats.get("suppressed", 0)
        + stats.get("opted_out", 0)
    )
    if submitted and failed:
        status = "partially_completed"
    elif submitted:
        status = "completed"
    else:
        status = "failed"
    tdb.update_campaign(
        campaign_id,
        user_id,
        status=status,
        completed_at=_now().isoformat(),
        stats_json=stats,
    )
    tdb.append_sms_audit(user_id, "campaign_completed", campaign_id=campaign_id, new_value=status)


def main():
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    logger.info(
        "SMS campaign worker starting id=%s provider=%s",
        worker_id,
        config.SMS_PROVIDER,
    )
    # Ensure schema present
    db.init_db()
    logger.info("SMS campaign worker schema ready id=%s", worker_id)
    ok = tdb.touch_worker_heartbeat(
        worker_id,
        status="running",
        metadata={"provider": config.SMS_PROVIDER, "boot": True},
    )
    logger.info("SMS campaign worker initial heartbeat ok=%s id=%s", ok, worker_id)
    idle_sleep = 2
    loops = 0
    while _RUNNING:
        try:
            ok = tdb.touch_worker_heartbeat(
                worker_id,
                status="running",
                metadata={"provider": config.SMS_PROVIDER},
            )
            loops += 1
            if not ok or loops % 30 == 1:
                logger.info(
                    "SMS campaign worker heartbeat ok=%s loop=%s id=%s",
                    ok,
                    loops,
                    worker_id,
                )
            _maybe_cleanup_expired_listings()
            worked = process_due_scheduled_messages()
            if process_one(worker_id):
                worked = True
            if worked:
                idle_sleep = 1
            else:
                time.sleep(idle_sleep)
                idle_sleep = min(idle_sleep + 1, 10)
        except Exception:
            logger.exception("Worker loop error")
            time.sleep(5)
    try:
        tdb.touch_worker_heartbeat(worker_id, status="stopped")
    except Exception:
        pass
    logger.info("Worker stopped cleanly")


if __name__ == "__main__":
    main()
