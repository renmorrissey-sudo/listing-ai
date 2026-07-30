# TopAI / listing-ai agent notes

## Production site audit

Read-only production audits use the Playwright framework under `tests/site-audit/`.

```bash
# Required environment variables (never print password values)
export TOPAI_AUDIT_BASE_URL=https://topairealestatetools.com
export TOPAI_AUDIT_EMAIL='audit-account@example.com'
export TOPAI_AUDIT_PASSWORD='***'

npm install
npx playwright install chromium
npm run audit
```

Outputs:

- `audit-results/site-audit.json`
- `audit-results/site-audit.md`
- `audit-results/screenshots/`

See `tests/site-audit/README.md` for route coverage, safety rules, and severity guidance.

## Safety

Production audits must not mutate data: no SMS, email, calls, campaigns, checkout, CSV import, lead changes, consent submissions, or password-reset requests.
