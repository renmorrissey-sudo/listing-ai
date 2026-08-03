"""Server-side public registration / new-subscription gate.

Controlled only by environment variables (never request parameters):
  REGISTRATION_ENABLED  — open only when exactly true/1/yes/on (missing = closed)
  REGISTRATION_ALLOWLIST — optional comma-separated emails permitted while closed
"""

from __future__ import annotations

from flask import jsonify, redirect, render_template, request, url_for

import config


REGISTRATION_CLOSED_ERROR = "registration_closed"
REGISTRATION_CLOSED_MESSAGE = "New registrations are temporarily unavailable."
PRIVATE_BETA_TITLE = "TopAI is currently in private beta"
PRIVATE_BETA_BODY = (
    "We are completing production testing before opening new customer registrations. "
    "Existing customers can continue to sign in and use their accounts."
)
PRIVATE_BETA_SUPPORTING = (
    "TopAI Real Estate Tools is currently in private beta while we finish production "
    "testing. New registrations will reopen soon."
)


class RegistrationClosedError(PermissionError):
    """Raised when account creation or new Checkout is blocked."""

    def __init__(self, message: str = REGISTRATION_CLOSED_MESSAGE):
        super().__init__(message)
        self.error = REGISTRATION_CLOSED_ERROR
        self.message = message


def registration_is_open() -> bool:
    """True only when REGISTRATION_ENABLED normalizes to an explicit open value."""
    return bool(getattr(config, "REGISTRATION_ENABLED", False))


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def email_is_allowlisted(email: str | None) -> bool:
    """Case-insensitive allowlist check. Never expose the list to clients."""
    normalized = normalize_email(email)
    if not normalized:
        return False
    allowlist = getattr(config, "REGISTRATION_ALLOWLIST", None) or set()
    return normalized in allowlist


def registration_allowed_for_email(email: str | None) -> bool:
    if registration_is_open():
        return True
    return email_is_allowlisted(email)


def registration_allowed_for_user(user) -> bool:
    if registration_is_open():
        return True
    if not user:
        return False
    return email_is_allowlisted(user.get("email"))


def assert_registration_allowed(email: str | None) -> None:
    """Raise before any DB user create or Stripe Checkout/customer mutation."""
    if not registration_allowed_for_email(email):
        raise RegistrationClosedError()


def wants_json_response() -> bool:
    accept = (request.headers.get("Accept") or "").lower()
    if "application/json" in accept and "text/html" not in accept:
        return True
    if request.is_json:
        return True
    path = request.path or ""
    if path.startswith("/api/"):
        return True
    return False


def closed_payload() -> dict:
    return {
        "error": REGISTRATION_CLOSED_ERROR,
        "message": REGISTRATION_CLOSED_MESSAGE,
    }


def registration_closed_response(*, status: int = 403):
    """Structured 403 for APIs; friendly private-beta HTML otherwise."""
    if wants_json_response():
        return jsonify(closed_payload()), status
    return render_template("private_beta.html"), status


def registration_closed_get_response():
    """Public HTML GET while closed: friendly page, not a generic error."""
    if wants_json_response():
        return jsonify(closed_payload()), 403
    return redirect(url_for("private_beta"), code=302)
