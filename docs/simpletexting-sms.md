# SimpleTexting SMS — ops & scale checklist

## Official API verdict (v2)

| Capability | Status |
|------------|--------|
| Explicit sender via `accountPhone` on `POST /v2/api/messages` | Supported |
| One master Bearer token; `GET /v2/api/phones` lists numbers | Supported |
| Inbound/delivery webhooks include `values.accountPhone` | Supported |
| Unsubscribe payload may omit destination number | Fallback required |
| Native ST campaigns / keywords on secondary numbers | Not used by TopAI |
| Public API to provision numbers | Not available — ops assigns manually |

Base URL: `https://api-app2.simpletexting.com/v2`  
Auth: `Authorization: Bearer <SIMPLETEXTING_API_TOKEN>`

## Architecture (production)

- Platform holds **one** SimpleTexting API token (never expose to tenants).
- Each TopAI tenant gets a **dedicated** sender number in `tenant_sms_senders`.
- Sends always pass that tenant’s `accountPhone`. Never fall back to a global number for an unconfigured tenant.
- Bulk campaigns are owned by TopAI (PostgreSQL jobs → per-recipient `POST /api/messages`). Do not use ST lists/campaigns as source of truth.
- Inbound: `accountPhone` → tenant → `contactPhone` → lead in that tenant only.

## Pilot vs scale (must confirm with SimpleTexting support before multi-brokerage)

1. **Secondary-number API volume** — product copy limits secondary numbers for native Campaigns; confirm campaign-scale `POST /api/messages` from secondary numbers is allowed.
2. **10DLC / multi-brand** — one master ST brand for unrelated brokerages is a TCR risk. Get written ISV/reseller guidance before assigning numbers to many brokerages.
3. Number provisioning remains manual (dashboard / support “Request more”).

Until those are confirmed, only assign a sender to the **pilot tenant**. Do not treat `SIMPLETEXTING_PHONE_NUMBER` as every tenant’s sender.

## Railway variables

| Variable | Role |
|----------|------|
| `SMS_PROVIDER=simpletexting` | Active provider |
| `SIMPLETEXTING_API_TOKEN` | Master Bearer token (secret) |
| `SIMPLETEXTING_WEBHOOK_SECRET` | Query token for webhook URLs |
| `SIMPLETEXTING_PHONE_NUMBER` | Dev/pilot fallback **only**; never implicit for unconfigured tenants |
| `APP_URL` | `https://topairealestatetools.com` |
| Twilio `TWILIO_*` | Retained for rollback / history |

## Webhook URLs (register in ST dashboard or via `POST /api/webhooks`)

```
https://topairealestatetools.com/webhooks/simpletexting/inbound?token=<SECRET>
https://topairealestatetools.com/webhooks/simpletexting/delivery?token=<SECRET>
https://topairealestatetools.com/webhooks/simpletexting/unsubscribe?token=<SECRET>
```

Triggers: `INCOMING_MESSAGE`, `DELIVERY_REPORT`, `NON_DELIVERED_REPORT`, `UNSUBSCRIBE_REPORT`.

## Railway services

- **web:** existing Gunicorn Procfile entry  
- **worker:** `python -m workers.sms_campaign_worker`

## Rollback

Set `SMS_PROVIDER=twilio` (with Twilio creds still present). Historical rows keep `provider` column.

## Signup & number assignment (ops)

1. Create/use the TopAI SimpleTexting master account; store `SIMPLETEXTING_API_TOKEN` only in Railway secrets.
2. In ST dashboard, request/add a dedicated phone number for the **pilot tenant only**.
3. Insert/enable mapping (ops SQL or admin helper):

```sql
INSERT INTO tenant_sms_senders
  (user_id, sms_provider, sender_number, sms_enabled, registration_status, created_at, updated_at, activated_at)
VALUES
  (<pilot_user_id>, 'simpletexting', '+1XXXXXXXXXX', 1, 'verified', now(), now(), now());
```

4. Never assign the same `sender_number` to two tenants. Never rely on `SIMPLETEXTING_PHONE_NUMBER` for production tenants.
5. Register the three webhook URLs with `?token=<SIMPLETEXTING_WEBHOOK_SECRET>`.
6. Deploy **web** and **worker** Railway services (`Procfile` has both).
7. Pilot verify: accept SMS terms → assign sender → one-to-one certified send → inbound to that number → STOP → campaign dry-run with ≤5 recipients.

## Verification checklist

- [ ] `SMS_PROVIDER=simpletexting` on web + worker
- [ ] Token + webhook secret set; Twilio vars retained for rollback
- [ ] Pilot tenant has one verified `tenant_sms_senders` row
- [ ] Unconfigured tenants cannot send (no silent global From)
- [ ] Compose/suggestion require certification checkbox
- [ ] Campaign certify → launch → worker claims jobs (`FOR UPDATE SKIP LOCKED` on Postgres)
- [ ] `/webhooks/simpletexting/*` reject missing token in production
- [ ] Rollback: set `SMS_PROVIDER=twilio`

## Consent language

Use **subscriber certification** only (`not_certified`, `user_certified`, …). Supporting consent records are agent-owned. Never imply TopAI independently verified consent.
