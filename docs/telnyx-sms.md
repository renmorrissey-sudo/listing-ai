# Telnyx Messaging API V2 — ops & trial checklist

## Official API (confirmed)

| Item | Value |
|------|--------|
| Send | `POST https://api.telnyx.com/v2/messages` |
| Auth | `Authorization: Bearer <TELNYX_API_KEY>` |
| Body | `from`, `to`, `text` (E.164); optional `messaging_profile_id`, `webhook_url` |
| Inbound event | `message.received` |
| Outbound progress | `message.sent` |
| Terminal delivery | `message.finalized` |
| Signature headers | `telnyx-signature-ed25519`, `telnyx-timestamp` |
| Signed payload | `{timestamp}\|{raw_json_body}` Ed25519 |

Webhook (single Messaging Profile URL):

```
https://topairealestatetools.com/webhooks/telnyx/messaging
```

API version on Messaging Profile: **API V2**.

## Railway variables

| Variable | Role |
|----------|------|
| `SMS_PROVIDER=telnyx` | Active provider |
| `TELNYX_API_KEY` | Secret |
| `TELNYX_MESSAGING_PROFILE_ID` | Profile for webhooks / optional send |
| `TELNYX_PHONE_NUMBER` | Toll-free messaging / From number (E.164). Production: `+18888210810` |
| `SMS_SUPPORT_DISPLAY` | Public SMS support display (default `(888) 821-0810`) |
| `SMS_SUPPORT_E164` | Public SMS support E.164 (default `+18888210810`) |
| `TELNYX_PUBLIC_KEY` | Ed25519 webhook verify |
| `TELNYX_TRIAL_MODE=true` | Restrict destinations |
| `TELNYX_VERIFIED_TEST_NUMBER` | Only allowed destination in trial |
| `TELNYX_TOLL_FREE_VERIFICATION_STATUS` | `pending` \| `verified` \| `unknown` (only `verified` enables outbound Telnyx SMS) |
| `APP_URL` | `https://topairealestatetools.com` |
| Twilio / SimpleTexting vars | Retained inactive for rollback |

Set `TELNYX_TOLL_FREE_VERIFICATION_STATUS=verified` on **both** the `web` and `worker` services (or as a shared Variable with no service-level override of `pending`). Changing it requires a **redeploy/restart** of both services — the value is read from the process environment at runtime and is not stored in the database or tenant SMS settings.

## Railway services

- **web:** `python -m migrations.runner && gunicorn app:app ...`
- **worker:** `python -m workers.sms_campaign_worker`

## SMS program support number

| Format | Value |
|--------|--------|
| Display | `(888) 821-0810` |
| E.164 | `+18888210810` |

Used on `/sms-consent`, Privacy Policy, Terms, opt-in confirmation copy, and the Telnyx HELP auto-reply:

> TopAI RE Tools: For SMS help, contact us at (888) 821-0810 or reply to this number. Message frequency varies. Message and data rates may apply. Reply STOP to opt out.

Public opt-in workflow screenshot: `https://topairealestatetools.com/static/sms-opt-in-proof.png`

## Trial test sequence

1. Set vars above; deploy web + worker. Confirm Railway `TELNYX_PHONE_NUMBER=+18888210810`.
2. Messaging Profile → webhook URL above (API V2).
3. Accept SMS terms at `/crm/sms-diagnostics`.
4. AI SMS: lead phone = verified test number → certify checkbox → Send.
5. Reply from that phone → Needs Attention + draft (no auto-send).
6. Text STOP → opted_out + suppression.
7. Inspect delivery on message row / diagnostics.

## Rollback

`SMS_PROVIDER=twilio` or `SMS_PROVIDER=simpletexting` (with matching credentials). Historical rows keep their `provider` column.

## Remaining before production bulk / multi-tenant

1. Set `TELNYX_TRIAL_MODE=false` after account upgrade.
2. Assign per-tenant `tenant_sms_senders` (number + messaging_profile_id); do not share one number across brokerages.
3. Confirm Telnyx **ISV/reseller** 10DLC brand/campaign APIs before automated onboarding.
4. Confirm long-code / campaign throughput limits before high-volume workers.
5. Optional: segment/cost UI polish; registration state machine UI.

## Scale blockers (need Telnyx confirmation)

- ISV/reseller registration API access and required fields
- Trial daily message caps / verified destination rules
- Whether one Messaging Profile per tenant vs shared profile + per-number routing is preferred
