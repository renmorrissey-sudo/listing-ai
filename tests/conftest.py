import os
import tempfile
import uuid

import pytest

# Must set before importing app/config/db.
_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["APP_ENV"] = "test"
os.environ["ENV"] = "test"
os.environ["DATABASE_PATH"] = _db_path
os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key-not-real")
os.environ.setdefault("FLASK_SECRET_KEY", "test-flask-secret")
os.environ["SUBSCRIPTION_REQUIRED"] = "false"
os.environ["ALLOW_DESTRUCTIVE_DB_RESET"] = "false"
os.environ["ALLOW_SQLITE_TABLE_REBUILD"] = "false"
os.environ["RUN_DEMO_SEED_ON_STARTUP"] = "false"


@pytest.fixture(scope="session", autouse=True)
def _cleanup_db():
    yield
    try:
        os.remove(_db_path)
    except OSError:
        pass


@pytest.fixture
def app_client():
    import db
    from app import app, limiter

    db.init_db()
    app.config["TESTING"] = True
    # Avoid cross-test IP rate-limit interference on auth endpoints.
    limiter.enabled = False
    with app.test_client() as client:
        yield client


@pytest.fixture
def two_users():
    import auth
    import db

    db.init_db()
    suffix = uuid.uuid4().hex[:10]
    u1 = db.create_user(f"agent1-{suffix}@example.com", auth.hash_password("password123"))
    u2 = db.create_user(f"agent2-{suffix}@example.com", auth.hash_password("password123"))
    return u1, u2
