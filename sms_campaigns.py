"""Bulk SMS campaign routes — TopAI-owned audience, attestation, and jobs."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import uuid

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

import auth
import config
import db
import tenant_sms_db as tdb
from sms_authorization import CAMPAIGN_CERT_TEXT, ONE_TO_ONE_CERT_TEXT, require_tenant_sender
from sms_validation import validate_e164_phone

logger = logging.getLogger(__name__)

sms_campaigns_bp = Blueprint("sms_campaigns", __name__)


def _user_or_redirect():
    user = auth.get_current_user()
    if not user or not auth.user_has_active_subscription(user):
        return None
    return user


def _nav(user, active_nav="sms-campaigns"):
    return {
        "email": user["email"],
        "has_billing_portal": bool(user.get("stripe_customer_id")),
        "active_nav": active_nav,
        "product_name": "TopAI Real Estate Tools",
    }


def _render_merge(template, fields, defaults=None):
    defaults = defaults or {}
    text = template or ""

    def repl(match):
        key = match.group(1).strip().lower()
        val = fields.get(key) or defaults.get(key) or ""
        return str(val)

    return re.sub(r"\[([a-zA-Z0-9_]+)\]", repl, text)


@sms_campaigns_bp.route("/crm/sms-campaigns")
def campaigns_list():
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("subscriber_app"))
    campaigns = tdb.list_campaigns(user["id"])
    sender, sender_err = require_tenant_sender(user["id"])
    return render_template(
        "crm_sms_campaigns.html",
        campaigns=campaigns,
        sender=sender,
        sender_err=sender_err,
        terms_ok=tdb.has_accepted_sms_terms(user["id"]),
        **_nav(user),
    )


@sms_campaigns_bp.route("/crm/sms-campaigns/new", methods=["GET", "POST"])
def campaign_new():
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("subscriber_app"))
    if request.method == "POST":
        title = (request.form.get("title") or "Untitled campaign").strip()[:200]
        purpose = (request.form.get("campaign_purpose") or "real_estate_follow_up").strip()
        cid = tdb.create_campaign(user["id"], title, campaign_purpose=purpose)
        tdb.append_sms_audit(user["id"], "campaign_created", actor_user_id=user["id"], campaign_id=cid)
        return redirect(url_for("sms_campaigns.campaign_detail", campaign_id=cid))
    return render_template("crm_sms_campaign_new.html", **_nav(user))


@sms_campaigns_bp.route("/crm/sms-campaigns/<int:campaign_id>", methods=["GET", "POST"])
def campaign_detail(campaign_id):
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("subscriber_app"))
    campaign = tdb.get_campaign(campaign_id, user["id"])
    if not campaign:
        return redirect(url_for("sms_campaigns.campaigns_list"))
    error = None
    sender, sender_err = require_tenant_sender(user["id"])

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        if action == "save_message":
            template = (request.form.get("message_template") or "").strip()[:1000]
            purpose = (request.form.get("campaign_purpose") or campaign.get("campaign_purpose") or "").strip()
            old_fp = campaign.get("content_fingerprint")
            new_fp = hashlib.sha256(f"{template}|{purpose}".encode()).hexdigest()
            tdb.update_campaign(
                campaign_id,
                user["id"],
                message_template=template,
                campaign_purpose=purpose,
                content_fingerprint=new_fp,
                sender_number=(sender or {}).get("sender_number"),
            )
            if old_fp and old_fp != new_fp:
                tdb.invalidate_campaign_attestations(user["id"], campaign_id)
                flash("Message changed — recertification required before launch.")
            tdb.append_sms_audit(user["id"], "campaign_edited", actor_user_id=user["id"], campaign_id=campaign_id)
            return redirect(url_for("sms_campaigns.campaign_detail", campaign_id=campaign_id))

        if action == "import_audience":
            error = _import_audience(user["id"], campaign_id, request)
            if not error:
                flash("Audience imported. Review exclusions, then certify before launch.")
                return redirect(url_for("sms_campaigns.campaign_detail", campaign_id=campaign_id))

        if action == "certify":
            if not request.form.get("attestation_accepted"):
                error = "You must check the campaign certification checkbox."
            else:
                campaign = tdb.get_campaign(campaign_id, user["id"])
                recipients = tdb.list_campaign_recipients(campaign_id, user["id"])
                eligible = [r for r in recipients if r.get("eligible")]
                excluded = len(recipients) - len(eligible)
                if not campaign.get("message_template"):
                    error = "Compose a message before certifying."
                elif not eligible:
                    error = "No eligible recipients to certify."
                elif sender_err:
                    error = sender_err
                else:
                    att_id = tdb.create_campaign_attestation(
                        user["id"],
                        user["id"],
                        campaign_id,
                        eligible_count=len(eligible),
                        excluded_count=excluded,
                        campaign_purpose=campaign.get("campaign_purpose") or "campaign",
                        message_body=campaign.get("message_template") or "",
                        audience_snapshot_id=campaign.get("audience_snapshot_id") or "",
                        provider=config.SMS_PROVIDER,
                        scheduled_launch_at=request.form.get("scheduled_at") or None,
                    )
                    tdb.update_campaign(campaign_id, user["id"], attestation_id=att_id)
                    tdb.append_sms_audit(
                        user["id"],
                        "consent_certification_accepted",
                        actor_user_id=user["id"],
                        campaign_id=campaign_id,
                        metadata={"attestation_id": att_id, "eligible": len(eligible)},
                    )
                    flash("Audience certified (subscriber certification — not TopAI verified).")
                    return redirect(url_for("sms_campaigns.campaign_detail", campaign_id=campaign_id))

        if action == "launch":
            campaign = tdb.get_campaign(campaign_id, user["id"])
            if config.TELNYX_TRIAL_MODE and (config.SMS_PROVIDER or "").lower() == "telnyx":
                recipients = tdb.list_campaign_recipients(campaign_id, user["id"], eligible_only=True)
                verified = "".join(c for c in (config.TELNYX_VERIFIED_TEST_NUMBER or "") if c.isdigit())
                bad = [
                    r
                    for r in recipients
                    if "".join(c for c in (r.get("phone_number") or "") if c.isdigit()) != verified
                ]
                if bad or not recipients:
                    error = (
                        "Telnyx trial mode: campaign audience must contain only the verified test phone number."
                    )
                    # Fall through to render with error
                else:
                    error = None
            else:
                error = None
            if not error:
                att = tdb.get_valid_campaign_attestation(
                    user["id"],
                    campaign_id,
                    message_body=campaign.get("message_template") or "",
                    audience_snapshot_id=campaign.get("audience_snapshot_id") or "",
                    purpose=campaign.get("campaign_purpose") or "campaign",
                )
                if not att:
                    error = "Certify the final audience and message before launch."
                elif sender_err:
                    error = sender_err
                else:
                    scheduled = (request.form.get("scheduled_at") or "").strip() or None
                    tdb.create_jobs_for_campaign(campaign_id, user["id"])
                    if scheduled:
                        tdb.update_campaign(
                            campaign_id, user["id"], status="scheduled", scheduled_at=scheduled
                        )
                        tdb.append_sms_audit(
                            user["id"], "campaign_scheduled", actor_user_id=user["id"], campaign_id=campaign_id
                        )
                    else:
                        from datetime import datetime, timezone

                        tdb.update_campaign(
                            campaign_id,
                            user["id"],
                            status="processing",
                            started_at=datetime.now(timezone.utc).isoformat(),
                        )
                        tdb.append_sms_audit(
                            user["id"], "campaign_launched", actor_user_id=user["id"], campaign_id=campaign_id
                        )
                    flash("Campaign queued. The worker sends messages outside this request.")
                    return redirect(url_for("sms_campaigns.campaign_detail", campaign_id=campaign_id))

        if action in {"pause", "resume", "cancel"}:
            mapping = {
                "pause": ("paused", "campaign_paused"),
                "resume": ("processing", "campaign_resumed"),
                "cancel": ("cancelled", "campaign_cancelled"),
            }
            status, audit = mapping[action]
            tdb.update_campaign(campaign_id, user["id"], status=status)
            if action == "cancel":
                with db.get_db() as conn:
                    conn.execute(
                        """
                        UPDATE sms_campaign_jobs SET status='cancelled', updated_at=?
                        WHERE campaign_id=? AND user_id=? AND status IN ('pending','claimed')
                        """,
                        (
                            __import__("datetime").datetime.now(
                                __import__("datetime").timezone.utc
                            ).isoformat(),
                            campaign_id,
                            user["id"],
                        ),
                    )
            tdb.append_sms_audit(user["id"], audit, actor_user_id=user["id"], campaign_id=campaign_id)
            return redirect(url_for("sms_campaigns.campaign_detail", campaign_id=campaign_id))

    campaign = tdb.get_campaign(campaign_id, user["id"])
    recipients = tdb.list_campaign_recipients(campaign_id, user["id"])
    job_stats = tdb.count_jobs_by_status(campaign_id, user["id"])
    return render_template(
        "crm_sms_campaign_detail.html",
        campaign=campaign,
        recipients=recipients[:200],
        recipient_count=len(recipients),
        eligible_count=sum(1 for r in recipients if r.get("eligible")),
        job_stats=job_stats,
        error=error,
        sender=sender,
        sender_err=sender_err,
        campaign_cert_text=CAMPAIGN_CERT_TEXT,
        warning_import=(
            "Upload only contacts who have authorized you or your business to text them. "
            "Importing or possessing a phone number does not establish consent."
        ),
        **_nav(user),
    )


def _import_audience(user_id, campaign_id, request):
    upload = request.files.get("audience_file")
    if not upload or not upload.filename:
        return "Upload a CSV or Excel file."
    name = upload.filename.lower()
    data = upload.read(config.SMS_IMPORT_MAX_BYTES + 1)
    if len(data) > config.SMS_IMPORT_MAX_BYTES:
        return "File exceeds size limit."
    rows = []
    try:
        if name.endswith(".csv"):
            text = data.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)
        elif name.endswith(".xlsx"):
            import openpyxl

            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            ws = wb.active
            headers = [str(c.value or "").strip() for c in next(ws.iter_rows(max_row=1))]
            for row in ws.iter_rows(min_row=2, values_only=True):
                rows.append({headers[i]: (row[i] if i < len(row) else None) for i in range(len(headers))})
        else:
            return "Supported formats: .csv, .xlsx"
    except Exception as exc:
        logger.warning("Audience import parse failed: %s", type(exc).__name__)
        return "Could not parse the uploaded file."

    if len(rows) > config.SMS_IMPORT_MAX_ROWS:
        return f"Import limited to {config.SMS_IMPORT_MAX_ROWS} rows."

    recipients = []
    seen = set()
    for row in rows:
        phone_raw = (
            row.get("phone")
            or row.get("phone_number")
            or row.get("mobile")
            or row.get("Phone")
            or ""
        )
        phone, err = validate_e164_phone(str(phone_raw))
        first = str(row.get("first_name") or row.get("firstName") or "").strip()
        last = str(row.get("last_name") or row.get("lastName") or "").strip()
        full = str(row.get("name") or row.get("full_name") or f"{first} {last}").strip()
        merge = {
            "first_name": first,
            "last_name": last,
            "full_name": full,
            "email": str(row.get("email") or "").strip(),
            "property_interest": str(row.get("property_interest") or "").strip(),
        }
        exclusion = None
        eligible = True
        lead_id = None
        if err or not phone:
            exclusion = "invalid_number"
            eligible = False
            phone = str(phone_raw)[:32]
        elif phone in seen:
            exclusion = "duplicate"
            eligible = False
        else:
            seen.add(phone)
            if tdb.is_suppressed(user_id, phone):
                exclusion = "suppressed"
                eligible = False
            lead = db.get_lead_by_phone(user_id, phone)
            if lead:
                lead_id = lead["id"]
                if (lead.get("opt_out_status") or "") == "opted_out" or (
                    lead.get("sms_consent_status") or ""
                ) == "opted_out":
                    exclusion = "opted_out"
                    eligible = False
            else:
                # Create lead as not_certified
                from external_leads.ingest import ingest_external_lead

                result = ingest_external_lead(
                    user_id,
                    {
                        "full_name": full or "Imported Contact",
                        "phone": phone,
                        "email": merge.get("email"),
                        "property_interest": merge.get("property_interest"),
                    },
                    method="csv",
                    actor_user_id=user_id,
                )
                lead_id = result.get("lead_id")
                if lead_id:
                    import external_leads_db as xdb

                    xdb.set_lead_sms_consent_state(
                        lead_id,
                        user_id,
                        sms_consent_status="not_certified",
                        sms_sending_blocked=True,
                        actor_user_id=user_id,
                        source="campaign_import",
                    )
        recipients.append(
            {
                "lead_id": lead_id,
                "phone_number": phone,
                "merge_fields": merge,
                "eligible": eligible,
                "exclusion_reason": exclusion,
            }
        )

    tdb.replace_campaign_recipients(campaign_id, user_id, recipients)
    tdb.append_sms_audit(
        user_id,
        "import_completed",
        actor_user_id=user_id,
        campaign_id=campaign_id,
        metadata={"rows": len(recipients)},
    )
    return None


@sms_campaigns_bp.route("/crm/sms-diagnostics", methods=["GET", "POST"])
def sms_diagnostics():
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("subscriber_app"))
    if request.method == "POST" and request.form.get("action") == "accept_terms":
        tdb.accept_sms_terms(
            user["id"],
            user["id"],
            ip_address=request.headers.get("X-Forwarded-For", request.remote_addr),
            user_agent=request.headers.get("User-Agent"),
        )
        flash("SMS terms accepted.")
        return redirect(url_for("sms_campaigns.sms_diagnostics"))

    from sms_providers import get_sms_provider

    provider = get_sms_provider()
    sender, sender_err = require_tenant_sender(user["id"])
    last_out = tdb.latest_sms_event(user["id"], direction="outbound")
    last_in = tdb.latest_sms_event(user["id"], direction="inbound")
    info = {
        "active_provider": config.SMS_PROVIDER,
        "provider_configured": provider.is_configured(),
        "sender": sender,
        "sender_error": sender_err,
        "token_configured": bool(
            config.TELNYX_API_KEY
            if (config.SMS_PROVIDER or "").lower() == "telnyx"
            else config.SIMPLETEXTING_API_TOKEN
        ),
        "webhook_secret_configured": bool(
            config.TELNYX_PUBLIC_KEY
            if (config.SMS_PROVIDER or "").lower() == "telnyx"
            else config.SIMPLETEXTING_WEBHOOK_SECRET
        ),
        "telnyx_trial_mode": bool(config.TELNYX_TRIAL_MODE)
        if (config.SMS_PROVIDER or "").lower() == "telnyx"
        else False,
        "telnyx_profile_configured": bool(config.TELNYX_MESSAGING_PROFILE_ID),
        "telnyx_phone_configured": bool(config.TELNYX_PHONE_NUMBER),
        "telnyx_verified_test_configured": bool(config.TELNYX_VERIFIED_TEST_NUMBER),
        "telnyx_public_key_configured": bool(config.TELNYX_PUBLIC_KEY),
        "webhook_route": "/webhooks/telnyx/messaging",
        "last_outbound": last_out,
        "last_inbound": last_in,
        "queue_backlog": tdb.count_pending_campaign_jobs(),
        "terms_ok": tdb.has_accepted_sms_terms(user["id"]),
        "terms_version": config.SMS_TERMS_VERSION,
        "one_to_one_cert_text": ONE_TO_ONE_CERT_TEXT,
        "campaign_cert_text": CAMPAIGN_CERT_TEXT,
        "sender_info": provider.get_sender_information(),
        "warning": (
            "You may send SMS only to contacts who have authorized you or your business "
            "to text them. TopAI does not independently verify your supporting consent records."
        ),
    }
    return render_template("crm_sms_diagnostics.html", **info, **_nav(user, "sms-diagnostics"))


@sms_campaigns_bp.route("/crm/sms-campaigns/<int:campaign_id>/export")
def campaign_export(campaign_id):
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("subscriber_app"))
    campaign = tdb.get_campaign(campaign_id, user["id"])
    if not campaign:
        return redirect(url_for("sms_campaigns.campaigns_list"))
    recipients = tdb.list_campaign_recipients(campaign_id, user["id"])
    jobs = {}
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM sms_campaign_jobs WHERE campaign_id = ? AND user_id = ?",
            (campaign_id, user["id"]),
        ).fetchall()
        for r in rows:
            jobs[r["recipient_id"]] = dict(r)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "phone_number",
            "eligible",
            "exclusion_reason",
            "job_status",
            "provider_message_id",
            "failure_message",
        ]
    )
    for r in recipients:
        job = jobs.get(r["id"]) or {}
        writer.writerow(
            [
                r.get("phone_number"),
                r.get("eligible"),
                r.get("exclusion_reason") or "",
                job.get("status") or "",
                job.get("provider_message_id") or "",
                job.get("failure_message") or "",
            ]
        )
    from flask import Response

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=campaign_{campaign_id}_export.csv"
        },
    )


@sms_campaigns_bp.route("/r/<token>")
def tracking_redirect(token):
    row = tdb.record_link_click(token)
    if not row:
        return "Not found", 404
    return redirect(row["destination_url"])
