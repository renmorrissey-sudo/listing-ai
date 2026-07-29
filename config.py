import os
import sys
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()


def _env(name, default=None):
    return os.environ.get(name, default)


def _env_strip(name, default=None):
    value = _env(name, default)
    return value.strip() if isinstance(value, str) else value


def _env_bool(name, default=False):
    raw = _env(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


# Environment separation: production | staging | development | test
APP_ENV = (_env_strip("APP_ENV") or _env_strip("ENV") or "development").lower()
if APP_ENV not in {"production", "staging", "development", "test"}:
    print(f"FATAL: APP_ENV must be production|staging|development|test (got {APP_ENV!r})", file=sys.stderr)
    sys.exit(1)

ENV = APP_ENV  # backward-compatible alias
IS_PRODUCTION = APP_ENV == "production"
IS_STAGING = APP_ENV == "staging"
IS_TEST = APP_ENV == "test"

ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
CLAUDE_MODEL = _env_strip("CLAUDE_MODEL") or "claude-opus-4-6"
FLASK_SECRET_KEY = _env("FLASK_SECRET_KEY")

# Database configuration
# Production/staging MUST use Railway-managed Postgres via DATABASE_URL.
# SQLite (DATABASE_PATH) is allowed only for development/test.
DATABASE_URL = _env_strip("DATABASE_URL") or ""
DATABASE_PATH = _env("DATABASE_PATH", "real_estate.db")

# Destructive flags — must never be enabled in production/staging.
ALLOW_DESTRUCTIVE_DB_RESET = _env_bool("ALLOW_DESTRUCTIVE_DB_RESET", False)
ALLOW_SQLITE_TABLE_REBUILD = _env_bool("ALLOW_SQLITE_TABLE_REBUILD", False)
RUN_DEMO_SEED_ON_STARTUP = _env_bool("RUN_DEMO_SEED_ON_STARTUP", False)

if DATABASE_URL:
    # Normalize older postgres:// URLs used by some hosts
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
    DB_ENGINE = "postgres"
else:
    DB_ENGINE = "sqlite"

STRIPE_SECRET_KEY = _env("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = _env("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_ID = _env("STRIPE_PRICE_ID")
APP_URL = _env("APP_URL", "http://localhost:8080")

# Public website copy. Keep these as constants so malformed host environment
# variables cannot leak into legal/pricing pages.
BUSINESS_NAME = "TopAI RE Tools"
PRODUCT_NAME = "TopAI Real Estate Tools"
LEGAL_ENTITY_NAME = "Sky Blue Holdings LLC"
CONTACT_EMAIL = "support@topairealestatetools.com"
SUBSCRIPTION_PRICE = "$49/month"
TRIAL_OFFER = "50% off first month with promo code TRIAL50"

# Transactional email (password reset). Prefer SendGrid; SMTP is fallback.
SENDGRID_API_KEY = _env_strip("SENDGRID_API_KEY")
SMTP_HOST = _env_strip("SMTP_HOST")
SMTP_PORT = int(_env("SMTP_PORT", "587") or "587")
SMTP_USERNAME = _env_strip("SMTP_USERNAME")
SMTP_PASSWORD = _env_strip("SMTP_PASSWORD")
SMTP_FROM_EMAIL = _env_strip("SMTP_FROM_EMAIL") or CONTACT_EMAIL
SMTP_USE_TLS = (_env("SMTP_USE_TLS", "true") or "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _email_list(name):
    raw = _env(name, "")
    return {email.strip().lower() for email in raw.split(",") if email.strip()}


FREE_ACCESS_EMAILS = _email_list("FREE_ACCESS_EMAILS")

VOICE_PROVIDER = _env("VOICE_PROVIDER", "vapi").lower()
VOICE_PROVIDER_API_KEY = _env("VOICE_PROVIDER_API_KEY")
VOICE_PROVIDER_WEBHOOK_SECRET = _env("VOICE_PROVIDER_WEBHOOK_SECRET")
# Prefer the lead-qualifier assistant ID; fall back to legacy VOICE_DEFAULT_ASSISTANT_ID.
VOICE_DEFAULT_ASSISTANT_ID = (
    _env_strip("REAL_ESTATE_LEAD_QUALIFIER_ASSISTANT_ID")
    or _env_strip("VOICE_DEFAULT_ASSISTANT_ID")
    or ""
)
REAL_ESTATE_LEAD_QUALIFIER_ASSISTANT_ID = VOICE_DEFAULT_ASSISTANT_ID
VOICE_PHONE_NUMBER_ID = _env("VOICE_PHONE_NUMBER_ID")
VOICE_CALL_FROM_NUMBER = _env("VOICE_CALL_FROM_NUMBER")
VOICE_DAILY_CALL_LIMIT = int(_env("VOICE_DAILY_CALL_LIMIT", "20"))

SMS_PROVIDER = (_env("SMS_PROVIDER", "twilio") or "twilio").lower().strip()
# TEMP diagnostic outbound auth: Account SID + Auth Token (not API keys).
TWILIO_ACCOUNT_SID = _env_strip("TWILIO_ACCOUNT_SID") or _env_strip("SMS_TWILIO_ACCOUNT_SID")
TWILIO_API_KEY_SID = _env_strip("TWILIO_API_KEY_SID")
TWILIO_API_KEY_SECRET = _env_strip("TWILIO_API_KEY_SECRET")
# Used for outbound SMS (temporary diagnostic) and webhook RequestValidator.
TWILIO_AUTH_TOKEN = _env_strip("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = _env_strip("TWILIO_PHONE_NUMBER") or _env_strip("SMS_FROM_NUMBER")
# Preferred for US A2P 10DLC: associate sends with an approved Messaging Service / campaign.
TWILIO_MESSAGING_SERVICE_SID = _env_strip("TWILIO_MESSAGING_SERVICE_SID")
# SimpleTexting master account (platform credentials — never expose to tenants).
SIMPLETEXTING_API_TOKEN = _env_strip("SIMPLETEXTING_API_TOKEN")
SIMPLETEXTING_WEBHOOK_SECRET = _env_strip("SIMPLETEXTING_WEBHOOK_SECRET")
# Dev/pilot fallback only — never implicit sender for unconfigured tenants.
SIMPLETEXTING_PHONE_NUMBER = _env_strip("SIMPLETEXTING_PHONE_NUMBER")
SIMPLETEXTING_API_BASE = (
    _env_strip("SIMPLETEXTING_API_BASE") or "https://api-app2.simpletexting.com/v2"
)
# Telnyx Messaging API V2 (active when SMS_PROVIDER=telnyx).
TELNYX_API_KEY = _env_strip("TELNYX_API_KEY")
TELNYX_MESSAGING_PROFILE_ID = _env_strip("TELNYX_MESSAGING_PROFILE_ID")
# Production toll-free messaging / SMS program number (E.164).
TELNYX_PHONE_NUMBER = _env_strip("TELNYX_PHONE_NUMBER")
TELNYX_PUBLIC_KEY = _env_strip("TELNYX_PUBLIC_KEY")
# Public SMS support display used on /sms-consent and compliance copy.
SMS_SUPPORT_DISPLAY = _env_strip("SMS_SUPPORT_DISPLAY") or "(888) 821-0810"
SMS_SUPPORT_E164 = _env_strip("SMS_SUPPORT_E164") or "+18888210810"
TELNYX_TRIAL_MODE = (_env("TELNYX_TRIAL_MODE", "true") or "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
TELNYX_VERIFIED_TEST_NUMBER = _env_strip("TELNYX_VERIFIED_TEST_NUMBER")
TELNYX_API_BASE = _env_strip("TELNYX_API_BASE") or "https://api.telnyx.com/v2"
TELNYX_WEBHOOK_TOLERANCE_SECONDS = int(_env("TELNYX_WEBHOOK_TOLERANCE_SECONDS", "300"))
# Customer-facing toll-free verification badge for diagnostics (pending|verified|unknown).
TELNYX_TOLL_FREE_VERIFICATION_STATUS = (
    _env_strip("TELNYX_TOLL_FREE_VERIFICATION_STATUS") or "pending"
).lower()
SMS_PROVIDER_MSGS_PER_SECOND = float(_env("SMS_PROVIDER_MSGS_PER_SECOND", "1"))

CONSENT_UPLOAD_DIR = _env_strip("CONSENT_UPLOAD_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "private_uploads", "consent_evidence"
)
CONSENT_UPLOAD_MAX_BYTES = int(_env("CONSENT_UPLOAD_MAX_BYTES", str(5 * 1024 * 1024)))
SMS_DAILY_LIMIT = int(_env("SMS_DAILY_LIMIT", "50"))
SMS_CAMPAIGN_MAX_RECIPIENTS = int(_env("SMS_CAMPAIGN_MAX_RECIPIENTS", "2000"))
SMS_IMPORT_MAX_ROWS = int(_env("SMS_IMPORT_MAX_ROWS", "5000"))
SMS_MSGS_PER_MINUTE = int(_env("SMS_MSGS_PER_MINUTE", "30"))
SMS_MSGS_PER_HOUR = int(_env("SMS_MSGS_PER_HOUR", "500"))
SMS_MSGS_PER_DAY = int(_env("SMS_MSGS_PER_DAY", "2000"))
SMS_MAX_PER_CONTACT_PER_DAY = int(_env("SMS_MAX_PER_CONTACT_PER_DAY", "3"))
SMS_MAX_RETRIES = int(_env("SMS_MAX_RETRIES", "5"))
SMS_QUIET_HOURS_START = int(_env("SMS_QUIET_HOURS_START", "21"))
SMS_QUIET_HOURS_END = int(_env("SMS_QUIET_HOURS_END", "8"))
SMS_TERMS_VERSION = _env_strip("SMS_TERMS_VERSION") or "sms_terms_v1_2026_07"
SMS_CERT_TEXT_VERSION_ONE_TO_ONE = "one_to_one_cert_v1"
SMS_CERT_TEXT_VERSION_CAMPAIGN = "campaign_cert_v1"
SMS_IMPORT_UPLOAD_DIR = _env_strip("SMS_IMPORT_UPLOAD_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "private_uploads", "sms_imports"
)
SMS_IMPORT_MAX_BYTES = int(_env("SMS_IMPORT_MAX_BYTES", str(10 * 1024 * 1024)))

# Skip subscription checks locally when Stripe is not configured.
SUBSCRIPTION_REQUIRED = _env("SUBSCRIPTION_REQUIRED", "true").lower() == "true"
if not STRIPE_SECRET_KEY and APP_ENV in {"development", "test"}:
    SUBSCRIPTION_REQUIRED = False

PORT = int(_env("PORT", 8080))


def _database_url_is_sqlite(url: str) -> bool:
    return (url or "").lower().startswith("sqlite:")


def _host_is_local_or_temporary(url: str) -> bool:
    if not url:
        return True
    if _database_url_is_sqlite(url):
        return True
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in {"", "localhost", "127.0.0.1", "0.0.0.0", "::1"}:
        return True
    # Allow Railway private networking; block other obvious temp hosts.
    if host.endswith(".local"):
        return True
    path = (parsed.path or "").lower()
    if "/tmp/" in path or path.endswith("/tmp"):
        return True
    return False


def _looks_like_test_database(url: str) -> bool:
    lowered = (url or "").lower()
    return any(m in lowered for m in ("/test", "_test", "test_", "pytest", "ci_test"))


def validate_database_config():
    """Refuse unsafe production/staging database configuration."""
    errors = []

    if APP_ENV in {"production", "staging"}:
        if not DATABASE_URL:
            errors.append(
                "DATABASE_URL is required in production/staging "
                "(use Railway PostgreSQL; do not store paid-user data in SQLite)."
            )
        elif _database_url_is_sqlite(DATABASE_URL):
            errors.append("DATABASE_URL must not point to SQLite in production/staging.")
        elif DB_ENGINE != "postgres":
            errors.append("Production/staging must use PostgreSQL via DATABASE_URL.")
        elif _host_is_local_or_temporary(DATABASE_URL):
            errors.append(
                "DATABASE_URL host appears local or temporary; "
                "production/staging must use a managed persistent Postgres host."
            )
        elif _looks_like_test_database(DATABASE_URL):
            errors.append("Production/staging must not use a test database URL.")

        if ALLOW_DESTRUCTIVE_DB_RESET:
            errors.append("ALLOW_DESTRUCTIVE_DB_RESET must be false in production/staging.")
        if ALLOW_SQLITE_TABLE_REBUILD:
            errors.append("ALLOW_SQLITE_TABLE_REBUILD must be false in production/staging.")
        if RUN_DEMO_SEED_ON_STARTUP:
            errors.append("RUN_DEMO_SEED_ON_STARTUP must be false in production/staging.")

    if APP_ENV == "test" and DATABASE_URL and not _looks_like_test_database(DATABASE_URL):
        # Soft warning path: allow explicit test postgres URLs that include test markers.
        pass

    if errors:
        for err in errors:
            print(f"FATAL: {err}", file=sys.stderr)
        sys.exit(1)

    # Safe summary — never log DATABASE_URL, passwords, or other secrets.
    postgres_active = DB_ENGINE == "postgres" and bool(DATABASE_URL) and not _database_url_is_sqlite(
        DATABASE_URL
    )
    if postgres_active:
        parsed = urlparse(DATABASE_URL)
        print(
            "Database startup check: "
            f"app_env={APP_ENV} engine=postgres postgres_active=true "
            f"host={(parsed.hostname or 'unknown')} "
            f"db={(parsed.path or '').lstrip('/') or 'unknown'}",
            file=sys.stderr,
        )
    else:
        print(
            "Database startup check: "
            f"app_env={APP_ENV} engine={DB_ENGINE} postgres_active=false "
            f"path={DATABASE_PATH}",
            file=sys.stderr,
        )


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

    validate_database_config()

    # Safe Twilio startup checks — never log credential values.
    account_sid_ok = bool(TWILIO_ACCOUNT_SID and TWILIO_ACCOUNT_SID.startswith("AC"))
    auth_token_present = bool(TWILIO_AUTH_TOKEN)
    print(
        "Twilio startup check: "
        f"account_sid_starts_with_AC={account_sid_ok} "
        f"auth_token_present={auth_token_present} "
        f"anthropic_api_key_present={bool(ANTHROPIC_API_KEY)}",
        file=sys.stderr,
    )
