"""Twilio webhook signature validation. Auth token is never logged or returned."""

from functools import wraps

from flask import abort, request
from twilio.request_validator import RequestValidator

import config


def twilio_request_url():
    """
    Public URL Twilio signed. Prefer APP_URL because Railway terminates TLS
    upstream and request.url may be http:// while Twilio signed https://.
    """
    base = (config.APP_URL or "").rstrip("/")
    if not base:
        return request.url
    url = f"{base}{request.path}"
    if request.query_string:
        url = f"{url}?{request.query_string.decode('utf-8', errors='ignore')}"
    return url


def validate_twilio_request(view):
    """Require a valid X-Twilio-Signature using TWILIO_AUTH_TOKEN only."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        auth_token = (config.TWILIO_AUTH_TOKEN or "").strip()
        if not auth_token:
            abort(403)

        signature = request.headers.get("X-Twilio-Signature", "")
        validator = RequestValidator(auth_token)
        if not validator.validate(twilio_request_url(), request.form, signature):
            abort(403)
        return view(*args, **kwargs)

    return wrapped
