"""Secure password reset / first-time password setup tokens."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone

import config
import db
from email_service import email_configured, send_password_reset_email

logger = logging.getLogger(__name__)

RESET_TOKEN_TTL_MINUTES = 45
NEUTRAL_FORGOT_MESSAGE = (
    "If an account exists for that email address, we sent password-reset instructions."
)
MIN_PASSWORD_LENGTH = 8

# Simple process-local rate limit by normalized email (complements IP limiter).
_EMAIL_RESET_HITS: dict[str, list[float]] = {}
_EMAIL_RESET_MAX = 3
_EMAIL_RESET_WINDOW_SEC = 3600


def _email_rate_limited(email: str) -> bool:
    import time

    now = time.time()
    hits = [t for t in _EMAIL_RESET_HITS.get(email, []) if now - t < _EMAIL_RESET_WINDOW_SEC]
    if len(hits) >= _EMAIL_RESET_MAX:
        _EMAIL_RESET_HITS[email] = hits
        return True
    hits.append(now)
    _EMAIL_RESET_HITS[email] = hits
    return False


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _hash_token(raw_token: str) -> str:
    # HMAC with app secret so DB leaks alone are insufficient without the secret.
    secret = (config.FLASK_SECRET_KEY or "dev").encode("utf-8")
    return hmac.new(secret, raw_token.encode("utf-8"), hashlib.sha256).hexdigest()


def public_app_url() -> str:
    """Canonical public site URL for emails (never localhost/Railway internals in production)."""
    raw = (config.APP_URL or "").strip().rstrip("/")
    if config.IS_PRODUCTION:
        return "https://topairealestatetools.com"
    if raw and "localhost" not in raw and "railway" not in raw.lower():
        return raw
    return raw or "http://localhost:8080"


def validate_new_password(password: str, confirm: str) -> str | None:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if password != confirm:
        return "Passwords do not match."
    if password.isdigit() or password.isalpha():
        return "Choose a stronger password with a mix of letters and numbers."
    return None


def email_eligible_for_reset(email: str) -> bool:
    """True if we should send a reset (user exists, free access, or Stripe entitlement)."""
    email = normalize_email(email)
    if not email or "@" not in email:
        return False
    user = db.get_user_by_email(email)
    if user:
        return True
    from auth import email_has_free_access

    if email_has_free_access(email):
        return True
    if not config.STRIPE_SECRET_KEY:
        return False
    try:
        from stripe_billing import stripe_has_active_subscription

        return bool(stripe_has_active_subscription(email))
    except Exception:
        logger.exception("Stripe eligibility check failed during password reset")
        return False


def request_password_reset(email: str) -> str:
    """
    Always returns the neutral message. Sends email only when eligible and mail works.
    """
    email = normalize_email(email)
    if not email or "@" not in email:
        return NEUTRAL_FORGOT_MESSAGE

    if not email_eligible_for_reset(email):
        return NEUTRAL_FORGOT_MESSAGE

    if _email_rate_limited(email):
        return NEUTRAL_FORGOT_MESSAGE

    if not email_configured():
        logger.error("Password reset requested but email delivery is not configured")
        return NEUTRAL_FORGOT_MESSAGE

    user = db.get_user_by_email(email)
    user_id = user["id"] if user else None
    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw_token)
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(minutes=RESET_TOKEN_TTL_MINUTES)).isoformat()

    db.invalidate_password_reset_tokens_for_email(email)
    db.create_password_reset_token(
        email=email,
        token_hash=token_hash,
        user_id=user_id,
        expires_at=expires_at,
        created_at=now.isoformat(),
    )

    reset_url = f"{public_app_url()}/reset-password?token={raw_token}"
    sent = send_password_reset_email(
        to_email=email,
        reset_url=reset_url,
        expires_minutes=RESET_TOKEN_TTL_MINUTES,
    )
    if not sent:
        logger.error("Password reset email failed to send")
    return NEUTRAL_FORGOT_MESSAGE


def peek_reset_token(raw_token: str) -> dict | None:
    if not raw_token or len(raw_token) > 200:
        return None
    row = db.get_password_reset_token_by_hash(_hash_token(raw_token))
    if not row:
        return None
    if row.get("used_at"):
        return None
    try:
        expires = datetime.fromisoformat(row["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
    except Exception:
        return None
    if expires < datetime.now(timezone.utc):
        return None
    return row


def consume_reset_token(raw_token: str, new_password: str) -> tuple[int | None, str | None]:
    """
    Validate token, set/create password on existing or new user, invalidate sessions.
    Returns (user_id, error_message).
    """
    row = peek_reset_token(raw_token)
    if not row:
        return None, "This password reset link is invalid or has expired."

    email = normalize_email(row["email"])
    password_hash = None
    from auth import hash_password, bump_session_version

    password_hash = hash_password(new_password)
    user = db.get_user_by_email(email)
    created_new = False
    if not user:
        # Stripe/free-access entitled email without app user yet.
        if not email_eligible_for_reset(email):
            return None, "This password reset link is invalid or has expired."
        user_id = db.create_user(email, password_hash, password_set=True)
        created_new = True
        user = db.get_user_by_id(user_id)
        if config.STRIPE_SECRET_KEY:
            try:
                from stripe_billing import sync_user_from_stripe

                sync_user_from_stripe(user, email)
                user = db.get_user_by_id(user_id)
            except Exception:
                logger.exception("Stripe sync after password setup failed")
    else:
        user_id = user["id"]
        db.update_user_password(user_id, password_hash, password_set=True)
        bump_session_version(user_id)

    db.mark_password_reset_token_used(row["id"])
    db.invalidate_password_reset_tokens_for_email(email)
    logger.info(
        "Password reset completed user_id=%s created_new=%s",
        user_id,
        created_new,
    )
    return user_id, None
