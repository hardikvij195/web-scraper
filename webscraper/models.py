from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Place:
    """One Google Maps listing, plus whatever enrichment found on its website."""

    job_id: int
    place_key: str                       # stable per-listing key (place_id if known, else cid/href hash)
    name: str | None = None
    category: str | None = None
    address: str | None = None
    country: str | None = None           # ISO region guessed from the address (or the job's country)
    phone: str | None = None
    phone_digits: str | None = None
    website: str | None = None
    domain: str | None = None
    rating: float | None = None
    reviews_count: int | None = None     # only visible in the full (headed) Maps layout
    price_range: str | None = None       # e.g. "₹200–400" — same caveat
    lat: float | None = None
    lng: float | None = None
    distance_km: float | None = None     # from the job's centre, when the job has one
    maps_url: str | None = None
    plus_code: str | None = None
    place_id: str | None = None
    # enrichment
    email: str | None = None
    emails: list[str] = field(default_factory=list)
    instagram: str | None = None
    facebook: str | None = None
    linkedin: str | None = None
    twitter_x: str | None = None
    youtube: str | None = None
    tiktok: str | None = None
    whatsapp_number: str | None = None
    whatsapp_source: str | None = None   # maps_link | wa_link | verified | unverified | none
    enrich_status: str = "pending"       # pending | done | no_website | failed | thin
    enriched_at: str | None = None
    scraped_at: str | None = None
    summary: str | None = None           # AI research (opt-in)
    owner: str | None = None
    team: list = field(default_factory=list)   # [{name, role, phone, email}]
    research_status: str | None = None   # pending | done | failed | no_website | no_key
    researched_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Contacts:
    """Result of crawling one business website."""

    emails: list[str] = field(default_factory=list)
    instagram: str | None = None
    facebook: str | None = None
    linkedin: str | None = None
    twitter_x: str | None = None
    youtube: str | None = None
    tiktok: str | None = None
    whatsapp_number: str | None = None
    pages_fetched: int = 0
    thin: bool = False                   # pages had almost no HTML/links (likely JS-only site)
