import logging
import secrets

import stripe
from anthropic import Anthropic
from flask import Flask, jsonify, redirect, render_template, request, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.exceptions import HTTPException

import auth
import config
import db
from validation import validate_listing_payload, validate_script_payload

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
    api_paths = ("/generate", "/generate-script", "/verify", "/session-status", "/webhook")
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


@app.route("/terms")
def terms():
    return render_template("legal.html", title="Terms of Service", doc="terms")


@app.route("/privacy")
def privacy():
    return render_template("legal.html", title="Privacy Policy", doc="privacy")


@app.route("/pricing")
def pricing():
    return render_template("pricing.html")


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
            model="claude-opus-4-6",
            max_tokens=1500,
            messages=[{"role": "user", "content": build_listing_prompt(cleaned)}],
        )
        raw = message.content[0].text
        listing = _extract_section(raw, "LISTING DESCRIPTION", "SOCIAL POSTS")
        social = _extract_section(raw, "SOCIAL POSTS", "PROSPECT EMAIL")
        email = _extract_section(raw, "PROSPECT EMAIL", None)
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
            model="claude-opus-4-6",
            max_tokens=1200,
            messages=[{"role": "user", "content": build_script_prompt(cleaned)}],
        )
        raw = message.content[0].text
        opening = _extract_section(raw, "OPENING SCRIPT", "OBJECTION HANDLERS")
        objections = _extract_section(raw, "OBJECTION HANDLERS", "VOICEMAIL SCRIPT")
        voicemail = _extract_section(raw, "VOICEMAIL SCRIPT", None)
        return jsonify({"opening": opening.strip(), "objections": objections.strip(), "voicemail": voicemail.strip()})
    except Exception:
        logger.exception("Script generation failed")
        return jsonify({"error": "Generation failed. Please try again."}), 500


if __name__ == "__main__":
    print(f"\nTopAI Real Estate Tools running -> http://localhost:{config.PORT}\n")
    app.run(host="0.0.0.0", debug=False, port=config.PORT)
