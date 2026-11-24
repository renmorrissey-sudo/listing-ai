"""Message Templates: agent-approved outbound message templates.

Phase III-5. The agent creates templates (draft), approves them, and
archives them here. The AI has NO route or tool to create or approve
templates — it can only USE an approved template (or have one used
for it) via the server-side compose path. Approval attaches to the
exact text: any edit resets an approved template to draft.

Mirrors the calendar_routes.py shape: an auth gate, a list page, and
POST actions that flash and redirect. Cross-tenant access returns
404 (never reveals another tenant's template).
"""

from __future__ import annotations

import logging

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

import auth
import config
from harness import templates_db
from harness.templates_db import TemplateError

logger = logging.getLogger(__name__)

templates_bp = Blueprint("templates", __name__)


def _auth_gate():
    user = auth.get_current_user()
    if not user:
        nxt = request.path
        if request.query_string:
            nxt = f"{request.path}?{request.query_string.decode()}"
        return None, redirect(url_for("login", next=nxt))
    if config.SUBSCRIPTION_REQUIRED and not auth.user_has_active_subscription(user):
        return None, redirect(url_for("subscribe"))
    return user, None


@templates_bp.route("/harness/templates", methods=["GET"])
def templates_page():
    user, resp = _auth_gate()
    if resp:
        return resp
    templates = templates_db.list_templates(user["id"])
    return render_template(
        "message_templates.html",
        email=user["email"],
        has_billing_portal=bool(user.get("stripe_customer_id")),
        active_nav="message-templates",
        product_name=config.PRODUCT_NAME,
        templates=templates,
        slot_allowlist=sorted(templates_db.SLOT_ALLOWLIST),
        max_body_chars=templates_db.MAX_BODY_CHARS,
    )


@templates_bp.route("/harness/templates", methods=["POST"])
def templates_create():
    user, resp = _auth_gate()
    if resp:
        return resp
    try:
        row = templates_db.create_template(
            user["id"],
            name=request.form.get("name") or "",
            channel=request.form.get("channel") or "sms",
            purpose=request.form.get("purpose") or "",
            body=request.form.get("body") or "",
        )
    except TemplateError as exc:
        flash(str(exc.message), "error")
        return redirect(url_for("templates.templates_page"))
    flash(f'Template "{row["name"]}" created as a draft.', "success")
    return redirect(url_for("templates.templates_page"))


def _template_or_404(user_id, template_id):
    row = templates_db.get_template(user_id, template_id)
    if row is None:
        abort(404)
    return row


@templates_bp.route("/harness/templates/<int:template_id>/approve", methods=["POST"])
def templates_approve(template_id):
    user, resp = _auth_gate()
    if resp:
        return resp
    _template_or_404(user["id"], template_id)
    try:
        row = templates_db.approve_template(
            user["id"], template_id, approved_by=user["id"]
        )
    except TemplateError as exc:
        flash(str(exc.message), "error")
        return redirect(url_for("templates.templates_page"))
    flash(f'Template "{row["name"]}" approved.', "success")
    return redirect(url_for("templates.templates_page"))


@templates_bp.route("/harness/templates/<int:template_id>/archive", methods=["POST"])
def templates_archive(template_id):
    user, resp = _auth_gate()
    if resp:
        return resp
    _template_or_404(user["id"], template_id)
    row = templates_db.archive_template(user["id"], template_id)
    flash(f'Template "{row["name"]}" archived.', "success")
    return redirect(url_for("templates.templates_page"))


@templates_bp.route("/harness/templates/<int:template_id>/unarchive", methods=["POST"])
def templates_unarchive(template_id):
    user, resp = _auth_gate()
    if resp:
        return resp
    _template_or_404(user["id"], template_id)
    try:
        row = templates_db.unarchive_template(user["id"], template_id)
    except TemplateError as exc:
        flash(str(exc.message), "error")
        return redirect(url_for("templates.templates_page"))
    flash(f'Template "{row["name"]}" moved back to draft.', "success")
    return redirect(url_for("templates.templates_page"))


@templates_bp.route("/harness/templates/<int:template_id>/edit", methods=["POST"])
def templates_edit(template_id):
    user, resp = _auth_gate()
    if resp:
        return resp
    _template_or_404(user["id"], template_id)
    try:
        row = templates_db.edit_template(
            user["id"],
            template_id,
            name=request.form.get("name"),
            channel=request.form.get("channel"),
            purpose=request.form.get("purpose"),
            body=request.form.get("body"),
        )
    except TemplateError as exc:
        flash(str(exc.message), "error")
        return redirect(url_for("templates.templates_page"))
    flash(
        f'Template "{row["name"]}" updated and reset to draft — '
        "approve it again before the AI can use it.",
        "success",
    )
    return redirect(url_for("templates.templates_page"))


@templates_bp.route("/api/harness/templates", methods=["GET"])
def templates_api():
    """JSON list of the tenant's templates (agent-facing read)."""
    user, resp = _auth_gate()
    if resp:
        return resp
    templates = templates_db.list_templates(user["id"])
    return jsonify(
        {
            "templates": [
                {
                    "id": t["id"],
                    "name": t["name"],
                    "channel": t["channel"],
                    "purpose": t["purpose"],
                    "body": t["body"],
                    "status": t["status"],
                    "approved_by": t.get("approved_by"),
                    "approved_at": t.get("approved_at"),
                    "created_at": t.get("created_at"),
                    "updated_at": t.get("updated_at"),
                }
                for t in templates
            ]
        }
    )
