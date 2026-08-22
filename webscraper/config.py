"""Runtime settings. `.env` (via python-dotenv) sets defaults; CLI flags override per run."""
from __future__ import annotations

import os
from dataclasses import dataclass
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


@dataclass
class Settings:
    delay_sec: float = _float(os.getenv("SCRAPE_DELAY_SEC"), 6.0)
    pause_every: int = _int(os.getenv("SCRAPE_PAUSE_EVERY"), 50)
    pause_sec: float = _float(os.getenv("SCRAPE_PAUSE_SEC"), 45.0)
    default_country: str = (os.getenv("DEFAULT_COUNTRY") or "IN").upper()
    headless: bool = _bool(os.getenv("HEADLESS"), True)
    maps_proxy: str | None = os.getenv("MAPS_PROXY") or None
    enrich_concurrency: int = _int(os.getenv("ENRICH_CONCURRENCY"), 5)
    db_path: Path = ROOT / (os.getenv("DB_PATH") or "data/leads.db")
    profile_dir: Path = ROOT / "data" / "browser-profile"
    export_dir: Path = ROOT / "data" / "exports"
    # WhatsApp number verification (opt-in per job). Each account = one persistent
    # browser profile under wa_profiles/<name>/. Cap + throttle guard the account.
    wa_profiles_dir: Path = ROOT / "data" / "wa-profiles"
    wa_daily_cap: int = _int(os.getenv("WA_DAILY_CAP"), 200)          # per account, per day
    wa_delay_min: float = _float(os.getenv("WA_VERIFY_DELAY_MIN"), 8.0)
    wa_delay_max: float = _float(os.getenv("WA_VERIFY_DELAY_MAX"), 20.0)
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
