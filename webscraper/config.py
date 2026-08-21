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


settings = Settings()
