# TopAI production site audit

Read-only Playwright audit against the production origin set in `TOPAI_AUDIT_BASE_URL`.

## Prerequisites

```bash
export TOPAI_AUDIT_BASE_URL='https://'"topai"'realestatetools.com'
export TOPAI_AUDIT_EMAIL='subscriber@example.com'
export TOPAI_AUDIT_PASSWORD='***'   # never log or commit

npm install
npx playwright install chromium
npm run audit
```

Confirm variables are present without printing values:

```bash
python3 - <<'PY'
import os
for k in ("TOPAI_AUDIT_BASE_URL","TOPAI_AUDIT_EMAIL","TOPAI_AUDIT_PASSWORD"):
    v=os.environ.get(k)
    print(f"{k}: {'PRESENT' if v else 'MISSING'}")
PY
```

## What it covers

- Public marketing, legal, health, and SMS consent routes from `topai-audit-routes.json`
- Discovered internal links from homepage / header / footer / pricing / mobile nav
- Authenticated CRM and tools routes after login
- Viewports: 390×844, 768×1024, 1440×900
- Error page / raw JSON / blank page / secret exposure checks
- Telnyx compliance UI (pending toll-free blocks send/launch; consent cannot override)
- Active-subscriber duplicate checkout guard on `/subscribe`

## Safety (mandatory)

Forms may be loaded and inspected. Do **not** submit mutating forms.

Never:

- send SMS / email / AI calls
- launch or create SMS campaigns
- submit SMS consent or lead inquiry
- import CSV
- create/update/delete leads
- request password-reset email
- create Stripe Checkout or mutate billing/subscription
- change account settings or production data

## Outputs

| Path | Purpose |
|------|---------|
| `audit-results/site-audit.json` | Machine-readable findings + run summary |
| `audit-results/site-audit.md` | Human-readable report |
| `audit-results/screenshots/` | Failure evidence |

## Severity

- **Critical** — secrets, auth bypass, cross-tenant access, unauthorized mutation, payment/SMS compliance bypass
- **High** — 500s on core routes, broken login/CRM/subscribe, duplicate subscription risk, raw exceptions
- **Medium** — non-core breakage, misleading nav/status, mobile blockers, obsolete Twilio customer copy
- **Low** — cosmetic / minor a11y / wording
- **External / Manual** — Telnyx verification pending, vendor/platform delays (verify product handles them safely)
