# AGENTS.md

TopAI Real Estate Tools — a Flask monolith (single web app, `app.py`) providing AI content/SMS/voice tools plus a CRM for real estate agents. Local development uses SQLite; production uses Railway PostgreSQL.

Standard setup, run, and test commands live in `SETUP.md` and `Procfile`. Read those first; the notes below only add non-obvious, cloud-specific context.

## Cursor Cloud specific instructions

- Python deps are installed into a virtualenv at `.venv` (the startup update script creates it and runs `pip install -r requirements.txt`). Activate it before running anything: `source .venv/bin/activate` (or call binaries directly, e.g. `.venv/bin/python app.py`). `.venv` is not tracked in git.
- `.env` is gitignored and is NOT recreated by the update script. For local dev, create it from `.env.example` with `APP_ENV=development`, leave `DATABASE_URL` unset (uses SQLite at `DATABASE_PATH`, default `real_estate.db`), and set `FLASK_SECRET_KEY` plus `ANTHROPIC_API_KEY`. A dummy `ANTHROPIC_API_KEY` is enough to boot and browse the whole app — the key is only validated when an AI generation endpoint is actually called (`/generate`, `/generate-script`, `/sms/generate`, inbound SMS coaching).
- Startup only hard-fails (`sys.exit(1)` from `config.validate_config()`) if `ANTHROPIC_API_KEY` or `FLASK_SECRET_KEY` is missing, or, when `APP_ENV=production`, if Stripe/`DATABASE_URL` requirements are unmet. Twilio/Telnyx/SimpleTexting/Vapi/SendGrid are all lazy and optional — missing keys only surface when you exercise those features.
- Run the dev server with `python app.py` (serves `http://localhost:8080`; note `app.run(debug=False)`, so there is no auto-reloader — restart the process after code changes). Do NOT use the `Procfile` gunicorn command for development. `db.init_db()` auto-applies pending migrations on startup, so `python -m migrations.runner` is usually unnecessary in dev but is the safe explicit command.
- Auth/paywall gotcha for testing tools: the public signup page `/subscribe` requires Stripe billing to be configured and returns HTTP 503 when it is not — so you cannot create an account through the UI without Stripe. In dev, create a user directly, e.g. `python -c "import db, auth; db.create_user('demo@topai.local', auth.hash_password('DemoPass123!'))"`, then log in at `/login`. When `STRIPE_SECRET_KEY` is unset in dev/test, `config` auto-sets `SUBSCRIPTION_REQUIRED=False`, so tool pages become reachable after login.
- Creating a lead without external providers: the SMS Assistant "Send"/"Generate" buttons need a provider/Anthropic, but the CRM "Add external lead" form (`/crm/external-leads/new`) creates a lead entirely offline. `POST /sms/messages` with `send_now=false` and `compliance_confirmed=true` also creates a CRM lead (saved as a draft) without a provider.
- Tests: `pytest` (uses a temp SQLite DB, `APP_ENV=test`, configured in `tests/conftest.py`). The suite is green on a clean `main` checkout: 1587 passed, 6 skipped, 0 failed (verified 2026-11-23 in the Phase III-4 fix round, base `6151fff8`).
- No linter/formatter is configured (no ruff/flake8/black/pylint/pyproject config).
- The bulk-SMS background worker (`python -m workers.sms_campaign_worker`) is optional and only needed to deliver queued bulk campaigns; the core web app runs fine without it.
