# web-scraper — Claude guide

Python 3.13 + Playwright Google Maps lead scraper + httpx website enricher. Lives in
`Hv Technologies\web-scraper`, own git repo. Built 2026-08-20 as the local spike for the
"Lead Finder" idea (replaces Apify; runs on the user's PC first, VPS later). Read `README.md`
for usage; this file is for working on the code.

## Layout

```
webscraper/
  cli.py         typer CLI: scrape | enrich | export | run | jobs | stats | serve
  server.py      FastAPI local UI (:8765) + single Worker thread; jobs.phase drives progress;
                 routes /api/jobs (POST/GET), /api/jobs/{id}[/places|/stop|/export?fmt=xlsx|csv|json]
  static/index.html  vanilla-JS single page: form -> jobs list -> progress bars -> leads table -> downloads
  maps.py        Playwright: search URL -> scroll feed (FeedCard) -> per-place panel -> Place
  enrich.py      async httpx: home + contact/about pages -> emails, socials, WhatsApp
  extractors.py  pure functions (regex/selectolax) — the only part with unit tests
  store.py       sqlite3 (jobs, places), stats, CSV/JSON export, tiny ALTER-TABLE migrate
  models.py      Place / Contacts dataclasses
  config.py      .env -> Settings (delay, pauses, headless, proxy, country, paths)
tests/test_extractors.py
data/            gitignored: leads.db, browser-profile/, exports/
```

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
  > `assumed_mobile` (phonenumbers says MOBILE) > `none`.
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

## Rules

- Keep it slow by default (`SCRAPE_DELAY_SEC=6`). Never add concurrency to the Maps step
  without a proxy pool; the enricher is the place for parallelism.
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
