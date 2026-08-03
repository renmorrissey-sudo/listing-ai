import logging
import secrets

import stripe
from anthropic import Anthropic
from flask import Flask, flash, jsonify, redirect, render_template, request, url_for
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
from external_leads_routes import external_leads_bp
from sms_campaigns import sms_campaigns_bp
from sms_prompts import build_sms_prompt
from sms_provider import (
    SmsProviderError,
    get_sms_provider,
    parse_provider_code_from_error_message,
    redact_secrets,
    sms_status_callback_url,
)
from sms_validation import (
    validate_sms_generate_payload,
    validate_sms_send_payload,
    validate_sms_test_payload,
)
from twilio_security import validate_twilio_request
from validation import validate_listing_payload, validate_script_payload
from voice_prompts import build_voice_call_prompt
from lead_service import (
    VOICE_SOURCE,
    apply_voice_call_webhook_to_lead,
    record_voice_call_started,
    upsert_crm_lead,
)
import registration_gate
import seo
from voice_provider import (
    VoiceProviderError,
    build_vapi_variable_values,
    get_voice_provider,
    log_variable_values_presence,
    normalize_voice_webhook,
    validate_vapi_variable_values,
)
from voice_validation import validate_voice_call_payload, validate_voice_persona_payload
from crm_constants import normalize_lead_status

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
app.register_blueprint(external_leads_bp)
app.register_blueprint(sms_campaigns_bp)
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
    path = request.path or "/"
    user = auth.get_current_user()
    can_signup = registration_gate.registration_allowed_for_user(user)
    return {
        "business_name": config.BUSINESS_NAME,
        "product_name": config.PRODUCT_NAME,
        "legal_entity_name": config.LEGAL_ENTITY_NAME,
        "contact_email": config.CONTACT_EMAIL,
        "subscription_price": config.SUBSCRIPTION_PRICE,
        "trial_offer": config.TRIAL_OFFER,
        "billing_frequency": config.BILLING_FREQUENCY,
        "canonical_url": seo.canonical_loc(path),
        "is_public_marketing_page": seo.is_public_marketing_path(path),
        "current_user": user,
        "user_subscribed": bool(user and auth.user_has_active_subscription(user)),
        "registration_enabled": registration_gate.registration_is_open(),
        "registration_can_signup": can_signup,
        "registration_cta_href": "/subscribe" if can_signup else "/private-beta",
        "registration_cta_label": "Subscribe" if can_signup else "Private beta",
        "registration_promo_label": (
            "Get 50% off your first month" if can_signup else "Private beta"
        ),
        "private_beta_supporting": registration_gate.PRIVATE_BETA_SUPPORTING,
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
    import uuid as _uuid

    correlation_id = _uuid.uuid4().hex[:12]
    logger.exception("Unhandled error correlation_id=%s: %s", correlation_id, error)
    # Public HTML forms should not receive opaque JSON 500s.
    if request.path == "/sms-consent" and request.method == "POST":
        try:
            import sms_consent as sms_consent_mod

            return (
                render_template(
                    "sms_consent.html",
                    error="We could not save your inquiry right now. Please try again in a moment.",
                    success=None,
                    form_name=str(request.form.get("name") or request.form.get("first_name") or "")[:120],
                    form_first_name=str(request.form.get("first_name") or "")[:60],
                    form_last_name=str(request.form.get("last_name") or "")[:60],
                    form_email=str(request.form.get("email") or "")[:200],
                    form_phone=str(request.form.get("phone") or "")[:32],
                    form_message=str(request.form.get("message") or "")[:2000],
                    form_sms_consent=str(request.form.get("sms_consent") or "").lower()
                    in {"1", "true", "on", "yes"},
                    form_campaign_source=str(request.form.get("campaign_source") or "")[:120],
                    sms_support_display=sms_consent_mod.SMS_SUPPORT_DISPLAY,
                    sms_consent_checkbox_text=sms_consent_mod.SMS_CONSENT_CHECKBOX_TEXT,
                ),
                500,
            )
        except Exception:
            logger.exception("Failed to render SMS consent error page")
    wants_html = (
        request.path.startswith("/crm/")
        or request.path.startswith("/app")
        or request.path.startswith("/tutorial")
        or request.path.startswith("/dashboard")
        or "text/html" in (request.headers.get("Accept") or "")
    )
    if wants_html and not request.path.startswith(("/api/", "/generate", "/sms/", "/voice/", "/webhook")):
        return (
            render_template(
                "error.html",
                message=(
                    "Something went wrong loading this page. "
                    f"Reference: {correlation_id}. Please try again."
                ),
            ),
            500,
        )
    payload = {
        "error": "Something went wrong. Please try again.",
        "correlation_id": correlation_id,
    }
    if request.path.startswith("/sms/"):
        payload["stage"] = "database"
        payload["error_category"] = "unhandled_exception"
        payload["error"] = (
            "TopAI could not complete the SMS request. "
            f"Reference: {correlation_id}."
        )
    return jsonify(payload), 500


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


from stripe_billing import (
    billing_config_error as _billing_config_error,
    billing_is_configured as _billing_is_configured,
    checkout_idempotency_key as _checkout_idempotency_key,
    create_subscription_checkout_session as _create_subscription_checkout_session,
    normalize_email as _normalize_email,
    resolve_subscribe_gate as _resolve_subscribe_gate,
    stripe_customer_for_email as _stripe_customer_for_email,
    stripe_has_active_subscription as _stripe_has_active_subscription,
    stripe_status_from_subscription as _stripe_status_from_subscription,
    sync_user_from_stripe as _sync_user_from_stripe,
)

@app.route("/health")
def health():
    from email_service import email_configured
    from sms_providers.factory import get_sms_provider
    from sms_providers.telnyx import TelnyxSMSProvider
    from sms_provider import TwilioSmsProvider

    active = (config.SMS_PROVIDER or "").lower().strip()
    provider = get_sms_provider()
    telnyx = TelnyxSMSProvider()
    twilio = TwilioSmsProvider()
    from sms_authorization import (
        get_telnyx_toll_free_verification_status,
        is_sms_sending_enabled,
    )

    return jsonify({
        "status": "ok",
        "email_configured": email_configured(),
        "password_reset": True,
        "sms_provider": active,
        "telnyx_configured": telnyx.is_configured(),
        "twilio_configured": twilio.is_configured(),
        "active_provider_configured": provider.is_configured(),
        "toll_free_verification_status": (
            get_telnyx_toll_free_verification_status() or "unknown"
            if active == "telnyx"
            else None
        ),
        "sms_sending_enabled": is_sms_sending_enabled(),
    }), 200


@app.route("/sitemap.xml")
def sitemap_xml():
    xml = seo.build_sitemap_xml()
    return app.response_class(
        xml,
        mimetype="application/xml; charset=utf-8",
    )


@app.route("/robots.txt")
def robots_txt():
    return app.response_class(
        seo.build_robots_txt(),
        mimetype="text/plain; charset=utf-8",
    )


@app.route("/")
def index():
    """Public marketing homepage — never auto-opens the Subscriber Access modal."""
    user = auth.get_current_user()
    if user and auth.user_has_active_subscription(user):
        return redirect(url_for("dashboard"))
    return render_template("landing.html")


@app.route("/app")
def subscriber_app():
    """Subscriber tools. Requires password sign-in (no email-only gate)."""
    user = auth.get_current_user()
    if not user:
        return redirect(url_for("login", next="/app"))
    if config.SUBSCRIPTION_REQUIRED and not auth.user_has_active_subscription(user):
        return redirect(url_for("subscribe"))
    notice = None
    if request.args.get("already_subscribed") == "1":
        notice = "Your subscription is already active."
    return render_template("index.html", subscribe_notice=notice)


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
    """Retired email-only access. Password sign-in is required."""
    return jsonify({
        "error": "Email-only access is no longer available. Please sign in with your password.",
        "login_url": "/login?next=/app",
    }), 410


@app.route("/login", methods=["GET", "POST"])
def login():
    if auth.get_current_user():
        return redirect(auth.safe_next_url(request.args.get("next") or "/app"))
    error = None
    password_updated = request.args.get("password_updated") == "1" or request.args.get("reset") == "1"
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        user = db.get_user_by_email(email)
        if user and auth.verify_password(user["password_hash"], password):
            auth.login_user(user["id"])
            return redirect(
                auth.safe_next_url(
                    request.args.get("next") or request.form.get("next") or "/app"
                )
            )
        error = "Invalid email or password."
    if registration_gate.registration_is_open():
        footer_text = 'No account? <a href="/subscribe">Create account</a>'
    else:
        footer_text = (
            'New registrations are in private beta. '
            '<a href="/private-beta">Private beta</a>'
        )
    return render_template(
        "auth_form.html",
        title="Sign in",
        submit_label="Log in",
        show_confirm=False,
        show_forgot=True,
        next_url=auth.safe_next_url(request.args.get("next") or "/app"),
        success=(
            "Your password has been updated. Sign in with your new password."
            if password_updated
            else None
        ),
        footer_text=footer_text,
        error=error,
    )


@app.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def forgot_password():
    import password_reset as pr

    if auth.get_current_user():
        return redirect(url_for("subscriber_app"))
    message = None
    if request.method == "POST":
        # Always show neutral message (anti-enumeration). Also rate-limit by email key.
        email = (request.form.get("email") or "").strip().lower()
        message = pr.request_password_reset(email)
    return render_template(
        "forgot_password.html",
        message=message,
        error=None,
    )


@app.route("/reset-password", methods=["GET", "POST"])
@limiter.limit("20 per minute")
def reset_password():
    import password_reset as pr

    if auth.get_current_user():
        return redirect(url_for("subscriber_app"))
    token = (request.args.get("token") or request.form.get("token") or "").strip()
    error = None
    if request.method == "GET":
        if not pr.peek_reset_token(token):
            return render_template(
                "reset_password.html",
                token="",
                error="This password reset link is invalid or has expired.",
                invalid=True,
            )
        return render_template("reset_password.html", token=token, error=None, invalid=False)

    pwd = request.form.get("password") or ""
    confirm = request.form.get("confirm_password") or ""
    err = pr.validate_new_password(pwd, confirm)
    if err:
        return render_template("reset_password.html", token=token, error=err, invalid=False), 400
    _user_id, consume_err = pr.consume_reset_token(token, pwd)
    if consume_err:
        return render_template(
            "reset_password.html",
            token="",
            error=consume_err,
            invalid=True,
        ), 400
    # Redirect strips the reset token from the browser URL.
    return redirect(url_for("password_updated"))


@app.route("/password-updated")
def password_updated():
    """Post-reset success screen (no token in URL)."""
    if auth.get_current_user():
        return redirect(url_for("subscriber_app"))
    return render_template("password_updated.html")


@app.route("/register", methods=["GET", "POST"])
@limiter.limit("20 per minute")
def register():
    """Legacy signup URL — account creation is on /subscribe when registration is open."""
    user = auth.get_current_user()
    if registration_gate.registration_allowed_for_user(user):
        return redirect(url_for("subscribe"), code=302)
    if registration_gate.wants_json_response():
        return registration_gate.registration_closed_response()
    return redirect(url_for("private_beta"), code=302)


@app.route("/private-beta")
def private_beta():
    """Friendly closed-registration page (not a generic error)."""
    return render_template("private_beta.html")


def _subscribe_page(
    *,
    error=None,
    notice=None,
    form_email="",
    status=200,
    gate=None,
    email_locked=False,
    registration_closed=False,
):
    user = auth.get_current_user()
    need_password = not user
    gate = gate or {"can_checkout": True, "state": "none"}
    show_checkout = bool(gate.get("can_checkout", True)) and not registration_closed
    return (
        render_template(
            "subscribe.html",
            billing_ready=_billing_is_configured(),
            billing_error=_billing_config_error(),
            need_password=need_password,
            form_email=form_email or ((user or {}).get("email") or ""),
            email_locked=email_locked or bool(user),
            error=error,
            notice=notice,
            billing_frequency=config.BILLING_FREQUENCY,
            gate=gate,
            show_checkout_form=show_checkout,
            registration_closed=registration_closed,
        ),
        status,
    )


def _redirect_active_subscriber():
    flash("Your subscription is already active.", "info")
    return redirect(url_for("subscriber_app", already_subscribed=1))


@app.route("/subscribe", methods=["GET", "POST"])
@limiter.limit("20 per minute")
def subscribe():
    """Public plan + signup page. Checkout Session is created only on valid POST."""
    user = auth.get_current_user()
    gate = _resolve_subscribe_gate(user) if user else {
        "can_checkout": True,
        "state": "none",
        "message": None,
        "access_ends_on": None,
        "show_manage_billing": False,
        "show_open_tools": False,
        "redirect": None,
    }
    if user and gate.get("redirect") == "subscriber_app":
        return _redirect_active_subscriber()
    if user and not gate.get("can_checkout", True):
        cancelled = request.args.get("cancelled") == "1"
        notice = (
            "Checkout was cancelled. You can try again whenever you are ready."
            if cancelled
            else None
        )
        block_status = 409 if request.method == "POST" else 200
        return _subscribe_page(
            notice=notice or gate.get("message"),
            gate=gate,
            form_email=(user.get("email") or ""),
            email_locked=True,
            status=block_status,
        )

    # Block public registration / new Checkout before any user or Stripe mutation.
    if user and not registration_gate.registration_allowed_for_user(user):
        if registration_gate.wants_json_response():
            return registration_gate.registration_closed_response()
        return _subscribe_page(
            notice=registration_gate.PRIVATE_BETA_SUPPORTING,
            gate={
                **gate,
                "can_checkout": False,
                "state": "registration_closed",
                "message": registration_gate.PRIVATE_BETA_SUPPORTING,
                "show_manage_billing": bool(user.get("stripe_customer_id")),
                "show_open_tools": False,
            },
            form_email=(user.get("email") or ""),
            email_locked=True,
            registration_closed=True,
            status=403 if request.method == "POST" else 200,
        )

    if not user and not registration_gate.registration_is_open():
        # Anonymous visitors: allowlist is enforced on POST after email is known.
        if request.method == "GET":
            return registration_gate.registration_closed_get_response()
        email_probe = _normalize_email(request.form.get("email"))
        if not registration_gate.email_is_allowlisted(email_probe):
            return registration_gate.registration_closed_response()

    cancelled = request.args.get("cancelled") == "1"
    notice = "Checkout was cancelled. You can try again whenever you are ready." if cancelled else None

    if request.method == "GET":
        # Never create a Checkout Session on GET.
        status = 200 if _billing_is_configured() else 503
        return _subscribe_page(
            notice=notice,
            status=status,
            gate=gate,
            email_locked=bool(user),
        )

    if not _billing_is_configured():
        logger.error(
            "Subscribe POST blocked: billing_config_error=%s",
            _billing_config_error(),
        )
        return _subscribe_page(
            error="Billing is temporarily unavailable. Please try again later.",
            form_email=_normalize_email(request.form.get("email")),
            status=503,
            gate=gate,
            email_locked=bool(user),
        )

    email = _normalize_email(request.form.get("email"))
    password = request.form.get("password") or ""
    confirm = request.form.get("confirm_password") or ""

    if user:
        # Authenticated unpaid user: reuse account email (immutable), skip password.
        email = _normalize_email(user.get("email"))
        try:
            registration_gate.assert_registration_allowed(email)
        except registration_gate.RegistrationClosedError:
            return registration_gate.registration_closed_response()
        # Re-check Stripe/local gate immediately before Checkout (tabs / races).
        gate = _resolve_subscribe_gate(user)
        if gate.get("redirect") == "subscriber_app":
            return _redirect_active_subscriber()
        if not gate.get("can_checkout", True):
            return _subscribe_page(
                notice=gate.get("message"),
                gate=gate,
                form_email=email,
                email_locked=True,
                status=409,
            )
    else:
        if not email or "@" not in email:
            return _subscribe_page(error="Enter a valid email address.", form_email=email, status=400)
        try:
            registration_gate.assert_registration_allowed(email)
        except registration_gate.RegistrationClosedError:
            return registration_gate.registration_closed_response()
        existing = db.get_user_by_email(email)
        if existing:
            # Never create a duplicate user/customer for an existing email.
            return _subscribe_page(
                error=(
                    "An account with this email already exists. "
                    "Please sign in or use Forgot password to recover access."
                ),
                form_email=email,
                status=400,
            )
        if len(password) < auth.MIN_PASSWORD_LENGTH:
            return _subscribe_page(
                error=f"Password must be at least {auth.MIN_PASSWORD_LENGTH} characters.",
                form_email=email,
                status=400,
            )
        if password != confirm:
            return _subscribe_page(
                error="Passwords do not match.",
                form_email=email,
                status=400,
            )
        # Block signup when Stripe already has a blocking subscription for this email.
        if config.STRIPE_SECRET_KEY:
            try:
                existing_customer = _stripe_customer_for_email(email)
                if existing_customer:
                    from stripe_billing import list_blocking_subscriptions

                    if list_blocking_subscriptions(existing_customer.id):
                        return _subscribe_page(
                            error=(
                                "This email already has a Stripe subscription. "
                                "Please sign in to manage billing or continue checkout."
                            ),
                            form_email=email,
                            status=400,
                        )
            except stripe.StripeError:
                logger.exception("Stripe pre-signup subscription check failed")
        user_id = db.create_user(email, auth.hash_password(password))
        auth.login_user(user_id)
        user = auth.get_current_user()

    try:
        checkout = _create_subscription_checkout_session(
            user,
            success_url=f"{config.APP_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{config.APP_URL}/subscribe?cancelled=1",
            idempotency_key=_checkout_idempotency_key(user["id"]),
        )
    except registration_gate.RegistrationClosedError:
        return registration_gate.registration_closed_response()
    except stripe.StripeError:
        logger.exception("Stripe checkout session creation failed for user_id=%s", user.get("id"))
        return _subscribe_page(
            error="We could not start checkout right now. Please try again in a moment.",
            form_email=email,
            status=502,
            email_locked=True,
        )
    except Exception:
        logger.exception("Unexpected checkout failure for user_id=%s", user.get("id"))
        return _subscribe_page(
            error="We could not start checkout right now. Please try again in a moment.",
            form_email=email,
            status=502,
            email_locked=True,
        )

    return redirect(checkout.url, code=303)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    auth.logout_user()
    if request.method == "GET" or "text/html" in (request.headers.get("Accept") or ""):
        return redirect(url_for("index"))
    return jsonify({"ok": True})


@app.route("/billing/success")
def billing_success():
    """Checkout return URL. Access is granted only by the verified Stripe webhook."""
    session_id = request.args.get("session_id")
    if session_id and config.STRIPE_SECRET_KEY:
        try:
            checkout = stripe.checkout.Session.retrieve(session_id)
            user = auth.get_current_user()
            # Persist customer linkage only — do not activate subscription here.
            if user and checkout.customer:
                db.set_stripe_customer(user["id"], checkout.customer)
        except stripe.StripeError:
            logger.exception("Failed to retrieve checkout session after success redirect")
    return render_template("billing_success.html")


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

    event_id = event.get("id") or ""
    event_type = event["type"]
    if event_id and not db.claim_stripe_webhook_event(event_id, event_type):
        return jsonify({"received": True, "duplicate": True}), 200

    data = event["data"]["object"]

    if event_type == "checkout.session.completed":
        user_id = data.get("client_reference_id") or (data.get("metadata") or {}).get("user_id")
        if user_id and data.get("subscription"):
            sub = stripe.Subscription.retrieve(data["subscription"])
            sub_id = sub["id"] if isinstance(sub, dict) else sub.id
            db.update_user_subscription(
                int(user_id),
                _stripe_status_from_subscription(sub),
                subscription_id=sub_id,
                stripe_customer_id=data.get("customer"),
            )

    elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        customer_id = data.get("customer")
        user = db.get_user_by_stripe_customer(customer_id) if customer_id else None
        if user:
            status = (
                "canceled"
                if event_type == "customer.subscription.deleted"
                else _stripe_status_from_subscription(data)
            )
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


def _voice_call_public_dict(c):
    """Serialize a voice call for the authenticated owner (never expose raw Vapi URLs)."""
    has_recording = db.voice_call_has_recording(c)
    can_play = bool(has_recording and c.get("provider_call_id"))
    status = c.get("recording_status")
    if can_play:
        status = "available"
    elif not status and c.get("status") == "completed":
        status = "unavailable"
    return {
        "id": c["id"],
        "lead_id": c.get("lead_id"),
        "persona_name": c.get("persona_name"),
        "lead_name": c.get("lead_name"),
        "phone_number": c.get("phone_number"),
        "lead_type": c.get("lead_type"),
        "status": c.get("status"),
        "outcome": c.get("outcome"),
        "appointment_requested": bool(c.get("appointment_requested")),
        "summary": c.get("summary"),
        "has_transcript": bool(c.get("transcript")),
        "recording_status": status,
        "recording_duration_seconds": c.get("recording_duration_seconds"),
        # Auth proxy paths only — never the stored Vapi/R2 URL.
        "recording_url": f"/api/voice-calls/{c['id']}/recording" if can_play else None,
        "transcript_url": f"/api/voice-calls/{c['id']}/transcript" if c.get("transcript") else None,
        "created_at": c.get("created_at"),
        "completed_at": c.get("completed_at"),
    }


def _serve_voice_call_recording(call_id, user_id):
    call = db.get_voice_call(call_id, user_id)
    if not call:
        return jsonify({"error": "Call not found."}), 404
    if not call.get("provider_call_id"):
        return jsonify({
            "error": "Recording unavailable",
            "recording_status": call.get("recording_status") or "unavailable",
        }), 404
    if not db.voice_call_has_recording(call) and call.get("recording_status") == "not_enabled":
        return jsonify({
            "error": "Recording was not enabled for this call",
            "recording_status": "not_enabled",
        }), 404
    if not db.voice_call_has_recording(call) and call.get("recording_status") == "processing":
        return jsonify({
            "error": "Recording processing",
            "recording_status": "processing",
        }), 404
    if not db.voice_call_has_recording(call):
        return jsonify({
            "error": "Recording unavailable",
            "recording_status": call.get("recording_status") or "unavailable",
        }), 404

    try:
        download_url = get_voice_provider().get_recording_download_url(call["provider_call_id"])
    except VoiceProviderError as exc:
        logger.warning("Recording fetch failed for call %s: %s", call_id, exc)
        return jsonify({
            "error": "Recording unavailable",
            "detail": str(exc),
            "recording_status": "unavailable",
        }), 503

    return redirect(download_url, code=302)


@app.route("/voice/calls")
@auth.subscription_required
def voice_calls():
    user = auth.get_current_user()
    calls = db.list_voice_calls(user["id"])
    return jsonify({"calls": [_voice_call_public_dict(c) for c in calls]})


@app.route("/voice/calls/<int:call_id>/recording")
@auth.subscription_required
def voice_call_recording(call_id):
    user = auth.get_current_user()
    return _serve_voice_call_recording(call_id, user["id"])


@app.route("/api/voice-calls/<int:call_id>/recording")
@auth.subscription_required
def api_voice_call_recording(call_id):
    user = auth.get_current_user()
    return _serve_voice_call_recording(call_id, user["id"])


@app.route("/api/voice-calls/<int:call_id>/transcript")
@auth.subscription_required
def api_voice_call_transcript(call_id):
    user = auth.get_current_user()
    call = db.get_voice_call(call_id, user["id"])
    if not call:
        return jsonify({"error": "Call not found."}), 404
    if not call.get("transcript"):
        return jsonify({"error": "Transcript unavailable."}), 404
    return jsonify({
        "call_id": call["id"],
        "transcript": call.get("transcript"),
        "summary": call.get("summary"),
        "completed_at": call.get("completed_at"),
        "recording_duration_seconds": call.get("recording_duration_seconds"),
    })


@app.route("/account/business-profile", methods=["GET", "PUT"])
@auth.subscription_required
def business_profile():
    user = auth.get_current_user()
    if request.method == "GET":
        profile = db.get_business_profile(user["id"]) or {
            "agent_name": "",
            "brokerage_name": "",
            "company_name": "",
            "timezone": "America/Denver",
        }
        return jsonify({"profile": profile})

    data = request.get_json(silent=True) or {}
    profile = db.update_business_profile(
        user["id"],
        agent_name=str(data.get("agent_name") or ""),
        brokerage_name=str(data.get("brokerage_name") or ""),
        company_name=str(data.get("company_name") or ""),
        timezone=str(data.get("timezone") or "") or None,
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

    # Shared CRM lead upsert (same as SMS) — match by user + E.164 phone.
    lead_id, _created, lead = upsert_crm_lead(
        user["id"],
        cleaned["phone_number"],
        cleaned,
        source=VOICE_SOURCE,
        initial_status="new",
        touch_call=False,
        assigned_user_id=user["id"],
    )
    cleaned["lead_id"] = lead_id

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
        if normalize_lead_status(lead.get("status")) == "new":
            crm_db.set_lead_status(
                user["id"], lead_id, "attempting_contact", from_automation=True
            )
        record_voice_call_started(
            user["id"],
            lead_id,
            call_id,
            phone_number=cleaned["phone_number"],
            provider_call_id=result.get("provider_call_id"),
        )
    except VoiceProviderError as exc:
        logger.warning("Voice call failed to start: %s", type(exc).__name__)
        db.update_voice_call_provider(call_id, None, "failed")
        crm_db.add_lead_activity(
            lead_id,
            user["id"],
            "voice_call_failed",
            "AI call could not be placed",
            {"voice_call_id": call_id, "status": "failed"},
            actor_user_id=user["id"],
        )
        return jsonify({"error": str(exc), "lead_id": lead_id, "id": call_id}), 503

    return jsonify({
        "id": call_id,
        "lead_id": lead_id,
        "provider_call_id": result["provider_call_id"],
        "status": "started",
    }), 201


def _attach_latest_outbound_status(status_payload, user_id, *, lead_id=None, phone_number=None):
    """Merge tenant-scoped latest outbound SMS diagnostics into status payload."""
    from sms_status_model import latest_outbound_diagnostics

    # Never trust a browser-supplied tenant id — always use session user_id.
    latest_outbound = db.get_latest_outbound_sms(
        user_id, lead_id=lead_id, phone_number=phone_number
    )
    diagnostics = latest_outbound_diagnostics(latest_outbound)
    status_payload.update(diagnostics)
    # Backward-compatible keys used by older UI snippets.
    if diagnostics.get("has_outbound"):
        status_payload["latest_send_status"] = diagnostics["latest_sms_status"]
    else:
        status_payload["latest_send_status"] = None
    return status_payload


@app.route("/sms/messages")
@auth.subscription_required
def sms_messages():
    user = auth.get_current_user()
    messages = db.list_sms_messages(user["id"])
    provider = get_sms_provider()
    latest = db.latest_failed_sms_error(user["id"])
    latest_code = parse_provider_code_from_error_message(
        (latest or {}).get("error_message")
    )
    if hasattr(provider, "configuration_status"):
        status = provider.configuration_status(
            latest_error_code=latest_code,
            latest_error_message=(latest or {}).get("error_message"),
        )
    else:
        status = provider.get_sender_information()
    status["sms_provider"] = getattr(provider, "name", config.SMS_PROVIDER)
    status["provider"] = status.get("provider") or status["sms_provider"]
    from sms_authorization import (
        TOLL_FREE_VERIFICATION_UI_MSG,
        get_telnyx_toll_free_verification_status,
        is_sms_sending_enabled,
        is_telnyx_toll_free_verified,
    )
    from sms_status_model import format_phone_display

    sending_enabled = bool(status.get("sms_sending_enabled", is_sms_sending_enabled()))
    status["sms_sending_enabled"] = sending_enabled
    verification_status = status.get("toll_free_verification_status")
    if verification_status is None and (config.SMS_PROVIDER or "").lower() == "telnyx":
        verification_status = get_telnyx_toll_free_verification_status() or "unknown"
        status["toll_free_verification_status"] = verification_status
    verification_blocked = (
        (config.SMS_PROVIDER or "").lower() == "telnyx"
        and not is_telnyx_toll_free_verified()
    )
    lead_id = request.args.get("lead_id", type=int)
    phone_number = (request.args.get("phone_number") or "").strip() or None
    _attach_latest_outbound_status(
        status, user["id"], lead_id=lead_id, phone_number=phone_number
    )
    # Prefer latest outbound failure fields over the older failed-only query when present.
    if status.get("has_outbound") and not status.get("latest_error_code"):
        # Keep configuration_status error fields only when the latest attempt failed.
        pass
    return jsonify({
        "send_configured": bool(status.get("send_configured", provider.is_configured())),
        "sms_sending_enabled": sending_enabled,
        "toll_free_verification_status": verification_status,
        "toll_free_verification_blocked": verification_blocked,
        "verification_block_message": (
            TOLL_FREE_VERIFICATION_UI_MSG if verification_blocked else None
        ),
        "coach_configured": sms_coach.is_configured(),
        "sms_provider": status["sms_provider"],
        "provider_status": status,
        "latest_outbound": {
            k: status.get(k)
            for k in (
                "has_outbound",
                "latest_sms_status",
                "latest_sms_status_label",
                "latest_sms_destination",
                "latest_sms_destination_display",
                "latest_sms_submitted_at",
                "latest_sms_delivered_at",
                "latest_sms_message_id",
                "latest_telnyx_error_code",
                "latest_correlation_id",
                "empty_state_message",
            )
        },
        # Legacy key retained for older clients; UI prefers provider_status.
        "twilio_status": status if status.get("provider") == "twilio" else None,
        "messages": [
            {
                "id": m["id"],
                "lead_id": m.get("lead_id"),
                "persona_name": m.get("persona_name"),
                "lead_name": m.get("lead_name"),
                "phone_number": m.get("phone_number"),
                "phone_number_display": format_phone_display(m.get("phone_number")),
                "lead_type": m.get("lead_type"),
                "direction": m.get("direction"),
                "status": m.get("status"),
                "provider_message_id": m.get("provider_message_id"),
                "message_body": m.get("message_body"),
                "error_message": m.get("error_message"),
                "created_at": m.get("created_at"),
                "sent_at": m.get("sent_at"),
                "submitted_at": m.get("submitted_at"),
                "delivered_at": m.get("delivered_at"),
                "failed_at": m.get("failed_at"),
            }
            for m in messages
        ],
    })


@app.route("/sms/status")
@auth.subscription_required
def sms_status():
    """Account-owner SMS configuration / latest outbound send diagnostics (no secrets)."""
    user = auth.get_current_user()
    provider = get_sms_provider()
    latest = db.latest_failed_sms_error(user["id"])
    latest_code = parse_provider_code_from_error_message(
        (latest or {}).get("error_message")
    )
    if hasattr(provider, "configuration_status"):
        payload = provider.configuration_status(
            latest_error_code=latest_code,
            latest_error_message=(latest or {}).get("error_message"),
        )
    else:
        payload = provider.get_sender_information()
        payload["latest_error_code"] = latest_code
        payload["latest_error_message"] = (latest or {}).get("error_message")
    payload["provider"] = getattr(provider, "name", config.SMS_PROVIDER)
    payload["sms_provider"] = payload["provider"]
    payload["trial_mode"] = bool(getattr(config, "TELNYX_TRIAL_MODE", False)) and (
        (config.SMS_PROVIDER or "").lower() == "telnyx"
    )
    payload["trial_message"] = (
        "Telnyx trial mode is active. Messages can only be sent to the verified test phone number."
        if payload.get("trial_mode")
        else None
    )
    lead_id = request.args.get("lead_id", type=int)
    phone_number = (request.args.get("phone_number") or "").strip() or None
    _attach_latest_outbound_status(
        payload, user["id"], lead_id=lead_id, phone_number=phone_number
    )
    # Never echo secrets even if misconfigured upstream.
    for secret_key in (
        "api_key",
        "public_key",
        "auth_token",
        "account_sid",
        "messaging_service_sid",
    ):
        payload.pop(secret_key, None)
    return jsonify(payload)


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
        return jsonify({"error": "Certify contact SMS consent before sending."}), 400

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
    if not data.get("compliance_confirmed"):
        return jsonify({"error": "Confirm contact SMS consent certification before sending."}), 400

    from sms_outbound import send_authorized_sms

    suggested_id = insight.get("suggested_message_id")
    if suggested_id:
        db.update_sms_message_send_result(suggested_id, status="draft", error_message=None)
        db.update_sms_message_body(suggested_id, user["id"], message_body, direction="outbound")

    result, err, status = send_authorized_sms(
        user["id"],
        lead["id"],
        message_body,
        source_page="sms_suggestion",
        compliance_confirmed=True,
        message_id=suggested_id,
    )
    if err:
        return jsonify({"error": err, **(result or {})}), status

    db.update_insight_status(insight_id, user["id"], "sent")
    db.record_tool_usage(user["id"], "ai_sms", "suggestion_sent")
    return jsonify({
        "ok": True,
        **result,
        "consent_status": "confirmed",
        "opt_out_status": lead.get("opt_out_status") or "active",
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }), 201


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
        return jsonify({
            "error": error,
            "stage": "validation",
            "error_category": "validation",
            "to_number": (data or {}).get("phone_number"),
        }), 400

    persona = db.get_voice_persona(cleaned["persona_id"], user["id"])
    if not persona:
        return jsonify({
            "error": "Selected persona was not found.",
            "stage": "validation",
            "error_category": "not_found",
        }), 404

    from lead_service import SMS_SOURCE, normalize_phone_e164
    from sms_outbound import send_authorized_sms

    normalized_to = normalize_phone_e164(cleaned["phone_number"])
    cleaned["phone_number"] = normalized_to
    try:
        lead_id, _created, _lead = upsert_crm_lead(
            user["id"],
            normalized_to,
            cleaned,
            source=SMS_SOURCE,
            touch_sms=True,
            assigned_user_id=user["id"],
        )
    except Exception:
        import uuid as _uuid

        correlation_id = _uuid.uuid4().hex[:12]
        logger.exception(
            "SMS lead upsert failed correlation_id=%s user_id=%s to=%s",
            correlation_id,
            user["id"],
            normalized_to,
        )
        return jsonify({
            "error": (
                "TopAI could not prepare the SMS send. "
                f"Reference: {correlation_id}."
            ),
            "stage": "database",
            "error_category": "database_error",
            "correlation_id": correlation_id,
            "to_number": normalized_to,
            "send_status": "failed",
        }), 500

    provider = get_sms_provider()
    if not cleaned.get("send_now"):
        message_id = db.create_sms_message(
            user_id=user["id"],
            persona_id=persona["id"],
            provider=config.SMS_PROVIDER,
            data=cleaned,
            status="draft",
            lead_id=lead_id,
            direction="outbound",
            consent_status="unknown",
            opt_out_status="active",
        )
        db.record_tool_usage(user["id"], "ai_sms", "saved")
        from sms_authorization import is_sms_sending_enabled

        return jsonify({
            "id": message_id,
            "lead_id": lead_id,
            "status": "draft",
            "message_body": cleaned["message_body"],
            "to_number": normalized_to,
            "send_configured": provider.is_configured(),
            "sms_sending_enabled": is_sms_sending_enabled(),
        }), 201

    from sms_authorization import check_telnyx_toll_free_send_allowed

    toll_ok, toll_err = check_telnyx_toll_free_send_allowed()
    if not toll_ok:
        return jsonify({
            "error": toll_err,
            "stage": "validation",
            "error_category": "toll_free_verification",
            "to_number": normalized_to,
            "send_status": "failed",
        }), 403

    result, err, status = send_authorized_sms(
        user["id"],
        lead_id,
        cleaned["message_body"],
        source_page="ai_sms_compose",
        compliance_confirmed=True,
        persona_id=persona["id"],
    )
    if err:
        body = {"error": err, **(result or {})}
        body.setdefault("to_number", normalized_to)
        return jsonify(body), status
    db.record_tool_usage(user["id"], "ai_sms", "sent")
    return jsonify({
        **result,
        "send_configured": True,
        "sms_sending_enabled": True,
        "to_number": (result or {}).get("to_number") or normalized_to,
    }), status


@app.route("/sms/test", methods=["POST"])
@auth.subscription_required
@limiter.limit("5 per minute", key_func=_user_rate_limit_key)
def sms_test_send():
    """Secure server-side test send. Lead phones still require certification + authorization."""
    user = auth.get_current_user()
    data = request.get_json(silent=True)
    cleaned, error = validate_sms_test_payload(data)
    if error:
        return jsonify({"ok": False, "error": error}), 400

    from sms_authorization import can_send_sms, check_telnyx_toll_free_send_allowed, require_tenant_sender
    from sms_outbound import send_authorized_sms

    toll_ok, toll_err = check_telnyx_toll_free_send_allowed()
    if not toll_ok:
        return jsonify({"ok": False, "error": toll_err}), 403

    existing_lead = db.get_lead_by_phone(user["id"], cleaned["to"])
    if existing_lead:
        if not data.get("compliance_confirmed"):
            return jsonify({
                "ok": False,
                "error": "Confirm contact SMS consent certification before sending to a CRM lead.",
            }), 400
        result, err, status = send_authorized_sms(
            user["id"],
            existing_lead["id"],
            cleaned["message"],
            source_page="sms_test",
            compliance_confirmed=True,
            skip_quiet_hours=True,
        )
        if err:
            return jsonify({"ok": False, "error": err, **(result or {})}), status
        return jsonify({"ok": True, **result, "to": cleaned["to"]}), status

    # Also enforce trial for non-lead test destinations
    from sms_authorization import check_telnyx_trial_destination

    trial_ok, trial_err = check_telnyx_trial_destination(cleaned["to"])
    if not trial_ok:
        return jsonify({"ok": False, "error": trial_err, "provider": "telnyx"}), 403

    sender, sender_err = require_tenant_sender(user["id"])
    if sender_err:
        return jsonify({"ok": False, "error": sender_err}), 403
    provider = get_sms_provider()
    if not provider.is_configured():
        return jsonify({"ok": False, "error": "SMS provider is not configured."}), 503

    message_id = db.create_sms_message(
        user_id=user["id"],
        persona_id=None,
        provider=config.SMS_PROVIDER,
        data={
            "lead_name": "SMS Test",
            "phone_number": cleaned["to"],
            "message_body": cleaned["message"],
            "lead_type": "test",
            "desired_outcome": "verify SMS connectivity",
        },
        status="draft",
    )
    try:
        result = provider.send_sms(
            cleaned["to"],
            cleaned["message"],
            status_callback=sms_status_callback_url(),
            from_number=sender.get("sender_number"),
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
        db.update_sms_message_send_result(message_id, status="failed", error_message=str(exc))
        return jsonify(exc.to_public_dict() | {"ok": False}), 503
    except Exception:
        logger.exception("SMS test send failed with unexpected error")
        safe = "SMS could not be sent due to an internal application error."
        db.update_sms_message_send_result(message_id, status="failed", error_message=safe)
        return jsonify({"ok": False, "error": safe}), 500


@app.route("/webhooks/telnyx/messaging", methods=["POST"])
@limiter.exempt
def telnyx_messaging_webhook():
    """Telnyx Messaging Profile API V2 webhook. Ed25519 signature required in production."""
    from sms_providers.telnyx import TelnyxSMSProvider
    import telnyx_webhooks as txwh

    provider = TelnyxSMSProvider()
    if not provider.validate_webhook(request):
        return jsonify({"ok": False, "error": "Invalid webhook signature"}), 401
    payload = request.get_json(silent=True) or {}
    try:
        result, status = txwh.handle_messaging_webhook(payload, app=app)
        return jsonify(result), status
    except Exception:
        logger.exception("Telnyx webhook failed")
        # Ack to avoid infinite retries on unexpected bugs after persistence attempt
        return jsonify({"ok": False}), 200


@app.route("/webhooks/simpletexting/inbound", methods=["POST"])
@app.route("/webhooks/simpletexting/delivery", methods=["POST"])
@app.route("/webhooks/simpletexting/unsubscribe", methods=["POST"])
@limiter.exempt
def simpletexting_webhooks():
    """SimpleTexting JSON webhooks. Auth via ?token=SIMPLETEXTING_WEBHOOK_SECRET."""
    from sms_providers.simpletexting import SimpleTextingSMSProvider
    import simpletexting_webhooks as stwh

    provider = SimpleTextingSMSProvider()
    if not provider.validate_webhook(request):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    payload = request.get_json(silent=True)
    if payload is None:
        # Also accept form for legacy forwarding
        payload = {k: request.values.get(k) for k in request.values.keys()}
    path = request.path.rstrip("/")
    try:
        if path.endswith("/inbound"):
            result, status = stwh.handle_inbound(payload, app=app)
            return jsonify(result), status
        if path.endswith("/delivery"):
            result, status = stwh.handle_delivery(payload)
            return jsonify(result), status
        if path.endswith("/unsubscribe"):
            result, status = stwh.handle_unsubscribe(payload)
            return jsonify(result), status
    except Exception:
        logger.exception("SimpleTexting webhook failed")
        return jsonify({"ok": False}), 500
    return jsonify({"ok": False, "error": "unknown"}), 404


@app.route("/webhook/sms/inbound", methods=["POST"])
@app.route("/webhooks/twilio/sms", methods=["POST"])
@limiter.exempt
@validate_twilio_request
def sms_inbound_webhook():
    """
    Twilio inbound SMS webhook.

    Preferred production URL: POST {APP_URL}/webhooks/twilio/sms
    Legacy alias: POST {APP_URL}/webhook/sms/inbound

    Validates Twilio signature, acks immediately with empty TwiML, and never auto-sends.
    Claude coaching runs in a background thread after the ack path.
    """
    from sms_inbound import parse_inbound_form, process_inbound_sms

    payload = parse_inbound_form(request.form)
    message_sid = payload.get("message_sid")
    logger.info(
        "Inbound SMS webhook received sid=%s path=%s",
        message_sid or "none",
        request.path,
    )
    try:
        result = process_inbound_sms(payload, defer_coach=True, app=app)
        logger.info(
            "Inbound SMS webhook result sid=%s status=%s tenant=%s lead=%s duplicate=%s ignored=%s",
            message_sid or "none",
            "ok" if result.get("ok") else "ignored",
            result.get("owner_id"),
            result.get("lead_id"),
            bool(result.get("duplicate")),
            result.get("ignored"),
        )
    except Exception:
        logger.exception("Failed to process inbound SMS sid=%s", message_sid or "none")

    return _twiml_empty_response()


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

    webhook_fields = {
        k: normalized.get(k)
        for k in (
            "call_id",
            "provider_call_id",
            "status",
            "outcome",
            "transcript",
            "summary",
            "recording_url",
            "stereo_recording_url",
            "recording_duration_seconds",
            "recording_status",
            "transcript_url",
            "appointment_requested",
        )
    }
    updated = db.update_voice_call_from_webhook(**webhook_fields)
    if not updated:
        logger.warning(
            "Voice webhook did not match an existing call: provider_call_id=%s call_id=%s",
            normalized.get("provider_call_id"),
            normalized.get("call_id"),
        )
        return jsonify({"received": True}), 200

    call_row = None
    if normalized.get("provider_call_id"):
        call_row = db.get_voice_call_by_provider_id(normalized["provider_call_id"])
    if not call_row and normalized.get("call_id"):
        try:
            internal_id = int(normalized["call_id"])
        except (TypeError, ValueError):
            internal_id = None
        if internal_id is not None:
            with db.get_db() as conn:
                row = conn.execute(
                    "SELECT * FROM voice_calls WHERE id = ? LIMIT 1",
                    (internal_id,),
                ).fetchone()
                call_row = dict(row) if row else None

    if call_row and call_row.get("user_id"):
        try:
            apply_voice_call_webhook_to_lead(call_row["user_id"], call_row, normalized)
        except Exception:
            logger.exception("Failed to apply voice webhook to CRM lead")

    return jsonify({"received": True}), 200


@app.route("/api/external-leads/webhook/<provider_key>", methods=["POST"])
@limiter.limit("60 per minute")
def external_leads_webhook(provider_key):
    """Authenticated tenant webhook. No session auth — secret header required."""
    from external_leads.webhook import process_webhook

    secret = (
        request.headers.get("X-TopAI-Webhook-Secret")
        or request.headers.get("X-Webhook-Secret")
        or ""
    )
    body = request.get_json(silent=True)
    if body is None:
        body = {}
    result, error, status = process_webhook(provider_key, body, secret)
    if error:
        return jsonify({"ok": False, "error": error}), status
    return jsonify(
        {
            "ok": True,
            "action": result.get("action"),
            "lead_id": result.get("lead_id"),
            "duplicate_match": result.get("duplicate_match"),
            "pending_evidence_id": result.get("pending_evidence_id"),
            "sms_consent_status": "unverified",
            "sms_sending_blocked": True,
        }
    ), status


@app.route("/sms-consent", methods=["GET", "POST"])
@limiter.limit("20 per minute", methods=["POST"])
def sms_consent():
    """Public A2P SMS consent / real estate inquiry page. No auth, gate, or auto-SMS."""
    import sms_consent as sms_consent_mod

    campaign_source = (
        request.values.get("campaign_source")
        or request.values.get("source")
        or request.values.get("utm_campaign")
        or ""
    ).strip()[:120]

    form_defaults = {
        "form_name": "",
        "form_first_name": "",
        "form_last_name": "",
        "form_email": "",
        "form_phone": "",
        "form_message": "",
        "form_sms_consent": False,
        "form_campaign_source": campaign_source,
    }
    ctx = {
        **form_defaults,
        "error": None,
        "success": None,
        "sms_support_display": sms_consent_mod.SMS_SUPPORT_DISPLAY,
        "sms_consent_checkbox_text": sms_consent_mod.SMS_CONSENT_CHECKBOX_TEXT,
    }

    if request.method == "GET":
        return render_template("sms_consent.html", **ctx)

    cleaned, error = sms_consent_mod.validate_sms_consent_form(request.form)
    ctx.update(
        {
            "form_first_name": str(request.form.get("first_name") or "")[:60],
            "form_last_name": str(request.form.get("last_name") or "")[:60],
            "form_name": str(request.form.get("name") or "")[:120],
            "form_email": str(request.form.get("email") or "")[:200],
            "form_phone": str(request.form.get("phone") or "")[:32],
            "form_message": str(request.form.get("message") or "")[:2000],
            "form_campaign_source": str(
                request.form.get("campaign_source") or campaign_source or ""
            )[:120],
            # Preserve checked state only on validation error (never default checked on fresh GET).
            "form_sms_consent": str(request.form.get("sms_consent") or "").lower()
            in {"1", "true", "on", "yes"},
        }
    )
    if error:
        ctx["error"] = error
        return render_template("sms_consent.html", **ctx), 400

    source_url = seo.canonical_loc("/sms-consent")
    try:
        inquiry_id, created_new = sms_consent_mod.create_sms_consent_inquiry(
            name=cleaned["name"],
            first_name=cleaned["first_name"],
            last_name=cleaned["last_name"],
            email=cleaned.get("email"),
            phone_number=cleaned["phone_number"],
            message=cleaned["message"],
            sms_consent=cleaned["sms_consent"],
            source_url=source_url,
            campaign_source=cleaned.get("campaign_source") or campaign_source or None,
            ip_address=(request.headers.get("X-Forwarded-For") or request.remote_addr or "")
            .split(",")[0]
            .strip(),
            user_agent=request.headers.get("User-Agent"),
        )
    except Exception:
        logger.exception("Failed to save public SMS consent inquiry")
        ctx["error"] = "We could not save your inquiry right now. Please try again in a moment."
        return render_template("sms_consent.html", **ctx), 500

    logger.info(
        "Public SMS consent inquiry saved id=%s consent=%s created_new=%s",
        inquiry_id,
        cleaned["sms_consent"],
        created_new,
    )
    support = sms_consent_mod.SMS_SUPPORT_DISPLAY
    if cleaned["sms_consent"]:
        if created_new:
            ctx["success"] = (
                "Thanks — your inquiry and SMS consent were recorded. "
                "We will not send an automated text just because you submitted this form. "
                f"SMS support number: {support}. "
                "Message frequency varies. Message and data rates may apply. "
                "Reply STOP to opt out or HELP for help."
            )
        else:
            ctx["success"] = (
                "Thanks — we already have your SMS consent on file for this number. "
                "Your inquiry details were updated. We will not send an automated text "
                f"just because you submitted this form. SMS support number: {support}."
            )
    else:
        ctx["success"] = (
            "Thanks — your inquiry was recorded. Because you did not check the SMS consent box, "
            "we will not send you SMS about this inquiry. "
            f"SMS support number: {support}."
        )
    ctx.update(form_defaults)
    ctx["form_campaign_source"] = campaign_source
    return render_template("sms_consent.html", **ctx)


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
        return redirect(url_for("subscriber_app"))
    return render_template(
        "tutorial.html",
        email=user["email"],
        has_billing_portal=bool(user.get("stripe_customer_id")),
        active_nav="tutorial",
    )


@app.route("/dashboard")
def dashboard():
    user = auth.get_current_user()
    if not user or not auth.user_has_active_subscription(user):
        return redirect(url_for("subscriber_app"))
    user_timezone = db.get_user_timezone(user["id"])
    windows = crm_db._follow_up_windows(user["id"], timezone_name=user_timezone)
    local_date = (request.args.get("local_date") or "").strip()[:10] or windows.local_date
    metrics = db.get_dashboard_metrics(user["id"])
    pipeline = crm_db.get_pipeline_metrics(
        user["id"],
        timezone_name=user_timezone,
        windows=windows,
    )
    needs = crm_db.list_needs_attention(user["id"], local_date=local_date)[:8]
    notifications = crm_db.list_notifications(user["id"], unread_only=True, limit=8)
    # Destination filters no longer need browser offset — account TZ is used server-side.
    date_qs = ""
    return render_template(
        "dashboard.html",
        email=user["email"],
        has_billing_portal=bool(user.get("stripe_customer_id")),
        active_nav="dashboard",
        metrics=metrics,
        pipeline=pipeline,
        needs_attention=needs,
        notifications=notifications,
        status_label=status_label,
        local_date=local_date or "",
        tz_offset_minutes=None,
        date_qs=date_qs,
        user_timezone=user_timezone,
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
