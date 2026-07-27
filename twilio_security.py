"""Twilio webhook signature validation. Auth token is never logged or returned."""

from functools import wraps

from flask import abort, request
from twilio.request_validator import RequestValidator

import config


def _candidate_request_urls():
    """
    Public URLs Twilio may have signed.
    Prefer APP_URL because Railway terminates TLS upstream and request.url may be http://
    while Twilio signed https://. Also try the raw request URL as a fallback.
    """
    candidates = []
    base = (config.APP_URL or "").rstrip("/")
    if base:
        url = f"{base}{request.path}"
        if request.query_string:
            url = f"{url}?{request.query_string.decode('utf-8', errors='ignore')}"
        candidates.append(url)
        # Messaging Service console sometimes uses trailing-slash variants.
        if not url.endswith("?"):
            if url.endswith("/"):
                candidates.append(url.rstrip("/"))
            else:
                candidates.append(url + "/")

    raw = request.url
    if raw and raw not in candidates:
        candidates.append(raw)
        if raw.startswith("http://"):
            candidates.append("https://" + raw[len("http://") :])

    # Dedupe while preserving order
    seen = set()
    ordered = []
    for url in candidates:
        if url and url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


def twilio_request_url():
    """Primary public URL used for signature validation."""
    urls = _candidate_request_urls()
    return urls[0] if urls else request.url


def validate_twilio_request(view):
    """Require a valid X-Twilio-Signature using TWILIO_AUTH_TOKEN only."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        auth_token = (config.TWILIO_AUTH_TOKEN or "").strip()
        if not auth_token:
            abort(403)

        signature = request.headers.get("X-Twilio-Signature", "")
        validator = RequestValidator(auth_token)
        form = request.form
        for url in _candidate_request_urls():
            if validator.validate(url, form, signature):
                return view(*args, **kwargs)
        abort(403)

    return wrapped
