import logging
import re
import secrets

import stripe
from anthropic import Anthropic
from flask import Flask, jsonify, redirect, render_template, request, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.exceptions import HTTPException

import auth
import config
import crm_db
import db
from datetime import datetime, timedelta, timezone

import sms_coach
from crm import crm_bp
from crm_constants import status_label
from sms_prompts import build_inbound_reply_analysis_prompt, build_sms_prompt
from sms_provider import SmsProviderError, get_sms_provider, sms_status_callback_url
from sms_validation import (
    validate_e164_phone,
    validate_sms_generate_payload,
    validate_sms_send_payload,
    validate_sms_test_payload,
)
from twilio_security import validate_twilio_request
from validation import validate_listing_payload, validate_script_payload
from voice_prompts import build_voice_call_prompt
from voice_provider import (
    VoiceProviderError,
    build_vapi_variable_values,
    get_voice_provider,
    log_variable_values_presence,
    normalize_voice_webhook,
    validate_vapi_variable_values,
)
from voice_validation import validate_voice_call_payload, validate_voice_persona_payload

config.validate_config()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["SECRET_KEY"] = config.FLASK_SECRET_KEY
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = config.IS_PRODUCTION
app.config["PERMANENT_SESSION_LIFETIME"] = 86400 * 14

db.init_db()
app.register_blueprint(crm_bp)
client = Anthropic(api_key=config.ANTHROPIC_API_KEY)

if config.STRIPE_SECRET_KEY:
    stripe.api_key = config.STRIPE_SECRET_KEY

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day"],
    storage_uri="memory://",
)


@app.context_processor
def inject_business_context():
    return {
        "business_name": config.BUSINESS_NAME,
        "product_name": config.PRODUCT_NAME,
        "contact_email": config.CONTACT_EMAIL,
        "subscription_price": config.SUBSCRIPTION_PRICE,
        "trial_offer": config.TRIAL_OFFER,
    }


def _user_rate_limit_key():
    user = auth.get_current_user()
    if user:
        return f"user:{user['id']}"
    return get_remote_address()


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if config.IS_PRODUCTION:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.errorhandler(HTTPException)
def handle_http_exception(error):
    api_paths = ("/generate", "/generate-script", "/verify", "/session-status", "/webhook", "/voice", "/sms")
    if request.path.startswith(api_paths):
        return jsonify({"error": error.description}), error.code
    return error


@app.errorhandler(Exception)
def handle_unexpected_exception(error):
    logger.exception("Unhandled error: %s", error)
    return jsonify({"error": "Something went wrong. Please try again."}), 500


def build_listing_prompt(data):
    features = data.get("features", "").strip()
    feature_line = f"\n- Additional features: {features}" if features else ""
    return f"""You are an expert real estate copywriter. Generate compelling, professional content for this property listing.

PROPERTY DETAILS:
- Address: {data.get("address", "N/A")}
- Price: ${data.get("price", "N/A")}
- Bedrooms: {data.get("beds", "N/A")}
- Bathrooms: {data.get("baths", "N/A")}
- Square footage: {data.get("sqft", "N/A")} sq ft
- Year built: {data.get("year_built", "N/A")}
- Garage: {data.get("garage", "None")}
- Pool: {data.get("pool", "No")}{feature_line}
- Neighborhood highlights: {data.get("neighborhood", "N/A")}

Generate exactly three sections, clearly labeled:

---LISTING DESCRIPTION---
Write a compelling MLS listing description (150-200 words). Start with a strong hook. Highlight the best features. End with a call to action. Do NOT use the word "nestled" or "stunning".

---SOCIAL POSTS---
Write 3 social media posts:
1. [INSTAGRAM] (~150 chars with 5 relevant hashtags)
2. [FACEBOOK] (2-3 sentences, conversational tone, include price)
3. [X/TWITTER] (~200 chars punchy and direct)

---PROSPECT EMAIL---
Write a short email (subject line + 3 paragraphs) to send to a prospect list announcing this listing. Professional but warm tone. Include a clear call to action to schedule a showing."""


def build_script_prompt(data):
    return f"""You are an expert real estate sales coach. Generate a professional cold call script for a real estate agent.

DETAILS:
- Target area: {data.get("area", "N/A")}
- Property type: {data.get("property_type", "Single Family")}
- Seller situation: {data.get("situation", "General Farming")}
- Agent name: {data.get("agent_name", "your agent")}
- Key benefit to mention: {data.get("key_benefit", "top market prices and fast closings")}

Generate exactly three sections, clearly labeled:

---OPENING SCRIPT---
Write a natural, confident cold call opening (about 100 words). Include a strong hook, quick value proposition, and a soft question to engage the seller. Sound human, not robotic.

---OBJECTION HANDLERS---
Write responses to these 3 common objections:
1. "I'm not interested."
2. "I already have an agent." (or "I'm listed.")
3. "What's my home worth?"
Each response should be 2-4 sentences, confident but not pushy.

---VOICEMAIL SCRIPT---
Write a 20-second voicemail script that sounds natural and gets a callback. Include agent name and a specific reason to call back."""


def _extract_section(text, start_marker, end_marker):
    start = text.find(f"---{start_marker}---")
    if start == -1:
        return ""
    start += len(f"---{start_marker}---")
    if end_marker:
        end = text.find(f"---{end_marker}---", start)
        return text[start:end] if end != -1 else text[start:]
    return text[start:]


def _stripe_status_from_subscription(subscription):
    status = subscription.get("status", "none")
    if status in ("active", "trialing"):
        return "active"
    if status in ("canceled", "unpaid", "incomplete_expired"):
        return "canceled"
    return status


def _stripe_customer_for_email(email):
    if not config.STRIPE_SECRET_KEY:
        return None
    customers = stripe.Customer.list(email=email, limit=1)
    return customers.data[0] if customers.data else None


def _stripe_has_active_subscription(email):
    customer = _stripe_customer_for_email(email)
    if not customer:
        return False
    for status in ("active", "trialing"):
        subs = stripe.Subscription.list(customer=customer.id, status=status, limit=1)
        if subs.data:
            return True
    return False


def _sync_user_from_stripe(user, email):
    if not config.STRIPE_SECRET_KEY:
        return
    customer = _stripe_customer_for_email(email)
    if not customer:
        return
    db.set_stripe_customer(user["id"], customer.id)
    for status in ("active", "trialing"):
        subs = stripe.Subscription.list(customer=customer.id, status=status, limit=1)
        if subs.data:
            db.update_user_subscription(
                user["id"],
                _stripe_status_from_subscription(subs.data[0]),
                subscription_id=subs.data[0].id,
                stripe_customer_id=customer.id,
            )
            return


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/session-status")
def session_status():
    user = auth.get_current_user()
    if user and auth.user_has_active_subscription(user):
        return jsonify({
            "logged_in": True,
            "email": user["email"],
            "has_billing_portal": bool(user.get("stripe_customer_id")),
        })
    return jsonify({"logged_in": False})


@app.route("/verify", methods=["POST"])
@limiter.limit("10 per minute")
def verify():
    data = request.get_json(silent=True) or {}
    email = data.get("email", "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"error": "Enter a valid email address."}), 400

    has_free_access = auth.email_has_free_access(email)

    if config.SUBSCRIPTION_REQUIRED and not has_free_access:
        if not config.STRIPE_SECRET_KEY:
            return jsonify({"error": "Billing is not configured yet."}), 503
        try:
            if not _stripe_has_active_subscription(email):
                return jsonify({"error": "No active subscription found for this email."}), 403
        except stripe.StripeError:
            logger.exception("Stripe verification failed for %s", email)
            return jsonify({"error": "Could not verify subscription. Please try again."}), 500

    user = db.get_user_by_email(email)
    if not user:
        user_id = db.create_user(email, auth.hash_password(secrets.token_urlsafe(32)))
        user = db.get_user_by_id(user_id)

    if config.STRIPE_SECRET_KEY:
        try:
            _sync_user_from_stripe(user, email)
            user = db.get_user_by_id(user["id"])
        except stripe.StripeError:
            logger.exception("Stripe sync failed for %s", email)

    if config.SUBSCRIPTION_REQUIRED and not auth.user_has_active_subscription(user):
        return jsonify({"error": "No active subscription found for this email."}), 403

    auth.login_user(user["id"])
    return jsonify({
        "email": user["email"],
        "has_billing_portal": bool(user.get("stripe_customer_id")),
    })


@app.route("/login", methods=["GET", "POST"])
def login():
    if auth.get_current_user():
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        user = db.get_user_by_email(email)
        if user and auth.verify_password(user["password_hash"], password):
            auth.login_user(user["id"])
            return redirect(request.args.get("next") or url_for("index"))
        error = "Invalid email or password."
    return render_template(
        "auth_form.html",
        title="Log in",
        submit_label="Log in",
        show_confirm=False,
        footer_text='No account? <a href="/register">Create one</a>',
        error=error,
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    if auth.get_current_user():
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if not email or "@" not in email:
            error = "Enter a valid email address."
        elif len(password) < auth.MIN_PASSWORD_LENGTH:
            error = f"Password must be at least {auth.MIN_PASSWORD_LENGTH} characters."
        elif password != confirm:
            error = "Passwords do not match."
        elif db.get_user_by_email(email):
            error = "An account with this email already exists."
        else:
            user_id = db.create_user(email, auth.hash_password(password))
            auth.login_user(user_id)
            return redirect(url_for("index"))
    return render_template(
        "auth_form.html",
        title="Create account",
        submit_label="Create account",
        show_confirm=True,
        footer_text='Already have an account? <a href="/login">Log in</a><br><br>By signing up you agree to our <a href="/terms">Terms</a> and <a href="/privacy">Privacy Policy</a>.',
        error=error,
    )


@app.route("/logout", methods=["POST"])
def logout():
    auth.logout_user()
    return jsonify({"ok": True})


@app.route("/subscribe")
@auth.login_required
def subscribe():
    user = auth.get_current_user()
    if auth.user_has_active_subscription(user):
        return redirect(url_for("index"))

    if not config.STRIPE_SECRET_KEY or not config.STRIPE_PRICE_ID:
        return render_template("error.html", message="Billing is not configured yet."), 503

    customer_id = user.get("stripe_customer_id")
    if not customer_id:
        customer = stripe.Customer.create(email=user["email"], metadata={"user_id": str(user["id"])})
        customer_id = customer.id
        db.set_stripe_customer(user["id"], customer_id)

    checkout = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": config.STRIPE_PRICE_ID, "quantity": 1}],
        success_url=f"{config.APP_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{config.APP_URL}/",
        client_reference_id=str(user["id"]),
        metadata={"user_id": str(user["id"])},
    )
    return redirect(checkout.url, code=303)


@app.route("/billing/success")
@auth.login_required
def billing_success():
    session_id = request.args.get("session_id")
    if session_id and config.STRIPE_SECRET_KEY:
        try:
            checkout = stripe.checkout.Session.retrieve(session_id)
            user = auth.get_current_user()
            if checkout.customer:
                db.set_stripe_customer(user["id"], checkout.customer)
            if checkout.subscription:
                sub = stripe.Subscription.retrieve(checkout.subscription)
                db.update_user_subscription(
                    user["id"],
                    _stripe_status_from_subscription(sub),
                    subscription_id=sub.id,
                    stripe_customer_id=checkout.customer,
                )
        except stripe.StripeError:
            logger.exception("Failed to sync checkout session")
    return redirect(url_for("index"))


@app.route("/billing/portal")
@auth.login_required
def billing_portal():
    user = auth.get_current_user()
    if not user.get("stripe_customer_id") or not config.STRIPE_SECRET_KEY:
        return redirect(url_for("subscribe"))
    portal = stripe.billing_portal.Session.create(
        customer=user["stripe_customer_id"],
        return_url=f"{config.APP_URL}/",
    )
    return redirect(portal.url, code=303)


@app.route("/webhook/stripe", methods=["POST"])
@limiter.exempt
def stripe_webhook():
    if not config.STRIPE_WEBHOOK_SECRET:
        return jsonify({"error": "Webhook not configured."}), 503

    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, config.STRIPE_WEBHOOK_SECRET)
    except ValueError:
        return jsonify({"error": "Invalid payload."}), 400
    except stripe.SignatureVerificationError:
        return jsonify({"error": "Invalid signature."}), 400

    event_type = event["type"]
    data = event["data"]["object"]

    if event_type == "checkout.session.completed":
        user_id = data.get("client_reference_id") or (data.get("metadata") or {}).get("user_id")
        if user_id and data.get("subscription"):
            sub = stripe.Subscription.retrieve(data["subscription"])
            db.update_user_subscription(
                int(user_id),
                _stripe_status_from_subscription(sub),
                subscription_id=sub.id,
                stripe_customer_id=data.get("customer"),
            )

    elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        customer_id = data.get("customer")
        user = db.get_user_by_stripe_customer(customer_id) if customer_id else None
        if user:
            status = "canceled" if event_type == "customer.subscription.deleted" else _stripe_status_from_subscription(data)
            db.update_user_subscription(user["id"], status, subscription_id=data.get("id"))

    return jsonify({"received": True}), 200


@app.route("/voice/personas")
@auth.subscription_required
def voice_personas():
    user = auth.get_current_user()
    personas = db.list_voice_personas(user["id"])
    return jsonify({
        "personas": [
            {
                "id": p["id"],
                "name": p["name"],
                "persona_type": p["persona_type"],
                "tone": p["tone"],
                "goal": p["goal"],
            }
            for p in personas
        ]
    })


@app.route("/voice/personas", methods=["POST"])
@auth.subscription_required
def create_voice_persona():
    user = auth.get_current_user()
    data = request.get_json(silent=True)
    cleaned, error = validate_voice_persona_payload(data)
    if error:
        return jsonify({"error": error}), 400
    persona_id = db.create_voice_persona(user["id"], cleaned)
    persona = db.get_voice_persona(persona_id, user["id"])
    return jsonify({
        "id": persona["id"],
        "name": persona["name"],
        "persona_type": persona["persona_type"],
        "tone": persona["tone"],
        "goal": persona["goal"],
    }), 201


@app.route("/voice/calls")
@auth.subscription_required
def voice_calls():
    user = auth.get_current_user()
    calls = db.list_voice_calls(user["id"])
    return jsonify({
        "calls": [
            {
                "id": c["id"],
                "persona_name": c.get("persona_name"),
                "lead_name": c.get("lead_name"),
                "phone_number": c.get("phone_number"),
                "lead_type": c.get("lead_type"),
                "status": c.get("status"),
                "outcome": c.get("outcome"),
                "appointment_requested": bool(c.get("appointment_requested")),
                "summary": c.get("summary"),
                "transcript": c.get("transcript"),
                # Private Vapi/R2 URLs are not browser-playable; expose our auth proxy instead.
                "recording_url": (
                    f"/voice/calls/{c['id']}/recording"
                    if c.get("recording_url") and c.get("provider_call_id")
                    else None
                ),
                "created_at": c.get("created_at"),
                "completed_at": c.get("completed_at"),
            }
            for c in calls
        ]
    })


@app.route("/voice/calls/<int:call_id>/recording")
@auth.subscription_required
def voice_call_recording(call_id):
    user = auth.get_current_user()
    call = db.get_voice_call(call_id, user["id"])
    if not call:
        return jsonify({"error": "Call not found."}), 404
    if not call.get("provider_call_id"):
        return jsonify({"error": "Recording is not available for this call."}), 404
    if not call.get("recording_url"):
        return jsonify({"error": "Recording is not available for this call yet."}), 404

    try:
        download_url = get_voice_provider().get_recording_download_url(call["provider_call_id"])
    except VoiceProviderError as exc:
        logger.warning("Recording fetch failed for call %s: %s", call_id, exc)
        return jsonify({"error": str(exc)}), 503

    return redirect(download_url, code=302)


@app.route("/account/business-profile", methods=["GET", "PUT"])
@auth.subscription_required
def business_profile():
    user = auth.get_current_user()
    if request.method == "GET":
        profile = db.get_business_profile(user["id"]) or {
            "agent_name": "",
            "brokerage_name": "",
            "company_name": "",
        }
        return jsonify({"profile": profile})

    data = request.get_json(silent=True) or {}
    profile = db.update_business_profile(
        user["id"],
        agent_name=str(data.get("agent_name") or ""),
        brokerage_name=str(data.get("brokerage_name") or ""),
        company_name=str(data.get("company_name") or ""),
    )
    return jsonify({"ok": True, "profile": profile})


@app.route("/voice/calls", methods=["POST"])
@auth.subscription_required
@limiter.limit(lambda: f"{config.VOICE_DAILY_CALL_LIMIT} per day", key_func=_user_rate_limit_key)
def start_voice_call():
    user = auth.get_current_user()
    data = request.get_json(silent=True)
    cleaned, error = validate_voice_call_payload(data)
    if error:
        return jsonify({"error": error}), 400

    persona = db.get_voice_persona(cleaned["persona_id"], user["id"])
    if not persona:
        return jsonify({"error": "Selected persona was not found."}), 404

    # Optional CRM lead enrichment — ownership checked server-side.
    if cleaned.get("lead_id"):
        lead = db.get_lead(cleaned["lead_id"], user["id"])
        if not lead:
            return jsonify({"error": "Selected lead was not found."}), 404
        cleaned["lead_name"] = cleaned.get("lead_name") or lead.get("name") or ""
        cleaned["property_interest"] = (
            cleaned.get("property_interest") or lead.get("property_interest") or ""
        )
        if not cleaned.get("lead_context"):
            note_bits = [lead.get("notes"), lead.get("next_action"), lead.get("lead_type")]
            cleaned["lead_context"] = " | ".join(bit for bit in note_bits if bit)[:1500]

    profile = db.get_business_profile(user["id"]) or {}
    variable_values = build_vapi_variable_values(profile, cleaned)
    validation_error = validate_vapi_variable_values(variable_values)
    if validation_error:
        return jsonify({"error": validation_error}), 400
    log_variable_values_presence(variable_values)

    call_id = db.create_voice_call(
        user_id=user["id"],
        persona_id=persona["id"],
        provider=config.VOICE_PROVIDER,
        direction="outbound",
        data=cleaned,
    )
    prompt = build_voice_call_prompt(persona, cleaned)
    try:
        result = get_voice_provider().start_outbound_call(
            call_id, cleaned, persona, prompt, variable_values=variable_values
        )
        db.update_voice_call_provider(call_id, result["provider_call_id"], "started")
    except VoiceProviderError as exc:
        logger.warning("Voice call failed to start: %s", type(exc).__name__)
        db.update_voice_call_provider(call_id, None, "failed")
        return jsonify({"error": str(exc)}), 503

    return jsonify({
        "id": call_id,
        "provider_call_id": result["provider_call_id"],
        "status": "started",
    }), 201


@app.route("/sms/messages")
@auth.subscription_required
def sms_messages():
    user = auth.get_current_user()
    messages = db.list_sms_messages(user["id"])
    provider = get_sms_provider()
    return jsonify({
        "send_configured": provider.is_configured(),
        "coach_configured": sms_coach.is_configured(),
        "messages": [
            {
                "id": m["id"],
                "lead_id": m.get("lead_id"),
                "persona_name": m.get("persona_name"),
                "lead_name": m.get("lead_name"),
                "phone_number": m.get("phone_number"),
                "lead_type": m.get("lead_type"),
                "direction": m.get("direction"),
                "status": m.get("status"),
                "message_body": m.get("message_body"),
                "error_message": m.get("error_message"),
                "created_at": m.get("created_at"),
                "sent_at": m.get("sent_at"),
            }
            for m in messages
        ],
    })


@app.route("/sms/leads")
@auth.subscription_required
def sms_leads():
    user = auth.get_current_user()
    leads = db.list_leads(user["id"])
    return jsonify({
        "leads": [
            {
                "id": lead["id"],
                "name": lead.get("name"),
                "phone_number": lead.get("phone_number"),
                "lead_type": lead.get("lead_type"),
                "property_interest": lead.get("property_interest"),
                "status": lead.get("status"),
                "next_action": lead.get("next_action"),
                "follow_up_at": lead.get("follow_up_at"),
                "message_count": lead.get("message_count") or 0,
                "updated_at": lead.get("updated_at"),
            }
            for lead in leads
        ]
    })


@app.route("/sms/leads/<int:lead_id>")
@auth.subscription_required
def sms_lead_detail(lead_id):
    user = auth.get_current_user()
    lead = db.get_lead(lead_id, user["id"])
    if not lead:
        return jsonify({"error": "Lead not found."}), 404
    return jsonify({"lead": lead})


@app.route("/sms/leads/<int:lead_id>/messages")
@auth.subscription_required
def sms_lead_messages(lead_id):
    user = auth.get_current_user()
    lead = db.get_lead(lead_id, user["id"])
    if not lead:
        return jsonify({"error": "Lead not found."}), 404
    messages = db.list_lead_messages(user["id"], lead_id)
    return jsonify({
        "lead": {
            "id": lead["id"],
            "name": lead.get("name"),
            "phone_number": lead.get("phone_number"),
            "status": lead.get("status"),
            "next_action": lead.get("next_action"),
            "follow_up_at": lead.get("follow_up_at"),
        },
        "messages": [
            {
                "id": m["id"],
                "direction": m.get("direction"),
                "status": m.get("status"),
                "message_body": m.get("message_body"),
                "created_at": m.get("created_at"),
                "sent_at": m.get("sent_at"),
            }
            for m in messages
        ],
    })


@app.route("/sms/inbox")
@auth.subscription_required
def sms_inbox():
    import json as _json

    user = auth.get_current_user()
    insights = db.list_pending_insights(user["id"])
    items = []
    for item in insights:
        raw = {}
        try:
            raw = _json.loads(item.get("raw_json") or "{}")
        except (TypeError, _json.JSONDecodeError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        items.append({
            "id": item["id"],
            "lead_id": item["lead_id"],
            "lead_name": item.get("lead_name"),
            "phone_number": item.get("phone_number"),
            "lead_status": item.get("lead_status"),
            "inbound_body": item.get("inbound_body"),
            "summary": item.get("summary"),
            "intent": item.get("intent"),
            "next_best_step": item.get("next_best_step") or raw.get("recommended_next_action"),
            "recommended_action": item.get("recommended_action") or raw.get("recommended_next_action"),
            "suggested_reply": item.get("suggested_reply") or raw.get("draft_reply"),
            "home_value_pitch": item.get("home_value_pitch") or raw.get("home_value_pitch"),
            "confidence_score": item.get("confidence_score") if item.get("confidence_score") is not None else raw.get("confidence"),
            "requires_manual_review": bool(item.get("requires_manual_review")),
            "escalation_topics": [
                t for t in str(item.get("escalation_topics") or "").split(",") if t
            ],
            "suggested_message_id": item.get("suggested_message_id"),
            "created_at": item.get("created_at"),
            "suggested_lead_status": raw.get("suggested_lead_status") or "",
            "suggested_lead_status_label": status_label(raw.get("suggested_lead_status")) if raw.get("suggested_lead_status") else "",
            "suggested_follow_up_at": raw.get("suggested_follow_up_at"),
            "suggested_follow_up_reason": raw.get("suggested_follow_up_reason") or "",
            "suggested_tasks": raw.get("suggested_tasks") or [],
            "appointment_requested": bool(raw.get("appointment_requested")),
            "appointment_details": raw.get("appointment_details"),
        })
    return jsonify({
        "coach_configured": sms_coach.is_configured(),
        "items": items,
    })


@app.route("/sms/suggestions/<int:insight_id>/dismiss", methods=["POST"])
@auth.subscription_required
def dismiss_sms_suggestion(insight_id):
    user = auth.get_current_user()
    insight = db.get_insight(insight_id, user["id"])
    if not insight:
        return jsonify({"error": "Suggestion not found."}), 404
    db.update_insight_status(insight_id, user["id"], "dismissed")
    if insight.get("suggested_message_id"):
        db.update_sms_message_send_result(
            insight["suggested_message_id"],
            status="dismissed",
            error_message="Dismissed by agent.",
        )
    return jsonify({"ok": True, "status": "dismissed"})


@app.route("/sms/suggestions/<int:insight_id>/send", methods=["POST"])
@auth.subscription_required
@limiter.limit(lambda: f"{config.SMS_DAILY_LIMIT} per day", key_func=_user_rate_limit_key)
def send_sms_suggestion(insight_id):
    """Agent-approved send of a Claude-suggested reply. Never auto-sends."""
    user = auth.get_current_user()
    data = request.get_json(silent=True) or {}
    if not data.get("compliance_confirmed"):
        return jsonify({"error": "Confirm that this lead consented to receive SMS before sending."}), 400

    insight = db.get_insight(insight_id, user["id"])
    if not insight or insight.get("status") != "pending":
        return jsonify({"error": "Suggestion not found or already handled."}), 404

    if insight.get("requires_manual_review") and not data.get("force_send_after_review"):
        return jsonify({
            "error": (
                "This reply was escalated for manual handling "
                "(legal, financing, negotiation, fair housing, complaint, or uncertain facts). "
                "Review carefully, then confirm send again."
            ),
            "requires_manual_review": True,
            "escalation_topics": [
                t for t in str(insight.get("escalation_topics") or "").split(",") if t
            ],
        }), 409

    message_body = str(data.get("message_body") or insight.get("suggested_reply") or "").strip()[:480]
    if not message_body:
        return jsonify({"error": "Suggested reply is empty."}), 400

    use_pitch = bool(data.get("use_home_value_pitch"))
    if use_pitch and insight.get("home_value_pitch"):
        message_body = str(insight["home_value_pitch"]).strip()[:480]

    lead = db.get_lead(insight["lead_id"], user["id"])
    if not lead:
        return jsonify({"error": "Lead not found."}), 404
    if (lead.get("opt_out_status") or "active") == "opted_out":
        return jsonify({"error": "This lead opted out. Do not send SMS."}), 403

    message_id = insight.get("suggested_message_id")
    if message_id:
        db.update_sms_message_send_result(message_id, status="draft", error_message=None)
        db.update_sms_message_body(message_id, user["id"], message_body, direction="outbound")
        db.update_sms_compliance(
            message_id,
            user["id"],
            consent_status="confirmed",
            opt_out_status=lead.get("opt_out_status") or "active",
        )
    else:
        message_id = db.create_sms_message(
            user_id=user["id"],
            persona_id=None,
            provider=config.SMS_PROVIDER,
            data={
                "lead_name": lead.get("name"),
                "phone_number": lead.get("phone_number"),
                "lead_type": lead.get("lead_type"),
                "property_interest": lead.get("property_interest"),
                "message_body": message_body,
            },
            status="draft",
            lead_id=lead["id"],
            direction="outbound",
            consent_status="confirmed",
            opt_out_status=lead.get("opt_out_status") or "active",
        )

    provider = get_sms_provider()
    if not provider.is_configured():
        return jsonify({"error": "Twilio SMS is not configured."}), 503

    try:
        result = provider.send_sms(
            lead["phone_number"],
            message_body,
            status_callback=sms_status_callback_url(),
        )
        db.update_sms_message_send_result(
            message_id,
            provider_message_id=result["provider_message_id"],
            status=result.get("status") or "queued",
        )
        db.set_lead_consent(lead["id"], user["id"], "confirmed")
        db.touch_lead_outbound(lead["id"], user["id"])
        db.update_insight_status(insight_id, user["id"], "sent")
        db.record_tool_usage(user["id"], "ai_sms", "suggestion_sent")
        return jsonify({
            "ok": True,
            "id": message_id,
            "lead_id": lead["id"],
            "status": result.get("status") or "queued",
            "provider_message_id": result["provider_message_id"],
            "message_body": message_body,
            "consent_status": "confirmed",
            "opt_out_status": lead.get("opt_out_status") or "active",
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }), 201
    except SmsProviderError as exc:
        logger.warning("Suggested SMS send failed code=%s", getattr(exc, "provider_code", None))
        db.update_sms_message_send_result(message_id, status="failed", error_message=str(exc))
        return jsonify({"error": str(exc)}), 503


@app.route("/sms/generate", methods=["POST"])
@auth.subscription_required
@limiter.limit("20 per minute", key_func=_user_rate_limit_key)
def generate_sms():
    user = auth.get_current_user()
    data = request.get_json(silent=True)
    cleaned, error = validate_sms_generate_payload(data)
    if error:
        return jsonify({"error": error}), 400

    persona = db.get_voice_persona(cleaned["persona_id"], user["id"])
    if not persona:
        return jsonify({"error": "Selected persona was not found."}), 404

    try:
        message = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": build_sms_prompt(persona, cleaned)}],
        )
        body = message.content[0].text.strip().strip('"').strip("'")
        db.record_tool_usage(user["id"], "ai_sms", "generated")
        return jsonify({"message_body": body})
    except Exception:
        logger.exception("SMS generation failed")
        return jsonify({"error": "Could not generate SMS. Please try again."}), 500


@app.route("/sms/messages", methods=["POST"])
@auth.subscription_required
@limiter.limit(lambda: f"{config.SMS_DAILY_LIMIT} per day", key_func=_user_rate_limit_key)
def send_sms_message():
    user = auth.get_current_user()
    data = request.get_json(silent=True)
    cleaned, error = validate_sms_send_payload(data)
    if error:
        return jsonify({"error": error}), 400

    persona = db.get_voice_persona(cleaned["persona_id"], user["id"])
    if not persona:
        return jsonify({"error": "Selected persona was not found."}), 404

    lead_id = db.upsert_lead(user["id"], cleaned["phone_number"], cleaned, source="sms")
    # validate_sms_send_payload already required compliance_confirmed.
    consent_status = "confirmed"
    db.set_lead_consent(lead_id, user["id"], "confirmed")
    message_id = db.create_sms_message(
        user_id=user["id"],
        persona_id=persona["id"],
        provider=config.SMS_PROVIDER,
        data=cleaned,
        status="draft",
        lead_id=lead_id,
        direction="outbound",
        consent_status=consent_status,
        opt_out_status="active",
    )
    db.record_tool_usage(user["id"], "ai_sms", "saved")

    provider = get_sms_provider()
    if not cleaned.get("send_now"):
        return jsonify({
            "id": message_id,
            "lead_id": lead_id,
            "status": "draft",
            "message_body": cleaned["message_body"],
            "send_configured": provider.is_configured(),
        }), 201

    if not provider.is_configured():
        db.update_sms_message_send_result(
            message_id,
            status="draft",
            error_message="Saved as draft. Twilio SMS is not configured.",
        )
        return jsonify({
            "id": message_id,
            "lead_id": lead_id,
            "status": "draft",
            "message_body": cleaned["message_body"],
            "send_configured": False,
            "warning": "SMS saved as draft. Twilio SMS is not configured for sending yet.",
        }), 201

    try:
        result = provider.send_sms(
            cleaned["phone_number"],
            cleaned["message_body"],
            status_callback=sms_status_callback_url(),
        )
        db.update_sms_message_send_result(
            message_id,
            provider_message_id=result["provider_message_id"],
            status=result.get("status") or "queued",
        )
        db.touch_lead_outbound(lead_id, user["id"])
        db.record_tool_usage(user["id"], "ai_sms", "sent")
        return jsonify({
            "id": message_id,
            "lead_id": lead_id,
            "status": result.get("status") or "queued",
            "provider_message_id": result["provider_message_id"],
            "message_body": cleaned["message_body"],
            "send_configured": True,
        }), 201
    except SmsProviderError as exc:
        logger.warning("SMS send failed code=%s", getattr(exc, "provider_code", None))
        db.update_sms_message_send_result(message_id, status="failed", error_message=str(exc))
        return jsonify({"error": str(exc), "id": message_id, "status": "failed"}), 503


@app.route("/sms/test", methods=["POST"])
@auth.subscription_required
@limiter.limit("5 per minute", key_func=_user_rate_limit_key)
def sms_test_send():
    """Secure server-side test send to a verified Twilio trial recipient."""
    user = auth.get_current_user()
    data = request.get_json(silent=True)
    cleaned, error = validate_sms_test_payload(data)
    if error:
        return jsonify({"ok": False, "error": error}), 400

    provider = get_sms_provider()
    if not provider.is_configured():
        return jsonify({"ok": False, "error": "Twilio SMS is not configured."}), 503

    message_id = db.create_sms_message(
        user_id=user["id"],
        persona_id=None,
        provider=config.SMS_PROVIDER,
        data={
            "lead_name": "SMS Test",
            "phone_number": cleaned["to"],
            "message_body": cleaned["message"],
            "lead_type": "test",
            "desired_outcome": "verify Twilio connectivity",
        },
        status="draft",
    )

    try:
        result = provider.send_sms(
            cleaned["to"],
            cleaned["message"],
            status_callback=sms_status_callback_url(),
        )
        db.update_sms_message_send_result(
            message_id,
            provider_message_id=result["provider_message_id"],
            status=result.get("status") or "queued",
        )
        db.record_tool_usage(user["id"], "ai_sms", "test_sent")
        return jsonify({
            "ok": True,
            "status": result.get("status") or "queued",
            "provider_message_id": result["provider_message_id"],
            "to": cleaned["to"],
        }), 201
    except SmsProviderError as exc:
        logger.warning("SMS test send failed code=%s", getattr(exc, "provider_code", None))
        db.update_sms_message_send_result(message_id, status="failed", error_message=str(exc))
        return jsonify({"ok": False, "error": str(exc)}), 503


@app.route("/webhook/sms/inbound", methods=["POST"])
@limiter.exempt
@validate_twilio_request
def sms_inbound_webhook():
    """
    Phase 1 inbound CRM webhook.
    Match TopAI user + lead by Twilio recipient (To) and sender (From).
    Save inbound message to conversation history and draft a Claude recommendation for agent approval.
    Never auto-sends.
    """
    from_number, from_error = validate_e164_phone(request.form.get("From"))
    to_number, to_error = validate_e164_phone(request.form.get("To"))
    body = str(request.form.get("Body") or "").strip()[:1500]
    message_sid = str(request.form.get("MessageSid") or "").strip() or None

    if from_error or to_error or not body:
        return _twiml_empty_response()

    # Recipient must be this app's configured Twilio number.
    configured_to = (config.TWILIO_PHONE_NUMBER or "").strip()
    if not configured_to or to_number != configured_to:
        logger.warning("Inbound SMS ignored: recipient number does not match configured Twilio number")
        return _twiml_empty_response()

    owner_id = db.find_sms_user_by_phone(from_number)
    if not owner_id:
        return _twiml_empty_response()

    try:
        seed = db.last_outbound_seed_for_phone(owner_id, from_number)
        lead_id = db.upsert_lead(owner_id, from_number, seed, source="sms")
        lead = db.get_lead(lead_id, owner_id)
        opted_out = _looks_like_opt_out(body)
        inbound_id = db.create_inbound_sms_message(
            user_id=owner_id,
            phone_number=from_number,
            message_body=body,
            provider_message_id=message_sid,
            lead_id=lead_id,
            lead_name=(lead or {}).get("name"),
            opt_out_status="opted_out" if opted_out else "active",
        )
        now = datetime.now(timezone.utc).isoformat()
        if opted_out:
            db.mark_lead_opt_out(lead_id, owner_id)
            crm_db.add_lead_activity(
                lead_id,
                owner_id,
                "opt_out",
                "Lead opted out via SMS keyword",
                {"body_preview": body[:120]},
            )
            crm_db.upsert_needs_attention(
                owner_id, lead_id, "opt_out", priority="urgent", source_ref_type="sms", source_ref_id=inbound_id
            )
        else:
            # Deterministic inbound touch only — Claude suggestions are never auto-applied.
            # DNC / opted-out leads cannot leave that status via automation.
            db.update_lead_from_analysis(
                lead_id,
                owner_id,
                last_inbound_at=now,
            )
            if (lead or {}).get("opt_out_status") != "opted_out":
                crm_db.set_lead_status(
                    owner_id, lead_id, "contacted", from_automation=True
                )
            crm_db.add_lead_activity(
                lead_id,
                owner_id,
                "sms_inbound",
                "Inbound SMS received",
                {"message_id": inbound_id},
            )
        _analyze_inbound_and_coach(owner_id, lead_id, inbound_id, body, opted_out=opted_out)
    except Exception:
        logger.exception("Failed to process inbound SMS")

    return _twiml_empty_response()


def _looks_like_opt_out(body):
    text = re.sub(r"[^a-z\s]", "", (body or "").strip().lower())
    tokens = set(text.split())
    return bool(tokens & {"stop", "unsubscribe", "cancel", "end", "quit"})


def _analyze_inbound_and_coach(user_id, lead_id, inbound_id, inbound_body, opted_out=False):
    """Claude coaching. Stores recommendations + draft only — never auto-sends or auto-applies CRM changes."""
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
    except sms_coach.SmsCoachError as exc:
        logger.warning("Claude inbound analysis failed: %s", type(exc).__name__)
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

    # Store recommendation text on the lead for visibility — do NOT apply status/follow-up/tasks.
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
            "confidence": analysis.get("confidence_score"),
        },
    )
    crm_db.apply_coach_queue_flags(user_id, lead_id, analysis, insight_id=insight_id)
    db.record_tool_usage(user_id, "ai_sms", "inbound_analyzed")


@app.route("/webhook/sms/status", methods=["POST"])
@limiter.exempt
@validate_twilio_request
def sms_status_webhook():
    """Twilio delivery-status webhook. Do not point Twilio here until this route is public."""
    message_sid = str(request.form.get("MessageSid") or "").strip()
    message_status = str(request.form.get("MessageStatus") or "").strip().lower()
    error_code = str(request.form.get("ErrorCode") or "").strip()

    if not message_sid:
        return jsonify({"received": True}), 200

    status_map = {
        "queued": "queued",
        "sending": "queued",
        "sent": "sent",
        "delivered": "delivered",
        "undelivered": "failed",
        "failed": "failed",
    }
    mapped = status_map.get(message_status)
    error_message = f"Delivery error {error_code}" if error_code and mapped == "failed" else None
    if mapped:
        db.update_sms_message_by_provider_id(
            message_sid,
            status=mapped,
            error_message=error_message,
        )
        if mapped == "failed":
            msg = db.get_sms_message_by_provider_id(message_sid)
            if msg and msg.get("user_id") and msg.get("lead_id"):
                crm_db.upsert_needs_attention(
                    msg["user_id"],
                    msg["lead_id"],
                    "delivery_failed",
                    priority="high",
                    source_ref_type="sms",
                    source_ref_id=msg.get("id"),
                )
                crm_db.add_lead_activity(
                    msg["lead_id"],
                    msg["user_id"],
                    "sms_delivery_failed",
                    "SMS delivery failed",
                    {"provider_message_id": message_sid, "error_code": error_code},
                )
    return jsonify({"received": True}), 200


def _twiml_empty_response():
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        200,
        {"Content-Type": "text/xml; charset=utf-8"},
    )


@app.route("/webhook/voice", methods=["POST"])
@limiter.exempt
def voice_webhook():
    if config.VOICE_PROVIDER_WEBHOOK_SECRET:
        supplied = request.headers.get("X-Voice-Webhook-Secret") or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if supplied != config.VOICE_PROVIDER_WEBHOOK_SECRET:
            return jsonify({"error": "Invalid signature."}), 401

    payload = request.get_json(silent=True) or {}
    normalized = normalize_voice_webhook(payload)
    if not normalized.get("provider_call_id") and not normalized.get("call_id"):
        return jsonify({"error": "Missing provider call ID."}), 400

    updated = db.update_voice_call_from_webhook(**normalized)
    if not updated:
        logger.warning(
            "Voice webhook did not match an existing call: provider_call_id=%s call_id=%s",
            normalized.get("provider_call_id"),
            normalized.get("call_id"),
        )
    return jsonify({"received": True}), 200


@app.route("/terms")
def terms():
    return render_template("legal.html", title="Terms of Service", doc="terms")


@app.route("/privacy")
def privacy():
    return render_template("legal.html", title="Privacy Policy", doc="privacy")


@app.route("/pricing")
def pricing():
    return render_template("pricing.html")


@app.route("/features")
def features():
    return render_template("features.html")


@app.route("/how-it-works")
def how_it_works():
    return render_template("how_it_works.html")


@app.route("/tutorial")
def tutorial():
    user = auth.get_current_user()
    if not user or not auth.user_has_active_subscription(user):
        return redirect(url_for("index"))
    return render_template(
        "tutorial.html",
        email=user["email"],
        has_billing_portal=bool(user.get("stripe_customer_id")),
    )


@app.route("/dashboard")
def dashboard():
    user = auth.get_current_user()
    if not user or not auth.user_has_active_subscription(user):
        return redirect(url_for("index"))
    metrics = db.get_dashboard_metrics(user["id"])
    pipeline = crm_db.get_pipeline_metrics(user["id"])
    needs = crm_db.list_needs_attention(user["id"])[:8]
    notifications = crm_db.list_notifications(user["id"], unread_only=True, limit=8)
    return render_template(
        "dashboard.html",
        email=user["email"],
        has_billing_portal=bool(user.get("stripe_customer_id")),
        metrics=metrics,
        pipeline=pipeline,
        needs_attention=needs,
        notifications=notifications,
        status_label=status_label,
    )


@app.route("/refund-policy")
def refund_policy():
    return render_template("legal.html", title="Refund Policy", doc="refund")


@app.route("/contact")
def contact():
    return render_template("legal.html", title="Contact", doc="contact")


@app.route("/generate", methods=["POST"])
@auth.subscription_required
@limiter.limit("10 per minute", key_func=_user_rate_limit_key)
@limiter.limit("100 per day", key_func=_user_rate_limit_key)
def generate():
    data = request.get_json(silent=True)
    cleaned, error = validate_listing_payload(data)
    if error:
        return jsonify({"error": error}), 400
    try:
        message = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": build_listing_prompt(cleaned)}],
        )
        raw = message.content[0].text
        listing = _extract_section(raw, "LISTING DESCRIPTION", "SOCIAL POSTS")
        social = _extract_section(raw, "SOCIAL POSTS", "PROSPECT EMAIL")
        email = _extract_section(raw, "PROSPECT EMAIL", None)
        user = auth.get_current_user()
        if user:
            db.record_tool_usage(user["id"], "listing_generator", "generated")
        return jsonify({"listing": listing.strip(), "social": social.strip(), "email": email.strip()})
    except Exception:
        logger.exception("Listing generation failed")
        return jsonify({"error": "Generation failed. Please try again."}), 500


@app.route("/generate-script", methods=["POST"])
@auth.subscription_required
@limiter.limit("10 per minute", key_func=_user_rate_limit_key)
@limiter.limit("100 per day", key_func=_user_rate_limit_key)
def generate_script():
    data = request.get_json(silent=True)
    cleaned, error = validate_script_payload(data)
    if error:
        return jsonify({"error": error}), 400
    try:
        message = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1200,
            messages=[{"role": "user", "content": build_script_prompt(cleaned)}],
        )
        raw = message.content[0].text
        opening = _extract_section(raw, "OPENING SCRIPT", "OBJECTION HANDLERS")
        objections = _extract_section(raw, "OBJECTION HANDLERS", "VOICEMAIL SCRIPT")
        voicemail = _extract_section(raw, "VOICEMAIL SCRIPT", None)
        user = auth.get_current_user()
        if user:
            db.record_tool_usage(user["id"], "cold_call_scripts", "generated")
        return jsonify({"opening": opening.strip(), "objections": objections.strip(), "voicemail": voicemail.strip()})
    except Exception:
        logger.exception("Script generation failed")
        return jsonify({"error": "Generation failed. Please try again."}), 500


if __name__ == "__main__":
    print(f"\nTopAI Real Estate Tools running -> http://localhost:{config.PORT}\n")
    app.run(host="0.0.0.0", debug=False, port=config.PORT)
