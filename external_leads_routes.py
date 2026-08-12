"""CRM routes for external lead sources, ingest, consent, and pond claim."""

from __future__ import annotations

import csv
import json
import logging
import os
import re

from flask import (
    Blueprint,
    Response,
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
    sms_consent_label,
    status_label,
)
from external_leads.consent_workflow import (
    confirm_qualifying_consent,
    resolve_upload_path,
    save_evidence_upload,
)
from external_leads.csv_import import (
    CSV_MAX_BYTES,
    commit_csv,
    decode_csv_bytes,
    preview_csv,
    sample_csv_text,
    suggest_mapping,
    validate_csv_filename,
)
from external_leads.duplicates import find_duplicate
from external_leads.ingest import ingest_external_lead
from external_leads.webhook import generate_webhook_secret, hash_webhook_secret
from lead_service import normalize_phone_e164

# Popular real-estate lead providers with zero direct API/OAuth/webhook-vendor
# integration in this codebase today (see external_leads/adapters.py stubs).
# Shown as disabled "Coming soon" cards — never represented as connectable.
POPULAR_LEAD_PROVIDERS = [
    {"key": "zillow", "name": "Zillow Premier Agent"},
    {"key": "realtor_com", "name": "Realtor.com / ReadyConnect"},
    {"key": "homes_com", "name": "Homes.com"},
    {"key": "cinc", "name": "CINC"},
    {"key": "real_geeks", "name": "Real Geeks"},
    {"key": "market_leader", "name": "Market Leader"},
    {"key": "smartzip", "name": "SmartZip"},
    {"key": "redx", "name": "REDX"},
    {"key": "meta_lead_ads", "name": "Meta / Facebook Lead Ads"},
    {"key": "google_lead_forms", "name": "Google Lead Forms"},
]

logger = logging.getLogger(__name__)

external_leads_bp = Blueprint("external_leads", __name__)


def _user_or_redirect():
    user = auth.get_current_user()
    if not user or not auth.user_has_active_subscription(user):
        return None
    return user


def _unauth_redirect():
    """Send anonymous users to login with next=; unpaid users to subscribe/app."""
    user = auth.get_current_user()
    if not user:
        nxt = request.path
        if request.query_string:
            nxt = f"{request.path}?{request.query_string.decode()}"
        return redirect(url_for("login", next=auth.safe_next_url(nxt)))
    return redirect(url_for("subscriber_app"))


def _nav(user, active="leads"):
    return {
        "email": user["email"],
        "has_billing_portal": bool(user.get("stripe_customer_id")),
        "needs_billing_attention": auth.user_needs_billing_attention(user),
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


def _last_lead_received_at(user_id, source_id):
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT MAX(created_at) AS last_at FROM leads WHERE user_id = ? AND external_source_id = ?",
            (user_id, source_id),
        ).fetchone()
        return (row or {}).get("last_at") if row else None


@external_leads_bp.route("/crm/external-sources", methods=["GET", "POST"])
def external_sources_page():
    user = _user_or_redirect()
    if not user:
        return _unauth_redirect()
    error = None
    created_secret = None
    prefill_name = request.args.get("name") or ""
    prefill_provider_key = request.args.get("provider_key") or ""
    prefill_category = request.args.get("category") or ""
    prefill_method = request.args.get("import_method") or ""
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
    connected_sources = []
    for source in sources:
        item = dict(source)
        item["last_lead_received_at"] = _last_lead_received_at(user["id"], source["id"])
        connected_sources.append(item)
    return render_template(
        "crm_external_sources.html",
        sources=connected_sources,
        categories=EXTERNAL_SOURCE_CATEGORIES,
        pond_statuses=POND_STATUSES,
        popular_providers=POPULAR_LEAD_PROVIDERS,
        error=error,
        created_secret=created_secret,
        prefill_name=prefill_name,
        prefill_provider_key=prefill_provider_key,
        prefill_category=prefill_category,
        prefill_method=prefill_method,
        **_nav(user, "leads"),
    )


@external_leads_bp.route("/crm/external-sources/<int:source_id>", methods=["GET", "POST"])
def external_source_detail(source_id):
    """Manage a single lead source: view config, rotate webhook secret."""
    user = _user_or_redirect()
    if not user:
        return _unauth_redirect()
    source = xdb.get_external_lead_source(source_id, user["id"])
    if not source:
        flash("Source not found.", "error")
        return redirect(url_for("external_leads.external_sources_page"))
    rotated_secret = None
    error = None
    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        if action == "rotate_secret":
            if source.get("import_method") not in {"webhook", "api"}:
                error = "Only webhook/API sources have a secret to rotate."
            else:
                raw_secret = generate_webhook_secret()
                xdb.update_source_webhook_secret(source_id, user["id"], hash_webhook_secret(raw_secret))
                rotated_secret = raw_secret
                flash("Webhook secret rotated. Copy it now — it will not be shown again.")
                source = xdb.get_external_lead_source(source_id, user["id"])
        else:
            error = "Unknown action."
    return render_template(
        "crm_external_source_detail.html",
        source=source,
        rotated_secret=rotated_secret,
        error=error,
        last_lead_received_at=_last_lead_received_at(user["id"], source_id),
        **_nav(user, "leads"),
    )


@external_leads_bp.route("/api/crm/leads", methods=["POST"])
def api_create_lead():
    """JSON create/find-or-update endpoint for the New Lead drawer.

    Thin wrapper around ingest_external_lead — same tenant-scoped duplicate
    detection, safe consent defaults, and source attribution as CSV/webhook.
    """
    user = _user_or_redirect()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}

    source_id = data.get("external_source_id")
    source_row = None
    if source_id:
        try:
            source_row = xdb.get_external_lead_source(int(source_id), user["id"])
        except (TypeError, ValueError):
            source_row = None

    payload = {
        "first_name": data.get("first_name"),
        "last_name": data.get("last_name"),
        "phone": data.get("phone"),
        "email": data.get("email"),
        "notes": data.get("notes"),
        "lead_type": data.get("lead_type"),
        "status": data.get("status"),
        "pond_status": "claimable",
    }
    result = ingest_external_lead(
        user["id"],
        payload,
        source_row=source_row,
        method="manual",
        actor_user_id=user["id"],
    )
    if result.get("error"):
        return jsonify({"error": result["error"]}), 400

    lead_id = result["lead_id"]
    return (
        jsonify(
            {
                "ok": True,
                "lead_id": lead_id,
                "created": result.get("action") == "created",
                "duplicate": result.get("action") in {"updated", "skipped_opted_out"},
                "duplicate_match": result.get("duplicate_match"),
                "redirect_url": url_for("crm.crm_lead_detail_page", lead_id=lead_id),
            }
        ),
        201 if result.get("action") == "created" else 200,
    )


@external_leads_bp.route("/api/crm/leads/check-duplicate", methods=["GET"])
def api_check_duplicate_lead():
    """Tenant-scoped pre-check so the New Lead drawer can warn before submit."""
    user = _user_or_redirect()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    phone_raw = request.args.get("phone") or ""
    phone = normalize_phone_e164(phone_raw)
    if not phone:
        return jsonify({"duplicate": False})
    existing, match = find_duplicate(user["id"], phone=phone)
    if not existing:
        return jsonify({"duplicate": False})
    return jsonify(
        {
            "duplicate": True,
            "match": match,
            "lead_id": existing["id"],
            "name": existing.get("name") or "Lead",
            "phone_number": existing.get("phone_number"),
            "url": url_for("crm.crm_lead_detail_page", lead_id=existing["id"]),
        }
    )


@external_leads_bp.route("/crm/external-leads/new", methods=["GET", "POST"])
def external_lead_new():
    user = _user_or_redirect()
    if not user:
        return _unauth_redirect()
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
                "Lead saved as Unverified + SMS Blocked. "
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


@external_leads_bp.route("/crm/external-leads/import/sample.csv")
def external_lead_import_sample():
    user = _user_or_redirect()
    if not user:
        return _unauth_redirect()
    return Response(
        sample_csv_text(),
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="external-leads-sample.csv"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@external_leads_bp.route("/crm/external-leads/import", methods=["GET", "POST"])
def external_lead_import():
    # Page view / import does not require Telnyx verification or SMS campaign workers.
    user = _user_or_redirect()
    if not user:
        return _unauth_redirect()
    sources = xdb.list_external_lead_sources(user["id"], active_only=True)
    batches = []
    try:
        with db.get_db() as conn:
            rows = conn.execute(
                """
                SELECT id, filename, created_count, updated_count, skipped_count,
                       invalid_count, pending_evidence_count, created_at
                FROM external_lead_import_batches
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT 10
                """,
                (user["id"],),
            ).fetchall()
            batches = [dict(r) for r in rows]
    except Exception:
        logger.exception("import history unavailable user=%s", user["id"])
        batches = []

    preview = None
    error = None
    result = None
    csv_text = ""
    mapping = {}
    source_id = request.form.get("external_source_id") or request.args.get("external_source_id")
    duplicate_mode = (request.form.get("duplicate_mode") or "skip").strip().lower()
    if duplicate_mode not in {"skip", "update"}:
        duplicate_mode = "skip"

    if request.method == "POST":
        action = (request.form.get("action") or "preview").strip()
        upload = request.files.get("csv_file")
        csv_text = request.form.get("csv_text") or ""
        if upload and upload.filename:
            fname_err = validate_csv_filename(upload.filename)
            if fname_err:
                error = fname_err
            else:
                raw = upload.read(CSV_MAX_BYTES + 1)
                decoded, decode_err = decode_csv_bytes(raw)
                if decode_err:
                    error = decode_err
                else:
                    csv_text = decoded or ""
        mapping_raw = request.form.get("mapping_json") or "{}"
        try:
            mapping = json.loads(mapping_raw) if mapping_raw else {}
        except json.JSONDecodeError:
            mapping = {}
        if not mapping and csv_text:
            headers = next(csv.reader([csv_text.splitlines()[0]])) if csv_text.splitlines() else []
            mapping = suggest_mapping([h.strip() for h in headers])

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
                filename=(upload.filename if upload and upload.filename else None),
                actor_user_id=user["id"],
                duplicate_mode=duplicate_mode,
            )
            if result.get("error"):
                error = result["error"]
            else:
                flash(
                    f"Import finished: {result.get('created', 0)} created, "
                    f"{result.get('updated', 0)} updated, "
                    f"{result.get('skipped', 0)} skipped, "
                    f"{result.get('invalid', 0)} invalid. "
                    "All external leads remain Unverified + Blocked. No SMS was sent."
                )
        elif not error:
            preview = preview_csv(
                csv_text,
                mapping=mapping,
                user_id=user["id"],
                duplicate_mode=duplicate_mode,
            )
            if preview.get("error"):
                error = preview["error"]
            mapping = preview.get("mapping") or mapping

    return render_template(
        "crm_external_import.html",
        sources=sources,
        batches=batches,
        preview=preview,
        error=error,
        result=result,
        csv_text=csv_text,
        mapping=mapping,
        mapping_json=json.dumps(mapping),
        source_id=source_id or "",
        duplicate_mode=duplicate_mode,
        max_upload_mb=CSV_MAX_BYTES // (1024 * 1024),
        **_nav(user, "leads"),
    )


@external_leads_bp.route("/crm/leads/<int:lead_id>/consent", methods=["GET", "POST"])
def lead_consent_page(lead_id):
    user = _user_or_redirect()
    if not user:
        return _unauth_redirect()
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
        sms_consent_label=sms_consent_label,
        **_nav(user, "leads"),
    )


@external_leads_bp.route("/crm/leads/<int:lead_id>/consent/confirm", methods=["POST"])
def lead_consent_confirm(lead_id):
    user = _user_or_redirect()
    if not user:
        return _unauth_redirect()
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
        return _unauth_redirect()
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
        return _unauth_redirect()
    path = resolve_upload_path(user["id"], upload_ref)
    if not path:
        return "Not found", 404
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))
