# TASKS — numbered work log (web-scraper)

`[x]` done · `[~]` in progress · `[ ]` todo. Newest on top.

> **State 2026-08-23: W0-W4 are all done.** Remote is `hardikvij195/web-scraper`, HEAD
> `a544ae7`, tree clean, 0 unpushed. Cross-repo backlog index:
> `../hvt-ai-crm-live/tasks.md` — the items below are the agent half of its **T136**
> (closed); the CRM half (UI, ETA display, job phases in the DB) lives there.
>
> Two known leftovers, neither blocking: `synced_upto` is **in-memory only**, so a restarted
> agent re-sends one job's rows once (absorbed by the `(job_id, place_key)` upsert, just
> wasteful), and the CRM UI does not refresh mid-job to show leads arriving.

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
