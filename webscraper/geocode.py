"""Independent geocoding of a job's LOCATION string (W117 / CRM T759).

`maps.py`'s `resolve_center()` guesses the search radius's centre from the MEDIAN of the
first Maps results — fine normally, but for a tiny UK place ("Sandwick, United Kingdom")
Maps sometimes finds nothing there and serves results near the searcher instead (the
scraping machines are in Noida/Ghaziabad, India). The median then drifts to ~28.6N 77.3E
and the "outside radius -> far" guard never fires because the radius CENTRE drifted with
the results. `geocode_location()` gives an independent answer ("where is this place,
really?") from OpenStreetMap Nominatim, used as a cross-check / fallback centre and as a
per-place sanity guard even when the job has no radius at all.

One call per job, never raises (network failure -> None, the job continues exactly as
before this feature existed), in-process LRU cache."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import httpx

log = logging.getLogger("webscraper.geocode")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
#: Nominatim's usage policy (https://operations.osmfoundation.org/policies/nominatim/)
#: requires a descriptive UA identifying the application — NOT a browser UA. One call per
#: job, well under their ~1 req/s guidance, so no owner-side approval / API key is needed.
USER_AGENT = "hvt-lead-finder/1.0 (web-scraper job geocoder; contact: as_dev_team@appsynergies.com)"
TIMEOUT_SEC = 10.0


@dataclass
class GeoHit:
    lat: float
    lng: float
    country_code: str | None      # ISO 3166-1 alpha-2, lowercase (Nominatim's convention)
    display_name: str | None


def parse_nominatim_response(data: Any) -> GeoHit | None:
    """Pure parse of a Nominatim `jsonv2` response (a JSON list). No network here — this is
    the tested part; `geocode_location()` just fetches and hands off to this."""
    if not isinstance(data, list) or not data:
        return None
    hit = data[0]
    try:
        lat = float(hit["lat"])
        lng = float(hit["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    addr = hit.get("address") or {}
    cc = addr.get("country_code")
    return GeoHit(lat=lat, lng=lng,
                  country_code=str(cc).lower() if cc else None,
                  display_name=hit.get("display_name"))


@lru_cache(maxsize=512)
def _geocode_cached(location: str) -> GeoHit | None:
    try:
        resp = httpx.get(
            NOMINATIM_URL,
            params={"format": "jsonv2", "limit": 1, "addressdetails": 1, "q": location},
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT_SEC,
        )
        resp.raise_for_status()
        return parse_nominatim_response(resp.json())
    except Exception:                                     # noqa: BLE001 — never break the job over this
        log.warning("geocode_location failed for %r", location, exc_info=True)
        return None


def geocode_location(location: str | None) -> GeoHit | None:
    """Where is `location`, independent of Google Maps. `None` on any failure (bad
    location, network error, no result) — callers must treat that exactly like "we don't
    know" and fall back to today's behaviour."""
    loc = (location or "").strip()
    if not loc:
        return None
    return _geocode_cached(loc)
