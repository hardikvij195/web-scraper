# Lead Finder Cloud — web-scraper v2 design

Date: 2026-08-21
Status: approved in chat (session 2026-08-21), pending spec review

## Summary

Turn the web-scraper from a single-user local tool + read-only cloud viewer into a small
multi-user SaaS:

- Cloud (Vercel FastAPI + Supabase) owns: auth, member management, job queue, lead storage,
  quota/credits, payments (Razorpay + PayU), webhook dispatch, AI-key storage, dashboard UI.
- Member's PC owns: the actual scraping. The existing Playwright/httpx pipeline gains an
  **agent mode** that polls the cloud for that member's queued jobs, runs them locally, and
  syncs results up.

The cloud site (`web-scraper-leads.vercel.app`) regains a Jobs tab for everyone: members
create and watch jobs in the browser; execution happens on their own machine.

## Decisions (locked)

| Question | Decision |
|---|---|
| Job runner | Member runs local scraper (agent mode). No central VPS worker for now. |
| Billing model | One-time credit packs, not recurring subscriptions. |
| Verified lead | Enrichment finished AND (phone OR email present). Only verified leads debit quota + fire webhook. |
| AI keys | Member's own Gemini + OpenAI keys power keyword suggest **and** per-lead AI summaries. |
| Auth | Supabase Auth (email+password). Admin creates members manually; no self-signup. |
| Isolation | Per-member private leads/jobs (`user_id` + RLS). Admin sees all. |
| Webhook | Fired from cloud, per lead, at sync time. Delivery log + retries + cron re-drive. |
| Currency | Fixed INR amounts; USD is marketing copy. Starter 3,000 leads = $10 / ₹880. Pro 5,000 leads = $15 / ₹1,320. |

## Architecture

```
Member browser ──login/jobs/leads/billing──> Vercel FastAPI ──REST──> Supabase (auth, tables, RLS)
                                              │        ▲
Member PC: `python -m webscraper agent` ──────┘        │ service role (server-only)
  polls /api/agent/jobs, claims, scrapes locally,      │
  POSTs batches to /api/agent/sync ────────────────────┘
Cloud on sync: verify rule → debit credits → insert leads → POST webhook per lead
Payments: Razorpay checkout.js / PayU form redirect → server verify → credits_ledger
```

### Components

1. **Supabase project** — the one already wired in `vercel-app` env (`SUPABASE_PROJECT_URL`).
   Auth + Postgres + RLS. Service-role key lives only in Vercel env vars, never in the client.
2. **Vercel FastAPI** (`vercel-app/api/`) — split from single `index.py` into modules (below).
   Verifies Supabase JWT on every authenticated route.
3. **Cloud UI** (`vercel-app/index.html`) — stays vanilla JS single page. Gains login screen
   and tabs: Jobs / Leads / Settings / Billing / Admin (admin-only).
4. **Local agent** (`webscraper/agent.py` + CLI command) — poll loop around the existing
   scrape/enrich pipeline. Auth via long-lived agent token.

## Data model (new/changed tables)

All tables RLS-enabled. "Owner-or-admin" = `user_id = auth.uid() OR profile.role = 'admin'`.

- `profiles` — `user_id uuid PK → auth.users`, `role text check in ('admin','member')`,
  `name text`, `active bool default true`, `created_at`. Owner-read; admin full.
- `user_settings` — `user_id uuid PK`, `webhook_url text`, `webhook_secret text`,
  `gemini_key text`, `openai_key text`, `updated_at`. Owner-only RLS; API never returns full
  keys (masked to last 4 chars). Keys used server-side only.
- `agent_tokens` — `id`, `user_id`, `token_hash text` (SHA-256 of random 32-byte token),
  `label`, `last_seen_at`, `revoked bool`. Owner-or-admin. Plain token shown once at creation.
- `scrape_jobs` — `id`, `user_id`, `query`, `location`, `lat/lng/radius_km`, `limit_places`,
  `status text` (`queued|claimed|running|paused_quota|done|error|cancelled`), `phase`,
  `progress jsonb` (counts mirrored from agent), `claimed_by` (agent_tokens.id),
  `error text`, timestamps. Owner-or-admin.
- `web_scraper_leads` — existing table + `user_id uuid`, `cloud_job_id bigint`,
  `verified bool`, `ai_summary text`, `webhook_status text`. PK becomes
  `(user_id, place_key)` so two members can each own the same place. Existing rows migrate to
  the admin user. Owner-or-admin RLS replaces service-role-only policy.
- `orders` — `id`, `user_id`, `pack text` (`starter_3k|pro_5k`), `leads int`, `amount_inr int`
  (paise), `gateway text` (`razorpay|payu`), `gateway_order_id text unique`, `status`
  (`created|paid|failed`), `created_at`, `paid_at`, `raw jsonb`. Owner-or-admin.
- `credits_ledger` — `id`, `user_id`, `delta int` (+credit/−debit), `reason text`
  (`purchase|debit|admin_adjust`), `order_id`, `job_id`, `created_at`. Balance = `sum(delta)`.
  Debits happen inside a `SECURITY DEFINER` RPC `debit_credits(user_id, n, job_id)` that locks
  and refuses to go below zero (race-safe). Owner-read; writes service-role only.
- `webhook_deliveries` — `id`, `user_id`, `lead_place_key`, `url`, `status`
  (`pending|ok|failed`), `attempts int`, `last_error`, `next_retry_at`, timestamps.
  Owner-or-admin read.

Migrations live in `supabase_migrations/*.sql` (replacing the single `supabase_setup.sql`),
applied via Supabase SQL editor or psql; each file idempotent.

## Flows

### Login & member management
- Login page (supabase-js) → session JWT attached to all `/api/*` calls; FastAPI validates
  against Supabase JWKS/`auth.getUser`.
- `POST /api/admin/members` (admin JWT required) → service-role
  `auth.admin.createUser({email, password, email_confirm: true})` + `profiles` row. Admin can
  deactivate (blocks login via `active=false` check in API) and `admin_adjust` credits.
- Members can change their own password (supabase-js `updateUser`).

### Job lifecycle
1. Member creates job in browser → `scrape_jobs` status `queued`.
2. Local agent polls `GET /api/agent/jobs` (header `X-Agent-Token`) → oldest own `queued` job;
   claim via `POST /api/agent/jobs/{id}/claim` (sets `claimed`, `claimed_by`).
3. Agent runs existing pipeline (scrape → enrich) locally, streaming progress via
   `POST /api/agent/jobs/{id}/progress` (throttled, updates `progress`/`phase`).
4. Agent syncs finished leads in batches ≤200: `POST /api/agent/sync`.
5. Cloud per batch: compute `verified` (phone OR email, enrich done) → for verified rows call
   `debit_credits`; on insufficient balance mark job `paused_quota`, store the batch's
   unverified rows anyway, reject remaining verified rows (agent keeps them queued locally and
   retries after top-up) → upsert leads → enqueue `webhook_deliveries` for newly-inserted
   verified leads → attempt delivery inline (3 tries, exp backoff), leave `pending` on failure.
6. Agent marks job `done`; browser polls `GET /api/jobs` for live status.

Browser Jobs tab shows queue state clearly: "waiting for your agent — run
`python -m webscraper agent` on your PC" when `queued` and no recent `last_seen_at`.

### Webhook
- Payload: single lead JSON (same shape as export columns) + `{event: "lead.verified",
  job_id, user_id}`. Header `X-Signature: hex(HMAC-SHA256(webhook_secret, body))`.
- Retry: 3 inline attempts; Vercel cron (`/api/cron/webhooks`, protected by `CRON_SECRET`)
  re-drives `pending|failed` with `attempts < 8` every 10 min.
- Settings tab: URL field, secret (regenerate), "Send test event" button, recent deliveries list.

### Payments
- Billing tab: two pack cards → gateway choice.
- **Razorpay**: `POST /api/pay/razorpay/order` (server creates order, amount from server-side
  pack table — never client) → checkout.js modal → `POST /api/pay/razorpay/verify` checks
  `razorpay_signature` (HMAC order_id|payment_id with key secret) → mark order `paid`, insert
  ledger credit. Razorpay webhook `payment.captured` (`/api/pay/razorpay/webhook`,
  signature-verified) as backup path. Crediting idempotent on `gateway_order_id`.
- **PayU**: `POST /api/pay/payu/initiate` returns hash + params → client form-POSTs to PayU →
  PayU redirects to `/api/pay/payu/return` (surl/furl) → server verifies **reverse hash** →
  credit + redirect to `#billing?status=ok|failed`. Same idempotency.
- Keys in Vercel env: `RAZORPAY_KEY_ID/SECRET`, `PAYU_KEY/SALT`, test mode first.

### AI keys
- `/api/suggest`: use member's Gemini key if set, else member's OpenAI key, else Google
  autosuggest fallback (existing code path). Server reads keys from `user_settings`.
- New `POST /api/leads/{place_key}/summarize`: builds prompt from lead fields, calls member's
  key, stores `ai_summary`. Button in lead detail row. No key → button hidden with hint.

## Error handling

- Agent offline → jobs sit `queued`; UI explains. Agent crash mid-job → job stuck `claimed`;
  agent re-claims own stale jobs on restart (claim older than 30 min with no progress → reclaim).
- Sync is idempotent: upsert on `(user_id, place_key)`; re-synced existing leads never
  re-debit (debit only on first insert of a verified lead) and never re-fire webhook.
- Payment verify failure → order `failed`, no credit, user-visible message.
- All `/api/*` errors JSON `{detail}`; 401 on bad/expired JWT → UI drops to login.

## Testing

- Unit (pytest, no network): verified-lead rule, quota math on ledger, PayU hash +
  reverse-hash, Razorpay signature verify, webhook HMAC, agent stale-claim rule.
- Payment E2E manually in both gateways' test modes before going live.
- Existing extractor tests untouched.

## Order of work

0. **Commit baseline** — repo currently has zero commits. Commit current tree as-is first.
1. Migrations (`profiles`, `user_settings`, `agent_tokens`, `scrape_jobs`, leads changes,
   `orders`, `credits_ledger`, `webhook_deliveries`, RPCs) + FastAPI auth layer + login UI.
2. Admin tab (create member, credits adjust) + Settings tab (webhook, AI keys, agent token).
3. Job queue: cloud Jobs tab (create/watch) + `webscraper/agent.py` + CLI + sync endpoint
   with verify/debit/upsert.
4. Webhook dispatch + deliveries log + cron re-drive + test button.
5. Payments: Razorpay then PayU, test mode, then live keys.
6. AI summaries endpoint + button.

Each step ends green (`pytest`) and deployable.

## Out of scope (explicit)

- Central VPS worker, recurring subscriptions, self-signup, email flows (reset via admin),
  multi-currency checkout, marketplace/API for third parties, CRM bridge (separate roadmap
  item in `CLAUDE.md`).
