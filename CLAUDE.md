# web-scraper

Python 3.13 + Playwright Google Maps lead scraper + httpx website enricher — the engine behind the
CRM's Lead Finder. Usage: `README.md`. Second device (Mac agent, device-targeted runs): `MAC-SETUP.md`.
Open work: `tasks.md` (W-numbers). CRM half: `../hvt-ai-crm-live/tasks.md`.

**Before EVERY push:** `python scripts/bump-version.py` (`VERSION`; the CRM flags agents on an older one).

## Layout

```
webscraper/
  cli.py        typer: scrape | enrich | export | run | jobs | stats | serve | agent | doctor
  server.py     FastAPI local UI :8765 + Worker
  agent.py      cloud agent: claims CRM jobs, mirrors into local jobs (jobs.cloud_id), syncs results
  lanes.py      discovery / enrichment / WhatsApp lanes (concurrent)
  maps.py       Playwright: collector tab (tiles search -> job_links + stub places) + opener tab (place panels)
  enrich.py     fetch ladder httpx -> curl_cffi (impersonate) -> browser (-> camoufox); CF wall classify/click
  proxies.py    ProxyPool (ENRICH_PROXIES, quarantine / re-admit)
  extractors.py pure parsing (the tested part)
  store.py      sqlite3: jobs, places, job_links, wa_checks; _migrate(); exports
  wa_verify.py  WhatsApp Web check per number, account rotation
  healthcheck.py  `python -m webscraper doctor`
vercel-app/     Lead Finder Cloud SaaS (web-scraper-leads.vercel.app), own Supabase gfgkcnjxvxlusplwmvae
scripts/        bump-version.py, regress-sites.py, install-agent.{sh,ps1} (the CRM "Install agent" button)
data/           gitignored: leads.db, browser profiles, exports
```

## Rules

- Slow by default (`SCRAPE_DELAY_SEC=6`). Discovery = exactly two Chrome tabs (collector + opener), each
  with its own profile, Playwright instance and `Store`. No concurrency in Maps without a proxy pool.
- Collector never calls the lane's `should_stop` / `wait_if_paused` / `on_event` (bound to the lane
  thread's sqlite connection): reads `stop_ev` / `pause_ev`, pushes events to a queue. Commit the stub
  `places` row BEFORE its `job_links` row.
- **Stub -> fill:** stub `detail_status='pending'`; `upsert_place` COALESCEs every column and sets `done`.
  Outside radius -> `far` (never DELETE; `agent._flat()` sends `_delete` to the CRM). "Real place" queries
  keep `COALESCE(detail_status,'done')='done'`.
- **Lanes: the `places` table is the queue.** Each lane owns its own `Store` and writes disjoint columns
  (`disc_*` / `enr_*` / `wa_*`). Never let two lanes write one column. `max_minutes` caps discovery only.
  End every lane via `Store.lane_end(job_id, lane, reason)` with a real reason.
- **WhatsApp is per NUMBER (`wa_checks`).** Candidates: `wa_link` > Maps phone > `site_phones`, deduped on
  digits. `record_wa_check` re-derives `wa_verified` (any yes -> yes, all no -> no, else unknown),
  `whatsapp_number`, `wa_numbers`. Unknown re-offered once; settled at `checks >= 2` (CRM uses the same).
  Never assume a WhatsApp; numbers stored `+E.164`.
- **Chrome profiles are ours to kill** (T397): `Relauncher(profile_dir=)` evicts the holder,
  `close_blank_pages`, `mark_profile_clean` + `RESTORE_BUBBLE_ARGS`. Crash recovery = `browser_recovery`.
  W120: Maps contexts are recycled every N places/tiles (`Relauncher.recycle`) because a long-lived
  tab grows past 1 GB.
- New field -> `Place`, `PLACE_COLS`, `SCHEMA`, `_migrate()`, `EXPORT_COLS`. Parsing in `extractors.py`
  with a test; Playwright only in `maps.py`.
- Every scraper change is measured with `python scripts/regress-sites.py` against `docs/test-sites.md`.
  `enrich_error` vocab: `tls` / `reset` / `refused` / `cf_deny` (static IP deny, not escalated) /
  `cf_non_interactive` / `cf_managed` / `cf_interactive` / `cf_embedded` / `blocked` / `http_403@<proxy>`.

## Facts

- Headless Chromium gets Google's lite panel: no review count, no price range. Headed = full layout.
- Stable hooks: `div[role="feed"] a[href*="/maps/place/"]`, `button[data-item-id="address"|"oloc"|^"phone:tel:"]`,
  `a[data-item-id="authority"]`, URL `!3d<lat>!4d<lng>`, `!19s<place_id>`.
- One crawl per website domain, per-host cap 2. Radius centre = median of first ~30 results' coords;
  one search caps ~120, so: circle <= 16 km = one whole-circle tile, > 16 km = radius/8 grid; a keyword
  whose feed comes back full is re-searched on four half-size child tiles (W50 / W119).
- Windows console is cp1252 — keep `→` out of typer help strings.

## Env (all optional)

| Var | Default | Effect |
|---|---|---|
| `<PROVIDER>_API_KEY_2..n` | — | extra AI keys per provider, pushed from the CRM registry; local `.env` wins |
| `WA_WINDOW[__<DEVICE>]` | `visible` | WhatsApp Chrome: `visible` / `hidden` / `headless` |
| `WA_DAILY_CAP` | `0` | 0 = unlimited (user directive) |
| `ENRICH_TLS_IMPERSONATE` / `_ROTATION` | `true` / `chrome` | curl_cffi tier + identity list |
| `ENRICH_BROWSER_FALLBACK` / `_HEADLESS` / `_REAL_CHROME` | `true` | browser tier |
| `ENRICH_PROXIES` (supersedes `ENRICH_PROXY`) | — | proxy pool; `_FIRST`, `_MAX_FAILURES` (3), `_COOLDOWN_SEC` (300) |
| `ENRICH_CF_CLICK` | `1` | click Turnstile (user directive: on) |
| `ENRICH_BROWSER_CAMOUFOX` | `0` | Camoufox last tier |
| `ENRICH_BROWSER_IDLE_SEC` | `300` | close idle fallback Chrome (W120, `browser_fetch.py`) |
| `MAPS_RELAUNCH_EVERY_PLACES` / `_TILES` | `40` / `20` | planned Maps context relaunch (W120); 0 = off |

## Lead Finder Cloud (vercel-app)

Own Supabase `gfgkcnjxvxlusplwmvae` (NOT the CRM's). Admin creates members; RLS owner-or-admin; API uses
service role. Verified lead = enriched AND (phone OR email) — only those debit credits (`debit_credits`
RPC) and fire the member's HMAC webhook. Pack prices live server-side in `api/_db.PACKS`. Razorpay / PayU
env not set yet -> payments 503. Deploy: `cd vercel-app && npx vercel deploy --prod --yes`.
