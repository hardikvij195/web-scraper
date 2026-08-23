# web-scraper — Google Maps lead scraper + contact enricher

Slow, polite, self-hosted. Pulls businesses off Google Maps for a keyword + location, then
crawls each business's own website for **email, Instagram, Facebook, LinkedIn, X, YouTube,
TikTok and a WhatsApp number**. Stores everything in SQLite, exports CSV/JSON. No Apify, no
proxies needed from a home IP at the default pace (~6 s per place).

## Fields per lead

| From Google Maps | From the business website |
|---|---|
| name, category, address, phone (E.164), website, rating, lat/lng, plus code, place_id, maps URL, WhatsApp button link (if Maps shows one) | email(s), instagram, facebook, linkedin, twitter_x, youtube, tiktok, whatsapp (`wa.me` / `api.whatsapp.com` links) |
| `reviews_count`, `price_range` — **only with `--no-headless`** (Google serves a lite panel to headless browsers) | `whatsapp_source` tells you how sure we are: `verified` (we checked it on WhatsApp) > `maps_link` > `wa_link` > `unverified` (a plain phone number — **not** a claim it is on WhatsApp, and it carries no tag in the UI) > `none` |

## Setup (Windows / any OS with Python 3.11+)

```bash
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env        # optional; defaults are fine
```

## Web UI (easiest)

```bash
python -m webscraper serve          # opens http://127.0.0.1:8765 in your browser
```

Enter keyword + location + max places (or **unlimited**) → **Start scraping**. Options:
**Radius (km)** keeps only places within that distance of the location (the circle is tiled
into ~2 km sub-searches when you ask for more than one Maps page, so unlimited + radius can
reach thousands); **Run window** (e.g. 01:00–07:00) makes the job wait/pause outside those
local hours; **Phone country** defaults to auto-detect from each lead's address. The page shows live progress
(places found / scraped, websites enriched), the leads table fills in as it runs (phone,
WhatsApp, email, website, IG/FB/LI/X links, address, rating), with filters, and **⬇ Excel**
/ CSV / JSON downloads per job. Jobs run one at a time on this machine in the background;
closing the tab doesn't stop them — use **Stop**. Restarting the server re-queues an
interrupted job. Check "Show browser window" to also get review counts and price range.

## CLI

```bash
# scrape -> enrich -> export in one go
python -m webscraper run "dentist" --location "Pune" --max 200 --format csv

# or step by step
python -m webscraper scrape "interior designer" -l "Koregaon Park, Pune" -n 100 --delay 6
python -m webscraper enrich --job 3            # crawl websites for emails/socials/WhatsApp
python -m webscraper export --job 3 -f json    # -> data/exports/
python -m webscraper jobs                      # list jobs
python -m webscraper stats                     # coverage % across everything
```

Flags: `--delay` seconds between place visits (default 6, jitter ±40%), `--no-headless` to
watch the browser **and** get review counts / price range, `--country` for phone parsing
(default IN), `--concurrency` for the website crawl (default 5 — that hits each business's
own site, not Google, so it's safe to raise).

Ctrl-C is safe: progress is saved per place; `enrich`/`export` work on a stopped job.

## Pacing / safety

- One persistent Chromium profile, one tab, images/fonts blocked.
- Random 0.6–1.4× jitter on every delay; a 45 s pause every 50 places.
- On a Google captcha it backs off 15–30 min once, then stops the job (`status=stopped`).
- Rule of thumb from a home IP: ≤ ~3k places/day at 6 s is comfortable. Datacenter IPs need
  a residential proxy (`MAPS_PROXY=socks5://…`) for anything beyond light use.

## Data

SQLite at `data/leads.db` — `jobs` + `places` (PK `job_id, place_key`; `place_key` =
Google `place_id` when known). Exports land in `data/exports/`. `data/` is gitignored.

## Known limits

- Review count + price range need headed mode (see above). Everything else works headless.
- Sites that are down / geo-blocked / JS-only show `enrich_status = failed | thin`.
- Google changes its DOM occasionally; selectors use `data-item-id` / aria-label hooks and
  the results-feed structure, which have been stable for years — but expect to patch someday.
- Scraping Google Maps is against Google's ToS (public business data; same route every
  lead-gen platform uses). Keep it slow and don't resell raw Google content.

## Next (planned)

Push results into HVT CRM (`lead_gen_results` -> Leads import UI), wallet debit, move the
same container to the VPS with CPU caps. See `CLAUDE.md`.

## Run as a cloud member (Lead Finder Cloud)

1. Sign in at https://web-scraper-leads.vercel.app (ask the admin for an account).
2. Settings tab → **Agent tokens → New token** — copy the `wsk_…` token (shown once).
3. On your PC (this repo, deps installed): `python -m webscraper agent --token wsk_…`
4. Create jobs in the browser (Scraper tab). Your agent picks them up, scrapes locally and
   syncs results; verified leads (phone or email found) use 1 credit each and are POSTed to
   your webhook (Settings) with an `X-Signature` HMAC header.
5. Buy credits in the Billing tab (Razorpay or PayU).
