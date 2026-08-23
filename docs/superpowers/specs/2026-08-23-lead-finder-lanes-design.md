# Lead Finder — three concurrent lanes, per-lane telemetry, job logs

**Date:** 2026-08-23 · **Repos:** `web-scraper` (agent) + `hvt-ai-crm-live` (UI/DB)
**Tracks:** web-scraper **W5–W10**, CRM **T141**

## Why

Today `Worker.run()` (`server.py:153`) runs four phases strictly in sequence against two
budgets: Maps until `maps_deadline_at`, then enrichment + AI research + WhatsApp sharing a
second budget (`enrich_budget_frac`, default 0.5 of `max_minutes`).

Two things fall out of that, both visible in live job #6 (Greater London, `max 5 min`):

| Lane | Shown | Reality |
|---|---|---|
| Discovery | 33 / 79 places | 79 links collected, 33 opened before the Maps cap |
| Enrichment | 33 / 33 sites | all discovered leads crawled |
| AI research | 30 / 30 | only 30 had a website |
| WhatsApp | 2 / 30 numbers — **"done"** | **not done.** The shared post-Maps budget (~2.5 min) was spent by enrichment + AI; `budget_up()` went true after 2 numbers and the lane exited, then got labelled `done` like the others |

1. **WhatsApp always loses the race.** It is last in line and the slowest by design
   (randomised human pacing), so on any short job it gets whatever is left, which is
   nothing. Running the lanes concurrently removes the race entirely.
2. **A lane that gave up is indistinguishable from a lane that finished.** There is no
   per-lane end reason, no per-lane runtime, and no success flag — `phase` is a single
   column holding whichever phase was last.

Separately, job #6's 9 `failed` enrichments were reproduced live: **8 are HTTP 403
WAF/bot-blocks** (`lookers.co.uk`, `hrowen.co.uk` ×3, `carluv.co.uk`,
`mayfairmotorsolutions.com`, `maryleboneminicabs.co.uk`, `luxurycarsltd.co.uk`) and 1 is a
dead domain (`atypesourcing.com`, `getaddrinfo failed`). `enrich_status="failed"` is set
whenever `pages_fetched == 0` and **the reason is never recorded**, so a quarter of a London
job looks arbitrarily broken.

## Decisions (agreed with Hardik 2026-08-23)

| Question | Decision |
|---|---|
| "3 windows" | **Both** — three real windows on the PC *and* three lanes in the CRM UI |
| Enrichment's window | httpx stays (browser would be ~15× slower per site); its window is a **live log pane**, not a browser |
| `max_minutes` | **Discovery cap only.** Enrichment + WhatsApp drain the backlog after it |
| AI research | **Folded into the enrichment lane** — a lead goes to WhatsApp only once site+socials *and* summary are done |
| `assumed_mobile` | **Removed.** Never assume a WhatsApp; verify it, and only tag a real link |
| WA numbers | Always rendered with a leading `+` |

## Architecture

### Lanes, and why the DB is the queue

```
Lane A  discovery    Maps Chrome, single-threaded (the proxy-pool rule stands)
   │  writes places rows, enrich_status='pending'
   ▼
Lane B  enrichment   httpx site+socials → AI summary, per lead
   │  writes enrich_status + research_status
   ▼
Lane C  whatsapp     WhatsApp Web Chrome, existing pacing + daily cap
```

Lanes do **not** hand each other Python objects. Each lane polls the `places` table for rows
in its own input state:

- B takes `enrich_status='pending'`, in batches of ≤10 (`enrich_places()` gets its speed from
  concurrency, so one-at-a-time would be a large regression).
- C takes rows whose enrichment has resolved, that carry a number, and whose `wa_verified` is
  undecided.

This was chosen over an in-memory `queue.Queue` because it is far less invasive to `maps.py`
and because **it makes every lane restart-safe for free**: a lane that dies mid-run resumes
from table state, which is exactly what the supervisor restart already relies on.

Each lane owns its **own `Store`** (`sqlite3` connections are not thread-safe). The DB moves
to WAL with a busy timeout. Lanes write **disjoint columns**, so there is no lost-update
hazard — this is the property that makes the design safe, and it must be preserved by anyone
adding a counter later.

### Termination

`a_done` / `b_done` are `threading.Event`s. B exits when `a_done` is set **and** no pending
rows remain; C exits when `b_done` is set and no candidates remain. The job is finished when
all three threads join. Stop still cuts all three promptly.

### Failure isolation

Every lane runs inside its own guard. A lane that raises records `ok=0` plus the exception
and sets its done-event so downstream lanes drain rather than hang. **WhatsApp dying no
longer costs the enrichment that already ran.**

### Crash recovery on all three lanes

`maps.py` already relaunches a dead browser and retries the tile/place (`a544ae7`). That
logic moves to `webscraper/browser_recovery.py` and is used by lane C too, so a killed
WhatsApp Chrome relaunches and retries that one number instead of taking the lane down.
Enrichment has no browser except the 403 retry below, which uses the same helper.

## Per-lane telemetry

New `jobs` columns, three per lane, written only by that lane:

```
disc_ended_at  disc_ok  disc_reason
enr_ended_at   enr_ok   enr_reason
wa_ended_at    wa_ok    wa_reason
```

Existing `scrape_started_at` / `enrich_started_at` / `wa_started_at` are the lane starts, so
runtime is `ended - started`. `*_reason` is a short machine token rendered to prose by the UI:

| Reason | Means |
|---|---|
| `completed` | ran out of work — the honest "done" |
| `maps_cap` | discovery hit `max_minutes` |
| `stopped` | user pressed Stop |
| `wa_daily_cap` | per-account WhatsApp cap reached; re-run to finish |
| `wa_not_logged_in` | no live WhatsApp Web session |
| `no_targets` | nothing qualified for this lane |
| `error:<detail>` | the lane raised |

`ok` is 1 only for `completed` / `no_targets`. This is what kills the "2 / 30 · done" lie:
that lane now reports its real reason with its real runtime.

## Job logs

New `job_logs` table (`id, job_id, ts, lane, level, message`), written through
`store.log(job_id, lane, level, msg)`. Every lane event that today goes only to
`data/agent.log` also lands here. The agent ships new rows to the CRM on each progress tick
(watermark = last synced `id`), and the CRM renders them in a **Logs** dialog per job.

`jobs.message` stays as the single latest-line field the progress bar uses; logs are the
history behind it.

## Enrichment: record the reason, and beat the 403

`places.enrich_error` records why a crawl produced nothing: `http_403`, `http_<code>`, `dns`,
`timeout`, `non_html`, `no_pages`.

When httpx gets 403 (or any block-shaped failure), the site is retried once through a real
Chromium page (`browser_fetch.py`, same recovery helper). This is the fast-path/slow-path
split — httpx for the ones that answer plainly, a browser only for the ones that refuse — and
it is what turns job #6's 9 failures into mostly-recovered leads.

## WhatsApp source, and the `+`

`whatsapp_source` values become:

| Value | Tag shown | Meaning |
|---|---|---|
| `maps_link` | **WA link** | Maps panel exposed a `wa.me` / `wa.link` |
| `wa_link` | **WA link** | the site exposed one |
| `verified` | **Verified** | we checked and WhatsApp accepted the number |
| `unverified` | *(no tag)* | a plain phone number we have not confirmed |
| *(null)* | — | none |

`assumed_mobile` is gone: existing rows migrate to `unverified`, and **an unverified number
never renders a tag**. On a verified miss the number is cleared, as today.

Numbers are stored and displayed E.164 **with the leading `+`**; `wa.me/` links strip it at
render time, since that URL form wants bare digits.

## Testing

- Unit: lane termination (A closes → B drains → C drains), lane guard records `ok=0` +
  reason, reason→prose mapping, `+` formatting, `enrich_error` classification. These are pure
  logic and go beside the existing `tests/test_extractors.py`.
- Integration: a real short job with all three lanes on, asserting the three lanes overlap in
  time (start/end stamps interleave) rather than run in series — that is the whole point of
  the change and the one thing a unit test cannot show.
- Live: re-run job #6's query and confirm the previously-403 domains now enrich.

## Out of scope

Proxy pool for Maps concurrency; a fourth lane for AI research; changing WhatsApp pacing or
the daily cap.
