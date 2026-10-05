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
| `WA_RELAUNCH_EVERY_NUMBERS` | `150` | W131: recycle an account's WhatsApp Chrome after N checks; 0 = never |
| `ENRICH_TLS_IMPERSONATE` / `_ROTATION` | `true` / `chrome` | curl_cffi tier + identity list |
| `ENRICH_BROWSER_FALLBACK` / `_HEADLESS` / `_REAL_CHROME` | `true` | browser tier |
| `ENRICH_PROXIES` (supersedes `ENRICH_PROXY`) | — | proxy pool; `_FIRST`, `_MAX_FAILURES` (3), `_COOLDOWN_SEC` (300) |
| `ENRICH_CF_CLICK` | `1` | click Turnstile (user directive: on) |
| `ENRICH_BROWSER_CAMOUFOX` | `0` | Camoufox last tier |
| `ENRICH_BROWSER_IDLE_SEC` | `300` | close idle fallback Chrome (W120, `browser_fetch.py`) |
| `MAPS_RELAUNCH_EVERY_PLACES` / `_TILES` | `40` / `20` | planned Maps context relaunch (W120); 0 = off |
| `WA_RELAUNCH__<DEVICE>` / `MAPS_RELAUNCH__<DEVICE>` / `ENRICH_IDLE__<DEVICE>` | — | T793: per-machine overrides of the three knobs above, pushed from the CRM and refreshed live |
| `MAX_INFLIGHT_JOBS` | `8` | W144: CRASH GUARD only (clamp 1..12) — never a scheduling knob. Jobs start per LANE: Maps = 1 tab, enrichment = `enrich_slots()`, WhatsApp = `wa_slots()` + a usable account (`server.may_start_job` / `job_next_lane`) |
| `MAX_INFLIGHT__<DEVICE>` | — | per-machine override of `MAX_INFLIGHT_JOBS` (W128, CRM `lead_gen_settings` Systems setting); read live, refreshed from cloud every 300s |
| `WA_NO_SESSION_GIVE_UP_SEC` | `900` | W144 (CRM T1024): a WhatsApp lane with no linked account waits this long for a relink (W110), then ends `wa_no_session` so the job ends `incomplete` and the CRM moves its WhatsApp pass to a machine with a session; 0 = wait for ever |
| `LANE_SLOTS_ENRICHMENT` | `2` | local override of jobs whose enrichment lane may run concurrently (W122 `StageGate`); W135: CRM `enrich_slots__<device>` (clamp 1..3) is the normal knob, refreshed live |
| `LANE_SLOTS_WHATSAPP` | `_wa_parallel()` | local override of concurrent WhatsApp lanes; W135: CRM `wa_slots__<device>` (>=1). Lanes yield a slot every 20 businesses / 25 numbers / 5 min when another job waits (round-robin); W136: an idle lane releases its slot |
| `MEMORY_START_MAX_PCT` | `85` | W129: Worker won't start a NEW job at/above this RAM used% (jobs already running keep going) |
| `WA_RESYNC_LONG_WAIT_SEC` | `900` | W143: episode-3 re-sync recovery — kill the profile's Chrome + lock files, relaunch, wait ONE sync this long; episode 4 flags the account `needs_relink` |
| `WA_RECYCLE_BACKOFF_CHECKS` | `400` | W143: after a boot whose sync took > 60 s, hold the W131 recycle for this many checks (heavy-history account); 0 = off |
| `AGENT_LOOP_WATCHDOG_SEC` | `300` | W142: no CRM heartbeat for this long (and not just offline) -> flag jobs, kill our Chromes, `os._exit(3)`; the supervisor loop relaunches. 0 = off |

## Lead Finder Cloud (vercel-app)

Own Supabase `gfgkcnjxvxlusplwmvae` (NOT the CRM's). Admin creates members; RLS owner-or-admin; API uses
service role. Verified lead = enriched AND (phone OR email) — only those debit credits (`debit_credits`
RPC) and fire the member's HMAC webhook. Pack prices live server-side in `api/_db.PACKS`. Razorpay / PayU
env not set yet -> payments 503. Deploy: `cd vercel-app && npx vercel deploy --prod --yes`.

## Rolling out a new agent version (T1011, 2026-10-03)

The CRM `update` command is DEFERRED (W100) until the worker is idle (`current_job is None`). With
`max_inflight__<device>` > 1 and a deep queue the worker never idles — on 2026-10-03 the awaited job on
DELL had finished and the machine still ran 2.1.8 an hour later. What works, per machine, ~2 min:
`stop` command + `lead_gen_agents.enabled = false` (the park stops the lanes; re-queue its running rows
on the same `target_agent`) → the parked loop runs the deferred update and restarts → `start` command +
`enabled = true`. After the restart the agent may report the parked jobs as `stopped` (CRM row stays
`running`, no `alive` block) — re-queue those; the CRM auto-heal picks them up anyway after 30 min.
W137 todo: drain mode (stop claiming while an update is parked) and resume parked jobs after a restart.

## Stage slots (W122 / W135 / W136 / W144)

W144: there is NO job cap. The Worker starts a queued job when the lane it needs FIRST (`job_next_lane`:
discovery > enrichment with websites pending > WhatsApp with numbers pending) has a free slot right now
(`StageGate.free()` minus promised starts; WhatsApp also needs a usable account). `capacity()` reports
`maps_free` / `enrich_free` / `wa_free` + `lane_slots` and the CRM offers one job per idle lane.
`max_inflight_jobs()` (default 8) is only the crash guard.

`enrichment` and `whatsapp` are `StageGate`s shared by every job in the process (slots from the CRM
`enrich_slots__` / `wa_slots__`). A lane holds a slot ONLY while it has a batch in hand: idle waits (empty
queue with the feeder still running, enrichment's "WhatsApp first" wait, WhatsApp login / relink waits)
call `_slot_idle()` and the lane re-queues with `_slot_resume()` when work shows up (W136, T1015 — DELL
deadlocked 21 h on two idle holders). Never add a wait inside `work()` that keeps the slot.
W137: a WhatsApp Web session stuck on its sync splash benches the account after 3 non-answers (nothing
recorded), the lane parks 5 min at a time; W143: per-ACCOUNT ladder — episode 3 browser recovery + one long
sync wait, episode 4 `needs_relink` (self-check shows NEEDS RELINK, `wa_login` clears it). The CRM judges a
machine by WORK done (`lead_gen_frozen_machines`: counters + useful output), never by log/row activity.
W138: lanes of different jobs run together — the Worker starts a lane-only job (re-enrich / WhatsApp-only)
while another job's Maps runs (`job_needs_discovery`, `capacity().lanes_free`, Edge Function offers only
no-Maps jobs then), and websites crawl alongside Maps / WhatsApp whenever nobody else is queued for the
websites slot (W76 is a priority, not a block). Machine-wide concurrency is still bounded by the slots.
