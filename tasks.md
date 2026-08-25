# TASKS — numbered work log (web-scraper)

`[x]` done · `[~]` in progress · `[ ]` todo. Newest on top.

> **State 2026-08-23: W0-W10 are all done** (W11 open — a pre-existing SaaS-sync break found by the live smoke). Remote is `hardikvij195/web-scraper`, HEAD
> `a544ae7`, tree clean, 0 unpushed. Cross-repo backlog index:
> `../hvt-ai-crm-live/tasks.md` — the items below are the agent half of its **T136**
> (closed); the CRM half (UI, ETA display, job phases in the DB) lives there.
>
> Two known leftovers, neither blocking: `synced_upto` is **in-memory only**, so a restarted
> agent re-sends one job's rows once (absorbed by the `(job_id, place_key)` upsert, just
> wasteful), and the CRM UI does not refresh mid-job to show leads arriving.

---

## Session 2026-08-25 — closed-window crash + Mac agent (CRM T165)

### W16 — discovery lane died on the 2nd closed window; Mac launcher  [x]
Job #13 (CRM) / #28 (local): human closed the headed Chrome window, `Relauncher` relaunched
once ("browser died during tile 1/1050"), the window was closed again 4 s later and the
**retry inside the `except` was unguarded** (`maps.py` old line 526) → `TargetClosedError`
killed the discovery lane, so enrichment + WhatsApp stopped too. Fix: the tile search is now
a `while True` retry through `_recover()` until the relaunch cap (3) is spent; the place loop
already skipped-and-continued. `Relauncher.recover()` now sleeps `RELAUNCH_SETTLE_SEC`=2 s
between closing the dead context and relaunching on the same `user_data_dir`, so a relaunch
can't attach to a still-exiting Chrome. Device targeting (`device` on every CRM call, from
`LEAD_FINDER_DEVICE` or hostname) was already in `agent.py` uncommitted from 08-24; added
the Mac twin of the Windows supervisor: `run-agent-loop.sh` + `scripts/install-agent-autostart-mac.sh`
(launchd, RunAtLoad + KeepAlive) and a README section.

## Session 2026-08-25 — proxy rotation (anti-bot tier 3 plumbing; CRM T163 half a)

Prompt (CRM session, T163): "web-scraper: proxy ROTATION … `ENRICH_PROXIES` … a small
`ProxyPool` … round-robin, per-proxy failure count, temporary quarantine after N consecutive
failures, automatic re-admit after a cooldown … retry once with the next proxy before
escalating tiers … direct (no-proxy) attempt first unless `ENRICH_PROXY_FIRST=1` … surface
which proxy (redacted) … no CAPTCHA solving".

### W15 — proxy rotation  [x]
Builds on W13's single inert `ENRICH_PROXY`. New `proxies.py`: `parse_proxy_list`
(comma/newline/whitespace, `user:pass@host:port`, scheme defaults to http, dedupe, junk
skipped with a warning), `redact()` (`host:port` only — the ONLY shape that reaches logs or
the DB), `proxy_arg()` (Playwright dict; `browser_fetch._proxy_arg` now delegates and takes
an explicit pick), and `ProxyPool` — round-robin `next()`, `failure()`/`success()` streaks,
bench after `ENRICH_PROXY_MAX_FAILURES` (3) consecutive proxy-blamed failures, re-admit after
`ENRICH_PROXY_COOLDOWN_SEC` (300) with an injected clock so it is deterministic under test.
`get_pool()` builds it from `ENRICH_PROXIES` only — **a lone `ENRICH_PROXY` builds no pool
and behaves exactly as W13** (httpx direct, curl_cffi + browser via that proxy).

Ladder (`enrich.crawl_site`): each tier (httpx → curl_cffi → browser) now runs under
`_with_proxies`: direct attempt first; only if that was a BLOCK (403/429/503/timeout) one
attempt via the pool's next proxy; a proxy-blamed failure there (`proxy_407`, or
`proxy_connect` = gateway unreachable) earns ONE retry with the next proxy before the tier
gives up. A proxy failure never becomes the verdict on the site — the tier hands back the
site's own error so the next tier still runs. `ENRICH_PROXY_FIRST=1` flips to proxy → rotate
→ direct; in that mode a proxied 403 followed by a direct 200 is "403 on a known-good page"
and DOES count against the proxy (a bare 403 is ambiguous and does not). httpx gets a
per-attempt short-lived proxied client (`_fetch_ex(proxy=…)`), curl_cffi takes
`impersonate_fetch_ex(url, proxy)` (new; returns `(html, error)` with 407/connect classified),
the browser binds the pool's pick at launch (persistent context = one proxy per launch).
Reporting: `enrich_via='tls@gw.test:7777'`, `enrich_error='http_403@gw.test:7777'`
(`Fetched.tag`, redacted; `is_block` strips the tag). Also closed the T163 denominator gap on
the agent side: `count_pending_enrichment` and the lane's live `enrich_done` now skip
site-less leads, so the CRM's live bar reads done / enrichable like its finished card.
Tests: `tests/test_proxies.py` (+20: parsing, redaction, `_proxy_arg` delegation, rotation,
quarantine/re-admit with a fake clock, all-benched, precedence, every ladder rule, crawl_site
end-to-end with stubbed tiers, curl_cffi error classification). 69 → **89 pass**. Not
exercised against a real proxy (none configured — inert until `ENRICH_PROXIES` is set).
Docs: `.env.example` + `CLAUDE.md` env table.

## Session 2026-08-24 (cont.) — re-enrich counter + headed-window fixes (from live job 11)

Found by driving live job #11 (Clinics, Cambridge) — a `reenrich_only` re-run.

### W14 — enrichment "starting from 0 / 180" + "Show window" ignored on re-run  [x]
Two real bugs, both surfaced on job 11:
1. **Counter**: `EnrichmentLane` set `enrich_total = count_places()` (all 180) while `enrich_done`
   counted only THIS run — so a 21-lead re-enrich read "5 / 180", looking like it restarted from
   scratch. Fixed: `enrich_total = seen + count_pending_enrichment(job)` (new store method, scoped
   to `place_keys`) — tracks correctly for a fresh job (pending grows as discovery feeds) AND a
   re-enrich (fixed subset → "5 / 21"). The agent's re-enrich reset itself was already correct
   (server.py resets only `failed`/`thin` rows, scoped to place_keys).
2. **Headed window**: the CRM's per-job "Show window" toggle (`job.headless`) never reached the
   enrichment browser — `browser_fetch` read the `ENRICH_BROWSER_HEADLESS` env only, and the agent's
   RE-RUN mirror dropped `headless` entirely. So a re-enrich asked to run headed ran hidden. Fixed:
   `enrich_places(headless=…)` threads the flag to `BrowserFetcher`, `EnrichmentLane` passes
   `job.headless`, and the agent re-run mirror now carries `headless`. Verified end-to-end headed on
   job 11's real 403 site (injuryactive.com) — rescued `via=tls` with email+IG, browser launched
   headed and alive. NOTE for the user: because TLS now rescues most 403s cheaply, the browser
   rarely launches during enrichment, so a visible window is now rare-by-design (that is the fast
   path working). 69 tests pass.

## Session 2026-08-24 (cont.) — stealth browser + proxy plumbing (anti-bot tier 2)

Prompt: "do t158 … can't we use my system and run chrom on playwrite and bypass that?" User
picked "stealth browser now + proxy plumbing (off until creds)". The user's instinct — run
their OWN real Chrome, headed, on their home IP — is the right cheap tier and is what got
built.

### W13 — real Chrome + patchright stealth browser + proxy plumbing  [x]
`browser_fetch.py` now (1) prefers **patchright** (drop-in Playwright fork that strips
`navigator.webdriver` / CDP / headless tells) with automatic fallback to stock Playwright;
(2) launches the machine's **real Google Chrome** (`channel="chrome"`, `ENRICH_BROWSER_REAL_CHROME`,
default on) with an automatic fall-back to bundled Chromium when Chrome is absent; (3) reads an
optional **`ENRICH_PROXY`** and wires it into BOTH the browser (`_proxy_arg` → Playwright proxy
dict, user/pass split out) and curl_cffi (`impersonate_fetch`), inert until a URL is supplied;
(4) re-polls a challenge page for up to `CHALLENGE_WAIT_MS`=6 s so a Cloudflare JS challenge
that auto-solves in 3-5 s is caught (the old 1.5 s settle missed those). 67→**69 tests pass**
(added `_proxy_arg` parse tests). Lifts the old `browser_fetch.py` "will not spoof fingerprints"
stance for public-page enrichment, by user directive.

**Honest result — tested headed on the two sites TLS could not crack:** varianse (control)
reads fine; **fullcarchecks.co.uk = a hard Cloudflare 1020 deny** and **autocapital.co.uk = a
managed Turnstile that never auto-solves even at 16 s** — neither passes with real Chrome +
patchright + headed. Those need a CAPTCHA-solving service (2captcha etc.), which is the line
NOT crossed. So tier-2 raises the pass rate on the middle band of blocks (JS challenges a real
browser can ride out) but the hardest managed challenges stay closed. Proxies would not change
these two (they are UA/JS-challenge, not IP-reputation); proxy plumbing is there for the VPS
datacenter-IP case.

## Session 2026-08-24 — TLS-impersonation fetch (anti-bot tier 1)

Prompt (CRM session, ported here): "find best public repos on github on scraping which can
bypass all types of securities … improve ur scraping code so that it can bypass any types of
security". Brainstormed → scoped DOWN to the cheapest legitimate tier (user chose "TLS
fingerprint only"): scraping public business marketing sites past Cloudflare/WAF for contact
info, no proxies, no CAPTCHA solving, no identity rotation. The full stealth-browser +
residential-proxy stack was explicitly deferred.

### W12 — curl_cffi TLS-impersonation retry between httpx and the browser  [x]
**Shipped + proven on real sites.** Smoke against job #7's own 403 domains: varianse.co.uk
and sixt.co.uk (httpx 403) both read 200 via TLS impersonation (105 KB / 232 KB); e2e
`crawl_site("varianse.com")` with NO browser returned `via=tls`, extracted an Instagram — a
lead that would have stayed `http_403`. fullcarchecks + autocapital still block (JS challenge
/ IP reputation → browser or proxies). ~50 % of fingerprint-403s recovered at httpx cost.
67/67 tests pass. Below = the design as built:
Many Cloudflare 403s reject on the TLS/HTTP2 handshake fingerprint alone; `curl_cffi`
`AsyncSession(impersonate="chrome")` sends a real Chrome handshake and gets 200 with no
browser (~0.3s vs the browser's ~5s). New fetch order in `crawl_site`:
`httpx → curl_cffi impersonate → headless browser`. New module `impersonate_fetch.py`
(lazy-imported, optional dep, degrades to today's behaviour if `curl_cffi` absent), a ~5-line
insert in `enrich.py` mirroring the existing `browser_retry` slow-path, an `enrich_via`
(httpx / tls / browser) tag so the rescue method is visible, `ENRICH_TLS_IMPERSONATE` env
(default on), and `curl_cffi` in requirements. Measured target: the 15 http_403s on live job
#7 (11 timeouts / 5 dns / 2 404 are not fingerprint-fixable). Overrides the old
`browser_fetch.py:50-52` "will not spoof fingerprints" stance — by user directive, for
public-data lead-gen only.

## Session 2026-08-23 — three concurrent lanes

Design: [`docs/superpowers/specs/2026-08-23-lead-finder-lanes-design.md`](./docs/superpowers/specs/2026-08-23-lead-finder-lanes-design.md).
CRM half = **T141-T143** in `../hvt-ai-crm-live/tasks.md`.

### W5 - Discovery, enrichment and WhatsApp run at the same time  [x]
Prompt: "in leads finder, can we do lead enrichment and whatsapp verify side by side of each
lead => like 3 different widnows, leads window will pass leads as soon as they get the lead
into the crm, then side by side as new leads come in, the enrichment will happen for leads
that have come in and once lead enrichment is done for that lead, then find wa as well".
`lanes.py` runs three threads against one job; **the `places` table is the queue** (B takes
`enrich_status='pending'`, C takes rows whose enrichment resolved and whose `wa_verified` is
undecided) rather than an in-memory handoff, which leaves `maps.py` alone and makes every
lane restart-safe for free. Each lane owns its own `Store` - sqlite3 connections are not
thread-safe - and lanes write **disjoint columns**, which is the property that makes three
threads on one SQLite file safe. `max_minutes` now caps **discovery only**; enrichment and
WhatsApp drain the backlog afterwards (user's call: a lead found at minute 29 is worth
nothing unverified). AI research is folded into the enrichment lane so a lead reaches
WhatsApp only once everything known about it is known.

### W6 - Per-lane runtime, end reason and success  [x]
Prompt: "also show info how much time time each ran, and the reason of end and was it
successfull".
Job #6 rendered `WhatsApp verification - 2 / 30 numbers` and then the word **done**. It was
not done: enrichment + AI research had spent the shared ~2.5 min post-Maps budget and the
lane exited after two checks. One `phase` column cannot describe three concurrent lanes, so
each lane now writes its own `*_ended_at` / `*_ok` / `*_reason` (`completed`, `no_targets`,
`maps_cap`, `stopped`, `wa_daily_cap`, `wa_not_logged_in`, `disabled`, `error:<detail>`);
`ok` is 1 only for the first two. **A lane that gave up can no longer render as done.**

### W7 - Job logs  [x]
Prompt: "add a logs btn to show logs in ad dialog box for each job on that page".
There was nothing to show: `jobs.message` holds only the latest line and is overwritten
constantly, and the real log went to `data/agent.log` on the PC. New `job_logs` table +
`Store.log()/logs()`, shipped to the CRM on each progress tick behind a `logs_synced_upto`
watermark that only advances after the POST succeeds.

### W8 - Crash recovery on every lane  [x]
Prompt: "is there a backup logic, if chrome shuts down due to some error or any issues, will
it open ?". Partly - **for discovery only**. `a544ae7` gave `maps.py` a relaunch-and-retry;
WhatsApp had none, so a dead WhatsApp Chrome killed that lane. The mechanism moved to
`browser_recovery.py` (`Relauncher`, `is_closed`, cap 3) and is now used by the WhatsApp lane
and the enrichment 403 retry too. Process-level recovery already existed
(`run-agent-loop.bat` + resetting stuck jobs to `queued` on startup).

### W9 - Say WHY enrichment failed, and beat the 403  [x]
Prompt: "why did enrichment failed for some leads => check job 6 once".
Reproduced live: of job #6's 9 failures, **8 are WAF/Cloudflare 403s**
(`lookers.co.uk`, `hrowen.co.uk` x3, `carluv.co.uk`, `mayfairmotorsolutions.com`,
`maryleboneminicabs.co.uk`, `luxurycarsltd.co.uk`) and 1 is a dead domain
(`atypesourcing.com`, `getaddrinfo failed`). `enrich_status='failed'` is set whenever
`pages_fetched == 0` and the reason was never recorded, so a quarter of a London job looked
arbitrarily broken. New `places.enrich_error` (`http_403` | `http_<code>` | `dns` | `timeout`
| `non_html` | `no_pages`), and a block-shaped failure is retried **once through a real
browser**. httpx stays the fast path - a browser is ~15x slower per site and that speed is
why enrichment keeps up with discovery.

### W11 - SaaS lead sync is broken on a missing column  [ ]  (pre-existing, found 2026-08-23)
Surfaced by the live lane smoke, NOT caused by the lane work:
`supabase push 400 {"code":"PGRST204", "message":"Could not find the 'wa_verified' column of
'web_scraper_leads' in the schema cache"}`. `wa_verified` was added to the local `places`
table when WhatsApp verification shipped, but **no migration ever added it to the cloud
table** - `grep -rn wa_verified supabase_migrations/` returns nothing. So every
`supa.push_job()` to the SaaS project fails outright and those leads never reach
`web_scraper_leads`. The CRM path (`agent.py` -> `lead-finder-agent`) is unaffected; only the
`web-scraper-leads.vercel.app` product is. Fix = a migration adding `wa_verified`
(plus `whatsapp_source`, `enrich_error`, and the other columns added since) to
`web_scraper_leads`, applied against Supabase `gfgkcnjxvxlusplwmvae`.

### W10 - "Assumed Mobile" retired, and every WA number gets a `+`  [x]
Prompt: "remove => Assumed Mobile tag => verify it and show the wa link tag, donot assume any
wa link, also add + as well in front of all wa nos".
A plain mobile number is a **candidate for verification, never a claim**. `assumed_mobile`
becomes `unverified` and renders **no tag at all**; only `maps_link`/`wa_link` show "WA link"
and only a real check shows "Verified". Numbers are stored and displayed E.164 with a leading
`+`; `wa.me/` links strip it because that URL form wants bare digits.

---

## Session 2026-08-22

### W1 — Push this repo to GitHub  [x]
Done — `hardikvij195/web-scraper`, `main` pushed. Before this, every commit since
2026-08-21 existed on this disk only.

### W0 — Time-budget split + live streaming  [x]  (committed in `fc7a1f8`)
Was found on disk uncommitted and undocumented — written before this log existed.
- `maps.py` — link collection now takes `collect_until` (a monotonic deadline) and
  `collect_target` (stop once there are more links than the remaining time could ever
  visit). A wide radius tiles into thousands of sub-searches, so without the cap a job
  burned its whole budget collecting links and scraped **zero** places.
- `server.py` — sets that split at **40% collect / 60% scrape** of `max_minutes`, derives
  `collect_target` from `scrape_seconds / delay_sec * 1.3`, and reports the cutover to the
  user ("collection time up at tile 12/150 — scraping the 240 places found so far").
- `maps.py` — tile order changed from query-major to **centre-major**. At 50 km × 52
  keywords that is 7,800 searches; ordered by query a truncated run came back with only
  keyword 1. Now every centre sweeps all keywords, so cutting the run short still leaves
  every category represented.
- `store.py` + `agent.py` — `places_after(job_id, after_rowid)` and a `synced_upto`
  watermark stream places to the CRM **while the job is still running**, so a stopped or
  timed-out job has already delivered what it found. This is **most of W3**.

### W2 — Second phase: enrichment after discovery  [x]  (`65509ac`)
Prompt (2026-08-22): "if the max time is 30 mins => search leads on google maps for 30 mins
and then stop that and start research on leads website and linkedin, insta, fb, whatsapp
numbers, summary".
The Maps deadline now ends **discovery only**. `enriching`, `researching` and
`verifying_wa` run against their **own** deadline with their own `budget_up` predicate, each
writing its phase to the job so the CRM can mirror it. **"30 mins" is read as the Maps cap**,
with research after — the literal reading of the prompt. `MAPS_BUDGET_FRAC` (default `1.0`)
and `ENRICH_BUDGET_FRAC` (default `0.5`) in `webscraper/config.py` make it configurable the
other way, e.g. `0.7`/`0.3` to fit everything inside the 30 minutes.

### W3 — Stream each lead into the job as it is found  [x]  (delivered by W0)
Prompt: "as soon as u find a lead => pass it into lead finder job".
Delivered by the `places_after` + `synced_upto` work in W0: places reach the
CRM as they are scraped, so a job killed mid-run keeps everything it had. Upserts are keyed
on (job_id, place_key) both locally and CRM-side, so re-sending is harmless.
Left to do: `synced_upto` is **in memory only**, so a restarted agent re-sends one job's
rows once (absorbed by the upsert, but wasteful), and the CRM UI does not yet refresh
mid-job to show them arriving.

### W4 — Estimated time on every task  [x]  (`65509ac`)
Prompt: "always show an estimated time for all tasks".
ETAs come from **banked rates, not guesses**: `phase_rates(job_id, phase, units, seconds,
recorded_at)` in `store.py`, read back as a **units-weighted** rolling average over the last
20 rows so a 3-place run cannot swing the number the way a 300-place one should. Precedence
per phase: the live rate from this run (needs a minimum unit count **and** 20s elapsed —
without the elapsed guard a just-resumed phase computes a near-zero rate and promises to
finish in seconds), then history, then nothing, which renders "estimating…". The whole-job
ETA is **null if any remaining phase is unestimable**, because a partial sum silently
under-promises. The old hardcoded `PLACE_OVERHEAD_SEC` / `ENRICH_SEC_PER_SITE` constants are
gone. Verified: pytest 26/26, and a scraping ETA of 2400s correctly clamped to the 1800s cap.
