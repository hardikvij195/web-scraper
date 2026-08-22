# TASKS — numbered work log (web-scraper)

`[x]` done · `[~]` in progress · `[ ]` todo. Newest on top.

> This repo has **no git remote** — commits exist only on this PC. Cross-repo backlog
> index: `../hvt-ai-crm-live/tasks.md`. The items below are the agent half of its **T136**;
> the CRM half (UI, ETA display, job phases in the DB) lives there.
>
> ⚠ **Four source files are uncommitted right now** (`agent.py`, `maps.py`, `server.py`,
> `store.py`, +91/-7). They are not scratch — they contain **W0 below, already working**, and
> nothing describes them anywhere else. Read `git diff` before editing those files.

---

## Session 2026-08-22

### W1 — Push this repo to GitHub  [ ]
Commits since 2026-08-21 (HEAD `4e00a3a`) exist nowhere but this disk.

### W0 — Time-budget split + live streaming  [~] **uncommitted**
Found on disk, working, undocumented — written before this log existed. Commit it.
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

### W2 — Second phase: enrichment after discovery  [ ]
Prompt (2026-08-22): "if the max time is 30 mins => search leads on google maps for 30 mins
and then stop that and start research on leads website and linkedin, insta, fb, whatsapp
numbers, summary".
W0 already splits the budget **inside** Maps (collect links → scrape places). What the
prompt asks for is a further phase **after** Maps: once the cap is reached, stop scraping
and research what was found — website, LinkedIn, Instagram, Facebook, WhatsApp number, AI
summary. `enrich.py` already resolves all of those; nothing triggers it as a phase.
So the run becomes three stages against one budget: collect → scrape → enrich.
Settle first: does the 30 minutes cover enrichment too, or is it the Maps cap with
enrichment running after it? Read literally it is the Maps cap.

### W3 — Stream each lead into the job as it is found  [~] **mostly done in W0**
Prompt: "as soon as u find a lead => pass it into lead finder job".
Delivered by the uncommitted `places_after` + `synced_upto` work in W0: places reach the
CRM as they are scraped, so a job killed mid-run keeps everything it had. Upserts are keyed
on (job_id, place_key) both locally and CRM-side, so re-sending is harmless.
Left to do: `synced_upto` is **in memory only**, so a restarted agent re-sends one job's
rows once (absorbed by the upsert, but wasteful), and the CRM UI does not yet refresh
mid-job to show them arriving.

### W4 — Estimated time on every task  [ ]
Prompt: "always show an estimated time for all tasks".
The agent needs to emit an ETA the CRM can render: for discovery from the tile count and
observed seconds-per-place, for enrichment from the number of leads times the observed
per-lead cost. Needs a rolling average from past jobs to be worth anything on the first
tick, and it should say "estimating" rather than print a wrong number early.
