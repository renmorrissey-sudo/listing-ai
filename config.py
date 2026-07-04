import os
import sys

from dotenv import load_dotenv

load_dotenv()


def _env(name, default=None):
    return os.environ.get(name, default)


ENV = _env("ENV", "development")
IS_PRODUCTION = ENV == "production"

ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
FLASK_SECRET_KEY = _env("FLASK_SECRET_KEY")
DATABASE_PATH = _env("DATABASE_PATH", "real_estate.db")

STRIPE_SECRET_KEY = _env("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = _env("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_ID = _env("STRIPE_PRICE_ID")
APP_URL = _env("APP_URL", "http://localhost:8080")
BUSINESS_NAME = _env("BUSINESS_NAME", "TOPAIRE REAL ESTATE")
PRODUCT_NAME = _env("PRODUCT_NAME", "TopAI Real Estate Tools")
CONTACT_EMAIL = _env("CONTACT_EMAIL", "ren.morrissey@gmail.com")
SUBSCRIPTION_PRICE = _env("SUBSCRIPTION_PRICE", "$49/month")
TRIAL_OFFER = _env("TRIAL_OFFER", "50% off first month with promo code TRIAL50")


def _email_list(name):
    raw = _env(name, "")
    return {email.strip().lower() for email in raw.split(",") if email.strip()}


FREE_ACCESS_EMAILS = _email_list("FREE_ACCESS_EMAILS")

# Skip subscription checks locally when Stripe is not configured.
SUBSCRIPTION_REQUIRED = _env("SUBSCRIPTION_REQUIRED", "true").lower() == "true"
if not STRIPE_SECRET_KEY and not IS_PRODUCTION:
    SUBSCRIPTION_REQUIRED = False

PORT = int(_env("PORT", 8080))


def validate_config():
    missing = []
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if not FLASK_SECRET_KEY:
        missing.append("FLASK_SECRET_KEY")
    if IS_PRODUCTION:
        if not STRIPE_SECRET_KEY:
            missing.append("STRIPE_SECRET_KEY")
        if not STRIPE_WEBHOOK_SECRET:
            missing.append("STRIPE_WEBHOOK_SECRET")
        if SUBSCRIPTION_REQUIRED and not STRIPE_PRICE_ID:
            missing.append("STRIPE_PRICE_ID")
    if missing:
        print(f"FATAL: Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
