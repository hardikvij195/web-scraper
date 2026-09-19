"""Google Maps scraper (Playwright, one persistent Chromium profile, deliberately slow).

Flow: search URL → scroll the results feed collecting place links → visit each place page →
read fields from the side panel. Pacing is configurable; defaults are safe for a home IP.
Selectors use Google's `data-item-id` / aria-label hooks which have been stable for years;
class names are avoided. If Google serves a captcha we back off instead of hammering.
"""
from __future__ import annotations

import hashlib
import logging
import os
import queue
import random
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus

from playwright.sync_api import Error as PWError, Page, TimeoutError as PWTimeout, sync_playwright

from webscraper.config import settings
from webscraper.extractors import (
    clean_url, country_from_address, domain_of, extract_whatsapp, normalise_phone, normalise_wa,
    region_of_phone,
)
from webscraper.geocode import GeoHit, geocode_location
from webscraper.models import Place
from webscraper.browser_recovery import (RESTORE_BUBBLE_ARGS, Relauncher, close_blank_pages,
                                         is_closed, mark_profile_clean)
from webscraper.store import Store, now_iso

log = logging.getLogger("webscraper.maps")

END_OF_LIST_RE = re.compile(r"reached the end of the list", re.I)
LATLNG_3D4D = re.compile(r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)")
LATLNG_AT = re.compile(r"/@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")
PLACE_ID_RE = re.compile(r"!19s(ChIJ[A-Za-z0-9_\-]+)")
CID_RE = re.compile(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", re.I)
RATING_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*stars?", re.I)
REVIEWS_RE = re.compile(r"([\d,\.]+)\s*reviews?", re.I)
# "4.2 (5,647)" as rendered in the full place panel header
PANEL_RATING_REVIEWS_RE = re.compile(r"(\d\.\d)\s*\(\s*([\d,\.]+)\s*\)")
# "₹200–400" / "$$" / "₹1,000+" right after the review count
PRICE_RANGE_RE = re.compile(r"\)\s*·\s*([₹$€£]\s?[\d,]+(?:\s?[–-]\s?[₹$€£]?\s?[\d,]+)?\+?|[₹$€£]{1,4})(?=\s)")


class CaptchaError(RuntimeError):
    pass


@dataclass
class Pacing:
    delay_sec: float = settings.delay_sec
    pause_every: int = settings.pause_every
    pause_sec: float = settings.pause_sec

    def sleep_between(self) -> None:
        time.sleep(self.delay_sec * random.uniform(0.6, 1.4))

    def maybe_long_pause(self, n: int) -> None:
        if self.pause_every and n and n % self.pause_every == 0:
            t = self.pause_sec * random.uniform(0.7, 1.3)
            log.info("long pause %.0fs after %d places", t, n)
            time.sleep(t)


def _blocked(page: Page) -> bool:
    if "/sorry/" in page.url:
        return True
    try:
        body = page.locator("body").inner_text(timeout=1500)
    except PWTimeout:
        return False
    low = body.lower()
    return "unusual traffic" in low or "i'm not a robot" in low


def _accept_consent(page: Page) -> None:
    """EU-style consent interstitial; no-op elsewhere."""
    try:
        if "consent.google" in page.url:
            for sel in ('button:has-text("Accept all")', 'button:has-text("I agree")',
                        'form[action*="consent"] button', 'button[aria-label*="Accept"]'):
                btn = page.locator(sel).first
                if btn.count():
                    btn.click(timeout=3000)
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                    return
    except PWTimeout:
        pass


def _attr(page: Page, selector: str, attr: str) -> str | None:
    try:
        loc = page.locator(selector).first
        if loc.count():
            return loc.get_attribute(attr, timeout=1500)
    except PWTimeout:
        pass
    return None


def _text(page: Page, selector: str) -> str | None:
    try:
        loc = page.locator(selector).first
        if loc.count():
            t = loc.inner_text(timeout=1500).strip()
            return t or None
    except PWTimeout:
        pass
    return None


def _aria_value(page: Page, selector: str, prefix: str) -> str | None:
    v = _attr(page, selector, "aria-label")
    if v and v.lower().startswith(prefix.lower()):
        v = v[len(prefix):]
    return v.strip(" :") if v else None


def search_url(query: str, location: str | None, lang: str = "en",
               center: tuple[float, float] | None = None, zoom: float | None = None) -> str:
    """Maps search URL. With `center`+`zoom` the results are biased to that viewport, which is
    how a radius is expressed to Maps (there is no radius parameter)."""
    q = f"{query} in {location}" if (location and not center) else query
    if center and zoom:
        return f"https://www.google.com/maps/search/{quote_plus(q)}/@{center[0]:.6f},{center[1]:.6f},{zoom:.1f}z?hl={lang}"
    return f"https://www.google.com/maps/search/{quote_plus(q)}/?hl={lang}"


def zoom_for_radius_km(radius_km: float) -> float:
    """Zoom whose viewport (~1366 px wide) spans roughly 2×radius. z=15 ≈ 1 km, z=12 ≈ 10 km."""
    import math
    radius_km = max(0.3, min(radius_km, 500))
    return max(8.0, min(17.0, 15.0 - math.log2(radius_km)))


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    import math
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def resolve_center(page: Page, query: str, location: str) -> tuple[float, float] | None:
    """Where is `location`? Headless Maps never exposes the map centre (no @lat,lng in the URL,
    og:image is a stale default), so run the plain "query in location" search and take the
    median of the first results' coordinates — they carry !3d<lat>!4d<lng> in their links."""
    try:
        page.goto(search_url(query, location), wait_until="domcontentloaded", timeout=60000)
        _accept_consent(page)
        time.sleep(random.uniform(2, 3))
        cards = collect_place_links(page, 30)
    except (PWTimeout, CaptchaError):
        return None
    pts = [(c.lat, c.lng) for c in cards if c.lat is not None and c.lng is not None]
    if len(pts) < 3:
        return None
    lats = sorted(p[0] for p in pts)
    lngs = sorted(p[1] for p in pts)
    return lats[len(lats) // 2], lngs[len(lngs) // 2]


def center_drift_km(radius_km: float) -> float:
    """Threshold past which the median-of-results centre is treated as having drifted from
    the independently geocoded location (W117): the "searcher's own city" failure mode."""
    return max(3 * radius_km, 50)


def place_far_km(radius_km: float | None) -> float:
    """Threshold past which a PLACE (not the search centre) is too far from the geocoded
    job location to be real, used even when the job has no radius at all."""
    return max(3 * float(radius_km), 150) if radius_km else 300.0


def is_place_far_from_geocode(place_lat: float, place_lng: float, geo: "GeoHit | None",
                              radius_km: float | None) -> tuple[bool, float]:
    """(is_far, distance_km). `geo is None` (geocoding unavailable/failed) always keeps the
    place — never reject on a guard we could not compute."""
    if geo is None:
        return False, 0.0
    km = haversine_km(geo.lat, geo.lng, place_lat, place_lng)
    return km > place_far_km(radius_km), km


@dataclass
class FeedCard:
    href: str
    name: str | None = None
    rating: float | None = None
    reviews_count: int | None = None
    lat: float | None = None      # from the href's !3d!4d — lets us skip far places before visiting
    lng: float | None = None

    @property
    def key(self) -> str:
        m = PLACE_ID_RE.search(self.href) or CID_RE.search(self.href)
        return m.group(1) if m else self.href


def grid_centers(center: tuple[float, float], radius_km: float, tile_km: float) -> list[tuple[float, float]]:
    """Square grid of sub-search centres covering a circle. Maps returns ≤~120 results per
    search, so a big radius has to be scraped tile by tile."""
    import math
    lat0, lng0 = center
    step = tile_km * 1.6                           # viewports overlap a little
    out: list[tuple[float, float]] = []
    n = int(math.ceil(radius_km / step))
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            dx, dy = i * step, j * step
            if math.hypot(dx, dy) <= radius_km + tile_km / 2:
                out.append((lat0 + dy / 111.0, lng0 + dx / (111.0 * max(0.2, math.cos(math.radians(lat0))))))
    out.sort(key=lambda c: haversine_km(lat0, lng0, c[0], c[1]))   # centre first, spiral out
    return out[:150]


#: W119 (CRM T765): circles up to this radius start as ONE tile — the whole circle in one viewport — and are
#: refined by the saturation quadtree. Bigger circles keep the coarse radius/8 grid (one viewport cannot hold them).
COARSE_FIRST_MAX_KM = 16.0


def initial_tiles(center: tuple[float, float], radius_km: float) -> tuple[list[tuple[float, float]], float]:
    """(tile centres, tile_km) a tiled collection starts from."""
    if radius_km <= COARSE_FIRST_MAX_KM:
        return [center], float(radius_km)
    tile_km = max(2.0, radius_km / 8.0)
    return grid_centers(center, radius_km, tile_km), tile_km


def saturated_keywords(band: list[str], kw_hits: dict, tile: tuple[float, float], split_at: int) -> list[str]:
    """The band's keywords whose feed on `tile` came back (nearly) full, in band order — only those are worth
    searching again on the tile's four children."""
    return [q for q in band if kw_hits.get((tile, q), 0) >= split_at]


# One round-trip per sweep: for every place link in the feed, walk up to the card and read the
# "4.8 stars 1,263 Reviews" aria-label. The place panel itself no longer shows a review count in
# the layout Google serves to headless sessions, so the feed is where we get it.
_FEED_JS = """
() => {
  const feed = document.querySelector('div[role="feed"]');
  if (!feed) return [];
  const out = [];
  for (const a of feed.querySelectorAll('a[href*="/maps/place/"]')) {
    let card = a.parentElement, img = null;
    for (let i = 0; i < 8 && card; i++) {
      img = card.querySelector('span[role="img"][aria-label*="star" i], span[role="img"][aria-label*="review" i]');
      if (img) break;
      card = card.parentElement;
    }
    out.push({ href: a.href, name: a.getAttribute('aria-label'), label: img ? img.getAttribute('aria-label') : null });
  }
  return out;
}
"""


def _parse_feed_label(label: str | None) -> tuple[float | None, int | None]:
    if not label:
        return None, None
    rating = reviews = None
    m = RATING_RE.search(label)
    if m:
        try:
            rating = float(m.group(1).replace(",", "."))
        except ValueError:
            pass
    m = REVIEWS_RE.search(label)
    if m:
        try:
            reviews = int(re.sub(r"[^\d]", "", m.group(1)))
        except ValueError:
            pass
    return rating, reviews


def collect_place_links(page: Page, max_places: int,
                        on_progress: Callable[[int], None] | None = None) -> list[FeedCard]:
    """Scroll the results feed until `max_places` links or end of list."""
    links: dict[str, FeedCard] = {}
    try:
        page.wait_for_selector('div[role="feed"]', timeout=15000)
    except PWTimeout:
        if "/maps/place/" in page.url:           # Maps jumped straight to a single result
            return [FeedCard(href=page.url)]
        if _blocked(page):
            raise CaptchaError("blocked on search page")
        return []
    feed = page.locator('div[role="feed"]')

    def sweep() -> None:
        try:
            cards = page.evaluate(_FEED_JS)
        except PWTimeout:
            return
        for c in cards:
            href = c.get("href")
            if not href or href in links:
                continue
            rating, reviews = _parse_feed_label(c.get("label"))
            lat = lng = None
            m = LATLNG_3D4D.search(href)
            if m:
                lat, lng = float(m.group(1)), float(m.group(2))
            links[href] = FeedCard(href=href, name=c.get("name"), rating=rating, reviews_count=reviews, lat=lat, lng=lng)
            if len(links) >= max_places:
                break

    stale_rounds = 0
    while len(links) < max_places:
        sweep()
        if on_progress:
            on_progress(len(links))
        if len(links) >= max_places:
            break
        before = len(links)
        feed.evaluate("el => el.scrollBy(0, el.scrollHeight)")
        time.sleep(random.uniform(1.5, 3.5))
        try:
            if END_OF_LIST_RE.search(feed.inner_text(timeout=1500) or ""):
                sweep()                              # catch the last batch rendered after the marker
                break
        except PWTimeout:
            pass
        stale_rounds = stale_rounds + 1 if len(links) == before else 0
        if stale_rounds >= 6:
            log.info("feed stopped growing at %d", len(links))
            break
        if _blocked(page):
            raise CaptchaError("blocked while scrolling")
    return list(links.values())[:max_places]


_WA_HREF_RE = re.compile(r"wa\.me|wa\.link|whatsapp\.com|whatsapp://", re.I)


def _panel_whatsapp_links(page: Page) -> list[str]:
    """Any WhatsApp-ish links Google shows on the place panel (chat button, booking link)."""
    try:
        hrefs = page.locator('div[role="main"]').first.evaluate(
            "el => [...el.querySelectorAll('a[href]')].map(a => a.href)", timeout=3000)
    except PWTimeout:
        return []
    return [h for h in hrefs if h and _WA_HREF_RE.search(h)]


def scrape_place(page: Page, href: str, job_id: int, country: str) -> Place:
    page.goto(href, wait_until="domcontentloaded", timeout=45000)
    _accept_consent(page)
    if _blocked(page):
        raise CaptchaError("blocked on place page")
    try:
        page.wait_for_selector('div[role="main"] h1', timeout=15000)
    except PWTimeout:
        pass

    name = _text(page, 'div[role="main"] h1')
    category = _text(page, 'button[jsaction*="category"]')
    address = _aria_value(page, 'button[data-item-id="address"]', "Address:")
    website = clean_url(_attr(page, 'a[data-item-id="authority"]', "href"))
    plus_code = _aria_value(page, 'button[data-item-id="oloc"]', "Plus code:")
    pid = _attr(page, 'button[data-item-id^="phone:tel:"]', "data-item-id")
    phone_raw = pid.split("phone:tel:", 1)[1] if pid else _aria_value(page, 'button[data-item-id^="phone"]', "Phone:")

    # Rating comes from the stars aria-label. Review count + price range only render in the
    # full (headed) layout — Google serves a lite panel to headless sessions — so they stay
    # None under --headless and fill in under --no-headless.
    rating = reviews = None
    price_range = None
    main_html = main_text = ""
    try:
        main = page.locator('div[role="main"]').first
        main_html = main.inner_html(timeout=3000)
        main_text = main.inner_text(timeout=3000)
    except PWTimeout:
        pass
    m = RATING_RE.search(main_html)
    if m:
        try:
            rating = float(m.group(1).replace(",", "."))
        except ValueError:
            rating = None
    m = PANEL_RATING_REVIEWS_RE.search(main_text) if main_text else None
    if m:
        try:
            reviews = int(re.sub(r"[^\d]", "", m.group(2)))
            if rating is None:
                rating = float(m.group(1))
        except ValueError:
            pass
    m = PRICE_RANGE_RE.search(main_text) if main_text else None
    if m:
        price_range = m.group(1).strip()

    url = page.url
    lat = lng = None
    m = LATLNG_3D4D.search(url) or LATLNG_AT.search(url)
    if m:
        lat, lng = float(m.group(1)), float(m.group(2))
    mp = PLACE_ID_RE.search(url) or PLACE_ID_RE.search(href)
    place_id = mp.group(1) if mp else None
    mc = CID_RE.search(url) or CID_RE.search(href)
    cid = mc.group(1) if mc else None
    key = place_id or cid or hashlib.sha1(href.encode()).hexdigest()[:16]

    # Country: the address names it for foreign places ("…, United Kingdom"); Maps drops it
    # for places in the browser's own region. A service-area listing has NO address at all,
    # and falling straight through to the job's country (itself often unset → the agent's
    # default region) tagged 808 London businesses with +44 numbers as 'IN' on job #45
    # (2026-09-05). An international-format phone names its country unambiguously, so it
    # ranks above the job/default fallback — but only when it carries a '+': a national
    # number parsed against the wrong region would just launder the same mistake.
    place_country = country_from_address(address)
    if not place_country and phone_raw and phone_raw.strip().startswith("+"):
        e164_guess, _ = normalise_phone(phone_raw, country or "ZZ")
        place_country = region_of_phone(e164_guess, "") or None
    place_country = place_country or country
    phone_e164, phone_digits = normalise_phone(phone_raw, place_country)
    wa_links = _panel_whatsapp_links(page)
    wa_number = None
    wa_region = region_of_phone(phone_e164, place_country)
    for link in wa_links:
        wa_number = normalise_wa(extract_whatsapp(link), wa_region)
        if wa_number:
            break
    return Place(
        job_id=job_id, place_key=key, name=name, category=category, address=address,
        country=place_country, phone=phone_e164 or phone_raw, phone_digits=phone_digits,
        website=website, domain=domain_of(website), rating=rating, reviews_count=reviews,
        price_range=price_range,
        lat=lat, lng=lng, maps_url=url, plus_code=plus_code, place_id=place_id,
        whatsapp_number=wa_number, whatsapp_source="maps_link" if wa_number else None,
        scraped_at=now_iso(),
        raw={"cid": cid, "href": href, "phone_raw": phone_raw, "maps_wa_links": wa_links},
    )


#: Poll interval for the opener while the collector is still tiling and the queue is empty.
OPENER_POLL_SEC = 2.0
#: W120 (CRM T767): planned browser relaunch cadence. One headed Maps tab grows ~500 MB →
#: ~1.4 GB within 40 place navigations (renderer 175 → 800 MB) and a new page does not give
#: it back; a context relaunch does (~280 MB, ~2 s). Two such tabs + the enrichment Chrome
#: pushed a Windows agent into out-of-memory black screens. 0 disables.
def _device_upper() -> str:
    try:
        from webscraper.agent import DEVICE_NAME
    except Exception:                                             # noqa: BLE001
        return ""
    return DEVICE_NAME.upper()


def _per_device(name: str, generic: str, default: str) -> int:
    """T793: `<NAME>__<DEVICE>` (a CRM setting, refreshed live) > the generic env > the default."""
    try:
        from webscraper.agent import DEVICE_NAME
    except Exception:                                             # noqa: BLE001
        DEVICE_NAME = ""
    raw = (os.getenv(f"{name}__{DEVICE_NAME.upper()}") if DEVICE_NAME else None) \
        or os.getenv(generic) or default
    try:
        return max(0, int(str(raw).strip()))
    except ValueError:
        return int(default)


def opener_relaunch_every() -> int:
    return _per_device("MAPS_RELAUNCH", "MAPS_RELAUNCH_EVERY_PLACES", "40")


def collect_relaunch_every() -> int:
    # The tile cadence follows the same per-device knob at half the value (a tile is cheaper
    # than a place panel), so one CRM setting tunes both loops on a RAM-bound laptop.
    n = _per_device("MAPS_RELAUNCH", "MAPS_RELAUNCH_EVERY_TILES", "20")
    return max(1, n // 2) if os.getenv(f"MAPS_RELAUNCH__{_device_upper()}") else n


def recycle_due(count: int, every: int) -> bool:
    """True right before the (every+1)-th, (2·every+1)-th … unit — never before the first."""
    return every > 0 and count > 1 and (count - 1) % every == 0
# W103 — a place whose `page.goto` fails with a plain navigation error (net::ERR_ABORTED,
# ERR_CONNECTION_RESET/CLOSED, ERR_NETWORK_CHANGED …) is retried once after this pause,
# then skipped. Only this many skipped places IN A ROW fail the lane: one flaky request
# is a place, five back to back is the network.
NAV_RETRY_SLEEP_SEC = 3.0
MAX_CONSECUTIVE_NAV_FAILURES = 5


def _first_line(e: BaseException) -> str:
    """A Playwright error's message is the reason plus a multi-line call log; the log and
    the `skip` event want just the reason (`Page.goto: net::ERR_ABORTED at https://…`)."""
    return (str(e).strip().splitlines() or [""])[0][:200]
#: The opener's own persistent Chrome profile, beside the collector's (`settings.profile_dir`).
OPENER_PROFILE_NAME = "browser-profile-open"


def opener_profile_dir() -> Path:
    return settings.profile_dir.parent / OPENER_PROFILE_NAME


def _launch_kwargs(profile_dir: Path, headless: bool) -> dict:
    profile_dir.mkdir(parents=True, exist_ok=True)
    mark_profile_clean(profile_dir)   # T397: no "Restore pages?" after a hard agent exit
    kw: dict = dict(
        user_data_dir=str(profile_dir), headless=headless, locale="en-IN",
        viewport={"width": 1366, "height": 850},
        args=["--disable-blink-features=AutomationControlled", "--lang=en-IN", *RESTORE_BUBBLE_ARGS],
    )
    if settings.maps_proxy:
        kw["proxy"] = {"server": settings.maps_proxy}
    return kw


def _open_context(pw, launch_kwargs: dict):
    log.info("launching %s Chrome (profile %s)…", "headless" if launch_kwargs.get("headless") else "headed", launch_kwargs.get("user_data_dir"))
    c = pw.chromium.launch_persistent_context(**launch_kwargs)
    # images/fonts/media add nothing we read — skipping them cuts bandwidth ~80%
    c.route(re.compile(r"\.(png|jpe?g|gif|webp|svg|woff2?|ttf|mp4|webm)(\?|$)", re.I),
            lambda route: route.abort())
    pg = c.pages[0] if c.pages else c.new_page()
    close_blank_pages(c, keep=pg)   # T397
    pg.set_default_timeout(20000)
    return c, pg


#: W112 (CRM T608): keywords run in BANDS of this size across EVERY map area before the next
#: band starts. Category jobs carry 350–950 keywords; the old centre-major order ran all of
#: them in one area first, so an 8 h job searched ~1 of its ~89 areas (UK Health & Medical:
#: 950 keywords, 84,550 steps, <1 % covered). Now the top keywords cover the whole circle.
TOP_KEYWORDS = 25


def keyword_bands(queries: list[str], size: int = TOP_KEYWORDS) -> list[list[str]]:
    qs = [q for q in queries if q] or [""]
    return [qs[i:i + size] for i in range(0, len(qs), size)]


def plan_steps(bands: list[list[str]], centers: list, tile_km: float) -> list[tuple]:
    """(keyword, centre, tile_km, band) — band-major; within a band each centre sweeps its keywords."""
    return [(qy, c, tile_km, b) for b, band in enumerate(bands) for c in centers for qy in band]


def step_key(qy: str, c, tk: float | None, band: int) -> str:
    """Stable id of one (band, centre, tile size, keyword) search — what a re-run skips (W112)."""
    cc = "none" if c is None else f"{c[0]:.5f},{c[1]:.5f}"
    return f"{band}|{cc}|{float(tk or 0):.3f}|{qy}"


def _card_from_link(r: dict) -> FeedCard:
    return FeedCard(href=r["href"], name=r.get("name"), rating=r.get("rating"),
                    reviews_count=r.get("reviews"), lat=r.get("lat"), lng=r.get("lng"))


#: W115 (CRM T646): retries of ONE tile's persist on top of Store._write's own LOCK_RETRIES,
#: before giving up on just that tile instead of letting the lock climb out and end collection.
PERSIST_RETRIES = 4
PERSIST_RETRY_SEC = 3.0


def _persist_tile(cstore: Store, job_id: int, fresh: list, country: str | None, step: str,
                  tile: int, tiles: int, emit: Callable[[str, dict], None]) -> bool:
    """W115 (CRM T646): save this tile's stub places + links and mark its step done, retrying
    a locked DB rather than raising it into `_collect_links`'s outer `except Exception` — that
    used to end the WHOLE collection on one transient lock (job #323 on the Dell agent: 7
    "database is locked" retries in `Store._write`, then "link collection stopped ... opening
    what was found", losing every tile still to search). `Store._write` already retries each
    individual statement; this wraps the tile's two-part write (stubs + links, then the step
    marker) so a lock spanning that whole sequence still resolves with the job's OWN connections
    (worker/opener/enrich lanes on the same sqlite file) rather than aborting the run. Returns
    False only after every retry is spent, and only for this one tile — the caller carries on."""
    for attempt in range(PERSIST_RETRIES + 1):
        try:
            if fresh:
                cstore.save_stub_places(job_id, fresh, country)
                cstore.save_links(job_id, fresh)
            cstore.mark_collect_step(job_id, step)
            return True
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower():
                raise                      # a genuine schema/bind error must still surface
            if attempt == PERSIST_RETRIES:
                emit("tile_persist_failed", {"tile": tile, "tiles": tiles,
                                             "error": f"{type(e).__name__}: {str(e)[:160]}"})
                return False
            emit("tile_persist_retry", {"tile": tile, "tiles": tiles, "attempt": attempt + 1})
            time.sleep(PERSIST_RETRY_SEC * (attempt + 1))
    return False


def _collect_links(*, store_path: Path, job_id: int, queries: list[str], location: str | None,
                   limit: int, unlimited: bool, max_places: int, headless: bool, country: str,
                   emit: Callable[[str, dict], None], radius_km: float | None,
                   center: tuple[float, float] | None, known_keys: set[str] | None,
                   collect_until: float | None, collect_target: int | None,
                   stop_ev: threading.Event, pause_ev: threading.Event, result: dict,
                   geo: "GeoHit | None" = None) -> None:
    """COLLECTOR thread (W26): the tile × keyword loop, persisting every tile's cards to
    `job_links` + a stub `places` row the moment they are seen, so the opener (and, through
    it, the CRM) can start on them while the next tile is still loading.

    Runs on its own thread with its OWN Store (sqlite3 connections are thread-bound), its
    own Playwright instance and its own persistent profile. It never calls the caller's
    `should_stop` / `wait_if_paused` / `on_event` directly — those closures belong to the
    lane thread — but reads `stop_ev` / `pause_ev` (set by the opener) and pushes events
    through `emit`, which is a thread-safe queue drained on the lane thread."""
    cstore = Store(store_path)
    skipped_far = skipped_known = 0
    merged: set[str] = set()
    budget_hit = False

    def pause_gate() -> None:
        while pause_ev.is_set() and not stop_ev.is_set():
            time.sleep(1.0)

    try:
        with sync_playwright() as pw:
            kw = _launch_kwargs(settings.profile_dir, headless)
            rl = Relauncher(lambda: _open_context(pw, kw), profile_dir=settings.profile_dir,
                            on_restart=lambda where, n: emit("browser_restart", {"where": where, "attempt": n}))
            ctx, page = rl.open()
            try:
                zoom: float | None = None
                if radius_km and (center or location):
                    if not center:                     # no pinned centre from the map picker → ask Maps
                        median = resolve_center(page, queries[0], location or "")
                        if median and geo:
                            drift = haversine_km(median[0], median[1], geo.lat, geo.lng)
                            if drift > center_drift_km(radius_km):
                                # W117: Maps' own results drifted (typically to the
                                # searcher's own city) — trust the independent geocode
                                # instead so the "outside radius → far" guard below can
                                # actually catch the drifted results.
                                emit("location_drift", {
                                    "expected": {"lat": geo.lat, "lng": geo.lng,
                                                 "country": geo.country_code},
                                    "got": {"lat": median[0], "lng": median[1]},
                                    "km": round(drift, 1)})
                                center = (geo.lat, geo.lng)
                            else:
                                center = median
                        elif median:
                            center = median
                        elif geo:
                            center = (geo.lat, geo.lng)
                    if center:
                        zoom = zoom_for_radius_km(radius_km)
                        emit("center", {"lat": center[0], "lng": center[1], "zoom": zoom})
                    else:
                        emit("center_failed", {})
                    time.sleep(random.uniform(1, 2))
                # One Maps search returns ~120 places max. For "unlimited" or big asks inside a
                # radius, tile the circle with ~2 km sub-searches and merge.
                MAPS_PAGE_CAP = 110
                tiling = bool(center and radius_km) and (unlimited or max_places > MAPS_PAGE_CAP)
                # W50 — adaptive quadtree. A fixed 2 km grid capped at 150 tiles only ever
                # covered the central ~22 km of a big circle. Now: a coarse pass over the WHOLE
                # circle first (tile ≈ radius/8, ≥2 km), then any tile whose feed hit Maps'
                # ~110-result cap is split into four half-size tiles and searched again, down
                # to SPLIT_MIN_KM. Dense cities end up on fine tiles, empty land costs one search.
                SPLIT_AT = MAPS_PAGE_CAP - 10     # a feed this full was probably truncated
                SPLIT_MIN_KM = 0.5
                # W111 (CRM T606): a Maps timeout retries the tile, then skips only that tile;
                # MAX_TILE_FAILS skipped tiles in a row (Maps really down) still ends the collection.
                # W115 (CRM T646): two in-place retries now (was one), with growing backoff, and the
                # LAST retry gets a fresh page — a page that has already timed out twice tends to be
                # the thing that's stuck (detached frame, wedged renderer), not just a slow tile.
                TILE_TIMEOUT_RETRIES = 2
                MAX_TILE_FAILS = 3
                MAX_STEPS = 6000                  # (tile, keyword) pairs — safety, the time budget rules in practice
                if tiling:
                    # W119 (CRM T765): a circle of <= COARSE_FIRST_MAX_KM starts as ONE tile (the whole
                    # circle) instead of a fixed 2 km grid. A 10 km job used to be 37 tiles x every keyword
                    # (8,500 searches for a 230-keyword sector — no time cap ever finished it); most B2B
                    # keywords return far fewer than Maps' ~110-result cap over 10 km, so one search IS the
                    # full answer, and only a keyword whose feed came back full gets the quadtree treatment.
                    centers, tile_km = initial_tiles(center, float(radius_km))
                    emit("tiles", {"count": len(centers)})
                else:
                    centers = [center]
                    tile_km = float(radius_km or 0)
                # Centre-major, NOT query-major: each centre sweeps every keyword, so a
                # truncated run still leaves every keyword represented. `steps` GROWS while
                # we walk it (children of a saturated tile are appended), so index by hand.
                # W112 (CRM T608): keyword bands across every area (see TOP_KEYWORDS), and a
                # re-run of this job on this machine carries on from the steps already searched.
                bands = keyword_bands(queries)
                steps: list[tuple] = plan_steps(bands, centers, tile_km)
                done_steps = cstore.collect_steps_done(job_id)
                if done_steps and all(step_key(*st) in done_steps for st in steps):
                    cstore.clear_collect_steps(job_id)            # all searched before: a fresh pass
                    done_steps = set()
                top_left = {c0: sum(1 for qy0 in bands[0] if step_key(qy0, c0, tile_km, 0) not in done_steps)
                            for c0 in centers}
                run_t0 = time.monotonic()
                run_steps = 0

                def emit_plan() -> None:
                    emit("plan", {"steps_total": len(steps), "steps_done": len(done_steps),
                                  "areas_total": len(centers),
                                  "areas_top_done": sum(1 for v in top_left.values() if v <= 0),
                                  "top_keywords": len(bands[0]), "keywords_total": len(queries),
                                  "pace_sec": round((time.monotonic() - run_t0) / run_steps, 1) if run_steps else None})
                emit_plan()
                tile_hits: dict[tuple[float, float], int] = {}
                kw_hits: dict[tuple[tuple[float, float], str], int] = {}     # W119: fullness per (tile, keyword)
                kid_seen: set[tuple[tuple[float, float], int]] = set()       # W119: (child tile, band) already planned
                tiles_seen: set[tuple[float, float]] = set(centers)
                s_i = 0
                fail_streak = 0
                tiles_run = 0
                # W115 (CRM T646): tiles that ran out of in-place retries, grouped by band, so
                # they can be re-queued once at the end of their band instead of just vanishing.
                band_failed: dict[int, list[tuple]] = {}
                requeued: set[str] = set()
                while s_i < len(steps):
                    s_i += 1
                    qy, c, tk, band = steps[s_i - 1]
                    key = step_key(qy, c, tk, band)
                    if key in done_steps:
                        continue                                  # W112: searched in an earlier run
                    tile_zoom = zoom_for_radius_km(tk) if tiling else zoom
                    if stop_ev.is_set() or len(merged) >= limit:
                        break
                    # Stop collecting while there is still time left to actually open the
                    # places found — without this a wide radius tiles until the clock runs out.
                    if collect_until is not None and time.monotonic() >= collect_until:
                        budget_hit = True
                        emit("links_budget", {"count": len(merged), "tile": s_i,
                                              "tiles": len(steps), "reason": "time"})
                        break
                    if collect_target is not None and len(merged) >= collect_target:
                        budget_hit = True
                        emit("links_budget", {"count": len(merged), "tile": s_i,
                                              "tiles": len(steps), "reason": "enough"})
                        break
                    tiles_run += 1
                    if recycle_due(tiles_run, collect_relaunch_every()):
                        try:
                            ctx, page = rl.recycle(f"tile {s_i}/{len(steps)}")
                        except Exception as e:                        # noqa: BLE001
                            log.warning("recycle failed (%s) — taking the crash path", _first_line(e))
                            if not rl.recover(f"recycle at tile {s_i}"):
                                raise
                            ctx, page = rl.current
                        emit("browser_recycled", {"where": "collector", "count": tiles_run - 1})
                    pause_gate()
                    want = 10**6 if (tiling or unlimited or (center and radius_km)) else max_places
                    # Retry this tile through the relauncher until it reads or the relaunch
                    # cap is spent. Everything already persisted survives a crash.
                    # W111 (CRM T606): a slow Google Maps page (goto 60 s, or the results feed not
                    # showing in 20 s) used to raise out of here and end the WHOLE collection —
                    # jobs #81/#103/#125/#147/#213 each lost the rest of their tiles on 2026-09-14.
                    cards = None
                    timeouts = 0
                    while True:
                        try:
                            page.goto(search_url(qy, location, center=c, zoom=tile_zoom),
                                      wait_until="domcontentloaded", timeout=60000)
                            _accept_consent(page)
                            time.sleep(random.uniform(2, 4))
                            cards = collect_place_links(
                                page, want,
                                on_progress=lambda n: emit("links", {"count": len(merged) + n,
                                                                     "tile": s_i, "tiles": len(steps)}))
                            break
                        except PWError as e:
                            if is_closed(e):
                                if not rl.recover(f"tile {s_i}/{len(steps)}"):
                                    raise
                                ctx, page = rl.current
                                continue
                            if not isinstance(e, PWTimeout):
                                raise
                            first_line = str(e).split("\n")[0][:160]
                            timeouts += 1
                            if timeouts <= TILE_TIMEOUT_RETRIES:
                                if timeouts == TILE_TIMEOUT_RETRIES:
                                    # W115: the last in-place attempt gets a fresh page — a page
                                    # that has already timed out is often the actual problem.
                                    try:
                                        stale = page
                                        page = ctx.new_page()
                                        stale.close()
                                        emit("tile_page_recycled", {"tile": s_i, "tiles": len(steps)})
                                    except PWError:
                                        pass                       # keep the stale page, still worth a try
                                emit("tile_retry", {"tile": s_i, "tiles": len(steps), "error": first_line})
                                time.sleep(min(30.0, 5.0 * timeouts) + random.uniform(0, 3))  # growing backoff
                                continue
                            fail_streak += 1
                            if fail_streak >= MAX_TILE_FAILS:
                                raise
                            emit("tile_failed", {"tile": s_i, "tiles": len(steps), "error": first_line})
                            if key not in requeued:               # W115: give it one more shot, band-end
                                band_failed.setdefault(band, []).append(steps[s_i - 1])
                            break
                    # W115 (CRM T646): reached the end of this band (no more queued steps share its
                    # band number, or this was the last step) — re-queue anything that band lost to
                    # a timeout, once, instead of the run just carrying on without it.
                    band_end = s_i == len(steps) or steps[s_i][3] != band
                    if band_end and band in band_failed:
                        for st_ in band_failed.pop(band):
                            stk = step_key(*st_)
                            if stk not in requeued:
                                requeued.add(stk)
                                steps.append(st_)
                        emit("tiles", {"count": len(steps)})
                    if cards is None:
                        continue                          # W111: this tile skipped, the rest carry on
                    fail_streak = 0
                    fresh: list[FeedCard] = []
                    for card in cards:
                        if card.key in merged:
                            continue
                        if known_keys and card.key in known_keys:   # 'only new businesses' job
                            skipped_known += 1
                            continue
                        # pre-filter on the coordinates embedded in the link — no visit needed
                        if center and radius_km and card.lat is not None:
                            d = haversine_km(center[0], center[1], card.lat, card.lng)
                            if d > radius_km:
                                skipped_far += 1
                                continue
                        merged.add(card.key)
                        fresh.append(card)
                        if len(merged) >= limit:
                            break
                    # Persist THIS tile now: the opener takes links from job_links, and the
                    # stub rows are what the CRM shows before the panel has been read.
                    # Stub rows BEFORE the links: the opener takes a link the instant it is
                    # committed, and its fill must land on an existing row.
                    # W115 (CRM T646): a lock here used to raise straight out of the collector
                    # and end the whole run; now it retries (beyond Store's own retries) and,
                    # only if still locked, leaves this one tile unmarked (a re-run repeats it)
                    # instead of losing every tile after it.
                    persisted = _persist_tile(cstore, job_id, fresh, country, key, s_i, len(steps), emit)
                    if not persisted:
                        merged.difference_update(c.key for c in fresh)   # free them to retry if seen again
                    emit("tile", {"tile": s_i, "tiles": len(steps),
                                  "added": len(fresh) if persisted else 0, "total": len(merged)})
                    emit("links", {"count": len(merged), "tile": s_i, "tiles": len(steps)})
                    if persisted:
                        # W112: remember this step (a re-run carries on after it) and report coverage.
                        done_steps.add(key)
                    run_steps += 1
                    if band == 0 and tk == tile_km and top_left.get(c, 0) > 0:
                        top_left[c] -= 1
                    emit_plan()
                    # W50 — split a saturated tile once its last keyword has run.
                    if tiling:
                        tile_hits[c] = max(tile_hits.get(c, 0), len(cards))
                        kw_hits[(c, qy)] = len(cards)
                        last_for_tile = s_i == len(steps) or steps[s_i][1] != c
                        # W119: only the keywords whose own feed came back full are searched again on the
                        # four child tiles — a full "cleaning company" feed is no reason to re-run "bird
                        # control" four more times.
                        sat = saturated_keywords(bands[band], kw_hits, c, SPLIT_AT)
                        if (last_for_tile and sat and tk > SPLIT_MIN_KM
                                and len(steps) + 4 * len(sat) <= MAX_STEPS):
                            import math as _m
                            half = tk / 2.0
                            off = tk * 0.4                                   # children cover the parent's 1.6×tile square
                            kids = []
                            for dx, dy in ((-off, -off), (-off, off), (off, -off), (off, off)):
                                kc = (c[0] + dy / 111.0, c[1] + dx / (111.0 * max(0.2, _m.cos(_m.radians(c[0])))))
                                if haversine_km(center[0], center[1], kc[0], kc[1]) > float(radius_km) + half:
                                    continue
                                if (kc, band) in kid_seen:
                                    continue
                                kid_seen.add((kc, band))
                                tiles_seen.add(kc)
                                kids.append(kc)
                            if kids:
                                steps.extend((q2, kc, half, band) for kc in kids for q2 in sat)
                                emit("tiles", {"count": len(tiles_seen)})
                                emit("tile_split", {"tile": s_i, "hits": tile_hits[c], "from_km": round(tk, 2),
                                                    "to_km": round(half, 2), "children": len(kids), "tiles": len(tiles_seen)})
                    if len(steps) > 1:
                        time.sleep(random.uniform(1.5, 3.5))
            finally:
                rl.close()
    except Exception as e:                                        # noqa: BLE001 — reported, then re-raised by the caller
        result["error"] = e
        emit("collect_failed", {"error": f"{type(e).__name__}: {str(e)[:200]}"})
    finally:
        result.update(count=len(merged), skipped_far=skipped_far, skipped_known=skipped_known,
                      budget_hit=budget_hit)
        emit("links_done", {"count": len(merged), "skipped_far": skipped_far,
                            "skipped_known": skipped_known, "budget_hit": budget_hit})
        cstore.close()


def run_scrape(store: Store, job_id: int, query: str, location: str | None, max_places: int,
               pacing: Pacing, headless: bool | None = None, country: str | None = None,
               on_event: Callable[[str, dict], None] | None = None,
               should_stop: Callable[[], bool] | None = None,
               radius_km: float | None = None,
               wait_if_paused: Callable[[], None] | None = None,
               center: tuple[float, float] | None = None,
               known_keys: set[str] | None = None,
               collect_until: float | None = None,
               collect_target: int | None = None,
               preset_links: list[FeedCard] | None = None) -> int:
    """Scrape up to `max_places` (0 = unlimited) for (query, location) into `store`. Returns count saved.

    W26 — two Chrome tabs side by side. A COLLECTOR thread runs the tile/keyword loop and
    persists every tile's links + a stub place row as it goes; the OPENER (this thread)
    takes unopened links from `job_links` in feed order, reads each place panel and fills
    the row. The opener starts on the first tile's links while the collector is still on
    tile two — before this the whole collect phase ran first and downstream lanes idled
    (job #16: 96 min, 1,260 links, 0 places). Each thread has its own persistent profile
    (`settings.profile_dir` / `opener_profile_dir()`), its own Playwright and its own Store.

    `should_stop` is polled between places on this thread (the user's Stop, or the Maps
    deadline); when it returns True the job is marked stopped and the collector is told to
    quit. `collect_until` / `collect_target` cap the COLLECTOR only — the opener keeps
    opening until the deadline. `wait_if_paused` is called between places and may block
    (run-time window); the collector waits with it. `preset_links` = open exactly these
    (a capped run's leftovers) — no search, no centre lookup, no tiling, no collector."""
    headless = settings.headless if headless is None else headless
    country = country or settings.default_country
    emit_direct = on_event or (lambda kind, data: None)
    should_stop = should_stop or (lambda: False)
    wait_if_paused = wait_if_paused or (lambda: None)
    unlimited = max_places <= 0
    limit = 10**9 if unlimited else max_places
    queries = [q.strip() for q in (query or "").split(",") if q.strip()] or [(query or "").strip()]

    # Collector → opener event bridge. The caller's on_event closures are bound to THIS
    # thread's Store, so the collector never calls them; it queues, we drain here.
    events: "queue.Queue[tuple[str, dict]]" = queue.Queue()
    stop_ev = threading.Event()
    pause_ev = threading.Event()
    collector_done = threading.Event()
    # W117: one geocode call per job, independent of Maps — used to catch a drifted centre
    # AND, below, to guard individual places even on a job with no radius at all.
    geo = geocode_location(location) if location else None
    shared = {"center": center, "geo": geo}
    cresult: dict = {"error": None, "count": 0, "skipped_far": 0, "skipped_known": 0, "budget_hit": False}

    def drain() -> None:
        while True:
            try:
                kind, data = events.get_nowait()
            except queue.Empty:
                return
            if kind == "center":
                shared["center"] = (data["lat"], data["lng"])
            emit_direct(kind, data)

    def _set_active(flag: int) -> None:
        try:
            store.update_job(job_id, disc_active=flag)
        except Exception:                                         # noqa: BLE001 — CLI jobs may predate the column
            pass

    collector: threading.Thread | None = None
    if preset_links is not None:
        store.save_stub_places(job_id, preset_links, country)
        store.save_links(job_id, preset_links)
        emit_direct("links", {"count": len(preset_links), "tile": 1, "tiles": 1})
        emit_direct("links_done", {"count": len(preset_links), "skipped_far": 0,
                                   "skipped_known": 0, "budget_hit": False})
        cresult["count"] = len(preset_links)
        collector_done.set()
    else:
        def _run_collector() -> None:
            try:
                _collect_links(
                    store_path=store.path, job_id=job_id, queries=queries, location=location,
                    limit=limit, unlimited=unlimited, max_places=max_places, headless=headless,
                    country=country, emit=lambda k, d: events.put((k, d)), radius_km=radius_km,
                    center=center, known_keys=known_keys, collect_until=collect_until,
                    collect_target=collect_target, stop_ev=stop_ev, pause_ev=pause_ev, result=cresult,
                    geo=geo)
            finally:
                collector_done.set()
        collector = threading.Thread(target=_run_collector, name=f"maps-collect-{job_id}", daemon=True)
        collector.start()

    saved = 0
    opened = 0
    try:
        with sync_playwright() as pw:
            kw = _launch_kwargs(opener_profile_dir(), headless)
            rl = Relauncher(lambda: _open_context(pw, kw), profile_dir=opener_profile_dir(),
                            on_restart=lambda where, n: emit_direct("browser_restart", {"where": where, "attempt": n}))
            ctx, page = rl.open()
            try:
                # Places whose panel this job has ALREADY read (an earlier area / run) — a
                # second visit is reported as `dup`. Stubs are not "known" in that sense.
                known = store.detailed_place_keys(job_id)
                nav_failures = 0          # W103: skipped places in a row; reset by every place that opens
                while True:
                    drain()
                    if should_stop():
                        stop_ev.set()
                        emit_direct("abort", {"reason": "stopped by user"})
                        store.finish_job(job_id, "stopped", "stopped by user")
                        return saved
                    if saved >= limit:
                        stop_ev.set()
                        break
                    pause_ev.set()
                    try:
                        wait_if_paused()
                    finally:
                        pause_ev.clear()
                    link = store.next_pending_link(job_id)
                    if link is None:
                        if collector_done.is_set():
                            drain()
                            if store.next_pending_link(job_id) is None:
                                break
                            continue
                        time.sleep(OPENER_POLL_SEC)
                        continue
                    card = _card_from_link(link)
                    href = card.href
                    opened += 1
                    # Opened = attempted. A timeout or a far-away skip still counts: "pending"
                    # means never looked at, so a capped run's leftovers are exactly the rest.
                    try:
                        store.mark_link_opened(job_id, card.key)
                    except Exception:                                 # noqa: BLE001
                        pass
                    if recycle_due(opened, opener_relaunch_every()):
                        try:
                            ctx, page = rl.recycle(f"place {opened}")
                        except Exception as e:                        # noqa: BLE001
                            log.warning("recycle failed (%s) — taking the crash path", _first_line(e))
                            if not rl.recover(f"recycle at place {opened}"):
                                raise
                            ctx, page = rl.current
                        emit_direct("browser_recycled", {"where": "opener", "count": opened - 1})
                    _set_active(1)
                    pacing.sleep_between()
                    place = None
                    nav_retried = False
                    try:
                        while place is None:
                            try:
                                place = scrape_place(page, href, job_id, country)
                            except CaptchaError:
                                backoff = random.uniform(900, 1800)
                                emit_direct("captcha", {"backoff_sec": backoff})
                                log.warning("captcha — backing off %.0fs", backoff)
                                time.sleep(backoff)
                                try:
                                    place = scrape_place(page, href, job_id, country)
                                except CaptchaError:
                                    stop_ev.set()
                                    emit_direct("abort", {"reason": "captcha twice"})
                                    store.finish_job(job_id, "stopped", "captcha twice")
                                    return saved
                            except PWTimeout:
                                emit_direct("skip", {"href": href, "reason": "timeout"})
                                break
                            except PWError as e:
                                if is_closed(e):
                                    # Same recovery as the collector: a dead browser costs this
                                    # one place, not the remaining list and not the places
                                    # already saved.
                                    if not rl.recover(f"place {opened}"):
                                        raise
                                    ctx, page = rl.current
                                    emit_direct("skip", {"href": href, "reason": "browser restarted"})
                                    break
                                # W103: a plain navigation failure is NOT "the browser died" —
                                # `is_closed()` correctly says no, but until this fix that meant
                                # the error was re-raised and ended the whole lane: job #1625
                                # (local 54) lost 133 unopened places at 1455/1578, 7h18m in, to
                                # one `net::ERR_ABORTED`. Browser and page are still alive, so
                                # retry THIS place once, then give it up like a timeout (the link
                                # is already `opened`, the stub stays `pending`, and W98's stub
                                # re-open hands it back later). Only a run of skipped places
                                # ends the lane — that is a dead network, and it must surface.
                                if not nav_retried:
                                    nav_retried = True
                                    time.sleep(NAV_RETRY_SLEEP_SEC)
                                    continue
                                nav_failures += 1
                                log.warning("skipped %s: %s", card.name or href, _first_line(e))
                                emit_direct("skip", {"href": href, "reason": "navigation failed",
                                                     "error": _first_line(e)})
                                if nav_failures >= MAX_CONSECUTIVE_NAV_FAILURES:
                                    log.error("%d places in a row failed to open — giving up on the lane: %s",
                                              nav_failures, _first_line(e))
                                    raise
                                break
                    finally:
                        _set_active(0)
                    if place is None:
                        continue
                    nav_failures = 0
                    # feed card values fill whatever the panel didn't expose
                    if place.name is None and card.name:
                        place.name = card.name
                    if place.rating is None and card.rating is not None:
                        place.rating = card.rating
                    if place.reviews_count is None and card.reviews_count is not None:
                        place.reviews_count = card.reviews_count
                    c0 = shared["center"]
                    if c0 and place.lat is not None and place.lng is not None:
                        place.distance_km = round(haversine_km(c0[0], c0[1], place.lat, place.lng), 2)
                        if radius_km and place.distance_km > radius_km * 1.05:   # feed coords were approximate
                            cresult["skipped_far"] += 1
                            try:
                                store.drop_stub(job_id, place.place_key)     # the stub was provisional
                            except Exception:                             # noqa: BLE001
                                pass
                            emit_direct("far", {"name": place.name, "distance_km": place.distance_km,
                                                "skipped": cresult["skipped_far"]})
                            continue
                    # W117: independent of `radius_km`/`center` — a place whose pin is nowhere
                    # near the geocoded job location (typically drifted to the searcher's own
                    # city) is 'far' too, even on a job with no radius at all.
                    geo0 = shared.get("geo")
                    if geo0 and place.lat is not None and place.lng is not None:
                        is_far, geo_km = is_place_far_from_geocode(place.lat, place.lng, geo0, radius_km)
                        if is_far:
                            cresult["skipped_far"] += 1
                            cresult["skipped_drift"] = cresult.get("skipped_drift", 0) + 1
                            try:
                                store.drop_stub(job_id, place.place_key)
                            except Exception:                             # noqa: BLE001
                                pass
                            emit_direct("far", {"name": place.name, "distance_km": round(geo_km, 2),
                                                "reason": "location_drift",
                                                "skipped": cresult["skipped_far"]})
                            continue
                    if place.place_key in known:
                        emit_direct("dup", {"name": place.name})
                    place.detail_status = "done"
                    store.upsert_place(place)
                    known.add(place.place_key)
                    saved += 1
                    try:
                        offered, _ = store.link_counts(job_id)
                    except Exception:                                     # noqa: BLE001
                        offered = cresult["count"]
                    emit_direct("place", {"i": opened, "n": max(offered, opened), "saved": saved, "place": place})
                    pacing.maybe_long_pause(opened)
            finally:
                _set_active(0)
                rl.close()
    finally:
        # Whatever way we leave, the collector must not keep a Chrome open on its own.
        stop_ev.set()
        if collector is not None:
            collector.join(timeout=120)
        drain()
    if cresult.get("error") is not None and saved == 0:
        # The search itself failed (captcha on the results page, dead browser) and there
        # was nothing to open: surface it as the lane error it is, not a silent "done".
        raise cresult["error"]
    store.finish_job(job_id, "done")
    return saved
