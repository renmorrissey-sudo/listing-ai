# TopAI Real Estate Tools — Production Site Audit

- **Started:** 2026-07-31T00:06:16.576Z
- **Completed:** 2026-07-31T00:07:00.512Z
- **Base URL host:** PRODUCTION_ORIGIN
- **Login succeeded:** false
- **Tests:** passed=54 failed=0 skipped=9
- **Env presence:** {"TOPAI_AUDIT_BASE_URL":"PRESENT","TOPAI_AUDIT_EMAIL":"PRESENT","TOPAI_AUDIT_PASSWORD":"PRESENT"}

## Verdict

TopAI production audit found issues requiring attention.

## Routes tested

### Public
- `/`
- `/login`
- `/forgot-password`
- `/subscribe`
- `/terms`
- `/privacy`
- `/refund-policy`
- `/contact`
- `/sms-consent`
- `/pricing`
- `/features`
- `/how-it-works`
- `/static/sms-opt-in-proof.png`
- `/health`

### Authenticated
- _(none — login unavailable or skipped)_

## Critical findings (0)

_None_

## High findings (3)

### Authenticated login failed

- **Severity:** High
- **Route:** `/login`
- **Timestamp:** 2026-07-31T00:06:18.396Z
- **Viewport:** desktop-1440
- **Auth state:** logged-out
- **HTTP status:** 200
- **Expected:** User reaches /app or authenticated CRM/tools with Log out visible
- **Actual:** Final URL: https://PRODUCTION_ORIGIN/login; status=200; flash=Invalid email or password.; console=none
- **Screenshot:** audit-results/screenshots/High_desktop-1440__login.png
- **Suspected code:** app.py:/login, auth.py
- **Recommended fix:** Refresh TOPAI_AUDIT_EMAIL / TOPAI_AUDIT_PASSWORD in the automation environment to a valid active subscriber (do not commit secrets)
- **Regression test:** tests/site-audit/auth.spec.ts login flow
- **Reproduction:**
  1. Open /login
  1. Submit the configured audit subscriber credentials
  1. Observe final URL and page content

### Authenticated login failed

- **Severity:** High
- **Route:** `/login`
- **Timestamp:** 2026-07-31T00:06:46.272Z
- **Viewport:** tablet-768
- **Auth state:** logged-out
- **HTTP status:** 200
- **Expected:** User reaches /app or authenticated CRM/tools with Log out visible
- **Actual:** Final URL: https://PRODUCTION_ORIGIN/login; status=200; flash=Invalid email or password.; console=none
- **Screenshot:** audit-results/screenshots/High_tablet-768__login.png
- **Suspected code:** app.py:/login, auth.py
- **Recommended fix:** Refresh TOPAI_AUDIT_EMAIL / TOPAI_AUDIT_PASSWORD in the automation environment to a valid active subscriber (do not commit secrets)
- **Regression test:** tests/site-audit/auth.spec.ts login flow
- **Reproduction:**
  1. Open /login
  1. Submit the configured audit subscriber credentials
  1. Observe final URL and page content

### Authenticated login failed

- **Severity:** High
- **Route:** `/login`
- **Timestamp:** 2026-07-31T00:06:54.016Z
- **Viewport:** mobile-390
- **Auth state:** logged-out
- **HTTP status:** 200
- **Expected:** User reaches /app or authenticated CRM/tools with Log out visible
- **Actual:** Final URL: https://PRODUCTION_ORIGIN/login; status=200; flash=Invalid email or password.; console=none
- **Screenshot:** audit-results/screenshots/High_mobile-390__login.png
- **Suspected code:** app.py:/login, auth.py
- **Recommended fix:** Refresh TOPAI_AUDIT_EMAIL / TOPAI_AUDIT_PASSWORD in the automation environment to a valid active subscriber (do not commit secrets)
- **Regression test:** tests/site-audit/auth.spec.ts login flow
- **Reproduction:**
  1. Open /login
  1. Submit the configured audit subscriber credentials
  1. Observe final URL and page content

## Medium findings (0)

_None_

## Low findings (0)

_None_

## External dependencies / manual actions (2)

### Telnyx toll-free verification still pending

- **Severity:** External Dependency
- **Route:** `/health`
- **Timestamp:** 2026-07-31T00:06:22.833Z
- **Viewport:** desktop-1440
- **Auth state:** logged-out
- **HTTP status:** 200
- **Expected:** Vendor verification may be pending; site must handle safely
- **Actual:** toll_free_verification_status=pending; sms_sending_enabled=false
- **Screenshot:** n/a
- **Suspected code:** n/a
- **Recommended fix:** Complete Telnyx toll-free verification in vendor dashboard (not a code change)
- **Regression test:** Authenticated SMS UI must block send/launch while pending
- **Reproduction:**
  1. GET /health

### Audit subscriber credentials rejected by production

- **Severity:** Manual Action Required
- **Route:** `/login`
- **Timestamp:** 2026-07-31T00:06:18.396Z
- **Viewport:** all
- **Auth state:** logged-out
- **HTTP status:** 200
- **Expected:** Configured audit subscriber can sign in and reach /app
- **Actual:** Production returned Invalid email or password; authenticated CRM suites could not run
- **Screenshot:** n/a
- **Suspected code:** Cursor automation secrets / production user store
- **Recommended fix:** Create or reset a dedicated active-subscriber audit account and update TOPAI_AUDIT_* secrets (never commit them)
- **Regression test:** Authenticated site-audit project should pass login before CRM route coverage
- **Reproduction:**
  1. Confirm TOPAI_AUDIT_EMAIL and TOPAI_AUDIT_PASSWORD are present (presence only)
  1. POST /login with those credentials
  1. Observe flash: Invalid email or password

## Safety confirmations

- No production data was modified.
- No SMS, email, call, campaign, checkout, billing action, consent submission, password reset, or CSV import was triggered.

## Report paths

- `audit-results/site-audit.md`
- `audit-results/site-audit.json`
- `audit-results/screenshots/`

## Notes

- Playwright status: passed
- Authenticated suites skipped after production rejected audit credentials (flash: Invalid email or password). Public audit completed. No repair PR: credential configuration is Manual Action Required, not a code defect.
