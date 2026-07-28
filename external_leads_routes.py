"""CRM routes for external lead sources, ingest, consent, and pond claim."""

from __future__ import annotations

import json
import logging
import os
import re

from flask import (
    Blueprint,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

import auth
import crm_db
import db
import external_leads_db as xdb
from crm_constants import (
    CONSENT_CONFIRMATION_STATEMENT,
    CONSENT_METHODS,
    EVIDENCE_TYPES,
    EXTERNAL_SOURCE_CATEGORIES,
    POND_STATUSES,
    VERBAL_CONSENT_SCRIPT,
    status_label,
)
from external_leads.consent_workflow import (
    confirm_qualifying_consent,
    resolve_upload_path,
    save_evidence_upload,
)
from external_leads.csv_import import commit_csv, preview_csv, suggest_mapping
from external_leads.ingest import ingest_external_lead
from external_leads.webhook import generate_webhook_secret, hash_webhook_secret

logger = logging.getLogger(__name__)

external_leads_bp = Blueprint("external_leads", __name__)


def _user_or_redirect():
    user = auth.get_current_user()
    if not user or not auth.user_has_active_subscription(user):
        return None
    return user


def _nav(user, active="leads"):
    return {
        "email": user["email"],
        "has_billing_portal": bool(user.get("stripe_customer_id")),
        "active_nav": active,
        "product_name": "TopAI Real Estate Tools",
    }


def _slug_key(value):
    text = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return text[:80] or "source"


def _wants_json():
    return (
        request.is_json
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in (request.headers.get("Accept") or "")
    )


@external_leads_bp.route("/crm/external-sources", methods=["GET", "POST"])
def external_sources_page():
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("subscriber_app"))
    error = None
    created_secret = None
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        category = (request.form.get("category") or "other").strip()
        provider_key = (request.form.get("provider_key") or "").strip() or _slug_key(name)
        import_method = (request.form.get("import_method") or "manual").strip()
        default_pond = (request.form.get("default_pond_status") or "claimable").strip()
        if default_pond not in POND_STATUSES:
            default_pond = "claimable"
        if not name:
            error = "Source name is required."
        elif category not in set(EXTERNAL_SOURCE_CATEGORIES):
            error = "Select a valid category."
        else:
            raw_secret = generate_webhook_secret() if import_method in {"webhook", "api"} else None
            source_id = xdb.create_external_lead_source(
                user["id"],
                name=name,
                category=category,
                provider_key=provider_key,
                import_method=import_method,
                default_lead_type=(request.form.get("default_lead_type") or "").strip() or None,
                default_lead_status=(request.form.get("default_lead_status") or "new").strip()
                or "new",
                default_pond_status=default_pond,
                webhook_secret_hash=hash_webhook_secret(raw_secret) if raw_secret else None,
            )
            if raw_secret:
                created_secret = raw_secret
            flash(f"Source created (id={source_id}). Consent behavior is always unverified + blocked.")
            if not created_secret:
                return redirect(url_for("external_leads.external_sources_page"))
    sources = xdb.list_external_lead_sources(user["id"])
    return render_template(
        "crm_external_sources.html",
        sources=sources,
        categories=EXTERNAL_SOURCE_CATEGORIES,
        pond_statuses=POND_STATUSES,
        error=error,
        created_secret=created_secret,
        **_nav(user, "leads"),
    )


@external_leads_bp.route("/crm/external-leads/new", methods=["GET", "POST"])
def external_lead_new():
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("subscriber_app"))
    sources = xdb.list_external_lead_sources(user["id"], active_only=True)
    error = None
    if request.method == "POST":
        source_id = request.form.get("external_source_id")
        source_row = None
        if source_id:
            try:
                source_row = xdb.get_external_lead_source(int(source_id), user["id"])
            except (TypeError, ValueError):
                source_row = None
        payload = {
            "first_name": request.form.get("first_name"),
            "last_name": request.form.get("last_name"),
            "full_name": request.form.get("full_name"),
            "phone": request.form.get("phone"),
            "email": request.form.get("email"),
            "external_record_id": request.form.get("external_record_id"),
            "property_address": request.form.get("property_address"),
            "property_url": request.form.get("property_url"),
            "inquiry_notes": request.form.get("inquiry_notes"),
            "lead_type": request.form.get("lead_type"),
            "original_consent_status": request.form.get("original_consent_status"),
            "original_consent_date": request.form.get("original_consent_date"),
            "original_consent_text": request.form.get("original_consent_text"),
            "pond_status": request.form.get("pond_status") or "claimable",
        }
        result = ingest_external_lead(
            user["id"],
            payload,
            source_row=source_row,
            method="manual",
            actor_user_id=user["id"],
        )
        if result.get("error"):
            error = result["error"]
        else:
            flash(
                "External lead saved as Unverified + SMS Blocked. "
                "Confirm consent evidence before texting."
            )
            return redirect(url_for("crm.crm_lead_detail_page", lead_id=result["lead_id"]))
    return render_template(
        "crm_external_lead_new.html",
        sources=sources,
        pond_statuses=POND_STATUSES,
        error=error,
        form=request.form,
        **_nav(user, "leads"),
    )


@external_leads_bp.route("/crm/external-leads/import", methods=["GET", "POST"])
def external_lead_import():
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("subscriber_app"))
    sources = xdb.list_external_lead_sources(user["id"], active_only=True)
    preview = None
    error = None
    result = None
    csv_text = ""
    mapping = {}
    source_id = request.form.get("external_source_id") or request.args.get("external_source_id")

    if request.method == "POST":
        action = (request.form.get("action") or "preview").strip()
        upload = request.files.get("csv_file")
        csv_text = request.form.get("csv_text") or ""
        if upload and upload.filename:
            try:
                csv_text = upload.read().decode("utf-8-sig")
            except UnicodeDecodeError:
                error = "CSV must be UTF-8 encoded."
        mapping_raw = request.form.get("mapping_json") or "{}"
        try:
            mapping = json.loads(mapping_raw) if mapping_raw else {}
        except json.JSONDecodeError:
            mapping = {}
        if not mapping and csv_text:
            headers = (csv_text.splitlines() or [""])[0]
            mapping = suggest_mapping([h.strip() for h in headers.split(",")])

        source_row = None
        if source_id:
            try:
                source_row = xdb.get_external_lead_source(int(source_id), user["id"])
            except (TypeError, ValueError):
                source_row = None

        if not error and not csv_text.strip():
            error = "Upload a CSV or paste CSV text."
        elif not error and action == "commit":
            result = commit_csv(
                user["id"],
                csv_text,
                mapping,
                source_row=source_row,
                filename=(upload.filename if upload else None),
                actor_user_id=user["id"],
            )
            if result.get("error"):
                error = result["error"]
            else:
                flash(
                    f"Import finished: {result.get('created', 0)} created, "
                    f"{result.get('updated', 0)} updated, "
                    f"{result.get('invalid', 0)} invalid. "
                    "All external leads remain Unverified + Blocked."
                )
        elif not error:
            preview = preview_csv(csv_text, mapping=mapping)
            if preview.get("error"):
                error = preview["error"]
            mapping = preview.get("mapping") or mapping

    return render_template(
        "crm_external_import.html",
        sources=sources,
        preview=preview,
        error=error,
        result=result,
        csv_text=csv_text,
        mapping=mapping,
        mapping_json=json.dumps(mapping),
        source_id=source_id or "",
        **_nav(user, "leads"),
    )


@external_leads_bp.route("/crm/leads/<int:lead_id>/consent", methods=["GET", "POST"])
def lead_consent_page(lead_id):
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("subscriber_app"))
    lead = db.get_lead(lead_id, user["id"])
    if not lead:
        return redirect(url_for("crm.crm_leads_page"))
    error = None
    if request.method == "POST":
        action = (request.form.get("action") or "confirm").strip()
        if action == "evidence":
            upload_ref, upload_err = save_evidence_upload(
                user["id"], request.files.get("evidence_file")
            )
            if upload_err:
                error = upload_err
            else:
                xdb.create_consent_evidence(
                    user["id"],
                    lead_id,
                    {
                        "consent_status": "pending",
                        "consent_method": request.form.get("consent_method") or "other",
                        "source_provider": request.form.get("source_provider"),
                        "source_url": request.form.get("source_url"),
                        "consent_at": request.form.get("consent_at"),
                        "phone_number": lead.get("phone_number"),
                        "disclosure_text": request.form.get("disclosure_text"),
                        "evidence_type": request.form.get("evidence_type") or "other",
                        "upload_ref": upload_ref,
                        "notes": request.form.get("notes"),
                        "authorized_agent_name": request.form.get("authorized_agent_name"),
                        "authorized_brokerage_name": request.form.get("authorized_brokerage_name"),
                    },
                )
                flash("Evidence saved as pending. Confirm qualifying consent to enable SMS.")
                return redirect(url_for("external_leads.lead_consent_page", lead_id=lead_id))
        elif action == "confirm":
            _result, err = confirm_qualifying_consent(
                user["id"],
                lead_id,
                request.form,
                file_storage=request.files.get("evidence_file"),
            )
            if err:
                error = err
            else:
                flash("Consent verified. SMS sending is enabled for this lead.")
                return redirect(url_for("crm.crm_lead_detail_page", lead_id=lead_id))
        elif action == "not_permitted":
            xdb.set_lead_sms_consent_state(
                lead_id,
                user["id"],
                sms_consent_status="not_permitted",
                sms_sending_blocked=True,
                actor_user_id=user["id"],
                source="agent",
            )
            flash("Marked not permitted for SMS.")
            return redirect(url_for("crm.crm_lead_detail_page", lead_id=lead_id))
        elif action == "opt_out":
            xdb.apply_opt_out_consent(
                lead_id, user["id"], actor_user_id=user["id"], source="agent"
            )
            flash("Opt-out recorded. SMS remains blocked.")
            return redirect(url_for("crm.crm_lead_detail_page", lead_id=lead_id))

    evidence = xdb.list_consent_evidence(user["id"], lead_id)
    audit = xdb.list_consent_audit(user["id"], lead_id)
    return render_template(
        "crm_lead_consent.html",
        lead=lead,
        evidence=evidence,
        audit=audit,
        error=error,
        consent_methods=CONSENT_METHODS,
        evidence_types=EVIDENCE_TYPES,
        attestation=CONSENT_CONFIRMATION_STATEMENT,
        verbal_script=VERBAL_CONSENT_SCRIPT,
        status_label=status_label,
        **_nav(user, "leads"),
    )


@external_leads_bp.route("/crm/leads/<int:lead_id>/consent/confirm", methods=["POST"])
def lead_consent_confirm(lead_id):
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("subscriber_app"))
    _result, err = confirm_qualifying_consent(
        user["id"], lead_id, request.form, file_storage=request.files.get("evidence_file")
    )
    if err:
        flash(err, "error")
        return redirect(url_for("external_leads.lead_consent_page", lead_id=lead_id))
    flash("Consent verified. SMS sending is enabled for this lead.")
    return redirect(url_for("crm.crm_lead_detail_page", lead_id=lead_id))


@external_leads_bp.route("/crm/leads/<int:lead_id>/consent/evidence", methods=["POST"])
def lead_consent_evidence(lead_id):
    user = _user_or_redirect()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    lead = db.get_lead(lead_id, user["id"])
    if not lead:
        return jsonify({"error": "Lead not found"}), 404
    upload_ref, upload_err = save_evidence_upload(user["id"], request.files.get("evidence_file"))
    if upload_err:
        return jsonify({"error": upload_err}), 400
    eid = xdb.create_consent_evidence(
        user["id"],
        lead_id,
        {
            "consent_status": "pending",
            "consent_method": request.form.get("consent_method") or "other",
            "source_provider": request.form.get("source_provider"),
            "source_url": request.form.get("source_url"),
            "consent_at": request.form.get("consent_at"),
            "phone_number": lead.get("phone_number"),
            "disclosure_text": request.form.get("disclosure_text"),
            "evidence_type": request.form.get("evidence_type") or "other",
            "upload_ref": upload_ref,
            "notes": request.form.get("notes"),
        },
    )
    return jsonify({"ok": True, "evidence_id": eid}), 201


@external_leads_bp.route("/crm/leads/<int:lead_id>/consent/not-permitted", methods=["POST"])
def lead_consent_not_permitted(lead_id):
    user = _user_or_redirect()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    if not db.get_lead(lead_id, user["id"]):
        return jsonify({"error": "Lead not found"}), 404
    xdb.set_lead_sms_consent_state(
        lead_id,
        user["id"],
        sms_consent_status="not_permitted",
        sms_sending_blocked=True,
        actor_user_id=user["id"],
        source="agent",
    )
    return jsonify({"ok": True})


@external_leads_bp.route("/crm/leads/<int:lead_id>/consent/opt-out", methods=["POST"])
def lead_consent_opt_out(lead_id):
    user = _user_or_redirect()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    if not db.get_lead(lead_id, user["id"]):
        return jsonify({"error": "Lead not found"}), 404
    xdb.apply_opt_out_consent(lead_id, user["id"], actor_user_id=user["id"], source="agent")
    return jsonify({"ok": True})


@external_leads_bp.route("/crm/leads/<int:lead_id>/claim", methods=["POST"])
def lead_claim(lead_id):
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("subscriber_app"))
    lead, err = xdb.claim_lead(lead_id, user["id"])
    if err:
        if _wants_json():
            return jsonify({"error": err}), 400
        flash(err, "error")
        return redirect(url_for("crm.crm_lead_detail_page", lead_id=lead_id))
    msg = "Lead claimed. Claiming does not grant permission to text."
    if _wants_json():
        return jsonify({"ok": True, "lead": lead, "message": msg})
    flash(msg)
    return redirect(url_for("crm.crm_lead_detail_page", lead_id=lead_id))


@external_leads_bp.route("/crm/consent-uploads/<path:upload_ref>")
def consent_upload_download(upload_ref):
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("subscriber_app"))
    path = resolve_upload_path(user["id"], upload_ref)
    if not path:
        return "Not found", 404
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))
