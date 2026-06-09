from functools import wraps

from flask import jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import config
import db

MIN_PASSWORD_LENGTH = 8


def hash_password(password):
    return generate_password_hash(password)


def verify_password(password_hash, password):
    return check_password_hash(password_hash, password)


def login_user(user_id):
    session.clear()
    session["user_id"] = user_id
    session.permanent = True


def logout_user():
    session.clear()


def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return db.get_user_by_id(user_id)


def user_has_active_subscription(user):
    if not config.SUBSCRIPTION_REQUIRED:
        return True
    return user and user.get("subscription_status") == "active"


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = get_current_user()
        if not user:
            if request.path.startswith("/generate"):
                return jsonify({"error": "Please log in to continue."}), 401
            return redirect(url_for("login", next=request.path))
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
