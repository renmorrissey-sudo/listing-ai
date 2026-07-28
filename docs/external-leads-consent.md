# External lead ingestion + SMS consent

Provider-neutral external lead ingestion (manual form, CSV, authenticated webhook) with mandatory unverified + blocked SMS defaults, consent evidence, and centralized `can_send_sms` enforcement.

## Defaults (non-negotiable)

Every externally ingested lead is created with:

- `sms_consent_status = unverified`
- `sms_sending_blocked = true`

Consent is **never** inferred from:

- source / provider name
- CSV `consent=true` (or similar)
- claiming or assigning a pond lead
- a generic compliance checkbox alone on an external lead

CSV/webhook “consent” claims create a **pending** `sms_consent_evidence` row only. An agent must run **Confirm qualifying consent** before SMS is enabled.

## Architecture

1. Configure a source under `/crm/external-sources` (`consent_behavior` is always `unverified_blocked`).
2. Ingest via `/crm/external-leads/new`, `/crm/external-leads/import`, or `POST /api/external-leads/webhook/<provider_key>`.
3. Lead detail shows Consent panel (Unverified + Blocked).
4. Agent adds evidence and confirms on `/crm/leads/<id>/consent`.
5. All outbound SMS paths call `sms_authorization.can_send_sms` (or internal attestation only for non-external leads).

## CSV columns

Recognized headers (aliases supported): `first_name`, `last_name`, `full_name` / `name`, `phone` / `phone_number`, `email`, `external_record_id`, `property_address`, `property_url`, `inquiry_notes` / `notes`, `lead_type`, `original_consent_status` / `consent`, `original_consent_date`, `original_consent_text`.

Workflow: Preview → Commit. Invalid phones are counted as invalid rows.

## Webhook

```
POST /api/external-leads/webhook/<provider_key>
Header: X-TopAI-Webhook-Secret: <secret>
Content-Type: application/json
```

- Secret is hashed per source (`webhook_secret_hash`); compared with constant-time digest.
- Tenant is resolved by matching `provider_key` + secret (not by session).
- Idempotent on `external_record_id` (or `id` / `lead_id` in payload) within the source.
- Rate limited. Logs avoid dumping secrets/PII payloads.

Example body:

```json
{
  "name": "Jordan Lee",
  "phone": "+15551234567",
  "email": "jordan@example.com",
  "external_record_id": "portal-123",
  "consent_status": "yes",
  "property_address": "123 Main St"
}
```

## Consent states

| Status | Meaning |
|--------|---------|
| `unverified` | Default for external; SMS blocked |
| `verified` | Evidence confirmed; SMS may send if not blocked |
| `opted_out` | STOP / recorded opt-out; always blocked |
| `revoked` | Consent revoked; blocked |
| `not_permitted` | Agent marked not permitted; blocked |

`sms_sending_blocked` must be false **and** status `verified` for `can_send_sms` to allow.

Legacy `consent_status` / `opt_out_status` stay in sync from the new writers.

## Evidence rules

Confirm requires:

- Attestation checkbox (exact confirmation statement)
- Consent method, date/time, agent + brokerage names
- Phone matching the lead (E.164)
- Evidence type
- For `external_platform`: platform name, how disclosure authorized the sender, and URL/text/upload
- For `verbal`: context, affirmative response (recommended script available)

Uploads go to `CONSENT_UPLOAD_DIR` (private). Download only via authenticated `/crm/consent-uploads/<ref>`.

## Ponds

Same-account fields: `pond_status` (`unassigned` | `claimable` | `claimed` | `assigned`), `claimed_at`, `claimed_by_user_id`.

**Claiming does not change consent.** UI copy states this explicitly.

## Adapters

`external_leads/adapters.py` is a stub registry for future provider-specific normalizers (Zillow, etc.). Adapters must never auto-verify consent.

## Authorization

```python
from sms_authorization import can_send_sms
ok, message = can_send_sms(user_id, lead_id)
```

Wired on: suggestion Approve & Send, AI SMS compose send, SMS test send (when destination is a CRM lead phone).

Internal (non-external) AI SMS sends may record a send-time attestation; external leads cannot use that path.

## Needs Attention

Ingest upserts `consent_review_required` once per lead. Confirm clears it.

## Railway / env

| Variable | Purpose |
|----------|---------|
| `CONSENT_UPLOAD_DIR` | Private evidence file directory |
| `CONSENT_UPLOAD_MAX_BYTES` | Max upload size (default 5MB) |

Webhook secrets are stored hashed on each `external_lead_sources` row — not as a single global env secret.
