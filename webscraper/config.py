"""Runtime settings. `.env` (via python-dotenv) sets defaults; CLI flags override per run."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(v: str | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _float(v: str | None, default: float) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


def _int(v: str | None, default: int) -> int:
    try:
        return int(v) if v not in (None, "") else default
    except ValueError:
        return default


def _proxy_list(raw: str | None) -> list[str]:
    # Local import: proxies.py must stay importable without config (and vice versa).
    from webscraper.proxies import parse_proxy_list
    return parse_proxy_list(raw)


@dataclass
class Settings:
    delay_sec: float = _float(os.getenv("SCRAPE_DELAY_SEC"), 6.0)
    pause_every: int = _int(os.getenv("SCRAPE_PAUSE_EVERY"), 50)
    pause_sec: float = _float(os.getenv("SCRAPE_PAUSE_SEC"), 45.0)
    default_country: str = (os.getenv("DEFAULT_COUNTRY") or "IN").upper()
    headless: bool = _bool(os.getenv("HEADLESS"), True)
    maps_proxy: str | None = os.getenv("MAPS_PROXY") or None
    # Residential/rotating proxy for the ENRICHMENT fetch paths (curl_cffi + the browser
    # fallback). Off by default — set ENRICH_PROXY to a full URL, e.g.
    # http://user:pass@gw.provider.com:7777 . This is the tier that beats IP-reputation
    # blocks on a VPS (datacenter IP). On the user's own PC the home IP already looks
    # residential, so this stays inert there; nothing changes without it.
    enrich_proxy: str | None = os.getenv("ENRICH_PROXY") or None
    # W15: a LIST of proxies (comma/newline-separated, `user:pass@host:port` ok) rotated
    # round-robin with per-proxy quarantine (see proxies.py). Supersedes ENRICH_PROXY when
    # set; with only ENRICH_PROXY the pool holds that one URL and behaves as before.
    enrich_proxies: list[str] = field(default_factory=lambda: _proxy_list(os.getenv("ENRICH_PROXIES")))
    # Try a proxy BEFORE the direct (own-IP) attempt. Default off: on the user's PC the home
    # IP is the best residential identity there is; on a VPS set this to 1.
    enrich_proxy_first: bool = _bool(os.getenv("ENRICH_PROXY_FIRST"), False)
    # Consecutive proxy-blamed failures (407 / connect error) before a proxy is benched, and
    # for how long, in seconds, before it is offered again.
    enrich_proxy_max_failures: int = _int(os.getenv("ENRICH_PROXY_MAX_FAILURES"), 3)
    enrich_proxy_cooldown_sec: float = _float(os.getenv("ENRICH_PROXY_COOLDOWN_SEC"), 300.0)
    # W22: press Cloudflare's managed/interactive Turnstile checkbox in the browser tier.
    # Default ON — user directive 2026-08-25 (bench: +3/19 sites). `0`/`false` turns it off,
    # in which case the wall is only classified (enrich_error = cf_managed / cf_interactive).
    enrich_cf_click: bool = _bool(os.getenv("ENRICH_CF_CLICK"), True)
    enrich_concurrency: int = _int(os.getenv("ENRICH_CONCURRENCY"), 5)
    db_path: Path = ROOT / (os.getenv("DB_PATH") or "data/leads.db")
    profile_dir: Path = ROOT / "data" / "browser-profile"
    export_dir: Path = ROOT / "data" / "exports"
    # WhatsApp number verification (opt-in per job). Each account = one persistent
    # browser profile under wa_profiles/<name>/. Cap + throttle guard the account.
    wa_profiles_dir: Path = ROOT / "data" / "wa-profiles"
    # 0 = NO cap (user directive 2026-08-25: "remove daily cap logic from wa verify").
    # Set a number to restore the per-account per-day ceiling.
    wa_daily_cap: int = _int(os.getenv("WA_DAILY_CAP"), 0)
    # Pause between one number and the next. 8-20s (avg ~14s) allowed ~250
    # numbers an hour; 3-8s (avg ~5.5s) is roughly 2.5x that. Still random and
    # still seconds apart on purpose: what gets an account challenged is a
    # fixed, machine-looking cadence more than the raw rate. If challenges do
    # start, raise both — that signal costs a logged-in session to ignore.
    wa_delay_min: float = _float(os.getenv("WA_VERIFY_DELAY_MIN"), 3.0)
    wa_delay_max: float = _float(os.getenv("WA_VERIFY_DELAY_MAX"), 8.0)
    # Headed by default: WhatsApp Web treats a headless Chromium as a NEW device and
    # shows the QR again (session doesn't carry over), so headless verify sees every
    # account as logged-out. On a headless VPS set WA_VERIFY_HEADLESS=true + run under
    # xvfb, or keep a headed display.
    wa_verify_headless: bool = _bool(os.getenv("WA_VERIFY_HEADLESS"), False)
    # ── job time budget: how a job's `max_minutes` is divided between phases ──────
    # Read the user's ask literally — "if the max time is 30 mins => search leads on
    # google maps for 30 mins and then stop that and start research on leads website
    # and linkedin, insta, fb, whatsapp numbers, summary". So `max_minutes` is the
    # **Google Maps cap**, and the research/enrichment phases run *after* it on a
    # budget of their own. A 30-minute job therefore spends up to 30 min on Maps and
    # up to another 15 min (0.5x) enriching what it found.
    #
    # Both halves are configurable so the same code expresses the other reading:
    # MAPS_BUDGET_FRAC=0.7 + ENRICH_BUDGET_FRAC=0.3 makes the 30 minutes cover the
    # whole run (21 min Maps, 9 min enrichment).
    maps_budget_frac: float = _float(os.getenv("MAPS_BUDGET_FRAC"), 1.0)
    # Of `max_minutes`. 0 = the post-Maps phases are not time-capped at all.
    enrich_budget_frac: float = _float(os.getenv("ENRICH_BUDGET_FRAC"), 0.5)
    # Of the *Maps* budget — the W0 40/60 split between collecting links and opening
    # each place. A wide radius tiles into thousands of sub-searches, so without this
    # the collection step burns the whole cap and the job scrapes zero places.
    collect_budget_frac: float = _float(os.getenv("COLLECT_BUDGET_FRAC"), 0.4)
    # How many past runs of a phase feed its rolling seconds-per-unit average (W4).
    eta_history_jobs: int = _int(os.getenv("ETA_HISTORY_JOBS"), 20)


settings = Settings()
