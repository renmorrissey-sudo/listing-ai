"""Subscriber CMA builder and saved report pages."""

from flask import Blueprint, jsonify, make_response, redirect, render_template, request, url_for

import auth
import cma_db
import db
from cma_service import CMAValidationError, build_cma
from cma_property_search import ComparableSearchError, search_sold_comparables


cma_bp = Blueprint("cma", __name__)


def _money(value):
    return f"${float(value or 0):,.0f}"


def _number(value):
    return f"{int(value or 0):,}"


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
            automatic_comps = search_sold_comparables(payload)
            manual_comps = payload.get("comparables") if isinstance(payload.get("comparables"), list) else []
            payload["comparables"] = automatic_comps + manual_comps
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
        money=_money,
        number=_number,
    )
