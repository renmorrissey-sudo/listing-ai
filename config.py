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
ASK_TOPAI_MODEL = _env_strip("ASK_TOPAI_MODEL") or "claude-sonnet-5"
# Cold-call scripts are short, latency-sensitive generations: use the faster
# Sonnet model already used by Ask TopAI, not the Opus model used for listings.
SCRIPT_CLAUDE_MODEL = _env_strip("SCRIPT_CLAUDE_MODEL") or "claude-sonnet-5"
OPENAI_API_KEY = _env("OPENAI_API_KEY")
ASK_TOPAI_REALTIME_MODEL = _env_strip("ASK_TOPAI_REALTIME_MODEL") or "gpt-realtime-2.1"
ASK_TOPAI_REALTIME_VOICE = _env_strip("ASK_TOPAI_REALTIME_VOICE") or "marin"
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
STRIPE_PUBLISHABLE_KEY = _env_strip("STRIPE_PUBLISHABLE_KEY") or _env_strip(
    "STRIPE_PUBLIC_KEY"
)
STRIPE_WEBHOOK_SECRET = _env("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_ID = _env_strip("STRIPE_PRICE_ID")
# Optional Stripe Payment Method Configuration with Card enabled and Link disabled.
# When unset, Checkout falls back to payment_method_types=["card"].
STRIPE_SUBSCRIPTION_PAYMENT_METHOD_CONFIGURATION = _env_strip(
    "STRIPE_SUBSCRIPTION_PAYMENT_METHOD_CONFIGURATION"
) or ""
APP_URL = _env("APP_URL", "http://localhost:8080")

# Public website copy. Keep these as constants so malformed host environment
# variables cannot leak into legal/pricing pages.
BUSINESS_NAME = "TopAI RE Tools"
PRODUCT_NAME = "TopAI Real Estate Tools"
LEGAL_ENTITY_NAME = "Sky Blue Holdings LLC"
CONTACT_EMAIL = "support@topairealestatetools.com"
# Prefer a well-formed env override; fall back to the known plan copy.
_raw_subscription_price = _env_strip("SUBSCRIPTION_PRICE") or ""
if _raw_subscription_price.startswith("$") and "/month" in _raw_subscription_price.lower():
    SUBSCRIPTION_PRICE = _raw_subscription_price
else:
    SUBSCRIPTION_PRICE = "$49/month"
TRIAL_OFFER = "Get 50% off your first month with promo code TRIAL50 — this is a discount, not a free trial"
BILLING_FREQUENCY = "Billed monthly"

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

# Public registration / new Checkout gate (Railway env).
# Missing or any value other than true/1/yes/on => closed (safe default).
REGISTRATION_ENABLED = _env_bool("REGISTRATION_ENABLED", False)
# Optional comma-separated emails that may still register/checkout while closed.
REGISTRATION_ALLOWLIST = _email_list("REGISTRATION_ALLOWLIST")

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

SMS_PROVIDER = (_env("SMS_PROVIDER", "telnyx") or "telnyx").lower().strip()
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
# Production toll-free number. Staging must never send from it
# (validate_staging_safety() refuses to boot if it is configured there).
PRODUCTION_TELNYX_NUMBER = "+18888210810"
# Customer-facing toll-free verification badge for diagnostics (pending|verified|unknown).
# Source of truth for outbound eligibility when SMS_PROVIDER=telnyx — only "verified" allows sends.
TELNYX_TOLL_FREE_VERIFICATION_STATUS = (
    (_env_strip("TELNYX_TOLL_FREE_VERIFICATION_STATUS") or "").strip().lower()
)
SMS_PROVIDER_MSGS_PER_SECOND = float(_env("SMS_PROVIDER_MSGS_PER_SECOND", "1"))
# Optional override for Bulk SMS worker presence (true|false). Empty = auto-detect via recent job claims.
SMS_CAMPAIGN_WORKER_AVAILABLE = _env_strip("SMS_CAMPAIGN_WORKER_AVAILABLE") or ""

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
# AI SMS Agent auto-reply to inbound lead messages (Telnyx two-way conversations).
SMS_AI_AUTO_REPLY_ENABLED = _env_bool("SMS_AI_AUTO_REPLY_ENABLED", True)
# Loop / abuse guard: max automated replies per contact per rolling day.
SMS_AI_MAX_REPLIES_PER_CONTACT_PER_DAY = int(
    _env("SMS_AI_MAX_REPLIES_PER_CONTACT_PER_DAY", "20")
)
SMS_CERT_TEXT_VERSION_ONE_TO_ONE = "one_to_one_cert_v1"
SMS_CERT_TEXT_VERSION_CAMPAIGN = "campaign_cert_v1"

# --- Harness staging safety -------------------------------------------------
# HARNESS_MOCK_PROVIDERS: when true, the SMS provider factory returns the
# mock provider (sms_providers/mock.py), which records sends in memory
# and never touches the network. Defaults ON in staging and OFF elsewhere;
# an explicit env value always wins. Staging therefore cannot send a real
# SMS unless someone deliberately sets HARNESS_MOCK_PROVIDERS=false AND
# turns Telnyx trial mode off (which validate_staging_safety() refuses).
HARNESS_MOCK_PROVIDERS = _env_bool(
    "HARNESS_MOCK_PROVIDERS", APP_ENV == "staging"
)
SMS_IMPORT_UPLOAD_DIR = _env_strip("SMS_IMPORT_UPLOAD_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "private_uploads", "sms_imports"
)
SMS_IMPORT_MAX_BYTES = int(_env("SMS_IMPORT_MAX_BYTES", str(10 * 1024 * 1024)))

# Listing Generator persistence (60-day workspace + archive).
LISTING_GENERATION_RETENTION_DAYS = int(_env("LISTING_GENERATION_RETENTION_DAYS", "60"))

# Social Media Connections — OAuth token encryption + per-provider app credentials.
# SOCIAL_TOKEN_ENCRYPTION_KEY must be a urlsafe-base64 Fernet key (cryptography.fernet.Fernet.generate_key()).
SOCIAL_TOKEN_ENCRYPTION_KEY = _env_strip("SOCIAL_TOKEN_ENCRYPTION_KEY") or ""
# Tenant-owned integration credentials (currently SendGrid Marketing). A
# separate key is preferred; falling back preserves compatibility for
# installations that already provisioned the social-token Fernet key.
INTEGRATION_CREDENTIAL_ENCRYPTION_KEY = (
    _env_strip("INTEGRATION_CREDENTIAL_ENCRYPTION_KEY")
    or SOCIAL_TOKEN_ENCRYPTION_KEY
)
APP_BASE_URL = (_env_strip("APP_BASE_URL") or _env_strip("APP_URL") or "http://localhost:8080").rstrip("/")

LINKEDIN_CLIENT_ID = _env_strip("LINKEDIN_CLIENT_ID") or ""
LINKEDIN_CLIENT_SECRET = _env_strip("LINKEDIN_CLIENT_SECRET") or ""

# Shared Meta app credentials power both Facebook Page and Instagram professional-account posting.
FACEBOOK_APP_ID = _env_strip("FACEBOOK_APP_ID") or ""
FACEBOOK_APP_SECRET = _env_strip("FACEBOOK_APP_SECRET") or ""
# Flip to true only after Meta App Review + Business Verification are approved for
# pages_manage_posts / instagram_content_publish in production.
META_APP_REVIEW_APPROVED = _env_bool("META_APP_REVIEW_APPROVED", False)

TIKTOK_CLIENT_KEY = _env_strip("TIKTOK_CLIENT_KEY") or ""
TIKTOK_CLIENT_SECRET = _env_strip("TIKTOK_CLIENT_SECRET") or ""
# Flip to true only after TikTok's unaudited-app user cap is lifted via app audit.
TIKTOK_AUDIT_APPROVED = _env_bool("TIKTOK_AUDIT_APPROVED", False)

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


def validate_staging_safety():
    """Fail-closed staging guards. Returns a list of error strings.

    Staging must never be able to send a real SMS, send a real email,
    place a real call, or charge a real card by misconfiguration. These
    checks run at boot (see validate_config) and refuse to start when
    any fires. They run in every process that calls validate_config()
    (web, harness worker, SMS campaign worker — see workers/* main()).

    * a live Stripe secret in staging (real charges possible);
    * the production Telnyx toll-free number as the staging sender;
    * a real Telnyx API key with mock providers off AND trial mode off
      (unrestricted real sends to arbitrary numbers);
    * trial mode on but no verified test number configured (trial mode
      cannot restrict destinations without one);
    * real Twilio / SimpleTexting credentials for the *active* SMS
      provider with mock providers off (Telnyx trial mode is a
      Telnyx-only restriction; the other providers have no equivalent,
      so there is no safe non-mock config for them in staging);
    * a SendGrid key or SMTP configuration (real email is reachable
      from the staging UI — e.g. password reset via
      email_service.send_email — and there is no SendGrid test key, so
      staging email stays unset until an email mock exists);
    * a Vapi key (POST /voice/calls places a real outbound call
      outside the harness gate) or an OpenAI key (POST
      /api/ask-topai/live/session mints a real metered realtime
      session, also outside the harness gate).

    Keys for a provider that is NOT the active SMS provider are inert
    (the factory never constructs it) and do not trip the guard; keys
    for any provider are inert while HARNESS_MOCK_PROVIDERS is on, so
    a mock-on staging config passes with real keys set.
    """
    errors = []
    if not IS_STAGING:
        return errors
    stripe_key = (STRIPE_SECRET_KEY or "").strip()
    if stripe_key.startswith("sk_live_"):
        errors.append(
            "Staging must not use a live Stripe secret key "
            "(sk_live_…); leave Stripe unset in staging."
        )
    telnyx_number = (TELNYX_PHONE_NUMBER or "").strip()
    if telnyx_number and telnyx_number == PRODUCTION_TELNYX_NUMBER:
        errors.append(
            "Staging must not use the production Telnyx number "
            f"({PRODUCTION_TELNYX_NUMBER}); leave TELNYX_PHONE_NUMBER unset "
            "in staging (mock provider) or use a staging-only number."
        )
    telnyx_key = (TELNYX_API_KEY or "").strip()
    if telnyx_key and not HARNESS_MOCK_PROVIDERS and not TELNYX_TRIAL_MODE:
        errors.append(
            "Staging has a real Telnyx API key with mock providers off "
            "and trial mode off; that would allow real SMS to arbitrary "
            "numbers. Set HARNESS_MOCK_PROVIDERS=true or "
            "TELNYX_TRIAL_MODE=true."
        )
    if telnyx_key and not HARNESS_MOCK_PROVIDERS and TELNYX_TRIAL_MODE:
        if not (TELNYX_VERIFIED_TEST_NUMBER or "").strip():
            errors.append(
                "Staging Telnyx trial mode requires "
                "TELNYX_VERIFIED_TEST_NUMBER; without it trial mode "
                "cannot restrict destinations."
            )
    # Other SMS providers: Telnyx trial mode does not restrict them, so
    # a real credential for the active provider with mock off is an
    # unrestricted real-send path. Scoped to the active provider —
    # credentials for an unselected provider are inert.
    active_provider = (SMS_PROVIDER or "telnyx").lower().strip()
    if active_provider == "twilio" and not HARNESS_MOCK_PROVIDERS:
        twilio_sid = (TWILIO_ACCOUNT_SID or "").strip()
        twilio_token = (TWILIO_AUTH_TOKEN or "").strip()
        if twilio_sid.startswith("AC") and twilio_token:
            errors.append(
                "Staging must not use real Twilio credentials with mock "
                "providers off; that would allow real SMS to arbitrary "
                "numbers (Twilio has no trial-mode restriction in this "
                "codebase). Set HARNESS_MOCK_PROVIDERS=true or leave "
                "Twilio unset in staging."
            )
    if active_provider == "simpletexting" and not HARNESS_MOCK_PROVIDERS:
        if (SIMPLETEXTING_API_TOKEN or "").strip():
            errors.append(
                "Staging must not use a real SimpleTexting API token with "
                "mock providers off; that would allow real SMS to "
                "arbitrary numbers. Set HARNESS_MOCK_PROVIDERS=true or "
                "leave SimpleTexting unset in staging."
            )
    # Email: a SendGrid key or SMTP config makes real email reachable
    # from the staging UI (password reset, etc.) with no harness gate
    # in the way. There is no SendGrid test key, so refuse, don't warn.
    if (SENDGRID_API_KEY or "").strip():
        errors.append(
            "Staging must not set SENDGRID_API_KEY (SendGrid); it would allow real "
            "email from staging (e.g. password reset). Leave email unset "
            "in staging until an email mock exists."
        )
    if (SMTP_HOST or "").strip():
        errors.append(
            "Staging must not configure SMTP (SMTP_HOST is set); it would "
            "allow real email from staging. Leave email unset in staging "
            "until an email mock exists."
        )
    # Voice / realtime: both are reachable from the staging UI outside
    # the harness gate and spend real money (Vapi outbound calls,
    # OpenAI realtime sessions). Refuse until a mock exists for them.
    if (VOICE_PROVIDER_API_KEY or "").strip():
        errors.append(
            "Staging must not set VOICE_PROVIDER_API_KEY (Vapi); it would "
            "allow real outbound calls from staging. Leave voice unset "
            "in staging until a voice mock exists."
        )
    if (OPENAI_API_KEY or "").strip():
        errors.append(
            "Staging must not set OPENAI_API_KEY; it would allow real "
            "metered OpenAI realtime sessions from staging. Leave it "
            "unset in staging until a realtime mock exists."
        )
    return errors


# Processes that genuinely perform Stripe operations (checkout, webhooks,
# subscription gating). Only the web process is in this set: neither
# worker imports stripe_billing/stripe or touches Stripe at runtime, so
# requiring Stripe credentials in a worker only spreads a live secret to
# services that never use it (see 2026-11-09 production incident, where
# both workers crashed at boot for a missing STRIPE_SECRET_KEY they did
# not need). A process outside this set skips the Stripe requirements.
_STRIPE_REQUIRED_PROCESSES = {"web"}


def validate_config(process="web"):
    """Fail closed on a bad config for the given process.

    ``process`` names the calling process: "web" (default, the Flask
    app), "harness-worker", or "sms-campaign-worker". Stripe
    credentials are required only for processes that perform Stripe
    operations (currently only "web"); workers skip them. Every other
    check — Anthropic/Flask keys, database config, staging safety
    guards — applies to all processes unchanged. Callers must pass
    their real process name; the default is "web" so existing callers
    (app.py) keep the strict behavior.
    """
    missing = []
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if not FLASK_SECRET_KEY:
        missing.append("FLASK_SECRET_KEY")
    if IS_PRODUCTION and process in _STRIPE_REQUIRED_PROCESSES:
        if not STRIPE_SECRET_KEY:
            missing.append("STRIPE_SECRET_KEY")
        if not STRIPE_WEBHOOK_SECRET:
            missing.append("STRIPE_WEBHOOK_SECRET")
        if SUBSCRIPTION_REQUIRED and not STRIPE_PRICE_ID:
            missing.append("STRIPE_PRICE_ID")
    if missing:
        print(f"FATAL: Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    # Soft production guard: refuse obvious Stripe price placeholders without crashing tests.
    # Web-only: workers never perform Stripe operations, so a worker never
    # needs a valid Stripe price id either.
    if process in _STRIPE_REQUIRED_PROCESSES and IS_PRODUCTION and SUBSCRIPTION_REQUIRED and STRIPE_PRICE_ID:
        from stripe_billing import is_valid_stripe_price_id, stripe_mode_mismatch

        if not is_valid_stripe_price_id(STRIPE_PRICE_ID):
            print(
                "FATAL: STRIPE_PRICE_ID looks invalid (use a real price_… id from Stripe).",
                file=sys.stderr,
            )
            sys.exit(1)
        if stripe_mode_mismatch():
            print(
                "FATAL: Stripe secret/publishable key mode mismatch (test vs live).",
                file=sys.stderr,
            )
            sys.exit(1)

    validate_database_config()

    # Staging safety guards fail closed at boot (see
    # validate_staging_safety). They are part of validate_config so the
    # web process, the harness worker, and the migration runner path all
    # enforce them.
    staging_errors = validate_staging_safety()
    if staging_errors:
        for err in staging_errors:
            print(f"FATAL: {err}", file=sys.stderr)
        sys.exit(1)

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
    print(
        "Ask TopAI realtime check: "
        f"openai_api_key_present={bool(OPENAI_API_KEY)} "
        f"realtime_model={ASK_TOPAI_REALTIME_MODEL}",
        file=sys.stderr,
    )
