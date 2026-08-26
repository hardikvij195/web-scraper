# web-scraper — Claude guide

Python 3.13 + Playwright Google Maps lead scraper + httpx website enricher. Lives in
`Hv Technologies\web-scraper`, own git repo. Built 2026-08-20 as the local spike for the
"Lead Finder" idea (replaces Apify; runs on the user's PC first, VPS later). Read `README.md`
for usage; this file is for working on the code.

> **W26 (2026-08-25): discovery = collector + opener tabs side by side; WhatsApp checks EVERY number** (Maps phone immediately, website numbers after enrichment) with per-number verdicts in `wa_checks`. See Rules below and `tasks.md` W26.
> **WhatsApp cap (2026-08-25):** `WA_DAILY_CAP` defaults to 0 = unlimited (user directive); job logs now carry one line per site / number (W25).
> **Self-check / installers (2026-08-25):** `webscraper/healthcheck.py` + `python -m webscraper doctor`; `scripts/install-agent.{sh,ps1}` are what the CRM's "Install agent" button downloads (raw GitHub → repo must be public).
> **Versioning (2026-08-26):** `VERSION` (1.0.1, 1.0.2 … 1.0.9 → 1.1.0) — run `python scripts/bump-version.py` before every push that touches agent code; the CRM shows "outdated" against main's `VERSION`.
> **Mac / second device?** Read [`MAC-SETUP.md`](./MAC-SETUP.md) — device-targeted runs, launchd agent, multi-device test plan (2026-08-25).

## Layout

```
webscraper/
  cli.py         typer CLI: scrape | enrich | export | run | jobs | stats | serve | agent
  server.py      FastAPI local UI (:8765) + single Worker thread; jobs.phase drives progress;
                 routes /api/jobs (POST/GET), /api/jobs/{id}[/places|/stop|/export?fmt=xlsx|csv|json]
  agent.py       cloud agent mode: polls the Vercel API for the member's queued jobs, mirrors them
                 into local jobs rows (jobs.cloud_id), reuses the same Worker, syncs results up
  static/index.html  vanilla-JS single page: form -> jobs list -> progress bars -> leads table -> downloads
  maps.py        Playwright, TWO tabs since W26: a collector thread tiles the search and
                 persists each tile (job_links + stub places rows) while the opener thread
                 reads place panels from job_links in feed order; profiles
                 data/browser-profile (collector) + data/browser-profile-open (opener)
  enrich.py      async httpx: home + contact/about pages -> emails, socials, WhatsApp;
                 fetch ladder httpx -> impersonate_fetch (curl_cffi) -> browser_fetch (Playwright)
                 (-> camoufox_fetch with ENRICH_BROWSER_CAMOUFOX=1); browser_fetch classifies
                 Cloudflare walls (cf_*) and can click Turnstile with ENRICH_CF_CLICK=1 (W22)
  proxies.py     W15 ProxyPool: ENRICH_PROXIES round-robin, quarantine/re-admit, redact()
  extractors.py  pure functions (regex/selectolax) — the only part with unit tests
  store.py       sqlite3 (jobs, places, job_links, wa_checks), stats, CSV/JSON export, tiny
                 ALTER-TABLE migrate; wa_candidates() / aggregate_wa() = the per-number WA model
  wa_verify.py   WhatsApp Web check per NUMBER (maps phone | wa_link | site), account rotation
  models.py      Place / Contacts dataclasses
  config.py      .env -> Settings (delay, pauses, headless, proxy, country, paths)
vercel-app/      cloud product on Vercel (web-scraper-leads.vercel.app) — see "Cloud product" below
  api/index.py   app assembly (mounts routers below, serves index.html, /api/config, /api/health)
  api/_db.py     PostgREST/GoTrue helpers + PACKS (pack prices live HERE, server-side only)
  api/_auth.py   JWT session dep + X-Agent-Token dep; _accounts.py login/settings/agent-tokens
  api/_admin.py  member management; _jobs.py browser job CRUD; _agent.py claim/progress/sync
  api/_webhooks.py signed per-lead webhooks + /api/cron/webhooks; _pay.py Razorpay+PayU; _ai.py
  index.html     login overlay + tabs: Scraper(jobs) / All leads / Billing / Settings / Admin
supabase_migrations/  001 accounts, 002 jobs+credits (+debit_credits RPC), 003 leads multiuser
scripts/         apply_migrations.py (psycopg2 pooler scan), create_admin.py (GoTrue admin REST)
tests/test_extractors.py + tests/cloud/ (pure-logic unit tests for the cloud API)
data/            gitignored: leads.db, browser-profile/, browser-profile-open/, exports/
```

## Cloud product (Lead Finder Cloud, since 2026-08-21)

- **Spec/plan:** `docs/superpowers/specs/2026-08-21-lead-finder-cloud-design.md` +
  `docs/superpowers/plans/2026-08-21-lead-finder-cloud.md`.
- **Supabase project `gfgkcnjxvxlusplwmvae`** (own — NOT the CRM's). Auth: admin creates members
  (no self-signup); roles in `profiles`. RLS owner-or-admin everywhere; the API uses the service
  role key from env.
- **Job flow:** member creates job in browser → `scrape_jobs` queued → member's PC runs
  `python -m webscraper agent --token <wsk_…>` → claims, scrapes locally, `POST /api/agent/sync`.
- **Verified lead** = enrich_status finished AND (phone OR email). Only verified leads debit
  credits (`debit_credits` RPC, race-safe, partial-accept → job `paused_quota`) and fire the
  member's webhook (HMAC `X-Signature`, retries, `webhook_deliveries` log, daily Vercel cron
  re-drive `/api/cron/webhooks` with `CRON_SECRET`).
- **Packs (one-time):** `starter_3k` 3,000 leads $10/₹880 · `pro_5k` 5,000 leads $15/₹1,320 —
  amounts are server-side constants in `_db.PACKS`, never trust the client. Razorpay =
  order → checkout.js → server signature verify (+ `payment.captured` webhook backup). PayU =
  server hash → form redirect → reverse-hash verify on `/api/pay/payu/return`. Crediting
  idempotent per order.
- **AI keys:** member's own Gemini/OpenAI keys (Settings tab, stored server-side, masked in API)
  power `/api/suggest` + `/api/leads/summarize`; server `GEMINI_API_KEY` is the fallback.
- **Vercel env (production):** SUPABASE_PROJECT_URL / SERVICE_ROLE_KEY / ANON_KEY,
  GEMINI_API_KEY, APP_BASE_URL, CRON_SECRET set 2026-08-21; RAZORPAY_KEY_ID/KEY_SECRET/
  WEBHOOK_SECRET + PAYU_KEY/SALT/PAYU_BASE **not set yet** — payments 503 until then.
  Local mirror: `vercel-app/.env.deploy` (gitignored).
- Deploy: `cd vercel-app && npx vercel deploy --prod --yes`. Local API run for testing:
  `set -a && . ./.env.deploy && set +a && python -m uvicorn api.index:app --port 8899`.

## Facts learned the hard way (2026-08-20)

- **Google serves a lite place panel to headless Chromium** (both headless-shell and full
  `channel="chromium"` new-headless): tabs = Overview/About only, no review count, no price
  range, feed aria-label is just `"4.2 stars"`. **Headed** (`--no-headless`) gets the full
  layout: `4.2 (5,647)` + Reviews tab + `₹200–400`. `scrape_place` parses those from the
  panel `inner_text`; they stay `None` headless. Everything else (name, category, address,
  phone, website, plus code, lat/lng, place_id) is identical in both modes.
- Stable hooks: `div[role="feed"] a[href*="/maps/place/"]` (+ `aria-label` = name),
  `button[data-item-id="address"|"oloc"|^"phone:tel:"]`, `a[data-item-id="authority"]`,
  `button[jsaction*="category"]`, `span[role="img"][aria-label*="stars"]`, `div[role="main"] h1`.
  URL carries `!3d<lat>!4d<lng>`, `!19s<ChIJ place_id>`, `!1s<cid hex>`.
- Maps panel sometimes shows a WhatsApp button (`wa.me/…`, `api.whatsapp.com/send?phone=…`,
  or `wa.link/<slug>` short link). Captured into `raw.maps_wa_links`; `wa.link` is resolved
  by following the redirect during `enrich`. Source priority: `maps_link` > `wa_link` (site)
  > `unverified` (a plain number we have NOT confirmed) > `none`. A real check promotes it to
  `verified`; a miss clears the number. `assumed_mobile` was retired 2026-08-23 — never assume
  a WhatsApp, and never render a tag for `unverified`. Numbers are stored `+E.164`.
- Windows console is cp1252 — `__main__.py` reconfigures stdout/stderr to UTF-8; avoid `→`
  in typer help strings anyway.
- Enrichment: one crawl per website **domain** (chains list 5–10 Maps entries on one site),
  per-host cap 2, contact pages only when home lacks email/socials. Before that, 9 branches of
  onebodyldn.com (~5 s/page) hogged the slots and tripped timeouts on unrelated sites.
- **Radius**: headless Maps never exposes the map centre (URL has no `@lat,lng`, `og:image` is a
  stale default), so `resolve_center` = median of the first ~30 results' `!3d!4d` coords. Feed
  links carry coords → far places are dropped *before* visiting. Unlimited/large asks inside a
  radius are **tiled** (`grid_centers`, ~2 km tiles, ≤150) because one Maps search caps ~120.
- **Run window**: `jobs.window_start/end` "HH:MM" local; worker skips jobs outside (phase
  `waiting`) and `wait_if_paused` blocks between places if the window closes mid-run.
- Country per lead: `country_from_address` (trailing country name) → job country → `IN`.
  Phone/WA region: lead's own `+cc` > address country > job country.
- First test numbers (Pune, 31 places across dentist / interior designer / cafe): phone 100%,
  website 96%, email ~50%, Instagram ~55%, Facebook ~50%, LinkedIn ~20%, X ~25%, explicit
  WhatsApp link ~32%, mobile-heuristic covers most of the rest. ~3.5 s/place at `--delay 3`.

## Env — enrichment anti-bot tiers (all optional, inert when unset)

| Var | Default | Effect |
|---|---|---|
| `ENRICH_TLS_IMPERSONATE` | `true` | W12: curl_cffi Chrome-fingerprint retry between httpx and the browser |
| `ENRICH_BROWSER_FALLBACK` / `_HEADLESS` / `_REAL_CHROME` | `true` / `true` / `true` | W13: browser tier on/off, window, use installed Chrome |
| `ENRICH_PROXY` | — | W13: ONE proxy URL for the curl_cffi + browser tiers (httpx stays direct) |
| `ENRICH_PROXIES` | — | W15: comma/newline list (`user:pass@host:port` ok). **Supersedes `ENRICH_PROXY`.** Every tier: direct attempt, then if blocked one attempt via the pool's next proxy; a 407 / unreachable gateway earns one retry with the next proxy before the tier escalates |
| `ENRICH_PROXY_FIRST` | `0` | W15: proxy before the own-IP attempt (VPS / datacenter IP) |
| `ENRICH_PROXY_MAX_FAILURES` / `_COOLDOWN_SEC` | `3` / `300` | W15: consecutive proxy-blamed failures before a proxy is benched, and for how long |
| `ENRICH_CF_CLICK` | **`1`** | W22: press a managed/interactive Cloudflare Turnstile checkbox in the browser tier (≤3 tries). Default ON — user directive 2026-08-25. `0` = the wall is only classified — `enrich_error` = `cf_non_interactive` / `cf_managed` / `cf_interactive` / `cf_embedded` / `blocked` |
| `ENRICH_BROWSER_CAMOUFOX` | `0` | W22: last tier after Chrome — Camoufox (fingerprint-rewritten Firefox, stock Playwright, `pip install camoufox && camoufox fetch`). Fingerprint frozen in `data/camoufox-profile/camoufox-opts.json`; delete to re-roll. `enrich_via='camoufox'` |

W22 also fixed the non-browser tiers' identity: httpx sends a full Chrome 150 header set
(`sec-ch-ua`, `Sec-Fetch-*`, Google referer) matching curl_cffi's `chrome` alias, and the
browser tier launches with Scrapling's stealth args/context (no `IsolateOrigins` flag, `--lang`
instead of `locale`, dark scheme, DPR 2, headless UA de-"Headless"ed). Bench on job #7's 19
blocked URLs: 9 → 12 readable with the click off, 15 with it on (the default), Camoufox +0.

The proxy that read (or failed) a site shows up redacted (`host:port`, never credentials) as
`enrich_via='tls@gw:7777'` / `enrich_error='http_403@gw:7777'`. The browser binds its proxy at
launch (one pick per launch); httpx/curl_cffi rotate per attempt.

## Rules

- Keep it slow by default (`SCRAPE_DELAY_SEC=6`). Never add concurrency to the Maps step
  without a proxy pool; the enricher is the place for parallelism. **Under W26 discovery is
  exactly two Chrome tabs** — one collector (search feeds) and one opener (place panels),
  each with its own persistent profile, Playwright instance and `Store`. Do not add a third.
  The collector never calls the lane's `should_stop` / `wait_if_paused` / `on_event` (they
  are bound to the lane thread's sqlite connection): it reads `stop_ev` / `pause_ev` and
  pushes events into a queue the opener drains. Stub rows are committed BEFORE their
  `job_links` row so the opener never picks a link whose place row does not exist yet.
- **Stub → fill.** The collector writes a `places` row with `detail_status='pending'`
  (name/rating/reviews/coords/maps_url from the feed card) per link; the opener's
  `upsert_place` fills it and sets `detail_status='done'`. `upsert_place` COALESCEs every
  column so a NULL from the panel never wipes what the stub had. Enrichment queues only
  `detail_status='done'` rows (a stub has no website); WhatsApp does not wait for enrichment.
- **WhatsApp is per NUMBER (`wa_checks`).** Candidates = `wa_candidates(row)`: a WhatsApp
  link (`wa_link`) > the Maps phone (`maps`) > every number the website lists
  (`site_phones`, `site`), deduped on digits. The Maps phone is checked as soon as the
  opener writes it; site numbers join once `enrich_status` resolves. `record_wa_check`
  re-derives `wa_verified` (any yes → yes, all no → no, else unknown), `whatsapp_number`
  (first yes in that priority, source `verified`) and the `wa_numbers` JSON summary. The WA
  lane's done/total are numbers, not places.
- **Lanes (2026-08-23, `lanes.py`): the `places` table is the queue.** Discovery, enrichment
  and WhatsApp run concurrently; each owns its **own `Store`** (sqlite3 connections are not
  thread-safe) and each writes **disjoint columns** (`disc_*` / `enr_*` / `wa_*` plus its own
  counters). That disjointness is what makes three threads on one SQLite file safe — preserve
  it when adding a counter, and never have two lanes write the same column.
- `max_minutes` caps **discovery only**. Enrichment and WhatsApp drain the backlog afterwards.
- A lane must never report success when it gave up: end every lane through
  `Store.lane_end(job_id, lane, reason)` with a real reason token.
- Browser crash recovery is shared — `browser_recovery.Relauncher` / `is_closed`. Any new
  Playwright surface uses it rather than growing its own copy.
- New extracted field = add to `Place`, `PLACE_COLS`, `SCHEMA`, `_migrate()` and
  `EXPORT_COLS` in `store.py`. Schema changes on an existing DB go through `_migrate()`.
- Pure parsing goes in `extractors.py` with a test; Playwright code stays in `maps.py`.
- No Supabase/CRM wiring yet — when added, write to HVT CRM via REST/service role from a
  separate `sync.py`, never from `maps.py`/`enrich.py`.

## Roadmap

1. CRM bridge: `lead_gen_jobs` + `lead_gen_results` tables in `hvt-ai-crm-live`, Lead Finder
   module (form -> job -> review table -> import to Leads), wallet debit.
2. Dockerfile + compose for the VPS (`cpus: 1`, `mem_limit: 2g`, off-peak cron or queue).
3. Optional: Hunter/Apollo fallback for email when the crawl finds none; Justdial/IndiaMART
   sources; JS-render fallback for `thin` sites.

## Open work

Tracked in [`tasks.md`](./tasks.md). **W0–W4 are done** (remote, budget split, live streaming,
phased jobs, banked-rate ETAs). In flight as of 2026-08-23: **W5–W10** — three concurrent
lanes, per-lane runtime/reason/success, job logs, crash recovery on every lane, recording
*why* enrichment failed plus a browser retry for WAF 403s, and retiring the "Assumed Mobile"
tag. Design:
[`docs/superpowers/specs/2026-08-23-lead-finder-lanes-design.md`](./docs/superpowers/specs/2026-08-23-lead-finder-lanes-design.md).
The CRM half is **T141–T143** in `../hvt-ai-crm-live/tasks.md`.
