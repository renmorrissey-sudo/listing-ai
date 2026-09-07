"""Web dashboard for external lead source funnel and ROI metrics."""

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from flask import Blueprint, flash, redirect, render_template, request, url_for

import auth
import source_roi_db


source_roi_bp = Blueprint("source_roi", __name__)


def _user_or_redirect():
    user = auth.get_current_user()
    if not user or not auth.user_has_active_subscription(user):
        return None
    return user


def _money(cents):
    return f"${int(round(cents or 0)) / 100:,.0f}"


def _parse_cents(value, label):
    try:
        amount = Decimal((value or "").replace(",", "").replace("$", "").strip())
    except InvalidOperation:
        return None, f"Enter a valid {label}."
    if not amount.is_finite():
        return None, f"Enter a valid {label}."
    cents = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return cents, None


@source_roi_bp.route("/crm/source-roi")
def source_roi_dashboard():
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("subscriber_app"))
    dashboard = source_roi_db.get_source_roi_dashboard(user["id"])
    total_cost = dashboard["total_cost_cents"]
    dashboard["total_roi_multiple"] = (
        dashboard["total_gci_cents"] / total_cost if total_cost else None
    )
    return render_template(
        "source_roi_dashboard.html",
        dashboard=dashboard,
        today=date.today().isoformat(),
        money=_money,
        email=user["email"],
        has_billing_portal=bool(user.get("stripe_customer_id")),
        active_nav="source-roi",
        product_name="TopAI Real Estate Tools",
    )


@source_roi_bp.route("/crm/source-roi/spend", methods=["POST"])
def add_source_spend():
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("subscriber_app"))
    try:
        source_id = int(request.form.get("source_id") or 0)
    except ValueError:
        source_id = 0
    amount_cents, error = _parse_cents(request.form.get("amount"), "marketing cost")
    spent_on = (request.form.get("spent_on") or "").strip()
    try:
        date.fromisoformat(spent_on)
    except ValueError:
        error = error or "Enter a valid spend date."
    if not source_id:
        error = error or "Select a lead source."
    if error:
        flash(error, "error")
    else:
        _, error = source_roi_db.add_source_spend(
            user["id"], source_id, amount_cents, spent_on, request.form.get("notes")
        )
        flash(error or "Marketing cost added.", "error" if error else "success")
    return redirect(url_for("source_roi.source_roi_dashboard"), code=303)


@source_roi_bp.route("/crm/source-roi/spend/<int:spend_id>/delete", methods=["POST"])
def delete_source_spend(spend_id):
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("subscriber_app"))
    deleted = source_roi_db.delete_source_spend(user["id"], spend_id)
    flash("Marketing cost removed." if deleted else "Marketing cost not found.", "success" if deleted else "error")
    return redirect(url_for("source_roi.source_roi_dashboard"), code=303)

@source_roi_bp.route("/crm/source-roi/gci", methods=["POST"])
def update_lead_gci():
    user = _user_or_redirect()
    if not user:
        return redirect(url_for("subscriber_app"))
    try:
        lead_id = int(request.form.get("lead_id") or 0)
    except ValueError:
        lead_id = 0
    amount_cents, error = _parse_cents(request.form.get("amount"), "GCI amount")
    if not lead_id:
        error = error or "Select a closed lead."
    if error:
        flash(error, "error")
    else:
        _, error = source_roi_db.set_lead_gci(user["id"], lead_id, amount_cents)
        flash(error or "Closed-lead GCI updated.", "error" if error else "success")
    return redirect(url_for("source_roi.source_roi_dashboard"), code=303)
