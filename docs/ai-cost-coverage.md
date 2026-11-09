# AI cost coverage — universal recording + spend-cap enforcement

Branch: `launch-universal-costs`. Status: every billable AI/provider
execution path in the repo records its usage to the harness cost ledger
(`harness_costs`) and is gated by the per-tenant monthly spend cap —
except the explicitly listed exceptions below.

**All dollar figures are ESTIMATES, never billed charges.**
Token counts are exact; cents are derived from the pricing tables in
`harness/costs.py` (Anthropic per-token rates plus clearly-labelled
placeholder rates for Vapi / Telnyx / Twilio / SimpleTexting / OpenAI
Realtime / SendGrid). See `harness/costs.py` and
`docs/ai-cost-coverage.md` — every rate that is an assumption is marked
as one, with its env override. Confirm rates against each provider's
pricing page before relying on a dollar figure.

## The cap

- Default: **$50 / tenant / month** (`HARNESS_MONTHLY_SPEND_CAP_CENTS`,
  default `5000` cents) — confirmed by Ren 2026-11-08.
- Change it (deployment-wide; there is **no per-tenant override**
  column — `harness_settings` carries mode/paused/ai_enabled only):
  set `HARNESS_MONTHLY_SPEND_CAP_CENTS` in the Railway environment
  (e.g. `10000` for $100) and restart the web + worker services.
  A non-positive value disables the cap. An unset / blank /
  unparsable value falls back to the $50 default.
- Check the current cap and spend: `GET /api/harness/status` reports
  the tenant's month-to-date spend (see `harness/routes.py`).
- Gate behaviour when tripped: the operation does **not** run and
  nothing is sent. Direct web routes return HTTP 402 with the cap
  message; Ask TopAI returns an error result (`code="spend_cap_reached"`);
  the SMS coach path returns `error="spend_cap_reached"` and flags the
  lead for manual review; the harness SMS job BLOCKs (not retryable);
  campaign-worker jobs fail permanently; scheduled AI replies fail.
  The cap resets with the calendar month (UTC `YYYY-MM` period).
- Fail-open rule (deliberate, matches pre-existing `harness.policy`
  behaviour): if the ledger cannot be read, the gate allows the spend
  rather than taking the product down. A recording failure likewise
  never breaks the request that already succeeded — recording is
  always best-effort. The one exception is the SMS coach: its
  recording point is inside the coach call itself, so a recording
  failure there is swallowed the same way.
- The harness policy gate (`harness/policy.py`, spend-cap step) still
  enforces the cap for harness-gated sends/writes; this work adds the
  same gate to every path that previously bypassed `policy.decide`.

## Billable-path inventory

`Recorded?` / `Capped?` are the state **after** this work. `tool` is
the `harness_costs.tool` value; `unit_type` is the ledger unit.

| # | Path | Provider call (file:line) | Provider / model / price basis | Ledger `tool` / `unit_type` | Recorded? | Capped? |
|---|------|---------------------------|--------------------------------|-----------------------------|-----------|---------|
| 1 | Listing generation (`POST /generate`) | `app.py:2471` `client.messages.create`, model `config.CLAUDE_MODEL` (`claude-opus-4-6`) | Anthropic Opus, 1500/7500 ¢/1M tok (estimate) | `listing_generation` / `tokens` | Yes — `app.py` `_record_generation_cost` after success | Yes — cap checked before the call, 402 when tripped |
| 2 | Script generation (`POST /generate-script`) | `app.py:2741`, model `config.SCRIPT_CLAUDE_MODEL` (`claude-sonnet-5`) | Anthropic Sonnet, 300/1500 ¢/1M tok (estimate) | `script_generation` / `tokens` | Yes | Yes — 402 when tripped |
| 3 | SMS generation (`POST /sms/generate`) | `app.py:1739`, model `config.CLAUDE_MODEL` (`claude-opus-4-6`) | Anthropic Opus (estimate) | `sms_generation` / `tokens` | Yes | Yes — 402 when tripped |
| 4 | SMS coach — inbound analysis (direct + harness paths) | `sms_coach.py:45` `client.messages.create`, model `config.CLAUDE_MODEL` | Anthropic Opus (estimate) | `sms_coach` / `tokens` | Yes — recorded exactly once inside `sms_coach.analyze_inbound_reply` | Yes — `sms_inbound.analyze_inbound_and_coach` (`sms_inbound.py:414`) checks before the call; tripped → `error="spend_cap_reached"`, manual-review insight, no model call |
| 5 | Harness SMS auto-reply job | `harness/sms_handler.py` via path 4 (`sms_inbound.py:414`) | Anthropic Opus (estimate) | `sms_coach` / `tokens` (correlation id = job correlation id) | Yes — via path 4; the handler's old duplicate `_record_cost` (`sms_auto_reply` rows) was removed so the same call is not double-counted | Yes — via path 4, plus `harness.policy.decide` send cap |
| 6 | Ask TopAI tool-use loop | `ask_topai/claude.py:110` via `ask_topai/agent.py:188` `call_claude`, model `config.ASK_TOPAI_MODEL` (`claude-sonnet-5`) | Anthropic Sonnet (estimate) | `ask_topai` / `tokens` | Yes — every round recorded in `ask_topai/agent.py:complete` | Yes — checked before the loop and before every round; tripped → error result `spend_cap_reached`, no further rounds |
| 7 | Ask TopAI legacy parser (single-shot) | `ask_topai/parser.py:154`, model `config.CLAUDE_MODEL` | Anthropic Opus (estimate) | `ask_topai_legacy` / `tokens` | Yes — when `user_id` is passed | Yes — when `user_id` is passed; raises before the call. See exception E2 below |
| 8 | OpenAI Realtime voice session (Ask TopAI live) | `ask_topai/realtime/openai_client.py:270` `mint_ephemeral_secret` (model `gpt-realtime-2.1` default, `ask_topai/realtime/settings.py`) | OpenAI Realtime, placeholder 6 ¢/min (`HARNESS_OPENAI_REALTIME_PRICE_CENTS_PER_MINUTE`), recorded as a 1-minute minimum per mint | `realtime_session` / `sessions` | Yes — `ask_topai/realtime/service.py:start_session` | Yes — checked before minting; tripped → `{ok: False, code: "spend_cap_reached"}` |
| 9 | OpenAI Realtime diagnostics mint | `ask_topai/realtime/service.py:187` (inside `diagnostics`) | OpenAI Realtime, same placeholder rate | `realtime_session` / `sessions` | Yes | Yes — checked before minting |
| 10 | Vapi outbound AI call | `voice_provider.py:94` `start_outbound_call` → `POST https://api.vapi.ai/call` (called from `app.py:1261` `start_voice_call`) | Vapi, placeholder 10 ¢/min (`HARNESS_VAPI_PRICE_CENTS_PER_MINUTE`), recorded as a 1-minute minimum at start | `vapi_outbound_call` / `minutes` | Yes — `app.py` after successful create | Yes — checked before `start_outbound_call`, 402 when tripped |
| 11 | Vapi call duration true-up | `app.py:2144` `voice_webhook` ← `voice_provider.py:321` `normalize_voice_webhook` duration | Vapi, same placeholder rate, minutes beyond the first | `vapi_call_duration` / `minutes` | Yes — idempotent per provider call id (`record_cost_once`); only when the webhook reports a duration > 60s | N/A (post-call true-up, no new spend decision) |
| 12 | Outbound SMS — one-to-one (manual / suggestion / test-via-lead) | `sms_providers/telnyx.py:181` / `simpletexting.py:108` / `twilio_adapter.py:24` `send_message`, via `sms_outbound.py:471` `send_authorized_sms` | Telnyx/Twilio/SimpleTexting: Telnyx-reported `cost.amount` (USD) when supplied, else placeholder 1 ¢/segment Telnyx/Twilio, 2 ¢/segment SimpleTexting (`HARNESS_SMS_PRICE_CENTS_PER_<PROVIDER>_PER_UNIT`); mock sends 0 ¢ | `sms_send` / `segments` | Yes — `sms_outbound.py` after provider acceptance | Yes — checked before the provider call, 402 `spend_cap_reached` |
| 13 | Outbound SMS — AI auto-reply (direct path) | Same provider calls, via `sms_ai_agent.py:290` `process_inbound_ai` | Same as 12 | `sms_ai_reply` / `segments` | Yes — after provider acceptance | Yes — checked before the send (re-checked after the coach call, whose spend may have tripped the cap) |
| 14 | Outbound SMS — scheduled/deferred AI reply | Same provider calls, via `workers/sms_campaign_worker.py:175` `_send_scheduled_ai_reply` | Same as 12 | `sms_ai_reply` / `segments` | Yes | Yes — checked before the send; tripped → message marked failed (no retry loop) |
| 15 | Outbound SMS — bulk campaign | Same provider calls, via `workers/sms_campaign_worker.py:372` campaign job send | Same as 12 | `sms_campaign` / `segments` | Yes | Yes — checked before the send; tripped → job `failed_permanent` |
| 16 | Outbound SMS — test send (non-lead destination) | Same provider calls, via `app.py:1880` `sms_test_send` | Same as 12 | `sms_test_send` / `segments` | Yes | Yes — 402 when tripped |
| 17 | SendGrid lead email | `email_campaign_providers/sendgrid.py:215` `send_email`, via `lead_email_service.py:132` `send_lead_email` | SendGrid, placeholder 1 ¢/message (`HARNESS_SENDGRID_PRICE_CENTS_PER_MESSAGE`) | `lead_email` / `messages` | Yes — after successful send | Yes — checked before the send |
| 18 | Telnyx HELP compliance auto-reply | `sms_providers/telnyx.py:181` via `telnyx_webhooks.py:494` `_send_help_reply` | Telnyx, same basis as 12 | `sms_help_reply` / `segments` | Yes | **No — explicit exception E1** (carrier-compliance reply must go out; see below) |

## Exceptions (recorded or unrecorded, with reasons)

- **E1 — HELP reply is recorded but NOT capped (path 18).** A consumer
  who texts HELP must receive the program information; blocking that
  reply on spend would violate the Telnyx messaging program's
  compliance rules. The send is still recorded, so its cost counts
  toward the tenant's monthly total.
- **E2 — Legacy Ask TopAI parser without a tenant (path 7).** `call_llm`
  / `interpret_request` take an optional `user_id`; when it is `None`
  the spend cannot be attributed to a tenant and is neither recorded
  nor capped. This path has no in-repo callers (the live path is
  `ask_topai.agent.complete`, used by `ask_topai.service.interpret`
  and `harness.orchestrator.run`), so the gap is unreachable from the
  product today. Any future caller must pass `user_id`.
- **E3 — Ask TopAI health ping (`ask_topai/claude.py:174` `ping`).**
  A CLI-only deploy check (`python -m ask_topai.claude`) with no
  tenant context: at most 32 output tokens, no `user_id` to attribute
  to. Not recorded, not capped. Not reachable from the web app.
- **E4 — Transactional email (`email_service.py`, SendGrid `mail/send`
  or SMTP fallback).** Password-reset / system mail. No tenant scope
  on the call (the caller is `password_reset.py`, not a tenant action),
  not AI usage, and not part of the per-tenant AI cap. Not recorded.
  If tenant-scoped transactional mail is ever billed per tenant, it
  needs a `user_id` threaded through first.
- **E5 — Stripe (`stripe_billing.py`, `app.py` billing routes).**
  Billing infrastructure, not AI/provider usage. Stripe fees are not
  tenant AI spend and are outside the cap by design. Not recorded.
- **E6 — Vapi in-call tool calls (`voice_tools.py`
  `handle_vapi_tool_calls`).** CRM tool executions performed by the
  Vapi assistant during a call. They are not separate billable calls —
  the model usage is inside the Vapi call minutes recorded by paths
  10–11. Not separately recorded.
- **E7 — Social posting (`social_providers/`, `listing_publish.py`).**
  No usage-based provider billing in the repo for these calls (OAuth
  posting to Meta etc.). Not recorded.
- **E8 — OpenAI Realtime session duration.** The browser session's
  real audio/token usage is never reported back to the server, so a
  session is recorded as a 1-minute minimum at mint time. A long
  session costs more than its row. This is a known under-recording
  gap; fixing it needs OpenAI usage reporting (session `usage` events
  or the Realtime usage API) plumbed back to the server.
- **E9 — Vapi model cost inside a call.** The placeholder 10 ¢/min
  covers Vapi platform + telephony at list; the LLM running inside the
  call is billed by Vapi on top of that in reality, so Vapi rows are a
  conservative floor. Confirm against Vapi's pricing page and set
  `HARNESS_VAPI_PRICE_CENTS_PER_MINUTE` accordingly.

## Ledger schema

`harness_costs` (migration 029) gained two nullable columns in
migration 033 (`migrations/versions/033_harness_costs_model_correlation.py`,
additive, both engines, registered in `migrations/runner.py`):

- `model TEXT` — provider model id when one applies
  (`claude-sonnet-5`, `gpt-realtime-2.1`, ...).
- `correlation_id TEXT` — traces a row to the request/event that
  caused the spend (inbound SMS id, Ask TopAI request id / job
  correlation id, Vapi provider call id, ...).

`harness.costs.record_cost` writes the new columns when they exist and
falls back to the 029 column set when they do not, so code and schema
can roll out in either order. Rows written before 033 have both
columns NULL. `record_cost_once` provides idempotent writes keyed on
`(user_id, period, tool, correlation_id)` for redeliverable events
(Vapi duration true-up).

> RELEASE NOTE (topai-launch-readiness-release, 2026-11-09): this
> release contains no schema migrations, so 033 is excluded — the
> ledger has no `model`/`correlation_id` columns and `record_cost`
> runs on its 029-column fallback. Cost recording and cap enforcement
> are unaffected (they use provider/tool/units/cost_cents only), but
> per-model breakdown, per-event traceability, and true-up dedup are
> lost until 033 lands in a future migration-bearing release. See
> `tests/test_release_no_migrations.py`.

## Rate env vars (all optional; defaults are the documented estimates)

| Var | Default | Meaning |
|-----|---------|---------|
| `HARNESS_MONTHLY_SPEND_CAP_CENTS` | `5000` ($50) | Per-tenant monthly cap |
| `HARNESS_ANTHROPIC_PRICES_JSON` | — | JSON per-model token rates |
| `HARNESS_ANTHROPIC_PRICE_<MODEL>_INPUT_CENTS_PER_M` / `..._OUTPUT_CENTS_PER_M` | Sonnet 300/1500, Opus 1500/7500 | Per-model token rates |
| `HARNESS_VAPI_PRICE_CENTS_PER_MINUTE` | `10` | Vapi placeholder ¢/min |
| `HARNESS_OPENAI_REALTIME_PRICE_CENTS_PER_MINUTE` | `6` | Realtime placeholder ¢/min |
| `HARNESS_SENDGRID_PRICE_CENTS_PER_MESSAGE` | `1` | SendGrid placeholder ¢/message |
| `HARNESS_SMS_PRICE_CENTS_PER_TELNYX_PER_UNIT` | `1` | Telnyx placeholder ¢/segment |
| `HARNESS_SMS_PRICE_CENTS_PER_TWILIO_PER_UNIT` | `1` | Twilio placeholder ¢/message |
| `HARNESS_SMS_PRICE_CENTS_PER_SIMPLETEXTING_PER_UNIT` | `2` | SimpleTexting placeholder ¢/message |

## Tests

`tests/test_launch_universal_costs.py` — one record test and one
cap-block test per billable path (see the test file for the per-path
list), plus ledger/migration tests for the new columns and the
placeholder rates. All provider calls are mocked; no test places a
real call or uses a real provider key. Run with:

```
<venv>/bin/python -m pytest tests/test_launch_universal_costs.py tests/test_harness_costs.py -q
```
