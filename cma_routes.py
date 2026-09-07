"""Subscriber CMA builder and saved report pages."""

from io import BytesIO
import re

from flask import (
    Blueprint,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

import auth
import cma_db
import db
from cma_pdf import render_cma_pdf
from lead_email_service import send_direct_email
from cma_service import CMAValidationError, build_cma
from cma_property_search import ComparableSearchError, search_market_comparables


cma_bp = Blueprint("cma", __name__)


def _money(value):
    return f"${float(value or 0):,.0f}"


def _number(value):
    return f"{int(value or 0):,}"


def _pdf_filename(report):
    address = re.sub(r"[^A-Za-z0-9]+", "-", str(report.get("subject_address") or "property")).strip("-")
    return f"CMA-{address or 'property'}.pdf"


def _email_template(user_id, report):
    profile = db.get_business_profile(user_id) or {}
    recipient = str(report.get("prospect_name") or "").strip().split(" ")[0] or "there"
    agent_name = str(profile.get("agent_name") or "").strip()
    brokerage = str(profile.get("brokerage_name") or profile.get("company_name") or "").strip()
    analysis = report.get("analysis") or {}
    body = (
        f"Hi {recipient},\n\n"
        f"I've prepared a comparative market analysis for {report.get('subject_address')}, "
        f"{report.get('city')}, {report.get('state')}.\n\n"
        f"The current automated market estimate is {_money(analysis.get('indicated_value'))}, "
        f"with an indicated range of {_money(analysis.get('indicated_range_low'))} to "
        f"{_money(analysis.get('indicated_range_high'))}.\n\n"
        "The attached report includes the selected comparable market evidence, property details, "
        "valuation methodology, and important verification disclosures. Please review it, and let "
        "me know if you'd like to discuss the property's condition, upgrades, or pricing strategy."
    )
    signature = [value for value in (agent_name, brokerage) if value]
    if signature:
        body += "\n\nBest,\n" + "\n".join(signature)
    return {
        "subject": f"Comparative Market Analysis - {report.get('subject_address')}",
        "body": body,
    }

def _page_user():
    user = auth.get_current_user()
    if not user or not auth.user_has_active_subscription(user):
        return None
    return user


@cma_bp.route("/cma")
def builder():
    user = _page_user()
    if not user:
        return redirect(url_for("subscriber_app"))
    response = make_response(
        render_template(
            "cma_builder.html",
            email=user["email"],
            has_billing_portal=bool(user.get("stripe_customer_id")),
            active_nav="cma",
            recent_reports=cma_db.list_recent(user["id"]),
        )
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@cma_bp.route("/api/cma/reports", methods=["POST"])
@auth.subscription_required
def create_report():
    try:
        payload = request.get_json(silent=True) or {}
        if payload.get("automatic_search"):
            if payload.get("records_provider_disclosure_accepted") is not True:
                raise CMAValidationError(
                    "Acknowledge the RentCast records-provider disclosure before automatic search."
                )
            payload = dict(payload)
            market_data = search_market_comparables(payload)
            manual_comps = payload.get("comparables") if isinstance(payload.get("comparables"), list) else []
            payload["comparables"] = market_data["comparables"] + manual_comps
            payload["market_valuation"] = market_data["valuation"]
        report = build_cma(payload)
        saved = cma_db.create_report(auth.get_current_user()["id"], report)
        db.record_tool_usage(auth.get_current_user()["id"], "cma_generator", "generated")
    except ComparableSearchError as exc:
        return jsonify({"error": str(exc)}), 503
    except CMAValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(
        {
            "report_id": saved["id"],
            "detail_url": url_for("cma.report_detail", report_id=saved["id"]),
            "analysis": saved["analysis"],
        }
    ), 201


@cma_bp.route("/cma/reports/<int:report_id>")
def report_detail(report_id):
    user = _page_user()
    if not user:
        return redirect(url_for("subscriber_app"))
    report = cma_db.get_report(user["id"], report_id)
    if not report:
        return render_template(
            "error.html",
            title="CMA report not found",
            message="This report does not exist or belongs to another account.",
        ), 404
    return render_template(
        "cma_report.html",
        email=user["email"],
        has_billing_portal=bool(user.get("stripe_customer_id")),
        active_nav="cma",
        report=report,
        email_template=_email_template(user["id"], report),
        pdf_filename=_pdf_filename(report),
        money=_money,
        number=_number,
    )


@cma_bp.route("/cma/reports/<int:report_id>/pdf")
def report_pdf(report_id):
    user = _page_user()
    if not user:
        return redirect(url_for("subscriber_app"))
    report = cma_db.get_report(user["id"], report_id)
    if not report:
        return jsonify({"error": "CMA report not found."}), 404
    pdf_bytes = render_cma_pdf(report)
    response = send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=_pdf_filename(report),
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@cma_bp.route("/api/cma/reports/<int:report_id>/email", methods=["POST"])
@auth.subscription_required
def email_report(report_id):
    user = auth.get_current_user()
    report = cma_db.get_report(user["id"], report_id)
    if not report:
        return jsonify({"error": "CMA report not found."}), 404
    payload = request.get_json(silent=True) or {}
    pdf_bytes = render_cma_pdf(report)
    result, error = send_direct_email(
        user["id"],
        to_email=payload.get("to_email"),
        subject=payload.get("subject"),
        body=payload.get("body"),
        attachments=[
            {
                "filename": _pdf_filename(report),
                "content_type": "application/pdf",
                "content": pdf_bytes,
            }
        ],
    )
    if error:
        return jsonify({"error": error}), 400
    db.record_tool_usage(user["id"], "cma_email", "sent")
    return jsonify(
        {
            "ok": True,
            "to_email": result["to_email"],
            "provider": result["provider"],
            "message": f"CMA report emailed to {result['to_email']}.",
        }
    )
