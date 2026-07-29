from functools import wraps
from urllib.parse import urlparse

from flask import jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import config
import db

MIN_PASSWORD_LENGTH = 8


def safe_next_url(candidate, default="/app"):
    """Allow only same-origin relative paths (block open redirects)."""
    if not isinstance(candidate, str):
        return default
    nxt = candidate.strip()
    if not nxt or not nxt.startswith("/") or nxt.startswith("//"):
        return default
    if any(ch in nxt for ch in ("\\", "\n", "\r", "\0")):
        return default
    parsed = urlparse(nxt)
    if parsed.scheme or parsed.netloc:
        return default
    if not parsed.path.startswith("/"):
        return default
    return nxt


def hash_password(password):
    return generate_password_hash(password)


def verify_password(password_hash, password):
    return check_password_hash(password_hash, password)


def login_user(user_id):
    session.clear()
    session["user_id"] = user_id
    user = db.get_user_by_id(user_id)
    session["session_version"] = int((user or {}).get("session_version") or 1)
    session.permanent = True


def logout_user():
    session.clear()


def bump_session_version(user_id):
    db.bump_user_session_version(user_id)


def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = db.get_user_by_id(user_id)
    if not user:
        logout_user()
        return None
    expected = int(user.get("session_version") or 1)
    actual = session.get("session_version")
    if actual is not None and int(actual) != expected:
        logout_user()
        return None
    # Backfill session_version for older sessions created before this field.
    if actual is None:
        session["session_version"] = expected
    return user


def email_has_free_access(email):
    return email and email.lower().strip() in config.FREE_ACCESS_EMAILS


def user_has_active_subscription(user):
    if not config.SUBSCRIPTION_REQUIRED:
        return True
    return user and (
        user.get("subscription_status") == "active"
        or email_has_free_access(user.get("email"))
    )


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = get_current_user()
        if not user:
            if request.path.startswith("/generate") or request.path.startswith("/api/"):
                return jsonify({"error": "Please log in to continue."}), 401
            nxt = request.path
            if request.query_string:
                nxt = f"{request.path}?{request.query_string.decode()}"
            return redirect(url_for("login", next=safe_next_url(nxt)))
        return view(*args, **kwargs)

    return wrapped


def subscription_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({"error": "Please log in to continue."}), 401
        if not user_has_active_subscription(user):
            return jsonify({"error": "An active subscription is required."}), 402
        return view(*args, **kwargs)

    return wrapped
