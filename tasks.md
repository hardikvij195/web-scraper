# TASKS — numbered work log (web-scraper)

`[x]` done · `[~]` in progress · `[ ]` todo. Newest on top.

> **State 2026-08-25: W26 done (uncommitted)** — discovery is a collector + opener pair, WhatsApp checks every number; 117 tests pass. Earlier: **W0-W10 are all done** (W11 open — a pre-existing SaaS-sync break found by the live smoke). Remote is `hardikvij195/web-scraper`, HEAD
> `a544ae7`, tree clean, 0 unpushed. Cross-repo backlog index:
> `../hvt-ai-crm-live/tasks.md` — the items below are the agent half of its **T136**
> (closed); the CRM half (UI, ETA display, job phases in the DB) lives there.
>
> Two known leftovers, neither blocking: `synced_upto` is **in-memory only**, so a restarted
> agent re-sends one job's rows once (absorbed by the `(job_id, place_key)` upsert, just
> wasteful), and the CRM UI does not refresh mid-job to show leads arriving.

---

## Session 2026-08-26 — installer 404 → repo PUBLIC; Mac pip fix; CRM-driven wa-login; honest AI-research failures

| # | What | State |
|---|---|---|
| **W34** | Mac re-registered as `Unknown_26:e5:…` then `Unknown_ba:67:…` — a fresh device on every reboot (launchd hostname), orphaning pinned jobs + queued commands | `[x]` 2026-08-26 — `_device_name()` now persists its pick in `data/device_name` (order: file → `LEAD_FINDER_DEVICE` → `scutil ComputerName`/`LocalHostName` → hostname) so the label never changes again; new CRM commands `rename <new>` (writes the file + swaps `DEVICE_NAME` live) and `checks` (self-check now). CRM T207 has the Rename / Remove / Re-check / Start-WhatsApp buttons. |
| **W33** | Mac stuck at "waiting for Hardiks-MacBook-Pro.local to pick it up" after the Start-WhatsApp-session click — the Mac agent still ran install-time commit `71680d4`, so it never polled `command` | `[x]` 2026-08-26 — `run-agent-loop.sh` / `.bat` now `git pull --ff-only` + `pip install -r requirements.txt` before EVERY agent start (offline/diverged = run what is on disk), so a restart is an upgrade. One last manual restart per machine is needed to get THIS change: Mac `cd ~/hvt-lead-finder-agent && git pull && launchctl kickstart -k gui/$(id -u)/app.hvtechnologies.leadfinder-agent`; PC = restart the "HVT Lead Finder Agent" task. |
| **W31** | CRM can start a WhatsApp session on an agent machine (CRM T202): `agent.py` polls `command` each tick, runs `wa_verify.login(<label>)` in a thread (headed WhatsApp Web + QR on THAT machine), reports `command_done`, re-sends the self-check | `[x]` 2026-08-26 — needs the agent restarted on each machine to pick up the new code (Mac: re-run the installer one-liner or `launchctl kickstart`; PC: restart the "HVT Lead Finder Agent" task). |
| **W32** | Job #17 (Mac, Singapore clinics): AI research lane said "50 done" while every Gemini call 429'd (key out of quota) — CRM showed 0 summary / 0 owner and no reason. Website crawl itself was fine (50/50 sites, 34 emails, 45 IG/FB, 43 WA) | `[x]` 2026-08-26 — `research.py` now records the HTTP status per failure (`Gemini quota exhausted (HTTP 429) — replace GEMINI_API_KEY …`), `lanes.py` logs `AI research failed for N of M — <why>` per batch and ends the enrichment lane `error:AI research: <why>` when ≥ half the Gemini calls fail, so the CRM lane card reads Failed with the reason instead of Completed. Root cause (exhausted Gemini key shared with the WA chatbots) is the user's to fix: new key in Lead Finder Setup or the agent `.env`. |
| **W30** | Mac installer (screenshot 2026-08-26 11:41, `Hardik - MAC`, Python 3.14.4) got past the clone (W29 public flip works) then died at step 3/6 `python deps`: `ERROR: Invalid requirement: 'curl_cffi>=0.7': Expected a marker variable or quoted string` | `[x]` 2026-08-26 — root cause: three `requirements.txt` lines (`curl_cffi`, `patchright`, `psycopg2-binary`) used `;` to start an inline comment; pip parses `;` as the PEP 508 environment-marker separator, so the comment text was read as a marker and rejected. Changed all three to `#` comments (`pip install --dry-run -r requirements.txt` exit 0). Installer clones `main`, so the push is the fix — user re-runs the same install line on the Mac. |
| **W29** | One-click agent installer 404'd on both a Mac and a second Windows laptop (`iwr raw.githubusercontent.com/.../install-agent.ps1` → 404, then CommandNotFoundException on the missing file) | `[x]` 2026-08-26 — root cause: `hardikvij195/web-scraper` was still **private**, so both the raw download AND the `git clone` inside the script fail without auth (the long-known blocker). Scanned every tracked file for key material (JWTs, `sk_live`, `wsk_`, `rzp_live`): **none** — only `.env.example` is tracked, `.env`/`data/` gitignored. User approved → flipped repo to **public** via GitHub API (`PATCH /repos … {"private":false}`). Verified: raw URL now 200. User re-runs the same installer line on both machines. ⚠ Screenshot showed agent token `wsk_jPoT…g42R` in the command — hash-only server-side; re-mint from SaaS Settings if it ever leaks beyond a screenshot. |

---

## Session 2026-08-25 — concurrent discovery + every-number WhatsApp

Prompt (verbatim): "divide google discovery into 2 jobs => as it scraps the places, it adds
whatever info it has in the crm, it runs one more chrome tab to open those places one by one
and scrape reviews and more info => so 2 chrome tabs will run side by side for the google
maps scraper => and then as soon as each place comes in and updates in crm => websites
scraper starts and wa scraper starts side by side => make sure to check all nos in whatsapp
verification => the number we have got from google maps and all numbers we have got from
website".

### W28 — Google reCAPTCHA handling (was slipping through / not clicked)  [x]
`osbournepinner.com` served a Google reCAPTCHA "I'm not a robot" checkbox (NOT Cloudflare
Turnstile, which is all `_click_turnstile` handled). It slipped past `looks_blocked` (no
matching marker) so the challenge HTML was returned as a "page" with no contacts, and the
checkbox was never pressed. Added: reCAPTCHA markers to `_BLOCK_MARKERS`; `detect_cloudflare`
returns `recaptcha`; `cf_error`/`is_block` treat it as a block; `_click_recaptcha` presses
the anchor-iframe checkbox (same `ENRICH_CF_CLICK` gate) and BAILS if the image (bframe)
challenge appears — no solver crossed. 117 tests pass.

### W27 — installers take the CRM's Supabase URL (clone tenants)  [x]
`install-agent.sh --crm-url` / `install-agent.ps1 -CrmUrl` write `VITE_SUPABASE_URL` into the
agent `.env`, which `cli._crm_base()` already prefers over the HVT default — so the launcher a
CLONED CRM downloads points its agent at the tenant project. The CRM embeds its own
`VITE_SUPABASE_URL` in the launcher (CRM T184).

### W26 — concurrent discovery (collector + opener) + every-number WhatsApp  [x]
**Why.** `run_scrape` collected ALL links (tiles × keywords, up to `collect_until` /
`collect_target`) and only then opened places, so enrichment and WhatsApp — which poll the
`places` table — idled for the whole collect phase. Job #16: 96 min, 1,260 links, 0 places,
nothing in the CRM.

**A. Discovery = two Chrome tabs side by side** (`maps.py`). A **collector** thread runs
today's tile/keyword loop on `data/browser-profile` and, per tile, commits a stub `places`
row (`detail_status='pending'`: name, rating, review count, coords, maps_url from the feed
card) and then the `job_links` row — stub first, so the opener never picks a link whose row
is missing. The **opener** (the discovery lane's own thread, profile
`data/browser-profile-open`) takes `next_pending_link` in feed order → `scrape_place` →
`upsert_place` (now `COALESCE(excluded.c, places.c)` per column, so a NULL from the headless
panel keeps the stub's rating/reviews/coords) → `detail_status='done'`; it idles 2 s when the
queue is empty and ends when the collector is done and the queue is drained, or on
`should_stop` (Stop / Maps deadline), which also tells the collector to quit. Each thread has
its own Playwright, own `Store`, own profile; the collector never touches the lane's closures
(sqlite is thread-bound) — it reads `stop_ev`/`pause_ev` and queues events the opener drains
on the lane thread. `collect_until`/`collect_target` cap the collector only; the opener runs
to the deadline. Far-on-exact-coords → the stub is dropped again. `preset_links`
(`discovery_pending`, W21) seeds stubs+links and runs the opener alone. Enrichment queues only
`detail_status='done'` rows (`pending_enrichment`, `count_pending_enrichment`); old rows are
migrated to `'done'`. `eta._live_counts` discovery = (panels read, `jobs.disc_active`,
unopened links). Logs: `tile i/n: +k links, total m` per tile, the existing per-place line,
`collect_failed` / `browser_restart` as warnings. A search that fails with nothing opened
still raises (lane `error:`), not a silent "done".

**B. WhatsApp checks every number** (`store.py`, `wa_verify.py`, `lanes.py`, `agent.py`,
`enrich.py`, `extractors.py`). New table **`wa_checks(job_id, place_key, number, source
maps|wa_link|site, verdict yes|no|unknown, checked_at, account)`**, PK `(job_id, place_key,
number)`. `wa_candidates(row)` = WhatsApp link > Maps phone > every number the website
listed, deduped on digits (the `unverified` guess IS the Maps phone and collapses into it);
site numbers only once `enrich_status` has resolved, the Maps phone **immediately** — no
enrichment wait. Enrichment now stores **`site_phones`** (new `extract_phones`: `tel:` links,
then phonenumbers' VALID matcher over the visible text, ≤6, E.164 `+`). `pending_wa_verify`
/ `count_wa_pending` / `count_wa_done` are per NUMBER (place row + `number` + `source`); a
number with any `wa_checks` row is not re-offered in the run (an `unknown` used to loop).
`record_wa_check` re-derives the place: `wa_verified` = yes if ANY number is yes, no if all
checked are no, else unknown; `whatsapp_number` = first yes in wa_link > maps > site
(`whatsapp_source='verified'`); all-no clears an `unverified` guess; `wa_numbers` JSON
`[{number, source, verdict}]` on the row. `verify_places` takes one-number rows from the lane
or expands bare rows itself; callback is `(place_key, status, number, source)`. Log line:
`Name · +44… (maps|whatsapp link|website) → ON WhatsApp ✓ / not on WhatsApp ✗`. The WA
lane's done/total/queued are numbers. CRM re-verify (`agent._reverify_wa`) uses the same
candidates + aggregation (units = numbers) — but the CRM `results` action carries no
`site_phones`, so a re-verify covers Maps phone + WhatsApp link only; website numbers are
checked in the live job. `wa_numbers` rides in the sync row (`supa._COLS`): the CRM Edge
Function's `sync` whitelists `LEAD_COLS` and silently drops unknown keys, so it is harmless
there and becomes useful once the CRM adds the column (`set_wa` likewise ignores it).
`python -m webscraper wa-verify <job>` now iterates unchecked numbers.

**C. Tests + smoke.** `tests/test_w26_discovery.py` (+13): extract_phones; stub → fill via
COALESCE (+ `changed_at` bump for W20 streaming); pending_enrichment excludes stubs; old-row
migration; WA pending = Maps phone before enrichment, wa_link + site after, dedupe, verdict
aggregation and promotion order, unverified clearing; `_wa_line` wording; the real
`verify_places` with a fake WhatsApp page recording two per-number verdicts;
`_live_counts` discovery = (1, 1, 2); and `run_scrape` with a fake Playwright: the opener
fills tile 1 while tile 3 is still collecting, every place existed as a stub before its
fill, feed-card name/rating survive; preset_links never starts the collector; Stop ends both
threads in < 5 s. **104 → 117 pass.**

Smoke (real Maps, headless, temp store + temp profiles, 2 keywords `cafe, bakery`, 1 km
radius around Koregaon Park, `max_places=8`, delay 2 s):

```
   0.77s  center 18.5362,73.8939 z15
  28.77s  collector tile 1/2: +8 links, total 8      ← limit reached on tile 1
  31.01s  opener   opening ChIJqUxT…                ← opener starts while the collector is finishing
  32.39s  opener   filled 1/8 'Cafe - The Voyage' · +918596950267
  32.39s  collector DONE — 8 links (far 4)
  35.72s  opener   filled 2/8 'Cafe MAPLE'
  …
  52.86s  opener   filled 8/8 'Cafe Soussol' · +919112372777 · https://soussol.in/
places: 8  detailed: 8  links: (8, 8) — every row has changed_at set, i.e. the fill was an
UPDATE on a pre-existing stub (only the UPDATE trigger writes changed_at)
WA numbers checkable right now (Maps phones, before enrichment): 6
```

Unlimited variant (`max_places=0`, stop after 10 fills) — the second keyword's tile is
collected while the opener is already opening the first keyword's places:

```
   2.46s  center 18.5362,73.8939 z15
  22.46s  collector tile 1/2 (cafe): +17 links, total 17
  24.05s  opener   opening ChIJqUxT…  (row before fill: pending)   ← stub existed
  25.49s  opener   filled 1/17 'Cafe - The Voyage' · +918596950267
  27.62s  opener   opening ChIJM-jw…  (row before fill: pending)
  …
  42.51s  opener   opening ChIJo3RE…  (row before fill: pending)
  43.23s  opener   filled 6/22 'Jubilee cafe Pune' · +919272102369
  43.23s  collector tile 2/2 (bakery): +5 links, total 22          ← landed while the opener was on place 6
  46.37s  collector DONE — 22 links (far 58)
  48.58s  opener   filled 8/22 'Cafe Soussol' · +919112372777 · https://soussol.in/
  54.80s  opener   filled 10/22 'Café Vinyaasa' · +918010255853
  54.80s  abort stopped by user (should_stop after 10)             ← collector already ended; join instant
places: 22  detailed: 10  links: (22, 10) — the 12 unopened ones are stubs (name + rating
+ coords from the feed card, detail_status='pending'); WA numbers checkable now: 8 (maps)
```

---

## Session 2026-08-25 — nodriver / Scrapling / Camoufox source read, techniques ported

Prompt (CRM session, ported): "This is the result of reading nodriver, Scrapling and Camoufox
source. Port these techniques (no new heavy dependencies except optional camoufox which is
ALREADY installed locally …). No proxies needed (leave W15 plumbing intact/inert). No
CAPTCHA-solving services." Follow-up the same day: "the Turnstile checkbox click is
APPROVED and must be ENABLED BY DEFAULT."

### W25 — detailed job logs (CRM T179)  [x]
The CRM Logs dialog now tells the whole story: discovery logs "Google Maps offered N places"
and one line per place opened (`i/n Name · phone · website`); enrichment logs each batch
("crawling 10 website(s): …") and one line per lead — `Name · site → done via tls · 2 emails,
instagram, whatsapp (wa_link)` / `→ FAILED: cf_interactive` (warn) / `no website listed`;
WhatsApp logs the batch start with the accounts in play and one line per number —
`Name · +44… → ON WhatsApp ✓` / `not on WhatsApp ✗` / `no number to check`. The CRM-driven
re-verify writes the same lines onto the mirrored local job so they reach the dialog too.
~1 line per lead per lane (job #14 ≈ 800 lines) — the CRM dialog paginates.

### W24 — WhatsApp daily cap removed (user directive)  [x]
`WA_DAILY_CAP` now defaults to **0 = no cap**; `pick_wa_account` rotates over every enabled
account regardless of `sent_today`, `verify_places` only reports `capped` when a cap is set.
Set `WA_DAILY_CAP=200` to restore the old per-account ceiling. Also: the CRM re-verify path
now ends its lane with a real reason (`wa_daily_cap` / `completed`) instead of leaving the
CRM to print "Interrupted · ran 0s" (job #14, T178).

### W23 — headed browser tier died after W22 ("Show window" showed nothing)  [x]
Job #14's follow-up re-enrich ran with the window toggle on and no browser appeared. Not
headless: real Chrome REFUSED to launch — Playwright rejects `device_scale_factor` together
with `no_viewport` ("deviceScaleFactor option is not supported with null viewport"), which
W22 combined for headed runs — and the bundled-Chromium fallback was patchright 1.62's
chromium-1234, never installed, so the tier vanished silently (`browser fallback
unavailable`). Fix: DPR 2 only with the headless fixed viewport; headed keeps the real
window's DPR. Also `python -m patchright install chromium` on the PC so the fallback exists.
Verified: `BrowserFetcher(headless=False).fetch()` opens a real window and reads the page.

### W22 — nodriver/Scrapling/Camoufox techniques ported  [x]
Four tiers touched, W15 proxy plumbing untouched and still inert.
**A. httpx** (`enrich.HEADERS`): the bare Chrome/126 UA + 2 headers became a full,
hand-written Chrome **150** identity matching curl_cffi's `chrome` alias (chrome150, macOS
UA): `sec-ch-ua` / `-mobile` / `-platform`, all four `Sec-Fetch-*`, `Upgrade-Insecure-Requests`,
`Referer: https://www.google.com/`, `Accept-Language: en-GB,en;q=0.9`, and an
`Accept-Encoding` that only advertises what httpx can decode here (`brotli` present → `br`;
`zstandard` absent → no `zstd`). Not browserforge — its Sec-Fetch output is wrong on this box.
**B. curl_cffi** (`impersonate_fetch.py`): same `Referer` + `Accept-Language` on top of the
alias's own headers; ONE retry after 1 s on `curl_cffi.curl.CurlError` (connection-level
only — a 403 is never retried).
**C. Browser** (`browser_fetch.py`, Scrapling's stealth ported): launch args =
Scrapling DEFAULT+STEALTH set **minus** `--disable-features=IsolateOrigins,site-per-process`
(nodriver: a non-default feature flag is itself fingerprintable) and minus the
window-position pair; `--lang=en-GB --accept-lang=en-GB,en` replace the context `locale`
(Cloudflare compares worker vs document language); `ignore_default_args` drops
`--enable-automation` + 4 others; the `AutomationControlled` blink flag is only added on
stock Playwright (patchright: redundant and a signal). Context: `color_scheme=dark`, DPR 2,
no touch/mobile, service workers allowed, geolocation+notifications granted, 1920×1080
screen+viewport when headless (headed keeps the real window), headless UA has "Headless"
stripped (learned on first launch, relaunched once, cached process-wide); headed real
Chrome keeps its own UA. Asset blocking is now by `request.resource_type`
(image/media/font; stylesheets kept). `CHALLENGE_WAIT_MS` 6 → 12 s. **Cloudflare
classification** (`detect_cloudflare`, port of Scrapling's `_detect_cloudflare`):
`non-interactive` / `managed` / `interactive` from the `cType: '…'` marker, `embedded` from a
Turnstile script tag. The class travels out as `fetch_ex() → (html, 'cf_managed' …)`,
`browser_retry` now returns a 3-tuple, `is_block` accepts `cf_*`/`blocked`, and the reason
lands in `enrich_error` so the CRM's issues dialog says WHICH wall remains. **Turnstile
click** (`ENRICH_CF_CLICK`, **default ON — user directive 2026-08-25**; `0`/`false` turns
it off): for `managed`/`interactive`, find the
`challenges.cloudflare.com/cdn-cgi/challenge-platform/` frame, press `(x+27, y+26)` with a
100–200 ms hold, wait network idle, poll ≤10 s, ≤3 attempts. The flag lives in
`config.Settings.enrich_cf_click`. First bench pass exposed a race — the cleared page
navigates and `page.content()` raises mid-swap — fixed with a tolerant read; the click had
actually worked (the profile held `cf_clearance` afterwards and the same sites loaded in 4 s).
**D. Camoufox** (`camoufox_fetch.py`, `ENRICH_BROWSER_CAMOUFOX=1`, default OFF): last tier
after Chrome fails on a block; `CamoufoxFetcher(BrowserFetcher)` on STOCK Playwright's
`firefox.launch_persistent_context` with `camoufox.utils.launch_options(humanize=False,
block_images=True, os=windows|macos)`; the opts dict is **frozen** to
`data/camoufox-profile/camoufox-opts.json` on first use and reused (a `cf_clearance` is
bound to the fingerprint), only `headless` re-applied. Lazy import; missing package = one
log line and the tier is skipped. `via='camoufox'`.

**Bench** (19 blocked URLs from job #7, headless, ladder via `crawl_site` +
`BrowserFetcher(headless=True)`, ≤30 s/site; baseline before W22 = **9/19**):

| run | readable | tls | browser | camoufox | left |
|---|---|---|---|---|---|
| 1. A+B+C, `ENRICH_CF_CLICK=0` | **12/19** | 9 | 3 | – | fullcarchecks `blocked` (1020 deny), addlestone / autocapital / truckandvanplus `cf_interactive`, yell + staiano `blocked`, jany.io `network` (flaky, OK in runs 2–3) |
| 2. click off + `ENRICH_BROWSER_CAMOUFOX=1` | **13/19** | 9 | 4 | **0** | same walls — Camoufox cleared nothing Chrome had not; the +1 is jany.io answering this time |
| 3. click ON (the new default), fresh profile | **15/19** | 9 | 6 | – | **all three `cf_interactive` sites cleared by the click** (35–53 s each incl. queueing); still `blocked`: fullcarchecks, yell, staiano; browningsgarage flaked `network` this pass |

Per-site (run 3): fullcarchecks FAIL blocked · varianse OK tls · sixt ×5 OK tls · jany.io OK
browser · truckandplant OK tls · addlestone OK browser · autocapital OK browser ·
browningsgarage FAIL network · tvcexports OK tls · global-commercials OK tls ·
truckandvanplus OK browser · dr-azmat OK browser · yell FAIL blocked · reliablemedicare OK
browser · staiano FAIL blocked. So: headers+stealth alone 9 → 12, the click adds +3 (the
whole `cf_interactive` band) and is now on by default, Camoufox is worth 0 on this set and
stays off. Never rescued by anything: a hard 1020 deny and two non-Cloudflare `blocked` pages.
Note the clearance cookie persists in `data/fetch-profile` — a site clicked once loads in
~4 s afterwards, so the per-site cost above is a first-visit cost.

**Deliberately NOT adopted:** nodriver as a library (its own CDP driver — a second browser
stack for no measured gain over patchright), Scrapling as a dependency (its fetchers drag in
browserforge + its own Playwright fork; the two techniques worth having are ~80 lines here),
Camoufox `humanize` (cursor animation, seconds per page, we never interact), `geoip`
(MaxMind download + only meaningful behind a proxy), browserforge headers (wrong Sec-Fetch
on this machine), any CAPTCHA-solving service, proxies (W15 stays inert).
Tests: `tests/test_w22_stealth.py` (+15: header set + UA/impersonate-target match,
`detect_cloudflare` ×4 + negatives, `is_block` on `cf_*`, launch-arg findings, `crawl_site`
carrying `cf_managed` → reason and camoufox → via, opts freeze + corrupt-cache regen,
CurlError retry-once / not-twice / never-on-403). 89 → **104 pass**. Docs: `.env.example`,
`CLAUDE.md` env table + layout.

---

## Session 2026-08-25 — closed-window crash + Mac agent (CRM T165)

### W21 — persisted Maps links + `discovery_pending` re-run (CRM T172)  [x]
New `job_links` table (every feed card, `opened` flag; saved at `links_done`, marked when a
place is attempted). `run_scrape(preset_links=)` skips search/centre/tiling and opens exactly
those; `server._discovery` uses it when `jobs.discovery_pending` (mirrored from the CRM) is
set, then clears the flag. `_local_progress` adds `links_offered/links_opened/skipped_known/
skipped_far`. Old jobs have no links → the CRM tells the user to use Find missed.

### W20 — stream UPDATED rows mid-job; caps are stops, not failures (CRM T171)  [x]
`places.changed_at` bumped by a trigger on every update; `Store.places_changed_since()`;
`_tick` streams changed rows (rowid ≤ synced watermark) before new ones, watermark
`_changed_upto` in memory. The CRM read "64 / 248" while 116 were enriched locally because
only NEW rows were streamed until the job ended. `eta._lane_state`: ok=0 → `failed` only for
`error:` reasons, `stopped` for maps_cap / wa_daily_cap / stopped. Test updated. 89 pass.

### W19 — store-derived lane counts + in-flight/queued per lane (CRM T170)  [x]
`eta.lanes()` no longer trusts the per-run counters on the job row when it has a store:
`_live_counts()` counts places (discovery done), `count_enriched` (enrichment done),
`count_wa_done` (WA done), `count_pending_enrichment` / `count_wa_pending` (queued) and the
new `jobs.enrich_active` / `jobs.wa_active` columns (batch in flight, set by the lanes
around each batch, cleared on exit). Each lane dict gains `active` + `pending`; `total`
= done + active + pending (discovery: max(links_found, done + pending)). Kills the
"6 / 237 beside 83 leads" and "1 / ≥ 1 beside 45 emails" readings after a resume. No
tests broke (89). Needs an agent restart to take effect.

### W18 — counters after an orphan re-queue  [x]
Agent restarted mid-job #14 (W17 rollout) → the Worker re-queued it, but `links_found` is
per-run while `scraped_count` is cumulative ("77 / 29"), and the enrichment lane set
`enrich_done=0` at start, hiding the interrupted run's work ("1 / ≥ 1" beside 45 emails).
`eta._at_least()` keeps every lane's total ≥ done; `Store.count_enriched()` (scope-aware,
website-only, `enrich_status<>'pending'`) seeds the lane's `seen`. Fresh jobs and scoped
re-enrich runs are unchanged (both start at 0). Rule learnt: never stop/start the agent
while a job is running — check `data/agent.log` first.

### W17 — self-check + one-click installers (CRM T168)  [x]
`webscraper/healthcheck.py`: 11 cheap checks (python, crm_token, playwright, chromium,
real_chrome*, curl_cffi*, patchright*, wa_session*, ai_keys*, disk, autostart; * = optional,
degrades a lane, not the agent) → `{ok, os, version, git, python, checks{name:{ok,detail,fix}}}`.
`CrmCloud.jobs()` attaches it every `CHECKS_EVERY_SEC`=300 s; the CRM stores it in
`lead_gen_agents.checks` and renders the Setup tab "Agent health" panel. `python -m webscraper
doctor` prints the same report (exit 1 when a required check fails). `scripts/install-agent.sh`
(mac/linux) + `scripts/install-agent.ps1` (windows): idempotent full install — tools via
brew/winget when missing, clone/pull to `~/hvt-lead-finder-agent`, venv, deps, playwright
chromium, `.env` CRM_AGENT_TOKEN + LEAD_FINDER_DEVICE, autostart, start, doctor. The CRM's
"Install agent on this computer" button mints a token and downloads a `.command`/`.bat` that
curls these from `raw.githubusercontent.com/.../main/scripts/` — **needs the repo public**.
89 tests still pass.

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
